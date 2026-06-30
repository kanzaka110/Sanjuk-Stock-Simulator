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


def _patch_sections(monkeypatch, sections):
    monkeypatch.setattr(dd, "_cache", {}, raising=False)
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
    assert item["quantity"] == 1
    assert item["estimated_amount_krw"] == 30_000.0
    assert item["stop_loss"] == 28_200.0
    assert item["target_price"] == 31_500.0
    assert item["current_vs_limit_gap_pct"] == 0.0
    assert "즉시체결" in item["fill_risk_note"]
    assert item["condition"]
    assert item["execution_gate"] == "Hermes PASS + 승호 최종 승인 필요"
    assert item["read_only_notice"].startswith("GET-only")
    assert item["missing_fields"] == []
    assert item["stock_agent_ready"] is True


def test_toss_buy_candidates_limit_exceeded_not_stock_agent_ready(monkeypatch):
    sections = _sections(new=[_new_cand("222.KS", "고가주", price=600_000)])
    _patch_sections(monkeypatch, sections)

    result = dd.toss_buy_candidates_data(range_="today")

    item = next(i for i in result["items"] if i["symbol"] == "222.KS")
    assert item["limit_exceeded"] is True
    assert item["missing_fields"] == []
    assert item["stock_agent_ready"] is False
    assert "한도" in " ".join(item["risk_notes"])





def test_toss_candidate_with_weak_flow_only_becomes_conditional_small_entry(monkeypatch):
    weak = _new_cand("000444.KQ", "수급약함", price=48_000)
    weak = weak.__class__(
        **{**weak.__dict__,
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
