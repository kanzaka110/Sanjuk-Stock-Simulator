"""Standalone KIS/Naver point-in-time observation collector.

The module is import-side-effect free. Network fetches and v2 SQLite construction
happen only from explicit collection calls or the best-effort daemon worker.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

from core.market_data_fetch import (
    CacheSource,
    FetchErrorType,
    FetchResult,
    FetchStatus,
)

log = logging.getLogger(__name__)

ORDERBOOK_DATASET = "domestic_orderbook"
INVESTOR_DATASET = "domestic_investor_flow"
_SCHEMA_VERSION = 1
_TRANSFORM_VERSION = 1
_MAX_SYMBOLS = 10
_KR_SYMBOL_RE = re.compile(r"^[0-9]{6}\.(?:KS|KQ)$")
_KST = timezone(timedelta(hours=9))
_INVESTOR_SUCCESS_INTERVAL = timedelta(hours=20)
_INVESTOR_RETRY_INTERVAL = timedelta(hours=1)
_WORKER_LOCK = threading.Lock()
_PERSISTENCE_ERROR_TYPES = {
    "DatabaseError": "persistence_databaseerror",
    "IntegrityError": "persistence_integrityerror",
    "OperationalError": "persistence_operationalerror",
}


@dataclass(frozen=True)
class _ObservationInput:
    source: str
    dataset: str
    source_record_id: str
    symbol: str
    source_as_of: datetime
    ingested_at: datetime
    source_event_sequence: int
    fallback_used: bool
    payload: dict[str, Any]


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name}_must_be_timezone_aware")
    return value.astimezone(timezone.utc)


def _utc_from_text(value: Any, name: str) -> datetime:
    if type(value) is not str:
        raise ValueError(f"{name}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name}_invalid") from None
    return _utc(parsed, name)


def _business_close_utc(value: str) -> datetime:
    try:
        business_date = datetime.strptime(value, "%Y%m%d")
    except ValueError:
        raise ValueError("investor_date_invalid") from None
    return business_date.replace(hour=15, minute=30, tzinfo=_KST).astimezone(
        timezone.utc
    )


def _symbols(values: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("symbols_empty")
    if len(values) > _MAX_SYMBOLS:
        raise ValueError("symbols_limit_exceeded")
    normalized: list[str] = []
    for value in values:
        if type(value) is not str:
            raise ValueError("kr_symbol_invalid")
        symbol = value.strip().upper()
        if not _KR_SYMBOL_RE.fullmatch(symbol):
            raise ValueError("kr_symbol_invalid")
        normalized.append(symbol)
    if len(set(normalized)) != len(normalized):
        raise ValueError("symbols_duplicate")
    return tuple(normalized)


def _code(symbol: str) -> str:
    return symbol.split(".", 1)[0]


def _safe_error(result: FetchResult) -> str:
    value = result.error_type.value
    return "" if value == FetchErrorType.NONE.value else value


def _persistence_error_type(exc: Exception) -> str:
    return _PERSISTENCE_ERROR_TYPES.get(type(exc).__name__, "persistence_failed")


def _failed_fetch_result(
    *,
    provider: str,
    symbol: str,
    fallback_used: bool,
    started_at: datetime,
    error_type: FetchErrorType,
) -> FetchResult:
    completed_at = datetime.now(timezone.utc)
    if completed_at < started_at:
        completed_at = started_at
    return FetchResult(
        status=FetchStatus.FAILED,
        provider=provider,
        endpoint="",
        tr_id=None,
        venue="J",
        symbol=_code(symbol),
        started_at_utc=started_at,
        completed_at_utc=completed_at,
        error_type=error_type,
        cache_source=CacheSource.NONE,
        fallback_used=fallback_used,
        value=None,
    )


def _call_fetcher(
    *,
    provider: str,
    symbol: str,
    fallback_used: bool,
    callback: Callable[[], Any],
) -> FetchResult:
    started_at = datetime.now(timezone.utc)
    try:
        result = callback()
    except Exception as exc:
        log.warning(
            "KR observation fetch failed: provider=%s error_type=%s",
            provider,
            type(exc).__name__,
        )
        return _failed_fetch_result(
            provider=provider,
            symbol=symbol,
            fallback_used=fallback_used,
            started_at=started_at,
            error_type=FetchErrorType.PROVIDER,
        )
    if isinstance(result, FetchResult):
        return result
    log.warning(
        "KR observation fetch failed: provider=%s error_type=%s",
        provider,
        "MalformedResult",
    )
    return _failed_fetch_result(
        provider=provider,
        symbol=symbol,
        fallback_used=fallback_used,
        started_at=started_at,
        error_type=FetchErrorType.MALFORMED,
    )


def _fetch_payload(result: FetchResult, observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "observation": observation,
        "fetch": {
            "provider": result.provider,
            "endpoint": result.endpoint,
            "tr_id": result.tr_id,
            "venue": result.venue,
            "symbol": result.symbol,
            "started_at_utc": result.started_at_utc.isoformat(),
            "completed_at_utc": result.completed_at_utc.isoformat(),
            "source_fetched_at_utc": (
                result.source_fetched_at_utc.isoformat()
                if result.source_fetched_at_utc is not None
                else None
            ),
            "cache_source": result.cache_source.value,
            "fallback_used": result.fallback_used,
        },
    }


def _validate_result(
    result: Any,
    *,
    expected_provider: str,
    expected_code: str,
    expected_fallback: bool,
) -> FetchResult:
    if not isinstance(result, FetchResult):
        raise ValueError("fetch_result_invalid")
    if result.provider.upper() != expected_provider or result.symbol != expected_code:
        raise ValueError("fetch_result_lineage_invalid")
    if result.fallback_used is not expected_fallback:
        raise ValueError("fetch_result_fallback_invalid")
    return result


def _validate_orderbook_value(
    value: Any, *, symbol: str, completed_at: datetime
) -> tuple[dict[str, Any], datetime]:
    if type(value) is not dict:
        raise ValueError("orderbook_value_invalid")
    if value.get("ticker") != symbol or value.get("symbol") != _code(symbol):
        raise ValueError("orderbook_value_lineage_invalid")
    source_as_of = _utc_from_text(value.get("source_as_of"), "source_as_of")
    if source_as_of != completed_at:
        raise ValueError("orderbook_source_time_invalid")
    levels = value.get("levels")
    if not isinstance(levels, list) or len(levels) != 10:
        raise ValueError("orderbook_depth_invalid")
    for expected_level, level in enumerate(levels, start=1):
        if type(level) is not dict or level.get("level") != expected_level:
            raise ValueError("orderbook_depth_invalid")
        for field in ("ask_price", "ask_size", "bid_price", "bid_size"):
            metric = level.get(field)
            if type(metric) is not int or metric < 0:
                raise ValueError("orderbook_depth_invalid")
    if any(
        type(value.get(field)) is not dict
        for field in ("raw_totals", "expected_execution", "units")
    ):
        raise ValueError("orderbook_value_invalid")
    return value, source_as_of


def _orderbook_observations(
    symbol_results: list[tuple[str, FetchResult]],
) -> tuple[list[_ObservationInput], int, list[str]]:
    observations: list[_ObservationInput] = []
    invalid = 0
    errors: list[str] = []
    for symbol, raw_result in symbol_results:
        try:
            result = _validate_result(
                raw_result,
                expected_provider="KIS",
                expected_code=_code(symbol),
                expected_fallback=False,
            )
            if result.status is not FetchStatus.SUCCESS:
                if result.status not in {FetchStatus.EMPTY, FetchStatus.SKIPPED}:
                    errors.append(_safe_error(result) or "fetch_failed")
                elif _safe_error(result):
                    errors.append(_safe_error(result))
                continue
            completed = _utc(result.completed_at_utc, "completed_at_utc")
            ingested_at = _utc(
                result.source_fetched_at_utc or result.completed_at_utc,
                "source_fetched_at_utc",
            )
            payload_value, source_as_of = _validate_orderbook_value(
                result.value,
                symbol=symbol,
                completed_at=completed,
            )
            observations.append(
                _ObservationInput(
                    source="kis",
                    dataset=ORDERBOOK_DATASET,
                    source_record_id=f"{symbol}:{source_as_of.isoformat()}",
                    symbol=symbol,
                    source_as_of=source_as_of,
                    ingested_at=ingested_at,
                    source_event_sequence=0,
                    fallback_used=False,
                    payload=_fetch_payload(result, payload_value),
                )
            )
        except Exception:
            invalid += 1
            errors.append("malformed")
    return observations, invalid, errors


def _investor_observations(
    symbol_results: list[tuple[str, FetchResult]],
    *,
    source: str,
    expected_provider: str,
    fallback_used: bool,
) -> tuple[list[_ObservationInput], int, list[str], set[str]]:
    observations: list[_ObservationInput] = []
    invalid = 0
    errors: list[str] = []
    invalid_symbols: set[str] = set()
    for symbol, raw_result in symbol_results:
        try:
            result = _validate_result(
                raw_result,
                expected_provider=expected_provider,
                expected_code=_code(symbol),
                expected_fallback=fallback_used,
            )
            if result.status is not FetchStatus.SUCCESS:
                if result.status not in {FetchStatus.EMPTY, FetchStatus.SKIPPED}:
                    errors.append(_safe_error(result) or "fetch_failed")
                elif _safe_error(result):
                    errors.append(_safe_error(result))
                continue
            parent_units: dict[str, Any] | None = None
            derived_schema_version: Any = None
            if source == "naver":
                if type(result.value) is not dict:
                    raise ValueError("investor_value_invalid")
                if result.value.get("code") != _code(symbol):
                    raise ValueError("investor_value_lineage_invalid")
                rows = result.value.get("rows")
                parent_units = result.value.get("units")
                derived_schema_version = result.value.get("derived_schema_version")
                if not isinstance(rows, list) or type(parent_units) is not dict:
                    raise ValueError("investor_value_invalid")
            else:
                if not isinstance(result.value, list):
                    raise ValueError("investor_value_invalid")
                rows = result.value
            ingested_at = _utc(result.completed_at_utc, "completed_at_utc")
            source_fetched_at = _utc(
                result.source_fetched_at_utc or result.completed_at_utc,
                "source_fetched_at_utc",
            )
            per_source_time: dict[str, int] = {}
            result_observations: list[_ObservationInput] = []
            for row in rows:
                if type(row) is not dict:
                    raise ValueError("investor_row_invalid")
                date = row.get("date")
                if type(date) is not str or len(date) != 8 or not date.isdigit():
                    raise ValueError("investor_date_invalid")
                if source == "naver":
                    assert parent_units is not None
                    source_as_of = _business_close_utc(date)
                    availability_as_of = source_fetched_at
                    normalized_row = {
                        **row,
                        "source_as_of": source_as_of.isoformat(),
                        "source_as_of_precision": "business_date",
                        "availability_as_of": availability_as_of.isoformat(),
                        "intraday": False,
                        "units": dict(parent_units),
                        "derived_schema_version": derived_schema_version,
                    }
                else:
                    source_as_of = _utc_from_text(
                        row.get("source_as_of"), "source_as_of"
                    )
                    availability_as_of = _utc_from_text(
                        row.get("availability_as_of"), "availability_as_of"
                    )
                    if (
                        row.get("ticker") != symbol
                        or row.get("symbol") != _code(symbol)
                        or row.get("source_as_of_precision") != "business_date"
                        or row.get("intraday") is not False
                        or type(row.get("units")) is not dict
                        or type(row.get("official_fields")) is not dict
                        or source_as_of != _business_close_utc(date)
                        or availability_as_of != ingested_at
                    ):
                        raise ValueError("investor_value_lineage_invalid")
                    normalized_row = row
                if not source_as_of <= availability_as_of <= ingested_at:
                    raise ValueError("investor_value_lineage_invalid")
                source_key = source_as_of.isoformat()
                sequence = per_source_time.get(source_key, 0)
                per_source_time[source_key] = sequence + 1
                result_observations.append(
                    _ObservationInput(
                        source=source,
                        dataset=INVESTOR_DATASET,
                        source_record_id=f"{symbol}:{date}:{sequence}",
                        symbol=symbol,
                        source_as_of=source_as_of,
                        ingested_at=ingested_at,
                        source_event_sequence=sequence,
                        fallback_used=fallback_used,
                        payload=_fetch_payload(result, normalized_row),
                    )
                )
            observations.extend(result_observations)
        except Exception:
            invalid += 1
            errors.append("malformed")
            invalid_symbols.add(symbol)
    return observations, invalid, errors, invalid_symbols


def _run_status(
    results: list[FetchResult], observations: list[_ObservationInput], invalid: int, errors: list[str]
) -> tuple[str, str]:
    if observations:
        return ("partial", errors[0]) if errors or invalid else ("success", "")
    if invalid:
        return "failed", errors[0] if errors else "malformed"
    if any(result.status in {FetchStatus.FAILED, FetchStatus.INCOMPLETE} for result in results):
        return "failed", errors[0] if errors else "fetch_failed"
    return "skipped", errors[0] if errors else ""


def _summary(
    *,
    source: str,
    dataset: str,
    status: str,
    rows_seen: int,
    rows_inserted: int,
    rows_duplicate: int,
    rows_skipped: int,
    rows_invalid: int,
    error_type: str,
) -> dict[str, Any]:
    return {
        "source": source,
        "dataset": dataset,
        "status": status,
        "rows_seen": rows_seen,
        "rows_inserted": rows_inserted,
        "rows_duplicate": rows_duplicate,
        "rows_skipped": rows_skipped,
        "rows_invalid": rows_invalid,
        "error_type": error_type,
    }


def _persist_batch(
    *,
    store,
    source: str,
    dataset: str,
    run_id: str,
    results: list[FetchResult],
    observations: list[_ObservationInput],
    invalid: int,
    errors: list[str],
) -> dict[str, Any]:
    started_at = min(_utc(result.started_at_utc, "started_at_utc") for result in results)
    completed_at = max(_utc(result.completed_at_utc, "completed_at_utc") for result in results)
    status, error_type = _run_status(results, observations, invalid, errors)

    def write(conn):
        inserted = 0
        duplicate = 0
        for item in observations:
            append_result = store.append(
                source=item.source,
                dataset=item.dataset,
                source_record_id=item.source_record_id,
                symbol=item.symbol,
                market="KR",
                currency_or_unit="MIXED",
                source_as_of=item.source_as_of,
                source_event_sequence=item.source_event_sequence,
                ingested_at=item.ingested_at,
                schema_version=_SCHEMA_VERSION,
                transform_version=_TRANSFORM_VERSION,
                fallback_used=item.fallback_used,
                payload=item.payload,
                _conn=conn,
            )
            if append_result.inserted:
                inserted += 1
            else:
                duplicate += 1
        rows_seen = len(observations) + invalid
        store.record_collection_run(
            source=source,
            dataset=dataset,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            status=status,
            rows_seen=rows_seen,
            rows_inserted=inserted,
            rows_duplicate=duplicate,
            rows_skipped=0,
            rows_invalid=invalid,
            error_type=error_type,
            _conn=conn,
        )
        return _summary(
            source=source,
            dataset=dataset,
            status=status,
            rows_seen=rows_seen,
            rows_inserted=inserted,
            rows_duplicate=duplicate,
            rows_skipped=0,
            rows_invalid=invalid,
            error_type=error_type,
        )

    try:
        return store.atomic_write(write)
    except Exception as exc:
        persistence_error = _persistence_error_type(exc)
        failed_seen = len(observations) + invalid
        store.record_collection_run(
            source=source,
            dataset=dataset,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            status="failed",
            rows_seen=failed_seen,
            rows_inserted=0,
            rows_duplicate=0,
            rows_skipped=0,
            rows_invalid=failed_seen,
            error_type=persistence_error,
        )
        log.warning(
            "KR observation persistence failed: error_type=%s failed_count=%d",
            type(exc).__name__,
            failed_seen,
        )
        return _summary(
            source=source,
            dataset=dataset,
            status="failed",
            rows_seen=failed_seen,
            rows_inserted=0,
            rows_duplicate=0,
            rows_skipped=0,
            rows_invalid=failed_seen,
            error_type=persistence_error,
        )


def collect_orderbook_observations(
    symbols: Sequence[str],
    *,
    store,
    run_id: str,
    fetcher: Callable[[str], FetchResult] | None = None,
) -> dict[str, Any]:
    """Collect one point-in-time KIS orderbook snapshot per bounded KR symbol."""
    normalized = _symbols(symbols)
    if fetcher is None:
        from core.market_kis import fetch_domestic_orderbook_result

        fetcher = fetch_domestic_orderbook_result
    assert fetcher is not None
    pairs = [
        (
            symbol,
            _call_fetcher(
                provider="KIS",
                symbol=symbol,
                fallback_used=False,
                callback=lambda symbol=symbol: fetcher(symbol),
            ),
        )
        for symbol in normalized
    ]
    observations, invalid, errors = _orderbook_observations(pairs)
    return _persist_batch(
        store=store,
        source="kis",
        dataset=ORDERBOOK_DATASET,
        run_id=run_id,
        results=[result for _, result in pairs],
        observations=observations,
        invalid=invalid,
        errors=errors,
    )


def collect_investor_observations(
    symbols: Sequence[str],
    *,
    store,
    run_id: str,
    kis_fetcher: Callable[[str], FetchResult] | None = None,
    naver_fetcher: Callable[..., FetchResult] | None = None,
) -> dict[str, Any]:
    """Collect KIS EOD investor rows, using Naver only for explicit fetch failures."""
    normalized = _symbols(symbols)
    if kis_fetcher is None:
        from core.market_kis import fetch_domestic_investor_result

        kis_fetcher = fetch_domestic_investor_result
    if naver_fetcher is None:
        from core.kr_market import fetch_naver_frgn_result

        naver_fetcher = fetch_naver_frgn_result
    assert kis_fetcher is not None
    assert naver_fetcher is not None

    kis_pairs = [
        (
            symbol,
            _call_fetcher(
                provider="KIS",
                symbol=symbol,
                fallback_used=False,
                callback=lambda symbol=symbol: kis_fetcher(symbol),
            ),
        )
        for symbol in normalized
    ]
    kis_observations, kis_invalid, kis_errors, kis_invalid_symbols = (
        _investor_observations(
            kis_pairs,
            source="kis",
            expected_provider="KIS",
            fallback_used=False,
        )
    )
    kis_summary = _persist_batch(
        store=store,
        source="kis",
        dataset=INVESTOR_DATASET,
        run_id=run_id,
        results=[result for _, result in kis_pairs],
        observations=kis_observations,
        invalid=kis_invalid,
        errors=kis_errors,
    )

    fallback_symbols = [
        symbol
        for symbol, result in kis_pairs
        if isinstance(result, FetchResult)
        and (
            result.status
            in {FetchStatus.FAILED, FetchStatus.SKIPPED, FetchStatus.INCOMPLETE}
            or symbol in kis_invalid_symbols
        )
    ]
    if not fallback_symbols:
        return {"kis": kis_summary, "naver": None}

    naver_pairs = [
        (
            symbol,
            _call_fetcher(
                provider="NAVER",
                symbol=symbol,
                fallback_used=True,
                callback=lambda symbol=symbol: naver_fetcher(
                    _code(symbol), fallback_used=True
                ),
            ),
        )
        for symbol in fallback_symbols
    ]
    naver_observations, naver_invalid, naver_errors, _ = _investor_observations(
        naver_pairs,
        source="naver",
        expected_provider="NAVER",
        fallback_used=True,
    )
    naver_summary = _persist_batch(
        store=store,
        source="naver",
        dataset=INVESTOR_DATASET,
        run_id=run_id,
        results=[result for _, result in naver_pairs],
        observations=naver_observations,
        invalid=naver_invalid,
        errors=naver_errors,
    )
    return {"kis": kis_summary, "naver": naver_summary}


def _default_store():
    from config.settings import DB_DIR
    from core.source_observations_v2 import SourceObservationStoreV2

    return SourceObservationStoreV2(DB_DIR / "source_observations_v2.db")


def _investor_collection_due(store, now_utc: datetime) -> bool:
    latest = store.latest_collection_run("kis", INVESTOR_DATASET)
    if latest is None:
        return True
    completed_at = _utc_from_text(latest.completed_at, "collection_completed_at")
    interval = (
        _INVESTOR_SUCCESS_INTERVAL
        if latest.status == "success"
        else _INVESTOR_RETRY_INTERVAL
    )
    return now_utc - completed_at >= interval


def run_candidate_observation_cycle(
    symbols: Sequence[str],
    *,
    run_id: str | None = None,
    store=None,
    now_utc: datetime | None = None,
    orderbook_fetcher=None,
    kis_investor_fetcher=None,
    naver_fetcher=None,
) -> dict[str, Any]:
    """Run orderbook and throttled EOD flow collection for one exact cohort."""
    normalized = _symbols(symbols)
    resolved_store = store if store is not None else _default_store()
    resolved_run_id = run_id or f"kr-observation-{uuid.uuid4().hex}"
    resolved_now = _utc(
        now_utc if now_utc is not None else datetime.now(timezone.utc),
        "now_utc",
    )
    orderbook = collect_orderbook_observations(
        normalized,
        store=resolved_store,
        run_id=resolved_run_id,
        fetcher=orderbook_fetcher,
    )
    investor = (
        collect_investor_observations(
            normalized,
            store=resolved_store,
            run_id=resolved_run_id,
            kis_fetcher=kis_investor_fetcher,
            naver_fetcher=naver_fetcher,
        )
        if _investor_collection_due(resolved_store, resolved_now)
        else {"skipped": "throttled"}
    )
    return {
        "symbols": list(normalized),
        "orderbook": orderbook,
        "investor": investor,
    }


def _worker(
    symbols: tuple[str, ...],
    run_id: str | None,
    store,
    orderbook_fetcher,
    kis_investor_fetcher,
    naver_fetcher,
) -> None:
    try:
        run_candidate_observation_cycle(
            symbols,
            run_id=run_id,
            store=store,
            orderbook_fetcher=orderbook_fetcher,
            kis_investor_fetcher=kis_investor_fetcher,
            naver_fetcher=naver_fetcher,
        )
    except Exception as exc:
        log.warning(
            "KR observation worker failed: error_type=%s failed_count=%d",
            type(exc).__name__,
            len(symbols),
        )
    finally:
        _WORKER_LOCK.release()


def enqueue_candidate_observation_cycle(
    symbols: Sequence[str],
    *,
    run_id: str | None = None,
    store=None,
    orderbook_fetcher=None,
    kis_investor_fetcher=None,
    naver_fetcher=None,
) -> bool:
    """Start one best-effort collector daemon, or drop immediately while busy."""
    normalized = _symbols(symbols)
    if not _WORKER_LOCK.acquire(blocking=False):
        return False
    try:
        worker = threading.Thread(
            target=_worker,
            args=(
                normalized,
                run_id,
                store,
                orderbook_fetcher,
                kis_investor_fetcher,
                naver_fetcher,
            ),
            daemon=True,
            name="kr-market-observation-collector",
        )
        worker.start()
    except Exception as exc:
        _WORKER_LOCK.release()
        log.warning(
            "KR observation worker start failed: error_type=%s failed_count=%d",
            type(exc).__name__,
            len(normalized),
        )
        return False
    return True
