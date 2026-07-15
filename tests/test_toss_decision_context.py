"""
Toss 판단 컨텍스트 테스트

- 값 포함/마스킹 검증
- 실패 시 warning degrade
- live_orders_allowed=false
- included_in_total_portfolio=false
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest


@pytest.fixture(autouse=True)
def _context_tests_run_as_owner(monkeypatch):
    """(2026-07-15 계약) 비소유 프로세스는 브로커 직접 조회가 차단된다.

    이 파일은 브로커 응답 → decision context 가공 로직을 검증하므로
    owner를 명시한다. 운영 경로는 consumer로서 snapshot을 소비한다.
    """
    monkeypatch.setenv("TOSS_PROCESS_ROLE", "broker_owner")

from core.toss_decision_context import get_toss_decision_context, context_to_briefing_text, format_toss_live_pilot_briefing_lessons


def _mock_context(**overrides):
    """mock된 toss_client로 컨텍스트 생성."""
    accounts = [{"accountSeq": 1, "accountType": "BROKERAGE", "accountNo": "99900001234"}]
    holdings = {"items": [], "marketValue": {"amount": {"krw": "0", "usd": None}}}
    bp_krw = {"currency": "KRW", "cashBuyingPower": "10000000"}
    bp_usd = {"currency": "USD", "cashBuyingPower": "5.67"}
    fx = {"baseCurrency": "USD", "quoteCurrency": "KRW", "rate": "1539.0"}
    cal = {"today": {"date": "2026-06-23"}}

    with patch("core.toss_decision_context._cache_data", None), \
         patch("core.toss_decision_context._cache_ts", 0.0), \
         patch("core.toss_client.is_configured", return_value=overrides.get("configured", True)), \
         patch("core.toss_client.get_accounts", return_value=overrides.get("accounts", accounts)), \
         patch("core.toss_client.get_holdings", return_value=overrides.get("holdings", holdings)), \
         patch("core.toss_client.get_buying_power", side_effect=lambda seq, cur: bp_krw if cur == "KRW" else bp_usd), \
         patch("core.toss_client.get_exchange_rate", return_value=overrides.get("fx", fx)), \
         patch("core.toss_client.get_market_calendar", return_value=overrides.get("cal", cal)), \
         patch("core.toss_client.sanitize_dict", side_effect=lambda x: x):
        return get_toss_decision_context()


class TestDecisionContext:
    def test_enabled(self):
        ctx = _mock_context()
        assert ctx["enabled"] is True

    def test_not_configured(self):
        ctx = _mock_context(configured=False)
        assert ctx["enabled"] is False

    def test_included_in_portfolio_false(self):
        ctx = _mock_context()
        assert ctx["included_in_total_portfolio"] is False

    def test_cash_krw(self):
        ctx = _mock_context()
        assert ctx["cash_krw"] == 10000000.0

    def test_cash_usd(self):
        ctx = _mock_context()
        assert ctx["cash_usd"] == 5.67

    def test_total_value(self):
        ctx = _mock_context()
        assert ctx["total_account_value_krw"] == 10000000.0

    def test_usdkrw(self):
        ctx = _mock_context()
        assert ctx["usdkrw"] == 1539.0

    def test_automation_live_false(self):
        ctx = _mock_context()
        assert ctx["automation"]["live_orders_allowed"] is False

    def test_automation_kill_switch(self):
        ctx = _mock_context()
        assert ctx["automation"]["kill_switch"] is True

    def test_data_quality_flags(self):
        ctx = _mock_context()
        dq = ctx["data_quality"]
        assert dq["toss_available"] is True
        assert dq["cash_available"] is True
        assert dq["fx_available"] is True

    def test_no_account_no_in_output(self):
        ctx = _mock_context()
        s = str(ctx)
        assert "99900001234" not in s

    def test_empty_accounts(self):
        ctx = _mock_context(accounts=[])
        assert ctx["cash_krw"] == 0
        assert "계좌 목록 조회 실패" in ctx["data_quality"]["warnings"]


class TestBriefingText:
    def test_contains_key_info(self):
        ctx = _mock_context()
        text = context_to_briefing_text(ctx)
        assert "Toss 실전 AI 자동거래 계좌" in text
        assert "기존 포트폴리오 미합산" in text
        assert "10,000,000" in text
        assert "paper" in text

    def test_no_live_order_wording(self):
        ctx = _mock_context()
        text = context_to_briefing_text(ctx)
        assert "실제 주문 아님" in text

    def test_disabled_returns_empty(self):
        ctx = _mock_context(configured=False)
        text = context_to_briefing_text(ctx)
        assert text == ""

    def test_no_account_number(self):
        ctx = _mock_context()
        text = context_to_briefing_text(ctx)
        assert "99900001234" not in text


class TestLivePilotBriefingLessons:
    def test_lessons_include_sellable_polling_rule(self):
        events = [
            {"symbol": "BBAI", "side": "sell", "event_type": "live_send_failed", "live_order_sent": False, "reason": "http_422"},
            {"symbol": "BBAI", "side": "buy", "event_type": "live_sent", "live_order_sent": True, "reason": ""},
        ]
        policy = {
            "side_mode": "BUY_SELL",
            "allowed_sides": ["buy", "sell"],
            "sell_allowed": True,
            "transport": {"live_order_sent_possible": True},
        }
        with patch("core.toss_live_pilot_events.list_events", return_value=events), \
             patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy", return_value=policy), \
             patch("core.toss_live_transport.get_transport_status", return_value={"live_order_sent_possible": True}):
            text = format_toss_live_pilot_briefing_lessons()
        assert "BUY_SELL" in text
        assert "매도가능수량" in text
        assert "수동 매도 여부를 묻지" in text
        assert "BBAI sell" in text

    def test_lessons_degrade_empty_on_import_error(self):
        with patch.dict("sys.modules", {"core.toss_live_pilot_events": None}):
            text = format_toss_live_pilot_briefing_lessons()
        assert isinstance(text, str)
