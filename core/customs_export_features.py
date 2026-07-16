"""관세청 누적 수출을 누수 없는 shadow 산업 피처로 변환한다."""

from __future__ import annotations

from calendar import monthrange
from typing import Any

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


def derive_industry_features(periods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """같은 API 응답 vintage 안에서만 누적·구간 YoY를 계산한다."""
    if type(periods) is not list:
        raise ValueError("customs_feature_periods_invalid")
    rows = [_validate_period(row) for row in periods]
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
            output.append(
                {
                    "feature_set_version": 1,
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
                    "workday_adjusted_interval_yoy_pct": None,
                    "workday_quality": "missing",
                    "business_day_adjusted": False,
                    "export_share_pct": share,
                    "export_share_yoy_pp": share_change,
                    "feature_ready": not missing_reasons,
                    "quality_flags": sorted(set(missing_reasons)),
                    "source_snapshot_ids": source_snapshot_ids,
                    "symbol_exposures": _exposures(industry),
                    "taxonomy_status": "official_label_taxonomy_unverified",
                    "eligible_for_production_score": False,
                    "vintage_policy": "realtime_as_observed",
                }
            )
    return output
