"""tests/test_toss_live_pilot_policy.py

승인형 Toss Live Pilot 정책 모듈 테스트.
"""

import unittest
from unittest.mock import patch

import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.toss_live_pilot_policy import (
    compute_toss_live_pilot_policy,
    check_symbol_allowed,
)


class TestDefaultPolicy(unittest.TestCase):
    """기본값 — env 없음, evaluated_count=0."""

    def setUp(self):
        self.policy = compute_toss_live_pilot_policy(evaluated_count=0)

    def test_live_pilot_enabled_false(self):
        self.assertFalse(self.policy["live_pilot_enabled"])

    def test_live_order_allowed_false(self):
        self.assertFalse(self.policy["live_order_allowed"])

    def test_requires_user_confirmation(self):
        self.assertTrue(self.policy["requires_user_confirmation"])

    def test_requires_second_confirmation(self):
        self.assertTrue(self.policy["requires_second_confirmation"])

    def test_adapter_disabled(self):
        self.assertEqual(self.policy["adapter_status"], "disabled")

    def test_mode(self):
        self.assertEqual(self.policy["mode"], "approval_only_live_pilot")

    def test_reason_present(self):
        self.assertIn("비활성", self.policy["reason"])

    def test_max_daily_krw(self):
        self.assertLessEqual(self.policy["max_daily_krw"], 300_000)

    def test_max_orders_per_day(self):
        self.assertEqual(self.policy["max_orders_per_day"], 1)


class TestEnvNotSet(unittest.TestCase):
    """env TOSS_LIVE_PILOT_ENABLED 미설정 → live pilot blocked."""

    def test_no_env_live_pilot_disabled(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("TOSS_LIVE_PILOT_ENABLED", None)
            policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertFalse(policy["live_pilot_enabled"])
        self.assertFalse(policy["live_order_allowed"])
        self.assertIn("block_reason", policy)

    def test_env_true_still_adapter_disabled(self):
        """env=true 여도 이번 단계에서는 adapter disabled."""
        with patch.dict("os.environ", {"TOSS_LIVE_PILOT_ENABLED": "true"}):
            policy = compute_toss_live_pilot_policy(evaluated_count=0)
        # live_pilot_enabled는 False 유지 (이번 단계)
        self.assertFalse(policy["live_order_allowed"])
        self.assertEqual(policy["adapter_status"], "disabled")


class TestSampleInsufficient(unittest.TestCase):
    """evaluated_count < 5 → 초보수 모드."""

    def test_max_order_krw_insufficient(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertLessEqual(policy["max_order_krw"], 100_000)

    def test_warning_present(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=2)
        self.assertTrue(any("표본부족" in w for w in policy["warnings"]))

    def test_sample_insufficient_flag(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=3)
        self.assertTrue(policy["sample_insufficient"])

    def test_stable_sample_higher_budget(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=10)
        self.assertGreater(policy["max_order_krw"], 100_000)
        self.assertFalse(policy["sample_insufficient"])


class TestBlockedSymbols(unittest.TestCase):
    """위험 종목 차단."""

    def test_161510_blocked(self):
        r = check_symbol_allowed("161510.KS")
        self.assertFalse(r["allowed"])
        self.assertTrue(any("위험" in b or "저신뢰" in b for b in r["blocks"]))

    def test_005930_blocked(self):
        r = check_symbol_allowed("005930.KS")
        self.assertFalse(r["allowed"])
        self.assertTrue(any("anomaly" in b or "price" in b for b in r["blocks"]))

    def test_069500_preferred(self):
        r = check_symbol_allowed("069500.KS")
        self.assertTrue(r["allowed"])
        self.assertTrue(r["preferred"])
        self.assertEqual(r["blocks"], [])

    def test_sofi_not_blocked(self):
        r = check_symbol_allowed("SOFI")
        self.assertTrue(r["allowed"])
        self.assertFalse(r["preferred"])


class TestPreferredSymbols(unittest.TestCase):
    def test_preferred_list_contains_069500(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertIn("069500.KS", policy["preferred_symbols"])

    def test_blocked_list_contains_danger_symbols(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertIn("161510.KS", policy["blocked_symbols"])
        self.assertIn("005930.KS", policy["blocked_symbols"])


class TestNoSensitiveInfo(unittest.TestCase):
    def test_no_secret_in_policy(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        text = str(policy)
        for kw in ("APP_SECRET", "APP_KEY", "accountNo", "Bearer "):
            self.assertNotIn(kw, text)


if __name__ == "__main__":
    unittest.main()
