"""Point-in-time market-price observations for shadow decisions.

This module is shadow-only.  It does not score candidates, change ordering, or send
orders.  The CLI fetches quotes first, freezes one completion timestamp, and then
appends immutable source observations.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterable

DATASET = "market_close_quote"
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
_KR_SYMBOL_RE = re.compile(r"^[0-9]{6}(?:\.(?:KS|KQ))?$")
_MAX_SYMBOLS = 200
_MAX_DECISION_SCAN = 10_000
_MAX_PRICE = 1_000_000_000.0
_SOURCE_MAP = {
    "kis": ("kis", False),
    "afterhours": ("yfinance", True),
    "yf_fast": ("yfinance", True),
    "yf_daily": ("yfinance", True),
    "yfinance_live": ("yfinance", True),
    "yfinance_daily": ("yfinance", True),
}


def _aware_utc(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name}_timezone_aware_required")
    return value.astimezone(timezone.utc)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result):
        return None
    return result


def _quote_value(quote: object, key: str) -> object:
    if isinstance(quote, dict):
        return quote.get(key)
    return getattr(quote, key, None)


def _quote_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _aware_utc(value, "quote_as_of")
    numeric = _number(value)
    if numeric is None or numeric < 0:
        return None
    try:
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _symbols(values: Iterable[object]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("symbols_invalid")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if type(value) is not str:
            raise ValueError("symbol_invalid")
        symbol = value.strip().upper()
        if not _SYMBOL_RE.fullmatch(symbol):
            raise ValueError("symbol_invalid")
        if symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
        if len(result) > _MAX_SYMBOLS:
            raise ValueError("symbols_too_many")
    return tuple(result)


def _validated_quote(
    quote: object,
    *,
    observed_at_utc: datetime,
) -> tuple[str, bool, datetime, dict[str, Any]] | None:
    if quote is None:
        return None
    raw_source = _quote_value(quote, "source")
    if type(raw_source) is not str:
        raise ValueError("quote_source_invalid")
    source_key = raw_source.strip().lower()
    source_contract = _SOURCE_MAP.get(source_key)
    if source_contract is None:
        raise ValueError("quote_source_invalid")

    source_as_of = _quote_timestamp(_quote_value(quote, "as_of"))
    if source_as_of is None or source_as_of > observed_at_utc:
        raise ValueError("quote_as_of_invalid")

    price = _number(_quote_value(quote, "price"))
    high = _number(_quote_value(quote, "high"))
    low = _number(_quote_value(quote, "low"))
    change = _number(_quote_value(quote, "change"))
    pct = _number(_quote_value(quote, "pct"))
    if price is None or not 0 < price <= _MAX_PRICE:
        raise ValueError("quote_price_invalid")
    high = price if high is None else high
    low = price if low is None else low
    if (
        not 0 < low <= price <= high <= _MAX_PRICE
        or change is None
        or pct is None
    ):
        raise ValueError("quote_range_invalid")

    source, fallback_used = source_contract
    return source, fallback_used, source_as_of, {
        "change": change,
        "high": high,
        "low": low,
        "price": price,
        "quote_source": raw_source,
    }


def collect_price_observations(
    symbols: Iterable[object],
    *,
    market: str,
    observed_at_utc: datetime,
    quote_fetcher: Callable[[str], object],
    store,
) -> dict[str, int]:
    """Append one immutable quote observation per valid symbol."""
    if market not in {"KR", "US"}:
        raise ValueError("market_invalid")
    observed_at = _aware_utc(observed_at_utc, "observed_at_utc")
    normalized = _symbols(symbols)
    if any(
        (_KR_SYMBOL_RE.fullmatch(symbol) is None) if market == "KR"
        else (_KR_SYMBOL_RE.fullmatch(symbol) is not None)
        for symbol in normalized
    ):
        raise ValueError("market_symbol_invalid")
    result = {
        "seen": len(normalized),
        "inserted": 0,
        "duplicate": 0,
        "invalid": 0,
        "unavailable": 0,
    }
    currency = "KRW" if market == "KR" else "USD"

    for symbol in normalized:
        try:
            quote = quote_fetcher(symbol)
        except Exception:
            result["unavailable"] += 1
            continue
        if quote is None:
            result["unavailable"] += 1
            continue
        try:
            validated = _validated_quote(quote, observed_at_utc=observed_at)
            if validated is None:
                result["unavailable"] += 1
                continue
            source, fallback_used, source_as_of, payload = validated
            append_result = store.append(
                source=source,
                dataset=DATASET,
                source_record_id=(
                    f"{symbol}:{source}:{source_as_of.isoformat()}"
                ),
                symbol=symbol,
                market=market,
                currency_or_unit=currency,
                source_as_of=source_as_of,
                source_event_sequence=0,
                ingested_at=observed_at,
                schema_version=1,
                transform_version=1,
                fallback_used=fallback_used,
                payload=payload,
            )
        except Exception:
            result["invalid"] += 1
            continue
        if append_result.inserted:
            result["inserted"] += 1
        else:
            result["duplicate"] += 1
    return result


def load_shadow_symbols(
    db_path: str | Path,
    *,
    market: str,
    decided_since_utc: datetime,
    limit: int = _MAX_SYMBOLS,
) -> tuple[str, ...]:
    """Read recent shadow-decision symbols without mutating the database."""
    if market not in {"KR", "US"}:
        raise ValueError("market_invalid")
    since = _aware_utc(decided_since_utc, "decided_since_utc")
    if type(limit) is not int or not 1 <= limit <= _MAX_SYMBOLS:
        raise ValueError("limit_invalid")
    path = Path(db_path).expanduser().resolve(strict=True)
    uri = f"{path.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            """SELECT symbol, features_json
               FROM (
                   SELECT id, symbol, decided_at_utc, features_json
                   FROM shadow_decisions
                   ORDER BY id DESC
                   LIMIT ?
               ) AS bounded_recent
               WHERE decided_at_utc >= ?
               ORDER BY decided_at_utc DESC, id DESC""",
            (_MAX_DECISION_SCAN, since.strftime("%Y-%m-%dT%H:%M:%S.%fZ")),
        ).fetchall()

    result: list[str] = []
    seen: set[str] = set()
    for raw_symbol, raw_features in rows:
        try:
            features = json.loads(str(raw_features))
            if not isinstance(features, dict) or features.get("market") != market:
                continue
            symbol = str(raw_symbol).strip().upper()
            if not _SYMBOL_RE.fullmatch(symbol):
                continue
        except Exception:
            continue
        if symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
        if len(result) >= limit:
            break
    return tuple(result)


def _default_paths() -> tuple[Path, Path]:
    from config.settings import DB_DIR

    return (
        Path(DB_DIR) / "shadow_measurements.db",
        Path(DB_DIR) / "source_observations_v2.db",
    )


def _shadow_quote_fetcher(symbol: str):
    """Fetch through the non-OAuth yfinance boundary only."""
    from core.market import _get_quote_daily, _get_quote_yf_live

    return _get_quote_yf_live(symbol) or _get_quote_daily(symbol)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect shadow-only price observations")
    parser.add_argument("--market", choices=("KR", "US"), required=True)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--shadow-db")
    parser.add_argument("--source-db")
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 30:
        parser.error("--days must be between 1 and 30")

    default_shadow, default_source = _default_paths()
    shadow_path = Path(args.shadow_db) if args.shadow_db else default_shadow
    source_path = Path(args.source_db) if args.source_db else default_source
    fetch_started = datetime.now(timezone.utc)
    symbols = load_shadow_symbols(
        shadow_path,
        market=args.market,
        decided_since_utc=fetch_started - timedelta(days=args.days),
    )

    from core.source_observations_v2 import SourceObservationStoreV2

    quotes = {symbol: _shadow_quote_fetcher(symbol) for symbol in symbols}
    completed_at = datetime.now(timezone.utc)
    result = collect_price_observations(
        symbols,
        market=args.market,
        observed_at_utc=completed_at,
        quote_fetcher=quotes.get,
        store=SourceObservationStoreV2(source_path),
    )
    print(json.dumps({"market": args.market, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
