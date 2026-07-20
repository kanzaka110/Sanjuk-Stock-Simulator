"""Read-only reconstruction of realized Toss auto-trade outcomes.

The module accepts sanitized broker-fill rows, or loads a bounded SQLite window with
``mode=ro`` plus ``query_only``.  It never sends an order.  Auto-pipeline BUY fills
are joined to protective/position-review SELL fills FIFO per symbol, and only fully
closed lifecycles are reported as calibration outcomes.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import math
import re
from pathlib import Path
import sqlite3
from typing import Iterable, Mapping, cast

_SCHEMA = "toss_execution_calibration.v1"
_OBSERVABILITY_CONTRACT = {
    "schema": _SCHEMA,
    "mode": "observability_only",
    "decision_usable": False,
    "decision_block_reason": "lifecycle_transition_model_unvalidated",
    "attribution_model": "symbol_fifo_v1",
    "attribution_verified": False,
    "cost_model": "decision_buffer_v1_not_broker_statement",
}
_BUY_REASONS = frozenset({"auto_pipeline"})
_SELL_REASONS = frozenset({"position_review_sell", "auto_exit_sell"})
_REAL_LIVE_EVENT_TYPES = frozenset({"live_sent", "autonomous_live_sent"})
_VERIFIED_FX_SOURCES = frozenset({"broker_order_preview"})
_CLIENT_ORDER_ID = re.compile(r"^tlive_[A-Za-z0-9_-]{1,30}$")
_KRX_SYMBOL = re.compile(r"^(\d{6})(?:\.(KS|KQ))?$")
_US_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,15}$")
_MAX_FILL_QUANTITY = 1_000_000
_MAX_FILL_PRICE = 1_000_000_000_000.0
_MAX_ESTIMATED_NOTIONAL_KRW = 1_000_000_000_000_000.0
_MIN_ESTIMATE_RATIO = 0.95
_MAX_ESTIMATE_RATIO = 1.05
_MIN_VERIFIED_USD_KRW = 500.0
_MAX_VERIFIED_USD_KRW = 3_000.0


def _canonical_symbol(value: str) -> str:
    symbol = value.upper().strip()
    match = _KRX_SYMBOL.fullmatch(symbol)
    return match.group(1) if match else symbol


def _krx_suffix(value: str) -> str | None:
    match = _KRX_SYMBOL.fullmatch(value.upper().strip())
    return match.group(2) if match else None


def _positive_number(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(cast(int | float, value))
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _timestamp(value: object) -> datetime | None:
    if type(value) is not str or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _production_envelope(row: object) -> bool:
    return (
        type(row) is dict
        and type(row.get("event_type")) is str
        and row.get("event_type") in _REAL_LIVE_EVENT_TYPES
        and type(row.get("live_order_sent")) is int
        and row.get("live_order_sent") == 1
        and type(row.get("adapter_status")) is str
        and row.get("adapter_status") == "enabled"
        and type(row.get("live_order_allowed")) is int
        and row.get("live_order_allowed") == 1
    )


def _production_like_fill(row: object) -> bool:
    if type(row) is not dict or not _production_envelope(row):
        return False
    return (
        type(row.get("broker_order_status")) is str
        and row.get("broker_order_status") == "FILLED"
        and type(row.get("side")) is str
        and row.get("side") in {"buy", "sell"}
    )


def _malformed_production_fill(row: object) -> bool:
    if type(row) is not dict or not _production_envelope(row):
        return False
    status = row.get("broker_order_status")
    if type(status) is not str:
        return True
    if status != "FILLED":
        return False
    side = row.get("side")
    reason = row.get("strategy_reason")
    if (
        type(side) is not str
        or side not in {"buy", "sell"}
        or type(reason) is not str
        or not reason.strip()
    ):
        return True
    return not (
        (side == "buy" and reason in _BUY_REASONS)
        or (side == "sell" and reason in _SELL_REASONS)
    )


def _trusted_intent(row: object) -> bool:
    if (
        type(row) is not dict
        or type(row.get("broker_order_status")) is not str
        or row.get("broker_order_status") != "FILLED"
    ):
        return False
    side = row.get("side")
    reason = row.get("strategy_reason")
    if type(side) is not str or type(reason) is not str:
        return False
    return (
        (side == "buy" and reason in _BUY_REASONS)
        or (side == "sell" and reason in _SELL_REASONS)
    )


def _trusted_fill(row: object) -> dict | None:
    if not _trusted_intent(row):
        return None
    assert type(row) is dict
    if (
        type(row.get("event_type")) is not str
        or row.get("event_type") not in _REAL_LIVE_EVENT_TYPES
        or type(row.get("live_order_sent")) is not int
        or row.get("live_order_sent") != 1
        or type(row.get("adapter_status")) is not str
        or row.get("adapter_status") != "enabled"
        or type(row.get("live_order_allowed")) is not int
        or row.get("live_order_allowed") != 1
    ):
        return None
    side = row.get("side")
    reason = row.get("strategy_reason")
    symbol = row.get("symbol")
    pilot_id = row.get("pilot_id")
    order_quantity = _positive_number(row.get("quantity"))
    quantity = _positive_number(row.get("filled_quantity"))
    if (
        order_quantity is None
        or quantity is None
        or not order_quantity.is_integer()
        or not quantity.is_integer()
        or order_quantity > _MAX_FILL_QUANTITY
        or quantity > _MAX_FILL_QUANTITY
        or order_quantity != quantity
    ):
        return None
    price = _positive_number(row.get("filled_price"))
    if price is not None and price > _MAX_FILL_PRICE:
        price = None
    notional_krw = _positive_number(row.get("estimated_amount_krw"))
    if notional_krw is not None and notional_krw > _MAX_ESTIMATED_NOTIONAL_KRW:
        notional_krw = None
    at = _timestamp(row.get("created_at"))
    if (
        type(symbol) is not str
        or not symbol.strip()
        or type(pilot_id) is not str
        or _CLIENT_ORDER_ID.fullmatch(pilot_id) is None
        or price is None
        or notional_krw is None
        or at is None
    ):
        return None
    raw_symbol = symbol.upper().strip()
    krx_match = _KRX_SYMBOL.fullmatch(raw_symbol)
    if krx_match is None and _US_SYMBOL.fullmatch(raw_symbol) is None:
        return None
    native_notional = price * quantity
    if not math.isfinite(native_notional) or native_notional <= 0:
        return None
    if krx_match is not None:
        canonical_notional_krw = native_notional
        market = "KR"
        currency = "KRW"
        krx_suffix = krx_match.group(2)
        fx_usdkrw = None
        fx_source = None
    else:
        fx_source = row.get("fx_source")
        fx_usdkrw = _positive_number(row.get("fx_usdkrw"))
        if (
            type(fx_source) is not str
            or fx_source not in _VERIFIED_FX_SOURCES
            or fx_usdkrw is None
            or not _MIN_VERIFIED_USD_KRW <= fx_usdkrw <= _MAX_VERIFIED_USD_KRW
        ):
            return None
        canonical_notional_krw = native_notional * fx_usdkrw
        market = "US"
        currency = "USD"
        krx_suffix = None
    if (
        not math.isfinite(canonical_notional_krw)
        or canonical_notional_krw <= 0
        or canonical_notional_krw > _MAX_ESTIMATED_NOTIONAL_KRW
    ):
        return None
    estimate_ratio = notional_krw / canonical_notional_krw
    if (
        not math.isfinite(estimate_ratio)
        or not _MIN_ESTIMATE_RATIO <= estimate_ratio <= _MAX_ESTIMATE_RATIO
    ):
        return None
    return {
        "pilot_id": pilot_id,
        "side": side,
        "strategy_reason": reason,
        "symbol": _canonical_symbol(raw_symbol),
        "raw_symbol": raw_symbol,
        "market": market,
        "currency": currency,
        "krx_suffix": krx_suffix,
        "quantity": quantity,
        "price": price,
        "notional_krw": canonical_notional_krw,
        "estimated_notional_krw": notional_krw,
        "fx_usdkrw": fx_usdkrw,
        "fx_source": fx_source,
        "created_at": at,
    }


def _rounded(value: float) -> float:
    return round(value, 4)


def reconstruct_execution_calibration(
    rows: Iterable[Mapping] | object,
    *,
    min_samples: int = 20,
) -> dict:
    """Reconstruct fully realized FIFO lifecycles from sanitized fill rows.

    Partial positions remain in ``open_lot_count`` and never contribute to win rate
    or expected return.  The conservative cost buffer mirrors the current income
    decision model: ``max(1,000 KRW, entry notional × 0.15%)`` once per lifecycle.
    """
    if type(min_samples) is not int or isinstance(min_samples, bool) or min_samples < 1:
        raise ValueError("min_samples_invalid")
    raw_rows: list | tuple
    if type(rows) in (list, tuple):
        raw_rows = cast(list | tuple, rows)
    else:
        raw_rows = []

    trusted_by_pilot: dict[str, dict] = {}
    conflicted_pilot_ids: set[str] = set()
    ignored_count = 0
    conflict_count = 0
    quarantined_fill_count = 0
    invalid_fill_count = 0
    for raw in raw_rows:
        trusted_intent = _trusted_intent(raw)
        invalid_production_id = (
            _production_like_fill(raw)
            and (
                type(raw.get("pilot_id")) is not str
                or _CLIENT_ORDER_ID.fullmatch(raw.get("pilot_id")) is None
            )
        )
        malformed_production_fill = _malformed_production_fill(raw)
        fill = _trusted_fill(raw)
        if fill is None:
            ignored_count += 1
            if trusted_intent or invalid_production_id or malformed_production_fill:
                quarantined_fill_count += 1
                invalid_fill_count += 1
            continue
        pilot_id = fill["pilot_id"]
        if pilot_id in conflicted_pilot_ids:
            ignored_count += 1
            quarantined_fill_count += 1
            continue
        previous = trusted_by_pilot.get(pilot_id)
        if previous is None:
            trusted_by_pilot[pilot_id] = fill
        elif previous == fill:
            ignored_count += 1
        else:
            del trusted_by_pilot[pilot_id]
            conflicted_pilot_ids.add(pilot_id)
            conflict_count += 1
            ignored_count += 2
            quarantined_fill_count += 2
    trusted = list(trusted_by_pilot.values())
    symbol_aliases: dict[str, set[str]] = defaultdict(set)
    for fill in trusted:
        symbol_aliases[fill["symbol"]].add(fill["raw_symbol"])
    conflicted_symbols = {
        symbol
        for symbol, raw_symbols in symbol_aliases.items()
        if len({
            raw.rsplit(".", 1)[1]
            for raw in raw_symbols
            if raw.endswith((".KS", ".KQ"))
        }) > 1
    }
    symbol_alias_conflict_count = len(conflicted_symbols)
    if conflicted_symbols:
        quarantined = [
            fill for fill in trusted if fill["symbol"] in conflicted_symbols
        ]
        quarantined_fill_count += len(quarantined)
        ignored_count += len(quarantined)
        trusted = [
            fill for fill in trusted if fill["symbol"] not in conflicted_symbols
        ]
    fill_order_groups: dict[tuple[str, datetime], list[dict]] = defaultdict(list)
    for fill in trusted:
        fill_order_groups[(fill["symbol"], fill["created_at"])].append(fill)
    ambiguous_keys = {
        key for key, fills in fill_order_groups.items() if len(fills) > 1
    }
    ambiguous_fill_count = sum(
        len(fill_order_groups[key]) for key in ambiguous_keys
    )
    if ambiguous_keys:
        quarantined_fill_count += ambiguous_fill_count
        invalid_fill_count += ambiguous_fill_count
        ignored_count += ambiguous_fill_count
        trusted = [
            fill
            for fill in trusted
            if (fill["symbol"], fill["created_at"]) not in ambiguous_keys
        ]
    trusted.sort(key=lambda row: row["created_at"])

    open_lots: dict[str, list[dict]] = defaultdict(list)
    outcomes: list[dict] = []
    unmatched_sell_fill_count = 0
    unmatched_sell_quantity = 0.0
    for fill in trusted:
        symbol = fill["symbol"]
        if fill["side"] == "buy":
            open_lots[symbol].append({
                "buy_pilot_id": fill["pilot_id"],
                "entry_quantity": fill["quantity"],
                "remaining_quantity": fill["quantity"],
                "entry_price": fill["price"],
                "entry_notional_krw": fill["notional_krw"],
                "krx_suffix": fill["krx_suffix"],
                "entered_at": fill["created_at"],
                "exit_notional_krw": 0.0,
                "exit_count": 0,
                "last_exit_at": None,
            })
            continue

        sell_remaining = fill["quantity"]
        lots = open_lots.get(symbol) or []
        if not lots:
            ignored_count += 1
            unmatched_sell_fill_count += 1
            unmatched_sell_quantity += sell_remaining
            continue
        while sell_remaining > 0 and lots:
            lot = lots[0]
            matched = min(sell_remaining, lot["remaining_quantity"])
            lot["exit_notional_krw"] += (
                fill["notional_krw"] * matched / fill["quantity"]
            )
            lot["remaining_quantity"] -= matched
            lot["exit_count"] += 1
            lot["last_exit_at"] = fill["created_at"]
            sell_remaining -= matched
            if lot["remaining_quantity"] > 1e-9:
                continue

            entry_value = lot["entry_notional_krw"]
            gross_return_pct = (
                lot["exit_notional_krw"] / entry_value - 1.0
            ) * 100.0
            cost_buffer_krw = max(1_000.0, entry_value * 0.0015)
            cost_return_pct = cost_buffer_krw / entry_value * 100.0
            net_return_pct = gross_return_pct - cost_return_pct
            if not all(math.isfinite(value) for value in (
                entry_value,
                lot["exit_notional_krw"],
                gross_return_pct,
                cost_buffer_krw,
                cost_return_pct,
                net_return_pct,
            )):
                quarantined_fill_count += 1
                invalid_fill_count += 1
                ignored_count += 1
                lots.pop(0)
                continue
            canonical_net_return_pct = _rounded(net_return_pct)
            outcome = "win" if canonical_net_return_pct > 0 else (
                "loss" if canonical_net_return_pct < 0 else "flat"
            )
            outcomes.append({
                "symbol": symbol,
                "buy_pilot_id": lot["buy_pilot_id"],
                "entry_quantity": int(lot["entry_quantity"]),
                "entry_price": _rounded(lot["entry_price"]),
                "exit_count": lot["exit_count"],
                "gross_return_pct": _rounded(gross_return_pct),
                "cost_buffer_krw": round(cost_buffer_krw, 2),
                "net_return_pct": canonical_net_return_pct,
                "outcome": outcome,
                "entered_at": lot["entered_at"].isoformat(),
                "closed_at": lot["last_exit_at"].isoformat(),
            })
            lots.pop(0)
        if sell_remaining > 1e-9:
            ignored_count += 1
            unmatched_sell_fill_count += 1
            unmatched_sell_quantity += sell_remaining
        if not lots:
            open_lots.pop(symbol, None)

    open_rows = [lot for lots in open_lots.values() for lot in lots]
    open_positions = []
    for symbol, lots in sorted(open_lots.items()):
        suffixes = {
            lot["krx_suffix"] for lot in lots if lot.get("krx_suffix") is not None
        }
        open_positions.append({
            "symbol": symbol,
            "quantity": int(round(sum(
                lot["remaining_quantity"] for lot in lots
            ))),
            "krx_suffix": next(iter(suffixes)) if len(suffixes) == 1 else None,
        })
    wins = [row for row in outcomes if row["outcome"] == "win"]
    losses = [row for row in outcomes if row["outcome"] == "loss"]
    flats = [row for row in outcomes if row["outcome"] == "flat"]
    completed_count = len(outcomes)
    lineage_reasons: list[str] = []
    if conflict_count:
        lineage_reasons.append("pilot_payload_conflict")
    if symbol_alias_conflict_count:
        lineage_reasons.append("krx_symbol_alias_conflict")
    if invalid_fill_count:
        lineage_reasons.append("fill_contract_invalid")
    if ambiguous_fill_count:
        lineage_reasons.append("fill_order_ambiguous")
    if unmatched_sell_fill_count:
        lineage_reasons.append("unmatched_sell_fill")
    lineage_status = "incomplete" if lineage_reasons else "complete"

    def _average(source: list[dict]) -> float | None:
        if not source:
            return None
        return _rounded(sum(row["net_return_pct"] for row in source) / len(source))

    return {
        **_OBSERVABILITY_CONTRACT,
        "status": "partial" if lineage_status == "incomplete" else "ok",
        "completed_count": completed_count,
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "win_rate": (
            _rounded(len(wins) / completed_count) if completed_count else None
        ),
        "avg_win_pct": _average(wins),
        "avg_loss_pct": _average(losses),
        "mean_net_return_pct": _average(outcomes),
        "minimum_sample_reached": completed_count >= min_samples,
        "sample_sufficient": completed_count >= min_samples,
        "evidence_sufficient": (
            completed_count >= min_samples
            and lineage_status == "complete"
            and _OBSERVABILITY_CONTRACT["attribution_verified"] is True
        ),
        "min_samples": min_samples,
        "lineage_status": lineage_status,
        "lineage_reasons": lineage_reasons,
        "unmatched_sell_fill_count": unmatched_sell_fill_count,
        "unmatched_sell_quantity": int(round(unmatched_sell_quantity)),
        "open_lot_count": len(open_rows),
        "open_quantity": int(round(sum(row["remaining_quantity"] for row in open_rows))),
        "open_positions": open_positions,
        "ignored_count": ignored_count,
        "quarantined_fill_count": quarantined_fill_count,
        "invalid_fill_count": invalid_fill_count,
        "conflict_count": conflict_count,
        "symbol_alias_conflict_count": symbol_alias_conflict_count,
        "ambiguous_fill_count": ambiguous_fill_count,
        "outcomes": outcomes,
    }


def reconcile_calibration_with_holdings(
    calibration: object,
    holdings: object,
) -> dict:
    """Compare reconstructed open auto lots with a fresh holdings snapshot."""
    if type(calibration) is not dict:
        return {
            **_OBSERVABILITY_CONTRACT,
            "holdings_reconciliation_status": "unavailable",
            "lineage_status": "incomplete",
            "lineage_reasons": ["holdings_reconciliation_unavailable"],
            "evidence_sufficient": False,
        }
    result = dict(calibration)

    def _mark_unavailable() -> dict:
        raw_reasons = result.get("lineage_reasons")
        reasons = list(raw_reasons) if type(raw_reasons) is list else []
        if "holdings_reconciliation_unavailable" not in reasons:
            reasons.append("holdings_reconciliation_unavailable")
        result.update({
            "holdings_reconciliation_status": "unavailable",
            "lineage_status": "incomplete",
            "lineage_reasons": reasons,
            "evidence_sufficient": False,
        })
        if result.get("status") == "ok":
            result["status"] = "partial"
        return result

    open_positions = result.get("open_positions")
    if type(open_positions) is not list or type(holdings) not in (list, tuple):
        return _mark_unavailable()

    holding_quantities: dict[str, float] = defaultdict(float)
    holding_suffixes: dict[str, set[str]] = defaultdict(set)
    invalid = False
    for row in cast(list | tuple, holdings):
        if type(row) is not dict:
            invalid = True
            break
        symbol = row.get("symbol")
        quantity = _positive_number(row.get("quantity"))
        if (
            type(symbol) is not str
            or not symbol.strip()
            or quantity is None
            or not quantity.is_integer()
            or quantity > _MAX_FILL_QUANTITY
        ):
            invalid = True
            break
        raw_symbol = symbol.upper().strip()
        canonical_symbol = _canonical_symbol(raw_symbol)
        holding_quantities[canonical_symbol] += quantity
        suffix = _krx_suffix(raw_symbol)
        if suffix is not None:
            holding_suffixes[canonical_symbol].add(suffix)
        if holding_quantities[canonical_symbol] > _MAX_FILL_QUANTITY:
            invalid = True
            break
    if invalid:
        return _mark_unavailable()

    excess = 0.0
    alias_conflicts: set[str] = {
        symbol for symbol, suffixes in holding_suffixes.items() if len(suffixes) > 1
    }
    for row in open_positions:
        if type(row) is not dict:
            return _mark_unavailable()
        symbol = row.get("symbol")
        quantity = _positive_number(row.get("quantity"))
        suffix = row.get("krx_suffix")
        if (
            type(symbol) is not str
            or not symbol.strip()
            or quantity is None
            or not quantity.is_integer()
            or quantity > _MAX_FILL_QUANTITY
            or (suffix is not None and (type(suffix) is not str or suffix not in {"KS", "KQ"}))
        ):
            return _mark_unavailable()
        canonical_symbol = _canonical_symbol(symbol)
        explicit_holding_suffixes = holding_suffixes.get(canonical_symbol, set())
        suffix_conflict = (
            canonical_symbol in alias_conflicts
            or (
                suffix is not None
                and bool(explicit_holding_suffixes)
                and suffix not in explicit_holding_suffixes
            )
        )
        if suffix_conflict:
            alias_conflicts.add(canonical_symbol)
            available_quantity = 0.0
        else:
            available_quantity = holding_quantities.get(canonical_symbol, 0.0)
        excess += max(0.0, quantity - available_quantity)

    raw_reasons = result.get("lineage_reasons")
    lineage_reasons = list(raw_reasons) if type(raw_reasons) is list else []
    if alias_conflicts and "holdings_symbol_alias_conflict" not in lineage_reasons:
        lineage_reasons.append("holdings_symbol_alias_conflict")
    if excess > 0 and "open_lots_exceed_holdings" not in lineage_reasons:
        lineage_reasons.append("open_lots_exceed_holdings")
    reconciliation_status = (
        "incomplete" if excess > 0 or alias_conflicts else "complete"
    )
    result.update({
        "holdings_reconciliation_status": reconciliation_status,
        "open_quantity_exceeds_holdings": int(round(excess)),
        "holdings_symbol_alias_conflict_count": len(alias_conflicts),
        "lineage_status": (
            "incomplete"
            if reconciliation_status == "incomplete" or lineage_reasons
            else "complete"
        ),
        "lineage_reasons": lineage_reasons,
        "evidence_sufficient": False,
    })
    if reconciliation_status == "incomplete" and result.get("status") == "ok":
        result["status"] = "partial"
    return result


def _readonly_connection(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve(strict=True)
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _require_unique_ledger_schema(conn: sqlite3.Connection) -> None:
    column_rows = conn.execute(
        "PRAGMA table_info(live_pilot_ledger)"
    ).fetchall()
    columns = {str(row["name"]): row for row in column_rows}
    pilot_id = columns.get("pilot_id")
    reason = columns.get("reason")
    has_exact_binary_unique_index = False
    for index_row in conn.execute(
        "PRAGMA index_list(live_pilot_ledger)"
    ).fetchall():
        if int(index_row["unique"] or 0) != 1 or int(index_row["partial"] or 0) != 0:
            continue
        index_name = str(index_row["name"])
        quoted_name = index_name.replace('"', '""')
        index_columns = conn.execute(
            f'PRAGMA index_xinfo("{quoted_name}")'
        ).fetchall()
        key_columns = [
            row for row in index_columns if int(row["key"] or 0) == 1
        ]
        if (
            len(key_columns) == 1
            and int(key_columns[0]["cid"]) >= 0
            and str(key_columns[0]["name"]) == "pilot_id"
            and str(key_columns[0]["coll"] or "").upper() == "BINARY"
        ):
            has_exact_binary_unique_index = True
            break
    if (
        pilot_id is None
        or str(pilot_id["type"] or "").upper() != "TEXT"
        or reason is None
        or str(reason["type"] or "").upper() != "TEXT"
        or not has_exact_binary_unique_index
    ):
        raise sqlite3.DatabaseError("live_pilot_ledger_unique_schema_required")


def load_execution_calibration(
    *,
    events_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    min_samples: int = 20,
    max_source_rows: int = 5_000,
) -> dict:
    """Load a bounded event window and matching ledger reasons read-only."""
    if (
        type(max_source_rows) is not int
        or isinstance(max_source_rows, bool)
        or not 1 <= max_source_rows <= 10_000
    ):
        raise ValueError("max_source_rows_invalid")
    repo_root = Path(__file__).resolve().parents[1]
    events_db = Path(events_path) if events_path is not None else (
        repo_root / "db" / "data" / "toss_live_pilot_events.db"
    )
    ledger_db = Path(ledger_path) if ledger_path is not None else (
        repo_root / "db" / "data" / "toss_live_pilot.db"
    )
    try:
        events_conn = _readonly_connection(events_db)
        try:
            fetched_rows = events_conn.execute(
                """SELECT event_id, pilot_id, event_type, side, symbol, quantity,
                          estimated_amount_krw, created_at, broker_order_status,
                          filled_quantity, filled_price, live_order_sent,
                          adapter_status, live_order_allowed
                   FROM live_pilot_events
                   ORDER BY created_at DESC, event_id DESC
                   LIMIT ?""",
                (max_source_rows + 1,),
            ).fetchall()
        finally:
            events_conn.close()
        source_window_truncated = len(fetched_rows) > max_source_rows
        event_rows = list(reversed(fetched_rows[:max_source_rows]))

        pilot_ids = sorted({
            str(row["pilot_id"])
            for row in event_rows
            if row["pilot_id"]
        })
        ledger_reasons: dict[str, str] = {}
        ledger_reason_conflicts: set[str] = set()
        ledger_reason_invalid: set[str] = set()
        ledger_conn = _readonly_connection(ledger_db)
        try:
            _require_unique_ledger_schema(ledger_conn)
            for start in range(0, len(pilot_ids), 900):
                chunk = pilot_ids[start:start + 900]
                placeholders = ",".join("?" for _ in chunk)
                reason_rows = ledger_conn.execute(
                    f"SELECT pilot_id, reason FROM live_pilot_ledger "
                    f"WHERE pilot_id COLLATE BINARY IN ({placeholders}) LIMIT ?",
                    [*chunk, len(chunk) + 1],
                ).fetchall()
                if len(reason_rows) > len(chunk):
                    raise sqlite3.DatabaseError("ledger_cardinality_exceeded")
                for row in reason_rows:
                    raw_pilot_id = row["pilot_id"]
                    raw_reason = row["reason"]
                    if type(raw_pilot_id) is not str or type(raw_reason) is not str:
                        invalid_key = (
                            raw_pilot_id
                            if type(raw_pilot_id) is str and raw_pilot_id
                            else f"ledger_row:{len(ledger_reason_invalid)}"
                        )
                        ledger_reason_invalid.add(invalid_key)
                        continue
                    pilot_id = raw_pilot_id
                    reason = raw_reason
                    previous = ledger_reasons.get(pilot_id)
                    if previous is None:
                        ledger_reasons[pilot_id] = reason
                    else:
                        ledger_reason_conflicts.add(pilot_id)
        finally:
            ledger_conn.close()
        for pilot_id in ledger_reason_conflicts:
            ledger_reasons.pop(pilot_id, None)
        ledger_reason_missing: set[str] = set()
        for row in event_rows:
            if row["broker_order_status"] != "FILLED":
                continue
            pilot_id = str(row["pilot_id"] or "")
            if pilot_id in ledger_reason_conflicts:
                continue
            if pilot_id in ledger_reason_invalid:
                continue
            if not pilot_id or not ledger_reasons.get(pilot_id, "").strip():
                missing_key = pilot_id or f"event:{row['event_id']}"
                ledger_reason_missing.add(missing_key)
    except (OSError, sqlite3.Error) as exc:
        return {
            **_OBSERVABILITY_CONTRACT,
            "status": "unavailable",
            "reason": "execution_calibration_source_unavailable",
            "error_type": type(exc).__name__,
            "completed_count": 0,
            "wins": 0,
            "losses": 0,
            "flats": 0,
            "win_rate": None,
            "avg_win_pct": None,
            "avg_loss_pct": None,
            "mean_net_return_pct": None,
            "minimum_sample_reached": False,
            "sample_sufficient": False,
            "evidence_sufficient": False,
            "min_samples": min_samples,
            "lineage_status": "incomplete",
            "lineage_reasons": ["execution_calibration_source_unavailable"],
            "unmatched_sell_fill_count": 0,
            "unmatched_sell_quantity": 0,
            "open_lot_count": 0,
            "open_quantity": 0,
            "ignored_count": 0,
            "quarantined_fill_count": 0,
            "invalid_fill_count": 0,
            "conflict_count": 0,
            "symbol_alias_conflict_count": 0,
            "source": "read_only_live_pilot_event_ledger",
            "source_window_truncated": False,
            "source_row_limit": max_source_rows,
            "source_rows_loaded": 0,
            "ledger_reason_conflict_count": 0,
            "ledger_reason_missing_count": 0,
            "ledger_reason_invalid_count": 0,
            "ambiguous_fill_count": 0,
            "outcomes": [],
        }

    normalized_rows = []
    for row in event_rows:
        pilot_id = str(row["pilot_id"] or "")
        normalized_rows.append({
            "pilot_id": pilot_id,
            "event_type": row["event_type"],
            "side": row["side"],
            "symbol": row["symbol"],
            "quantity": row["quantity"],
            "filled_quantity": row["filled_quantity"],
            "filled_price": row["filled_price"],
            "created_at": row["created_at"],
            "broker_order_status": row["broker_order_status"],
            "strategy_reason": ledger_reasons.get(pilot_id, ""),
            "estimated_amount_krw": row["estimated_amount_krw"],
            "fx_usdkrw": None,
            "fx_source": None,
            "live_order_sent": row["live_order_sent"],
            "adapter_status": row["adapter_status"],
            "live_order_allowed": row["live_order_allowed"],
        })
    summary = reconstruct_execution_calibration(
        normalized_rows,
        min_samples=min_samples,
    )
    lineage_reasons = list(summary.get("lineage_reasons") or [])
    if source_window_truncated:
        lineage_reasons.append("source_window_truncated")
    if ledger_reason_conflicts:
        lineage_reasons.append("ledger_reason_conflict")
    if ledger_reason_missing:
        lineage_reasons.append("ledger_reason_missing")
    if ledger_reason_invalid:
        lineage_reasons.append("ledger_reason_invalid")
    lineage_reasons = list(dict.fromkeys(lineage_reasons))
    lineage_status = "incomplete" if lineage_reasons else "complete"
    summary.update({
        "status": "partial" if lineage_status == "incomplete" else "ok",
        "source": "read_only_live_pilot_event_ledger",
        "source_window_truncated": source_window_truncated,
        "source_row_limit": max_source_rows,
        "source_rows_loaded": len(event_rows),
        "ledger_reason_conflict_count": len(ledger_reason_conflicts),
        "ledger_reason_missing_count": len(ledger_reason_missing),
        "ledger_reason_invalid_count": len(ledger_reason_invalid),
        "lineage_status": lineage_status,
        "lineage_reasons": lineage_reasons,
        "evidence_sufficient": False,
    })
    return summary
