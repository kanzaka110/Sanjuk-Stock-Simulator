"""tests/test_income_briefing.py

수입 중심 브리핑 결정론 payload 테스트.

핵심 불변식:
- Toss/삼성 계좌 합산 금지, Toss 자동 / 삼성 수동 완전 분리
- 실현수입 None은 0으로 바뀌지 않는다 (산출불가)
- 오늘 평가변동은 실현수입으로 이동하지 않는다
- LLM fallback(normalized=None)에서도 KPI/보유관리는 존재, 가짜 티켓 없음
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import core.income_briefing as ib


# ─── fixtures ─────────────────────────────────────────────────────

_TOSS_SUMMARY = {
    "holdings_count": 20,
    "profit_loss": {"krw": -120_000},
    "today_profit_loss": {"krw": 15_000},
    "realized_profit_loss": {"krw": None},
    "cash": {"krw_native": 3_840_000, "usd": 0.85},
    "total_account_value": {"krw": 9_490_000},
    "error": "",
}

_PORTFOLIO = {
    "total_asset": 146_000_000,
    "total_cash": 30_600_000,
    "today_pnl_krw": 970_000,
    "holdings_as_of": "2026-07-03",
    "accounts": [
        {"name": "일반", "asset_total": 50_000_000, "cash": 10_000_000,
         "pnl_krw": 1_000_000, "pnl_pct": 2.0, "today_pnl_krw": 300_000,
         "items": [
             {"ticker": "005930.KS", "name": "삼성전자", "pnl_pct": -9.5,
              "day_pct": -1.0, "eval_krw": 5_000_000, "horizon": "중기"},
             {"ticker": "MSFT", "name": "마이크로소프트", "pnl_pct": 20.0,
              "day_pct": 0.5, "eval_krw": 4_000_000, "horizon": "단기"},
         ]},
        {"name": "RIA", "asset_total": 40_000_000, "cash": 5_000_000,
         "pnl_krw": 500_000, "pnl_pct": 1.5, "today_pnl_krw": 200_000,
         "items": [
             {"ticker": "069500.KS", "name": "KODEX200", "pnl_pct": 20.0,
              "day_pct": 0.2, "eval_krw": 30_000_000, "horizon": "장기 적립"},
         ]},
    ],
}

_BUYS = {"items": [
    {"symbol": "AAA.KS", "name": "준비후보", "stock_agent_ready": True,
     "price": 10_000, "limit_price": 10_000, "quantity": 10,
     "estimated_amount_krw": 100_000, "risk_reward": 2.0,
     "target_price": 11_000, "stop_loss": 9_600,
     "execution_status": "executable",
     "income_strategy": {"income_pass": True, "expected_pnl_krw": 8_000,
                         "income_edge_ratio": 0.08}},
    {"symbol": "BBB.KS", "name": "차단후보", "stock_agent_ready": False,
     "execution_status": "cash_unavailable",
     "income_strategy": {"income_pass": False}},
    {"symbol": "CCC.KS", "name": "레디지만수입미달", "stock_agent_ready": True,
     "income_strategy": {"income_pass": False}},
]}

_PLAN = {
    "portfolio_rebalance_required": False,
    "funding_rebalance_required": True,
    "funding_currency": "USD",
    "funding_target": {"symbol": "MS", "estimated_amount_native": 222.13,
                       "expected_pnl_krw": 7_769.96},
    "holdings_count": 20,
    "sell_to_fund_candidates": [
        {"symbol": "XOM", "name": "엑슨모빌", "quantity": 1,
         "estimated_release_krw": 207_579.7, "auto_sell_eligible": True,
         "ai_berkshire": {"classification": "trim"},
         "auto_sell_block_reason": None, "funding_target_symbol": "MS"},
    ],
}

_EVENTS = {"records": [
    {"symbol": "006400.KS", "side": "sell", "status": "live_sent",
     "reason": "position_review_sell", "created_at": "2026-07-10T11:43:00+09:00",
     "broker_order_id": "SECRET-123", "account_no": "9999999999"},
]}


def _build(briefing_type="KR_OPEN", pf=None, toss=None, plan=None, buys=None):
    with patch("core.dashboard_data.toss_account_summary",
               return_value=dict(toss if toss is not None else _TOSS_SUMMARY)), \
         patch("core.dashboard_data.portfolio_data",
               return_value=dict(pf if pf is not None else _PORTFOLIO)), \
         patch("core.dashboard_data.toss_buy_candidates_data",
               return_value=dict(buys if buys is not None else _BUYS)), \
         patch("core.dashboard_data.toss_rebalance_plan_data",
               return_value=dict(plan if plan is not None else _PLAN)), \
         patch("core.dashboard_data.toss_live_pilot_events_data",
               return_value=dict(_EVENTS)), \
         patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
               return_value={"autonomous_mode": True, "autonomous_kill_switch": False}):
        return ib.build_income_briefing_context(briefing_type)


_NOW_LIVE = "2026-07-03"  # holdings_as_of와 같은 날 → live 판정용


def _fresh_pf() -> dict:
    from datetime import datetime, timedelta, timezone
    kst = timezone(timedelta(hours=9))
    pf = dict(_PORTFOLIO)
    pf["holdings_as_of"] = datetime.now(kst).strftime("%Y-%m-%d")
    return pf


# ─── 1~3. KPI 불변식 ─────────────────────────────────────────────

class TestKpiInvariants(unittest.TestCase):
    def test_toss_and_samsung_never_merged(self):
        p = _build()
        toss = p["income_kpi"]["toss"]
        ss = p["income_kpi"]["samsung"]
        # 서로 다른 계좌 총액이 그대로 분리 유지 — 합산 필드 자체가 없다
        self.assertEqual(toss["total_account_value_krw"], 9_490_000)
        self.assertEqual(ss["total_asset_krw"], 146_000_000)
        self.assertNotIn("combined", p["income_kpi"])
        self.assertNotIn("total", p["income_kpi"])

    def test_realized_none_stays_none_not_zero(self):
        p = _build()
        self.assertIsNone(p["income_kpi"]["toss"]["realized_income_krw"])
        self.assertIsNone(p["income_kpi"]["samsung"]["realized_income_krw"])
        self.assertEqual(p["income_kpi"]["toss"]["realized_income_status"], "unavailable")

    def test_today_unrealized_not_copied_into_realized(self):
        p = _build()
        toss = p["income_kpi"]["toss"]
        ss = p["income_kpi"]["samsung"]
        self.assertEqual(toss["today_unrealized_krw"], 15_000)
        self.assertEqual(ss["today_unrealized_krw"], 970_000)
        self.assertIsNone(toss["realized_income_krw"])
        self.assertIsNone(ss["realized_income_krw"])


# ─── 4~5. 삼성 stale / manual-only ────────────────────────────────

class TestSamsungStale(unittest.TestCase):
    def test_stale_holdings_block_manual_tickets(self):
        p = _build()  # holdings_as_of=2026-07-03 → 24h 초과 stale
        self.assertEqual(p["income_kpi"]["samsung"]["data_status"], "stale")
        normalized = {"executable_actions": [
            {"account": "[일반]", "ticker": "NEW.KS", "name": "새후보", "side": "매수",
             "limit_price": 10_000, "quantity": 5, "target_price": 11_000,
             "stop_loss": 9_600},
        ]}
        out = ib.finalize_income_briefing(p, normalized, "KR_OPEN")
        self.assertEqual(out["samsung"]["manual_income_tickets"], [])
        self.assertTrue(any("stale" in b["reason"] for b in out["samsung"]["blocked_tickets"]))

    def test_all_samsung_tickets_manual_only(self):
        p = _build(pf=_fresh_pf())
        normalized = {"executable_actions": [
            {"account": "[일반]", "ticker": "NEW.KS", "name": "새후보", "side": "매수",
             "limit_price": 10_000, "quantity": 5, "target_price": 11_000,
             "stop_loss": 9_600, "risk_reward": 2.5, "score": 80},
        ]}
        out = ib.finalize_income_briefing(p, normalized, "KR_OPEN")
        self.assertTrue(out["samsung"]["manual_only"])
        self.assertFalse(out["samsung"]["auto_execution"])
        for t in out["samsung"]["manual_income_tickets"]:
            self.assertTrue(t["manual_only"])
            self.assertFalse(t["auto_execution"])


# ─── 6~9. Toss 섹션 ──────────────────────────────────────────────

class TestTossSection(unittest.TestCase):
    def test_not_ready_candidates_excluded_from_ready_buys(self):
        p = _build()
        symbols = {r["symbol"] for r in p["toss"]["ready_buys"]}
        self.assertNotIn("BBB.KS", symbols)     # ready=false
        self.assertNotIn("CCC.KS", symbols)     # income_pass=false
        self.assertIn("AAA.KS", symbols)

    def test_ready_buy_has_expected_pnl_fields(self):
        p = _build()
        rb = p["toss"]["ready_buys"][0]
        self.assertEqual(rb["expected_pnl_krw"], 8_000)
        self.assertEqual(rb["income_edge_ratio"], 0.08)
        self.assertEqual(rb["automation"], "toss_autonomous")

    def test_rebalance_rendered_before_new_buys_in_telegram(self):
        p = ib.finalize_income_briefing(_build(), None, "KR_OPEN")
        text = "\n".join(ib.render_income_telegram(p))
        self.assertLess(text.index("보유 관리·현금 만들기"), text.index("Toss: 자동운영"))
        self.assertLess(text.index("리밸런싱"), text.index("자동 매수") if "자동 매수" in text else len(text))

    def test_funding_currency_target_and_release(self):
        p = _build()
        reb = p["toss"]["rebalance"]
        self.assertTrue(reb["funding_rebalance_required"])
        self.assertEqual(reb["funding_currency"], "USD")
        self.assertEqual(reb["funding_target"]["symbol"], "MS")
        self.assertEqual(reb["expected_release_krw"], 207_579.7)


# ─── 10~12. LLM fallback ─────────────────────────────────────────

class TestLlmFallback(unittest.TestCase):
    def test_fallback_keeps_kpi_and_position_management(self):
        p = ib.finalize_income_briefing(_build(), None, "KR_OPEN")
        self.assertIsNotNone(p["income_kpi"]["toss"]["total_account_value_krw"])
        self.assertTrue(p["samsung"]["position_management"])  # -9.5% 삼성전자 등
        self.assertTrue(p["toss"]["ready_buys"])

    def test_fallback_creates_no_samsung_tickets(self):
        p = ib.finalize_income_briefing(_build(pf=_fresh_pf()), None, "KR_OPEN")
        self.assertEqual(p["samsung"]["manual_income_tickets"], [])

    def test_toss_actions_stripped_from_normalized(self):
        normalized = {
            "executable_actions": [
                {"account": "[토스]", "ticker": "AAA.KS", "side": "매수"},
                {"account": "[일반]", "ticker": "BBB.KS", "side": "매수"},
            ],
            "conditional_buy_candidates": [
                {"account": "[토스 AI]", "ticker": "CCC.KS"},
            ],
        }
        out = ib.strip_toss_from_manual_normalized(normalized)
        self.assertEqual(len(out["executable_actions"]), 1)
        self.assertEqual(out["executable_actions"][0]["ticker"], "BBB.KS")
        self.assertEqual(out["conditional_buy_candidates"], [])
        self.assertEqual(out["toss_actions_stripped"], 2)
        # 원본 불변
        self.assertEqual(len(normalized["executable_actions"]), 2)


# ─── 13~16. 삼성 manual ticket 심사 ──────────────────────────────

class TestManualTicketJudge(unittest.TestCase):
    def _finalize(self, action, pf=None):
        p = _build(pf=pf if pf is not None else _fresh_pf())
        return ib.finalize_income_briefing(
            p, {"executable_actions": [action]}, "KR_OPEN")

    def test_already_held_buy_moves_to_position_management(self):
        action = {"account": "[일반]", "ticker": "005930.KS", "name": "삼성전자",
                  "side": "매수", "limit_price": 60_000, "quantity": 5,
                  "target_price": 66_000, "stop_loss": 57_500}
        out = self._finalize(action)
        self.assertEqual(out["samsung"]["manual_income_tickets"], [])
        pm = [r for r in out["samsung"]["position_management"]
              if r["symbol"] == "005930.KS" and r["status"] == "thesis_review"]
        self.assertTrue(pm)
        self.assertTrue(pm[0]["manual_only"])

    def test_income_fail_buy_blocked(self):
        # 손익비 1.0 → income gate 미달
        action = {"account": "[일반]", "ticker": "ZZZ.KS", "name": "저수익",
                  "side": "매수", "limit_price": 10_000, "quantity": 5,
                  "target_price": 10_200, "stop_loss": 9_800, "risk_reward": 1.0}
        out = self._finalize(action)
        self.assertEqual(out["samsung"]["manual_income_tickets"], [])
        self.assertTrue(any("income gate" in b["reason"]
                            for b in out["samsung"]["blocked_tickets"]))

    def test_ai_berkshire_avoid_buy_blocked(self):
        action = {"account": "[일반]", "ticker": "AVOID.KS", "name": "회피종목",
                  "side": "매수", "limit_price": 10_000, "quantity": 5,
                  "target_price": 12_000, "stop_loss": 9_600, "risk_reward": 2.5,
                  "score": 85}
        avoid_item = {"stored_classification": "avoid", "classification": "avoid",
                      "thesis_expired": False, "freshness_valid": True}
        with patch("core.ai_berkshire_toss.score_for_symbol", return_value=avoid_item):
            out = self._finalize(action)
        self.assertEqual(out["samsung"]["manual_income_tickets"], [])
        self.assertTrue(any("avoid" in b["reason"] for b in out["samsung"]["blocked_tickets"]))

    def test_expired_or_invalid_thesis_gives_hold(self):
        action = {"account": "[일반]", "ticker": "EXP.KS", "name": "만료종목",
                  "side": "매수", "limit_price": 10_000, "quantity": 20,
                  "target_price": 12_000, "stop_loss": 9_600, "risk_reward": 2.5,
                  "score": 85}
        expired_item = {"stored_classification": "hold", "classification": "gray_zone",
                        "thesis_expired": True, "freshness_valid": False}
        with patch("core.ai_berkshire_toss.score_for_symbol", return_value=expired_item):
            out = self._finalize(action)
        tickets = out["samsung"]["manual_income_tickets"]
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["verdict"], "HOLD")


# ─── 17. thesis 분류 ─────────────────────────────────────────────

class TestThesisSection(unittest.TestCase):
    def test_valid_expired_invalid_split(self):
        p = _build()
        thesis = p["thesis"]
        # 실제 repo JSON 기준 — 8종목 전부 유효 (2026-10-10까지)
        self.assertGreaterEqual(len(thesis["valid"]), 1)
        self.assertIsInstance(thesis["expired"], list)
        self.assertIsInstance(thesis["invalid"], list)
        for row in thesis["valid"]:
            self.assertTrue(row["freshness_valid"])


# ─── 18. US_CLOSE ────────────────────────────────────────────────

class TestUsClose(unittest.TestCase):
    def test_us_close_renders_no_future_actions(self):
        p = ib.finalize_income_briefing(_build("US_CLOSE"), None, "US_CLOSE")
        self.assertTrue(p["daily_review"])
        text = "\n".join(ib.render_income_telegram(p))
        self.assertNotIn("삼성: 수동 주문만", text)   # 미래 수동 후보 미렌더
        self.assertNotIn("자동 매수 준비", text)
        self.assertIn("오늘 실행 결과", text)
        self.assertIn("오늘 수입 계기판", text)

    def test_us_close_no_manual_tickets_even_with_normalized(self):
        normalized = {"executable_actions": [
            {"account": "[일반]", "ticker": "NEW.KS", "side": "매수",
             "limit_price": 10_000, "quantity": 5,
             "target_price": 11_000, "stop_loss": 9_600},
        ]}
        p = ib.finalize_income_briefing(_build("US_CLOSE", pf=_fresh_pf()),
                                        normalized, "US_CLOSE")
        self.assertEqual(p["samsung"]["manual_income_tickets"], [])


# ─── 19~21. 렌더 마커/일관성 ─────────────────────────────────────

class TestRenderers(unittest.TestCase):
    def _payload(self):
        return ib.finalize_income_briefing(_build(), None, "KR_OPEN")

    def test_telegram_required_markers(self):
        text = "\n".join(ib.render_income_telegram(self._payload()))
        for marker in ("오늘 수입 계기판", "실현수입: 산출불가", "오늘 평가변동",
                       "Toss: 자동운영", "삼성: 수동 주문만 · 자동실행 없음",
                       "실제 수입 아님"):
            self.assertIn(marker, text, marker)

    def test_html_required_markers(self):
        html = ib.render_income_html(self._payload())
        for marker in ("오늘 수입 계기판", "산출불가", "오늘 평가변동",
                       "자동운영", "수동 주문만", "실현수입 아님"):
            self.assertIn(marker, html, marker)

    def test_telegram_and_html_show_same_kpi_numbers(self):
        payload = self._payload()
        text = "\n".join(ib.render_income_telegram(payload))
        html = ib.render_income_html(payload)
        for value in ("9,490,000", "3,840,000", "146,000,000", "30,600,000",
                      "15,000", "970,000"):
            self.assertIn(value, text, value)
            self.assertIn(value, html, value)


# ─── 22~24. 보안/read-only 불변식 ────────────────────────────────

class TestSafetyInvariants(unittest.TestCase):
    def test_no_token_account_order_id_in_payload(self):
        import json
        p = ib.finalize_income_briefing(_build(), None, "KR_OPEN")
        blob = json.dumps(p, ensure_ascii=False, default=str)
        self.assertNotIn("SECRET-123", blob)      # broker_order_id 미복사
        self.assertNotIn("9999999999", blob)      # account_no 미복사
        for key in ("app_key", "app_secret", "access_token", "authorization"):
            self.assertNotIn(key, blob.lower())

    def test_module_uses_only_readonly_sources(self):
        src = Path(ib.__file__).read_text(encoding="utf-8")
        for forbidden in ("requests.post", "requests.put", "requests.delete",
                          "process_candidate", "dispatch_toss_order",
                          "try_autonomous_finalize", "send_simple_message",
                          "send_email"):
            self.assertNotIn(forbidden, src, forbidden)

    def test_transport_finalizer_untouched_by_this_feature(self):
        # income_briefing은 주문 실행 모듈을 import하지 않는다
        src = Path(ib.__file__).read_text(encoding="utf-8")
        for mod in ("toss_live_transport", "toss_autonomous_finalizer",
                    "toss_live_order_http"):
            self.assertNotIn(mod, src, mod)


if __name__ == "__main__":
    unittest.main()
