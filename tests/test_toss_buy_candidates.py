"""tests/test_toss_buy_candidates.py

/api/toss/buy-candidates는 기존 삼성/RIA/관심 추천(predictions DB)을 재사용하지
않고, core.discovery_candidates의 신규 발굴 후보 중 토스 소액 조건을 통과한
후보만 노출한다. items가 0이어도 excluded에 '기존 후보 제외' + '신규 스캔 탈락
이유'가 함께 담긴다.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import dashboard_data as dd
from core import discovery_candidates as disc
from core.discovery_candidates import (
    DiscoverySections,
    NewCandidate,
    RejectedCandidate,
)


def _new_cand(ticker, name, market="KR", price=30_000, score=70):
    target = round(price * 1.05, 2)
    stop = round(price * 0.94, 2)
    return NewCandidate(
        ticker=ticker, name=name, market=market, price=float(price),
        score=score, idea=f"{name} 신규 발굴 아이디어",
        reasons=("거래대금 충분", "수급 개선"),
        target_price=target, stop_loss=stop, risk_reward=1.8,
        change_pct=2.0, tags=("거래량급증",),
    )


def _strong_cand(ticker, name, price):
    candidate = _new_cand(ticker, name, price=price, score=88)
    return candidate.__class__(
        **{
            **candidate.__dict__,
            "target_price": round(price * 1.16, 2),
            "stop_loss": round(price * 0.94, 2),
            "risk_reward": 2.6,
        }
    )


def _sections(new=(), rejected=(), market="KR"):
    return DiscoverySections(
        holdings_management=(),
        watchlist_reeval=(),
        new_discovery=tuple(new),
        new_rejected=tuple(rejected),
        market=market,
    )


def _patch_network(monkeypatch):
    """KIS 실시세 네트워크 호출 차단 (샌드박스에서 재시도 지연으로 타임아웃 유발)."""
    # 라이브 정책 계산은 KIS 시세 조회(~6초+)를 하므로 고정값으로 대체
    from core import toss_live_pilot_policy as tlp
    monkeypatch.setattr(tlp, "compute_toss_live_pilot_policy",
                        lambda *a, **k: {"max_order_krw": 500_000})
    # 후보별 Toss/KIS 교차검증도 KIS 실시세 조회를 하므로 차단
    monkeypatch.setattr(dd, "_cross_check_price_quality",
                        lambda sym, cur=None: {"quality": "unknown", "checks": []})
    # Toss 환율(실네트워크+토큰) / novelty(memory DB, 봇이 실시간 갱신) 차단 —
    # 살아있는 전역 상태가 score/사이징에 스며들면 기대손익 순서가 비결정적이 된다
    from core import toss_client as tc
    monkeypatch.setattr(tc, "get_exchange_rate",
                        lambda base="USD", quote="KRW": {"rate": 1500.0})
    monkeypatch.setattr(disc, "recent_recommended_tickers", lambda *a, **k: set())
    # 기본 테스트는 계좌 현금과 read-only snapshot이 모두 fresh인 상태로 고정한다.
    # 운영 repo에 우연히 존재하는 snapshot 파일에 테스트 결과가 의존하면
    # 같은 commit도 GCP/live와 격리 worktree에서 baseline이 달라진다.
    monkeypatch.setattr("core.toss_readonly_snapshot.load_snapshot", lambda: {
        "ok": True,
        "status": "fresh",
        "usable_for_decisions": True,
    })
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
        "snapshot_status": "fresh",
        "snapshot_usable_for_decisions": True,
        "cash": {"krw": 10_000_000, "krw_native": 10_000_000, "usd": 10_000.0, "usd_krw": 15_000_000},
        "holdings_count": 0,
    })


def _patch_sections(monkeypatch, sections):
    monkeypatch.setattr(dd, "_cache", {}, raising=False)
    # 테스트에서 실제 유니버스 병렬 시세 스캔(네트워크, 최대 15초/호출) 차단
    monkeypatch.setattr(disc, "_fallback_universe_candidates", lambda markets: [])
    _patch_network(monkeypatch)
    monkeypatch.setattr(
        disc, "build_discovery_sections",
        lambda *a, **k: sections,
    )


def _patch_positive_lifecycle_income(monkeypatch):
    """수학 모델과 무관한 품질/cap/리밸런싱 테스트용 양수 실행 EV."""
    from core import toss_income_strategy as tis

    def positive(candidate, **kwargs):
        entry = float(candidate.get("limit_price") or candidate.get("price") or 0)
        target = float(candidate.get("target_price") or entry)
        estimated = float(candidate.get("estimated_amount_krw") or 1)
        decision_expected = max(target - entry, 1.0)
        return {
            "version": "income_v2",
            "expected_pnl_model": "income_exit_cashflow_v2",
            "expected_pnl_scope": "next_realized_exit_only",
            "expected_pnl_krw": -decision_expected,
            "income_edge_ratio": -decision_expected / estimated,
            "decision_expected_pnl_model": "income_exit_lifecycle_v1",
            "decision_expected_pnl_scope": "full_position_threshold_exit",
            "decision_expected_pnl_krw": decision_expected,
            "decision_income_edge_ratio": decision_expected / estimated,
            "income_pass": True,
            "income_grade": "SMALL_INCOME_PASS",
            "income_block_reason": "",
            "income_block_label": "",
        }

    monkeypatch.setattr(tis, "compute_income_edge", positive)


def test_toss_buy_candidates_uses_new_discovery_only(monkeypatch):
    sections = _sections(new=[_new_cand("000111.KS", "신규소액주", price=30_000)])
    _patch_sections(monkeypatch, sections)

    result = dd.toss_buy_candidates_data(range_="today")

    assert result["count"] == 1
    item = result["items"][0]
    assert item["symbol"] == "000111.KS"
    assert item["candidate_scope"] == "new_discovery"
    assert item["read_only"] is True


def test_toss_buy_candidate_contract_is_autonomous_and_account_scoped(monkeypatch):
    sections = _sections(new=[_new_cand("000111.KS", "신규소액주", price=30_000)])
    _patch_sections(monkeypatch, sections)

    item = dd.toss_buy_candidates_data(range_="today")["items"][0]

    assert item["execution_gate"] == "Hermes PASS + deterministic safety gates"
    assert "승호 최종 승인" not in item["condition"]
    assert "승호 최종 승인" not in item["execution_gate"]
    assert item["broker_execution"] == "Toss AI autonomous live pilot"


def test_toss_buy_candidate_cache_is_shared_across_requested_limits(monkeypatch):
    sections = _sections(new=[
        _new_cand("000111.KS", "후보1", price=30_000),
        _new_cand("000112.KS", "후보2", price=31_000),
    ])
    _patch_sections(monkeypatch, sections)
    calls = {"count": 0}

    def counted_sections(*args, **kwargs):
        calls["count"] += 1
        return sections

    monkeypatch.setattr(disc, "build_discovery_sections", counted_sections)

    first = dd.toss_buy_candidates_data(range_="today", limit=1, market="ALL")
    second = dd.toss_buy_candidates_data(range_="today", limit=80, market="ALL")

    assert calls["count"] == 1
    assert len(first["items"]) == 1
    assert len(second["items"]) == 2


def test_fast_fallback_candidates_do_not_repeat_same_kis_cross_check(monkeypatch):
    sections = _sections(new=[_new_cand("000111.KS", "신규소액주", price=30_000)])
    _patch_sections(monkeypatch, sections)

    def forbidden_cross_check(*args, **kwargs):
        raise AssertionError("same-source KIS recheck must not run on fast fallback")

    monkeypatch.setattr(dd, "_cross_check_price_quality", forbidden_cross_check)
    item = dd.toss_buy_candidates_data(range_="today", market="ALL")["items"][0]

    assert item["data_quality"]["same_source_only"] is True
    assert item["data_quality"]["quality"] == "unknown"


def test_toss_buy_candidates_excludes_user_blocked_krafton(monkeypatch):
    sections = _sections(new=[
        _new_cand("259960.KS", "크래프톤", price=300_000),
        _new_cand("000111.KS", "소액주", price=30_000),
    ])
    _patch_sections(monkeypatch, sections)

    result = dd.toss_buy_candidates_data(range_="today")

    symbols = {i["symbol"] for i in result["items"]}
    assert "259960.KS" not in symbols
    assert "000111.KS" in symbols
    excluded = [e for e in result["excluded"] if e.get("ticker") == "259960.KS" or e.get("symbol") == "259960.KS"]
    assert excluded
    assert "사용자 제외" in excluded[0]["reason"]
    assert "259960.KS" in result["scan_summary"]["user_blocked_buy_symbols"]


def test_toss_buy_candidates_excludes_current_toss_holdings(monkeypatch):
    sections = _sections(new=[
        _new_cand("316140.KS", "우리금융", price=31_000),
        _new_cand("000111.KS", "신규소액주", price=30_000),
    ])
    _patch_sections(monkeypatch, sections)
    held_row = {"symbol": "316140", "name": "우리금융지주", "quantity": 14, "last_price": 31_200}
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {
        "316140": held_row,
        "316140.KS": held_row,
        "316140.KQ": held_row,
    })

    result = dd.toss_buy_candidates_data(range_="today")

    symbols = {i["symbol"] for i in result["items"]}
    assert "316140.KS" not in symbols
    assert "000111.KS" in symbols
    excluded = [e for e in result["excluded"] if e.get("symbol") == "316140.KS"]
    assert excluded
    assert excluded[0]["scope"] == "already_held_toss_position"
    assert excluded[0]["quantity"] == 14
    assert result["scan_summary"]["toss_held_excluded_count"] == 1

def test_toss_buy_candidates_excludes_reuse_and_scan_rejects_when_zero(monkeypatch):
    # 신규 통과 0개 + 탈락 사유 존재
    sections = _sections(
        new=[],
        rejected=[RejectedCandidate("999.KS", "급등탈락", "당일 급등 추격 위험")],
    )
    _patch_sections(monkeypatch, sections)

    result = dd.toss_buy_candidates_data(range_="today")

    assert result["count"] == 0
    reasons = " ".join(e.get("reason", "") for e in result["excluded"])
    # 기존 후보 재사용 금지 + 신규 스캔 탈락 사유 둘 다 포함
    assert "재사용" in reasons
    assert "급등" in reasons


def test_toss_buy_candidates_over_limit_shown_not_executable(monkeypatch):
    # 한도 초과 KR 후보도 items에 포함하되 즉시 실행 불가로 표시한다.
    # 1회 한도 50만원 초과(60만원) 후보.
    sections = _sections(new=[_strong_cand("222.KS", "고가주", 600_000)])
    _patch_sections(monkeypatch, sections)

    result = dd.toss_buy_candidates_data(range_="today")

    assert result["count"] >= 1
    item = next(i for i in result["items"] if i["symbol"] == "222.KS")
    assert item["executable_now"] is False
    assert item["limit_exceeded"] is True
    assert item["execution_status"] == "limit_exceeded"
    assert "한도" in item["block_reason"]
    assert item.get("suggested_action")
    # 한도 초과는 excluded(toss_soak)로 빠지지 않는다
    assert "222.KS" not in {e.get("ticker") for e in result["excluded"]}
    # scan_summary에 한도 초과 카운트 분리 표기
    assert result["scan_summary"]["limit_exceeded_count"] >= 1


def test_toss_buy_candidates_price_field_populated(monkeypatch):
    # price 필드는 라이브 시세(limit_price)와 동일하게 채워져야 한다 (None 금지).
    sections = _sections(new=[_new_cand("000111.KS", "소액주", price=30_000)])
    _patch_sections(monkeypatch, sections)

    result = dd.toss_buy_candidates_data(range_="today")

    item = next(i for i in result["items"] if i["symbol"] == "000111.KS")
    assert item["price"] is not None
    assert item["price"] == item["limit_price"] == 30_000.0


def test_toss_buy_candidates_within_limit_executable(monkeypatch):
    # 한도 이내이고 실행 품질도 충족한 후보는 executable_now=True.
    sections = _sections(new=[_strong_cand("000111.KS", "소액주", 30_000)])
    _patch_sections(monkeypatch, sections)

    result = dd.toss_buy_candidates_data(range_="today")

    item = next(i for i in result["items"] if i["symbol"] == "000111.KS")
    assert item["executable_now"] is True
    assert item["limit_exceeded"] is False
    assert item["execution_status"] == "executable"


def test_toss_buy_candidates_154700_executable(monkeypatch):
    # 원익IPS급 154,700원 + 실행 품질 충족 후보는 1회 한도 이내라 실행 가능.
    sections = _sections(new=[_strong_cand("240810.KS", "원익IPS", 154_700)])
    _patch_sections(monkeypatch, sections)

    result = dd.toss_buy_candidates_data(range_="today")

    item = next(i for i in result["items"] if i["symbol"] == "240810.KS")
    assert item["executable_now"] is True
    assert item["limit_exceeded"] is False
    assert item["execution_status"] == "executable"


def test_toss_buy_candidates_us_excluded(monkeypatch):
    sections = _sections(
        new=[_new_cand("XYZ", "미국주", market="US", price=50.0)], market="US")
    _patch_sections(monkeypatch, sections)

    result = dd.toss_buy_candidates_data(range_="today")

    assert "XYZ" not in {i["symbol"] for i in result["items"]}


def test_toss_buy_candidates_returns_scan_summary(monkeypatch):
    # pandas 미설치 + 네트워크 없는 경량 시세 → 실제 universe fallback 스캔
    monkeypatch.setattr(dd, "_cache", {}, raising=False)
    _patch_network(monkeypatch)
    monkeypatch.setattr(disc, "_pandas_available", lambda: False)
    monkeypatch.setattr(
        disc, "_light_quote",
        lambda t, m: ({
            "ticker": t, "name": disc._name_for(t), "market": "KR",
            "price": 40_000.0, "change_pct": 2.0, "ret_20d": 9.0,
            "ret_60d": 20.0, "rsi": 58.0, "vol_surge": 2.1,
            "pct_from_52w_high": -4.0, "volume_value": 6e10,
            "source": "유니버스(fallback)", "tags": ("유니버스",),
            "has_catalyst": True,
        } if m == "KR" else None),
    )

    result = dd.toss_buy_candidates_data(range_="today")

    assert result.get("scan_summary"), "scan_summary 누락"
    s = result["scan_summary"]
    assert s["dependency_fallback_used"] is True
    assert s["universe_count"] > 0
    for k in ("scanned_count", "pass_count", "reject_count", "top_reject_reasons"):
        assert k in s


def test_toss_buy_candidates_scan_failure_has_reasons(monkeypatch):
    monkeypatch.setattr(dd, "_cache", {}, raising=False)
    _patch_network(monkeypatch)
    monkeypatch.setattr(disc, "_pandas_available", lambda: False)
    monkeypatch.setattr(disc, "_light_quote", lambda t, m: None)

    result = dd.toss_buy_candidates_data(range_="today")

    # items=0이어도 excluded가 reuse_blocked 하나만은 아니어야 한다
    scopes = {e.get("scope") for e in result["excluded"]}
    assert scopes != {"reuse_blocked"}
    assert result["scan_summary"]["scanned_count"] == 0



def test_toss_buy_candidates_cache_exception_returns_canonical_empty_v3(monkeypatch):
    def fail_cache(*_args, **_kwargs):
        raise RuntimeError("synthetic cache failure")

    monkeypatch.setattr(dd, "_cached", fail_cache)

    result = dd.toss_buy_candidates_data(range_="today")

    assert result["schema"] == "toss_buy_candidates.v3.dual_income_ev"
    assert result["scan_summary"]["income_gate_version"] == "income_v2_dual_ev"
    assert result["items"] == []
    assert result["excluded"] == []


@pytest.mark.parametrize(
    "cached",
    [
        {
            "schema": "toss_buy_candidates.v2.stock_agent_ready",
            "scan_summary": {"income_gate_version": "income_v1"},
            "items": [{"stock_agent_ready": True}],
            "excluded": [],
        },
        {
            "schema": "toss_buy_candidates.v3.dual_income_ev",
            "scan_summary": {"income_gate_version": "income_v2_dual_ev"},
            "items": ["not-a-dict"],
            "excluded": [],
        },
    ],
)
def test_toss_buy_candidates_rejects_stale_or_corrupt_cached_authority(
    monkeypatch,
    cached,
):
    monkeypatch.setattr(dd, "_cached", lambda *_args, **_kwargs: cached)

    result = dd.toss_buy_candidates_data(range_="today")

    assert result["schema"] == "toss_buy_candidates.v3.dual_income_ev"
    assert result["scan_summary"]["income_gate_version"] == "income_v2_dual_ev"
    assert result["items"] == []
    assert result["excluded"] == []


def test_toss_buy_candidates_stock_agent_review_fields(monkeypatch):
    # Stock-Agent가 PASS/HOLD/BLOCK을 판단할 수 있도록 주문표 핵심 필드를 명시한다.
    sections = _sections(new=[_new_cand("000111.KS", "소액주", price=30_000)])
    _patch_sections(monkeypatch, sections)

    result = dd.toss_buy_candidates_data(range_="today")

    assert result["schema"] == "toss_buy_candidates.v3.dual_income_ev"
    assert result["scan_summary"]["income_gate_version"] == "income_v2_dual_ev"
    item = next(i for i in result["items"] if i["symbol"] == "000111.KS")
    assert item["account"] == "토스 AI"
    assert item["account_type"] == "토스 AI"
    assert item["order_type"] == "LIMIT"
    assert item["current_price"] == 30_000.0
    assert item["entry_price"] == 30_000.0
    assert item["limit_price"] == 30_000.0
    assert item["quantity"] == 8
    assert item["estimated_amount_krw"] == 240_000.0
    assert item["quantity_source"] == "confidence_rr_stop_sizing"
    assert item["position_budget_krw"] == 250_000.0
    assert item["position_sizing"]["score"] == 70.0
    assert item["position_sizing"]["risk_reward"] == 1.8
    assert item["position_sizing"]["stop_risk_pct"] == 6.0
    assert item["original_stop_loss"] == 28_200.0
    assert round(item["stop_loss"], 1) == 28_740.0
    assert item["target_price"] == 31_500.0
    assert item["current_vs_limit_gap_pct"] == 0.0
    assert "즉시체결" in item["fill_risk_note"]
    assert item["condition"]
    assert item["execution_gate"] == "Hermes PASS + deterministic safety gates"
    assert item["broker_execution"] == "Toss AI autonomous live pilot"
    assert item["read_only_notice"].startswith("GET-only")
    assert item["missing_fields"] == []
    assert item["income_strategy"]["income_pass"] is False
    assert item["stock_agent_ready"] is False
    assert "손익비" in item["block_reason"]


def test_toss_buy_candidates_reports_systemic_income_liveness_regression(monkeypatch):
    sections = _sections(new=[_strong_cand("000112.KS", "운영분포후보", price=100_000)])
    _patch_sections(monkeypatch, sections)

    result = dd.toss_buy_candidates_data(range_="today")
    summary = result["scan_summary"]

    assert summary["income_liveness_version"] == "income_liveness_v1"
    assert summary["upstream_executable_count"] > 0
    assert summary["income_ready_count"] == 0
    assert summary["income_liveness_status"] == "degraded"
    diagnosis = summary["income_liveness_diagnosis"]
    assert diagnosis["reason"] == "upstream_executable_but_no_income_ready"
    assert diagnosis["top_income_block_reasons"]
    assert diagnosis["top_income_block_reasons"][0]["count"] >= 1


def test_income_liveness_diagnosis_counts_only_upstream_executable_candidates(monkeypatch):
    sections = _sections(new=[
        _strong_cand("000121.KS", "실행후단차단", price=100_000),
        _new_cand("000122.KS", "앞단차단1", price=30_000),
        _new_cand("000123.KS", "앞단차단2", price=31_000),
        _new_cand("000124.KS", "앞단차단3", price=32_000),
    ])
    _patch_sections(monkeypatch, sections)

    summary = dd.toss_buy_candidates_data(range_="today")["scan_summary"]
    diagnosis = summary["income_liveness_diagnosis"]

    assert summary["upstream_executable_count"] == 1
    assert sum(row["count"] for row in diagnosis["top_income_block_reasons"]) == 1
    assert diagnosis["top_income_block_reasons"] == [
        {"reason": "multi_share_lifecycle_unmodeled", "count": 1}
    ]


def test_income_liveness_preserves_multiple_downstream_block_reasons(monkeypatch):
    sections = _sections(new=[
        _strong_cand("000131.KS", "다주수명주기", price=100_000),
        _strong_cand("000132.KS", "일주기대값", price=400_000),
    ])
    _patch_sections(monkeypatch, sections)

    summary = dd.toss_buy_candidates_data(range_="today")["scan_summary"]
    reasons = summary["income_liveness_diagnosis"]["top_income_block_reasons"]

    assert summary["income_gate_eligible_count"] == 2
    assert sum(row["count"] for row in reasons) == 2
    assert {row["reason"] for row in reasons} == {
        "multi_share_lifecycle_unmodeled",
        "expected_pnl_below_threshold",
    }


def test_income_liveness_excludes_candidates_blocked_before_income_gate(monkeypatch):
    sections = _sections(new=[
        _strong_cand("000125.KS", "주문한도초과", price=600_000),
    ])
    _patch_sections(monkeypatch, sections)

    result = dd.toss_buy_candidates_data(range_="today")
    item = result["items"][0]
    summary = result["scan_summary"]

    assert item["decision_bucket"] in {"PASS_EXECUTE", "SMALL_PASS"}
    assert item["limit_exceeded"] is True
    assert summary["upstream_executable_count"] == 0
    assert summary["income_liveness_status"] == "no_signal"
    assert summary["income_liveness_diagnosis"]["reason"] == (
        "no_income_gate_eligible_candidates"
    )


def test_execution_calibration_is_summary_only_and_never_authorizes_buy(monkeypatch):
    from src import toss_execution_calibration as calibration

    sections = _sections(new=[
        _strong_cand("000129.KS", "검증후보", price=100_000),
    ])
    _patch_sections(monkeypatch, sections)
    _patch_positive_lifecycle_income(monkeypatch)

    def calibration_report(sell_price):
        report = calibration.reconstruct_execution_calibration([
            {
                "pilot_id": "tlive_buy-summary",
                "side": "buy",
                "symbol": "005930.KS",
                "quantity": 1,
                "filled_quantity": 1,
                "filled_price": 100_000,
                "estimated_amount_krw": 100_000,
                "broker_order_status": "FILLED",
                "strategy_reason": "auto_pipeline",
                "event_type": "autonomous_live_sent",
                "live_order_sent": 1,
                "adapter_status": "enabled",
                "live_order_allowed": 1,
                "created_at": "2026-07-15T09:00:00+09:00",
            },
            {
                "pilot_id": "tlive_sell-summary",
                "side": "sell",
                "symbol": "005930.KS",
                "quantity": 1,
                "filled_quantity": 1,
                "filled_price": sell_price,
                "estimated_amount_krw": sell_price,
                "broker_order_status": "FILLED",
                "strategy_reason": "position_review_sell",
                "event_type": "autonomous_live_sent",
                "live_order_sent": 1,
                "adapter_status": "enabled",
                "live_order_allowed": 1,
                "created_at": "2026-07-15T10:00:00+09:00",
            },
        ], min_samples=1)
        report.update({
            "status": "ok",
            "source": "read_only_live_pilot_event_ledger",
            "source_window_truncated": False,
            "source_row_limit": 5_000,
            "source_rows_loaded": 2,
            "ledger_reason_conflict_count": 0,
            "ledger_reason_missing_count": 0,
            "ledger_reason_invalid_count": 0,
        })
        return report

    monkeypatch.setattr(
        calibration,
        "load_execution_calibration",
        lambda **kwargs: calibration_report(90_000),
    )
    low_report = dd.toss_buy_candidates_data(range_="today")
    monkeypatch.setattr(dd, "_cache", {}, raising=False)
    monkeypatch.setattr(
        calibration,
        "load_execution_calibration",
        lambda **kwargs: calibration_report(120_000),
    )
    high_report = dd.toss_buy_candidates_data(range_="today")

    assert high_report["items"] == low_report["items"]
    assert high_report["excluded"] == low_report["excluded"]
    assert high_report["count"] == low_report["count"]
    assert high_report["items"][0]["stock_agent_ready"] == low_report["items"][0][
        "stock_agent_ready"
    ]
    report = high_report["scan_summary"]["execution_calibration"]
    assert report["mode"] == "observability_only"
    assert report["decision_usable"] is False
    assert report["decision_block_reason"] == (
        "lifecycle_transition_model_unvalidated"
    )
    assert "outcomes" not in report
    assert "open_positions" not in report


def test_execution_calibration_reconciles_fresh_holdings_without_raw_lot_leak(monkeypatch):
    from src import toss_execution_calibration as calibration

    _patch_sections(monkeypatch, _sections())
    raw = calibration.reconstruct_execution_calibration([
        {
            "pilot_id": "tlive_buy-1",
            "side": "buy",
            "symbol": "005930.KS",
            "quantity": 4,
            "filled_quantity": 4,
            "filled_price": 100_000,
            "estimated_amount_krw": 400_000,
            "broker_order_status": "FILLED",
            "strategy_reason": "auto_pipeline",
            "event_type": "autonomous_live_sent",
            "live_order_sent": 1,
            "adapter_status": "enabled",
            "live_order_allowed": 1,
            "created_at": "2026-07-15T09:00:00+09:00",
        },
    ])
    raw.update({
        "status": "ok",
        "source": "read_only_live_pilot_event_ledger",
        "source_window_truncated": False,
        "source_row_limit": 5_000,
        "source_rows_loaded": 1,
        "ledger_reason_conflict_count": 0,
        "ledger_reason_missing_count": 0,
        "ledger_reason_invalid_count": 0,
    })
    monkeypatch.setattr(calibration, "load_execution_calibration", lambda **kwargs: raw)
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
        "snapshot_status": "fresh",
        "snapshot_usable_for_decisions": True,
        "cash": {"krw": 10_000_000, "krw_native": 10_000_000},
        "holdings_count": 1,
        "holdings_items": [{"symbol": "005930.KS", "quantity": 2}],
    })

    report = dd.toss_buy_candidates_data(range_="today")["scan_summary"][
        "execution_calibration"
    ]

    assert report["status"] == "partial"
    assert report["holdings_reconciliation_status"] == "incomplete"
    assert report["open_quantity_exceeds_holdings"] == 2
    assert "open_lots_exceed_holdings" in report["lineage_reasons"]
    assert report["evidence_sufficient"] is False
    assert "open_positions" not in report
    assert "outcomes" not in report


def test_income_liveness_separates_post_income_downstream_block(monkeypatch):
    sections = _sections(new=[
        _strong_cand("000126.KS", "수입통과후차단", price=100_000),
    ])
    _patch_sections(monkeypatch, sections)
    _patch_positive_lifecycle_income(monkeypatch)

    def block_after_income(item, _scores):
        item["stock_agent_ready"] = False
        item["ai_berkshire_buy_block"] = True
        item["execution_status"] = "ai_berkshire_buy_block"

    monkeypatch.setattr(dd, "_apply_ai_berkshire_buy_gate", block_after_income)

    summary = dd.toss_buy_candidates_data(range_="today")["scan_summary"]

    assert summary["income_gate_eligible_count"] == 1
    assert summary["income_pass_count"] == 1
    assert summary["income_ready_count"] == 0
    assert summary["income_liveness_status"] == "downstream_blocked"
    assert summary["income_liveness_diagnosis"]["reason"] == (
        "income_pass_but_no_final_ready"
    )


def test_toss_buy_candidates_empty_cache_fallback_preserves_v3_schema(monkeypatch):
    monkeypatch.setattr(dd, "_cached", lambda *args, **kwargs: {})

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert result["schema"] == "toss_buy_candidates.v3.dual_income_ev"
    assert result["scan_summary"]["income_gate_version"] == "income_v2_dual_ev"
    assert result["scan_summary"]["income_liveness_status"] == "unavailable"
    assert result["scan_summary"]["income_liveness_diagnosis"]["reason"] == (
        "candidate_cache_payload_unavailable"
    )
    assert result["scan_summary"]["execution_calibration"]["decision_usable"] is False
    assert result["items"] == []
    assert result["excluded"] == []
    assert result["requested_limit"] == 3


def test_toss_buy_candidates_quarantines_stale_cached_contract_versions(monkeypatch):
    monkeypatch.setattr(dd, "_cached", lambda *args, **kwargs: {
        "schema": "toss_buy_candidates.v2.stock_agent_ready",
        "scan_summary": {"income_gate_version": "income_v1"},
        "items": [{"stock_agent_ready": True}],
        "excluded": [],
    })

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert result["schema"] == "toss_buy_candidates.v3.dual_income_ev"
    assert result["scan_summary"]["income_gate_version"] == "income_v2_dual_ev"
    assert result["items"] == []
    assert result["excluded"] == []


def test_toss_buy_candidates_quarantines_unsafe_cached_calibration(monkeypatch):
    monkeypatch.setattr(dd, "_cached", lambda *args, **kwargs: {
        "schema": "toss_buy_candidates.v3.dual_income_ev",
        "scan_summary": {
            "income_gate_version": "income_v2_dual_ev",
            "income_liveness_version": "income_liveness_v1",
            "income_liveness_status": "healthy",
            "execution_calibration": {
                "schema": "toss_execution_calibration.v1",
                "mode": "observability_only",
                "decision_usable": True,
                "decision_block_reason": "lifecycle_transition_model_unvalidated",
                "attribution_verified": False,
                "outcomes": [{"buy_pilot_id": "raw-secret"}],
                "open_positions": [{"symbol": "RAW", "quantity": 1}],
            },
        },
        "items": [{"stock_agent_ready": True}],
        "excluded": [],
    })

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)
    calibration = result["scan_summary"]["execution_calibration"]

    assert result["scan_summary"]["cache_contract_valid"] is False
    assert result["items"] == []
    assert calibration["decision_usable"] is False
    assert calibration["attribution_verified"] is False
    assert calibration["evidence_sufficient"] is False
    assert "outcomes" not in calibration
    assert "open_positions" not in calibration


class _CalibrationListSubclass(list):
    pass


class _CalibrationStrSubclass(str):
    pass


class _DeepcopyBomb:
    def __deepcopy__(self, memo):
        raise AssertionError("untrusted cache value must not be deep-copied")


class _BoolListSubclass(list):
    bool_calls = 0

    def __bool__(self):
        type(self).bool_calls += 1
        return True

    def __iter__(self):
        raise AssertionError("untrusted list subclass must not be iterated")


class _HostileKey(str):
    armed = False
    hash_calls = 0
    eq_calls = 0

    def __hash__(self):
        if type(self).armed:
            type(self).hash_calls += 1
        return str.__hash__(self)

    def __eq__(self, other):
        if type(self).armed:
            type(self).eq_calls += 1
        return str.__eq__(self, other)


def _valid_cached_execution_calibration():
    return {
        "schema": "toss_execution_calibration.v1",
        "status": "ok",
        "mode": "observability_only",
        "decision_usable": False,
        "decision_block_reason": "lifecycle_transition_model_unvalidated",
        "attribution_model": "symbol_fifo_v1",
        "attribution_verified": False,
        "cost_model": "decision_buffer_v1_not_broker_statement",
        "completed_count": 1,
        "wins": 1,
        "losses": 0,
        "flats": 0,
        "win_rate": 1.0,
        "avg_win_pct": 1.0,
        "avg_loss_pct": None,
        "mean_net_return_pct": 1.0,
        "minimum_sample_reached": False,
        "sample_sufficient": False,
        "evidence_sufficient": False,
        "min_samples": 20,
        "lineage_status": "complete",
        "lineage_reasons": [],
        "unmatched_sell_fill_count": 0,
        "unmatched_sell_quantity": 0,
        "symbol_alias_conflict_count": 0,
        "ambiguous_fill_count": 0,
        "holdings_reconciliation_status": "complete",
        "holdings_symbol_alias_conflict_count": 0,
        "open_quantity_exceeds_holdings": 0,
        "open_lot_count": 0,
        "open_quantity": 0,
        "ignored_count": 0,
        "quarantined_fill_count": 0,
        "invalid_fill_count": 0,
        "conflict_count": 0,
        "source": "read_only_live_pilot_event_ledger",
        "source_window_truncated": False,
        "source_row_limit": 5_000,
        "source_rows_loaded": 2,
        "ledger_reason_conflict_count": 0,
        "ledger_reason_missing_count": 0,
        "ledger_reason_invalid_count": 0,
    }


def _cached_candidate_payload(calibration):
    return {
        "schema": "toss_buy_candidates.v3.dual_income_ev",
        "scan_summary": {
            "income_gate_version": "income_v2_dual_ev",
            "income_liveness_version": "income_liveness_v1",
            "income_liveness_status": "idle",
            "raw_income_pass_count": 0,
            "income_pass_count": 0,
            "income_block_count": 0,
            "income_gate_eligible_count": 0,
            "upstream_executable_count": 0,
            "income_ready_count": 0,
            "income_liveness_diagnosis": None,
            "execution_calibration": calibration,
        },
        "items": [],
        "excluded": [],
        "count": 0,
        "excluded_count": 0,
    }


def _proofed_ready_cache_item():
    from core import toss_quality_gate as qg

    item = {
        "symbol": "091180.KS",
        "side": "buy",
        "market": "KR",
        "currency": "KRW",
        "quantity": 10,
        "limit_price": 30_000,
        "stop_loss": 28_000,
        "target_price": 34_000,
        "risk_reward": 2.0,
        "change_pct": 2.0,
        "blocking_risk_flags": [],
        "decision_bucket": "PASS_EXECUTE",
        "decision_reason": "quality pass",
        "quality_score": 75,
        "quality_finalized": True,
        "income_execution_contract_valid": True,
        "missing_fields": [],
        "limit_exceeded": False,
        "execution_status": "ready",
        "executable_now": True,
        "stock_agent_ready": True,
        "income_strategy": {
            "version": "income_v2_dual_ev",
            "income_pass": True,
            "decision_expected_pnl_model": "income_exit_lifecycle_v1",
            "decision_expected_pnl_scope": "full_position_threshold_exit",
            "expected_pnl_krw": 5000.0,
            "income_edge_ratio": 2.0,
            "decision_expected_pnl_krw": 5000.0,
            "decision_income_edge_ratio": 2.0,
            "planned_entry_price": 30_000,
            "planned_stop_loss": 28_000,
            "planned_target_price": 34_000,
            "planned_quantity": 10,
            "decision_position_size": 10,
            "income_expected_pnl_source": "decision_ev",
            "income_edge_ratio_source": "decision_edge",
            "decision_contract_version": "income_decision_v1",
            "decision_contract_frozen_at": "2026-07-20T00:00:00+00:00",
        },
        "quality_breakdown": {
            "score_total": 75,
            "score_momentum": 18,
            "score_liquidity": 17,
            "score_risk_reward": 16,
            "score_reliability": 12,
            "score_market_regime": 12,
            "score_supply_demand": 0,
            "penalty_overheat": 0,
            "penalty_duplicate": 0,
            "penalty_event_risk": 0,
            "rr_ratio": 2.0,
            "regime": "중립",
        },
    }
    breakdown = item["quality_breakdown"]
    breakdown.update({
        "decision_bucket": item["decision_bucket"],
        "decision_reason": item["decision_reason"],
        "score_symbol": item["symbol"],
        "score_side": item["side"],
        "decision_change_pct": item["change_pct"],
        "decision_days_to_earnings": -1,
        "decision_has_stop": True,
        "decision_has_target": True,
        "decision_blocking_risk_flags": [],
        "decision_origin_bucket": item["decision_bucket"],
        "decision_origin_reason": item["decision_reason"],
        "score_schema_version": qg.QUALITY_SCORE_SCHEMA_VERSION,
    })
    weight_hash = qg._weight_profile_hash()
    breakdown["weight_profile_hash"] = weight_hash
    breakdown["score_breakdown_sha256"] = qg._score_breakdown_hash(
        breakdown,
        schema_version=qg.QUALITY_SCORE_SCHEMA_VERSION,
        weight_hash=weight_hash,
    )
    assert breakdown["score_breakdown_sha256"]
    assert qg.attach_quality_proof(item) is True
    return item


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("evidence_sufficient", True),
        ("completed_count", "1"),
        ("lineage_reasons", {}),
        ("lineage_reasons", _CalibrationListSubclass()),
        ("lineage_reasons", [_CalibrationStrSubclass("unmatched_sell_fill")]),
        ("mean_net_return_pct", float("nan")),
        ("win_rate", 10**400),
        ("avg_win_pct", 10**400),
        ("status", _CalibrationStrSubclass("ok")),
        ("source_row_limit", 10**400),
        ("ledger_reason_missing_count", 1),
        ("ambiguous_fill_count", 1),
        ("ledger_reason_invalid_count", 1),
        ("quarantined_fill_count", 1),
        ("source_rows_loaded", 1),
        ("avg_loss_pct", -77.0),
        ("mean_net_return_pct", 0.5),
        ("wins", 2),
    ],
)
def test_toss_buy_candidates_quarantines_malformed_nested_calibration(
    monkeypatch,
    field,
    bad_value,
):
    calibration = _valid_cached_execution_calibration()
    calibration[field] = bad_value
    monkeypatch.setattr(
        dd,
        "_cached",
        lambda *args, **kwargs: _cached_candidate_payload(calibration),
    )

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)
    report = result["scan_summary"]["execution_calibration"]

    assert result["scan_summary"]["cache_contract_valid"] is False
    assert result["items"] == []
    assert report["decision_usable"] is False
    assert report["attribution_verified"] is False
    assert report["evidence_sufficient"] is False


@pytest.mark.parametrize(
    "updates",
    [
        {
            "status": "partial",
            "lineage_status": "incomplete",
            "lineage_reasons": ["pilot_payload_conflict"],
            "source_rows_loaded": 3,
            "conflict_count": 1,
            "quarantined_fill_count": 1,
            "ignored_count": 1,
        },
        {
            "status": "partial",
            "lineage_status": "incomplete",
            "lineage_reasons": ["krx_symbol_alias_conflict"],
            "source_rows_loaded": 3,
            "symbol_alias_conflict_count": 1,
            "quarantined_fill_count": 1,
            "ignored_count": 1,
        },
        {
            "status": "partial",
            "lineage_status": "incomplete",
            "lineage_reasons": [
                "fill_contract_invalid",
                "fill_order_ambiguous",
            ],
            "source_rows_loaded": 3,
            "ambiguous_fill_count": 1,
            "invalid_fill_count": 1,
            "quarantined_fill_count": 1,
            "ignored_count": 1,
        },
        {
            "status": "partial",
            "lineage_status": "incomplete",
            "lineage_reasons": ["source_window_truncated"],
            "source_window_truncated": True,
        },
        {
            "status": "unavailable",
            "lineage_status": "incomplete",
            "lineage_reasons": [
                "execution_calibration_source_unavailable",
                "holdings_reconciliation_unavailable",
            ],
            "holdings_reconciliation_status": "unavailable",
            "reason": "execution_calibration_source_unavailable",
        },
        {
            "status": "partial",
            "lineage_status": "incomplete",
            "lineage_reasons": ["fill_contract_invalid"],
            "invalid_fill_count": 3,
            "quarantined_fill_count": 3,
        },
        {
            "ignored_count": 3,
        },
        {
            "status": "partial",
            "lineage_status": "incomplete",
            "lineage_reasons": ["fill_order_ambiguous"],
        },
        {
            "status": "partial",
            "lineage_status": "incomplete",
            "lineage_reasons": ["ledger_reason_invalid"],
        },
        {
            "wins": 0,
            "losses": 1,
            "win_rate": 0.0,
            "avg_win_pct": 1.0,
            "avg_loss_pct": -1.0,
            "mean_net_return_pct": -1.0,
        },
        {
            "avg_win_pct": 0.0,
            "mean_net_return_pct": 0.0,
        },
        {
            "wins": 0,
            "losses": 1,
            "win_rate": 0.0,
            "avg_win_pct": None,
            "avg_loss_pct": -0.0,
            "mean_net_return_pct": -0.0,
        },
    ],
)
def test_toss_buy_candidates_quarantines_inconsistent_cached_pnl_lineage(
    monkeypatch,
    updates,
):
    calibration = _valid_cached_execution_calibration()
    calibration.update(updates)
    monkeypatch.setattr(
        dd,
        "_cached",
        lambda *args, **kwargs: _cached_candidate_payload(calibration),
    )

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert result["scan_summary"]["cache_contract_valid"] is False
    assert result["items"] == []


def test_toss_buy_candidates_accepts_completed_lifecycle_with_oversized_sell_cache(
    monkeypatch,
):
    from src.toss_execution_calibration import reconstruct_execution_calibration

    report = reconstruct_execution_calibration([
        {
            "pilot_id": "tlive_buy-oversized-sell",
            "side": "buy",
            "symbol": "005930.KS",
            "quantity": 1,
            "filled_quantity": 1,
            "filled_price": 100_000,
            "estimated_amount_krw": 100_000,
            "broker_order_status": "FILLED",
            "strategy_reason": "auto_pipeline",
            "event_type": "autonomous_live_sent",
            "live_order_sent": 1,
            "adapter_status": "enabled",
            "live_order_allowed": 1,
            "created_at": "2026-07-15T09:00:00+09:00",
        },
        {
            "pilot_id": "tlive_sell-oversized-sell",
            "side": "sell",
            "symbol": "005930.KS",
            "quantity": 2,
            "filled_quantity": 2,
            "filled_price": 105_000,
            "estimated_amount_krw": 210_000,
            "broker_order_status": "FILLED",
            "strategy_reason": "position_review_sell",
            "event_type": "autonomous_live_sent",
            "live_order_sent": 1,
            "adapter_status": "enabled",
            "live_order_allowed": 1,
            "created_at": "2026-07-15T10:00:00+09:00",
        },
    ])
    calibration = _valid_cached_execution_calibration()
    calibration.update({
        key: value
        for key, value in report.items()
        if key in calibration
    })
    calibration.update({
        "source": "read_only_live_pilot_event_ledger",
        "source_window_truncated": False,
        "source_row_limit": 5_000,
        "source_rows_loaded": 2,
        "ledger_reason_conflict_count": 0,
        "ledger_reason_missing_count": 0,
        "ledger_reason_invalid_count": 0,
    })
    monkeypatch.setattr(
        dd,
        "_cached",
        lambda *args, **kwargs: _cached_candidate_payload(calibration),
    )

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert result["scan_summary"]["cache_contract_valid"] is True
    projected = result["scan_summary"]["execution_calibration"]
    assert projected["completed_count"] == 1
    assert projected["ignored_count"] == 1
    assert projected["unmatched_sell_fill_count"] == 1
    assert projected["unmatched_sell_quantity"] == 1
    assert "unmatched_sell_fill" in projected["lineage_reasons"]


def test_toss_buy_candidates_accepts_canonical_unavailable_calibration_cache(monkeypatch):
    calibration = _valid_cached_execution_calibration()
    zero_fields = (
        "completed_count",
        "wins",
        "losses",
        "flats",
        "unmatched_sell_fill_count",
        "unmatched_sell_quantity",
        "symbol_alias_conflict_count",
        "ambiguous_fill_count",
        "holdings_symbol_alias_conflict_count",
        "open_quantity_exceeds_holdings",
        "open_lot_count",
        "open_quantity",
        "ignored_count",
        "quarantined_fill_count",
        "invalid_fill_count",
        "conflict_count",
        "source_rows_loaded",
        "ledger_reason_conflict_count",
        "ledger_reason_missing_count",
        "ledger_reason_invalid_count",
    )
    calibration.update({field: 0 for field in zero_fields})
    calibration.update({
        "status": "unavailable",
        "lineage_status": "incomplete",
        "lineage_reasons": [
            "execution_calibration_source_unavailable",
            "holdings_reconciliation_unavailable",
        ],
        "holdings_reconciliation_status": "unavailable",
        "reason": "execution_calibration_source_unavailable",
        "win_rate": None,
        "avg_win_pct": None,
        "avg_loss_pct": None,
        "mean_net_return_pct": None,
        "minimum_sample_reached": False,
        "sample_sufficient": False,
        "source_window_truncated": False,
    })
    monkeypatch.setattr(
        dd,
        "_cached",
        lambda *args, **kwargs: _cached_candidate_payload(calibration),
    )

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert result["scan_summary"]["cache_contract_valid"] is True
    assert result["scan_summary"]["execution_calibration"]["status"] == "unavailable"


def test_toss_buy_candidates_accepts_complete_truncated_source_window_cache(monkeypatch):
    calibration = _valid_cached_execution_calibration()
    calibration.update({
        "status": "partial",
        "lineage_status": "incomplete",
        "lineage_reasons": ["source_window_truncated"],
        "source_window_truncated": True,
        "source_rows_loaded": calibration["source_row_limit"],
    })
    monkeypatch.setattr(
        dd,
        "_cached",
        lambda *args, **kwargs: _cached_candidate_payload(calibration),
    )

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert result["scan_summary"]["cache_contract_valid"] is True
    report = result["scan_summary"]["execution_calibration"]
    assert report["source_window_truncated"] is True
    assert report["source_rows_loaded"] == report["source_row_limit"]


def test_toss_buy_candidates_accepts_consistent_conflict_quarantine_cache(monkeypatch):
    calibration = _valid_cached_execution_calibration()
    calibration.update({
        "status": "partial",
        "lineage_status": "incomplete",
        "lineage_reasons": ["pilot_payload_conflict"],
        "source_rows_loaded": 5,
        "conflict_count": 1,
        "quarantined_fill_count": 3,
        "ignored_count": 3,
    })
    monkeypatch.setattr(
        dd,
        "_cached",
        lambda *args, **kwargs: _cached_candidate_payload(calibration),
    )

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert result["scan_summary"]["cache_contract_valid"] is True
    report = result["scan_summary"]["execution_calibration"]
    assert report["conflict_count"] == 1
    assert report["quarantined_fill_count"] == 3
    assert report["lineage_reasons"] == ["pilot_payload_conflict"]


def test_toss_buy_candidates_accepts_consistent_mixed_pnl_cache(monkeypatch):
    calibration = _valid_cached_execution_calibration()
    calibration.update({
        "completed_count": 3,
        "wins": 1,
        "losses": 1,
        "flats": 1,
        "win_rate": 0.3333,
        "avg_win_pct": 3.0,
        "avg_loss_pct": -3.0,
        "mean_net_return_pct": 0.0,
        "source_rows_loaded": 6,
    })
    monkeypatch.setattr(
        dd,
        "_cached",
        lambda *args, **kwargs: _cached_candidate_payload(calibration),
    )

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert result["scan_summary"]["cache_contract_valid"] is True
    assert result["scan_summary"]["execution_calibration"] == calibration


def test_toss_buy_candidates_accepts_exact_safe_nested_calibration(monkeypatch):
    calibration = _valid_cached_execution_calibration()
    monkeypatch.setattr(
        dd,
        "_cached",
        lambda *args, **kwargs: _cached_candidate_payload(calibration),
    )

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert result["scan_summary"]["cache_contract_valid"] is True
    assert result["scan_summary"]["execution_calibration"] == calibration


@pytest.mark.parametrize(
    "raw_field", ["outcomes", "open_positions", "unexpected_field"]
)
def test_toss_buy_candidates_rejects_raw_fields_outside_calibration(
    monkeypatch,
    raw_field,
):
    payload = _cached_candidate_payload(_valid_cached_execution_calibration())
    payload["scan_summary"][raw_field] = [{"raw": "must-not-leak"}]
    monkeypatch.setattr(dd, "_cached", lambda *args, **kwargs: payload)

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert result["scan_summary"]["cache_contract_valid"] is False
    assert raw_field not in result["scan_summary"]
    assert result["items"] == []


@pytest.mark.parametrize("container", ["items", "excluded"])
def test_toss_buy_candidates_rejects_forbidden_raw_fields_at_any_cache_depth(
    monkeypatch,
    container,
):
    payload = _cached_candidate_payload(_valid_cached_execution_calibration())
    payload[container] = [
        {"symbol": "SAFE", "nested": {"outcomes": [], "open_positions": []}}
    ]
    payload["excluded_count" if container == "excluded" else "count"] = 1
    if container == "items":
        payload["scan_summary"]["income_liveness_status"] = "no_signal"
        payload["scan_summary"]["income_liveness_diagnosis"] = {
            "reason": "no_income_gate_eligible_candidates",
            "upstream_executable_count": 0,
            "income_pass_count": 0,
            "income_ready_count": 0,
            "top_income_block_reasons": [],
        }
    monkeypatch.setattr(dd, "_cached", lambda *args, **kwargs: payload)

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert result["scan_summary"]["cache_contract_valid"] is False
    assert result["items"] == []
    assert result["excluded"] == []


def test_toss_buy_candidates_never_deepcopies_untrusted_cached_items(monkeypatch):
    payload = _cached_candidate_payload(_valid_cached_execution_calibration())
    payload["items"] = [{"raw": _DeepcopyBomb()}]
    payload["count"] = 1
    monkeypatch.setattr(dd, "_cached", lambda *args, **kwargs: payload)

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert result["scan_summary"]["cache_contract_valid"] is False
    assert result["items"] == []
    assert result["scan_summary"]["execution_calibration"]["evidence_sufficient"] is False


def test_toss_buy_candidates_does_not_call_hostile_cached_list_truthiness(monkeypatch):
    payload = _cached_candidate_payload(_valid_cached_execution_calibration())
    hostile_items = _BoolListSubclass([{}])
    _BoolListSubclass.bool_calls = 0
    payload["items"] = hostile_items
    payload["count"] = 1
    payload["scan_summary"]["income_liveness_status"] = "no_signal"
    payload["scan_summary"]["income_liveness_diagnosis"] = {
        "reason": "no_income_gate_eligible_candidates",
        "upstream_executable_count": 0,
        "income_pass_count": 0,
        "income_ready_count": 0,
        "top_income_block_reasons": [],
    }
    monkeypatch.setattr(dd, "_cached", lambda *args, **kwargs: payload)

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert _BoolListSubclass.bool_calls == 0
    assert result["scan_summary"]["cache_contract_valid"] is False
    assert result["items"] == []


def test_toss_buy_candidates_rejects_malformed_liveness_diagnosis(monkeypatch):
    payload = _cached_candidate_payload(_valid_cached_execution_calibration())
    payload["scan_summary"]["income_liveness_diagnosis"] = {
        "reason": "unexpected",
        "upstream_executable_count": 0,
        "income_pass_count": 0,
        "income_ready_count": 0,
        "top_income_block_reasons": [],
        "raw": "unknown",
    }
    monkeypatch.setattr(dd, "_cached", lambda *args, **kwargs: payload)

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert result["scan_summary"]["cache_contract_valid"] is False
    assert result["items"] == []


def test_toss_buy_candidates_rejects_semantically_wrong_liveness_reason(monkeypatch):
    payload = _cached_candidate_payload(_valid_cached_execution_calibration())
    payload["items"] = [{}]
    payload["count"] = 1
    payload["scan_summary"]["income_liveness_status"] = "no_signal"
    payload["scan_summary"]["income_liveness_diagnosis"] = {
        "reason": "semantically_wrong_but_primitive",
        "upstream_executable_count": 0,
        "income_pass_count": 0,
        "income_ready_count": 0,
        "top_income_block_reasons": [],
    }
    monkeypatch.setattr(dd, "_cached", lambda *args, **kwargs: payload)

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert result["scan_summary"]["cache_contract_valid"] is False
    assert result["items"] == []


def test_toss_buy_candidates_rejects_cached_item_liveness_mismatch(monkeypatch):
    payload = _cached_candidate_payload(_valid_cached_execution_calibration())
    payload["items"] = [{
        "side": "buy",
        "decision_bucket": "PASS_EXECUTE",
        "missing_fields": [],
        "limit_exceeded": False,
        "blocking_risk_flags": [],
        "execution_status": "ready",
        "stock_agent_ready": True,
        "income_strategy": {"income_pass": True},
    }]
    payload["count"] = 1
    payload["scan_summary"]["income_liveness_status"] = "no_signal"
    payload["scan_summary"]["income_liveness_diagnosis"] = {
        "reason": "no_income_gate_eligible_candidates",
        "upstream_executable_count": 0,
        "income_pass_count": 0,
        "income_ready_count": 0,
        "top_income_block_reasons": [],
    }
    monkeypatch.setattr(dd, "_cached", lambda *args, **kwargs: payload)

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert result["scan_summary"]["cache_contract_valid"] is False
    assert result["items"] == []


def test_toss_buy_candidates_accepts_proofed_ready_cache(monkeypatch):
    payload = _cached_candidate_payload(_valid_cached_execution_calibration())
    payload["items"] = [_proofed_ready_cache_item()]
    payload["count"] = 1
    payload["scan_summary"].update({
        "raw_income_pass_count": 1,
        "income_pass_count": 1,
        "income_block_count": 0,
        "income_gate_eligible_count": 1,
        "upstream_executable_count": 1,
        "income_ready_count": 1,
        "income_liveness_status": "healthy",
        "income_liveness_diagnosis": None,
    })
    monkeypatch.setattr(dd, "_cached", lambda *args, **kwargs: payload)

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert result["scan_summary"]["cache_contract_valid"] is True
    assert len(result["items"]) == 1
    assert result["items"][0]["quantity"] == 10


@pytest.mark.parametrize(
    ("authority_field", "bad_value"),
    [
        ("side", "sell"),
        ("decision_bucket", "HOLD"),
        ("income_pass", False),
        ("stock_agent_ready", 1),
        ("quantity", "10"),
        ("quality_proof", None),
    ],
)
def test_toss_buy_candidates_rejects_cached_ready_authority_mismatch(
    monkeypatch,
    authority_field,
    bad_value,
):
    payload = _cached_candidate_payload(_valid_cached_execution_calibration())
    item = _proofed_ready_cache_item()
    if authority_field == "income_pass":
        item["income_strategy"]["income_pass"] = bad_value
    elif authority_field == "quality_proof":
        item["quality_breakdown"].pop("candidate_snapshot_sha256", None)
    else:
        item[authority_field] = bad_value
    payload["items"] = [item]
    payload["count"] = 1
    payload["scan_summary"].update({
        "raw_income_pass_count": 1,
        "income_pass_count": 1,
        "income_block_count": 0,
        "income_gate_eligible_count": 1,
        "upstream_executable_count": 1,
        "income_ready_count": 1,
        "income_liveness_status": "healthy",
        "income_liveness_diagnosis": None,
    })
    monkeypatch.setattr(dd, "_cached", lambda *args, **kwargs: payload)

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert result["scan_summary"]["cache_contract_valid"] is False
    assert result["items"] == []


@pytest.mark.parametrize("hostile_container", ["diagnosis", "reason_row"])
def test_toss_buy_candidates_rejects_hostile_liveness_keys_without_hooks(
    monkeypatch,
    hostile_container,
):
    payload = _cached_candidate_payload(_valid_cached_execution_calibration())
    payload["items"] = [{}]
    payload["count"] = 1
    summary = payload["scan_summary"]
    summary.update({
        "income_liveness_status": "degraded",
        "income_gate_eligible_count": 2,
        "upstream_executable_count": 2,
        "income_block_count": 2,
    })
    reason_row = {"reason": "risk_block", "count": 2}
    diagnosis = {
        "reason": "upstream_executable_but_no_income_ready",
        "upstream_executable_count": 2,
        "income_pass_count": 0,
        "income_ready_count": 0,
        "top_income_block_reasons": [reason_row],
    }
    if hostile_container == "diagnosis":
        diagnosis = {_HostileKey(key): value for key, value in diagnosis.items()}
    else:
        diagnosis["top_income_block_reasons"] = [
            {_HostileKey(key): value for key, value in reason_row.items()}
        ]
    summary["income_liveness_diagnosis"] = diagnosis
    monkeypatch.setattr(dd, "_cached", lambda *args, **kwargs: payload)
    _HostileKey.hash_calls = 0
    _HostileKey.eq_calls = 0
    _HostileKey.armed = True

    try:
        result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)
    finally:
        _HostileKey.armed = False

    assert _HostileKey.hash_calls == 0
    assert _HostileKey.eq_calls == 0
    assert result["scan_summary"]["cache_contract_valid"] is False
    assert result["items"] == []


@pytest.mark.parametrize(
    "reason_row",
    [
        {"reason": "x", "count": 999},
        {"reason": "x", "count": 0},
        {"reason": "", "count": 1},
    ],
)
def test_toss_buy_candidates_rejects_impossible_liveness_reason_counts(
    monkeypatch,
    reason_row,
):
    payload = _cached_candidate_payload(_valid_cached_execution_calibration())
    payload["items"] = [{}]
    payload["count"] = 1
    summary = payload["scan_summary"]
    summary.update({
        "income_liveness_status": "degraded",
        "income_gate_eligible_count": 2,
        "upstream_executable_count": 2,
        "income_block_count": 2,
        "income_liveness_diagnosis": {
            "reason": "upstream_executable_but_no_income_ready",
            "upstream_executable_count": 2,
            "income_pass_count": 0,
            "income_ready_count": 0,
            "top_income_block_reasons": [reason_row],
        },
    })
    monkeypatch.setattr(dd, "_cached", lambda *args, **kwargs: payload)

    result = dd.toss_buy_candidates_data(range_="today", market="KR", limit=3)

    assert result["scan_summary"]["cache_contract_valid"] is False
    assert result["items"] == []


@pytest.mark.parametrize(
    ("market", "field", "bad_value", "reason"),
    [
        ("KR", "quantity", "1", "quantity_invalid"),
        ("KR", "quantity", True, "quantity_invalid"),
        ("KR", "risk_reward", "3.6", "risk_reward_invalid"),
        ("KR", "estimated_amount_krw", "1400000", "estimated_notional_invalid"),
        ("KR", "score", 0, "score_invalid"),
        ("US", "fx_usdkrw", 0, "fx_usdkrw_invalid"),
        ("KR", "quantity", float("nan"), "quantity_invalid"),
        ("KR", "quantity", float("inf"), "quantity_invalid"),
        ("KR", "quantity", float("-inf"), "quantity_invalid"),
        ("KR", "limit_price", float("nan"), "entry_price_invalid"),
        ("KR", "target_price", 10**400, "target_price_invalid"),
        ("KR", "stop_loss", 10**400, "stop_loss_invalid"),
        ("KR", "price", "POISON", "entry_price_invalid"),
        ("KR", "entry_price", "POISON", "entry_price_invalid"),
        ("KR", "current_price", "POISON", "entry_price_invalid"),
        ("KR", "side", "sell", "side_invalid"),
        ("KR", "market", "EU", "market_invalid"),
        ("KR", "market", "US", "symbol_market_mismatch"),
        ("KR", "currency", "USD", "currency_invalid"),
        ("US", "currency", "KRW", "currency_invalid"),
        ("US", "estimated_amount_usd", "1400", "estimated_notional_invalid"),
    ],
)
def test_toss_buy_candidates_preserves_explicit_invalid_execution_inputs(
    monkeypatch, market, field, bad_value, reason
):
    _patch_sections(monkeypatch, _sections())
    symbol = "NVDA" if market == "US" else "207940.KS"
    price = 1_400.0 if market == "US" else 1_400_000.0
    raw = {
        "symbol": symbol, "ticker": symbol, "name": "오염입력후보",
        "market": market, "side": "buy", "price": price,
        "current_price": price, "limit_price": price, "quantity": 1,
        "score": 88, "risk_reward": 3.6,
        "target_price": 1_500.0 if market == "US" else 1_500_000.0,
        "stop_loss": 1_300.0 if market == "US" else 1_300_000.0,
        "estimated_amount_krw": 2_100_000.0 if market == "US" else 1_400_000.0,
        "estimated_amount_usd": 1_400.0 if market == "US" else None,
        "fx_usdkrw": 1_500.0, "decision_bucket": "PASS_EXECUTE",
        "candidate_scope": "new_discovery", "read_only": True,
    }
    raw[field] = bad_value
    monkeypatch.setattr(disc, "toss_eligible_new_candidates", lambda *a, **k: {
        "items": [raw], "excluded": [], "count": 1, "excluded_count": 0,
        "scan_summary": {}, "note": "test fixture",
    })
    from core import toss_quality_gate as qg
    monkeypatch.setattr(qg, "score_candidates_batch", lambda items, **kwargs: items)
    monkeypatch.setattr(qg, "finalize_quality_proof", lambda item: True)
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})

    item = dd.toss_buy_candidates_data(range_="today", market=market)["items"][0]
    income = item["income_strategy"]

    assert item["upstream_input_validation_error"] == reason
    assert item["symbol"] == symbol
    assert income["income_pass"] is False
    assert income["income_block_reason"] == reason
    assert income["expected_pnl_krw"] is None
    assert income["decision_expected_pnl_krw"] is None
    assert item["stock_agent_ready"] is False


def test_toss_buy_candidates_blocks_buy_limit_above_current(monkeypatch):
    # KT 사례: 현재가 53,500원인데 지정가가 더 높으면 자동매수 후보로 PASS되면 안 된다.
    monkeypatch.setattr(dd, "_cache", {}, raising=False)
    _patch_network(monkeypatch)
    monkeypatch.setattr(
        disc, "_fallback_universe_candidates",
        lambda markets: [],
    )
    monkeypatch.setattr(
        disc, "build_discovery_sections",
        lambda *a, **k: _sections(),
    )
    monkeypatch.setattr(
        disc, "toss_eligible_new_candidates",
        lambda sections, max_order_krw=500_000: {
            "items": [{
                "symbol": "030200.KS", "ticker": "030200.KS", "name": "KT",
                "side": "buy", "quantity": 1, "current_price": 53_500.0,
                "price": 53_500.0, "limit_price": 55_000.0,
                "estimated_amount_krw": 55_000.0, "stop_loss": 50_000.0,
                "target_price": 60_000.0, "execution_status": "conditional_small_entry",
            }],
            "excluded": [], "count": 1, "excluded_count": 0,
            "scan_summary": {}, "note": "test",
        },
    )

    result = dd.toss_buy_candidates_data(range_="today")

    item = result["items"][0]
    assert item["symbol"] == "030200.KS"
    assert item["current_price"] == 53_500.0
    assert item["limit_price"] == 55_000.0
    assert item["execution_status"] == "chase_block"
    assert item["executable_now"] is False
    assert item["stock_agent_ready"] is False
    assert "지정가가 현재가보다 높음" in item["fill_risk_note"]
    assert "55,000원 > 현재가 53,500원" in item["block_reason"]

def test_toss_buy_candidates_limit_exceeded_not_stock_agent_ready(monkeypatch):
    sections = _sections(new=[_strong_cand("222.KS", "고가주", 600_000)])
    _patch_sections(monkeypatch, sections)

    result = dd.toss_buy_candidates_data(range_="today")

    item = next(i for i in result["items"] if i["symbol"] == "222.KS")
    assert item["limit_exceeded"] is True
    assert item["missing_fields"] == []
    assert item["stock_agent_ready"] is False
    assert "한도" in " ".join(item["risk_notes"])


def test_toss_buy_candidates_sizes_by_confidence_rr_and_stop(monkeypatch):
    strong = _new_cand("000777.KS", "강한후보", price=50_000, score=88)
    strong = strong.__class__(
        **{**strong.__dict__, "risk_reward": 2.8, "stop_loss": 48_500.0}
    )
    cautious = _new_cand("000888.KS", "보수후보", price=50_000, score=66)
    cautious = cautious.__class__(
        **{**cautious.__dict__, "risk_reward": 1.3, "stop_loss": 46_000.0}
    )
    sections = _sections(new=[strong, cautious])
    _patch_sections(monkeypatch, sections)

    result = dd.toss_buy_candidates_data(range_="today")

    by_symbol = {i["symbol"]: i for i in result["items"]}
    assert by_symbol["000777.KS"]["quantity"] == 10
    assert by_symbol["000777.KS"]["estimated_amount_krw"] == 500_000.0
    assert by_symbol["000777.KS"]["position_sizing"]["stop_risk_pct"] == 3.0
    assert by_symbol["000888.KS"]["quantity"] == 5
    assert by_symbol["000888.KS"]["estimated_amount_krw"] == 250_000.0
    assert by_symbol["000888.KS"]["position_sizing"]["stop_risk_pct"] == 8.0





def test_toss_candidate_missing_quality_bucket_is_never_stock_agent_ready(monkeypatch):
    from core import toss_quality_gate as qg

    weak = _new_cand("000445.KQ", "품질누락", price=48_000)
    weak = weak.__class__(
        **{**weak.__dict__,
           "target_price": 56_000.0,
           "stop_loss": 46_500.0,
           "risk_reward": 3.0,
           "risk_flags": ("수급 약함 — 즉시 실행보다 관찰",),
           "suggested_accounts": ("삼성 수동", "ISA", "토스 AI")}
    )
    _patch_sections(monkeypatch, _sections(new=[weak]))
    _patch_positive_lifecycle_income(monkeypatch)
    monkeypatch.setattr(qg, "score_candidates_batch", lambda items, **kwargs: items)

    item = dd.toss_buy_candidates_data(range_="today")["items"][0]

    assert item["decision_bucket"] == "BLOCK"
    assert item["decision_reason"] == "quality_finalization_failed"
    assert item["stock_agent_ready"] is False
    assert item["executable_now"] is False
    assert item["execution_status"] == "quality_finalization_failed"
    assert item["block_reason"] == "최종 품질 증명 생성 실패"


def test_toss_candidate_with_weak_flow_only_becomes_conditional_small_entry(monkeypatch):
    weak = _new_cand("000444.KQ", "수급약함", price=48_000)
    weak = weak.__class__(
        **{**weak.__dict__,
           "target_price": 56_000.0,
           "stop_loss": 46_500.0,
           "risk_reward": 3.0,
           "risk_flags": ("수급 약함 — 즉시 실행보다 관찰",),
           "suggested_accounts": ("삼성 수동", "ISA", "토스 AI")}
    )
    sections = _sections(new=[weak])
    _patch_sections(monkeypatch, sections)
    _patch_positive_lifecycle_income(monkeypatch)

    result = dd.toss_buy_candidates_data(range_="today")
    item = result["items"][0]

    assert item["execution_status"] == "conditional_small_entry"
    assert item["executable_now"] is True
    assert item["stock_agent_ready"] is True
    assert item["blocking_risk_flags"] == []
    assert item["observation_flags"] == ["수급 약함 — 즉시 실행보다 관찰"]
    assert "수급 약함" in " ".join(item["risk_notes"])
    assert result["scan_summary"]["conditional_small_entry_count"] == 1


def test_toss_candidate_with_intraday_risk_is_hold_not_executable(monkeypatch):
    risky = _new_cand("000333.KQ", "장중반전", price=158_000)
    risky = risky.__class__(
        **{**risky.__dict__,
           "high_price": 177_600.0,
           "low_price": 156_000.0,
           "intraday_drawdown_pct": -11.0,
           "intraday_range_pct": 13.8,
           "risk_flags": ("장중 고점 대비 급락 -11.0%",),
           "suggested_accounts": ("삼성 수동", "ISA", "토스 AI")}
    )
    sections = _sections(new=[risky])
    _patch_sections(monkeypatch, sections)
    result = dd.toss_buy_candidates_data(range_="today")
    item = result["items"][0]
    assert item["execution_status"] == "hold_risk_flags"
    assert item["executable_now"] is False
    assert item["stock_agent_ready"] is False
    assert "장중" in item["block_reason"]
    assert "장중" in " ".join(item["risk_notes"])



def test_toss_buy_candidates_blocks_kr_buy_when_native_krw_cash_insufficient(monkeypatch):
    sections = _sections(new=[_new_cand("000111.KS", "신규소액주", price=30_000)])
    _patch_sections(monkeypatch, sections)
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
        "cash": {"krw": 2_000_000, "krw_native": 4_000, "usd": 1400, "usd_krw": 1_996_000},
        "holdings_count": 0,
    })

    result = dd.toss_buy_candidates_data(range_="today")
    item = result["items"][0]

    assert item["execution_status"] == "cash_unavailable"
    assert item["executable_now"] is False
    assert item["stock_agent_ready"] is False
    assert "KRW 예수금 부족" in item["block_reason"]


def test_toss_buy_candidates_blocks_us_buy_when_usd_cash_insufficient(monkeypatch):
    sections = _sections(new=[_new_cand("NVDA", "엔비디아", market="US", price=190.0)])
    _patch_sections(monkeypatch, sections)
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
        "cash": {"krw": 500_000, "krw_native": 500_000, "usd": 10.0, "usd_krw": 14_000},
        "holdings_count": 0,
    })

    result = dd.toss_buy_candidates_data(range_="today", market="US")
    item = result["items"][0]

    assert item["execution_status"] == "cash_unavailable"
    assert item["executable_now"] is False
    assert item["stock_agent_ready"] is False
    assert "USD 예수금 부족" in item["block_reason"]



def test_toss_buy_candidates_excludes_recent_position_review_sells(monkeypatch):
    sections = _sections(new=[
        _new_cand("403870.KS", "HPSP", price=39_000),
        _new_cand("000111.KS", "신규소액주", price=30_000),
    ])
    _patch_sections(monkeypatch, sections)
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {"403870.KS": {"reason": "position_review_sell", "created_at": "2026-07-08T11:27:43+09:00"}})

    result = dd.toss_buy_candidates_data(range_="today")

    symbols = {i["symbol"] for i in result["items"]}
    assert "403870.KS" not in symbols
    assert "000111.KS" in symbols
    excluded = [e for e in result["excluded"] if e.get("symbol") == "403870.KS"]
    assert excluded
    assert excluded[0]["scope"] == "recent_risk_sell_cooldown"
    assert "최근 리스크 매도" in excluded[0]["reason"]
    assert result["scan_summary"]["recent_risk_sell_excluded_count"] == 1


def test_toss_buy_candidates_excludes_recent_sell_to_fund_symbols(monkeypatch):
    """리밸런싱 매도로 판 종목이 곧바로 신규 BUY 후보로 되돌아오면 안 된다."""
    sections = _sections(new=[
        _new_cand("015760.KS", "한국전력", price=35_450),
        _new_cand("000111.KS", "신규소액주", price=30_000),
    ])
    _patch_sections(monkeypatch, sections)
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
        "cash": {"krw": 2_000_000, "krw_native": 2_000_000, "usd": 0.0},
        "holdings_count": 0,
    })
    monkeypatch.setattr(
        "core.toss_live_pilot_ledger.list_live_pilot_records",
        lambda *a, **k: [{
            "symbol": "015760.KS", "side": "sell", "status": "live_sent",
            "reason": "income_rebalance_sell_to_fund",
            "created_at": "2026-07-09T11:27:43+09:00",
        }],
    )

    result = dd.toss_buy_candidates_data(range_="today")

    symbols = {i["symbol"] for i in result["items"]}
    assert "015760.KS" not in symbols
    assert "000111.KS" in symbols
    excluded = [e for e in result["excluded"] if e.get("symbol") == "015760.KS"]
    assert excluded
    assert excluded[0]["scope"] == "recent_risk_sell_cooldown"


def test_recent_risk_sell_symbols_maps_sell_to_fund_reason(monkeypatch):
    monkeypatch.setattr(
        "core.toss_live_pilot_ledger.list_live_pilot_records",
        lambda *a, **k: [
            {"symbol": "015760.KS", "side": "sell", "status": "live_sent",
             "reason": "income_rebalance_sell_to_fund"},
            {"symbol": "035420.KS", "side": "sell", "status": "live_sent",
             "reason": "auto_pipeline"},
        ],
    )
    out = dd._recent_toss_risk_sell_symbols()
    assert out["015760.KS"]["reason"] == "income_rebalance_sell_to_fund"
    assert out["015760"]["reason"] == "income_rebalance_sell_to_fund"
    assert "035420.KS" not in out


def test_toss_buy_candidates_income_strategy_fields_and_ready_gate(monkeypatch):
    strong = _new_cand("000777.KS", "강한수입후보", price=50_000, score=88)
    strong = strong.__class__(
        **{**strong.__dict__, "target_price": 56_000.0, "stop_loss": 48_000.0, "risk_reward": 3.0}
    )
    sections = _sections(new=[strong])
    _patch_sections(monkeypatch, sections)
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})

    result = dd.toss_buy_candidates_data(range_="today")
    item = next(i for i in result["items"] if i["symbol"] == "000777.KS")
    income = item["income_strategy"]

    assert income["version"] == "income_v2"
    assert income["expected_pnl_model"] == "income_exit_cashflow_v2"
    assert income["income_pass"] is False
    assert income["expected_pnl_krw"] < 0
    assert income["profit_exit_quantity"] < income["loss_exit_quantity"]
    assert item["stock_agent_ready"] is False


def test_toss_buy_candidates_stale_snapshot_blocks_ready_and_sizing(monkeypatch):
    strong = _new_cand("000779.KS", "snapshot차단후보", price=50_000, score=88)
    strong = strong.__class__(
        **{**strong.__dict__, "target_price": 56_000.0, "stop_loss": 48_000.0, "risk_reward": 3.0}
    )
    _patch_sections(monkeypatch, _sections(new=[strong]))
    monkeypatch.setattr(dd, "_dashboard_toss_broker_reads_isolated", lambda: True)
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
        "snapshot_status": "fresh",
        "snapshot_usable_for_decisions": True,
        "cash": {"krw": 10_000_000, "krw_native": 10_000_000},
        "total_account_value": {"krw": 20_000_000},
        "holdings_count": 0,
    })
    monkeypatch.setattr(
        "core.toss_readonly_snapshot.load_snapshot",
        lambda: {"ok": True, "status": "stale", "usable_for_decisions": False},
    )
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})
    monkeypatch.setattr(
        "core.toss_income_strategy.compute_income_edge",
        lambda *a, **k: {
            "version": "income_v2",
            "income_pass": True,
            "income_grade": "INCOME_PASS",
            "expected_pnl_krw": 12_000.0,
            "income_edge_ratio": 0.02,
            "income_block_reason": "",
            "income_block_label": "",
        },
    )

    result = dd.toss_buy_candidates_data(range_="today")
    item = next(i for i in result["items"] if i["symbol"] == "000779.KS")

    assert item["income_strategy"]["income_pass"] is True
    assert item["stock_agent_ready"] is False
    assert item["executable_now"] is False
    assert item["execution_status"] == "toss_snapshot_stale"
    assert item["cash_check"]["checked"] is False
    assert result["scan_summary"]["snapshot_candidate_blocked"] is True
    assert result["scan_summary"]["snapshot_status"] == "stale"
    assert result["scan_summary"]["income_ready_count"] == 0


def test_toss_buy_candidates_missing_snapshot_also_fails_closed(monkeypatch):
    strong = _new_cand("000780.KS", "snapshot누락후보", price=50_000, score=88)
    strong = strong.__class__(
        **{**strong.__dict__, "target_price": 56_000.0, "stop_loss": 48_000.0, "risk_reward": 3.0}
    )
    _patch_sections(monkeypatch, _sections(new=[strong]))
    monkeypatch.setattr(dd, "_dashboard_toss_broker_reads_isolated", lambda: True)
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
        "error": "stock_bot_snapshot_unavailable",
        "cash": {"krw": 0},
    })
    monkeypatch.setattr(
        "core.toss_readonly_snapshot.load_snapshot",
        lambda: {"ok": False, "status": "missing", "reason": "snapshot_missing"},
    )
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})

    result = dd.toss_buy_candidates_data(range_="today")
    item = next(i for i in result["items"] if i["symbol"] == "000780.KS")

    assert item["stock_agent_ready"] is False
    assert item["executable_now"] is False
    assert item["execution_status"] == "toss_snapshot_stale"
    assert result["scan_summary"]["snapshot_candidate_blocked"] is True
    assert result["scan_summary"]["snapshot_status"] == "missing"


@pytest.mark.parametrize(
    "snapshot_state",
    [
        {"ok": True, "status": "unknown", "usable_for_decisions": True},
        {"ok": "true", "status": "fresh", "usable_for_decisions": "true"},
    ],
)
def test_toss_buy_candidates_snapshot_requires_fresh_exact_booleans(monkeypatch, snapshot_state):
    strong = _new_cand("000781.KS", "snapshot계약차단", price=50_000, score=88)
    strong = strong.__class__(
        **{**strong.__dict__, "target_price": 56_000.0, "stop_loss": 48_000.0, "risk_reward": 3.0}
    )
    _patch_sections(monkeypatch, _sections(new=[strong]))
    monkeypatch.setattr(dd, "_dashboard_toss_broker_reads_isolated", lambda: True)
    monkeypatch.setattr("core.toss_readonly_snapshot.load_snapshot", lambda: snapshot_state)

    result = dd.toss_buy_candidates_data(range_="today")
    item = next(i for i in result["items"] if i["symbol"] == "000781.KS")

    assert item["stock_agent_ready"] is False
    assert item["execution_status"] == "toss_snapshot_stale"
    assert result["scan_summary"]["snapshot_candidate_blocked"] is True


@pytest.mark.parametrize(
    "summary",
    [
        {"snapshot_status": "unknown", "snapshot_usable_for_decisions": True},
        {"snapshot_status": "fresh", "snapshot_usable_for_decisions": "true"},
    ],
)
def test_toss_buy_candidates_account_snapshot_fallback_is_exact(monkeypatch, summary):
    strong = _new_cand("000782.KS", "account계약차단", price=50_000, score=88)
    strong = strong.__class__(
        **{**strong.__dict__, "target_price": 56_000.0, "stop_loss": 48_000.0, "risk_reward": 3.0}
    )
    _patch_sections(monkeypatch, _sections(new=[strong]))
    monkeypatch.setattr(dd, "_dashboard_toss_broker_reads_isolated", lambda: False)
    monkeypatch.setattr(
        dd,
        "toss_account_summary",
        lambda: {
            **summary,
            "cash": {"krw": 10_000_000, "krw_native": 10_000_000},
            "holdings_count": 0,
        },
    )

    result = dd.toss_buy_candidates_data(range_="today")
    item = next(i for i in result["items"] if i["symbol"] == "000782.KS")

    assert item["stock_agent_ready"] is False
    assert item["execution_status"] == "toss_snapshot_stale"
    assert result["scan_summary"]["snapshot_candidate_blocked"] is True


def test_toss_buy_candidates_income_block_disables_stock_agent_ready(monkeypatch):
    weak = _new_cand("000112.KS", "수입약한후보", price=50_000, score=70)
    weak = weak.__class__(
        **{**weak.__dict__, "target_price": 51_000.0, "stop_loss": 47_000.0, "risk_reward": 2.0}
    )
    sections = _sections(new=[weak])
    _patch_sections(monkeypatch, sections)
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})

    result = dd.toss_buy_candidates_data(range_="today")
    item = next(i for i in result["items"] if i["symbol"] == "000112.KS")

    assert item["income_strategy"]["income_pass"] is False
    assert item["stock_agent_ready"] is False
    assert "수입 기대값" in item["block_reason"]


def test_toss_buy_candidates_non_boolean_income_pass_never_sets_ready(monkeypatch):
    strong = _new_cand("000114.KS", "타입오염후보", price=50_000, score=88)
    strong = strong.__class__(
        **{**strong.__dict__, "target_price": 58_000.0,
           "stop_loss": 47_000.0, "risk_reward": 2.6}
    )
    _patch_sections(monkeypatch, _sections(new=[strong]))
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})

    from core import toss_income_strategy
    monkeypatch.setattr(
        toss_income_strategy,
        "compute_income_edge",
        lambda *a, **k: {
            "version": "income_v1",
            "income_pass": "false",
            "income_grade": "PASS",
            "expected_pnl_krw": 100_000,
        },
    )

    result = dd.toss_buy_candidates_data(range_="today")
    item = next(i for i in result["items"] if i["symbol"] == "000114.KS")

    assert item["income_strategy"]["income_pass"] == "false"
    assert item["stock_agent_ready"] is False


def test_finalized_dashboard_buy_records_and_validates_quality(
    monkeypatch, tmp_path,
):
    strong = _new_cand("000115.KS", "최종품질후보", price=50_000, score=88)
    strong = strong.__class__(
        **{**strong.__dict__, "target_price": 58_000.0,
           "stop_loss": 47_000.0, "risk_reward": 2.6}
    )
    _patch_sections(monkeypatch, _sections(new=[strong]))
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})

    from core import toss_quality_gate as qg
    monkeypatch.setattr(qg, "_outcomes_db_path", lambda: tmp_path / "final-dashboard.db")
    qg._outcomes_schema_created = False

    result = dd.toss_buy_candidates_data(range_="today")
    item = next(i for i in result["items"] if i["symbol"] == "000115.KS")
    pilot_id = "tlive_20260714_160010_0001"
    decision_ref = "execution_decision:hermes_dashboard_final_0001"

    recorded = qg.record_execution_quality_decision(
        item, pilot_id=pilot_id, decision_ref=decision_ref,
    )
    rec = {
        "side": "buy", "pilot_id": pilot_id, "decision_ref": decision_ref,
        "symbol": item["symbol"], "quantity": item["quantity"],
        "limit_price": item["limit_price"], "stop_loss": item["stop_loss"],
        "target_price": item["target_price"],
    }
    validated = qg.validate_execution_quality_decision(rec, pilot_id=pilot_id)

    assert item["quality_breakdown"]["rr_ratio"] == pytest.approx(
        qg._recompute_rr_ratio(item["limit_price"], item["stop_loss"], item["target_price"]),
        abs=0.01,
    )
    assert recorded["ok"] is True
    assert validated["ok"] is True


def test_toss_buy_candidates_same_symbol_pending_blocks_ready(monkeypatch):
    strong = _new_cand("000113.KS", "펜딩후보", price=50_000, score=88)
    strong = strong.__class__(
        **{**strong.__dict__, "target_price": 56_000.0, "stop_loss": 48_000.0, "risk_reward": 3.0}
    )
    sections = _sections(new=[strong])
    _patch_sections(monkeypatch, sections)
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {"000113.KS": {"status": "pending"}})

    result = dd.toss_buy_candidates_data(range_="today")
    item = next(i for i in result["items"] if i["symbol"] == "000113.KS")

    assert item["income_strategy"]["income_pass"] is False
    assert item["stock_agent_ready"] is False
    assert "PENDING" in item["block_reason"]



def test_toss_buy_candidates_income_plan_tightens_stop_and_allows_strong_candidate(monkeypatch):
    strong = _new_cand("000778.KS", "고수익후보", price=1_400_000, score=88)
    strong = strong.__class__(
        **{**strong.__dict__, "target_price": 1_620_000.0,
           "stop_loss": 1_300_000.0, "risk_reward": 2.6}
    )
    sections = _sections(new=[strong])
    _patch_sections(monkeypatch, sections)
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})
    from core import toss_live_pilot_policy as tlp
    monkeypatch.setattr(tlp, "compute_toss_live_pilot_policy", lambda *a, **k: {"max_order_krw": None})
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
        "cash": {"krw": 5_000_000, "krw_native": 5_000_000, "usd": 0.0},
        "total_account_value": {"krw": 10_000_000},
        "holdings_count": 10,
    })

    result = dd.toss_buy_candidates_data(range_="today")
    item = next(i for i in result["items"] if i["symbol"] == "000778.KS")

    assert item["original_stop_loss"] == 1_300_000.0
    assert item["stop_loss"] > 1_300_000.0
    assert item["income_exit_plan"]["stop_risk_pct"] <= 4.5
    assert item["income_strategy"]["decision_expected_pnl_krw"] > 0
    assert item["income_strategy"]["income_pass"] is True
    assert item["stock_agent_ready"] is True


def test_income_edge_uses_finalized_bucket_not_preliminary_bucket(monkeypatch):
    strong = _new_cand("000779.KS", "최종버킷후보", price=1_400_000, score=85)
    strong = strong.__class__(
        **{**strong.__dict__, "target_price": 1_620_000.0,
           "stop_loss": 1_340_000.0, "risk_reward": 3.6}
    )
    _patch_sections(monkeypatch, _sections(new=[strong]))

    from core import toss_income_strategy as tis
    from core import toss_live_pilot_policy as tlp
    from core import toss_quality_gate as qg

    monkeypatch.setattr(tlp, "compute_toss_live_pilot_policy", lambda *a, **k: {"max_order_krw": None})
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
        "cash": {"krw": 5_000_000, "krw_native": 5_000_000, "usd": 0.0},
        "total_account_value": {"krw": 10_000_000},
        "holdings_count": 10,
    })

    def finalize_to_small_pass(item):
        item["decision_bucket"] = "SMALL_PASS"
        item["decision_reason"] = "finalized_small_pass"
        return True

    seen_buckets = []
    real_compute = tis.compute_income_edge

    def compute_after_finalization(candidate, **kwargs):
        seen_buckets.append(candidate.get("decision_bucket"))
        return real_compute(candidate, **kwargs)

    monkeypatch.setattr(qg, "finalize_quality_proof", finalize_to_small_pass)
    monkeypatch.setattr(tis, "compute_income_edge", compute_after_finalization)

    item = dd.toss_buy_candidates_data(range_="today")["items"][0]

    assert seen_buckets == ["SMALL_PASS"]
    assert item["decision_bucket"] == "SMALL_PASS"
    assert item["income_strategy"]["decision_expected_pnl_krw"] < 0
    assert item["income_strategy"]["income_pass"] is False
    assert item["stock_agent_ready"] is False


def test_toss_buy_candidates_caps_ready_to_top_three_when_many_holdings(monkeypatch):
    cands = []
    for idx, target in enumerate([58_000, 57_000, 56_000, 55_000], start=1):
        c = _new_cand(f"10000{idx}.KS", f"후보{idx}", price=50_000, score=88)
        cands.append(c.__class__(
            **{**c.__dict__, "target_price": float(target), "stop_loss": 47_000.0, "risk_reward": 2.5}
        ))
    sections = _sections(new=cands)
    _patch_sections(monkeypatch, sections)
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})
    _patch_positive_lifecycle_income(monkeypatch)
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
        "cash": {"krw": 2_000_000, "krw_native": 2_000_000, "usd": 0.0},
        "holdings_count": 15,
    })

    result = dd.toss_buy_candidates_data(range_="today")
    items = result["items"]
    ready_symbols = {i["symbol"] for i in items if i["stock_agent_ready"]}

    # 검증 목적은 "반환 순서"가 아니라, 보유 12개 초과 시 기대손익 상위
    # 3개만 준비 완료로 남는지다. 순서 비교는 전역 상태에 따라 플래키했다.
    def _expected(i):
        return float((i.get("income_strategy") or {}).get("decision_expected_pnl_krw") or 0)

    top3_symbols = {i["symbol"] for i in sorted(items, key=_expected, reverse=True)[:3]}
    assert len(ready_symbols) == 3
    assert ready_symbols == top3_symbols
    assert result["scan_summary"]["portfolio_income_ready_cap"] == 3
    assert result["scan_summary"]["portfolio_cap_block_count"] == 1
    blocked = [i for i in items if i.get("execution_status") == "portfolio_income_cap"]
    assert len(blocked) == 1


def test_portfolio_cap_excludes_partial_v2_before_ranking(monkeypatch):
    cands = []
    for idx in range(1, 5):
        c = _new_cand(f"10000{idx}.KS", f"후보{idx}", price=50_000, score=88)
        cands.append(c.__class__(
            **{**c.__dict__, "target_price": 58_000.0,
               "stop_loss": 47_000.0, "risk_reward": 2.5}
        ))
    _patch_sections(monkeypatch, _sections(new=cands))
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
        "cash": {"krw": 2_000_000, "krw_native": 2_000_000, "usd": 0.0},
        "holdings_count": 15,
    })
    _patch_positive_lifecycle_income(monkeypatch)
    from core import toss_income_strategy as tis
    positive = tis.compute_income_edge

    def partial_first(candidate, **kwargs):
        out = positive(candidate, **kwargs)
        if candidate.get("symbol") == "100001.KS":
            out["expected_pnl_krw"] = 999_999.0
            out["decision_expected_pnl_krw"] = None
            out.pop("decision_expected_pnl_scope")
        return out

    monkeypatch.setattr(tis, "compute_income_edge", partial_first)

    result = dd.toss_buy_candidates_data(range_="today")
    by_symbol = {item["symbol"]: item for item in result["items"]}

    assert by_symbol["100001.KS"]["stock_agent_ready"] is False
    assert by_symbol["100004.KS"]["stock_agent_ready"] is True
    assert sum(item["stock_agent_ready"] is True for item in result["items"]) == 3
    assert not any(
        item.get("execution_status") == "portfolio_income_cap"
        for item in result["items"]
    )


def test_toss_buy_candidates_blocks_new_buy_when_holdings_over_twenty(monkeypatch):
    strong = _new_cand("100009.KS", "초과보유후보", price=50_000, score=90)
    strong = strong.__class__(
        **{**strong.__dict__, "target_price": 58_000.0, "stop_loss": 47_000.0, "risk_reward": 2.6}
    )
    sections = _sections(new=[strong])
    _patch_sections(monkeypatch, sections)
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})
    _patch_positive_lifecycle_income(monkeypatch)
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
        "cash": {"krw": 2_000_000, "krw_native": 2_000_000, "usd": 0.0},
        "holdings_count": 21,
    })

    result = dd.toss_buy_candidates_data(range_="today")
    item = result["items"][0]

    assert item["income_strategy"]["income_pass"] is True
    assert item["stock_agent_ready"] is False
    assert item["execution_status"] == "portfolio_rebalance_required"
    assert "보유 20개 초과" in item["block_reason"]
    assert result["scan_summary"]["portfolio_rebalance_required"] is True



def test_toss_buy_candidates_exposes_rebalance_plan_when_over_twenty_holdings(monkeypatch):
    strong = _new_cand("100010.KS", "리밸런싱후보", price=50_000, score=90)
    strong = strong.__class__(
        **{**strong.__dict__, "target_price": 58_000.0, "stop_loss": 47_000.0, "risk_reward": 2.6}
    )
    sections = _sections(new=[strong])
    _patch_sections(monkeypatch, sections)
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})
    _patch_positive_lifecycle_income(monkeypatch)
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
        "cash": {"krw": 50_000, "krw_native": 50_000, "usd": 0.0},
        "holdings_count": 25,
        "holdings_items": [
            {"symbol": "000001", "name": "약한보유", "quantity": "3", "lastPrice": "90000", "currency": "KRW",
             "marketValue": {"purchaseAmount": "300000", "amount": "270000"},
             "profitLoss": {"amountAfterCost": "-30000"}, "dailyProfitLoss": {"amount": "-10000"}},
        ],
    })

    result = dd.toss_buy_candidates_data(range_="today")
    plan = result["scan_summary"]["rebalance_plan"]

    assert plan["portfolio_rebalance_required"] is True
    assert plan["income_buy_waitlist"][0]["symbol"] == "100010.KS"
    assert plan["sell_to_fund_candidates"][0]["symbol"] == "000001.KS"
    assert result["items"][0]["rebalance_required"] is True


def test_toss_buy_candidates_no_fixed_cap_uses_account_risk_sizing(monkeypatch):
    """정책 max_order_krw=None을 50만원으로 되살리지 않고 동적 위험 수량을 쓴다."""
    expensive = _new_cand("207940.KS", "고가수입후보", price=1_400_000, score=88)
    expensive = expensive.__class__(
        **{
            **expensive.__dict__,
            "target_price": 1_620_000.0,
            "stop_loss": 1_340_000.0,
            "risk_reward": 3.6,
        }
    )
    _patch_sections(monkeypatch, _sections(new=[expensive]))
    from core import ai_berkshire_toss as abt
    from core import toss_live_pilot_policy as tlp
    # 이 테스트는 수량 산정 계약만 검증한다. 실제 repo score의 종목별 BUY
    # 판정과 분리해 신규 score 추가가 sizing 결과를 바꾸지 않게 한다.
    monkeypatch.setattr(abt, "load_ai_berkshire_scores", lambda *a, **k: {
        "items": {
            "UNRELATED": {
                "classification": "hold",
                "as_of": "2026-07-10",
                "valid_until": "2099-12-31",
                "thesis": "unrelated score fixture",
                "red_lines": ["fixture"],
                "source_urls": ["https://example.com/fixture"],
            }
        }
    })
    monkeypatch.setattr(tlp, "compute_toss_live_pilot_policy", lambda *a, **k: {"max_order_krw": None})
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
        "cash": {"krw": 5_000_000, "krw_native": 5_000_000, "usd": 0.0},
        "total_account_value": {"krw": 10_000_000},
        "holdings_count": 10,
    })

    result = dd.toss_buy_candidates_data(range_="today")
    item = next(i for i in result["items"] if i["symbol"] == "207940.KS")

    assert result["max_order_krw"] is None
    assert item["limit_exceeded"] is False
    assert item["quantity"] == 1
    assert item["estimated_amount_krw"] == 1_400_000.0
    assert item["position_sizing"]["method"] == "account_risk_concentration_sizing"
    assert item["position_sizing"]["account_risk_budget_pct"] == 1.0
    assert item["position_sizing"]["max_position_pct"] == 15.0
    assert item["income_strategy"]["income_pass"] is True
    assert item["stock_agent_ready"] is True


def test_toss_buy_candidates_explicit_fixed_cap_still_blocks_expensive_share(monkeypatch):
    expensive = _new_cand("207940.KS", "고가수입후보", price=1_400_000, score=88)
    expensive = expensive.__class__(
        **{
            **expensive.__dict__,
            "target_price": 1_620_000.0,
            "stop_loss": 1_340_000.0,
            "risk_reward": 3.6,
        }
    )
    _patch_sections(monkeypatch, _sections(new=[expensive]))
    from core import toss_live_pilot_policy as tlp
    monkeypatch.setattr(tlp, "compute_toss_live_pilot_policy", lambda *a, **k: {"max_order_krw": 500_000})
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
        "cash": {"krw": 5_000_000, "krw_native": 5_000_000, "usd": 0.0},
        "total_account_value": {"krw": 10_000_000},
        "holdings_count": 10,
    })

    result = dd.toss_buy_candidates_data(range_="today")
    item = next(i for i in result["items"] if i["symbol"] == "207940.KS")

    assert result["max_order_krw"] == 500_000
    assert item["limit_exceeded"] is True
    assert item["stock_agent_ready"] is False


# ── pending 차단 정밀화: stale 이력이 신규 후보를 영구 차단하지 않는다 ──

class TestPendingSymbolLifecycle:
    KST = timezone(timedelta(hours=9))

    def _now(self):
        return datetime.now(self.KST)

    def _rec(self, symbol="DELL", status="previewed", side="buy",
             pilot_id="tlive_x_1", created_delta_min=-5):
        return {
            "symbol": symbol, "status": status, "side": side,
            "pilot_id": pilot_id,
            "created_at": (self._now() + timedelta(minutes=created_delta_min)).isoformat(),
        }

    def _pending(self, records, snapshot=None, verification=None):
        import core.dashboard_data as dd
        snap = snapshot if snapshot is not None else {
            "ok": True, "status": "fresh", "usable_for_decisions": True,
            "broker_orders": []}
        with patch("core.toss_live_pilot_ledger.list_live_pilot_records",
                   return_value=records), \
             patch("core.toss_readonly_snapshot.load_snapshot",
                   return_value=snap), \
             patch("core.toss_live_pilot_verification.get_verification_for_pilot",
                   return_value=verification):
            return dd._pending_toss_order_symbols()

    def test_expired_pass_previewed_no_open_allows(self):
        v = {"status": "PASS",
             "expires_at": (self._now() - timedelta(hours=2)).isoformat()}
        out = self._pending([self._rec()], verification=v)
        assert out == {}   # expired PASS — 신규 후보 허용

    def test_fresh_pass_and_pending_block(self):
        for st in ("PASS", "PENDING"):
            v = {"status": st,
                 "expires_at": (self._now() + timedelta(minutes=5)).isoformat()}
            out = self._pending([self._rec()], verification=v)
            assert "DELL" in out, st

    def test_live_sent_with_matching_open_blocks(self):
        snap = {"ok": True, "status": "fresh", "usable_for_decisions": True,
                "broker_orders": [
            {"symbol": "DELL", "side": "BUY", "broker_order_status": "OPEN"}]}
        out = self._pending([self._rec(status="live_sent")], snapshot=snap)
        assert "DELL" in out
        assert out["DELL"]["source"] == "internal_ledger+broker_snapshot"

    def test_live_sent_terminal_or_absent_broker_allows(self):
        for orders in ([{"symbol": "DELL", "side": "BUY",
                         "broker_order_status": "FILLED"}], []):
            snap = {"ok": True, "status": "fresh",
                    "usable_for_decisions": True, "broker_orders": orders}
            out = self._pending([self._rec(status="live_sent")], snapshot=snap)
            assert out == {}, orders

    @pytest.mark.parametrize("usable", [False, "true", 1, None])
    def test_live_sent_snapshot_requires_exact_usable_authority(self, usable):
        snap = {"ok": True, "status": "fresh",
                "usable_for_decisions": usable, "broker_orders": []}
        out = self._pending([self._rec(status="live_sent")], snapshot=snap)
        assert out is not None
        assert "DELL" in out

    def test_broker_unavailable_fails_closed(self):
        for snap in ({"ok": False, "status": "expired"},
                     {"ok": False, "status": "missing"}):
            out = self._pending([self._rec(status="live_sent")], snapshot=snap)
            assert "DELL" in out, snap

    def test_newest_row_wins_per_symbol(self):
        # 최신 행이 terminal(blocked)이면 그 아래 옛 live_sent는 무시
        records = [
            self._rec(status="blocked", created_delta_min=-1),
            self._rec(status="live_sent", created_delta_min=-60,
                      pilot_id="tlive_x_0"),
        ]
        snap = {"ok": True, "status": "fresh", "usable_for_decisions": True,
                "broker_orders": [
            {"symbol": "DELL", "side": "BUY", "broker_order_status": "OPEN"}]}
        out = self._pending(records, snapshot=snap)
        assert out == {}   # 최신 상태(terminal) 기준 — stale live_sent 무시

    def test_no_verification_preview_ttl(self):
        # verification 없음: TTL 이내 차단, 초과 허용
        fresh = self._pending([self._rec(created_delta_min=-10)], verification=None)
        assert "DELL" in fresh
        old = self._pending([self._rec(created_delta_min=-120)], verification=None)
        assert old == {}

    def test_repeat_calls_do_not_mutate_db(self, tmp_path, monkeypatch):
        import sqlite3
        from db import store
        from core import toss_live_pilot_ledger as ledger

        monkeypatch.setattr(store, "DB_DIR", tmp_path)
        monkeypatch.setattr(ledger, "_schema_created", False)
        assert ledger.list_live_pilot_records(limit=1) == []
        db_path = tmp_path / "toss_live_pilot.db"

        def count():
            c = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
            try:
                return c.execute(
                    "SELECT COUNT(*) FROM live_pilot_ledger"
                ).fetchone()[0]
            finally:
                c.close()

        before_count = count()
        before_bytes = db_path.read_bytes()
        with patch(
            "core.toss_readonly_snapshot.load_snapshot",
            return_value={
                "ok": True,
                "status": "fresh",
                "usable_for_decisions": True,
                "broker_orders": [],
            },
        ):
            for _ in range(3):
                assert dd._pending_toss_order_symbols() == {}

        assert count() == before_count
        assert db_path.read_bytes() == before_bytes
