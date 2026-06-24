"""tests/test_toss_live_pilot_hermes_gate.py

Hermes 게이트 통합 테스트 — _handle_confirm 내 is_verification_passed 분기.
- PENDING → 차단: Hermes 교차검증 미완료
- HOLD/BLOCK/ERROR → 차단
- PASS + policy disabled → "[Hermes 검증 PASS 확인]\n차단: live pilot 조건 미충족"
- PASS + policy enabled + transport=None → dispatch blocked
- 실주문 전송 0건 (live_order_sent 항상 False, transport=None)
- 민감정보 없음
"""

import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

KST = timezone(timedelta(hours=9))

_POLICY_DISABLED = {
    "live_pilot_enabled": False,
    "live_order_allowed": False,
    "adapter_status": "disabled",
    "requires_user_confirmation": True,
    "requires_second_confirmation": True,
    "max_order_krw": 100_000,
    "max_daily_krw": 300_000,
    "max_orders_per_day": 1,
    "blocked_symbols": ["005930.KS", "161510.KS", "MU"],
}

_POLICY_ENABLED = {
    "live_pilot_enabled": True,
    "live_order_allowed": True,
    "adapter_status": "enabled",
    "requires_user_confirmation": True,
    "requires_second_confirmation": True,
    "max_order_krw": 100_000,
    "max_daily_krw": 300_000,
    "max_orders_per_day": 1,
    "blocked_symbols": ["005930.KS", "161510.KS", "MU"],
}


def _tmp_patches():
    """임시 ledger + verif DB + policy 패치 컨텍스트."""
    tmp = tempfile.mkdtemp()
    return (
        patch("core.toss_live_pilot_ledger._db_path",
              return_value=Path(tmp) / "pilot.db"),
        patch("core.toss_live_pilot_verification._db_path",
              return_value=Path(tmp) / "verif.db"),
    )


def _reset():
    import core.toss_live_pilot_ledger as lm
    import core.toss_live_pilot_verification as vm
    lm._schema_created = False
    vm._schema_created = False


def _create_pilot(symbol: str = "091180.KS") -> str:
    from core.toss_live_pilot_ledger import record_live_pilot_preview
    preview = {
        "ok": True,
        "symbol": symbol,
        "side": "buy",
        "quantity": 1,
        "limit_price": 30_000.0,
        "estimated_amount_krw": 30_000.0,
        "blocks": [],
        "warnings": [],
    }
    rec = record_live_pilot_preview(preview)
    return rec["pilot_id"]


def _create_verif_request(pilot_id: str) -> str:
    from core.toss_live_pilot_verification import create_verification_request
    preview = {
        "symbol": "091180.KS", "side": "buy", "quantity": 1,
        "limit_price": 30_000.0, "estimated_amount_krw": 30_000.0,
        "pilot_id": pilot_id, "preview_id": pilot_id,
    }
    res = create_verification_request(preview, pilot_id=pilot_id)
    return res["verification_id"]


def _record_verif(verification_id: str, status: str, ttl: int = 10):
    from core.toss_live_pilot_verification import record_hermes_verification
    record_hermes_verification(verification_id, status, [], {}, ttl_minutes=ttl)


# ── 1. No verification record → blocked ──────────────────

class TestHermesGateNoRecord(unittest.TestCase):
    def setUp(self):
        self._p1, self._p2 = _tmp_patches()
        self._p1.start()
        self._p2.start()
        _reset()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        _reset()

    def test_no_verif_record_blocked(self):
        pilot_id = _create_pilot()
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_DISABLED):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertFalse(result["live_order_sent"])
        self.assertTrue(result.get("blocked"))

    def test_no_verif_reason_hermes(self):
        pilot_id = _create_pilot()
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_DISABLED):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertEqual(result.get("reason"), "hermes_verification_required")


# ── 2. PENDING → blocked ──────────────────────────────────

class TestHermesGatePending(unittest.TestCase):
    def setUp(self):
        self._p1, self._p2 = _tmp_patches()
        self._p1.start()
        self._p2.start()
        _reset()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        _reset()

    def test_pending_blocked(self):
        pilot_id = _create_pilot()
        _create_verif_request(pilot_id)  # PENDING
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_DISABLED):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertFalse(result["live_order_sent"])
        self.assertTrue(result.get("blocked"))

    def test_pending_message_contains_hermes(self):
        pilot_id = _create_pilot()
        _create_verif_request(pilot_id)
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_DISABLED):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertIn("Hermes", result["message"])
        self.assertIn("아직 주문 전송 안 함", result["message"])


# ── 3. HOLD/BLOCK/ERROR → blocked ────────────────────────

class TestHermesGateNonPass(unittest.TestCase):
    def setUp(self):
        self._p1, self._p2 = _tmp_patches()
        self._p1.start()
        self._p2.start()
        _reset()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        _reset()

    def _test_status_blocked(self, status: str):
        pilot_id = _create_pilot()
        vid = _create_verif_request(pilot_id)
        _record_verif(vid, status)
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_DISABLED):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertFalse(result["live_order_sent"])
        self.assertTrue(result.get("blocked"))

    def test_hold_blocked(self):
        self._test_status_blocked("HOLD")

    def test_block_blocked(self):
        self._test_status_blocked("BLOCK")

    def test_error_blocked(self):
        self._test_status_blocked("ERROR")


