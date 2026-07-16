"""관세청 누적 10일 수출을 비교 가능한 산업 피처로 변환한다."""

from calendar import monthrange

import pytest


def _row(year, month, day, total, semiconductors, snapshot_id, group_id="run-v1"):
    kind = (
        "day_10"
        if day == 10
        else "day_20"
        if day == 20
        else "month_end"
        if day == monthrange(year, month)[1]
        else "invalid"
    )
    amounts = {
        "total": total,
        "semiconductors": semiconductors,
        "steel_products": 0,
        "passenger_cars": 0,
        "petroleum_products": 0,
        "wireless_communication_devices": 0,
        "ships": 0,
        "auto_parts": 0,
        "computer_peripherals": 0,
        "precision_instruments": 0,
        "home_appliances": 0,
    }
    return {
        "snapshot_id": snapshot_id,
        "snapshot_group_id": group_id,
        "period_year": year,
        "period_month": month,
        "period_end_day": day,
        "period_kind": kind,
        "amounts_thousand_usd": amounts,
        "provisional": year == 2026,
    }


def _feature(periods, *, year=2026, month=7, day=10, industry="semiconductors"):
    from core.customs_export_features import derive_industry_features

    return next(
        row
        for row in derive_industry_features(periods)
        if row["period_year"] == year
        and row["period_month"] == month
        and row["period_end_day"] == day
        and row["industry"] == industry
    )


def test_day10_uses_cumulative_amount_as_interval_and_computes_yoy():
    feature = _feature(
        [
            _row(2025, 7, 10, 1000, 100, "prior-10"),
            _row(2026, 7, 10, 1200, 120, "current-10"),
        ]
    )

    assert feature["feature_ready"] is True
    assert feature["quality_flags"] == []
    assert feature["cumulative_yoy_pct"] == pytest.approx(20)
    assert feature["interval_amount_thousand_usd"] == 120
    assert feature["prior_interval_amount_thousand_usd"] == 100
    assert feature["interval_yoy_pct"] == pytest.approx(20)
    assert feature["calendar_day_adjusted_interval_yoy_pct"] == pytest.approx(20)
    assert feature["source_snapshot_ids"] == ["current-10", "prior-10"]


def test_mixed_snapshot_groups_are_rejected_as_one_invalid_batch():
    from core.customs_export_features import derive_industry_features

    periods = [
        _row(2025, 7, 10, 1000, 100, "prior-v1", group_id="vintage-1"),
        _row(2026, 7, 10, 1200, 120, "current-v1", group_id="vintage-1"),
        _row(2025, 7, 10, 900, 90, "prior-v2", group_id="vintage-2"),
        _row(2026, 7, 10, 1300, 130, "current-v2", group_id="vintage-2"),
    ]

    with pytest.raises(ValueError, match="customs_feature_snapshot_group_mixed"):
        derive_industry_features(periods)


def test_zero_reference_value_is_not_divided_and_marks_feature_unready():
    feature = _feature(
        [
            _row(2025, 7, 10, 1000, 0, "prior-zero"),
            _row(2026, 7, 10, 1200, 120, "current-10"),
        ]
    )

    assert feature["feature_ready"] is False
    assert feature["cumulative_yoy_pct"] is None
    assert feature["interval_yoy_pct"] is None
    assert feature["calendar_day_adjusted_interval_yoy_pct"] is None
    assert feature["quality_flags"] == ["ZERO_OR_MISSING_REFERENCE"]
    assert feature["source_snapshot_ids"] == ["current-10", "prior-zero"]


def test_negative_interval_sets_revision_flag_and_marks_feature_unready():
    feature = _feature(
        [
            _row(2025, 7, 10, 800, 80, "prior-10"),
            _row(2025, 7, 20, 1700, 170, "prior-20"),
            _row(2026, 7, 10, 1000, 100, "current-10"),
            _row(2026, 7, 20, 1200, 90, "current-20"),
        ],
        day=20,
    )

    assert feature["interval_amount_thousand_usd"] == -10
    assert feature["prior_interval_amount_thousand_usd"] == 90
    assert feature["interval_yoy_pct"] is None
    assert feature["calendar_day_adjusted_interval_yoy_pct"] is None
    assert feature["feature_ready"] is False
    assert feature["quality_flags"] == ["SOURCE_REVISION_OR_INCONSISTENCY"]


