"""관세청 누적 10일 수출을 비교 가능한 산업 피처로 변환한다."""

from calendar import monthrange
import hashlib

import pytest

from datetime import date, datetime, timedelta, timezone


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


def _workday(
    year,
    month,
    day,
    tenths,
    snapshot_id,
    *,
    available_at=None,
    calendar_domain="KCS_REPORTED_OPERATING_DAYS",
):
    kind = (
        "day_10"
        if day == 10
        else "day_20"
        if day == 20
        else "month_end"
    )
    document_id = f"156{month:02d}{day:02d}00"
    source_hash = hashlib.sha256(document_id.encode("ascii")).hexdigest()
    release_year = 2026
    if day in {10, 20}:
        source_title = (
            f"’{release_year % 100:02d}년 {month}월 1일 ~ {month}월 {day}일 수출입 현황"
        )
        scheduled = date(release_year, month, day + 1)
    else:
        source_title = f"’{release_year % 100:02d}년 {month}월 수출입 현황"
        scheduled = date(release_year, month, monthrange(release_year, month)[1]) + timedelta(
            days=1
        )
    return {
        "source_record_id": f"{year:04d}{month:02d}{day:02d}:workdays",
        "period_year": year,
        "period_month": month,
        "period_end_day": day,
        "period_kind": kind,
        "workdays_mtd_tenths": tenths,
        "calendar_domain": calendar_domain,
        "method_version": "korea-kr-kcs-press-release-v1",
        "snapshot_id": snapshot_id,
        "source_document_id": document_id,
        "source_uri": (
            "https://m.korea.kr/briefing/pressReleaseView.do?newsId="
            + document_id
        ),
        "source_title": source_title,
        "source_agency": "관세청",
        "detail_header_title": source_title,
        "detail_header_release_date_kst": scheduled.isoformat(),
        "detail_header_verified": True,
        "source_document_sha256": source_hash,
        "scheduled_release_date_kst": scheduled.isoformat(),
        "source_published_at_utc": None,
        "publication_precision": "date_only",
        "available_at_utc": available_at or "2026-07-16T00:00:00.000000Z",
        "available_at_field": "observation.ingested_at",
        "revision_policy": "append_only_content_hash",
        "revision_seq": 0,
        "supersedes_snapshot_id": None,
        "shadow_only": True,
        "eligible_for_production_score": False,
    }


