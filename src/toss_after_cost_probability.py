"""Shadow-only after-cost probability calibration for Toss full exits.

This module is deliberately disconnected from candidate ranking, quality scores, and
order dispatch.  It joins fully closed execution outcomes to the exact BUY quality
row and fits a deterministic isotonic reliability map only when the sample gate is
met.  All production sources are opened read-only.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Iterable, Mapping, cast

from src.toss_execution_calibration import load_execution_calibration

_SCHEMA = "toss_after_cost_probability.v1"
_COST_MODEL = "decision_buffer_v1_not_broker_statement"
_EXIT_CONTRACT = "all_liquidation_single_exit_v1"
_TARGET = "net_return_pct_gt_zero"
_PILOT_ID = re.compile(r"^tlive_[A-Za-z0-9_-]{1,30}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_SAMPLES = 10_000


def _finite(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    number = float(cast(int | float, value))
    return number if math.isfinite(number) else None


def _round(value: float) -> float:
    return round(value, 6)


def _base_result() -> dict:
    return {
        "schema": _SCHEMA,
        "mode": "shadow_observability_only",
        "decision_usable": False,
        "target": _TARGET,
        "exit_contract": _EXIT_CONTRACT,
        "cost_model": _COST_MODEL,
        "model_type": "isotonic_reliability_bins_v1",
        "model_fitted": False,
        "promotion_eligible": False,
        "eligible_count": 0,
        "positive_count": 0,
        "negative_or_flat_count": 0,
        "score_conflict_count": 0,
        "excluded_counts": {},
        "raw_brier_score": None,
        "calibrated_brier_score": None,
        "bins": [],
    }


def _valid_score_row(row: object) -> tuple[str, float] | None:
    if type(row) is not dict:
        return None
    pilot_id = row.get("pilot_id")
    score = _finite(row.get("score_total"))
    schema_version = row.get("score_schema_version")
    if (
        type(pilot_id) is not str
        or _PILOT_ID.fullmatch(pilot_id) is None
        or score is None
        or not 0.0 <= score <= 100.0
        or type(row.get("side")) is not str
        or str(row.get("side")).upper() != "BUY"
        or type(row.get("quality_score_authority")) is not str
        or not str(row.get("quality_score_authority")).strip()
        or type(schema_version) is not int
        or isinstance(schema_version, bool)
        or schema_version < 1
        or type(row.get("decision_ref")) is not str
        or not str(row.get("decision_ref")).strip()
    ):
        return None
    for key in (
        "weight_profile_hash",
        "score_breakdown_sha256",
        "candidate_snapshot_sha256",
    ):
        value = row.get(key)
        if type(value) is not str or _SHA256.fullmatch(value) is None:
            return None
    return pilot_id, score


def _initial_bins(samples: list[dict], min_bin_samples: int) -> list[dict]:
    target_size = max(min_bin_samples, math.ceil(len(samples) / 10))
    score_groups: list[list[dict]] = []
    for sample in samples:
        if score_groups and score_groups[-1][0]["score"] == sample["score"]:
            score_groups[-1].append(sample)
        else:
            score_groups.append([sample])
    groups: list[list[dict]] = []
    current: list[dict] = []
    for score_group in score_groups:
        current.extend(score_group)
        if len(current) >= target_size:
            groups.append(current)
            current = []
    if current:
        if groups:
            groups[-1].extend(current)
        else:
            groups.append(current)
    return [
        {
            "samples": group,
            "weight": len(group),
            "probability": (sum(item["target"] for item in group) + 1.0)
            / (len(group) + 2.0),
        }
        for group in groups
    ]


def _isotonic_bins(samples: list[dict], min_bin_samples: int) -> list[dict]:
    blocks: list[dict] = []
    for block in _initial_bins(samples, min_bin_samples):
        blocks.append(block)
        while (
            len(blocks) >= 2
            and blocks[-2]["probability"] > blocks[-1]["probability"]
        ):
            right = blocks.pop()
            left = blocks.pop()
            weight = left["weight"] + right["weight"]
            blocks.append({
                "samples": left["samples"] + right["samples"],
                "weight": weight,
                "probability": (
                    left["probability"] * left["weight"]
                    + right["probability"] * right["weight"]
                ) / weight,
            })
    result = []
    for block in blocks:
        rows = block["samples"]
        positives = sum(row["target"] for row in rows)
        result.append({
            "score_min": _round(min(row["score"] for row in rows)),
            "score_max": _round(max(row["score"] for row in rows)),
            "sample_count": len(rows),
            "positive_count": positives,
            "empirical_probability": _round(positives / len(rows)),
            "calibrated_probability": _round(block["probability"]),
        })
    return result


def _calibrated_brier(samples: list[dict], bins: list[dict]) -> float:
    total = 0.0
    for sample in samples:
        matched = next(
            row for row in bins
            if row["score_min"] <= sample["score"] <= row["score_max"]
        )
        total += (matched["calibrated_probability"] - sample["target"]) ** 2
    return _round(total / len(samples))


def calibrate_after_cost_probability(
    calibration: object,
    score_rows: Iterable[Mapping] | object,
    *,
    min_samples: int = 20,
    min_bin_samples: int = 5,
) -> dict:
    """Fit a shadow isotonic map from quality score to positive net return."""
    if (
        type(min_samples) is not int
        or isinstance(min_samples, bool)
        or not 1 <= min_samples <= _MAX_SAMPLES
    ):
        raise ValueError("min_samples_invalid")
    if (
        type(min_bin_samples) is not int
        or isinstance(min_bin_samples, bool)
        or not 1 <= min_bin_samples <= _MAX_SAMPLES
    ):
        raise ValueError("min_bin_samples_invalid")
    result = _base_result()
    result["min_samples"] = min_samples
    result["min_bin_samples"] = min_bin_samples
    if type(calibration) is not dict:
        result.update(status="blocked", reason="calibration_contract_invalid")
        return result
    if calibration.get("cost_model") != _COST_MODEL:
        result.update(status="blocked", reason="cost_model_mismatch")
        return result
    if (
        calibration.get("mode") != "observability_only"
        or calibration.get("decision_usable") is not False
        or type(calibration.get("outcomes")) is not list
    ):
        result.update(status="blocked", reason="calibration_contract_invalid")
        return result

    raw_scores = (
        list(cast(list | tuple, score_rows))
        if type(score_rows) in (list, tuple)
        else []
    )
    scores: dict[str, float] = {}
    score_conflicts: set[str] = set()
    for raw in raw_scores:
        parsed = _valid_score_row(raw)
        if parsed is None:
            continue
        pilot_id, score = parsed
        if pilot_id in score_conflicts:
            continue
        if pilot_id in scores:
            scores.pop(pilot_id, None)
            score_conflicts.add(pilot_id)
        else:
            scores[pilot_id] = score

    excluded: Counter[str] = Counter()
    samples: list[dict] = []
    seen_outcomes: set[str] = set()
    conflicted_outcomes: set[str] = set()
    for raw in calibration["outcomes"]:
        if type(raw) is not dict:
            excluded["outcome_contract_invalid"] += 1
            continue
        pilot_id = raw.get("buy_pilot_id")
        if type(pilot_id) is not str or _PILOT_ID.fullmatch(pilot_id) is None:
            excluded["outcome_contract_invalid"] += 1
            continue
        if pilot_id in seen_outcomes or pilot_id in conflicted_outcomes:
            if pilot_id in seen_outcomes:
                seen_outcomes.remove(pilot_id)
                samples = [row for row in samples if row["pilot_id"] != pilot_id]
            conflicted_outcomes.add(pilot_id)
            excluded["outcome_join_conflict"] += 1
            continue
        seen_outcomes.add(pilot_id)
        if type(raw.get("exit_count")) is not int or raw.get("exit_count") != 1:
            excluded["exit_contract_mismatch"] += 1
            continue
        net_return = _finite(raw.get("net_return_pct"))
        if net_return is None:
            excluded["outcome_contract_invalid"] += 1
            continue
        if pilot_id in score_conflicts:
            excluded["score_join_conflict"] += 1
            continue
        score = scores.get(pilot_id)
        if score is None:
            excluded["score_lineage_missing_or_invalid"] += 1
            continue
        samples.append({
            "pilot_id": pilot_id,
            "score": score,
            "target": 1 if net_return > 0 else 0,
        })

    samples.sort(key=lambda row: (row["score"], row["pilot_id"]))
    positive_count = sum(row["target"] for row in samples)
    result.update({
        "eligible_count": len(samples),
        "positive_count": positive_count,
        "negative_or_flat_count": len(samples) - positive_count,
        "score_conflict_count": len(score_conflicts),
        "excluded_counts": dict(sorted(excluded.items())),
        "attribution_model": calibration.get("attribution_model"),
        "attribution_verified": calibration.get("attribution_verified") is True,
        "source_lineage_status": calibration.get("lineage_status"),
        "source_lineage_reasons": list(calibration.get("lineage_reasons") or []),
    })

    block_reasons = ["shadow_only"]
    if calibration.get("attribution_verified") is not True:
        block_reasons.append("attribution_unverified")
    if calibration.get("lineage_status") != "complete":
        block_reasons.append("source_lineage_incomplete")
    if len(samples) < min_samples:
        block_reasons.append("minimum_sample_not_reached")
    if positive_count in {0, len(samples)}:
        block_reasons.append("target_class_degenerate")
    if len({row["score"] for row in samples}) < 2:
        block_reasons.append("score_degenerate")
    result["promotion_block_reasons"] = block_reasons

    can_fit = (
        len(samples) >= min_samples
        and 0 < positive_count < len(samples)
        and len({row["score"] for row in samples}) >= 2
    )
    if not can_fit:
        result["status"] = (
            "insufficient_samples" if len(samples) < min_samples else "blocked"
        )
        return result

    bins = _isotonic_bins(samples, min_bin_samples)
    raw_brier = sum(
        ((row["score"] / 100.0) - row["target"]) ** 2 for row in samples
    ) / len(samples)
    result.update({
        "status": "ok",
        "model_fitted": True,
        "raw_brier_score": _round(raw_brier),
        "calibrated_brier_score": _calibrated_brier(samples, bins),
        "bins": bins,
    })
    return result


def _readonly_connection(path_value: str | Path) -> sqlite3.Connection:
    path = Path(path_value).expanduser().resolve(strict=True)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _require_exact_pilot_index(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA index_list(quality_gate_decisions)").fetchall()
    row = next((item for item in rows if str(item[1]) == "idx_qg_pilot_id_exact"), None)
    if row is None or int(row[2]) != 1 or int(row[4]) != 1:
        raise sqlite3.DatabaseError("quality_pilot_exact_index_required")
    keys = [
        str(item[2])
        for item in connection.execute(
            'PRAGMA index_xinfo("idx_qg_pilot_id_exact")'
        ).fetchall()
        if int(item[5]) == 1
    ]
    if keys != ["pilot_id"]:
        raise sqlite3.DatabaseError("quality_pilot_exact_index_required")
    sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        ("idx_qg_pilot_id_exact",),
    ).fetchone()
    sql = str(sql_row[0] or "") if sql_row else ""
    where_match = re.search(r"\bWHERE\s+(.+)$", sql, re.IGNORECASE)
    predicate = (
        re.sub(r'[\s"`\[\]\(\)]', "", where_match.group(1)).lower()
        if where_match
        else ""
    )
    if predicate != "pilot_id<>''":
        raise sqlite3.DatabaseError("quality_pilot_exact_index_required")


def load_after_cost_probability(
    *,
    events_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    quality_path: str | Path | None = None,
    min_samples: int = 20,
    min_bin_samples: int = 5,
    max_source_rows: int = 5_000,
) -> dict:
    """Load execution outcomes and exact score lineage using read-only databases."""
    calibration = load_execution_calibration(
        events_path=events_path,
        ledger_path=ledger_path,
        min_samples=min_samples,
        max_source_rows=max_source_rows,
    )
    if (
        type(calibration) is not dict
        or calibration.get("status") == "unavailable"
    ):
        result = _base_result()
        result.update({
            "status": "blocked",
            "reason": "execution_calibration_source_unavailable",
            "error_type": (
                calibration.get("error_type")
                if type(calibration) is dict
                else "ContractError"
            ),
            "min_samples": min_samples,
            "min_bin_samples": min_bin_samples,
            "promotion_block_reasons": [
                "execution_calibration_source_unavailable",
                "shadow_only",
            ],
        })
        return result
    outcomes = calibration.get("outcomes") if type(calibration) is dict else []
    pilot_id_set: set[str] = set()
    if type(outcomes) is list:
        for row in outcomes:
            if type(row) is not dict:
                continue
            pilot_id = row.get("buy_pilot_id")
            if type(pilot_id) is str:
                pilot_id_set.add(pilot_id)
    pilot_ids = sorted(pilot_id_set)
    repo_root = Path(__file__).resolve().parents[1]
    quality_db = Path(quality_path) if quality_path is not None else (
        repo_root / "db" / "data" / "toss_quality_gate.db"
    )
    score_rows: list[dict] = []
    try:
        with _readonly_connection(quality_db) as connection:
            _require_exact_pilot_index(connection)
            for start in range(0, len(pilot_ids), 900):
                chunk = pilot_ids[start:start + 900]
                if not chunk:
                    continue
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""SELECT pilot_id, score_total, decision_bucket, side,
                               quality_score_authority, score_schema_version,
                               weight_profile_hash, score_breakdown_sha256,
                               candidate_snapshot_sha256, decision_ref
                        FROM quality_gate_decisions
                        WHERE pilot_id COLLATE BINARY IN ({placeholders})
                        LIMIT ?""",
                    [*chunk, len(chunk) + 1],
                ).fetchall()
                if len(rows) > len(chunk):
                    raise sqlite3.DatabaseError("quality_score_cardinality_exceeded")
                score_rows.extend(dict(row) for row in rows)
    except (OSError, sqlite3.Error) as exc:
        result = _base_result()
        result.update({
            "status": "blocked",
            "reason": "quality_score_source_unavailable",
            "error_type": type(exc).__name__,
            "min_samples": min_samples,
            "min_bin_samples": min_bin_samples,
            "promotion_block_reasons": ["quality_score_source_unavailable", "shadow_only"],
        })
        return result
    result = calibrate_after_cost_probability(
        calibration,
        score_rows,
        min_samples=min_samples,
        min_bin_samples=min_bin_samples,
    )
    result.update({
        "source": "read_only_execution_calibration_plus_quality_gate",
        "quality_rows_loaded": len(score_rows),
        "source_outcomes_loaded": len(pilot_ids),
    })
    return result


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build shadow after-cost probability calibration"
    )
    parser.add_argument("--events-db")
    parser.add_argument("--ledger-db")
    parser.add_argument("--quality-db")
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--min-bin-samples", type=int, default=5)
    parser.add_argument("--max-source-rows", type=int, default=5_000)
    args = parser.parse_args(argv)
    result = load_after_cost_probability(
        events_path=args.events_db,
        ledger_path=args.ledger_db,
        quality_path=args.quality_db,
        min_samples=args.min_samples,
        min_bin_samples=args.min_bin_samples,
        max_source_rows=args.max_source_rows,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
