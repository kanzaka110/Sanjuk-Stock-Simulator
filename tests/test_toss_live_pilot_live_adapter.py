"""tests/test_toss_live_pilot_live_adapter.py

can_send_live_pilot_order + dispatch_toss_order_live 테스트.
- transport=None → 항상 blocked
- fake transport success → guard 통과 시 live_order_sent=True
- fake transport failure → live_order_sent=False
- amount/daily/duplicate/blocked symbol guard
- 민감정보 없음
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import os
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.toss_live_pilot_adapter import (
    can_send_live_pilot_order,
    dispatch_toss_order_live,
)

_ALL_GATES_ENV = {
    "TOSS_LIVE_PILOT_ENABLED": "true",
    "TOSS_LIVE_ORDER_ALLOWED": "true",
    "TOSS_LIVE_ADAPTER_ENABLED": "true",
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

def _ok_preview(symbol="091180.KS", price=30_000, qty=1, side="buy"):
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

def _ok_payload_result():
    return {"ok": True, "live_order_sent": False}

def _fake_transport_success(payload, policy):
    return {
        "ok": True,
        "live_order_sent": True,
        "broker_order_id": "ORD-TEST-12345",
        "status": "submitted",
    }

def _fake_transport_failure(payload, policy):
    return {
        "ok": False,
        "live_order_sent": False,
        "failure_reason": "exchange_rejected",
        "status": "failed",
    }


# ─── 1. can_send_live_pilot_order — policy disabled ──────

class TestCanSendDisabledPolicy(unittest.TestCase):
    def test_disabled_policy_blocked(self):
        ok, reasons = can_send_live_pilot_order(
            _POLICY_DISABLED, _ok_preview(), _ok_payload_result()
        )
        self.assertFalse(ok)
        self.assertTrue(any("live_order_allowed" in r for r in reasons))

    def test_disabled_adapter_status_blocked(self):
        ok, reasons = can_send_live_pilot_order(
            _POLICY_DISABLED, _ok_preview(), _ok_payload_result()
        )
        self.assertFalse(ok)
        self.assertTrue(any("adapter_status" in r for r in reasons))


# ─── 2. can_send — amount guard ──────────────────────────

class TestCanSendAmountGuard(unittest.TestCase):
    def test_amount_over_limit_blocked(self):
        preview = _ok_preview(price=150_000, qty=1)  # > 100,000 한도
        ok, reasons = can_send_live_pilot_order(_POLICY_ENABLED, preview, _ok_payload_result())
        self.assertFalse(ok)
        self.assertTrue(any("amount_over_limit" in r for r in reasons))

    def test_amount_within_limit_ok(self):
        preview = _ok_preview(price=30_000, qty=1)  # < 100,000
        with patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=[]):
            ok, reasons = can_send_live_pilot_order(_POLICY_ENABLED, preview, _ok_payload_result())
        # policy guard 통과 + 기타 이유 없어야 ok
        amount_reasons = [r for r in reasons if "amount_over_limit" in r]
        self.assertEqual(amount_reasons, [])


# ─── 3. can_send — blocked symbols ───────────────────────

class TestCanSendBlockedSymbols(unittest.TestCase):
    def test_005930_blocked(self):
        preview = _ok_preview(symbol="005930.KS", price=319_000)
        ok, reasons = can_send_live_pilot_order(_POLICY_ENABLED, preview, _ok_payload_result())
        self.assertFalse(ok)
        self.assertTrue(any("blocked_symbol" in r for r in reasons))

    def test_161510_blocked(self):
        preview = _ok_preview(symbol="161510.KS", price=1_000)
        ok, reasons = can_send_live_pilot_order(_POLICY_ENABLED, preview, _ok_payload_result())
        self.assertFalse(ok)
        self.assertTrue(any("blocked_symbol" in r for r in reasons))

    def test_mu_blocked(self):
        preview = _ok_preview(symbol="MU", price=50_000)
        ok, reasons = can_send_live_pilot_order(_POLICY_ENABLED, preview, _ok_payload_result())
        self.assertFalse(ok)
        self.assertTrue(any("blocked_symbol" in r for r in reasons))


# ─── 4. can_send — daily guard ───────────────────────────

class TestCanSendDailyGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._patch = patch(
            "core.toss_live_pilot_ledger._db_path",
            return_value=Path(self.tmp) / "test_pilot.db",
        )
        self._patch.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def tearDown(self):
        self._patch.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def _inject_today_sent(self, symbol="091180.KS", amount=30_000):
        """오늘 live_sent 레코드 직접 삽입."""
        from core.toss_live_pilot_ledger import _conn, _now_kst
        with _conn() as conn:
            conn.execute(
                """INSERT INTO live_pilot_ledger
                   (pilot_id, preview_id, symbol, side, quantity,
                    limit_price, estimated_amount_krw, status,
                    blocks, warnings, live_order_allowed, live_order_sent,
                    adapter_status, broker_order_id, failure_reason, payload_hash,
                    created_at, confirmed_at, cancelled_at, reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"tlive_daily_{symbol}", "prev_daily", symbol, "buy", 1,
                 float(amount), float(amount), "live_sent",
                 "[]", "[]", 0, 1, "enabled", "", "", "",
                 _now_kst(), None, None, ""),
            )
            conn.commit()

    def test_max_orders_per_day_exceeded(self):
        self._inject_today_sent()
        preview = _ok_preview(symbol="360750.KS", price=30_000)  # 다른 symbol
        ok, reasons = can_send_live_pilot_order(_POLICY_ENABLED, preview, _ok_payload_result())
        self.assertFalse(ok)
        self.assertTrue(any("daily_order_count_exceeded" in r for r in reasons))

    def test_duplicate_symbol_blocked(self):
        self._inject_today_sent(symbol="091180.KS", amount=30_000)
        preview = _ok_preview(symbol="091180.KS", price=30_000)
        ok, reasons = can_send_live_pilot_order(_POLICY_ENABLED, preview, _ok_payload_result())
        self.assertFalse(ok)
        self.assertTrue(any("duplicate_symbol" in r for r in reasons))

    def test_daily_amount_exceeded(self):
        # 이미 280,000 전송됨 → 30,000 추가하면 300,000 초과
        self._inject_today_sent(amount=280_000)
        preview = _ok_preview(price=30_000)
        ok, reasons = can_send_live_pilot_order(_POLICY_ENABLED, preview, _ok_payload_result())
        self.assertFalse(ok)
        self.assertTrue(any("daily_amount_exceeded" in r for r in reasons))


