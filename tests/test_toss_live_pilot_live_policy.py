"""tests/test_toss_live_pilot_live_policy.py

compute_toss_live_pilot_policy env gate 테스트.
- 기본 env 없음 → disabled
- 1/2개 env만 true → disabled
- 3개 env 모두 true → enabled 가능
- 종목 제한 해제 → blocked_symbols/live_allowed_symbols 빈 목록
"""

import unittest
from unittest.mock import patch
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.toss_live_pilot_policy import compute_toss_live_pilot_policy, check_symbol_allowed

_ALL_GATES_ENV = {
    "TOSS_LIVE_PILOT_ENABLED": "true",
    "TOSS_LIVE_ORDER_ALLOWED": "true",
    "TOSS_LIVE_ADAPTER_ENABLED": "true",
}


# ─── 1. 기본값 (env 없음) ─────────────────────────────────

class TestDefaultDisabled(unittest.TestCase):
    def setUp(self):
        # 혹시 env가 설정되어 있을 경우 제거
        self._patch = patch.dict(os.environ, {
            "TOSS_LIVE_PILOT_ENABLED": "",
            "TOSS_LIVE_ORDER_ALLOWED": "",
            "TOSS_LIVE_ADAPTER_ENABLED": "",
        })
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_live_order_allowed_false(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertFalse(policy["live_order_allowed"])

    def test_adapter_status_disabled(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertEqual(policy["adapter_status"], "disabled")

    def test_live_pilot_enabled_false(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertFalse(policy["live_pilot_enabled"])

    def test_all_gates_open_false(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertFalse(policy["all_live_gates_open"])

    def test_env_flags_all_false(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertFalse(policy["env_live_pilot_enabled"])
        self.assertFalse(policy["env_live_order_allowed"])
        self.assertFalse(policy["env_live_adapter_enabled"])


# ─── 2. env 1개만 true ───────────────────────────────────

class TestOneGateOnly(unittest.TestCase):
    def test_only_pilot_enabled(self):
        with patch.dict(os.environ, {
            "TOSS_LIVE_PILOT_ENABLED": "true",
            "TOSS_LIVE_ORDER_ALLOWED": "",
            "TOSS_LIVE_ADAPTER_ENABLED": "",
        }):
            policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertFalse(policy["live_order_allowed"])
        self.assertEqual(policy["adapter_status"], "disabled")

    def test_only_order_allowed(self):
        with patch.dict(os.environ, {
            "TOSS_LIVE_PILOT_ENABLED": "",
            "TOSS_LIVE_ORDER_ALLOWED": "true",
            "TOSS_LIVE_ADAPTER_ENABLED": "",
        }):
            policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertFalse(policy["live_order_allowed"])
        self.assertEqual(policy["adapter_status"], "disabled")

    def test_only_adapter_enabled(self):
        with patch.dict(os.environ, {
            "TOSS_LIVE_PILOT_ENABLED": "",
            "TOSS_LIVE_ORDER_ALLOWED": "",
            "TOSS_LIVE_ADAPTER_ENABLED": "true",
        }):
            policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertFalse(policy["live_order_allowed"])
        self.assertEqual(policy["adapter_status"], "disabled")


# ─── 3. env 2개만 true ───────────────────────────────────

class TestTwoGatesOnly(unittest.TestCase):
    def test_pilot_and_order_only(self):
        with patch.dict(os.environ, {
            "TOSS_LIVE_PILOT_ENABLED": "true",
            "TOSS_LIVE_ORDER_ALLOWED": "true",
            "TOSS_LIVE_ADAPTER_ENABLED": "",
        }):
            policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertFalse(policy["live_order_allowed"])
        self.assertEqual(policy["adapter_status"], "disabled")

    def test_pilot_and_adapter_only(self):
        with patch.dict(os.environ, {
            "TOSS_LIVE_PILOT_ENABLED": "true",
            "TOSS_LIVE_ORDER_ALLOWED": "",
            "TOSS_LIVE_ADAPTER_ENABLED": "true",
        }):
            policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertFalse(policy["live_order_allowed"])
        self.assertEqual(policy["adapter_status"], "disabled")


# ─── 4. 3개 gate 모두 true → enabled ─────────────────────

class TestAllGatesEnabled(unittest.TestCase):
    def setUp(self):
        self._patch = patch.dict(os.environ, _ALL_GATES_ENV)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_live_order_allowed_true(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertTrue(policy["live_order_allowed"])

    def test_adapter_status_enabled(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertEqual(policy["adapter_status"], "enabled")

    def test_live_pilot_enabled_true(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertTrue(policy["live_pilot_enabled"])

    def test_all_gates_open_true(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertTrue(policy["all_live_gates_open"])

    def test_requires_user_confirmation(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertTrue(policy["requires_user_confirmation"])

    def test_requires_second_confirmation(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertTrue(policy["requires_second_confirmation"])

    def test_max_order_krw_set(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertGreater(policy["max_order_krw"], 0)

    def test_max_daily_krw_set(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertGreater(policy["max_daily_krw"], 0)

    def test_max_orders_per_day_set(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertGreaterEqual(policy["max_orders_per_day"], 1)


# ─── 5. blocked_symbols (종목 제한 해제) ──────────────────

class TestBlockedSymbols(unittest.TestCase):
    def test_blocked_symbols_empty_after_unlock(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertEqual(policy["blocked_symbols"], [])

    def test_check_symbol_mu_allowed(self):
        result = check_symbol_allowed("MU")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["blocks"], [])

    def test_check_symbol_005930_allowed(self):
        result = check_symbol_allowed("005930.KS")
        self.assertTrue(result["allowed"])

    def test_check_symbol_091180_allowed(self):
        result = check_symbol_allowed("091180.KS")
        self.assertTrue(result["allowed"])


# ─── 6. live_allowed_symbols (화이트리스트 해제) ──────────

class TestLiveAllowedSymbols(unittest.TestCase):
    def test_live_allowed_symbols_present(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertIn("live_allowed_symbols", policy)

    def test_live_allowed_symbols_empty_after_unlock(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertEqual(policy["live_allowed_symbols"], [])


# ─── 7. 민감정보 없음 ────────────────────────────────────

class TestNoSensitiveInPolicy(unittest.TestCase):
    def test_no_sensitive_keys_in_policy(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        policy_str = str(policy)
        for kw in ("accountNo", "Bearer", "APP_KEY", "APP_SECRET", "KIS_APP"):
            self.assertNotIn(kw, policy_str)


if __name__ == "__main__":
    unittest.main()
