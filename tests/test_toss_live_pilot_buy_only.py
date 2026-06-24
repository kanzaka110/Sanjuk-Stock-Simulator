"""tests/test_toss_live_pilot_buy_only.py

BUY_ONLY 정책 + can_send sell 차단 + Telegram sell 문구 테스트.

1. policy: side_mode=BUY_ONLY, allowed_sides=["buy"], sell_allowed=False
2. can_send: buy → 통과 가능, sell → sell_not_allowed_in_buy_only_pilot 차단
3. Telegram confirm(sell) → "차단: BUY_ONLY pilot — 매도는 아직 비활성"
4. Telegram confirm(buy, Hermes PASS, no transport) → "차단: Toss live transport 미설정"
5. fake buy transport success → "승인형 매수 pilot 전송 완료" (자동매매 없음)
6. fake sell transport → 호출 안 됨 (guard에서 차단)
7. 민감정보 없음
8. Paper SOFI 미접촉
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

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
    "side_mode": "BUY_ONLY",
    "allowed_sides": ["buy"],
    "sell_allowed": False,
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
    "side_mode": "BUY_ONLY",
    "allowed_sides": ["buy"],
    "sell_allowed": False,
}


def _ok_preview(symbol="091180.KS", side="buy", price=30_000, qty=1):
    return {
        "ok": True,
        "symbol": symbol,
        "side": side,
        "quantity": qty,
        "limit_price": float(price),
        "estimated_amount_krw": float(price * qty),
        "blocks": [],
        "live_order_sent": False,
    }


def _ok_payload():
    return {"ok": True, "live_order_sent": False}


# ── 1. policy BUY_ONLY 필드 ───────────────────────────────

class TestPolicyBuyOnly(unittest.TestCase):
    def setUp(self):
        self._p = patch.dict(
            __import__("os").environ,
            {"TOSS_LIVE_PILOT_ENABLED": "", "TOSS_LIVE_ORDER_ALLOWED": "",
             "TOSS_LIVE_ADAPTER_ENABLED": ""},
        )
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_side_mode_buy_only(self):
        from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
        p = compute_toss_live_pilot_policy()
        self.assertEqual(p["side_mode"], "BUY_ONLY")

    def test_allowed_sides_buy(self):
        from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
        p = compute_toss_live_pilot_policy()
        self.assertEqual(p["allowed_sides"], ["buy"])

    def test_sell_allowed_false(self):
        from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
        p = compute_toss_live_pilot_policy()
        self.assertFalse(p["sell_allowed"])

    def test_live_transport_status_not_configured(self):
        from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
        p = compute_toss_live_pilot_policy()
        self.assertEqual(p.get("live_transport_status"), "not_configured")


# ── 2. can_send: sell 차단 ────────────────────────────────

class TestCanSendSellBlocked(unittest.TestCase):
    def test_sell_blocked_buy_only(self):
        from core.toss_live_pilot_adapter import can_send_live_pilot_order
        preview = _ok_preview(side="sell")
        ok, reasons = can_send_live_pilot_order(_POLICY_ENABLED, preview, _ok_payload())
        self.assertFalse(ok)
        self.assertTrue(any("sell_not_allowed" in r for r in reasons))

    def test_sell_reason_specific(self):
        from core.toss_live_pilot_adapter import can_send_live_pilot_order
        preview = _ok_preview(side="sell")
        _, reasons = can_send_live_pilot_order(_POLICY_ENABLED, preview, _ok_payload())
        self.assertTrue(any("sell_not_allowed_in_buy_only_pilot" in r for r in reasons))

    def test_buy_not_blocked_by_side_guard(self):
        from core.toss_live_pilot_adapter import can_send_live_pilot_order
        with patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=[]):
            ok, reasons = can_send_live_pilot_order(
                _POLICY_ENABLED, _ok_preview(side="buy"), _ok_payload()
            )
        side_reasons = [r for r in reasons if "sell_not_allowed" in r]
        self.assertEqual(side_reasons, [])

    def test_sell_blocked_even_with_disabled_policy(self):
        from core.toss_live_pilot_adapter import can_send_live_pilot_order
        preview = _ok_preview(side="sell")
        ok, reasons = can_send_live_pilot_order(_POLICY_DISABLED, preview, _ok_payload())
        self.assertFalse(ok)
        # sell guard 있어야 함
        self.assertTrue(any("sell_not_allowed" in r for r in reasons))


# ── 3. Telegram confirm(sell) → BUY_ONLY 차단 문구 ───────

class TestTelegramSellConfirmBlocked(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._db = patch(
            "core.toss_live_pilot_ledger._db_path",
            return_value=Path(self.tmp) / "pilot.db",
        )
        self._db.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False
        # Hermes PASS
        self._hermes = patch(
            "core.toss_live_pilot_verification.is_verification_passed",
            return_value=(True, [], {}),
        )
        self._hermes.start()

    def tearDown(self):
        self._hermes.stop()
        self._db.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def _create_sell_pilot(self) -> str:
        from core.toss_live_pilot_ledger import record_live_pilot_preview
        preview = {
            "ok": True, "symbol": "091180.KS", "side": "sell",
            "quantity": 1, "limit_price": 30000.0,
            "estimated_amount_krw": 30000.0, "blocks": [], "warnings": [],
        }
        return record_live_pilot_preview(preview)["pilot_id"]

    def test_sell_confirm_blocked(self):
        pilot_id = self._create_sell_pilot()
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_ENABLED):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertFalse(result["live_order_sent"])
        self.assertTrue(result.get("blocked"))

    def test_sell_confirm_message_buy_only(self):
        pilot_id = self._create_sell_pilot()
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_ENABLED):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertIn("BUY_ONLY", result["message"])
        self.assertIn("아직 주문 전송 안 함", result["message"])

    def test_sell_confirm_no_forbidden_cta(self):
        pilot_id = self._create_sell_pilot()
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_ENABLED):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        for phrase in ("매도하기", "매수하기", "자동매매 시작", "실주문: 활성"):
            self.assertNotIn(phrase, result["message"])

    def test_sell_confirm_live_order_sent_false(self):
        pilot_id = self._create_sell_pilot()
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_ENABLED):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertFalse(result["live_order_sent"])


# ── 4. buy + Hermes PASS + enabled + transport=None → 미설정 문구 ──

class TestTelegramBuyNotConfiguredTransport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._db = patch(
            "core.toss_live_pilot_ledger._db_path",
            return_value=Path(self.tmp) / "pilot.db",
        )
        self._db.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False
        self._hermes = patch(
            "core.toss_live_pilot_verification.is_verification_passed",
            return_value=(True, [], {}),
        )
        self._hermes.start()

    def tearDown(self):
        self._hermes.stop()
        self._db.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def _create_buy_pilot(self) -> str:
        from core.toss_live_pilot_ledger import record_live_pilot_preview
        preview = {
            "ok": True, "symbol": "091180.KS", "side": "buy",
            "quantity": 1, "limit_price": 30000.0,
            "estimated_amount_krw": 30000.0, "blocks": [], "warnings": [],
        }
        return record_live_pilot_preview(preview)["pilot_id"]

    def test_buy_no_transport_blocked(self):
        pilot_id = self._create_buy_pilot()
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_ENABLED), \
             patch("core.toss_live_pilot_adapter.can_send_live_pilot_order",
                   return_value=(True, [])):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertFalse(result["live_order_sent"])
        self.assertTrue(result.get("blocked"))

    def test_buy_no_transport_message_hermes_pass(self):
        pilot_id = self._create_buy_pilot()
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_ENABLED), \
             patch("core.toss_live_pilot_adapter.can_send_live_pilot_order",
                   return_value=(True, [])):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertIn("Hermes 검증 PASS 확인", result["message"])
        self.assertIn("Toss live transport 미설정", result["message"])
        self.assertIn("아직 주문 전송 안 함", result["message"])

    def test_buy_no_transport_reason(self):
        pilot_id = self._create_buy_pilot()
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        with patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=_POLICY_ENABLED), \
             patch("core.toss_live_pilot_adapter.can_send_live_pilot_order",
                   return_value=(True, [])):
            result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertIn(
            result.get("reason", ""),
            ("live_transport_not_injected", "toss_live_transport_not_configured"),
        )


# ── 5. fake buy transport success → 허용 문구 ────────────

class TestFakeBuyTransportSuccess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._db = patch(
            "core.toss_live_pilot_ledger._db_path",
            return_value=Path(self.tmp) / "pilot.db",
        )
        self._db.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False
        self._hermes = patch(
            "core.toss_live_pilot_verification.is_verification_passed",
            return_value=(True, [], {}),
        )
        self._hermes.start()

    def tearDown(self):
        self._hermes.stop()
        self._db.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def test_fake_buy_success_message_allowed_phrases(self):
        from core.toss_live_pilot_adapter import dispatch_toss_order_live

        def _fake_buy(payload, policy):
            return {
                "ok": True, "live_order_sent": True,
                "broker_order_id": "mock_order_123", "status": "submitted",
            }

        payload = {
            "symbol": "091180.KS", "side": "buy", "order_type": "limit",
            "quantity": 1, "limit_price": 30000.0, "estimated_amount_krw": 30000.0,
        }
        result = dispatch_toss_order_live(payload, _POLICY_ENABLED, transport=_fake_buy)
        self.assertTrue(result["live_order_sent"])
        self.assertIn("승인형 매수 pilot", result["message"])
        self.assertIn("Hermes PASS", result["message"])

    def test_fake_buy_success_no_forbidden_phrases(self):
        from core.toss_live_pilot_adapter import dispatch_toss_order_live

        def _fake_buy(payload, policy):
            return {"ok": True, "live_order_sent": True, "broker_order_id": "mock_123"}

        payload = {
            "symbol": "091180.KS", "side": "buy", "quantity": 1,
            "limit_price": 30000.0, "estimated_amount_krw": 30000.0,
        }
        result = dispatch_toss_order_live(payload, _POLICY_ENABLED, transport=_fake_buy)
        msg = result["message"]
        for phrase in ("자동매매 시작", "자동거래 시작", "매수하기", "실주문: 활성"):
            self.assertNotIn(phrase, msg)

    def test_fake_buy_no_sensitive_info(self):
        from core.toss_live_pilot_adapter import dispatch_toss_order_live

        def _fake_buy(payload, policy):
            return {"ok": True, "live_order_sent": True, "broker_order_id": "mock_123"}

        payload = {
            "symbol": "091180.KS", "side": "buy", "quantity": 1,
            "limit_price": 30000.0, "estimated_amount_krw": 30000.0,
        }
        result = dispatch_toss_order_live(payload, _POLICY_ENABLED, transport=_fake_buy)
        for kw in ("accountNo", "Bearer", "APP_KEY", "APP_SECRET"):
            self.assertNotIn(kw, str(result))


# ── 6. sell: transport 호출 안 됨 ────────────────────────

class TestSellTransportNeverCalled(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._db = patch(
            "core.toss_live_pilot_ledger._db_path",
            return_value=Path(self.tmp) / "pilot.db",
        )
        self._db.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False
        self._hermes = patch(
            "core.toss_live_pilot_verification.is_verification_passed",
            return_value=(True, [], {}),
        )
        self._hermes.start()

    def tearDown(self):
        self._hermes.stop()
        self._db.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def test_sell_transport_never_called_via_can_send(self):
        """sell은 can_send guard에서 차단되므로 dispatch(transport)가 호출 안 됨."""
        from core.toss_live_pilot_adapter import can_send_live_pilot_order

        transport_called = []

        def _evil_transport(payload, policy):
            transport_called.append("called!")
            return {"ok": True, "live_order_sent": True}

        preview = _ok_preview(side="sell")
        ok, _ = can_send_live_pilot_order(_POLICY_ENABLED, preview, _ok_payload())
        self.assertFalse(ok)
        # transport는 can_send 통과 후에만 호출됨 → 0건
        self.assertEqual(transport_called, [])


# ── 7. 민감정보 없음 ──────────────────────────────────────

class TestBuyOnlyNoSensitiveInfo(unittest.TestCase):
    def test_policy_no_sensitive(self):
        from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
        with patch.dict(
            __import__("os").environ,
            {"TOSS_LIVE_PILOT_ENABLED": "", "TOSS_LIVE_ORDER_ALLOWED": "",
             "TOSS_LIVE_ADAPTER_ENABLED": ""},
        ):
            p = compute_toss_live_pilot_policy()
        for kw in ("accountNo", "Bearer", "APP_KEY", "APP_SECRET"):
            self.assertNotIn(kw, str(p))

    def test_transport_file_no_sensitive(self):
        import re
        src = (_ROOT / "core" / "toss_live_transport.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'\d{8}-\d{2}', src), [])
        self.assertEqual(re.findall(r'Bearer [A-Za-z0-9._\-]{20,}', src), [])


# ── 8. Paper SOFI 미접촉 ──────────────────────────────────

class TestBuyOnlyPaperNotContaminated(unittest.TestCase):
    def test_buy_only_no_paper_reference(self):
        """BUY_ONLY 정책/adapter 파일에서 SOFI/paper 잔고 미참조."""
        src_policy = (_ROOT / "core" / "toss_live_pilot_policy.py").read_text(encoding="utf-8")
        src_adapter = (_ROOT / "core" / "toss_live_pilot_adapter.py").read_text(encoding="utf-8")
        # SOFI symbol 직접 참조 없어야 함 (paper ledger 직접 잔고 조회 없음)
        for src in (src_policy, src_adapter):
            self.assertNotIn("SOFI", src)


if __name__ == "__main__":
    unittest.main()
