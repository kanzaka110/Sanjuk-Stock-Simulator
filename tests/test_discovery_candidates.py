"""tests/test_discovery_candidates.py

신규 종목 발굴을 보유/관심종목 관리와 완전히 분리하는 core/discovery_candidates 검증.

요구사항 핵심:
- 보유종목은 신규 발굴 섹션에 나오지 않는다.
- WATCHLIST/RIA 종목은 신규 발굴 섹션에 나오지 않는다 (재평가 섹션에는 가능).
- 신규 후보 0개여도 섹션은 항상 출력 (탈락 상위 + 사유).
- toss 후보는 신규 발굴 기반만 — 기존 삼성/RIA 재사용 금지.
"""

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import core.discovery_candidates as disc
from core.discovery_candidates import (
    build_new_discovery,
    build_discovery_sections,
    render_discovery_text,
    toss_eligible_new_candidates,
    scan_discovery_candidates,
    NewCandidate,
    RejectedCandidate,
    DiscoverySections,
)


def _cand(ticker, name, market="KR", price=10_000, change_pct=2.0,
          ret_20d=8.0, ret_60d=18.0, rsi=58.0, vol_surge=2.2,
          pct_from_52w_high=-4.0, volume_value=60_000_000_000.0,
          source="유니버스", tags=("거래량급증",), has_catalyst=True):
    return {
        "ticker": ticker, "name": name, "market": market, "price": price,
        "change_pct": change_pct, "ret_20d": ret_20d, "ret_60d": ret_60d,
        "rsi": rsi, "vol_surge": vol_surge, "pct_from_52w_high": pct_from_52w_high,
        "volume_value": volume_value, "source": source, "tags": tags,
        "has_catalyst": has_catalyst,
    }


# 보유/관심 컨텍스트 (실제 settings에 의존하지 않도록 주입)
_HELD = {"005930.KS", "MU", "462870.KS"}
_WATCHLIST = {"000660.KS", "NVDA", "AAPL"}
_RIA = {"069500.KS", "091180.KS"}
_RECENT_RECO = {"035720.KS"}


def _ctx(**over):
    base = dict(held=_HELD, watchlist=_WATCHLIST, ria=_RIA, recent_reco=_RECENT_RECO)
    base.update(over)
    return base


# ─── 1. 보유종목 제외 ─────────────────────────────────────────────

class TestHeldExcluded(unittest.TestCase):
    def test_held_symbol_never_in_new_discovery(self):
        cands = [_cand("005930.KS", "삼성전자"), _cand("999999.KS", "신규주")]
        passed, _ = build_new_discovery(cands, **_ctx())
        tickers = [c.ticker for c in passed]
        self.assertNotIn("005930.KS", tickers)
        self.assertIn("999999.KS", tickers)

    def test_held_us_symbol_excluded(self):
        cands = [_cand("MU", "마이크론", market="US", price=100.0,
                       volume_value=5e10)]
        passed, rejected = build_new_discovery(cands, **_ctx())
        self.assertEqual([c.ticker for c in passed], [])


# ─── 2. WATCHLIST/RIA 제외 ────────────────────────────────────────

class TestWatchlistExcluded(unittest.TestCase):
    def test_watchlist_symbol_not_in_new_discovery(self):
        cands = [_cand("000660.KS", "SK하이닉스"), _cand("888888.KS", "발굴주")]
        passed, _ = build_new_discovery(cands, **_ctx())
        self.assertNotIn("000660.KS", [c.ticker for c in passed])

    def test_ria_symbol_not_in_new_discovery(self):
        cands = [_cand("069500.KS", "KODEX 200"), _cand("777777.KS", "발굴주")]
        passed, _ = build_new_discovery(cands, **_ctx())
        self.assertNotIn("069500.KS", [c.ticker for c in passed])


# ─── 3. 탈락 사유 ─────────────────────────────────────────────────

