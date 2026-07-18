"""관세청 10일 수출 응답을 append-only v2 관측으로 저장한다."""

from __future__ import annotations

import base64
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Callable

from core.customs_export import (
    MAX_CUSTOMS_CONTENT_TYPE_BYTES,
    MAX_CUSTOMS_RAW_BYTES,
    MAX_CUSTOMS_SERVICE_KEY_BYTES,
    normalize_xml_content_type,
)
from core.customs_export_features import derive_industry_features
from core.customs_export_workdays import WorkdayFetchResult
from core.market_data_fetch import FetchResult, FetchStatus
from core.sensitive_text import contains_sensitive_text

SOURCE = "korea_customs"
DATASET = "ten_day_product_exports"
RAW_DATASET = "ten_day_product_exports_raw"
REQUEST_DATASET = "ten_day_product_export_requests"
FEATURE_DATASET = "ten_day_export_industry_features"
WORKDAY_DATASET = "ten_day_export_workdays"
_ALL_DATASETS = (
    REQUEST_DATASET,
    RAW_DATASET,
    DATASET,
    WORKDAY_DATASET,
    FEATURE_DATASET,
)
_COLLECTION_MODES = frozenset({"scheduled_live", "research_backfill", "manual_replay"})
_OBSERVABLE_WORKDAY_FEATURE_ERROR_CODES = frozenset(
    {
        "customs_workday_available_at_invalid",
        "customs_workday_count_invalid",
        "customs_workday_decision_at_invalid",
        "customs_workday_future_snapshot",
        "customs_workday_method_invalid",
        "customs_workday_period_duplicate",
        "customs_workday_period_invalid",
        "customs_workday_row_invalid",
        "customs_workday_snapshot_id_invalid",
        "customs_workday_source_not_kcs",
        "customs_workday_title_variant_invalid",
        "customs_workdays_invalid",
    }
)
_BASE64_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
)
_SCHEMA_VERSION = 1
_TRANSFORM_VERSION = 1
_KST = timezone(timedelta(hours=9))
_RAW_ARTIFACT_SOURCE_AS_OF = datetime(1970, 1, 1, tzinfo=timezone.utc)
_EXPECTED_PRODUCTS = frozenset(
    {
        "total",
        "semiconductors",
        "steel_products",
        "passenger_cars",
        "petroleum_products",
        "wireless_communication_devices",
        "ships",
        "auto_parts",
        "computer_peripherals",
        "precision_instruments",
        "home_appliances",
    }
)
_FEATURE_SYMBOLS = {
    "semiconductors": "KCS:SEMI",
    "steel_products": "KCS:STEEL",
    "passenger_cars": "KCS:CARS",
    "petroleum_products": "KCS:PETROLEUM",
    "wireless_communication_devices": "KCS:WIRELESS",
    "ships": "KCS:SHIPS",
    "auto_parts": "KCS:AUTO_PARTS",
    "computer_peripherals": "KCS:COMPUTERS",
    "precision_instruments": "KCS:PRECISION",
    "home_appliances": "KCS:APPLIANCES",
}
class _SensitiveRawError(ValueError):
    pass


class _PersistenceClockRegression(ValueError):
    pass


class CollectionJournalUnavailable(RuntimeError):
    pass


def _contains_sensitive_text(payload: bytes, service_key: str) -> bool:
    secret = service_key.strip() if type(service_key) is str else ""
    return contains_sensitive_text(
        payload,
        known_secrets=(secret,) if secret else (),
    )


@dataclass(frozen=True)
class _ObservationInput:
    source_record_id: str
    source_as_of: datetime
    ingested_at: datetime
    payload: dict[str, Any]


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name}_must_be_timezone_aware")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return _utc(value, "timestamp").strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _validate_query_range(start_yymm: str, end_yymm: str) -> None:
    def parts(value: str) -> tuple[int, int]:
        if type(value) is not str or re.fullmatch(r"[0-9]{6}", value) is None:
            raise ValueError("customs_query_period_invalid")
        year = int(value[:4])
        month = int(value[4:])
        if year < 2016 or not 1 <= month <= 12:
            raise ValueError("customs_query_period_invalid")
        return year, month

    if parts(start_yymm) > parts(end_yymm):
        raise ValueError("customs_query_period_invalid")


def _assert_persistence_clock(conn, received_at: datetime) -> None:
    candidate = _utc_text(received_at)
    placeholders = ",".join("?" for _ in _ALL_DATASETS)
    params = (SOURCE, *_ALL_DATASETS)
    observation_row = conn.execute(
        f"SELECT MAX(ingested_at) FROM observations "
        f"WHERE source = ? AND dataset IN ({placeholders})",
        params,
    ).fetchone()
    run_row = conn.execute(
        f"SELECT MAX(completed_at) FROM collection_runs "
        f"WHERE source = ? AND dataset IN ({placeholders})",
        params,
    ).fetchone()
    high_waters = [
        value
        for value in (
            observation_row[0] if observation_row else None,
            run_row[0] if run_row else None,
        )
        if value is not None
    ]
    if high_waters and candidate < max(high_waters):
        raise _PersistenceClockRegression("customs_persistence_clock_regression")


