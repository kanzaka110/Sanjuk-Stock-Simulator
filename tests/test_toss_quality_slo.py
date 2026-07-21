from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import copy

import pytest

from core.shadow_measurements import ShadowMeasurementStore
from core.source_observations_v2 import SourceObservationStoreV2
from src.toss_quality_slo import (
    build_quality_report,
    evaluate_candidate_snapshot,
    evaluate_source_run_health,
    load_consecutive_zero_ready_read_only,
    load_source_runs_read_only,
    render_alert,
    run_quality_watchdog,
)


def _payload(*, market: str = "KR", fallback: bool = False) -> dict:
    return {
        "schema": "toss_buy_candidates.v3.dual_income_ev",
        "scan_summary": {
            "market": market,
            "dependency_fallback_used": fallback,
            "universe_count": 12,
            "scanned_count": 12,
            "toss_held_excluded_count": 1,
            "recent_risk_sell_excluded_count": 1,
            "pass_count": 6,
            "reject_count": 4,
            "executable_count": 5,
            "income_gate_eligible_count": 4,
            "upstream_executable_count": 4,
            "income_pass_count": 0,
            "income_ready_count": 0,
            "returned_candidate_count": 2,
            "returned_income_ready_count": 0,
            "income_liveness_version": "income_liveness_v1",
            "income_liveness_status": "degraded",
            "income_liveness_diagnosis": {
                "reason": "upstream_executable_but_no_income_ready",
                "upstream_executable_count": 4,
                "income_pass_count": 0,
                "income_ready_count": 0,
                "top_income_block_reasons": [
                    {"reason": "decision_model_unsupported", "count": 2}
                ],
            },
        },
        "items": [
            {
                "symbol": "005930.KS" if market == "KR" else "MU",
                "stock_agent_ready": False,
                "execution_status": "data_quality_block",
                "income_strategy": {
                    "income_pass": False,
                    "income_block_reason": "decision_model_unsupported",
                },
            },
            {
                "symbol": "000660.KS" if market == "KR" else "NVDA",
                "stock_agent_ready": False,
                "execution_status": "hold_risk_flags",
                "income_strategy": {
                    "income_pass": False,
                    "income_block_reason": "risk_flags",
                },
            },
        ],
    }


def test_candidate_snapshot_preserves_authoritative_funnel_and_degraded_reason():
    result = evaluate_candidate_snapshot(_payload(), expected_market="KR")

    assert result == {
        "market": "KR",
        "status": "degraded",
        "dependency_fallback_used": False,
        "candidate_count": 2,
        "upstream_executable_count": 4,
        "income_pass_count": 0,
        "ready_count": 0,
        "funnel": {
            "discovered": 12,
            "scanned": 12,
            "held_excluded": 1,
            "recent_risk_sell_excluded": 1,
            "quality_pass": 6,
            "quality_reject": 4,
            "executable": 5,
            "income_eligible": 4,
            "income_pass": 0,
            "ready": 0,
            "returned": 2,
        },
        "top_block_reasons": [
            {"reason": "decision_model_unsupported", "count": 2}
        ],
    }


def test_candidate_snapshot_exposes_fallback_without_relabeling_status():
    result = evaluate_candidate_snapshot(
        _payload(market="US", fallback=True),
        expected_market="US",
    )

    assert result["status"] == "degraded"
    assert result["dependency_fallback_used"] is True


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value["scan_summary"].__setitem__("dependency_fallback_used", "false"),
        lambda value: value["scan_summary"].__setitem__("income_pass_count", True),
        lambda value: value["scan_summary"].__setitem__("income_liveness_status", "healthy"),
        lambda value: value.__setitem__("items", {"not": "a list"}),
        lambda value: value["items"][0].__setitem__("stock_agent_ready", 1),
    ],
)
def test_candidate_snapshot_rejects_malformed_or_contradictory_authority(mutator):
    payload = copy.deepcopy(_payload())
    mutator(payload)

    with pytest.raises(ValueError, match="candidate_snapshot_invalid"):
        evaluate_candidate_snapshot(payload, expected_market="KR")