# ─── 4b. 최종 정책 — 건수 무제한 + 1일 총액 cap 200만 ────

# max_orders_per_day=None(무제한), max_daily_krw=2,000,000 → 최종 정책 형태
_POLICY_FINAL = {
    "live_pilot_enabled": True,
    "live_order_allowed": True,
    "adapter_status": "enabled",
    "requires_user_confirmation": True,
    "requires_second_confirmation": True,
    "max_order_krw": 500_000,
    "max_daily_krw": 2_000_000,
    "max_orders_per_day": None,
    "blocked_symbols": [],
}


class TestCanSendFinalPolicy(unittest.TestCase):
    """건수 제한 없음(총액 cap만) — 여러 건이어도 총액 이내면 차단 없음."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._patch = patch(
            "core.toss_live_pilot_ledger._db_path",
            return_value=Path(self.tmp) / "test_pilot.db",
        )
        self._patch.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def tearDown(self):
        self._patch.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def _inject_today_sent(self, symbol, amount):
        from core.toss_live_pilot_ledger import _conn, _now_kst
        with _conn() as conn:
            conn.execute(
                """INSERT INTO live_pilot_ledger
                   (pilot_id, preview_id, symbol, side, quantity,
                    limit_price, estimated_amount_krw, status,
                    blocks, warnings, live_order_allowed, live_order_sent,
                    adapter_status, broker_order_id, failure_reason, payload_hash,
                    created_at, confirmed_at, cancelled_at, reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"tlive_final_{symbol}", "prev_final", symbol, "buy", 1,
                 float(amount), float(amount), "live_sent",
                 "[]", "[]", 0, 1, "enabled", "", "", "",
                 _now_kst(), None, None, ""),
            )
            conn.commit()

    def test_many_orders_within_cap_no_count_block(self):
        # 3건(각 30만, 총 90만) 전송됨 + 추가 1건 30만 → 총 120만 < 200만 → 차단 없음
        self._inject_today_sent("111.KS", 300_000)
        self._inject_today_sent("222.KS", 300_000)
        self._inject_today_sent("333.KS", 300_000)
        preview = _ok_preview(symbol="444.KS", price=300_000)
        ok, reasons = can_send_live_pilot_order(_POLICY_FINAL, preview, _ok_payload_result())
        self.assertFalse(any("daily_order_count_exceeded" in r for r in reasons))
        self.assertFalse(any("daily_amount_exceeded" in r for r in reasons))
        self.assertTrue(ok, f"unexpected block: {reasons}")

    def test_daily_amount_over_2m_blocked(self):
        # 이미 1,800,000 전송됨 + 300,000 추가 → 2,100,000 > 2,000,000 cap → 차단
        self._inject_today_sent("111.KS", 1_800_000)
        preview = _ok_preview(symbol="444.KS", price=300_000)
        ok, reasons = can_send_live_pilot_order(_POLICY_FINAL, preview, _ok_payload_result())
        self.assertFalse(ok)
        self.assertTrue(any("daily_amount_exceeded" in r for r in reasons))


# ─── 5. dispatch_toss_order_live — transport=None ────────