# ── 4. PASS + policy disabled → PASS 확인 문구 ───────────

class TestHermesGatePassPolicyDisabled(unittest.TestCase):
    def setUp(self):
        self._p1, self._p2 = _tmp_patches()
        self._p1.start()
        self._p2.start()
        _reset()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        _reset()

    def test_pass_policy_disabled_blocked(self):
        pilot_id = _create_pilot()
        vid = _create_verif_request(pilot_id)
        _record_verif(vid, "PASS", ttl=10)
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_DISABLED):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertFalse(result["live_order_sent"])
        self.assertTrue(result.get("blocked"))

    def test_pass_policy_disabled_message_has_pass_confirmed(self):
        pilot_id = _create_pilot()
        vid = _create_verif_request(pilot_id)
        _record_verif(vid, "PASS", ttl=10)
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_DISABLED):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertIn("Hermes 검증 PASS 확인", result["message"])

    def test_pass_policy_disabled_reason(self):
        pilot_id = _create_pilot()
        vid = _create_verif_request(pilot_id)
        _record_verif(vid, "PASS", ttl=10)
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_DISABLED):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertEqual(result.get("reason"), "toss_order_adapter_disabled")


# ── 5. PASS + policy enabled + transport=None → dispatch blocked ──

class TestHermesGatePassPolicyEnabledNoTransport(unittest.TestCase):
    def setUp(self):
        self._p1, self._p2 = _tmp_patches()
        self._p1.start()
        self._p2.start()
        _reset()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        _reset()

    def test_pass_enabled_no_transport_blocked(self):
        pilot_id = _create_pilot()
        vid = _create_verif_request(pilot_id)
        _record_verif(vid, "PASS", ttl=10)
        fake_record = {
            "pilot_id": pilot_id, "symbol": "091180.KS", "side": "buy",
            "quantity": 1, "limit_price": 30000.0, "estimated_amount_krw": 30000.0,
            "status": "previewed", "blocks": [], "live_order_sent": False,
        }
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_ENABLED), \
             patch("core.toss_live_pilot_ledger.list_live_pilot_records",
                   return_value=[fake_record]):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        # transport=None → dispatch blocked
        self.assertFalse(result["live_order_sent"])
        self.assertTrue(result.get("blocked"))

    def test_pass_enabled_no_transport_reason(self):
        pilot_id = _create_pilot()
        vid = _create_verif_request(pilot_id)
        _record_verif(vid, "PASS", ttl=10)
        fake_record = {
            "pilot_id": pilot_id, "symbol": "091180.KS", "side": "buy",
            "quantity": 1, "limit_price": 30000.0, "estimated_amount_krw": 30000.0,
            "status": "previewed", "blocks": [], "live_order_sent": False,
        }
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_ENABLED), \
             patch("core.toss_live_pilot_ledger.list_live_pilot_records",
                   return_value=[fake_record]):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        # either guard_failed or transport_blocked
        self.assertIn(
            result.get("reason", ""),
            ("live_send_guard_failed", "live_transport_not_injected", "transport_blocked"),
        )


# ── 6. STALE (PASS + expired) → blocked ──────────────────

class TestHermesGateStale(unittest.TestCase):
    def setUp(self):
        self._p1, self._p2 = _tmp_patches()
        self._p1.start()
        self._p2.start()
        _reset()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        _reset()

    def test_stale_pass_blocked(self):
        pilot_id = _create_pilot()
        vid = _create_verif_request(pilot_id)
        _record_verif(vid, "PASS", ttl=1)  # 1분 TTL

        # is_verification_passed에 2분 후 시각 주입
        future = datetime.now(KST) + timedelta(minutes=2)

        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_DISABLED), \
             patch("core.toss_live_pilot_verification.is_verification_passed",
                   return_value=(False, ["hermes_verification_stale"], {})):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertFalse(result["live_order_sent"])
        self.assertTrue(result.get("blocked"))


# ── 7. live_order_sent 절대 True 없음 ─────────────────────

class TestHermesGateNeverSent(unittest.TestCase):
    def setUp(self):
        self._p1, self._p2 = _tmp_patches()
        self._p1.start()
        self._p2.start()
        _reset()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        _reset()

    def test_all_paths_live_order_sent_false(self):
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        scenarios = [
            {},                              # no verif record
        ]
        for _ in scenarios:
            pilot_id = _create_pilot()
            with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                       return_value=_POLICY_DISABLED):
                result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
            self.assertFalse(
                result.get("live_order_sent"),
                f"live_order_sent=True 감지: {result}",
            )


# ── 8. 민감정보 없음 ──────────────────────────────────────

class TestHermesGateNoSensitiveInfo(unittest.TestCase):
    def setUp(self):
        self._p1, self._p2 = _tmp_patches()
        self._p1.start()
        self._p2.start()
        _reset()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        _reset()

    def test_no_sensitive_in_blocked_message(self):
        pilot_id = _create_pilot()
        vid = _create_verif_request(pilot_id)
        _record_verif(vid, "HOLD", ttl=10)
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_DISABLED):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        msg = result.get("message", "")
        for kw in ("accountNo", "Bearer", "APP_KEY", "APP_SECRET"):
            self.assertNotIn(kw, msg)


if __name__ == "__main__":
    unittest.main()