def test_candidate_snapshot_rejects_market_mismatch():
    with pytest.raises(ValueError, match="candidate_snapshot_invalid"):
        evaluate_candidate_snapshot(_payload(market="US"), expected_market="KR")


def test_source_health_keeps_primary_failure_visible_when_fallback_succeeds():
    rows = [
        {
            "source": "kis",
            "dataset": "domestic_investor_flow",
            "status": "success",
            "completed_at": "2026-07-17T00:01:21.444599Z",
            "error_type": "",
        },
        {
            "source": "kis",
            "dataset": "domestic_investor_flow",
            "status": "failed",
            "completed_at": "2026-07-21T05:20:00.000000Z",
            "error_type": "numeric",
        },
        {
            "source": "kis",
            "dataset": "domestic_investor_flow",
            "status": "failed",
            "completed_at": "2026-07-21T06:20:00.000000Z",
            "error_type": "numeric",
        },
        {
            "source": "naver",
            "dataset": "domestic_investor_flow",
            "status": "success",
            "completed_at": "2026-07-21T06:21:00.000000Z",
            "error_type": "",
        },
        {
            "source": "kis",
            "dataset": "domestic_orderbook",
            "status": "success",
            "completed_at": "2026-07-21T06:21:00.000000Z",
            "error_type": "",
        },
    ]

    result = evaluate_source_run_health(rows)

    assert result == {
        "status": "degraded",
        "primary_failures": [
            {
                "source": "kis",
                "dataset": "domestic_investor_flow",
                "status": "failed",
                "error_type": "numeric",
                "consecutive_non_success": 2,
            }
        ],
        "active_fallbacks": [
            {
                "source": "naver",
                "dataset": "domestic_investor_flow",
                "primary_source": "kis",
            }
        ],
        "coverage_gaps": [
            {"source": "krx_openapi", "dataset": "domestic_eod_quote"}
        ],
    }


def test_source_health_rejects_unknown_status_and_string_boolean_like_fields():
    bad_rows = [
        {
            "source": "kis",
            "dataset": "domestic_orderbook",
            "status": "healthy",
            "completed_at": "2026-07-21T06:21:00.000000Z",
            "error_type": "false",
        }
    ]

    with pytest.raises(ValueError, match="source_run_health_invalid"):
        evaluate_source_run_health(bad_rows)


def test_source_run_loader_is_read_only_and_returns_allowlisted_rows(tmp_path):
    db_path = tmp_path / "source_observations_v2.db"
    store = SourceObservationStoreV2(db_path)
    completed = datetime(2026, 7, 21, 6, 21, tzinfo=timezone.utc)
    store.record_collection_run(
        source="kis",
        dataset="domestic_orderbook",
        run_id="kis-orderbook-1",
        started_at=completed,
        completed_at=completed,
        status="success",
        rows_seen=1,
        rows_inserted=1,
        rows_duplicate=0,
        rows_skipped=0,
        rows_invalid=0,
        error_type="",
    )
    before = db_path.read_bytes()

    rows = load_source_runs_read_only(db_path)

    assert rows == [
        {
            "source": "kis",
            "dataset": "domestic_orderbook",
            "status": "success",
            "completed_at": "2026-07-21T06:21:00.000000Z",
            "error_type": "",
        }
    ]
    assert db_path.read_bytes() == before
    assert not Path(str(db_path) + "-wal").exists()
    assert not Path(str(db_path) + "-shm").exists()