def _period_source_as_of(year: int, month: int, day: int) -> datetime:
    return datetime.combine(
        datetime(year, month, day).date(),
        time(23, 59, 59, 999999),
        tzinfo=_KST,
    ).astimezone(timezone.utc)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _publication_dates(year: int, month: int, day: int) -> tuple[date, date]:
    if day == 10:
        scheduled = date(year, month, 11)
        next_cutoff = date(year, month, 21)
    elif day == 20:
        next_year, next_month = _next_month(year, month)
        scheduled = date(year, month, 21)
        next_cutoff = date(next_year, next_month, 1)
    elif day == monthrange(year, month)[1]:
        next_year, next_month = _next_month(year, month)
        scheduled = date(next_year, next_month, 1)
        next_cutoff = date(next_year, next_month, 11)
    else:
        raise ValueError("customs_period_invalid")
    return scheduled, next_cutoff


def _validate_result_lineage(result: Any) -> FetchResult:
    if (
        not isinstance(result, FetchResult)
        or result.provider != "CUSTOMS"
        or result.endpoint != "/getPrlstMmUtPrviExpAcrs"
        or result.venue != "KR"
        or result.symbol != "KR_EXPORTS"
        or result.fallback_used is not False
    ):
        raise ValueError("customs_fetch_contract_invalid")
    return result


def _summary(*, status: str, rows_seen: int, rows_invalid: int, error_type: str):
    return {
        "source": SOURCE,
        "dataset": DATASET,
        "status": status,
        "rows_seen": rows_seen,
        "rows_inserted": 0,
        "rows_duplicate": 0,
        "rows_skipped": 0,
        "rows_invalid": rows_invalid,
        "error_type": error_type,
    }


def _record_failed_runs(
    *,
    store,
    run_id: str,
    started_at: datetime,
    completed_at: datetime,
    error_type: str,
    normalized_rows_seen: int = 0,
    raw_rows_seen: int = 0,
    request_rows_seen: int = 0,
) -> dict[str, Any]:
    counts = {
        REQUEST_DATASET: request_rows_seen,
        RAW_DATASET: raw_rows_seen,
        DATASET: normalized_rows_seen,
        WORKDAY_DATASET: 0,
        FEATURE_DATASET: 0,
    }

    def persist(selected_error_type: str) -> None:
        def write(conn):
            for dataset in _ALL_DATASETS:
                rows_seen = counts[dataset]
                store.record_collection_run(
                    source=SOURCE,
                    dataset=dataset,
                    run_id=run_id,
                    started_at=started_at,
                    completed_at=completed_at,
                    status="failed",
                    rows_seen=rows_seen,
                    rows_inserted=0,
                    rows_duplicate=0,
                    rows_skipped=0,
                    rows_invalid=rows_seen,
                    error_type=selected_error_type,
                    _conn=conn,
                )

        store.atomic_write(write)

    selected_error_type = error_type
    journal_unavailable = False
    try:
        persist(selected_error_type)
    except Exception as exc:
        selected_error_type = f"persistence.{type(exc).__name__.lower()}"
        try:
            persist(selected_error_type)
        except Exception:
            journal_unavailable = True
    if journal_unavailable:
        raise CollectionJournalUnavailable(
            "customs_collection_journal_unavailable"
        )
    return _summary(
        status="failed",
        rows_seen=normalized_rows_seen,
        rows_invalid=normalized_rows_seen,
        error_type=selected_error_type,
    )


def _record_zero_runs(
    *,
    store,
    run_id: str,
    started_at: datetime,
    completed_at: datetime,
    status: str,
    error_type: str,
) -> dict[str, Any]:
    def write(conn):
        for dataset in _ALL_DATASETS:
            store.record_collection_run(
                source=SOURCE,
                dataset=dataset,
                run_id=run_id,
                started_at=started_at,
                completed_at=completed_at,
                status=status,
                rows_seen=0,
                rows_inserted=0,
                rows_duplicate=0,
                rows_skipped=0,
                rows_invalid=0,
                error_type=error_type,
                _conn=conn,
            )

    try:
        store.atomic_write(write)
    except Exception as exc:
        return _record_failed_runs(
            store=store,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            error_type=f"persistence.{type(exc).__name__.lower()}",
        )
    return _summary(
        status=status,
        rows_seen=0,
        rows_invalid=0,
        error_type=error_type,
    )


def _validate_result_timestamps(result: FetchResult, received_at: datetime) -> None:
    started_at = _utc(result.started_at_utc, "started_at_utc")
    completed_at = _utc(result.completed_at_utc, "completed_at_utc")
    source_fetched_at = result.source_fetched_at_utc
    if (
        started_at > received_at
        or completed_at > received_at
        or (
            source_fetched_at is not None
            and _utc(source_fetched_at, "source_fetched_at_utc") > received_at
        )
    ):
        raise ValueError("timestamp.future")


def _workday_feature_error_type(exc: Exception) -> str:
    code = str(exc)
    if isinstance(exc, ValueError) and code in _OBSERVABLE_WORKDAY_FEATURE_ERROR_CODES:
        return code.removeprefix("customs_workday_")
    return "lineage.invalid"


