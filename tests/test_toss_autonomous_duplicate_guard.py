"""tests/test_toss_autonomous_duplicate_guard.py

중복 주문 방지 테스트.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

KST = timezone(timedelta(hours=9))


def _make_policy(**overrides):
    base = {
        "live_pilot_enabled": True,
        "live_order_allowed": True,
        "adapter_status": "enabled",
        "requires_user_confirmation": False,
        "requires_second_confirmation": False,
        "autonomous_mode": True,
        "allowed_asset_types": ["US_STOCK"],
        "allowed_sides": ["buy", "sell"],
        "autonomous_allowed_sides": ["buy", "sell"],
        "blocked_symbols": [],
        "max_order_krw": None,
        "max_daily_krw": None,
        "max_orders_per_day": None,
        "autonomous_us_max_order_usd": 1_000,
        "autonomous_kr_max_order_krw": 500_000,
        "autonomous_kr_max_daily_buy_krw": 1_500_000,
        "autonomous_symbol_max_weight_pct": 15,
    }
    base.update(overrides)
    return base


def _preview(symbol="NVDA", side="buy", qty=1, price=190.0, **extra):
    base = {
        "ok": True,
        "symbol": symbol,
        "side": side,
        "quantity": qty,
        "limit_price": price,
        "estimated_amount_krw": price * qty,
        "blocks": [],
        "live_order_sent": False,
        "stop_loss": 180.0,
    }
    base.update(extra)
    return base


class TestDuplicateSymbolSide:
    """같은 symbol+side 당일 중복 차단."""

    def test_duplicate_buy_blocked(self):
        today = datetime.now(KST).strftime("%Y-%m-%d")
        records = [{
            "status": "live_sent",
            "symbol": "NVDA",
            "side": "buy",
            "created_at": f"{today}T10:00:00+09:00",
            "estimated_amount_krw": 190_000,
        }]
        with patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=records):
            from core.toss_live_pilot_adapter import can_send_live_pilot_order
            ok, reasons = can_send_live_pilot_order(
                _make_policy(), _preview(), {"ok": True, "live_order_sent": False},
            )
        assert ok is False
        assert any("duplicate_symbol_side_today" in r for r in reasons)

    def test_different_side_allowed(self):
        """BUY 후 SELL은 중복이 아님."""
        today = datetime.now(KST).strftime("%Y-%m-%d")
        records = [{
            "status": "live_sent",
            "symbol": "NVDA",
            "side": "buy",
            "created_at": f"{today}T10:00:00+09:00",
            "estimated_amount_krw": 190_000,
        }]
        with patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=records):
            from core.toss_live_pilot_adapter import can_send_live_pilot_order
            ok, reasons = can_send_live_pilot_order(
                _make_policy(), _preview(side="sell"), {"ok": True, "live_order_sent": False},
            )
        # duplicate guard는 통과 (다른 가드에서 걸릴 수 있음)
        dup_reasons = [r for r in reasons if "duplicate" in r]
        assert len(dup_reasons) == 0

    def test_different_symbol_allowed(self):
        """다른 종목은 중복이 아님."""
        today = datetime.now(KST).strftime("%Y-%m-%d")
        records = [{
            "status": "live_sent",
            "symbol": "AAPL",
            "side": "buy",
            "created_at": f"{today}T10:00:00+09:00",
            "estimated_amount_krw": 190_000,
        }]
        with patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=records):
            from core.toss_live_pilot_adapter import can_send_live_pilot_order
            ok, reasons = can_send_live_pilot_order(
                _make_policy(), _preview(), {"ok": True, "live_order_sent": False},
            )
        dup_reasons = [r for r in reasons if "duplicate" in r]
        assert len(dup_reasons) == 0
