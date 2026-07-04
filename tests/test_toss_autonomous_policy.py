"""tests/test_toss_autonomous_policy.py

Autonomous mode policy 계산 테스트.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest


# ── helpers ───────────────────────────────────────────────────────

_BASE_ENV = {
    "TOSS_LIVE_PILOT_ENABLED": "true",
    "TOSS_LIVE_ORDER_ALLOWED": "true",
    "TOSS_LIVE_ADAPTER_ENABLED": "true",
}

_AUTONOMOUS_ENV = {
    **_BASE_ENV,
    "TOSS_AUTONOMOUS_MODE": "true",
}


def _policy(**env_overrides):
    env = {**_AUTONOMOUS_ENV, **env_overrides}
    with patch.dict(os.environ, env, clear=False):
        from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
        return compute_toss_live_pilot_policy(evaluated_count=10)


# ── 기본 모드 테스트 ──────────────────────────────────────────────

class TestDefaultApprovalMode:
    """TOSS_AUTONOMOUS_MODE 미설정 → 기존 승인 모드."""

    def test_default_is_approval_mode(self):
        with patch.dict(os.environ, _BASE_ENV, clear=False):
            from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
            p = compute_toss_live_pilot_policy(evaluated_count=10)
        assert p["mode"] == "approval_only_live_pilot"
        assert p["requires_user_confirmation"] is True
        assert p["requires_second_confirmation"] is True
        assert p["autonomous_mode"] is False

    def test_autonomous_false_explicit(self):
        p = _policy(TOSS_AUTONOMOUS_MODE="false")
        assert p["mode"] == "approval_only_live_pilot"
        assert p["autonomous_mode"] is False


# ── Autonomous 모드 테스트 ────────────────────────────────────────

class TestAutonomousMode:

    def test_autonomous_enabled(self):
        p = _policy()
        assert p["mode"] == "autonomous_live_pilot"
        assert p["requires_user_confirmation"] is False
        assert p["requires_second_confirmation"] is False
        assert p["autonomous_mode"] is True
        assert p["live_order_allowed"] is True

    def test_kill_switch_blocks_orders(self):
        p = _policy(TOSS_AUTONOMOUS_KILL_SWITCH="true")
        assert p["autonomous_mode"] is True
        assert p["autonomous_kill_switch"] is True
        assert p["live_order_allowed"] is False
        assert "kill_switch" in p["block_reason"]

    def test_kill_switch_reverts_to_approval(self):
        p = _policy(TOSS_AUTONOMOUS_KILL_SWITCH="true")
        assert p["mode"] == "approval_only_live_pilot"
        assert p["requires_user_confirmation"] is True

    def test_autonomous_without_gates_disabled(self):
        p = _policy(TOSS_LIVE_PILOT_ENABLED="false")
        assert p["autonomous_mode"] is True
        assert p["adapter_status"] == "disabled"
        assert p["live_order_allowed"] is False


# ── Asset type 테스트 ─────────────────────────────────────────────

class TestAutonomousAssetTypes:

    def test_default_us_stock_only(self):
        p = _policy()
        assert "US_STOCK" in p["autonomous_allowed_asset_types"]

    def test_kr_stock_added(self):
        p = _policy(TOSS_AUTONOMOUS_ALLOWED_ASSET_TYPES="US_STOCK,KR_STOCK")
        assert "US_STOCK" in p["allowed_asset_types"]
        assert "KR_STOCK" in p["allowed_asset_types"]
        assert "KR_STOCK" in p["autonomous_allowed_asset_types"]

    def test_effective_asset_types_merged(self):
        """autonomous asset types가 기본 allowed에 병합됨."""
        p = _policy(TOSS_AUTONOMOUS_ALLOWED_ASSET_TYPES="KR_STOCK")
        assert "US_STOCK" in p["allowed_asset_types"]
        assert "KR_STOCK" in p["allowed_asset_types"]


# ── Autonomous limit 기본값 테스트 ────────────────────────────────

class TestAutonomousLimits:

    def test_default_kr_limits_removed(self):
        # 2026-07-04 사용자 승인으로 금액 cap 제거 — 0 = 한도 없음
        p = _policy()
        assert p["autonomous_kr_max_order_krw"] == 0
        assert p["autonomous_kr_max_daily_buy_krw"] == 0

    def test_default_us_limits_removed(self):
        p = _policy()
        assert p["autonomous_us_max_order_usd"] == 0

    def test_default_weight(self):
        p = _policy()
        assert p["autonomous_symbol_max_weight_pct"] == 15

    def test_custom_kr_limit(self):
        p = _policy(TOSS_AUTONOMOUS_KR_MAX_ORDER_KRW="300000")
        assert p["autonomous_kr_max_order_krw"] == 300_000

    def test_custom_us_limit(self):
        p = _policy(TOSS_AUTONOMOUS_US_MAX_ORDER_USD="500")
        assert p["autonomous_us_max_order_usd"] == 500


# ── Allowed sides 테스트 ──────────────────────────────────────────

class TestAutonomousSides:

    def test_default_buy_sell(self):
        p = _policy()
        assert "buy" in p["autonomous_allowed_sides"]
        assert "sell" in p["autonomous_allowed_sides"]

    def test_buy_only(self):
        p = _policy(TOSS_AUTONOMOUS_ALLOWED_SIDES="BUY")
        assert p["autonomous_allowed_sides"] == ["buy"]

    def test_sell_only(self):
        p = _policy(TOSS_AUTONOMOUS_ALLOWED_SIDES="SELL")
        assert p["autonomous_allowed_sides"] == ["sell"]


# ── classify_asset_type 테스트 ────────────────────────────────────

class TestClassifyAssetType:

    def test_kr_stock(self):
        from core.toss_live_pilot_policy import classify_asset_type
        assert classify_asset_type("005930.KS") == "KR_STOCK"
        assert classify_asset_type("091160.KQ") == "KR_STOCK"

    def test_us_stock(self):
        from core.toss_live_pilot_policy import classify_asset_type
        assert classify_asset_type("NVDA") == "US_STOCK"
        assert classify_asset_type("LMT") == "US_STOCK"

    def test_empty(self):
        from core.toss_live_pilot_policy import classify_asset_type
        assert classify_asset_type("") == "US_STOCK"