class TestDispatchLiveNoTransport(unittest.TestCase):
    def test_no_transport_blocked(self):
        payload = {"symbol": "091180.KS", "quantity": 1, "limit_price": 30000}
        result = dispatch_toss_order_live(payload, _POLICY_ENABLED, transport=None)
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertFalse(result["live_order_sent"])

    def test_no_transport_reason(self):
        payload = {"symbol": "091180.KS", "quantity": 1, "limit_price": 30000}
        result = dispatch_toss_order_live(payload, _POLICY_ENABLED, transport=None)
        self.assertEqual(result["reason"], "live_transport_not_injected")

    def test_no_transport_message(self):
        payload = {"symbol": "091180.KS", "quantity": 1, "limit_price": 30000}
        result = dispatch_toss_order_live(payload, _POLICY_ENABLED, transport=None)
        self.assertIn("아직 주문 전송 안 함", result["message"])

    def test_disabled_policy_no_transport_blocked(self):
        payload = {"symbol": "091180.KS", "quantity": 1, "limit_price": 30000}
        result = dispatch_toss_order_live(payload, _POLICY_DISABLED, transport=None)
        self.assertFalse(result["live_order_sent"])
        self.assertTrue(result["blocked"])


# ─── 6. dispatch_toss_order_live — fake transport success ─

class TestDispatchLiveFakeSuccess(unittest.TestCase):
    def _dispatch_with_fake(self, symbol="091180.KS", price=30_000):
        payload = {
            "symbol": symbol,
            "side": "buy",
            "order_type": "limit",
            "quantity": 1,
            "limit_price": float(price),
            "estimated_amount_krw": float(price),
        }
        return dispatch_toss_order_live(
            payload, _POLICY_ENABLED, transport=_fake_transport_success
        )

    def test_fake_success_live_order_sent_true(self):
        result = self._dispatch_with_fake()
        self.assertTrue(result["live_order_sent"])

    def test_fake_success_ok_true(self):
        result = self._dispatch_with_fake()
        self.assertTrue(result["ok"])

    def test_fake_success_blocked_false(self):
        result = self._dispatch_with_fake()
        self.assertFalse(result.get("blocked", False))

    def test_fake_success_message(self):
        result = self._dispatch_with_fake()
        self.assertIn("승인형 매수 pilot", result["message"])
        self.assertNotIn("자동매매 시작", result["message"])

    def test_fake_success_no_sensitive_in_result(self):
        result = self._dispatch_with_fake()
        result_str = str(result)
        for kw in ("accountNo", "Bearer", "APP_KEY", "APP_SECRET"):
            self.assertNotIn(kw, result_str)

    def test_fake_success_broker_order_id_not_raw_account(self):
        result = self._dispatch_with_fake()
        import re
        # accountNo 형식 (8자리-2자리)이 없어야 함
        self.assertEqual(re.findall(r'\d{8}-\d{2}', str(result.get("broker_order_id", ""))), [])

    def test_fake_success_payload_hash_present(self):
        result = self._dispatch_with_fake()
        self.assertIn("payload_hash", result)
        self.assertGreater(len(result["payload_hash"]), 0)


# ─── 7. dispatch_toss_order_live — fake transport failure ─

class TestDispatchLiveFakeFailure(unittest.TestCase):
    def _dispatch_with_fake_fail(self):
        payload = {"symbol": "091180.KS", "quantity": 1, "limit_price": 30000,
                   "estimated_amount_krw": 30000}
        return dispatch_toss_order_live(
            payload, _POLICY_ENABLED, transport=_fake_transport_failure
        )

    def test_fake_failure_live_order_sent_false(self):
        result = self._dispatch_with_fake_fail()
        self.assertFalse(result["live_order_sent"])

    def test_fake_failure_ok_false(self):
        result = self._dispatch_with_fake_fail()
        self.assertFalse(result["ok"])

    def test_fake_failure_message(self):
        result = self._dispatch_with_fake_fail()
        self.assertIn("실패", result["message"])

    def test_fake_failure_reason_in_result(self):
        result = self._dispatch_with_fake_fail()
        self.assertIn("exchange_rejected", result.get("failure_reason", ""))


# ─── 8. dispatch — transport exception ───────────────────

class TestDispatchLiveTransportException(unittest.TestCase):
    def test_transport_exception_live_order_sent_false(self):
        def exploding_transport(payload, policy):
            raise RuntimeError("connection refused")

        payload = {"symbol": "091180.KS", "quantity": 1, "limit_price": 30000}
        result = dispatch_toss_order_live(
            payload, _POLICY_ENABLED, transport=exploding_transport
        )
        self.assertFalse(result["live_order_sent"])
        self.assertEqual(result["reason"], "transport_exception")

    def test_transport_exception_message_safe(self):
        def exploding_transport(payload, policy):
            raise RuntimeError("network error")

        payload = {"symbol": "091180.KS", "quantity": 1, "limit_price": 30000}
        result = dispatch_toss_order_live(
            payload, _POLICY_ENABLED, transport=exploding_transport
        )
        self.assertNotIn("accountNo", result.get("message", ""))


# ─── 9. 민감정보 소스 검사 ───────────────────────────────

class TestNoSensitiveInAdapterSource(unittest.TestCase):
    def test_no_hardcoded_account_no(self):
        import re
        src = (_ROOT / "core" / "toss_live_pilot_adapter.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'\d{8}-\d{2}', src), [])

    def test_no_hardcoded_bearer(self):
        import re
        src = (_ROOT / "core" / "toss_live_pilot_adapter.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'Bearer [A-Za-z0-9._\-]{20,}', src), [])


if __name__ == "__main__":
    unittest.main()
