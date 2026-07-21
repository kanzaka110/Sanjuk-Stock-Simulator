"""Typed KRX Open API daily close provider.

The provider is standalone and read-only. It is not imported by scoring, candidate,
broker, or order paths. Credentials are never retained in result objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
import argparse
import json
import os
from pathlib import Path
import re
from typing import Any, Callable


_BASE_URL = "https://data-dbg.krx.co.kr/svc/apis/sto"
_ENDPOINTS = {
    "KOSPI": ("stk_bydd_trd", "stk_isu_base_info"),
    "KOSDAQ": ("ksq_bydd_trd", "ksq_isu_base_info"),
}
_SUFFIX_MARKET = {"KS": "KOSPI", "KQ": "KOSDAQ"}
_QUOTE_MARKET_LABELS = {"KOSPI": frozenset({"KOSPI"}), "KOSDAQ": frozenset({"KOSDAQ"})}
_BASE_MARKET_LABELS = {
    "KOSPI": frozenset({"KOSPI"}),
    "KOSDAQ": frozenset({"KOSDAQ", "KOSDAQ GLOBAL"}),
}
_SYMBOL_RE = re.compile(r"^([0-9]{6})\.(KS|KQ)$")
_INTEGER_RE = re.compile(r"^[+]?[0-9]{1,21}(?:,[0-9]{3})*$|^[+]?[0-9]{1,21}$")
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_MAX_SYMBOLS = 10


class KRXStatus(str, Enum):
    SUCCESS = "success"
    EMPTY = "empty"
    FAILED = "failed"
    SKIPPED = "skipped"


class KRXError(str, Enum):
    NONE = "none"
    NOT_CONFIGURED = "not_configured"
    AUTH = "auth"
    HTTP = "http"
    NETWORK = "network"
    PROVIDER = "provider"
    MALFORMED = "malformed"
    NUMERIC = "numeric"


@dataclass(frozen=True)
class KRXFetchResult:
    status: KRXStatus
    error: KRXError
    rows: tuple[dict[str, Any], ...]
    requested_date: str
    requested_markets: tuple[str, ...]


class _PayloadError(Exception):
    def __init__(self, error: KRXError):
        super().__init__(error.value)
        self.error = error


def _result(
    *,
    status: KRXStatus,
    error: KRXError,
    business_date: str,
    markets: tuple[str, ...],
    rows: tuple[dict[str, Any], ...] = (),
) -> KRXFetchResult:
    return KRXFetchResult(status, error, rows, business_date, markets)


def _default_transport(url: str, **kwargs):
    import requests

    return requests.get(url, **kwargs)


def _credential(value: object) -> str | None:
    if value is None:
        value = os.environ.get("KRX_AUTH_KEY")
    if type(value) is not str or not value.strip():
        return None
    if value != value.strip():
        return None
    return value


def _symbols(values: object) -> tuple[tuple[str, str, str], ...]:
    if type(values) not in {list, tuple} or not 1 <= len(values) <= _MAX_SYMBOLS:
        raise ValueError("symbols_invalid")
    normalized: list[tuple[str, str, str]] = []
    for value in values:
        if type(value) is not str:
            raise ValueError("symbols_invalid")
        match = _SYMBOL_RE.fullmatch(value)
        if match is None:
            raise ValueError("symbols_invalid")
        code, suffix = match.groups()
        normalized.append((value, code, _SUFFIX_MARKET[suffix]))
    if len({item[0] for item in normalized}) != len(normalized):
        raise ValueError("symbols_invalid")
    return tuple(normalized)


def _integer(value: object) -> int:
    if type(value) is not str or not _INTEGER_RE.fullmatch(value):
        raise _PayloadError(KRXError.NUMERIC)
    try:
        parsed = int(value.replace(",", ""))
    except ValueError:
        raise _PayloadError(KRXError.NUMERIC) from None
    if parsed < 0 or parsed.bit_length() > 63:
        raise _PayloadError(KRXError.NUMERIC)
    return parsed


def _valid_isin(value: object) -> bool:
    if type(value) is not str or _ISIN_RE.fullmatch(value) is None:
        return False
    digits = "".join(str(ord(char) - 55) if "A" <= char <= "Z" else char for char in value)
    total = 0
    for index, character in enumerate(reversed(digits)):
        number = int(character)
        if index % 2 == 1:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def _payload(response: object) -> dict[str, Any]:
    status_code = getattr(response, "status_code", None)
    if type(status_code) is not int:
        raise _PayloadError(KRXError.HTTP)
    if status_code in {401, 403}:
        raise _PayloadError(KRXError.AUTH)
    if status_code < 200 or status_code >= 300:
        raise _PayloadError(KRXError.HTTP)
    try:
        value = response.json()
    except Exception:
        raise _PayloadError(KRXError.MALFORMED) from None
    if type(value) is not dict:
        raise _PayloadError(KRXError.MALFORMED)
    if "respCode" in value:
        code = value["respCode"]
        if type(code) is not str:
            raise _PayloadError(KRXError.MALFORMED)
        if code in {"401", "403"}:
            raise _PayloadError(KRXError.AUTH)
        raise _PayloadError(KRXError.PROVIDER)
    rows = value.get("OutBlock_1")
    if type(rows) is not list or any(type(row) is not dict for row in rows):
        raise _PayloadError(KRXError.MALFORMED)
    return value


def _request(
    endpoint: str,
    *,
    key: str,
    business_date: str,
    transport: Callable[..., object],
) -> dict[str, Any]:
    try:
        response = transport(
            f"{_BASE_URL}/{endpoint}",
            headers={"AUTH_KEY": key},
            params={"basDd": business_date},
            timeout=15,
            allow_redirects=False,
        )
    except Exception:
        raise _PayloadError(KRXError.NETWORK) from None
    return _payload(response)


def _quote_rows(payload: dict[str, Any], *, business_date: str, market: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in payload["OutBlock_1"]:
        required = {"BAS_DD", "ISU_CD", "ISU_NM", "MKT_NM", "TDD_CLSPRC", "ACC_TRDVOL", "ACC_TRDVAL"}
        if not required.issubset(raw):
            raise _PayloadError(KRXError.MALFORMED)
        code = raw["ISU_CD"]
        if (
            type(raw["BAS_DD"]) is not str
            or raw["BAS_DD"] != business_date
            or type(code) is not str
            or re.fullmatch(r"[0-9]{6}", code) is None
            or type(raw["ISU_NM"]) is not str
            or not raw["ISU_NM"]
            or type(raw["MKT_NM"]) is not str
            or raw["MKT_NM"] not in _QUOTE_MARKET_LABELS[market]
            or code in result
        ):
            raise _PayloadError(KRXError.MALFORMED)
        result[code] = {
            "business_date": business_date,
            "market": market,
            "close_krw": _integer(raw["TDD_CLSPRC"]),
            "volume_shares": _integer(raw["ACC_TRDVOL"]),
            "trade_value_krw": _integer(raw["ACC_TRDVAL"]),
        }
    return result


def _base_rows(payload: dict[str, Any], *, business_date: str, market: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in payload["OutBlock_1"]:
        required = {"BAS_DD", "ISU_CD", "ISU_SRT_CD", "MKT_TP_NM"}
        if not required.issubset(raw):
            raise _PayloadError(KRXError.MALFORMED)
        code = raw["ISU_SRT_CD"]
        isin = raw["ISU_CD"]
        if (
            type(raw["BAS_DD"]) is not str
            or raw["BAS_DD"] != business_date
            or type(code) is not str
            or re.fullmatch(r"[0-9]{6}", code) is None
            or not _valid_isin(isin)
            or type(raw["MKT_TP_NM"]) is not str
            or raw["MKT_TP_NM"] not in _BASE_MARKET_LABELS[market]
            or code in result
        ):
            raise _PayloadError(KRXError.MALFORMED)
        result[code] = isin
    return result


def fetch_krx_daily(
    *,
    business_date: date,
    symbols: object,
    auth_key: object = None,
    transport: Callable[..., object] | None = None,
) -> KRXFetchResult:
    """Fetch and validate complete KRX market responses before cohort projection."""
    if type(business_date) is not date:
        raise ValueError("business_date_invalid")
    requested = _symbols(symbols)
    business_date_text = business_date.strftime("%Y%m%d")
    markets = tuple(market for market in _ENDPOINTS if any(item[2] == market for item in requested))
    key = _credential(auth_key)
    if key is None:
        return _result(
            status=KRXStatus.SKIPPED,
            error=KRXError.NOT_CONFIGURED,
            business_date=business_date_text,
            markets=markets,
        )
    request = transport if transport is not None else _default_transport

    projected: list[dict[str, Any]] = []
    empty_markets: list[str] = []
    try:
        for market in markets:
            quote_endpoint, base_endpoint = _ENDPOINTS[market]
            quote_payload = _request(
                quote_endpoint,
                key=key,
                business_date=business_date_text,
                transport=request,
            )
            base_payload = _request(
                base_endpoint,
                key=key,
                business_date=business_date_text,
                transport=request,
            )
            quotes = _quote_rows(quote_payload, business_date=business_date_text, market=market)
            bases = _base_rows(base_payload, business_date=business_date_text, market=market)
            market_requests = [item for item in requested if item[2] == market]
            requested_codes = {item[1] for item in market_requests}
            if not quotes:
                if bases and not requested_codes.issubset(bases):
                    raise _PayloadError(KRXError.MALFORMED)
                empty_markets.append(market)
                continue
            if not requested_codes.issubset(quotes) or not requested_codes.issubset(bases):
                raise _PayloadError(KRXError.MALFORMED)
            for ticker, code, _ in market_requests:
                projected.append(
                    {
                        **quotes[code],
                        "ticker": ticker,
                        "isin": bases[code],
                    }
                )
    except _PayloadError as exc:
        return _result(
            status=KRXStatus.FAILED,
            error=exc.error,
            business_date=business_date_text,
            markets=markets,
        )

    if empty_markets and projected:
        return _result(
            status=KRXStatus.FAILED,
            error=KRXError.PROVIDER,
            business_date=business_date_text,
            markets=markets,
        )
    return _result(
        status=KRXStatus.SUCCESS if projected else KRXStatus.EMPTY,
        error=KRXError.NONE,
        business_date=business_date_text,
        markets=markets,
        rows=tuple(projected),
    )


_SOURCE = "krx_openapi"
_DATASET = "domestic_eod_quote"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_KST = timezone(timedelta(hours=9))


def _aware_utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name}_timezone_aware_required")
    return value.astimezone(timezone.utc)


def _source_as_of(value: str) -> datetime:
    parsed = datetime.strptime(value, "%Y%m%d")
    return parsed.replace(hour=15, minute=30, tzinfo=_KST).astimezone(timezone.utc)


def _summary(
    *,
    status: str,
    seen: int,
    inserted: int,
    duplicate: int,
    skipped: int,
    invalid: int,
    error: str,
) -> dict[str, Any]:
    return {
        "source": _SOURCE,
        "dataset": _DATASET,
        "status": status,
        "rows_seen": seen,
        "rows_inserted": inserted,
        "rows_duplicate": duplicate,
        "rows_skipped": skipped,
        "rows_invalid": invalid,
        "error_type": error,
    }


_PERSISTED_ROW_KEYS = frozenset(
    {
        "business_date",
        "ticker",
        "isin",
        "market",
        "close_krw",
        "volume_shares",
        "trade_value_krw",
    }
)


def _validated_result_rows(result: object) -> tuple[dict[str, Any], ...]:
    if (
        type(result) is not KRXFetchResult
        or result.status is not KRXStatus.SUCCESS
        or result.error is not KRXError.NONE
        or type(result.rows) is not tuple
        or not result.rows
        or type(result.requested_markets) is not tuple
        or not result.requested_markets
        or len(set(result.requested_markets)) != len(result.requested_markets)
        or any(market not in _ENDPOINTS for market in result.requested_markets)
    ):
        raise ValueError("krx_result_not_persistable")
    try:
        requested_date = _parse_business_date(result.requested_date).strftime("%Y%m%d")
    except ValueError:
        raise ValueError("krx_result_row_invalid") from None

    validated: list[dict[str, Any]] = []
    tickers: set[str] = set()
    isins: set[str] = set()
    for row in result.rows:
        if type(row) is not dict or set(row) != _PERSISTED_ROW_KEYS:
            raise ValueError("krx_result_row_invalid")
        ticker = row.get("ticker")
        match = _SYMBOL_RE.fullmatch(ticker) if type(ticker) is str else None
        market = row.get("market")
        isin = row.get("isin")
        numeric = (row.get("close_krw"), row.get("volume_shares"), row.get("trade_value_krw"))
        if (
            row.get("business_date") != requested_date
            or match is None
            or market not in result.requested_markets
            or _SUFFIX_MARKET[match.group(2)] != market
            or not _valid_isin(isin)
            or any(type(value) is not int or value < 0 or value.bit_length() > 63 for value in numeric)
            or ticker in tickers
            or isin in isins
        ):
            raise ValueError("krx_result_row_invalid")
        assert isinstance(ticker, str)
        assert isinstance(isin, str)
        tickers.add(ticker)
        isins.add(isin)
        validated.append(dict(row))
    return tuple(validated)


def persist_krx_daily_result(
    result: KRXFetchResult,
    *,
    store: object,
    ingested_at_utc: datetime,
    run_id: str,
) -> dict[str, Any]:
    """Persist one fully validated provider batch and run ledger atomically."""
    rows = _validated_result_rows(result)
    if type(run_id) is not str or not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id_invalid")
    ingested_at = _aware_utc(ingested_at_utc, "ingested_at_utc")
    source_as_of = _source_as_of(result.requested_date)
    if source_as_of > ingested_at:
        raise ValueError("source_as_of_after_ingested_at")
    if not hasattr(store, "atomic_write") or not hasattr(store, "append") or not hasattr(store, "record_collection_run"):
        raise ValueError("store_invalid")

    def write(connection):
        high_water = connection.execute(
            """SELECT MAX(value) FROM (
                   SELECT MAX(ingested_at) AS value FROM observations
                   WHERE source = ? AND dataset = ? AND market = 'KR'
                   UNION ALL
                   SELECT MAX(completed_at) AS value FROM collection_runs
                   WHERE source = ? AND dataset = ?
               )""",
            (_SOURCE, _DATASET, _SOURCE, _DATASET),
        ).fetchone()[0]
        incoming = ingested_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if high_water is not None and high_water > incoming:
            raise ValueError("collection_clock_regression")

        inserted = 0
        duplicate = 0
        for row in rows:
            source_record_id = f"{row['business_date']}:{row['isin']}"
            base_payload = {
                "business_date": row["business_date"],
                "ticker": row["ticker"],
                "isin": row["isin"],
                "market": row["market"],
                "close_krw": row["close_krw"],
                "volume_shares": row["volume_shares"],
                "trade_value_krw": row["trade_value_krw"],
                "source_origin": "KRX",
                "collector": _SOURCE,
                "units": {
                    "close_krw": "KRW/share",
                    "volume_shares": "shares",
                    "trade_value_krw": "KRW",
                },
            }
            previous = connection.execute(
                """SELECT snapshot_id,source_event_sequence,payload_json
                   FROM observations
                   WHERE source = ? AND dataset = ? AND source_record_id = ?
                   ORDER BY source_event_sequence DESC,id DESC LIMIT 1""",
                (_SOURCE, _DATASET, source_record_id),
            ).fetchone()
            event_sequence = 0
            payload = base_payload
            if previous is not None:
                previous_snapshot_id, previous_sequence, previous_payload_json = previous
                try:
                    previous_payload = json.loads(previous_payload_json)
                except (TypeError, json.JSONDecodeError):
                    raise ValueError("prior_krx_payload_invalid") from None
                if type(previous_payload) is not dict or type(previous_sequence) is not int or previous_sequence < 0:
                    raise ValueError("prior_krx_payload_invalid")
                previous_core = {
                    key: value
                    for key, value in previous_payload.items()
                    if key != "correction_of_snapshot_id"
                }
                if previous_core == base_payload:
                    event_sequence = previous_sequence
                    payload = previous_payload
                else:
                    event_sequence = previous_sequence + 1
                    payload = {
                        **base_payload,
                        "correction_of_snapshot_id": previous_snapshot_id,
                    }
            appended = store.append(
                source=_SOURCE,
                dataset=_DATASET,
                source_record_id=source_record_id,
                symbol=row["ticker"],
                market="KR",
                currency_or_unit="MIXED",
                source_as_of=source_as_of,
                source_event_sequence=event_sequence,
                ingested_at=ingested_at,
                schema_version=1,
                transform_version=1,
                fallback_used=False,
                payload=payload,
                _conn=connection,
            )
            inserted += int(appended.inserted)
            duplicate += int(not appended.inserted)
        store.record_collection_run(
            source=_SOURCE,
            dataset=_DATASET,
            run_id=run_id,
            started_at=ingested_at,
            completed_at=ingested_at,
            status="success",
            rows_seen=len(rows),
            rows_inserted=inserted,
            rows_duplicate=duplicate,
            rows_skipped=0,
            rows_invalid=0,
            error_type="",
            _conn=connection,
        )
        return _summary(
            status="success",
            seen=len(rows),
            inserted=inserted,
            duplicate=duplicate,
            skipped=0,
            invalid=0,
            error="",
        )

    return store.atomic_write(write)


def record_krx_non_success_result(
    result: KRXFetchResult,
    *,
    store: object,
    completed_at_utc: datetime,
    run_id: str,
    rows_seen: int,
) -> dict[str, Any]:
    """Record a configured empty/failed provider attempt without persisting observations."""
    if (
        type(result) is not KRXFetchResult
        or result.status not in {KRXStatus.EMPTY, KRXStatus.FAILED}
        or result.error is KRXError.NOT_CONFIGURED
        or result.rows
    ):
        raise ValueError("krx_non_success_result_invalid")
    if type(run_id) is not str or not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id_invalid")
    if type(rows_seen) is not int or not 0 <= rows_seen <= _MAX_SYMBOLS:
        raise ValueError("rows_seen_invalid")
    completed_at = _aware_utc(completed_at_utc, "completed_at_utc")
    record_run = getattr(store, "record_collection_run", None)
    if not callable(record_run):
        raise ValueError("store_invalid")
    status = "failed" if result.status is KRXStatus.FAILED else "skipped"
    error = "" if result.error is KRXError.NONE else result.error.value
    invalid = rows_seen if status == "failed" else 0
    skipped = rows_seen if status == "skipped" else 0
    record_run(
        source=_SOURCE,
        dataset=_DATASET,
        run_id=run_id,
        started_at=completed_at,
        completed_at=completed_at,
        status=status,
        rows_seen=rows_seen,
        rows_inserted=0,
        rows_duplicate=0,
        rows_skipped=skipped,
        rows_invalid=invalid,
        error_type=error,
    )
    return _summary(
        status=status,
        seen=rows_seen,
        inserted=0,
        duplicate=0,
        skipped=skipped,
        invalid=invalid,
        error=error,
    )


def _parse_business_date(value: str) -> date:
    if type(value) is not str or not re.fullmatch(r"[0-9]{8}", value):
        raise ValueError("business_date_invalid")
    try:
        parsed = datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        raise ValueError("business_date_invalid") from None
    if parsed.strftime("%Y%m%d") != value:
        raise ValueError("business_date_invalid")
    return parsed


def _default_db_path() -> Path:
    from config.settings import DB_DIR

    return Path(DB_DIR) / "source_observations_v2.db"


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect official KRX daily close observations")
    parser.add_argument("--date", required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--db")
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    try:
        business_date = _parse_business_date(args.date)
        requested = _symbols(args.symbols)
        run_id = args.run_id
        if run_id is None:
            run_id = f"krx-{args.date}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
        if type(run_id) is not str or not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError("run_id_invalid")

        result = fetch_krx_daily(
            business_date=business_date,
            symbols=[item[0] for item in requested],
        )
        if result.status is not KRXStatus.SUCCESS:
            seen = len(requested)
            if result.error is KRXError.NOT_CONFIGURED:
                output = _summary(
                    status=result.status.value,
                    seen=seen,
                    inserted=0,
                    duplicate=0,
                    skipped=seen,
                    invalid=0,
                    error=result.error.value,
                )
                print(json.dumps(output, sort_keys=True, separators=(",", ":")))
                return 2

            from core.source_observations_v2 import SourceObservationStoreV2

            store = SourceObservationStoreV2(Path(args.db) if args.db else _default_db_path())
            output = record_krx_non_success_result(
                result,
                store=store,
                completed_at_utc=datetime.now(timezone.utc),
                run_id=run_id,
                rows_seen=seen,
            )
            print(json.dumps(output, sort_keys=True, separators=(",", ":")))
            return 2

        from core.source_observations_v2 import SourceObservationStoreV2

        store = SourceObservationStoreV2(Path(args.db) if args.db else _default_db_path())
        ingested_at = datetime.now(timezone.utc)
        try:
            output = persist_krx_daily_result(
                result,
                store=store,
                ingested_at_utc=ingested_at,
                run_id=run_id,
            )
        except Exception:
            store.record_collection_run(
                source=_SOURCE,
                dataset=_DATASET,
                run_id=run_id,
                started_at=ingested_at,
                completed_at=ingested_at,
                status="failed",
                rows_seen=len(result.rows),
                rows_inserted=0,
                rows_duplicate=0,
                rows_skipped=0,
                rows_invalid=len(result.rows),
                error_type="persistence",
            )
            raise
        print(json.dumps(output, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}, sort_keys=True, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
