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
        self.assertEqual(self.policy["max_daily_krw"], 2_000_000)

    def test_max_orders_per_day_unlimited(self):
        # 주문 건수 제한 없음
        self.assertIsNone(self.policy["max_orders_per_day"])
        self.assertEqual(self.policy["max_orders_per_day_label"], "unlimited")
        self.assertFalse(self.policy["order_count_limited"])


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
    """evaluated_count < 5 → 표본부족 경고만 (한도는 고정)."""

    def test_max_order_krw_fixed_even_insufficient(self):
        # 표본부족이어도 1회 한도는 고정 50만원
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertEqual(policy["max_order_krw"], 500_000)

    def test_warning_present(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=2)
        self.assertTrue(any("표본부족" in w for w in policy["warnings"]))

    def test_sample_insufficient_flag(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=3)
        self.assertTrue(policy["sample_insufficient"])

    def test_stable_sample_same_fixed_cap(self):
        # 표본 충분해도 한도는 동일 고정 (목표금액/증액 개념 아님)
        policy = compute_toss_live_pilot_policy(evaluated_count=10)
        self.assertEqual(policy["max_order_krw"], 500_000)
        self.assertFalse(policy["sample_insufficient"])


class TestFinalLimitPolicy(unittest.TestCase):
    """최종 한도 정책: 1회 50만, 1일 상한(cap) 200만, 건수 제한 없음."""

    def setUp(self):
        self.policy = compute_toss_live_pilot_policy(evaluated_count=10)

    def test_max_order_krw_500k(self):
        self.assertEqual(self.policy["max_order_krw"], 500_000)

    def test_max_daily_krw_2m(self):
        self.assertEqual(self.policy["max_daily_krw"], 2_000_000)

    def test_daily_is_cap_not_target(self):
        self.assertTrue(self.policy["daily_krw_is_cap"])
        self.assertFalse(self.policy["daily_krw_is_target"])

    def test_order_count_unlimited(self):
        self.assertIsNone(self.policy["max_orders_per_day"])
        self.assertFalse(self.policy["order_count_limited"])

    def test_buy_only_and_sell_blocked(self):
        self.assertEqual(self.policy["side_mode"], "BUY_ONLY")
        self.assertEqual(self.policy["allowed_sides"], ["buy"])
        self.assertFalse(self.policy["sell_allowed"])

    def test_requires_user_confirmation_kept(self):
        self.assertTrue(self.policy["requires_user_confirmation"])
        self.assertTrue(self.policy["requires_second_confirmation"])

    def test_no_forbidden_target_phrases(self):
        # "목표 집행/한도 소진/남은 한도 사용/200만원 채우기/일일 목표금액" 의미 없음
        text = str(self.policy)
        for bad in ("목표 집행", "한도 소진", "남은 한도", "채우기", "목표금액"):
            self.assertNotIn(bad, text, f"금지 문구 발견: {bad}")

    def test_hold_is_normal_phrasing(self):
        # 후보 없으면 매수 없음/HOLD가 정상이라는 의미가 정책에 표현됨
        note = self.policy["daily_policy_note"]
        self.assertIn("목표 아님", note)
        self.assertIn("HOLD", note)


class TestBlockedSymbols(unittest.TestCase):
    """종목 제한 해제 — 블록목록 비활성, 모든 종목 symbol-guard 통과."""

    def test_161510_no_longer_blocked(self):
        r = check_symbol_allowed("161510.KS")
        self.assertTrue(r["allowed"])
        self.assertEqual(r["blocks"], [])

    def test_005930_no_longer_blocked(self):
        r = check_symbol_allowed("005930.KS")
        self.assertTrue(r["allowed"])
        self.assertEqual(r["blocks"], [])

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

    def test_blocked_list_empty_after_unlock(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertEqual(policy["blocked_symbols"], [])


class TestNoSensitiveInfo(unittest.TestCase):
    def test_no_secret_in_policy(self):
        policy = compute_toss_live_pilot_policy(evaluated_count=0)
        text = str(policy)
        for kw in ("APP_SECRET", "APP_KEY", "accountNo", "Bearer "):
            self.assertNotIn(kw, text)


if __name__ == "__main__":
    unittest.main()
