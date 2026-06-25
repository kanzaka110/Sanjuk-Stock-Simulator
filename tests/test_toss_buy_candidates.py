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


def test_toss_buy_candidates_over_limit_excluded(monkeypatch):
    sections = _sections(new=[_new_cand("222.KS", "고가주", price=500_000)])
    _patch_sections(monkeypatch, sections)

    result = dd.toss_buy_candidates_data(range_="today")

    assert "222.KS" not in {i["symbol"] for i in result["items"]}
    reasons = " ".join(e.get("reason", "") for e in result["excluded"])
    assert "한도" in reasons


def test_toss_buy_candidates_us_excluded(monkeypatch):
    sections = _sections(
        new=[_new_cand("XYZ", "미국주", market="US", price=50.0)], market="US")
    _patch_sections(monkeypatch, sections)

    result = dd.toss_buy_candidates_data(range_="today")

    assert "XYZ" not in {i["symbol"] for i in result["items"]}