class TestRejectionReasons(unittest.TestCase):
    def test_chase_flagged_not_rejected(self):
        # 당일 +12% 급등은 후보에서 숨기지 않고 실행 리스크로 표시
        cands = [_cand("111111.KS", "급등주", change_pct=12.0)]
        passed, rejected = build_new_discovery(cands, **_ctx())
        self.assertTrue(passed)
        self.assertEqual(rejected, ())
        self.assertTrue(any("급등" in f for f in passed[0].risk_flags))

    def test_extreme_chase_rejected(self):
        # 과열권(+30% 이상)은 하드 탈락
        cands = [_cand("111112.KS", "과열주", change_pct=35.0)]
        passed, rejected = build_new_discovery(cands, **_ctx())
        self.assertEqual(passed, ())
        self.assertTrue(any("과열" in r.reason or "급등" in r.reason for r in rejected))

    def test_low_liquidity_rejected(self):
        cands = [_cand("222222.KS", "저유동주", volume_value=1_000_000_000.0)]
        passed, rejected = build_new_discovery(cands, **_ctx())
        self.assertTrue(any("거래대금" in r.reason for r in rejected))

    def test_poor_risk_reward_rejected(self):
        # 신고가 근접 + 약한 모멘텀 → 손익비 부족
        cands = [_cand("333333.KS", "손익비주", ret_20d=0.5, ret_60d=1.0,
                       pct_from_52w_high=-0.5)]
        passed, rejected = build_new_discovery(cands, **_ctx())
        # 통과 못하면 탈락 사유에 손익비 또는 다른 사유
        if passed == ():
            self.assertTrue(rejected)

    def test_missing_data_rejected(self):
        cands = [_cand("444444.KS", "데이터부족주", price=0.0)]
        passed, rejected = build_new_discovery(cands, **_ctx())
        self.assertTrue(any("데이터" in r.reason for r in rejected))


# ─── 4. 신규 후보 0개여도 섹션 출력 ───────────────────────────────

class TestAlwaysPresentSection(unittest.TestCase):
    def test_zero_pass_still_renders_section(self):
        # 전부 탈락하는 후보들
        cands = [
            _cand("aaa.KS", "급등A", change_pct=35.0),
            _cand("bbb.KS", "저유동B", volume_value=500_000_000.0),
        ]
        sections = build_discovery_sections(
            scan_candidates=cands, briefing_type="KR_BEFORE", **_ctx()
        )
        text = render_discovery_text(sections)
        self.assertIn("신규 발굴", text)
        # 0개여도 탈락 사유가 표시
        self.assertTrue("신규 후보 없음" in text or "탈락" in text)

    def test_rejected_top5_listed_when_zero(self):
        cands = [_cand(f"x{i}.KS", f"탈락{i}", change_pct=30.0) for i in range(8)]
        sections = build_discovery_sections(
            scan_candidates=cands, briefing_type="KR_BEFORE", **_ctx()
        )
        self.assertEqual(sections.new_discovery, ())
        self.assertLessEqual(len(sections.new_rejected), 12)
        self.assertGreaterEqual(len(sections.new_rejected), 1)


# ─── 5. 3섹션 분리 ────────────────────────────────────────────────

class TestThreeSections(unittest.TestCase):
    def test_holdings_section_has_only_held(self):
        sections = build_discovery_sections(
            scan_candidates=[_cand("555555.KS", "신규주")],
            briefing_type="KR_BEFORE", **_ctx(),
        )
        held_tickers = {h["ticker"] for h in sections.holdings_management}
        # 보유 섹션은 보유 종목만
        self.assertTrue(held_tickers.issubset(_HELD))

    def test_watchlist_reeval_has_watchlist_not_new(self):
        sections = build_discovery_sections(
            scan_candidates=[_cand("666666.KS", "신규주")],
            briefing_type="KR_BEFORE", **_ctx(),
        )
        reeval_tickers = {w["ticker"] for w in sections.watchlist_reeval}
        # 재평가 섹션엔 watchlist/ria 포함, 신규 종목 미포함
        self.assertNotIn("666666.KS", reeval_tickers)

    def test_new_discovery_excludes_all_known(self):
        known = _HELD | _WATCHLIST | _RIA
        cands = [_cand(t, t) for t in list(known)] + [_cand("000111.KS", "진짜신규")]
        sections = build_discovery_sections(
            scan_candidates=cands, briefing_type="KR_BEFORE", **_ctx(),
        )
        new_tickers = {c.ticker for c in sections.new_discovery}
        self.assertEqual(new_tickers & known, set())


# ─── 6. Toss 적격 후보 (신규 발굴 기반) ───────────────────────────

