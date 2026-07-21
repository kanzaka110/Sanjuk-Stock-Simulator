"""Append-only close-to-close outcomes for shadow decisions.

The evaluator reads immutable market-close observations and appends labels only after
the requested number of subsequent observed market sessions exists.  It never scores
candidates, changes ordering, or sends orders.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from typing import Iterable
from zoneinfo import ZoneInfo

from src.shadow_price_observations import DATASET

_CONTRACT_VERSION = "close_to_close_after_cost_v1"
_COST_MODEL = "backtest_round_trip_v1"
_HORIZON_SESSIONS = {"1d": 1, "3d": 3, "5d": 5}
_ROUND_TRIP_COST_PCT = {"KR": 0.23, "US": 0.10}
_MARKET_TIMEZONE = {"KR": ZoneInfo("Asia/Seoul"), "US": ZoneInfo("America/New_York")}
_MAX_DECISIONS = 20_000
_MAX_OBSERVATIONS = 100_000


@dataclass(frozen=True)
class _Decision:
    decision_id: str
    symbol: str
    decided_at: datetime
    market: str
    currency: str


@dataclass(frozen=True)
class _Quote:
    snapshot_id: str
    symbol: str
    market: str
    source_as_of: datetime
    ingested_at: datetime
    fallback_used: bool
    price: float
    high: float
    low: float


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name}_timezone_aware_required")
    return value.astimezone(timezone.utc)


def _timestamp(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp_invalid") from exc
    return _aware_utc(parsed, "timestamp")


def _positive_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("number_invalid")
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > 1_000_000_000:
        raise ValueError("number_invalid")
    return number


def _ro_connection(path_value: str | Path) -> sqlite3.Connection:
    path = Path(path_value).expanduser().resolve(strict=True)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _load_decisions(
    shadow_db_path: str | Path,
    *,
    decision_not_before: datetime,
    evaluated_at: datetime,
) -> list[_Decision]:
    with _ro_connection(shadow_db_path) as connection:
        rows = connection.execute(
            """SELECT decision_id, symbol, decided_at_utc, features_json
               FROM shadow_decisions
               WHERE side = 'BUY'
                 AND decided_at_utc >= ?
                 AND decided_at_utc <= ?
               ORDER BY decided_at_utc, decision_id
               LIMIT ?""",
            (decision_not_before.isoformat(), evaluated_at.isoformat(), _MAX_DECISIONS),
        ).fetchall()
    result: list[_Decision] = []
    for decision_id, symbol, decided_text, features_text in rows:
        try:
            features = json.loads(str(features_text))
            if not isinstance(features, dict):
                raise ValueError("features_invalid")
            market = str(features.get("market") or "")
            currency = str(features.get("currency") or "")
            if market not in {"KR", "US"}:
                raise ValueError("market_invalid")
            expected_currency = "KRW" if market == "KR" else "USD"
            if currency != expected_currency:
                raise ValueError("currency_invalid")
            result.append(
                _Decision(
                    decision_id=str(decision_id),
                    symbol=str(symbol),
                    decided_at=_timestamp(decided_text),
                    market=market,
                    currency=currency,
                )
            )
        except Exception:
            continue
    return result


def _load_quotes(
    source_db_path: str | Path,
    *,
    evaluated_at: datetime,
) -> dict[str, list[_Quote]]:
    ceiling = evaluated_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with _ro_connection(source_db_path) as connection:
        rows = connection.execute(
            """SELECT snapshot_id, symbol, market, source_as_of, ingested_at,
                      fallback_used, payload_json
               FROM observations
               WHERE dataset = ?
                 AND source_as_of <= ?
                 AND ingested_at <= ?
               ORDER BY source_as_of, ingested_at, id
               LIMIT ?""",
            (DATASET, ceiling, ceiling, _MAX_OBSERVATIONS),
        ).fetchall()
    result: dict[str, list[_Quote]] = {}
    for row in rows:
        try:
            snapshot_id, symbol, market = str(row[0]), str(row[1]), str(row[2])
            if market not in {"KR", "US"}:
                raise ValueError("market_invalid")
            source_as_of = _timestamp(row[3])
            ingested_at = _timestamp(row[4])
            if source_as_of > ingested_at or ingested_at > evaluated_at:
                raise ValueError("observation_time_invalid")
            if type(row[5]) is not int or row[5] not in {0, 1}:
                raise ValueError("fallback_invalid")
            payload = json.loads(str(row[6]))
            if not isinstance(payload, dict):
                raise ValueError("payload_invalid")
            price = _positive_number(payload.get("price"))
            high = _positive_number(payload.get("high"))
            low = _positive_number(payload.get("low"))
            if not low <= price <= high:
                raise ValueError("price_range_invalid")
            result.setdefault(symbol, []).append(
                _Quote(
                    snapshot_id=snapshot_id,
                    symbol=symbol,
                    market=market,
                    source_as_of=source_as_of,
                    ingested_at=ingested_at,
                    fallback_used=bool(row[5]),
                    price=price,
                    high=high,
                    low=low,
                )
            )
        except Exception:
            continue
    return result


def _session_quotes(decision: _Decision, quotes: list[_Quote]) -> list[_Quote]:
    timezone_for_market = _MARKET_TIMEZONE[decision.market]
    sessions: dict[str, _Quote] = {}
    for quote in quotes:
        if quote.market != decision.market or quote.source_as_of < decision.decided_at:
            continue
        session_date = quote.source_as_of.astimezone(timezone_for_market).date().isoformat()
        sessions.setdefault(session_date, quote)
    return list(sessions.values())


def _outcome_payload(
    decision: _Decision,
    *,
    entry: _Quote,
    exit_quote: _Quote,
    path_after_entry: list[_Quote],
    horizon_sessions: int,
) -> dict:
    gross_return = ((exit_quote.price - entry.price) / entry.price) * 100.0
    cost = _ROUND_TRIP_COST_PCT[decision.market]
    maximum_high = max(quote.high for quote in path_after_entry)
    minimum_low = min(quote.low for quote in path_after_entry)
    return {
        "contract_version": _CONTRACT_VERSION,
        "currency": decision.currency,
        "decision_usable": False,
        "entry_price": round(entry.price, 6),
        "entry_snapshot_id": entry.snapshot_id,
        "entry_source_as_of_utc": entry.source_as_of.strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
        "exit_price": round(exit_quote.price, 6),
        "exit_snapshot_id": exit_quote.snapshot_id,
        "exit_source_as_of_utc": exit_quote.source_as_of.strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
        "fallback_used": any(
            quote.fallback_used for quote in [entry, *path_after_entry]
        ),
        "gross_return_pct": round(gross_return, 6),
        "horizon_sessions": horizon_sessions,
        "mae_pct": round(((minimum_low - entry.price) / entry.price) * 100.0, 6),
        "market": decision.market,
        "mfe_pct": round(((maximum_high - entry.price) / entry.price) * 100.0, 6),
        "return_pct_after_cost": round(gross_return - cost, 6),
        "round_trip_cost_model": _COST_MODEL,
        "round_trip_cost_pct": cost,
        "source_dataset": DATASET,
    }


def evaluate_shadow_outcomes(
    *,
    shadow_db_path: str | Path,
    source_db_path: str | Path,
    decision_not_before_utc: datetime,
    evaluated_at_utc: datetime,
    horizons: Iterable[str] = ("1d", "3d", "5d"),
    shadow_store=None,
) -> dict[str, int]:
    """Append mature immutable outcomes and report bounded aggregate counts."""
    activation = _aware_utc(decision_not_before_utc, "decision_not_before_utc")
    evaluated_at = _aware_utc(evaluated_at_utc, "evaluated_at_utc")
    if activation > evaluated_at:
        raise ValueError("decision_not_before_after_evaluated_at")
    requested = tuple(horizons)
    if (
        not requested
        or len(set(requested)) != len(requested)
        or any(horizon not in _HORIZON_SESSIONS for horizon in requested)
    ):
        raise ValueError("horizons_invalid")

    decisions = _load_decisions(
        shadow_db_path,
        decision_not_before=activation,
        evaluated_at=evaluated_at,
    )
    quotes_by_symbol = _load_quotes(source_db_path, evaluated_at=evaluated_at)
    resolved_store = shadow_store
    if resolved_store is None:
        from core.shadow_measurements import ShadowMeasurementStore

        resolved_store = ShadowMeasurementStore(shadow_db_path)

    result = {
        "decisions_seen": len(decisions),
        "labels_considered": len(decisions) * len(requested),
        "inserted": 0,
        "duplicate": 0,
        "pending": 0,
        "invalid": 0,
    }
    for decision in decisions:
        sessions = _session_quotes(decision, quotes_by_symbol.get(decision.symbol, []))
        for horizon in requested:
            if resolved_store.get_outcome(decision.decision_id, horizon) is not None:
                result["duplicate"] += 1
                continue
            horizon_sessions = _HORIZON_SESSIONS[horizon]
            if len(sessions) <= horizon_sessions:
                result["pending"] += 1
                continue
            entry = sessions[0]
            exit_quote = sessions[horizon_sessions]
            path_after_entry = sessions[1 : horizon_sessions + 1]
            try:
                appended = resolved_store.append_outcome(
                    decision_id=decision.decision_id,
                    horizon=horizon,
                    evaluated_at_utc=evaluated_at,
                    outcome=_outcome_payload(
                        decision,
                        entry=entry,
                        exit_quote=exit_quote,
                        path_after_entry=path_after_entry,
                        horizon_sessions=horizon_sessions,
                    ),
                )
            except Exception:
                result["invalid"] += 1
                continue
            if appended.inserted:
                result["inserted"] += 1
            else:
                result["duplicate"] += 1
    return result


def _parse_cli_time(value: str) -> datetime:
    return _timestamp(value)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate shadow close-to-close outcomes")
    parser.add_argument("--decision-not-before", required=True, type=_parse_cli_time)
    parser.add_argument("--shadow-db")
    parser.add_argument("--source-db")
    args = parser.parse_args(argv)

    from config.settings import DB_DIR

    shadow_path = Path(args.shadow_db) if args.shadow_db else Path(DB_DIR) / "shadow_measurements.db"
    source_path = Path(args.source_db) if args.source_db else Path(DB_DIR) / "source_observations_v2.db"
    evaluated_at = datetime.now(timezone.utc)
    result = evaluate_shadow_outcomes(
        shadow_db_path=shadow_path,
        source_db_path=source_path,
        decision_not_before_utc=args.decision_not_before,
        evaluated_at_utc=evaluated_at,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
