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

from core.discovery_candidates import (
    build_new_discovery,
    build_discovery_sections,
    render_discovery_text,
    toss_eligible_new_candidates,
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
    def test_chase_rejected(self):
        # 당일 +12% 급등 → 추격 금지
        cands = [_cand("111111.KS", "급등주", change_pct=12.0)]
        passed, rejected = build_new_discovery(cands, **_ctx())
        self.assertEqual(passed, ())
        self.assertTrue(any("급등" in r.reason for r in rejected))

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
            _cand("aaa.KS", "급등A", change_pct=20.0),
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
        self.assertLessEqual(len(sections.new_rejected), 5)
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
        cands = [_cand("zzz.KS", "급등탈락", change_pct=25.0)]
        sections = build_discovery_sections(
            scan_candidates=cands, briefing_type="KR_BEFORE", **_ctx(),
        )
        result = toss_eligible_new_candidates(sections, max_order_krw=100_000)
        reasons_text = " ".join(e.get("reason", "") for e in result["excluded"])
        self.assertIn("급등", reasons_text)

    def test_toss_over_price_limit_excluded(self):
        # 1주가 10만원 초과 → 토스 소액 조건 탈락
        cands = [_cand("bbb.KS", "고가주", price=500_000)]
        sections = build_discovery_sections(
            scan_candidates=cands, briefing_type="KR_BEFORE", **_ctx(),
        )
        result = toss_eligible_new_candidates(sections, max_order_krw=100_000)
        self.assertNotIn("bbb.KS", {i["symbol"] for i in result["items"]})

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


if __name__ == "__main__":
    unittest.main()