def _validate_workday_fetch_result(
    result: Any,
    *,
    amount_received_at: datetime,
    batch_completed_at: datetime,
) -> WorkdayFetchResult:
    if not isinstance(result, WorkdayFetchResult):
        raise ValueError("customs_workday_fetch_contract_invalid")
    started = _utc(result.started_at_utc, "workday_started_at_utc")
    completed = _utc(result.completed_at_utc, "workday_completed_at_utc")
    if not amount_received_at <= started <= completed <= batch_completed_at:
        raise ValueError("customs_workday_fetch_timestamp_invalid")
    if result.status == "success":
        if result.error_type != "none" or not result.rows:
            raise ValueError("customs_workday_fetch_contract_invalid")
    elif result.status == "failed":
        if (
            result.rows
            or type(result.error_type) is not str
            or re.fullmatch(r"[a-z0-9_.]+", result.error_type) is None
            or result.error_type == "none"
        ):
            raise ValueError("customs_workday_fetch_contract_invalid")
    else:
        raise ValueError("customs_workday_fetch_contract_invalid")
    completed_text = _utc_text(completed)
    for row in result.rows:
        if (
            type(row) is not dict
            or row.get("available_at_utc") != completed_text
            or row.get("first_seen_at_utc") != completed_text
            or row.get("source_published_at_utc") is not None
            or row.get("publication_precision") != "date_only"
            or row.get("source_agency") != "관세청"
            or row.get("detail_header_title") != row.get("source_title")
            or row.get("detail_header_release_date_kst")
            != row.get("scheduled_release_date_kst")
            or row.get("detail_header_verified") is not True
        ):
            raise ValueError("customs_workday_fetch_contract_invalid")
    return result


def _workday_storage_payload(conn, row: dict[str, Any]) -> dict[str, Any]:
    candidate = {
        key: value
        for key, value in row.items()
        if key
        not in {
            "first_seen_at_utc",
            "available_at_utc",
            "supersedes_snapshot_id",
            "revision_seq",
        }
    }
    candidate["available_at_field"] = "observation.ingested_at"
    prior = conn.execute(
        """
        SELECT snapshot_id, payload_json
        FROM observations
        WHERE source = ? AND dataset = ? AND source_record_id = ?
          AND symbol = ? AND market = ?
        ORDER BY source_as_of DESC, source_event_sequence DESC,
                 ingested_at DESC, id DESC
        LIMIT 1
        """,
        (
            SOURCE,
            WORKDAY_DATASET,
            row["source_record_id"],
            "KR_EXPORTS",
            "KR",
        ),
    ).fetchone()
    if prior is None:
        candidate["revision_seq"] = 0
        candidate["supersedes_snapshot_id"] = None
        return candidate
    prior_snapshot_id = str(prior["snapshot_id"])
    prior_payload = json.loads(str(prior["payload_json"]))
    prior_core = {
        key: value
        for key, value in prior_payload.items()
        if key not in {"revision_seq", "supersedes_snapshot_id"}
    }
    if candidate == prior_core:
        candidate["revision_seq"] = int(prior_payload.get("revision_seq", 0))
        candidate["supersedes_snapshot_id"] = prior_payload.get(
            "supersedes_snapshot_id"
        )
    else:
        candidate["revision_seq"] = int(prior_payload.get("revision_seq", 0)) + 1
        candidate["supersedes_snapshot_id"] = prior_snapshot_id
    return candidate


def _decode_raw_artifact(encoded: str) -> bytes:
    return base64.b64decode(encoded, validate=True)