def _feature(
    periods,
    *,
    year=2026,
    month=7,
    day=10,
    industry="semiconductors",
    workdays=None,
    decision_at=None,
):
    from core.customs_export_features import derive_industry_features

    return next(
        row
        for row in derive_industry_features(
            periods,
            workdays=workdays,
            decision_at_utc=decision_at,
        )
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

    assert feature["raw_feature_ready"] is True
    assert feature["workday_feature_ready"] is False
    assert feature["feature_ready"] is False
    assert feature["quality_flags"] == ["WORKDAY_LINEAGE_MISSING"]
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
    assert feature["quality_flags"] == [
        "WORKDAY_LINEAGE_MISSING",
        "ZERO_OR_MISSING_REFERENCE",
    ]
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
    assert feature["quality_flags"] == [
        "SOURCE_REVISION_OR_INCONSISTENCY",
        "WORKDAY_LINEAGE_MISSING",
    ]


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

    assert semiconductor["raw_feature_ready"] is True
    assert semiconductor["workday_feature_ready"] is False
    assert semiconductor["feature_ready"] is False
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

    assert feature["raw_feature_ready"] is True
    assert feature["workday_feature_ready"] is False
    assert feature["feature_ready"] is False
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


def test_missing_official_workday_lineage_blocks_top_level_readiness():
    feature = _feature(
        [
            _row(2025, 7, 10, 1000, 100, "prior-10"),
            _row(2026, 7, 10, 1200, 120, "current-10"),
        ]
    )

    assert feature["raw_feature_ready"] is True
    assert feature["calendar_feature_ready"] is True
    assert feature["workday_feature_ready"] is False
    assert feature["feature_ready"] is False
    assert feature["workday_adjusted_interval_yoy_pct"] is None
    assert feature["workday_quality"] == "missing"
    assert feature["business_day_adjusted"] is False
    assert "WORKDAY_LINEAGE_MISSING" in feature["quality_flags"]


def test_day10_uses_exact_kcs_reported_workdays_for_daily_yoy():
    feature = _feature(
        [
            _row(2025, 7, 10, 1000, 100, "prior-10"),
            _row(2026, 7, 10, 1200, 120, "current-10"),
        ],
        workdays=[
            _workday(2025, 7, 10, 70, "wd-prior-10"),
            _workday(2026, 7, 10, 80, "wd-current-10"),
        ],
        decision_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )

    assert feature["workday_adjusted_interval_yoy_pct"] == pytest.approx(5.0)
    assert feature["workday_feature_ready"] is True
    assert feature["feature_ready"] is True
    assert feature["workday_quality"] == "kcs_reported"
    assert feature["business_day_adjusted"] is True
    assert feature["workday_snapshot_ids"] == ["wd-current-10", "wd-prior-10"]
    assert len(feature["workday_vector_id"]) == 64
    assert "WORKDAY_LINEAGE_MISSING" not in feature["quality_flags"]


def test_day20_subtracts_cumulative_workdays_before_daily_yoy():
    feature = _feature(
        [
            _row(2025, 7, 10, 1000, 100, "prior-10"),
            _row(2025, 7, 20, 2000, 250, "prior-20"),
            _row(2026, 7, 10, 1100, 120, "current-10"),
            _row(2026, 7, 20, 2300, 300, "current-20"),
        ],
        day=20,
        workdays=[
            _workday(2025, 7, 10, 70, "wd-prior-10"),
            _workday(2025, 7, 20, 140, "wd-prior-20"),
            _workday(2026, 7, 10, 80, "wd-current-10"),
            _workday(2026, 7, 20, 150, "wd-current-20"),
        ],
        decision_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )

    assert feature["interval_amount_thousand_usd"] == 180
    assert feature["prior_interval_amount_thousand_usd"] == 150
    assert feature["workday_adjusted_interval_yoy_pct"] == pytest.approx(20.0)
    assert feature["workday_snapshot_ids"] == [
        "wd-current-10",
        "wd-current-20",
        "wd-prior-10",
        "wd-prior-20",
    ]


def test_month_end_subtracts_cumulative_workdays_before_daily_yoy():
    feature = _feature(
        [
            _row(2025, 7, 20, 2000, 250, "prior-20"),
            _row(2025, 7, 31, 3000, 450, "prior-end"),
            _row(2026, 7, 20, 2300, 300, "current-20"),
            _row(2026, 7, 31, 3400, 500, "current-end"),
        ],
        day=31,
        workdays=[
            _workday(2025, 7, 20, 140, "wd-prior-20"),
            _workday(2025, 7, 31, 220, "wd-prior-end"),
            _workday(2026, 7, 20, 150, "wd-current-20"),
            _workday(2026, 7, 31, 210, "wd-current-end"),
        ],
        decision_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )

    assert feature["interval_amount_thousand_usd"] == 200
    assert feature["prior_interval_amount_thousand_usd"] == 200
    assert feature["workday_adjusted_interval_yoy_pct"] == pytest.approx(100 / 3)


def test_non_monotonic_workday_vector_is_null_with_typed_flag():
    feature = _feature(
        [
            _row(2025, 7, 10, 1000, 100, "prior-10"),
            _row(2025, 7, 20, 2000, 250, "prior-20"),
            _row(2026, 7, 10, 1100, 120, "current-10"),
            _row(2026, 7, 20, 2300, 300, "current-20"),
        ],
        day=20,
        workdays=[
            _workday(2025, 7, 10, 70, "wd-prior-10"),
            _workday(2025, 7, 20, 140, "wd-prior-20"),
            _workday(2026, 7, 10, 80, "wd-current-10"),
            _workday(2026, 7, 20, 80, "wd-current-20"),
        ],
        decision_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )

    assert feature["workday_adjusted_interval_yoy_pct"] is None
    assert feature["workday_feature_ready"] is False
    assert feature["workday_quality"] == "invalid"
    assert "WORKDAY_INTERVAL_INVALID" in feature["quality_flags"]


def test_krx_trading_days_are_rejected_as_workday_source():
    with pytest.raises(ValueError, match="customs_workday_source_not_kcs"):
        _feature(
            [
                _row(2025, 7, 10, 1000, 100, "prior-10"),
                _row(2026, 7, 10, 1200, 120, "current-10"),
            ],
            workdays=[
                _workday(
                    2025,
                    7,
                    10,
                    70,
                    "wd-prior-10",
                    calendar_domain="KRX_TRADING_DAYS",
                ),
                _workday(2026, 7, 10, 80, "wd-current-10"),
            ],
            decision_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )


def test_future_workday_snapshot_is_rejected_at_decision_boundary():
    with pytest.raises(ValueError, match="customs_workday_future_snapshot"):
        _feature(
            [
                _row(2025, 7, 10, 1000, 100, "prior-10"),
                _row(2026, 7, 10, 1200, 120, "current-10"),
            ],
            workdays=[
                _workday(2025, 7, 10, 70, "wd-prior-10"),
                _workday(
                    2026,
                    7,
                    10,
                    80,
                    "wd-current-10",
                    available_at="2026-07-16T00:00:00.000001Z",
                ),
            ],
            decision_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )


def test_missing_official_source_sha_is_rejected_before_workday_ready():
    workdays = [
        _workday(2025, 7, 10, 70, "wd-prior-10"),
        _workday(2026, 7, 10, 80, "wd-current-10"),
    ]
    del workdays[0]["source_document_sha256"]

    with pytest.raises(ValueError, match="customs_workday_lineage_invalid"):
        _feature(
            [
                _row(2025, 7, 10, 1000, 100, "prior-10"),
                _row(2026, 7, 10, 1200, 120, "current-10"),
            ],
            workdays=workdays,
            decision_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize(
    ("field", "mutated"),
    (
        ("source_document_sha256", "A" * 64),
        (
            "source_uri",
            "https://m.korea.kr/briefing/pressReleaseView.do?newsId=156071000&x=1",
        ),
        ("source_published_at_utc", "2026-07-11T00:00:00.000000Z"),
        ("publication_precision", "exact"),
        ("available_at_field", "scheduled_release_date_kst"),
        ("source_agency", "한국거래소"),
        ("detail_header_title", "다른 상세 제목"),
        ("detail_header_release_date_kst", "2026-07-12"),
        ("detail_header_verified", False),
        ("revision_seq", 1),
        ("shadow_only", False),
        ("eligible_for_production_score", True),
    ),
)
def test_workday_lineage_mutations_are_rejected(field, mutated):
    workdays = [
        _workday(2025, 7, 10, 70, "wd-prior-10"),
        _workday(2026, 7, 10, 80, "wd-current-10"),
    ]
    workdays[0][field] = mutated

    with pytest.raises(ValueError, match="customs_workday_lineage_invalid"):
        _feature(
            [
                _row(2025, 7, 10, 1000, 100, "prior-10"),
                _row(2026, 7, 10, 1200, 120, "current-10"),
            ],
            workdays=workdays,
            decision_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize("mutation", ("coherent_bad_title", "coherent_bad_date"))
def test_coherently_forged_detail_metadata_is_rejected(mutation):
    workdays = [
        _workday(2025, 7, 10, 70, "wd-prior-10"),
        _workday(2026, 7, 10, 80, "wd-current-10"),
    ]
    for row in workdays:
        if mutation == "coherent_bad_title":
            row["source_title"] = "공식처럼 보이지만 기간이 없는 제목"
            row["detail_header_title"] = row["source_title"]
        else:
            row["scheduled_release_date_kst"] = "2026-07-31"
            row["detail_header_release_date_kst"] = "2026-07-31"

    with pytest.raises(ValueError, match="customs_workday_lineage_invalid"):
        _feature(
            [
                _row(2025, 7, 10, 1000, 100, "prior-10"),
                _row(2026, 7, 10, 1200, 120, "current-10"),
            ],
            workdays=workdays,
            decision_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )


def test_valid_but_mixed_release_hashes_make_workday_vector_not_ready():
    workdays = [
        _workday(2025, 7, 10, 70, "wd-prior-10"),
        _workday(2026, 7, 10, 80, "wd-current-10"),
    ]
    workdays[1]["source_document_sha256"] = "b" * 64

    feature = _feature(
        [
            _row(2025, 7, 10, 1000, 100, "prior-10"),
            _row(2026, 7, 10, 1200, 120, "current-10"),
        ],
        workdays=workdays,
        decision_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )

    assert feature["workday_adjusted_interval_yoy_pct"] is None
    assert feature["workday_feature_ready"] is False
    assert feature["feature_ready"] is False
    assert "WORKDAY_VECTOR_MIXED_RELEASE" in feature["quality_flags"]
