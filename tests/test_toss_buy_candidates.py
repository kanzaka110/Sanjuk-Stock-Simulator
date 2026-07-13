"""tests/test_toss_buy_candidates.py

/api/toss/buy-candidates는 기존 삼성/RIA/관심 추천(predictions DB)을 재사용하지
않고, core.discovery_candidates의 신규 발굴 후보 중 토스 소액 조건을 통과한
후보만 노출한다. items가 0이어도 excluded에 '기존 후보 제외' + '신규 스캔 탈락
이유'가 함께 담긴다.
"""

import sys
from pathlib import Path

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
    # 기본 테스트는 계좌 현금 충분 상태로 고정한다. 현금 부족 회귀는 별도 테스트가 override.
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
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
    sections = _sections(new=[_new_cand("222.KS", "고가주", price=600_000)])
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
    # 한도 이내 후보는 executable_now=True / limit_exceeded=False.
    sections = _sections(new=[_new_cand("000111.KS", "소액주", price=30_000)])
    _patch_sections(monkeypatch, sections)

    result = dd.toss_buy_candidates_data(range_="today")

    item = next(i for i in result["items"] if i["symbol"] == "000111.KS")
    assert item["executable_now"] is True
    assert item["limit_exceeded"] is False
    assert item["execution_status"] == "executable"


def test_toss_buy_candidates_154700_executable(monkeypatch):
    # 원익IPS급 154,700원 후보 — 1회 한도 50만원 이내라 즉시 실행 가능.
    sections = _sections(new=[_new_cand("240810.KS", "원익IPS", price=154_700)])
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



def test_toss_buy_candidates_stock_agent_review_fields(monkeypatch):
    # Stock-Agent가 PASS/HOLD/BLOCK을 판단할 수 있도록 주문표 핵심 필드를 명시한다.
    sections = _sections(new=[_new_cand("000111.KS", "소액주", price=30_000)])
    _patch_sections(monkeypatch, sections)

    result = dd.toss_buy_candidates_data(range_="today")

    assert result["schema"] == "toss_buy_candidates.v2.stock_agent_ready"
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
    sections = _sections(new=[_new_cand("222.KS", "고가주", price=600_000)])
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
    monkeypatch.setattr(qg, "score_candidates_batch", lambda items, **kwargs: items)

    item = dd.toss_buy_candidates_data(range_="today")["items"][0]

    assert not item.get("decision_bucket")
    assert item["stock_agent_ready"] is False
    assert item["executable_now"] is False
    assert item["block_reason"] == "quality_gate_decision_missing"


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

    assert "income_strategy" in item
    assert item["income_strategy"]["income_pass"] is True
    assert item["income_strategy"]["expected_pnl_krw"] > 0
    assert item["stock_agent_ready"] is True


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
    strong = _new_cand("000778.KS", "고수익후보", price=50_000, score=88)
    strong = strong.__class__(
        **{**strong.__dict__, "target_price": 58_000.0, "stop_loss": 47_000.0, "risk_reward": 2.6}
    )
    sections = _sections(new=[strong])
    _patch_sections(monkeypatch, sections)
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})

    result = dd.toss_buy_candidates_data(range_="today")
    item = next(i for i in result["items"] if i["symbol"] == "000778.KS")

    assert item["original_stop_loss"] == 47_000.0
    assert item["stop_loss"] > 47_000.0
    assert item["income_exit_plan"]["stop_risk_pct"] <= 4.5
    assert item["income_strategy"]["income_pass"] is True
    assert item["stock_agent_ready"] is True



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
        return float((i.get("income_strategy") or {}).get("expected_pnl_krw") or 0)

    top3_symbols = {i["symbol"] for i in sorted(items, key=_expected, reverse=True)[:3]}
    assert len(ready_symbols) == 3
    assert ready_symbols == top3_symbols
    assert result["scan_summary"]["portfolio_income_ready_cap"] == 3
    assert result["scan_summary"]["portfolio_cap_block_count"] == 1
    blocked = [i for i in items if i.get("execution_status") == "portfolio_income_cap"]
    assert len(blocked) == 1


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