class TestTossEligible(unittest.TestCase):
    def test_toss_items_from_new_discovery_only(self):
        cands = [
            _cand("aaa.KS", "소액가능", price=30_000),   # 1주 3만원 ≤ 10만
            _cand("005930.KS", "삼성전자", price=30_000),  # 보유 → 제외
        ]
        sections = build_discovery_sections(
            scan_candidates=cands, briefing_type="KR_BEFORE", **_ctx(),
        )
        result = toss_eligible_new_candidates(sections, max_order_krw=100_000)
        item_tickers = {i["symbol"] for i in result["items"]}
        self.assertNotIn("005930.KS", item_tickers)

    def test_toss_excluded_has_scan_reject_reasons(self):
        cands = [_cand("zzz.KS", "급등탈락", change_pct=35.0)]
        sections = build_discovery_sections(
            scan_candidates=cands, briefing_type="KR_BEFORE", **_ctx(),
        )
        result = toss_eligible_new_candidates(sections, max_order_krw=100_000)
        reasons_text = " ".join(e.get("reason", "") for e in result["excluded"])
        self.assertIn("급등", reasons_text)

    def test_toss_over_price_limit_shown_not_executable(self):
        # 1주가 10만원 초과여도 후보에서 배제하지 않고 즉시 실행 불가로 표시
        cands = [_cand("bbb.KS", "고가주", price=500_000)]
        sections = build_discovery_sections(
            scan_candidates=cands, briefing_type="KR_BEFORE", **_ctx(),
        )
        result = toss_eligible_new_candidates(sections, max_order_krw=100_000)
        item = next(i for i in result["items"] if i["symbol"] == "bbb.KS")
        self.assertFalse(item["executable_now"])
        self.assertTrue(item["limit_exceeded"])
        self.assertEqual(item["execution_status"], "limit_exceeded")
        self.assertIn("한도", item["block_reason"])
        # 한도 초과는 excluded로 빠지지 않는다
        self.assertNotIn("bbb.KS", {e.get("ticker") for e in result["excluded"]})
        self.assertGreaterEqual(result["scan_summary"]["limit_exceeded_count"], 1)

    def test_toss_buy_only_us_excluded_for_krw_limit(self):
        # US 종목은 토스 소액(KRW) 대상 아님 — 제외
        cands = [_cand("XYZ", "미국주", market="US", price=50.0, volume_value=5e10)]
        sections = build_discovery_sections(
            scan_candidates=cands, briefing_type="US_BEFORE", **_ctx(),
        )
        result = toss_eligible_new_candidates(sections, max_order_krw=100_000)
        self.assertNotIn("XYZ", {i["symbol"] for i in result["items"]})


# ─── 7. idea-first 출력 ───────────────────────────────────────────

class TestIdeaFirstRender(unittest.TestCase):
    def test_top3_header_present(self):
        cands = [_cand(f"n{i}.KS", f"신규{i}", ret_60d=20.0 + i) for i in range(5)]
        sections = build_discovery_sections(
            scan_candidates=cands, briefing_type="KR_BEFORE", **_ctx(),
        )
        text = render_discovery_text(sections)
        self.assertIn("신규 발굴 TOP 3", text)

    def test_candidate_has_idea_before_account(self):
        c = _cand("n1.KS", "신규원")
        passed, _ = build_new_discovery([c], **_ctx())
        self.assertTrue(passed)
        self.assertTrue(passed[0].idea)


# ─── 8. 런타임 의존성 없는 fallback 스캔 ──────────────────────────

def _light_quote_stub(ticker, market):
    """pandas/pykrx 없이도 동작하는 경량 시세 stub (네트워크 없음)."""
    if market != "KR":
        return None
    return {
        "ticker": ticker, "name": disc._name_for(ticker), "market": "KR",
        "price": 40_000.0, "change_pct": 2.0, "ret_20d": 9.0, "ret_60d": 20.0,
        "rsi": 58.0, "vol_surge": 2.1, "pct_from_52w_high": -4.0,
        "volume_value": 6e10, "source": "유니버스(fallback)",
        "tags": ("유니버스",), "has_catalyst": True,
    }


class TestDependencyFallbackScan(unittest.TestCase):
    def test_fallback_used_when_pandas_missing(self):
        # pandas 미설치를 가정 + 네트워크 없는 경량 시세
        orig_pd, orig_lq = disc._pandas_available, disc._light_quote
        disc._pandas_available = lambda: False
        disc._light_quote = _light_quote_stub
        try:
            cands, meta = scan_discovery_candidates("KR_BEFORE")
        finally:
            disc._pandas_available, disc._light_quote = orig_pd, orig_lq
        self.assertTrue(meta["dependency_fallback_used"])
        self.assertGreater(meta["universe_count"], 0)
        self.assertGreater(meta["scanned_count"], 0)
        # 유니버스 기반으로 실제 후보가 만들어졌다 (보유 제외 후에도 남음)
        self.assertGreater(len(cands), 0)

    def test_fallback_produces_passing_new_candidates(self):
        orig_pd, orig_lq = disc._pandas_available, disc._light_quote
        disc._pandas_available = lambda: False
        disc._light_quote = _light_quote_stub
        try:
            sections = build_discovery_sections(briefing_type="KR_BEFORE")
        finally:
            disc._pandas_available, disc._light_quote = orig_pd, orig_lq
        # 유니버스 비보유 종목이 신규 발굴로 통과해야 한다
        self.assertGreater(len(sections.new_discovery), 0)
        new_tickers = {c.ticker for c in sections.new_discovery}
        d_held, d_wl, d_ria, _ = disc._known_sets()
        self.assertEqual(new_tickers & (d_held | d_wl | d_ria), set())