def _append_shadow_cohort(
    store: ShadowMeasurementStore,
    *,
    market: str,
    decided_at: datetime,
    ready: tuple[bool, ...],
    sequence: int,
) -> None:
    for position, is_ready in enumerate(ready):
        decision_id = f"{market.lower()}-{sequence}-{position}"
        store.append_decision(
            decision_id=decision_id,
            decision_ref=f"shadow:{decision_id}",
            symbol="005930.KS" if market == "KR" else "MU",
            side="BUY",
            decided_at_utc=decided_at,
            production_bucket="PASS_EXECUTE" if is_ready else "WATCH",
            production_score=80.0 if is_ready else 60.0,
            feature_set_version="toss_final_candidate_v2_dual_income_ev",
            features={
                "market": market,
                "market_scope": market,
                "cohort_position": position,
                "cohort_size": len(ready),
                "final_state": {"stock_agent_ready": is_ready},
            },
            source_snapshots=[
                {
                    "snapshot_id": "srcobs_" + ("a" * 64),
                    "source": "toss_final_candidate",
                    "ingested_at_utc": decided_at,
                    "payload_sha256": "b" * 64,
                }
            ],
            candidate_snapshot_sha256="c" * 64,
        )


def test_shadow_loader_counts_consecutive_zero_ready_cohorts_read_only(tmp_path):
    db_path = tmp_path / "shadow_measurements.db"
    store = ShadowMeasurementStore(
        db_path,
        now_fn=lambda: datetime(2026, 7, 21, 23, 0, tzinfo=timezone.utc),
    )
    origin = datetime(2026, 7, 21, 1, 0, tzinfo=timezone.utc)
    _append_shadow_cohort(store, market="KR", decided_at=origin, ready=(True, False), sequence=0)
    for sequence in range(1, 4):
        _append_shadow_cohort(
            store,
            market="KR",
            decided_at=origin + timedelta(hours=sequence),
            ready=(False, False),
            sequence=sequence,
        )
    for sequence in range(2):
        _append_shadow_cohort(
            store,
            market="US",
            decided_at=origin + timedelta(hours=10 + sequence),
            ready=(False,),
            sequence=sequence,
        )
    before = db_path.read_bytes()

    result = load_consecutive_zero_ready_read_only(db_path)

    assert result == {"KR": 3, "US": 2}
    assert db_path.read_bytes() == before
    assert not Path(str(db_path) + "-wal").exists()
    assert not Path(str(db_path) + "-shm").exists()


def test_quality_report_alerts_ready_zero_primary_failure_and_explicit_fallback():
    kr = evaluate_candidate_snapshot(_payload(), expected_market="KR")
    us = evaluate_candidate_snapshot(_payload(market="US"), expected_market="US")
    source = evaluate_source_run_health(
        [
            {
                "source": "kis",
                "dataset": "domestic_investor_flow",
                "status": "failed",
                "completed_at": "2026-07-21T06:20:00.000000Z",
                "error_type": "numeric",
            },
            {
                "source": "naver",
                "dataset": "domestic_investor_flow",
                "status": "success",
                "completed_at": "2026-07-21T06:21:00.000000Z",
                "error_type": "",
            },
            {
                "source": "kis",
                "dataset": "domestic_orderbook",
                "status": "success",
                "completed_at": "2026-07-21T06:21:00.000000Z",
                "error_type": "",
            },
        ]
    )

    report = build_quality_report(
        candidate_snapshots=[kr, us],
        source_health=source,
        consecutive_zero_ready={"KR": 5, "US": 4},
        generated_at_utc=datetime(2026, 7, 21, 10, 30, tzinfo=timezone.utc),
    )
    alert = render_alert(report)

    assert report["schema"] == "toss_quality_slo.v1"
    assert report["status"] == "degraded"
    assert report["decision_usable"] is False
    assert "KR ready=0 5회 연속" in alert
    assert "US ready=0 4회 연속" in alert
    assert "KIS domestic_investor_flow numeric" in alert
    assert "Naver fallback 활성" in alert
    assert "score·gate·주문 변경 없음" in alert


def test_healthy_report_is_silent_in_alert_mode():
    healthy = {
        "market": "KR",
        "status": "healthy",
        "dependency_fallback_used": False,
        "candidate_count": 2,
        "upstream_executable_count": 2,
        "income_pass_count": 1,
        "ready_count": 1,
        "top_block_reasons": [],
    }
    source = {
        "status": "healthy",
        "primary_failures": [],
        "active_fallbacks": [],
        "coverage_gaps": [],
    }
    report = build_quality_report(
        candidate_snapshots=[healthy],
        source_health=source,
        consecutive_zero_ready={"KR": 0, "US": 0},
        generated_at_utc=datetime(2026, 7, 21, 10, 30, tzinfo=timezone.utc),
    )

    assert report["status"] == "healthy"
    assert render_alert(report) == ""


