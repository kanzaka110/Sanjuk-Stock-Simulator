"""tests/test_toss_autonomous_sell_guard.py

SELL 주문 가드 테스트 — 보유수량 확인, autonomous side 체크.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest


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


def _sell_preview(symbol="NVDA", qty=1, price=200.0, **extra):
    base = {
        "ok": True,
        "symbol": symbol,
        "side": "sell",
        "quantity": qty,
        "limit_price": price,
        "estimated_amount_krw": price * qty,
        "blocks": [],
        "live_order_sent": False,
    }
    base.update(extra)
    return base


class TestSellSideGuard:
    """autonomous_allowed_sides에 sell이 없으면 차단."""

    def test_sell_blocked_when_buy_only(self):
        from core.toss_live_pilot_adapter import can_send_live_pilot_order
        policy = _make_policy(autonomous_allowed_sides=["buy"])
        ok, reasons = can_send_live_pilot_order(
            policy, _sell_preview(), {"ok": True, "live_order_sent": False},
        )
        assert ok is False
        assert any("autonomous_side_not_allowed" in r for r in reasons)

    def test_sell_allowed_when_buy_sell(self):
        from core.toss_live_pilot_adapter import can_send_live_pilot_order
        policy = _make_policy(autonomous_allowed_sides=["buy", "sell"])
        ok, reasons = can_send_live_pilot_order(
            policy, _sell_preview(), {"ok": True, "live_order_sent": False},
        )
        # autonomous_side_not_allowed는 없어야 함
        side_reasons = [r for r in reasons if "autonomous_side" in r]
        assert len(side_reasons) == 0


class TestStopLossNotRequiredForSell:
    """SELL은 stop_loss 불필요."""

    def test_sell_no_stop_loss_ok(self):
        from core.toss_live_pilot_adapter import can_send_live_pilot_order
        preview = _sell_preview()
        # stop_loss 없음
        assert "stop_loss" not in preview or not preview.get("stop_loss")
        ok, reasons = can_send_live_pilot_order(
            _make_policy(), preview, {"ok": True, "live_order_sent": False},
        )
        stop_reasons = [r for r in reasons if "stop_loss" in r]
        assert len(stop_reasons) == 0


class TestBuyStopLossRequired:
    """BUY는 stop_loss 필수 (autonomous mode)."""

    def test_buy_without_stop_loss_blocked(self):
        from core.toss_live_pilot_adapter import can_send_live_pilot_order
        preview = {
            "ok": True,
            "symbol": "NVDA",
            "side": "buy",
            "quantity": 1,
            "limit_price": 190.0,
            "estimated_amount_krw": 190_000,
            "blocks": [],
            "live_order_sent": False,
            # stop_loss 없음
        }
        ok, reasons = can_send_live_pilot_order(
            _make_policy(), preview, {"ok": True, "live_order_sent": False},
        )
        assert ok is False
        assert any("stop_loss" in r for r in reasons)

    def test_buy_with_stop_loss_passes(self):
        from core.toss_live_pilot_adapter import can_send_live_pilot_order
        preview = {
            "ok": True,
            "symbol": "NVDA",
            "side": "buy",
            "quantity": 1,
            "limit_price": 190.0,
            "estimated_amount_krw": 190_000,
            "blocks": [],
            "live_order_sent": False,
            "stop_loss": 180.0,
        }
        ok, reasons = can_send_live_pilot_order(
            _make_policy(), preview, {"ok": True, "live_order_sent": False},
        )
        stop_reasons = [r for r in reasons if "stop_loss" in r]
        assert len(stop_reasons) == 0