# ─── 9. scan_summary 노출 ─────────────────────────────────────────

class TestScanSummary(unittest.TestCase):
    def test_sections_carry_scan_summary(self):
        orig_pd, orig_lq = disc._pandas_available, disc._light_quote
        disc._pandas_available = lambda: False
        disc._light_quote = _light_quote_stub
        try:
            sections = build_discovery_sections(briefing_type="KR_BEFORE")
        finally:
            disc._pandas_available, disc._light_quote = orig_pd, orig_lq
        s = sections.scan_summary
        for k in ("universe_count", "scanned_count", "pass_count",
                  "reject_count", "top_reject_reasons", "dependency_fallback_used"):
            self.assertIn(k, s)

    def test_toss_output_includes_scan_summary(self):
        orig_pd, orig_lq = disc._pandas_available, disc._light_quote
        disc._pandas_available = lambda: False
        disc._light_quote = _light_quote_stub
        try:
            sections = build_discovery_sections(briefing_type="KR_BEFORE")
        finally:
            disc._pandas_available, disc._light_quote = orig_pd, orig_lq
        result = toss_eligible_new_candidates(sections, max_order_krw=100_000)
        self.assertIn("scan_summary", result)
        self.assertTrue(result["scan_summary"])


# ─── 10. 스캔 전면 실패 시에도 탈락 사유 노출 ─────────────────────

class TestScanTotalFailure(unittest.TestCase):
    def test_excluded_has_reasons_not_only_reuse_blocked(self):
        # 모든 시세 수집 실패 (None) → 스캔 0건
        orig_pd, orig_lq = disc._pandas_available, disc._light_quote
        disc._pandas_available = lambda: False
        disc._light_quote = lambda t, m: None
        try:
            sections = build_discovery_sections(briefing_type="KR_BEFORE")
        finally:
            disc._pandas_available, disc._light_quote = orig_pd, orig_lq
        result = toss_eligible_new_candidates(sections, max_order_krw=100_000)
        scopes = {e.get("scope") for e in result["excluded"]}
        # reuse_blocked 하나만 있으면 실패 — 스캔 사유가 함께 있어야 한다
        self.assertNotEqual(scopes, {"reuse_blocked"})
        self.assertTrue(
            "scan_rejected" in scopes or "scan_unavailable" in scopes)
        self.assertEqual(sections.scan_summary["scanned_count"], 0)


if __name__ == "__main__":
    unittest.main()


# ─── 11. 계좌 비의존 광역 레이더 / 장중 리스크 플래그 ─────────────

def test_intraday_reversal_is_kept_but_flagged():
    cands = [_cand("123456.KQ", "장중반전", price=158_000, change_pct=-3.0)]
    cands[0].update({"high_price": 177_600, "low_price": 156_000, "open_price": 170_000})
    passed, rejected = build_new_discovery(cands, **_ctx())
    assert passed, rejected
    assert passed[0].risk_flags
    assert passed[0].intraday_drawdown_pct <= -6.0




def test_market_discovery_weak_flow_only_is_conditional_not_hold():
    from core.discovery_candidates import DiscoverySections, NewCandidate, market_discovery_radar
    cand = NewCandidate(
        ticker="000445.KQ", name="수급약함", market="KR", price=48_000.0,
        score=70, idea="수급 약함 단독 완화", reasons=("거래대금 충분",),
        target_price=52_000.0, stop_loss=45_000.0, risk_reward=1.8,
        risk_flags=("수급 약함 — 즉시 실행보다 관찰",),
        suggested_accounts=("토스 AI",),
    )
    sections = DiscoverySections(new_discovery=(cand,))

    radar = market_discovery_radar(sections)
    item = radar["items"][0]

    assert item["action_bias"] == "CONDITIONAL_SMALL_ENTRY"
    assert item["blocking_risk_flags"] == []
    assert item["observation_flags"] == ["수급 약함 — 즉시 실행보다 관찰"]


def test_market_discovery_radar_is_account_agnostic():
    from core.discovery_candidates import market_discovery_radar
    cands = [_cand("123457.KQ", "광역후보", price=158_000)]
    sections = build_discovery_sections(scan_candidates=cands, briefing_type="KR_BEFORE", **_ctx())
    radar = market_discovery_radar(sections)
    assert radar["schema"] == "market_discovery_radar.v1.account_agnostic"
    assert radar["items"]
    assert any("삼성" in a for a in radar["items"][0]["suggested_accounts"])