def test_no_signal_does_not_alert_from_historical_zero_ready_cohorts():
    no_signal = {
        "market": "KR",
        "status": "no_signal",
        "dependency_fallback_used": False,
        "candidate_count": 10,
        "upstream_executable_count": 0,
        "income_pass_count": 0,
        "ready_count": 0,
        "top_block_reasons": [],
    }
    source = {
        "status": "healthy",
        "primary_failures": [],
        "active_fallbacks": [],
        "coverage_gaps": [],
    }

    report = build_quality_report(
        candidate_snapshots=[no_signal],
        source_health=source,
        consecutive_zero_ready={"KR": 999, "US": 0},
        generated_at_utc=datetime(2026, 7, 21, 10, 30, tzinfo=timezone.utc),
    )

    assert report["status"] == "healthy"
    assert render_alert(report) == ""


def test_watchdog_combines_served_get_and_read_only_databases(tmp_path):
    source_path = tmp_path / "source_observations_v2.db"
    source_store = SourceObservationStoreV2(source_path)
    completed = datetime(2026, 7, 21, 6, 21, tzinfo=timezone.utc)
    for source, status, error in (
        ("kis", "failed", "numeric"),
        ("naver", "success", ""),
    ):
        source_store.record_collection_run(
            source=source,
            dataset="domestic_investor_flow",
            run_id=f"{source}-investor-1",
            started_at=completed,
            completed_at=completed,
            status=status,
            rows_seen=1,
            rows_inserted=1 if status == "success" else 0,
            rows_duplicate=0,
            rows_skipped=0,
            rows_invalid=0 if status == "success" else 1,
            error_type=error,
        )
    source_store.record_collection_run(
        source="kis",
        dataset="domestic_orderbook",
        run_id="kis-orderbook-1",
        started_at=completed,
        completed_at=completed,
        status="success",
        rows_seen=1,
        rows_inserted=1,
        rows_duplicate=0,
        rows_skipped=0,
        rows_invalid=0,
        error_type="",
    )

    shadow_path = tmp_path / "shadow_measurements.db"
    shadow_store = ShadowMeasurementStore(
        shadow_path,
        now_fn=lambda: datetime(2026, 7, 21, 23, 0, tzinfo=timezone.utc),
    )
    for sequence in range(3):
        _append_shadow_cohort(
            shadow_store,
            market="KR",
            decided_at=datetime(2026, 7, 21, 1 + sequence, tzinfo=timezone.utc),
            ready=(False,),
            sequence=sequence,
        )
    calls = []

    def fetch_json(url: str):
        calls.append(url)
        market = "US" if "market=US" in url else "KR"
        return _payload(market=market)

    report = run_quality_watchdog(
        base_url="http://127.0.0.1:8787",
        source_db=source_path,
        shadow_db=shadow_path,
        fetch_json=fetch_json,
        clock=lambda: datetime(2026, 7, 21, 10, 30, tzinfo=timezone.utc),
    )

    assert calls == [
        "http://127.0.0.1:8787/api/toss/buy-candidates?limit=20&market=KR",
        "http://127.0.0.1:8787/api/toss/buy-candidates?limit=20&market=US",
    ]
    assert report["status"] == "degraded"
    assert report["consecutive_zero_ready"] == {"KR": 3, "US": 0}


def test_watchdog_rejects_non_loopback_url_before_fetch(tmp_path):
    calls = []
    with pytest.raises(ValueError, match="base_url_invalid"):
        run_quality_watchdog(
            base_url="https://example.com",
            source_db=tmp_path / "missing-source.db",
            shadow_db=tmp_path / "missing-shadow.db",
            fetch_json=lambda url: calls.append(url),
            clock=lambda: datetime(2026, 7, 21, 10, 30, tzinfo=timezone.utc),
        )
    assert calls == []