def test_one_sided_negative_interval_still_suppresses_yoy():
    feature = _feature(
        [
            _row(2025, 7, 10, 800, 80, "prior-10"),
            _row(2026, 7, 10, 1000, 100, "current-10"),
            _row(2026, 7, 20, 1200, 90, "current-20"),
        ],
        day=20,
    )

    assert feature["interval_amount_thousand_usd"] == -10
    assert feature["prior_interval_amount_thousand_usd"] is None
    assert feature["interval_yoy_pct"] is None
    assert feature["calendar_day_adjusted_interval_yoy_pct"] is None
    assert "SOURCE_REVISION_OR_INCONSISTENCY" in feature["quality_flags"]
    assert feature["feature_ready"] is False


def test_all_industry_mappings_stay_zero_weight_and_precision_is_unmapped():
    from core.customs_export_features import INDUSTRIES, derive_industry_features

    features = [
        row
        for row in derive_industry_features(
            [
                _row(2025, 7, 10, 1000, 100, "prior-10"),
                _row(2026, 7, 10, 1200, 120, "current-10"),
            ]
        )
        if row["period_year"] == 2026 and row["period_end_day"] == 10
    ]

    assert {row["industry"] for row in features} == set(INDUSTRIES)
    assert all(row["eligible_for_production_score"] is False for row in features)
    assert all(
        exposure["exposure_weight"] == 0
        for row in features
        for exposure in row["symbol_exposures"]
    )
    precision = next(
        row for row in features if row["industry"] == "precision_instruments"
    )
    assert precision["symbol_exposures"] == []


def test_day20_uses_increment_not_cumulative_for_interval_yoy():
    from core.customs_export_features import derive_industry_features

    periods = [
        _row(2025, 7, 10, 800, 80, "prior-10"),
        _row(2025, 7, 20, 1700, 170, "prior-20"),
        _row(2026, 7, 10, 1000, 100, "current-10"),
        _row(2026, 7, 20, 2300, 260, "current-20"),
    ]

    features = derive_industry_features(periods)
    semiconductor = next(
        row
        for row in features
        if row["period_year"] == 2026
        and row["period_month"] == 7
        and row["period_end_day"] == 20
        and row["industry"] == "semiconductors"
    )

    assert semiconductor["feature_ready"] is True
    assert semiconductor["cumulative_yoy_pct"] == pytest.approx((260 / 170 - 1) * 100)
    assert semiconductor["interval_amount_thousand_usd"] == 160
    assert semiconductor["prior_interval_amount_thousand_usd"] == 90
    assert semiconductor["interval_yoy_pct"] == pytest.approx((160 / 90 - 1) * 100)
    assert semiconductor["calendar_day_adjusted_interval_yoy_pct"] == pytest.approx(
        (160 / 10) / (90 / 10) * 100 - 100
    )
    assert semiconductor["export_share_yoy_pp"] == pytest.approx(
        260 / 2300 * 100 - 170 / 1700 * 100
    )
    assert semiconductor["business_day_adjusted"] is False
    assert semiconductor["workday_adjusted_interval_yoy_pct"] is None
    assert semiconductor["workday_quality"] == "missing"
    assert semiconductor["source_snapshot_ids"] == [
        "current-10",
        "current-20",
        "prior-10",
        "prior-20",
    ]
    assert [item["symbol"] for item in semiconductor["symbol_exposures"][:2]] == [
        "005930.KS",
        "000660.KS",
    ]
    assert all(
        item["exposure_weight"] == 0
        and item["mapping_status"] == "unverified_candidate"
        for item in semiconductor["symbol_exposures"]
    )
    assert semiconductor["eligible_for_production_score"] is False


def test_month_end_compares_calendar_month_end_across_leap_year():
    from core.customs_export_features import derive_industry_features

    periods = [
        _row(2023, 2, 20, 5000, 500, "prior-20"),
        _row(2023, 2, 28, 8000, 800, "prior-end"),
        _row(2024, 2, 20, 6000, 600, "current-20"),
        _row(2024, 2, 29, 9000, 900, "current-end"),
    ]

    feature = next(
        row
        for row in derive_industry_features(periods)
        if row["period_year"] == 2024
        and row["period_end_day"] == 29
        and row["industry"] == "semiconductors"
    )

    assert feature["feature_ready"] is True
    assert feature["cumulative_yoy_pct"] == pytest.approx(12.5)
    assert feature["interval_amount_thousand_usd"] == 300
    assert feature["prior_interval_amount_thousand_usd"] == 300
    assert feature["interval_yoy_pct"] == pytest.approx(0)
    assert feature["calendar_day_adjusted_interval_yoy_pct"] == pytest.approx(
        (300 / 9) / (300 / 8) * 100 - 100
    )
    assert feature["source_snapshot_ids"] == [
        "current-20",
        "current-end",
        "prior-20",
        "prior-end",
    ]
