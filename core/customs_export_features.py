"""관세청 누적 수출을 누수 없는 shadow 산업 피처로 변환한다."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timezone
import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

INDUSTRIES = (
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
)

_WORKDAY_DOMAIN = "KCS_REPORTED_OPERATING_DAYS"
_WORKDAY_METHOD_VERSION = "korea-kr-kcs-press-release-v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_DOCUMENT_ID_RE = re.compile(r"[0-9]{9}")

# 후보일 뿐이다. 사업보고서/IR 근거로 version을 동결하기 전에는 가중치가 0이다.
_UNVERIFIED_SYMBOL_CANDIDATES = {
    "semiconductors": ("005930.KS", "000660.KS", "000990.KS"),
    "steel_products": ("005490.KS", "004020.KS"),
    "passenger_cars": ("005380.KS", "000270.KS"),
    "petroleum_products": ("010950.KS", "096770.KS", "078930.KS"),
    "wireless_communication_devices": ("005930.KS",),
    "ships": ("009540.KS", "010140.KS", "042660.KS"),
    "auto_parts": ("012330.KS", "204320.KS", "011210.KS"),
    "computer_peripherals": ("005930.KS", "000660.KS"),
    "precision_instruments": (),
    "home_appliances": ("066570.KS", "005930.KS"),
}


def _pct_change(current: float, reference: float) -> float | None:
    if reference <= 0:
        return None
    return (current / reference - 1.0) * 100.0


def _parse_utc(value: Any) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError("customs_workday_available_at_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("customs_workday_available_at_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("customs_workday_available_at_invalid")
    return parsed


def _workday_title_year(title: Any, *, month: int, day: int) -> int:
    if type(title) is not str or len(title.encode("utf-8")) > 512:
        raise ValueError("customs_workday_lineage_invalid")
    normalized_title = (
        title.removesuffix(" [잠정치]") if title.endswith(" [잠정치]") else title
    )
    year_token = r"(?:(?P<long>20[0-9]{2})|[’']?(?P<short>[0-9]{2}))"
    if day in {10, 20}:
        suffix = (
            rf"\s*년\s*{month}\s*월\s*1\s*일\s*[~∼-]\s*"
            rf"(?:{month}\s*월\s*)?{day}\s*일\s*수출입\s*현황"
        )
    else:
        suffix = rf"\s*년\s*{month}\s*월\s*수출입\s*현황"
    match = re.fullmatch(year_token + suffix, normalized_title)
    if match is None:
        if "[잠정치]" in title:
            raise ValueError("customs_workday_title_variant_invalid")
        raise ValueError("customs_workday_lineage_invalid")
    year = int(match.group("long") or match.group("short"))
    if match.group("long") is None:
        year += 2000
    return year


def _release_date_matches_period(
    released: date,
    *,
    period_year: int,
    month: int,
    day: int,
) -> bool:
    if day == 10:
        return (
            released.year == period_year
            and released.month == month
            and 11 <= released.day <= 13
        )
    if day == 20:
        return (
            released.year == period_year
            and released.month == month
            and 21 <= released.day <= 23
        )
    next_month = date(period_year + int(month == 12), 1 if month == 12 else month + 1, 1)
    return next_month <= released <= next_month.fromordinal(next_month.toordinal() + 2)


def _validate_workday_lineage(row: dict[str, Any], *, snapshot_id: str) -> None:
    year = row["period_year"]
    month = row["period_month"]
    day = row["period_end_day"]
    document_id = row.get("source_document_id")
    source_uri = row.get("source_uri")
    source_title = row.get("source_title")
    source_agency = row.get("source_agency")
    detail_title = row.get("detail_header_title")
    detail_release_date = row.get("detail_header_release_date_kst")
    source_hash = row.get("source_document_sha256")
    source_record_id = row.get("source_record_id")
    if (
        type(document_id) is not str
        or _DOCUMENT_ID_RE.fullmatch(document_id) is None
        or type(source_uri) is not str
        or type(source_title) is not str
        or not source_title.strip()
        or len(source_title.encode("utf-8")) > 512
        or source_agency != "관세청"
        or detail_title != source_title
        or type(detail_release_date) is not str
        or row.get("detail_header_verified") is not True
        or type(source_hash) is not str
        or _SHA256_RE.fullmatch(source_hash) is None
        or source_record_id != f"{year:04d}{month:02d}{day:02d}:workdays"
        or row.get("source_published_at_utc") is not None
        or row.get("publication_precision") != "date_only"
        or row.get("available_at_field") != "observation.ingested_at"
        or row.get("revision_policy") != "append_only_content_hash"
        or row.get("shadow_only") is not True
        or row.get("eligible_for_production_score") is not False
    ):
        raise ValueError("customs_workday_lineage_invalid")
    try:
        parsed_uri = urlsplit(source_uri)
        query = parse_qs(parsed_uri.query, strict_parsing=True)
        hostname = parsed_uri.hostname
        port = parsed_uri.port
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ValueError("customs_workday_lineage_invalid") from exc
    if (
        parsed_uri.scheme != "https"
        or hostname != "m.korea.kr"
        or parsed_uri.username is not None
        or parsed_uri.password is not None
        or port not in (None, 443)
        or parsed_uri.path != "/briefing/pressReleaseView.do"
        or parsed_uri.fragment
        or query != {"newsId": [document_id]}
    ):
        raise ValueError("customs_workday_lineage_invalid")
    scheduled_text = row.get("scheduled_release_date_kst")
    if type(scheduled_text) is not str:
        raise ValueError("customs_workday_lineage_invalid")
    try:
        scheduled = date.fromisoformat(scheduled_text)
        detail_scheduled = date.fromisoformat(detail_release_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("customs_workday_lineage_invalid") from exc
    title_year = _workday_title_year(source_title, month=month, day=day)
    if (
        detail_scheduled != scheduled
        or year not in {title_year - 1, title_year}
        or not _release_date_matches_period(
            scheduled,
            period_year=title_year,
            month=month,
            day=day,
        )
    ):
        raise ValueError("customs_workday_lineage_invalid")
    revision_seq = row.get("revision_seq")
    supersedes = row.get("supersedes_snapshot_id")
    if type(revision_seq) is not int or revision_seq < 0:
        raise ValueError("customs_workday_lineage_invalid")
    if (revision_seq == 0 and supersedes is not None) or (
        revision_seq > 0
        and (
            type(supersedes) is not str
            or not supersedes
            or len(supersedes) > 128
            or supersedes == snapshot_id
        )
    ):
        raise ValueError("customs_workday_lineage_invalid")


def _validate_workdays(
    workdays: list[dict[str, Any]] | None,
    *,
    decision_at_utc: datetime | None,
) -> dict[tuple[int, int, int], dict[str, Any]]:
    if workdays is None:
        return {}
    if type(workdays) is not list:
        raise ValueError("customs_workdays_invalid")
    if workdays and (
        not isinstance(decision_at_utc, datetime)
        or decision_at_utc.tzinfo is None
        or decision_at_utc.utcoffset() is None
    ):
        raise ValueError("customs_workday_decision_at_invalid")
    decision = None if decision_at_utc is None else decision_at_utc.astimezone(timezone.utc)
    result: dict[tuple[int, int, int], dict[str, Any]] = {}
    for row in workdays:
        if type(row) is not dict:
            raise ValueError("customs_workday_row_invalid")
        year = row.get("period_year")
        month = row.get("period_month")
        day = row.get("period_end_day")
        kind = row.get("period_kind")
        if (
            type(year) is not int
            or type(month) is not int
            or type(day) is not int
            or not 1 <= month <= 12
        ):
            raise ValueError("customs_workday_period_invalid")
        final_day = monthrange(year, month)[1]
        expected_kind = {10: "day_10", 20: "day_20", final_day: "month_end"}.get(day)
        if kind != expected_kind:
            raise ValueError("customs_workday_period_invalid")
        if row.get("calendar_domain") != _WORKDAY_DOMAIN:
            raise ValueError("customs_workday_source_not_kcs")
        if row.get("method_version") != _WORKDAY_METHOD_VERSION:
            raise ValueError("customs_workday_method_invalid")
        count = row.get("workdays_mtd_tenths")
        snapshot_id = row.get("snapshot_id")
        if type(count) is not int or count <= 0:
            raise ValueError("customs_workday_count_invalid")
        if type(snapshot_id) is not str or not snapshot_id or len(snapshot_id) > 128:
            raise ValueError("customs_workday_snapshot_id_invalid")
        _validate_workday_lineage(row, snapshot_id=snapshot_id)
        available_at = _parse_utc(row.get("available_at_utc"))
        if decision is not None and available_at > decision:
            raise ValueError("customs_workday_future_snapshot")
        key = (year, month, day)
        if key in result:
            raise ValueError("customs_workday_period_duplicate")
        result[key] = row
    return result


def _derive_workday_yoy(
    *,
    by_key: dict[tuple[int, int, int], dict[str, Any]],
    year: int,
    month: int,
    day: int,
    period_kind: str,
    current_amount: int | None,
    prior_amount: int | None,
) -> tuple[float | None, list[str], str | None, str | None]:
    previous_day = 0 if day == 10 else 10 if day == 20 else 20
    prior_day = monthrange(year - 1, month)[1] if period_kind == "month_end" else day
    current = by_key.get((year, month, day))
    prior = by_key.get((year - 1, month, prior_day))
    current_previous = None if previous_day == 0 else by_key.get((year, month, previous_day))
    prior_previous = None if previous_day == 0 else by_key.get((year - 1, month, previous_day))
    rows = [current, prior] if previous_day == 0 else [current_previous, current, prior_previous, prior]
    if any(row is None for row in rows):
        return None, [], None, "WORKDAY_LINEAGE_MISSING"
    assert current is not None
    assert prior is not None
    if previous_day:
        assert current_previous is not None
        assert prior_previous is not None
    concrete = [row for row in rows if row is not None]
    if len({row["method_version"] for row in concrete}) != 1:
        return None, [], None, "WORKDAY_VECTOR_MIXED_METHOD"
    release_fields = (
        "source_document_id",
        "source_uri",
        "source_title",
        "source_document_sha256",
        "scheduled_release_date_kst",
        "publication_precision",
        "available_at_utc",
        "revision_seq",
    )
    release_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = [
        (current, prior)
    ]
    if previous_day:
        assert current_previous is not None
        assert prior_previous is not None
        release_pairs.append((current_previous, prior_previous))
    if any(
        any(left[field] != right[field] for field in release_fields)
        for left, right in release_pairs
    ):
        return None, [], None, "WORKDAY_VECTOR_MIXED_RELEASE"
    if previous_day == 0:
        current_days = current["workdays_mtd_tenths"]
        prior_days = prior["workdays_mtd_tenths"]
    else:
        current_days = current["workdays_mtd_tenths"] - current_previous["workdays_mtd_tenths"]
        prior_days = prior["workdays_mtd_tenths"] - prior_previous["workdays_mtd_tenths"]
    snapshot_ids = [row["snapshot_id"] for row in concrete]
    if (
        current_amount is None
        or prior_amount is None
        or current_days <= 0
        or prior_days <= 0
    ):
        return None, snapshot_ids, None, "WORKDAY_INTERVAL_INVALID"
    yoy = _pct_change(current_amount / current_days, prior_amount / prior_days)
    if yoy is None:
        return None, snapshot_ids, None, "WORKDAY_REFERENCE_INVALID"
    vector_id = hashlib.sha256(
        json.dumps(snapshot_ids, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return yoy, snapshot_ids, vector_id, None


def _validate_period(row: Any) -> dict[str, Any]:
    if type(row) is not dict:
        raise ValueError("customs_feature_row_invalid")
    required_strings = ("snapshot_id", "snapshot_group_id", "period_kind")
    if any(type(row.get(name)) is not str or not row[name] for name in required_strings):
        raise ValueError("customs_feature_row_invalid")
    year = row.get("period_year")
    month = row.get("period_month")
    day = row.get("period_end_day")
    if (
        type(year) is not int
        or type(month) is not int
        or type(day) is not int
        or not 1 <= month <= 12
    ):
        raise ValueError("customs_feature_period_invalid")
    final_day = monthrange(year, month)[1]
    expected_kind = {10: "day_10", 20: "day_20", final_day: "month_end"}.get(day)
    if row["period_kind"] != expected_kind:
        raise ValueError("customs_feature_period_invalid")
    amounts = row.get("amounts_thousand_usd")
    expected_amounts = frozenset(("total", *INDUSTRIES))
    if type(amounts) is not dict or frozenset(amounts) != expected_amounts:
        raise ValueError("customs_feature_amounts_invalid")
    if any(type(value) is not int or value < 0 for value in amounts.values()):
        raise ValueError("customs_feature_amounts_invalid")
    if any(amounts[name] > amounts["total"] for name in INDUSTRIES):
        raise ValueError("customs_feature_amounts_invalid")
    return row


def _exposures(industry: str) -> list[dict[str, Any]]:
    return [
        {
            "symbol": symbol,
            "exposure_weight": 0,
            "mapping_status": "unverified_candidate",
            "mapping_version": "kcs-nature-label-candidates-v0",
        }
        for symbol in _UNVERIFIED_SYMBOL_CANDIDATES[industry]
    ]


def derive_industry_features(
    periods: list[dict[str, Any]],
    *,
    workdays: list[dict[str, Any]] | None = None,
    decision_at_utc: datetime | None = None,
) -> list[dict[str, Any]]:
    """같은 API 응답 vintage 안에서만 누적·구간 YoY를 계산한다."""
    if type(periods) is not list:
        raise ValueError("customs_feature_periods_invalid")
    rows = [_validate_period(row) for row in periods]
    workdays_by_key = _validate_workdays(
        workdays,
        decision_at_utc=decision_at_utc,
    )
    if len({row["snapshot_group_id"] for row in rows}) > 1:
        raise ValueError("customs_feature_snapshot_group_mixed")
    by_key: dict[tuple[str, int, int, int], dict[str, Any]] = {}
    for row in rows:
        key = (
            row["snapshot_group_id"],
            row["period_year"],
            row["period_month"],
            row["period_end_day"],
        )
        if key in by_key:
            raise ValueError("customs_feature_period_duplicate")
        by_key[key] = row

    output: list[dict[str, Any]] = []
    for current in rows:
        group = current["snapshot_group_id"]
        year = current["period_year"]
        month = current["period_month"]
        day = current["period_end_day"]
        previous_day = 0 if day == 10 else 10 if day == 20 else 20
        prior_day = (
            monthrange(year - 1, month)[1]
            if current["period_kind"] == "month_end"
            else day
        )
        prior = by_key.get((group, year - 1, month, prior_day))
        current_previous = (
            None if previous_day == 0 else by_key.get((group, year, month, previous_day))
        )
        prior_previous = (
            None
            if previous_day == 0
            else by_key.get((group, year - 1, month, previous_day))
        )
        required = [prior]
        if previous_day:
            required.extend((current_previous, prior_previous))

        for industry in INDUSTRIES:
            missing_reasons: list[str] = []
            if any(item is None for item in required):
                missing_reasons.append("MISSING_COMPARABLE_CUTOFF")
            current_amount = current["amounts_thousand_usd"][industry]
            prior_amount = None if prior is None else prior["amounts_thousand_usd"][industry]
            current_total = current["amounts_thousand_usd"]["total"]
            prior_total = None if prior is None else prior["amounts_thousand_usd"]["total"]

            if previous_day == 0:
                current_interval = current_amount
                prior_interval = prior_amount
                current_interval_days = 10
                prior_interval_days = 10
            else:
                current_interval = (
                    None
                    if current_previous is None
                    else current_amount
                    - current_previous["amounts_thousand_usd"][industry]
                )
                prior_interval = (
                    None
                    if prior_amount is None or prior_previous is None
                    else prior_amount - prior_previous["amounts_thousand_usd"][industry]
                )
                current_interval_days = (
                    None if current_previous is None else day - previous_day
                )
                prior_interval_days = (
                    None if prior_amount is None or prior_previous is None
                    else prior_day - previous_day
                )
            interval_revised = any(
                value is not None and value < 0
                for value in (current_interval, prior_interval)
            )
            if interval_revised:
                missing_reasons.append("SOURCE_REVISION_OR_INCONSISTENCY")

            cumulative_yoy = (
                None
                if prior_amount is None
                else _pct_change(current_amount, prior_amount)
            )
            interval_yoy = (
                None
                if interval_revised
                or current_interval is None
                or prior_interval is None
                else _pct_change(current_interval, prior_interval)
            )
            calendar_day_yoy = (
                None
                if interval_revised
                or current_interval is None
                or prior_interval is None
                or current_interval_days is None
                or prior_interval_days is None
                else _pct_change(
                    current_interval / current_interval_days,
                    prior_interval / prior_interval_days,
                )
            )
            share = None if current_total <= 0 else current_amount / current_total * 100.0
            prior_share = (
                None
                if prior_amount is None or prior_total is None or prior_total <= 0
                else prior_amount / prior_total * 100.0
            )
            share_change = None if share is None or prior_share is None else share - prior_share
            if cumulative_yoy is None or (
                interval_yoy is None and not interval_revised
            ):
                missing_reasons.append("ZERO_OR_MISSING_REFERENCE")

            source_rows = (
                [current, prior]
                if previous_day == 0
                else [current_previous, current, prior_previous, prior]
            )
            source_snapshot_ids = [
                item["snapshot_id"] for item in source_rows if item is not None
            ]
            raw_feature_ready = not missing_reasons
            (
                workday_yoy,
                workday_snapshot_ids,
                workday_vector_id,
                workday_error,
            ) = _derive_workday_yoy(
                by_key=workdays_by_key,
                year=year,
                month=month,
                day=day,
                period_kind=current["period_kind"],
                current_amount=current_interval,
                prior_amount=prior_interval,
            )
            workday_feature_ready = workday_yoy is not None and workday_error is None
            workday_quality_flags = [] if workday_error is None else [workday_error]
            workday_quality = (
                "kcs_reported"
                if workday_feature_ready
                else "missing"
                if workday_error == "WORKDAY_LINEAGE_MISSING"
                else "invalid"
            )
            output.append(
                {
                    "feature_set_version": 2,
                    "industry": industry,
                    "period_year": year,
                    "period_month": month,
                    "period_end_day": day,
                    "period_kind": current["period_kind"],
                    "period_scope": "mtd_cumulative",
                    "snapshot_group_id": group,
                    "current_amount_thousand_usd": current_amount,
                    "cumulative_yoy_pct": cumulative_yoy,
                    "interval_amount_thousand_usd": current_interval,
                    "prior_interval_amount_thousand_usd": prior_interval,
                    "interval_yoy_pct": interval_yoy,
                    "calendar_day_adjusted_interval_yoy_pct": calendar_day_yoy,
                    "workday_adjusted_interval_yoy_pct": workday_yoy,
                    "workday_quality": workday_quality,
                    "business_day_adjusted": workday_feature_ready,
                    "workday_snapshot_ids": workday_snapshot_ids,
                    "workday_vector_id": workday_vector_id,
                    "raw_feature_ready": raw_feature_ready,
                    "calendar_feature_ready": calendar_day_yoy is not None,
                    "workday_feature_ready": workday_feature_ready,
                    "export_share_pct": share,
                    "export_share_yoy_pp": share_change,
                    "feature_ready": raw_feature_ready and workday_feature_ready,
                    "quality_flags": sorted(
                        set((*missing_reasons, *workday_quality_flags))
                    ),
                    "source_snapshot_ids": source_snapshot_ids,
                    "symbol_exposures": _exposures(industry),
                    "taxonomy_status": "official_label_taxonomy_unverified",
                    "eligible_for_production_score": False,
                    "vintage_policy": "realtime_as_observed",
                }
            )
    return output