def _raw_artifact_payload(
    result: FetchResult,
    *,
    start_yymm: str,
    end_yymm: str,
    service_key: str,
) -> dict[str, Any]:
    if type(result.value) is not dict:
        raise ValueError("customs_raw_artifact_invalid")
    encoded = result.value.get("raw_xml_base64")
    expected_hash = result.value.get("raw_xml_sha256")
    expected_size = result.value.get("raw_size_bytes")
    request_params = result.value.get("request_params")
    http_status = result.value.get("http_status")
    content_type = result.value.get("content_type")
    parser_version = result.value.get("parser_contract_version")
    max_encoded_size = ((MAX_CUSTOMS_RAW_BYTES + 2) // 3) * 4
    if (
        type(encoded) is not str
        or type(expected_hash) is not str
        or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
        or type(expected_size) is not int
        or type(expected_size) is bool
        or not 0 < expected_size <= MAX_CUSTOMS_RAW_BYTES
        or len(encoded) > max_encoded_size
    ):
        raise ValueError("customs_raw_artifact_invalid")
    padding = len(encoded) - len(encoded.rstrip("="))
    unpadded = encoded[:-padding] if padding else encoded
    if (
        len(encoded) % 4 != 0
        or padding > 2
        or any(character not in _BASE64_ALPHABET for character in unpadded)
        or (padding == 0 and len(unpadded) % 4 != 0)
        or (padding == 1 and len(unpadded) % 4 != 3)
        or (padding == 2 and len(unpadded) % 4 != 2)
    ):
        raise ValueError("customs_raw_artifact_invalid")
    decoded_size = len(encoded) // 4 * 3 - padding
    if decoded_size != expected_size:
        raise ValueError("customs_raw_artifact_invalid")
    if type(content_type) is not str:
        raise ValueError("customs_raw_artifact_invalid")
    encoded_content_type = content_type.encode("utf-8")
    if len(encoded_content_type) > MAX_CUSTOMS_CONTENT_TYPE_BYTES:
        raise ValueError("customs_raw_artifact_invalid")
    if _contains_sensitive_text(encoded_content_type, service_key):
        raise _SensitiveRawError("customs_raw_artifact_sensitive")
    canonical_content_type = normalize_xml_content_type(content_type)
    if canonical_content_type is None:
        raise ValueError("customs_raw_artifact_invalid")
    try:
        raw = _decode_raw_artifact(encoded)
    except (ValueError, TypeError) as exc:
        raise ValueError("customs_raw_artifact_invalid") from exc
    if len(raw) > MAX_CUSTOMS_RAW_BYTES:
        raise ValueError("customs_raw_artifact_invalid")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("customs_raw_artifact_invalid") from exc
    if _contains_sensitive_text(raw, service_key):
        raise _SensitiveRawError("customs_raw_artifact_sensitive")
    if (
        hashlib.sha256(raw).hexdigest() != expected_hash
        or expected_size != len(raw)
        or type(request_params) is not dict
        or request_params != {"strtYymm": start_yymm, "endYymm": end_yymm}
        or http_status != 200
        or parser_version != 1
    ):
        raise ValueError("customs_raw_artifact_invalid")
    return {
        "raw_xml_base64": encoded,
        "raw_xml_sha256": expected_hash,
        "raw_size_bytes": expected_size,
        "request_params": {"strtYymm": start_yymm, "endYymm": end_yymm},
        "http_status": http_status,
        "content_type": canonical_content_type,
        "parser_contract_version": parser_version,
        "official_dataset_id": "15157908",
    }


def _raw_content_payload(raw_artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "raw_xml_base64": raw_artifact["raw_xml_base64"],
        "raw_xml_sha256": raw_artifact["raw_xml_sha256"],
        "raw_size_bytes": raw_artifact["raw_size_bytes"],
        "snapshot_group_id": raw_artifact["raw_xml_sha256"],
        "artifact_identity": "content_sha256",
        "official_dataset_id": raw_artifact["official_dataset_id"],
    }


def _request_lineage_payload(
    raw_artifact: dict[str, Any], *, collection_mode: str
) -> dict[str, Any]:
    return {
        "request_params": dict(raw_artifact["request_params"]),
        "collection_mode": collection_mode,
        "raw_artifact_sha256": raw_artifact["raw_xml_sha256"],
        "http_status": raw_artifact["http_status"],
        "content_type": raw_artifact["content_type"],
        "parser_contract_version": raw_artifact["parser_contract_version"],
        "official_dataset_id": raw_artifact["official_dataset_id"],
        "shadow_only": True,
    }


def _normalize_items(
    result: FetchResult,
    *,
    snapshot_group_id: str,
    ingested_at: datetime,
    start_yymm: str,
    end_yymm: str,
    collection_mode: str,
) -> list[_ObservationInput]:
    _validate_result_lineage(result)
    if result.status is not FetchStatus.SUCCESS or type(result.value) is not dict:
        raise ValueError("customs_fetch_contract_invalid")
    items = result.value.get("items")
    total_count = result.value.get("total_count")
    if type(items) is not list or type(total_count) is not int or total_count != len(items):
        raise ValueError("customs_fetch_contract_invalid")
    current_kst = ingested_at.astimezone(_KST)
    normalized: list[_ObservationInput] = []
    seen_periods: set[tuple[int, int, int]] = set()
    for row in items:
        if type(row) is not dict:
            raise ValueError("customs_row_invalid")
        year = row.get("period_year")
        month = row.get("period_month")
        day = row.get("period_end_day")
        kind = row.get("period_kind")
        if (
            type(year) is not int
            or type(month) is not int
            or type(day) is not int
            or type(kind) is not str
            or not 1 <= month <= 12
        ):
            raise ValueError("customs_period_invalid")
        final_day = monthrange(year, month)[1]
        expected_kind = {10: "day_10", 20: "day_20", final_day: "month_end"}.get(day)
        row_yymm = f"{year:04d}{month:02d}"
        if (
            expected_kind is None
            or kind != expected_kind
            or not start_yymm <= row_yymm <= end_yymm
        ):
            raise ValueError("customs_period_invalid")
        period = (year, month, day)
        if period in seen_periods:
            raise ValueError("customs_period_duplicate")
        seen_periods.add(period)
        amounts = row.get("amounts_thousand_usd")
        if type(amounts) is not dict or frozenset(amounts) != _EXPECTED_PRODUCTS:
            raise ValueError("customs_amounts_invalid")
        if any(type(value) is not int or value < 0 for value in amounts.values()):
            raise ValueError("customs_amounts_invalid")
        total = amounts["total"]
        if any(value > total for key, value in amounts.items() if key != "total"):
            raise ValueError("customs_amounts_invalid")
        source_as_of = _period_source_as_of(year, month, day)
        if source_as_of > ingested_at:
            raise ValueError("customs_period_not_available")
        scheduled_release_date, next_cutoff_release_date = _publication_dates(
            year, month, day
        )
        normalized.append(
            _ObservationInput(
                source_record_id=f"{year:04d}{month:02d}{day:02d}",
                source_as_of=source_as_of,
                ingested_at=ingested_at,
                payload={
                    "period_year": year,
                    "period_month": month,
                    "period_end_day": day,
                    "period_kind": kind,
                    "amounts_thousand_usd": dict(amounts),
                    "units": {"amounts_thousand_usd": "thousand_USD"},
                    "provisional": (year, month) == (current_kst.year, current_kst.month),
                    "snapshot_group_id": snapshot_group_id,
                    "collection_mode": collection_mode,
                    "scheduled_release_date_kst": scheduled_release_date.isoformat(),
                    "next_cutoff_release_date_kst": next_cutoff_release_date.isoformat(),
                    "source_published_at_utc": None,
                    "publication_precision": "date_only",
                    "available_at_field": "observation.ingested_at",
                    "vintage_policy": "research_backfill_current_vintage",
                    "official_dataset_id": "15157908",
                },
            )
        )
    latest_source_as_of = max(item.source_as_of for item in normalized)
    current_local = ingested_at.astimezone(_KST)
    current_yymm = current_local.strftime("%Y%m")
    current_date = current_local.date()
    for item in normalized:
        if item.source_as_of == latest_source_as_of:
            scheduled_release_date = date.fromisoformat(
                item.payload["scheduled_release_date_kst"]
            )
            next_cutoff_release_date = date.fromisoformat(
                item.payload["next_cutoff_release_date_kst"]
            )
            if (
                collection_mode == "scheduled_live"
                and end_yymm == current_yymm
                and scheduled_release_date <= current_date < next_cutoff_release_date
            ):
                item.payload["vintage_policy"] = "realtime_as_observed"
        else:
            item.payload["vintage_policy"] = (
                "reference_latest_revised_as_observed"
            )
        item.payload["historical_backtest_eligible"] = False
        item.payload["eligible_for_production_score"] = False
        item.payload["shadow_only"] = True
    return normalized


def collect_customs_export_observations(
    start_yymm: str,
    end_yymm: str,
    *,
    store,
    run_id: str,
    service_key: str = "",
    fetcher: Callable[..., FetchResult] | None = None,
    workday_fetcher: Callable[[list[dict[str, Any]]], WorkdayFetchResult] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    collection_mode: str = "research_backfill",
) -> dict[str, Any]:
    """한 API 응답의 모든 기간을 성공 run과 같은 트랜잭션에 저장한다."""
    _validate_query_range(start_yymm, end_yymm)
    if collection_mode not in _COLLECTION_MODES:
        raise ValueError("customs_collection_mode_invalid")
    if (
        type(service_key) is not str
        or len(service_key.strip().encode("utf-8")) > MAX_CUSTOMS_SERVICE_KEY_BYTES
    ):
        raise ValueError("customs_service_key_invalid")
    if type(run_id) is not str or _contains_sensitive_text(
        run_id.encode("utf-8"), service_key
    ):
        raise ValueError("customs_run_id_sensitive")
    invocation_started = _utc(clock(), "clock")
    if fetcher is None:
        from core.customs_export import fetch_customs_export_result

        fetcher = fetch_customs_export_result
    try:
        result = fetcher(start_yymm, end_yymm, service_key=service_key)
    except Exception as exc:
        try:
            failure_completed = _utc(clock(), "clock")
        except (TypeError, ValueError):
            failure_completed = invocation_started
            error_type = "timestamp.invalid"
        else:
            if failure_completed < invocation_started:
                failure_completed = invocation_started
                error_type = "timestamp.clock_regression"
            else:
                error_type = f"fetch_exception.{type(exc).__name__.lower()}"
        return _record_failed_runs(
            store=store,
            run_id=run_id,
            started_at=invocation_started,
            completed_at=failure_completed,
            error_type=error_type,
        )
    try:
        received_at = _utc(clock(), "clock")
    except (TypeError, ValueError):
        return _record_failed_runs(
            store=store,
            run_id=run_id,
            started_at=invocation_started,
            completed_at=invocation_started,
            error_type="timestamp.invalid",
        )
    if received_at < invocation_started:
        return _record_failed_runs(
            store=store,
            run_id=run_id,
            started_at=invocation_started,
            completed_at=invocation_started,
            error_type="timestamp.clock_regression",
        )
    try:
        result = _validate_result_lineage(result)
    except (TypeError, ValueError):
        return _record_failed_runs(
            store=store,
            run_id=run_id,
            started_at=invocation_started,
            completed_at=received_at,
            error_type="lineage.invalid",
        )
    try:
        _validate_result_timestamps(result, received_at)
    except (TypeError, ValueError):
        return _record_failed_runs(
            store=store,
            run_id=run_id,
            started_at=invocation_started,
            completed_at=received_at,
            error_type="timestamp.future",
        )
    started_at = invocation_started
    completed_at = received_at
    if result.status is FetchStatus.EMPTY:
        try:
            raw_artifact = _raw_artifact_payload(
                result,
                start_yymm=start_yymm,
                end_yymm=end_yymm,
                service_key=service_key,
            )
            if (
                type(result.value) is not dict
                or result.value.get("items") != []
                or result.value.get("total_count") != 0
            ):
                raise ValueError("customs_empty_contract_invalid")
            raw_source_as_of = _RAW_ARTIFACT_SOURCE_AS_OF
        except _SensitiveRawError:
            return _record_failed_runs(
                store=store,
                run_id=run_id,
                started_at=started_at,
                completed_at=completed_at,
                error_type="sensitive_raw",
                raw_rows_seen=1,
            )
        except (TypeError, ValueError):
            return _record_failed_runs(
                store=store,
                run_id=run_id,
                started_at=started_at,
                completed_at=completed_at,
                error_type="malformed",
                raw_rows_seen=1,
            )

        def write_empty(conn):
            _assert_persistence_clock(conn, completed_at)
            request_append = store.append(
                source=SOURCE,
                dataset=REQUEST_DATASET,
                source_record_id=run_id,
                symbol="KR_EXPORT_REQUEST",
                market="KR",
                currency_or_unit="UNITLESS",
                source_as_of=completed_at,
                source_event_sequence=0,
                ingested_at=completed_at,
                schema_version=_SCHEMA_VERSION,
                transform_version=_TRANSFORM_VERSION,
                fallback_used=False,
                payload=_request_lineage_payload(
                    raw_artifact, collection_mode=collection_mode
                ),
                _conn=conn,
            )
            raw_append = store.append(
                source=SOURCE,
                dataset=RAW_DATASET,
                source_record_id=raw_artifact["raw_xml_sha256"],
                symbol="KR_EXPORTS_RAW",
                market="KR",
                currency_or_unit="UNITLESS",
                source_as_of=raw_source_as_of,
                source_event_sequence=0,
                ingested_at=completed_at,
                schema_version=_SCHEMA_VERSION,
                transform_version=_TRANSFORM_VERSION,
                fallback_used=False,
                payload=_raw_content_payload(raw_artifact),
                _conn=conn,
            )
            store.record_collection_run(
                source=SOURCE,
                dataset=REQUEST_DATASET,
                run_id=run_id,
                started_at=started_at,
                completed_at=completed_at,
                status="success",
                rows_seen=1,
                rows_inserted=int(request_append.inserted),
                rows_duplicate=int(not request_append.inserted),
                rows_skipped=0,
                rows_invalid=0,
                error_type="",
                _conn=conn,
            )
            store.record_collection_run(
                source=SOURCE,
                dataset=RAW_DATASET,
                run_id=run_id,
                started_at=started_at,
                completed_at=completed_at,
                status="success",
                rows_seen=1,
                rows_inserted=int(raw_append.inserted),
                rows_duplicate=int(not raw_append.inserted),
                rows_skipped=0,
                rows_invalid=0,
                error_type="",
                _conn=conn,
            )
            for dataset in (DATASET, WORKDAY_DATASET, FEATURE_DATASET):
                store.record_collection_run(
                    source=SOURCE,
                    dataset=dataset,
                    run_id=run_id,
                    started_at=started_at,
                    completed_at=completed_at,
                    status="skipped",
                    rows_seen=0,
                    rows_inserted=0,
                    rows_duplicate=0,
                    rows_skipped=0,
                    rows_invalid=0,
                    error_type="",
                    _conn=conn,
                )
            return {
                "source": SOURCE,
                "dataset": DATASET,
                "status": "skipped",
                "rows_seen": 0,
                "rows_inserted": 0,
                "rows_duplicate": 0,
                "rows_skipped": 0,
                "rows_invalid": 0,
                "error_type": "",
            }

        try:
            return store.atomic_write(write_empty)
        except _PersistenceClockRegression:
            return _record_failed_runs(
                store=store,
                run_id=run_id,
                started_at=started_at,
                completed_at=completed_at,
                error_type="timestamp.clock_regression",
                raw_rows_seen=1,
                request_rows_seen=1,
            )
        except Exception as exc:
            return _record_failed_runs(
                store=store,
                run_id=run_id,
                started_at=started_at,
                completed_at=completed_at,
                error_type=f"persistence.{type(exc).__name__.lower()}",
                raw_rows_seen=1,
                request_rows_seen=1,
            )
    if result.status is not FetchStatus.SUCCESS:
        status = (
            "failed"
            if result.status in {FetchStatus.FAILED, FetchStatus.INCOMPLETE}
            else "skipped"
        )
        error_type = "" if result.error_type.value == "none" else result.error_type.value
        if status == "failed":
            return _record_failed_runs(
                store=store,
                run_id=run_id,
                started_at=started_at,
                completed_at=completed_at,
                error_type=error_type,
            )
        return _record_zero_runs(
            store=store,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            status=status,
            error_type=error_type,
        )
    try:
        raw_artifact = _raw_artifact_payload(
            result,
            start_yymm=start_yymm,
            end_yymm=end_yymm,
            service_key=service_key,
        )
        observations = _normalize_items(
            result,
            snapshot_group_id=raw_artifact["raw_xml_sha256"],
            ingested_at=received_at,
            start_yymm=start_yymm,
            end_yymm=end_yymm,
            collection_mode=collection_mode,
        )
        raw_source_as_of = _RAW_ARTIFACT_SOURCE_AS_OF
    except _SensitiveRawError:
        raw_items = result.value.get("items") if type(result.value) is dict else None
        rows_seen = len(raw_items) if type(raw_items) is list else 0
        return _record_failed_runs(
            store=store,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            error_type="sensitive_raw",
            normalized_rows_seen=rows_seen,
            raw_rows_seen=1,
        )
    except (TypeError, ValueError):
        raw_items = result.value.get("items") if type(result.value) is dict else None
        rows_seen = len(raw_items) if type(raw_items) is list else 0
        return _record_failed_runs(
            store=store,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            error_type="malformed",
            normalized_rows_seen=rows_seen,
            raw_rows_seen=1,
        )

    workday_status = "skipped"
    workday_error_type = ""
    workday_rows: list[dict[str, Any]] = []
    if workday_fetcher is not None:
        fetched_workdays: Any = None
        try:
            fetched_workdays = workday_fetcher(
                [dict(item.payload) for item in observations]
            )
        except Exception as exc:
            workday_status = "failed"
            workday_error_type = f"fetch_exception.{type(exc).__name__.lower()}"
        try:
            batch_completed_at = _utc(clock(), "clock")
        except (TypeError, ValueError):
            return _record_failed_runs(
                store=store,
                run_id=run_id,
                started_at=started_at,
                completed_at=completed_at,
                error_type="timestamp.invalid",
                normalized_rows_seen=len(observations),
                raw_rows_seen=1,
                request_rows_seen=1,
            )
        if batch_completed_at < completed_at:
            return _record_failed_runs(
                store=store,
                run_id=run_id,
                started_at=started_at,
                completed_at=completed_at,
                error_type="timestamp.clock_regression",
                normalized_rows_seen=len(observations),
                raw_rows_seen=1,
                request_rows_seen=1,
            )
        completed_at = batch_completed_at
        if fetched_workdays is not None:
            try:
                validated_workdays = _validate_workday_fetch_result(
                    fetched_workdays,
                    amount_received_at=received_at,
                    batch_completed_at=completed_at,
                )
            except (TypeError, ValueError):
                workday_status = "failed"
                workday_error_type = "lineage.invalid"
            else:
                workday_status = validated_workdays.status
                workday_error_type = (
                    ""
                    if validated_workdays.error_type == "none"
                    else validated_workdays.error_type
                )
                workday_rows = [dict(row) for row in validated_workdays.rows]
        if workday_status == "success":
            validation_periods = [
                {**item.payload, "snapshot_id": f"amount-{index}"}
                for index, item in enumerate(observations)
            ]
            validation_workdays = [
                {
                    **row,
                    "available_at_field": "observation.ingested_at",
                    "revision_seq": 0,
                    "supersedes_snapshot_id": None,
                    "snapshot_id": hashlib.sha256(
                        (
                            row["source_record_id"]
                            + ":"
                            + row["source_document_sha256"]
                        ).encode("utf-8")
                    ).hexdigest(),
                }
                for row in workday_rows
            ]
            try:
                derive_industry_features(
                    validation_periods,
                    workdays=validation_workdays,
                    decision_at_utc=completed_at,
                )
            except (TypeError, ValueError, KeyError) as exc:
                workday_status = "failed"
                workday_error_type = _workday_feature_error_type(exc)
                workday_rows = []

    def write(conn):
        _assert_persistence_clock(conn, completed_at)
        request_append = store.append(
            source=SOURCE,
            dataset=REQUEST_DATASET,
            source_record_id=run_id,
            symbol="KR_EXPORT_REQUEST",
            market="KR",
            currency_or_unit="UNITLESS",
            source_as_of=received_at,
            source_event_sequence=0,
            ingested_at=received_at,
            schema_version=_SCHEMA_VERSION,
            transform_version=_TRANSFORM_VERSION,
            fallback_used=False,
            payload=_request_lineage_payload(
                raw_artifact, collection_mode=collection_mode
            ),
            _conn=conn,
        )
        raw_append = store.append(
            source=SOURCE,
            dataset=RAW_DATASET,
            source_record_id=raw_artifact["raw_xml_sha256"],
            symbol="KR_EXPORTS_RAW",
            market="KR",
            currency_or_unit="UNITLESS",
            source_as_of=raw_source_as_of,
            source_event_sequence=0,
            ingested_at=received_at,
            schema_version=_SCHEMA_VERSION,
            transform_version=_TRANSFORM_VERSION,
            fallback_used=False,
            payload=_raw_content_payload(raw_artifact),
            _conn=conn,
        )
        store.record_collection_run(
            source=SOURCE,
            dataset=REQUEST_DATASET,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            status="success",
            rows_seen=1,
            rows_inserted=int(request_append.inserted),
            rows_duplicate=int(not request_append.inserted),
            rows_skipped=0,
            rows_invalid=0,
            error_type="",
            _conn=conn,
        )
        store.record_collection_run(
            source=SOURCE,
            dataset=RAW_DATASET,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            status="success",
            rows_seen=1,
            rows_inserted=int(raw_append.inserted),
            rows_duplicate=int(not raw_append.inserted),
            rows_skipped=0,
            rows_invalid=0,
            error_type="",
            _conn=conn,
        )
        inserted = 0
        duplicate = 0
        feature_inputs: list[dict[str, Any]] = []
        for item in observations:
            append_result = store.append(
                source=SOURCE,
                dataset=DATASET,
                source_record_id=item.source_record_id,
                symbol="KR_EXPORTS",
                market="KR",
                currency_or_unit="USD",
                source_as_of=item.source_as_of,
                source_event_sequence=0,
                ingested_at=item.ingested_at,
                schema_version=_SCHEMA_VERSION,
                transform_version=_TRANSFORM_VERSION,
                fallback_used=False,
                payload=item.payload,
                _conn=conn,
            )
            feature_inputs.append({**item.payload, "snapshot_id": append_result.snapshot_id})
            if append_result.inserted:
                inserted += 1
            else:
                duplicate += 1

        workday_inserted = 0
        workday_duplicate = 0
        workday_feature_inputs: list[dict[str, Any]] = []
        for row in workday_rows:
            available_at = datetime.fromisoformat(
                row["available_at_utc"][:-1] + "+00:00"
            )
            stored_workday_payload = _workday_storage_payload(conn, row)
            workday_result = store.append(
                source=SOURCE,
                dataset=WORKDAY_DATASET,
                source_record_id=row["source_record_id"],
                symbol="KR_EXPORTS",
                market="KR",
                currency_or_unit="UNITLESS",
                source_as_of=_period_source_as_of(
                    row["period_year"],
                    row["period_month"],
                    row["period_end_day"],
                ),
                source_event_sequence=0,
                ingested_at=available_at,
                schema_version=_SCHEMA_VERSION,
                transform_version=_TRANSFORM_VERSION,
                fallback_used=False,
                payload=stored_workday_payload,
                _conn=conn,
            )
            workday_feature_inputs.append(
                {
                    **stored_workday_payload,
                    "available_at_utc": row["available_at_utc"],
                    "snapshot_id": workday_result.snapshot_id,
                }
            )
            workday_inserted += int(workday_result.inserted)
            workday_duplicate += int(not workday_result.inserted)

        all_features = derive_industry_features(
            feature_inputs,
            workdays=workday_feature_inputs,
            decision_at_utc=completed_at,
        )
        latest_period = max(
            (item.source_as_of, item.source_record_id) for item in observations
        )[1]
        feature_rows = [
            feature
            for feature in all_features
            if f"{feature['period_year']:04d}{feature['period_month']:02d}"
            f"{feature['period_end_day']:02d}"
            == latest_period
        ]
        latest_vintage_policy = next(
            item.payload["vintage_policy"]
            for item in observations
            if item.source_record_id == latest_period
        )
        feature_inserted = 0
        feature_duplicate = 0
        feature_ready = 0
        for feature in feature_rows:
            industry = feature["industry"]
            feature_payload = {
                **feature,
                "vintage_policy": latest_vintage_policy,
                "collection_mode": collection_mode,
                "historical_backtest_eligible": False,
                "eligible_for_production_score": False,
                "shadow_only": True,
            }
            feature_result = store.append(
                source=SOURCE,
                dataset=FEATURE_DATASET,
                source_record_id=f"{latest_period}:{industry}",
                symbol=_FEATURE_SYMBOLS[industry],
                market="KR",
                currency_or_unit="MIXED",
                source_as_of=_period_source_as_of(
                    feature["period_year"],
                    feature["period_month"],
                    feature["period_end_day"],
                ),
                source_event_sequence=0,
                ingested_at=completed_at,
                schema_version=_SCHEMA_VERSION,
                transform_version=_TRANSFORM_VERSION,
                fallback_used=False,
                payload=feature_payload,
                _conn=conn,
            )
            feature_inserted += int(feature_result.inserted)
            feature_duplicate += int(not feature_result.inserted)
            feature_ready += int(feature["feature_ready"])
        store.record_collection_run(
            source=SOURCE,
            dataset=DATASET,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            status="success",
            rows_seen=len(observations),
            rows_inserted=inserted,
            rows_duplicate=duplicate,
            rows_skipped=0,
            rows_invalid=0,
            error_type="",
            _conn=conn,
        )
        store.record_collection_run(
            source=SOURCE,
            dataset=WORKDAY_DATASET,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            status=workday_status,
            rows_seen=len(workday_rows),
            rows_inserted=workday_inserted,
            rows_duplicate=workday_duplicate,
            rows_skipped=0,
            rows_invalid=0,
            error_type=workday_error_type,
            _conn=conn,
        )
        store.record_collection_run(
            source=SOURCE,
            dataset=FEATURE_DATASET,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            status="success",
            rows_seen=len(feature_rows),
            rows_inserted=feature_inserted,
            rows_duplicate=feature_duplicate,
            rows_skipped=0,
            rows_invalid=0,
            error_type="",
            _conn=conn,
        )
        overall_status = "partial" if workday_status == "failed" else "success"
        overall_error_type = (
            f"workday.{workday_error_type}" if workday_status == "failed" else ""
        )
        return {
            "source": SOURCE,
            "dataset": DATASET,
            "status": overall_status,
            "rows_seen": len(observations),
            "rows_inserted": inserted,
            "rows_duplicate": duplicate,
            "rows_skipped": 0,
            "rows_invalid": 0,
            "error_type": overall_error_type,
            "workday_status": workday_status,
            "workday_rows_seen": len(workday_rows),
            "workday_rows_inserted": workday_inserted,
            "workday_rows_duplicate": workday_duplicate,
            "workday_error_type": workday_error_type,
            "feature_rows_seen": len(feature_rows),
            "feature_rows_inserted": feature_inserted,
            "feature_rows_duplicate": feature_duplicate,
            "feature_rows_ready": feature_ready,
        }

    try:
        return store.atomic_write(write)
    except _PersistenceClockRegression:
        return _record_failed_runs(
            store=store,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            error_type="timestamp.clock_regression",
            normalized_rows_seen=len(observations),
            raw_rows_seen=1,
            request_rows_seen=1,
        )
    except Exception as exc:
        error_type = f"persistence.{type(exc).__name__.lower()}"
        rows_seen = len(observations)
        return _record_failed_runs(
            store=store,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            error_type=error_type,
            normalized_rows_seen=rows_seen,
            raw_rows_seen=1,
            request_rows_seen=1,
        )
