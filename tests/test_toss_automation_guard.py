"""
Toss 자동거래 가드레일 테스트

모든 경우에서 allowed_for_live=False인지 검증.
"""

from __future__ import annotations

from unittest.mock import patch

from core.toss_automation_guard import evaluate_toss_order_candidate
from config import toss_automation as cfg


def _base_candidate(**overrides) -> dict:
    c = {
        "symbol": "005930.KS",
        "side": "buy",
        "quantity": 2,
        "limit_price": 80000,
        "estimated_amount_krw": 160000,
        "confidence": 0.8,
        "quote_age_sec": 10,
    }
    c.update(overrides)
    return c


def _base_account(**overrides) -> dict:
    a = {
        "cash": {"krw": 10_000_000},
        "holdings_count": 0,
    }
    a.update(overrides)
    return a


def _base_stats(**overrides) -> dict:
    s = {"count": 0, "daily_amount_krw": 0}
    s.update(overrides)
    return s


class TestLiveAlwaysBlocked:
    """이번 단계에서 live는 항상 불가."""

    def test_live_blocked_default(self):
        r = evaluate_toss_order_candidate(_base_candidate(), _base_account(), _base_stats())
        assert r["allowed_for_live"] is False
        assert r["dry_run"] is True

    def test_live_blocked_even_with_good_candidate(self):
        r = evaluate_toss_order_candidate(
            _base_candidate(confidence=0.99),
            _base_account(cash={"krw": 50_000_000}),
            _base_stats(),
        )
        assert r["allowed_for_live"] is False


class TestKillSwitch:
    def test_kill_switch_in_reasons(self):
        r = evaluate_toss_order_candidate(_base_candidate(), _base_account(), _base_stats())
        assert "kill_switch_on" in r["reasons"]


class TestTelegramApproval:
    def test_telegram_required_in_reasons(self):
        r = evaluate_toss_order_candidate(_base_candidate(), _base_account(), _base_stats())
        assert "telegram_approval_required" in r["reasons"]


class TestMaxOrder:
    def test_within_limit(self):
        r = evaluate_toss_order_candidate(
            _base_candidate(estimated_amount_krw=200_000),
            _base_account(), _base_stats(),
        )
        assert "max_order_exceeded" not in r["reasons"]
        assert r["allowed_for_paper"] is True

    def test_exceeds_limit(self):
        r = evaluate_toss_order_candidate(
            _base_candidate(estimated_amount_krw=500_000),
            _base_account(), _base_stats(),
        )
        assert "max_order_exceeded" in r["reasons"]
        assert r["allowed_for_paper"] is False


class TestDailyBudget:
    def test_within_daily(self):
        r = evaluate_toss_order_candidate(
            _base_candidate(estimated_amount_krw=200_000),
            _base_account(),
            _base_stats(daily_amount_krw=700_000),
        )
        assert "daily_budget_exceeded" not in r["reasons"]

    def test_exceeds_daily(self):
        r = evaluate_toss_order_candidate(
            _base_candidate(estimated_amount_krw=200_000),
            _base_account(),
            _base_stats(daily_amount_krw=900_000),
        )
        assert "daily_budget_exceeded" in r["reasons"]
        assert r["allowed_for_paper"] is False


class TestCashBuffer:
    def test_sufficient_cash(self):
        r = evaluate_toss_order_candidate(
            _base_candidate(estimated_amount_krw=200_000),
            _base_account(cash={"krw": 5_000_000}),
            _base_stats(),
        )
        assert "cash_buffer_breach" not in r["reasons"]

    def test_insufficient_cash(self):
        r = evaluate_toss_order_candidate(
            _base_candidate(estimated_amount_krw=200_000),
            _base_account(cash={"krw": 2_100_000}),
            _base_stats(),
        )
        assert "cash_buffer_breach" in r["reasons"]


class TestBlacklist:
    def test_blacklisted_symbol(self):
        r = evaluate_toss_order_candidate(
            _base_candidate(symbol="MU"),
            _base_account(), _base_stats(),
        )
        assert "symbol_blacklisted" in r["reasons"]
        assert r["allowed_for_paper"] is False

    def test_non_blacklisted(self):
        r = evaluate_toss_order_candidate(
            _base_candidate(symbol="AAPL"),
            _base_account(), _base_stats(),
        )
        assert "symbol_blacklisted" not in r["reasons"]


class TestWhitelist:
    def test_empty_whitelist_blocks_live_not_paper(self):
        """whitelist 비어있으면 live 차단, paper는 허용."""
        r = evaluate_toss_order_candidate(
            _base_candidate(), _base_account(), _base_stats(),
        )
        # live는 항상 차단이므로 여기서는 paper만 확인
        assert r["allowed_for_paper"] is True


class TestQuoteStale:
    def test_fresh_quote(self):
        r = evaluate_toss_order_candidate(
            _base_candidate(quote_age_sec=10),
            _base_account(), _base_stats(),
        )
        assert "quote_stale" not in r["reasons"]

    def test_stale_quote(self):
        r = evaluate_toss_order_candidate(
            _base_candidate(quote_age_sec=600),
            _base_account(), _base_stats(),
        )
        assert "quote_stale" in r["reasons"]
        assert r["allowed_for_paper"] is False


class TestConfidence:
    def test_high_confidence(self):
        r = evaluate_toss_order_candidate(
            _base_candidate(confidence=0.9),
            _base_account(), _base_stats(),
        )
        assert "low_confidence" not in r["reasons"]

    def test_low_confidence(self):
        r = evaluate_toss_order_candidate(
            _base_candidate(confidence=0.3),
            _base_account(), _base_stats(),
        )
        assert "low_confidence" in r["reasons"]
        assert r["allowed_for_paper"] is False


class TestMaxPositions:
    def test_within_limit(self):
        r = evaluate_toss_order_candidate(
            _base_candidate(),
            _base_account(holdings_count=3),
            _base_stats(),
        )
        assert "max_positions_reached" not in r["reasons"]

    def test_at_limit(self):
        r = evaluate_toss_order_candidate(
            _base_candidate(),
            _base_account(holdings_count=5),
            _base_stats(),
        )
        assert "max_positions_reached" in r["reasons"]
