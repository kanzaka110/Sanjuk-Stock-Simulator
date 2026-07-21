"""Read-only Toss candidate quality SLO evaluation.

This module consumes the served candidate envelope only. It never imports scoring,
OAuth, broker, order, or persistence paths.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import re
import shutil
import sqlite3
import tempfile
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


_SCHEMA = "toss_buy_candidates.v3.dual_income_ev"
_LIVENESS_VERSION = "income_liveness_v1"
_STATUSES = frozenset({"healthy", "degraded", "downstream_blocked", "no_signal", "idle"})
_EXPECTED_REASON = {
    "degraded": "upstream_executable_but_no_income_ready",
    "downstream_blocked": "income_pass_but_no_final_ready",
    "no_signal": "no_income_gate_eligible_candidates",
}


def _invalid() -> ValueError:
    return ValueError("candidate_snapshot_invalid")


def _count(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise _invalid()
    return value


def _text(value: Any, *, maximum: int = 128) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise _invalid()
    return value


def evaluate_candidate_snapshot(payload: object, *, expected_market: str) -> dict[str, Any]:
    """Return a sanitized liveness summary from one authoritative served envelope."""
    if type(expected_market) is not str or expected_market not in {"KR", "US"}:
        raise _invalid()
    if type(payload) is not dict or payload.get("schema") != _SCHEMA:
        raise _invalid()

    summary = payload.get("scan_summary")
    items = payload.get("items")
    if type(summary) is not dict or type(items) is not list or len(items) > 100:
        raise _invalid()
    if any(type(item) is not dict for item in items):
        raise _invalid()
    if summary.get("market") != expected_market:
        raise _invalid()
    if summary.get("income_liveness_version") != _LIVENESS_VERSION:
        raise _invalid()

    fallback = summary.get("dependency_fallback_used")
    if type(fallback) is not bool:
        raise _invalid()
    status = summary.get("income_liveness_status")
    if type(status) is not str or status not in _STATUSES:
        raise _invalid()

    upstream = _count(summary.get("upstream_executable_count"))
    income_pass = _count(summary.get("income_pass_count"))
    income_ready = _count(summary.get("income_ready_count"))
    returned_count = _count(summary.get("returned_candidate_count"))
    returned_ready = _count(summary.get("returned_income_ready_count"))
    discovered = _count(summary.get("universe_count"))
    scanned = _count(summary.get("scanned_count"))
    held_excluded = _count(summary.get("toss_held_excluded_count"))
    risk_sell_excluded = _count(summary.get("recent_risk_sell_excluded_count"))
    quality_pass = _count(summary.get("pass_count"))
    quality_reject = _count(summary.get("reject_count"))
    executable = _count(summary.get("executable_count"))
    income_eligible = _count(summary.get("income_gate_eligible_count"))
    if (
        returned_count != len(items)
        or returned_ready > returned_count
        or income_ready < returned_ready
        or income_pass < income_ready
        or scanned > discovered
        or quality_pass + quality_reject > scanned
        or executable > quality_pass
        or income_eligible > executable
        or upstream > executable
        or income_eligible != upstream
        or income_pass > upstream
    ):
        raise _invalid()

    observed_ready = 0
    for item in items:
        ready = item.get("stock_agent_ready")
        if type(ready) is not bool:
            raise _invalid()
        income = item.get("income_strategy")
        if type(income) is not dict or type(income.get("income_pass")) is not bool:
            raise _invalid()
        if ready and income["income_pass"] is not True:
            raise _invalid()
        observed_ready += int(ready)
    if observed_ready != returned_ready:
        raise _invalid()

    valid_status = {
        "healthy": income_ready > 0,
        "degraded": upstream > 0 and income_pass == 0 and income_ready == 0,
        "downstream_blocked": income_pass > 0 and income_ready == 0,
        "no_signal": (
            upstream == 0
            and income_pass == 0
            and income_ready == 0
            and returned_count > 0
        ),
        "idle": (
            upstream == 0
            and income_pass == 0
            and income_ready == 0
            and returned_count == 0
        ),
    }[status]
    if not valid_status:
        raise _invalid()

    diagnosis = summary.get("income_liveness_diagnosis")
    reasons: list[dict[str, Any]] = []
    if status in {"healthy", "idle"}:
        if diagnosis is not None:
            raise _invalid()
    else:
        if type(diagnosis) is not dict:
            raise _invalid()
        expected_reason = _EXPECTED_REASON[status]
        if diagnosis.get("reason") != expected_reason:
            raise _invalid()
        for key, expected in (
            ("upstream_executable_count", upstream),
            ("income_pass_count", income_pass),
            ("income_ready_count", income_ready),
        ):
            if _count(diagnosis.get(key)) != expected:
                raise _invalid()

        raw_reasons = diagnosis.get("top_income_block_reasons")
        if type(raw_reasons) is not list or len(raw_reasons) > 5:
            raise _invalid()
        for row in raw_reasons:
            if type(row) is not dict or set(row) != {"reason", "count"}:
                raise _invalid()
            reasons.append({"reason": _text(row["reason"]), "count": _count(row["count"])})

    return {
        "market": expected_market,
        "status": status,
        "dependency_fallback_used": fallback,
        "candidate_count": returned_count,
        "upstream_executable_count": upstream,
        "income_pass_count": income_pass,
        "ready_count": returned_ready,
        "funnel": {
            "discovered": discovered,
            "scanned": scanned,
            "held_excluded": held_excluded,
            "recent_risk_sell_excluded": risk_sell_excluded,
            "quality_pass": quality_pass,
            "quality_reject": quality_reject,
            "executable": executable,
            "income_eligible": income_eligible,
            "income_pass": income_pass,
            "ready": returned_ready,
            "returned": returned_count,
        },
        "top_block_reasons": reasons,
    }


_RUN_KEYS = frozenset({"source", "dataset", "status", "completed_at", "error_type"})
_RUN_STATUSES = frozenset({"success", "partial", "failed", "skipped"})
_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_ERROR_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_PRIMARY_CONTRACTS = (
    ("kis", "domestic_investor_flow"),
    ("kis", "domestic_orderbook"),
)
_PRIMARY_MAX_AGE_SECONDS = {
    ("kis", "domestic_investor_flow"): 96 * 60 * 60,
    ("kis", "domestic_orderbook"): 30 * 60 * 60,
}
_OPTIONAL_CONTRACTS = (("krx_openapi", "domestic_eod_quote"),)
_FALLBACK_CONTRACTS = {
    ("naver", "domestic_investor_flow"): ("kis", "domestic_investor_flow"),
}


def _source_invalid() -> ValueError:
    return ValueError("source_run_health_invalid")


def _run_time(value: object) -> datetime:
    if type(value) is not str or len(value) > 40:
        raise _source_invalid()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _source_invalid() from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _source_invalid()
    return parsed.astimezone(timezone.utc)


def evaluate_source_run_health(
    rows: object,
    *,
    as_of_utc: datetime | None = None,
) -> dict[str, Any]:
    """Separate primary degradation, freshness, and explicit fallback success."""
    if type(rows) is not list or len(rows) > 10_000:
        raise _source_invalid()
    if as_of_utc is None:
        as_of = datetime.now(timezone.utc)
    elif (
        not isinstance(as_of_utc, datetime)
        or as_of_utc.tzinfo is None
        or as_of_utc.utcoffset() is None
    ):
        raise _source_invalid()
    else:
        as_of = as_of_utc.astimezone(timezone.utc)

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for append_index, row in enumerate(rows):
        if type(row) is not dict or set(row) != _RUN_KEYS:
            raise _source_invalid()
        source = row.get("source")
        dataset = row.get("dataset")
        status = row.get("status")
        error = row.get("error_type")
        if (
            type(source) is not str
            or not _NAME_RE.fullmatch(source)
            or type(dataset) is not str
            or not _NAME_RE.fullmatch(dataset)
            or type(status) is not str
            or status not in _RUN_STATUSES
            or type(error) is not str
            or (error and not _ERROR_RE.fullmatch(error))
            or (status == "success" and error)
            or (status in {"failed", "partial"} and not error)
        ):
            raise _source_invalid()
        normalized = dict(row)
        normalized["_completed"] = _run_time(row["completed_at"])
        normalized["_append_index"] = append_index
        if normalized["_completed"] > as_of:
            raise _source_invalid()
        grouped.setdefault((source, dataset), []).append(normalized)

    primary_missing: list[dict[str, str]] = []
    primary_failures: list[dict[str, Any]] = []
    for contract in _PRIMARY_CONTRACTS:
        values = grouped.get(contract, [])
        if not values:
            primary_missing.append({"source": contract[0], "dataset": contract[1]})
            continue
        if values[-1]["status"] == "success":
            continue
        consecutive = 0
        for row in reversed(values):
            if row["status"] == "success":
                break
            consecutive += 1
        latest = values[-1]
        primary_failures.append(
            {
                "source": contract[0],
                "dataset": contract[1],
                "status": latest["status"],
                "error_type": latest["error_type"],
                "consecutive_non_success": consecutive,
            }
        )

    stale_sources: list[dict[str, Any]] = []
    from core.market_hours import is_kr_market_open

    enforce_freshness = is_kr_market_open(as_of) is True
    for contract, max_age_seconds in _PRIMARY_MAX_AGE_SECONDS.items():
        values = grouped.get(contract, [])
        if not enforce_freshness or not values or values[-1]["status"] != "success":
            continue
        age_seconds = int((as_of - values[-1]["_completed"]).total_seconds())
        if age_seconds < 0:
            raise _source_invalid()
        if age_seconds > max_age_seconds:
            stale_sources.append(
                {
                    "source": contract[0],
                    "dataset": contract[1],
                    "age_seconds": age_seconds,
                    "max_age_seconds": max_age_seconds,
                }
            )

    active_fallbacks: list[dict[str, str]] = []
    for fallback, primary in _FALLBACK_CONTRACTS.items():
        fallback_rows = grouped.get(fallback, [])
        primary_rows = grouped.get(primary, [])
        if (
            fallback_rows
            and fallback_rows[-1]["status"] == "success"
            and primary_rows
            and primary_rows[-1]["status"] != "success"
            and fallback_rows[-1]["_append_index"] > primary_rows[-1]["_append_index"]
        ):
            active_fallbacks.append(
                {
                    "source": fallback[0],
                    "dataset": fallback[1],
                    "primary_source": primary[0],
                }
            )

    coverage_gaps = [
        {"source": source, "dataset": dataset}
        for source, dataset in _OPTIONAL_CONTRACTS
        if not grouped.get((source, dataset))
        or grouped[(source, dataset)][-1]["status"] != "success"
    ]
    if primary_missing or primary_failures or stale_sources:
        status = "degraded"
    elif coverage_gaps:
        status = "coverage_gap"
    else:
        status = "healthy"
    return {
        "status": status,
        "primary_failures": primary_failures,
        "primary_missing": primary_missing,
        "active_fallbacks": active_fallbacks,
        "coverage_gaps": coverage_gaps,
        "stale_sources": stale_sources,
    }


_MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024


def _family_signature(path: Path) -> tuple[tuple[str, int, int, int] | None, ...]:
    signature: list[tuple[str, int, int, int] | None] = []
    for member in (path, Path(f"{path}-wal")):
        try:
            stat = member.stat()
        except FileNotFoundError:
            signature.append(None)
        else:
            signature.append((member.name, stat.st_ino, stat.st_size, stat.st_mtime_ns))
    return tuple(signature)


@contextmanager
def _open_stable_read_snapshot(path: Path) -> Iterator[sqlite3.Connection]:
    """Open only a stable temporary copy of main+WAL; never the live DB family."""
    last_error: Exception | None = None
    with tempfile.TemporaryDirectory(prefix="stock-quality-snapshot-") as directory:
        snapshot = Path(directory) / "snapshot.db"
        for _attempt in range(3):
            before = _family_signature(path)
            sizes = [entry[2] for entry in before if entry is not None]
            if not sizes or sum(sizes) > _MAX_SNAPSHOT_BYTES:
                raise ValueError("read_snapshot_invalid")
            snapshot.unlink(missing_ok=True)
            Path(f"{snapshot}-wal").unlink(missing_ok=True)
            try:
                shutil.copyfile(path, snapshot)
                if before[1] is not None:
                    shutil.copyfile(Path(f"{path}-wal"), Path(f"{snapshot}-wal"))
            except (FileNotFoundError, OSError) as exc:
                last_error = exc
                continue
            if _family_signature(path) == before:
                break
        else:
            raise ValueError("read_snapshot_unstable") from last_error

        uri = f"{snapshot.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=0.75)
        try:
            connection.execute("PRAGMA query_only = ON")
            yield connection
        finally:
            connection.close()


def load_source_runs_read_only(db_path: str | Path, *, limit: int = 10_000) -> list[dict[str, str]]:
    """Read a bounded collection-run window without constructing a writable store."""
    path = Path(db_path)
    if not path.is_file() or type(limit) is not int or not 1 <= limit <= 10_000:
        raise ValueError("source_run_db_invalid")
    with _open_stable_read_snapshot(path) as connection:
        rows = connection.execute(
            """SELECT source,dataset,status,completed_at,error_type
               FROM (
                   SELECT id,source,dataset,status,completed_at,error_type
                   FROM collection_runs ORDER BY id DESC LIMIT ?
               ) ORDER BY id""",
            (limit,),
        ).fetchall()
    return [
        {
            "source": str(row[0]),
            "dataset": str(row[1]),
            "status": str(row[2]),
            "completed_at": str(row[3]),
            "error_type": str(row[4]),
        }
        for row in rows
    ]


def load_consecutive_zero_ready_read_only(
    db_path: str | Path,
    *,
    row_limit: int = 2_000,
    as_of_utc: datetime | None = None,
) -> dict[str, int]:
    """Count complete current-version final cohorts since the latest ready candidate."""
    import json

    path = Path(db_path)
    if not path.is_file() or type(row_limit) is not int or not 1 <= row_limit <= 10_000:
        raise ValueError("shadow_liveness_db_invalid")
    if as_of_utc is None:
        as_of = datetime.now(timezone.utc)
    elif (
        not isinstance(as_of_utc, datetime)
        or as_of_utc.tzinfo is None
        or as_of_utc.utcoffset() is None
    ):
        raise ValueError("shadow_liveness_row_invalid")
    else:
        as_of = as_of_utc.astimezone(timezone.utc)
    with _open_stable_read_snapshot(path) as connection:
        rows = connection.execute(
            """SELECT decided_at_utc,feature_set_version,features_json
               FROM (
                   SELECT id,decided_at_utc,feature_set_version,features_json
                   FROM shadow_decisions ORDER BY id DESC LIMIT ?
               ) ORDER BY id""",
            (row_limit,),
        ).fetchall()

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for decided_at, version, features_json in rows:
        if version != "toss_final_candidate_v2_dual_income_ev":
            continue
        if type(features_json) is not str or len(features_json.encode("utf-8")) > 64 * 1024:
            raise ValueError("shadow_liveness_row_invalid")
        try:
            features = json.loads(features_json)
        except (TypeError, json.JSONDecodeError):
            raise ValueError("shadow_liveness_row_invalid") from None
        if type(features) is not dict:
            raise ValueError("shadow_liveness_row_invalid")
        market = features.get("market_scope")
        position = features.get("cohort_position")
        size = features.get("cohort_size")
        final_state = features.get("final_state")
        bucket = features.get("production_bucket")
        if (
            market not in {"KR", "US"}
            or type(position) is not int
            or type(size) is not int
            or not 1 <= size <= 10
            or not 0 <= position < size
            or type(final_state) is not dict
            or type(final_state.get("stock_agent_ready")) is not bool
            or bucket not in {
                "PASS_EXECUTE",
                "SMALL_PASS",
                "WAIT_PULLBACK",
                "WATCH",
                "CHASE_BLOCK",
                "BLOCK",
            }
        ):
            raise ValueError("shadow_liveness_row_invalid")
        missing_fields = final_state.get("missing_fields", [])
        blocking_flags = final_state.get("blocking_risk_flags", [])
        limit_exceeded = final_state.get("limit_exceeded", False)
        execution_status = final_state.get("execution_status", "")
        if (
            type(missing_fields) is not list
            or type(blocking_flags) is not list
            or type(limit_exceeded) is not bool
            or type(execution_status) is not str
        ):
            raise ValueError("shadow_liveness_row_invalid")
        pre_income_blocked = execution_status in {
            "hold_risk_flags",
            "chase_block",
            "data_quality_block",
            "cash_unavailable",
            "quality_finalization_failed",
            "toss_snapshot_stale",
        }
        eligible = (
            bucket in {"PASS_EXECUTE", "SMALL_PASS"}
            and not missing_fields
            and not limit_exceeded
            and not blocking_flags
            and not pre_income_blocked
        )
        if final_state["stock_agent_ready"] and not eligible:
            raise ValueError("shadow_liveness_row_invalid")
        when = _run_time(decided_at)
        if when > as_of:
            raise ValueError("shadow_liveness_row_invalid")
        key = (market, when.isoformat())
        group = groups.setdefault(
            key,
            {
                "market": market,
                "when": when,
                "size": size,
                "positions": set(),
                "ready": False,
                "eligible": False,
            },
        )
        if group["size"] != size or position in group["positions"]:
            raise ValueError("shadow_liveness_row_invalid")
        group["positions"].add(position)
        group["ready"] = group["ready"] or final_state["stock_agent_ready"]
        group["eligible"] = group["eligible"] or eligible

    result = {"KR": 0, "US": 0}
    for market in ("KR", "US"):
        complete = [
            group
            for group in groups.values()
            if group["market"] == market and group["positions"] == set(range(group["size"]))
        ]
        complete.sort(key=lambda group: group["when"], reverse=True)
        for group in complete:
            if not group["eligible"]:
                continue
            if group["ready"]:
                break
            result[market] += 1
    return result


def _aware_generated(value: object) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("quality_report_invalid")
    return value.astimezone(timezone.utc).isoformat()


def build_quality_report(
    *,
    candidate_snapshots: object,
    source_health: object,
    consecutive_zero_ready: object,
    generated_at_utc: datetime,
) -> dict[str, Any]:
    """Combine sanitized market and source health without changing execution authority."""
    if type(candidate_snapshots) is not list or not candidate_snapshots:
        raise ValueError("quality_report_invalid")
    if any(type(item) is not dict for item in candidate_snapshots):
        raise ValueError("quality_report_invalid")
    markets = [item.get("market") for item in candidate_snapshots]
    if len(set(markets)) != len(markets) or any(market not in {"KR", "US"} for market in markets):
        raise ValueError("quality_report_invalid")
    if type(source_health) is not dict:
        raise ValueError("quality_report_invalid")
    if type(consecutive_zero_ready) is not dict or set(consecutive_zero_ready) != {"KR", "US"}:
        raise ValueError("quality_report_invalid")
    if any(type(value) is not int or value < 0 for value in consecutive_zero_ready.values()):
        raise ValueError("quality_report_invalid")

    market_statuses: dict[str, str] = {}
    candidate_fallback_markets: list[str] = []
    for item in candidate_snapshots:
        market = item.get("market")
        market_status = item.get("status")
        fallback_used = item.get("dependency_fallback_used")
        if market_status not in _STATUSES or type(fallback_used) is not bool:
            raise ValueError("quality_report_invalid")
        market_statuses[market] = market_status
        if fallback_used:
            candidate_fallback_markets.append(market)
    market_degraded = any(
        status in {"degraded", "downstream_blocked"}
        for status in market_statuses.values()
    )
    effective_zero = {
        market: consecutive_zero_ready[market]
        if market_statuses.get(market) in {"degraded", "downstream_blocked"}
        else 0
        for market in ("KR", "US")
    }
    source_status = source_health.get("status")
    if source_status not in {"healthy", "degraded", "coverage_gap"}:
        raise ValueError("quality_report_invalid")
    repeated_zero = any(value >= 3 for value in effective_zero.values())
    if market_degraded or candidate_fallback_markets or source_status == "degraded" or repeated_zero:
        status = "degraded"
    elif source_status == "coverage_gap":
        status = "coverage_gap"
    else:
        status = "healthy"
    return {
        "schema": "toss_quality_slo.v1",
        "generated_at_utc": _aware_generated(generated_at_utc),
        "status": status,
        "decision_usable": False,
        "markets": candidate_snapshots,
        "sources": source_health,
        "candidate_dependency_fallback_markets": candidate_fallback_markets,
        "consecutive_zero_ready": effective_zero,
        "observed_consecutive_zero_ready": dict(consecutive_zero_ready),
    }


def _render_name(value: object) -> str:
    if type(value) is not str or not _NAME_RE.fullmatch(value):
        raise ValueError("quality_report_invalid")
    return value


def _render_error(value: object) -> str:
    if type(value) is not str or (value and not _ERROR_RE.fullmatch(value)):
        raise ValueError("quality_report_invalid")
    return value


_MARKET_SUMMARY_KEYS = frozenset(
    {
        "market",
        "status",
        "dependency_fallback_used",
        "candidate_count",
        "upstream_executable_count",
        "income_pass_count",
        "ready_count",
        "funnel",
        "top_block_reasons",
    }
)
_FUNNEL_KEYS = frozenset(
    {
        "discovered",
        "scanned",
        "held_excluded",
        "recent_risk_sell_excluded",
        "quality_pass",
        "quality_reject",
        "executable",
        "income_eligible",
        "income_pass",
        "ready",
        "returned",
    }
)
_REPORT_KEYS = frozenset(
    {
        "schema",
        "generated_at_utc",
        "status",
        "decision_usable",
        "markets",
        "sources",
        "candidate_dependency_fallback_markets",
        "consecutive_zero_ready",
        "observed_consecutive_zero_ready",
    }
)


def _validate_render_market(row: object) -> None:
    if type(row) is not dict or set(row) != _MARKET_SUMMARY_KEYS:
        raise ValueError("quality_report_invalid")
    market = row.get("market")
    status = row.get("status")
    fallback = row.get("dependency_fallback_used")
    counts = (
        row.get("candidate_count"),
        row.get("upstream_executable_count"),
        row.get("income_pass_count"),
        row.get("ready_count"),
    )
    funnel = row.get("funnel")
    reasons = row.get("top_block_reasons")
    if (
        market not in {"KR", "US"}
        or status not in _STATUSES
        or type(fallback) is not bool
        or any(type(value) is not int or value < 0 for value in counts)
        or type(funnel) is not dict
        or set(funnel) != _FUNNEL_KEYS
        or any(type(value) is not int or value < 0 for value in funnel.values())
        or type(reasons) is not list
        or len(reasons) > 5
    ):
        raise ValueError("quality_report_invalid")
    assert all(type(value) is int for value in counts)
    candidate_count, upstream, income_pass, ready = counts
    if (
        funnel["returned"] != candidate_count
        or funnel["income_eligible"] != upstream
        or funnel["income_pass"] != income_pass
        or funnel["ready"] != ready
        or ready > income_pass
        or income_pass > upstream
    ):
        raise ValueError("quality_report_invalid")
    expected_status = {
        "healthy": ready > 0,
        "degraded": upstream > 0 and income_pass == 0 and ready == 0,
        "downstream_blocked": income_pass > 0 and ready == 0,
        "no_signal": upstream == 0 and income_pass == 0 and ready == 0 and candidate_count > 0,
        "idle": upstream == 0 and income_pass == 0 and ready == 0 and candidate_count == 0,
    }[status]
    if not expected_status:
        raise ValueError("quality_report_invalid")
    for reason in reasons:
        if (
            type(reason) is not dict
            or set(reason) != {"reason", "count"}
            or type(reason.get("count")) is not int
            or reason["count"] < 0
        ):
            raise ValueError("quality_report_invalid")
        _render_name(reason.get("reason"))


def render_alert(report: object) -> str:
    """Render an anomaly-only Telegram-safe message after full nested validation."""
    if (
        type(report) is not dict
        or set(report) != _REPORT_KEYS
        or report.get("schema") != "toss_quality_slo.v1"
        or report.get("decision_usable") is not False
    ):
        raise ValueError("quality_report_invalid")
    status = report.get("status")
    zero = report.get("consecutive_zero_ready")
    observed_zero = report.get("observed_consecutive_zero_ready")
    sources = report.get("sources")
    markets = report.get("markets")
    candidate_fallbacks = report.get("candidate_dependency_fallback_markets")
    generated = report.get("generated_at_utc")
    if (
        status not in {"healthy", "degraded", "coverage_gap"}
        or type(zero) is not dict
        or set(zero) != {"KR", "US"}
        or type(observed_zero) is not dict
        or set(observed_zero) != {"KR", "US"}
        or type(sources) is not dict
        or set(sources)
        != {
            "status",
            "primary_failures",
            "primary_missing",
            "active_fallbacks",
            "coverage_gaps",
            "stale_sources",
        }
        or sources.get("status") not in {"healthy", "degraded", "coverage_gap"}
        or type(markets) is not list
        or not 1 <= len(markets) <= 2
        or type(candidate_fallbacks) is not list
        or len(candidate_fallbacks) > 2
        or len(set(candidate_fallbacks)) != len(candidate_fallbacks)
        or any(market not in {"KR", "US"} for market in candidate_fallbacks)
        or type(generated) is not str
    ):
        raise ValueError("quality_report_invalid")
    for value in (*zero.values(), *observed_zero.values()):
        if type(value) is not int or value < 0:
            raise ValueError("quality_report_invalid")
    for market_row in markets:
        _validate_render_market(market_row)
    try:
        generated_at = datetime.fromisoformat(generated)
    except ValueError:
        raise ValueError("quality_report_invalid") from None
    if generated_at.tzinfo is None or generated_at.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("quality_report_invalid")
    rebuilt = build_quality_report(
        candidate_snapshots=markets,
        source_health=sources,
        consecutive_zero_ready=observed_zero,
        generated_at_utc=generated_at,
    )
    if rebuilt != report:
        raise ValueError("quality_report_invalid")

    failures = sources.get("primary_failures")
    missing = sources.get("primary_missing")
    fallbacks = sources.get("active_fallbacks")
    gaps = sources.get("coverage_gaps")
    stale = sources.get("stale_sources")
    if not all(
        type(value) is list and len(value) <= 10
        for value in (failures, missing, fallbacks, gaps, stale)
    ):
        raise ValueError("quality_report_invalid")
    assert isinstance(failures, list)
    assert isinstance(missing, list)
    assert isinstance(fallbacks, list)
    assert isinstance(gaps, list)
    assert isinstance(stale, list)

    for row in failures:
        if (
            type(row) is not dict
            or set(row)
            != {"source", "dataset", "status", "error_type", "consecutive_non_success"}
            or row.get("status") not in _RUN_STATUSES - {"success"}
            or type(row.get("consecutive_non_success")) is not int
            or row["consecutive_non_success"] <= 0
        ):
            raise ValueError("quality_report_invalid")
        _render_name(row.get("source"))
        _render_name(row.get("dataset"))
        _render_error(row.get("error_type"))
    for row in missing:
        if type(row) is not dict or set(row) != {"source", "dataset"}:
            raise ValueError("quality_report_invalid")
        _render_name(row.get("source"))
        _render_name(row.get("dataset"))
    for row in fallbacks:
        if type(row) is not dict or set(row) != {"source", "dataset", "primary_source"}:
            raise ValueError("quality_report_invalid")
        _render_name(row.get("source"))
        _render_name(row.get("dataset"))
        _render_name(row.get("primary_source"))
    for row in stale:
        if (
            type(row) is not dict
            or set(row) != {"source", "dataset", "age_seconds", "max_age_seconds"}
            or type(row.get("age_seconds")) is not int
            or row["age_seconds"] < 0
            or type(row.get("max_age_seconds")) is not int
            or row["max_age_seconds"] <= 0
        ):
            raise ValueError("quality_report_invalid")
        _render_name(row.get("source"))
        _render_name(row.get("dataset"))
    for row in gaps:
        if type(row) is not dict or set(row) != {"source", "dataset"}:
            raise ValueError("quality_report_invalid")
        _render_name(row.get("source"))
        _render_name(row.get("dataset"))

    anomalies_present = bool(
        candidate_fallbacks
        or failures
        or missing
        or fallbacks
        or gaps
        or stale
        or any(zero[market] >= 3 for market in ("KR", "US"))
    )
    if status == "healthy":
        if anomalies_present:
            raise ValueError("quality_report_invalid")
        return ""

    lines = [f"[Stock Quality SLO] {status.upper()}"]
    for market in ("KR", "US"):
        if zero[market] >= 3:
            lines.append(f"- {market} ready=0 {zero[market]}회 연속")
    for market in candidate_fallbacks:
        lines.append(f"- {market} dependency fallback 활성")
    for row in failures:
        detail = row["error_type"] or row["status"]
        lines.append(f"- {row['source'].upper()} {row['dataset']} {detail}")
    for row in missing:
        lines.append(f"- {row['source'].upper()} {row['dataset']} missing")
    for row in fallbacks:
        lines.append(f"- {row['source'].title()} fallback 활성")
    for row in stale:
        lines.append(f"- {row['source'].upper()} {row['dataset']} stale")
    for row in gaps:
        lines.append(f"- {row['source'].upper()} {row['dataset']} coverage gap")
    lines.append("- score·gate·주문 변경 없음")
    message = "\n".join(lines)
    if len(message) > 4000:
        raise ValueError("quality_report_invalid")
    return message


def _loopback_base_url(value: object) -> str:
    if type(value) is not str or len(value) > 128:
        raise ValueError("base_url_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("base_url_invalid") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port is None
        or not 1 <= port <= 65535
    ):
        raise ValueError("base_url_invalid")
    return value.rstrip("/")


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


def _fetch_json(url: str) -> dict[str, Any]:
    request = Request(url, method="GET", headers={"Accept": "application/json"})
    opener = build_opener(ProxyHandler({}), _RejectRedirects())
    with opener.open(request, timeout=15) as response:
        status = getattr(response, "status", None)
        content_type = str(response.headers.get("Content-Type", "")).lower()
        if status != 200 or "application/json" not in content_type:
            raise RuntimeError("candidate_get_failed")
        body = response.read(2_000_001)
    if len(body) > 2_000_000:
        raise RuntimeError("candidate_get_too_large")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("candidate_get_malformed") from None
    if type(value) is not dict:
        raise RuntimeError("candidate_get_malformed")
    return value


def run_quality_watchdog(
    *,
    base_url: str,
    source_db: str | Path,
    shadow_db: str | Path,
    fetch_json: Callable[[str], object] = _fetch_json,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Run the complete read-only quality watchdog against served state."""
    base = _loopback_base_url(base_url)
    if not callable(fetch_json) or (clock is not None and not callable(clock)):
        raise ValueError("quality_watchdog_invalid")
    snapshots = []
    for market in ("KR", "US"):
        payload = fetch_json(
            f"{base}/api/toss/buy-candidates?limit=20&market={market}"
        )
        snapshots.append(evaluate_candidate_snapshot(payload, expected_market=market))
    now = clock() if clock is not None else datetime.now(timezone.utc)
    source_health = evaluate_source_run_health(
        load_source_runs_read_only(source_db),
        as_of_utc=now,
    )
    zero_ready = load_consecutive_zero_ready_read_only(shadow_db, as_of_utc=now)
    return build_quality_report(
        candidate_snapshots=snapshots,
        source_health=source_health,
        consecutive_zero_ready=zero_ready,
        generated_at_utc=now,
    )


def _default_paths() -> tuple[Path, Path]:
    from config.settings import DB_DIR

    root = Path(DB_DIR)
    return root / "source_observations_v2.db", root / "shadow_measurements.db"


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Toss data-quality watchdog")
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--source-db")
    parser.add_argument("--shadow-db")
    parser.add_argument("--alert-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        default_source, default_shadow = _default_paths()
        report = run_quality_watchdog(
            base_url=args.base_url,
            source_db=Path(args.source_db) if args.source_db else default_source,
            shadow_db=Path(args.shadow_db) if args.shadow_db else default_shadow,
        )
        if args.alert_only:
            alert = render_alert(report)
            if alert:
                print(alert)
        else:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        if args.alert_only:
            print(f"[Stock Quality SLO] BLOCK\n- runtime {type(exc).__name__}\n- score·gate·주문 변경 없음")
        else:
            print(json.dumps({"status": "blocked", "error_type": type(exc).__name__}, sort_keys=True, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
