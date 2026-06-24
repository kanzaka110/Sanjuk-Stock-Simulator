"""tests/test_toss_live_pilot_transport_injection.py

Toss live pilot transport injection wiring 테스트.

정책:
- confirm 경로는 resolve_live_transport_for_confirm(policy)로 transport를 결정한다.
- env gate 3종 OFF(또는 policy 미충족) → resolver는 None → 기존처럼 차단.
- env gate 3종 + policy enabled + BUY_ONLY + DEFAULT_LIVE_TRANSPORT가
  NotConfigured가 아닐 때만 callable 반환 (명시적 주입 구조).
- 운영 기본값(DEFAULT_LIVE_TRANSPORT=NotConfigured)에서는 실주문 경로 안 열림.
- 실제 LiveTossTransport는 requests.post mock 없이는 절대 POST 안 함.
- sell / blocked symbol / amount over limit은 env ON mock에서도 guard가 차단.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.toss_live_pilot_telegram import (
    resolve_live_transport_for_confirm,
    handle_live_pilot_callback,
)
from core.toss_live_transport import (
    TossLiveTransportBase,
    LiveTossTransport,
)

_ALL_GATES_ENV = {
    "TOSS_LIVE_PILOT_ENABLED": "true",
    "TOSS_LIVE_ORDER_ALLOWED": "true",
    "TOSS_LIVE_ADAPTER_ENABLED": "true",
}
_CLEARED_ENV = {
    "TOSS_LIVE_PILOT_ENABLED": "",
    "TOSS_LIVE_ORDER_ALLOWED": "",
    "TOSS_LIVE_ADAPTER_ENABLED": "",
}

_ENABLED_POLICY = {
    "live_pilot_enabled": True,
    "live_order_allowed": True,
    "adapter_status": "enabled",
    "all_live_gates_open": True,
    "side_mode": "BUY_ONLY",
    "allowed_sides": ["buy"],
    "requires_user_confirmation": True,
    "requires_second_confirmation": True,
    "blocked_symbols": ["005930.KS", "161510.KS", "MU"],
    "max_order_krw": 300_000,
    "max_orders_per_day": 1,
    "max_daily_krw": 300_000,
}

_DISABLED_POLICY = {
    "live_pilot_enabled": False,
    "live_order_allowed": False,
    "adapter_status": "disabled",
    "all_live_gates_open": False,
    "side_mode": "BUY_ONLY",
    "allowed_sides": ["buy"],
}


class _FakeSuccessTransport(TossLiveTransportBase):
    """주입 검증용 fake — 항상 success 반환 (HTTP 호출 없음)."""

    def __init__(self):
        self.called_payloads = []

    def send_buy_order(self, payload: dict) -> dict:
        self.called_payloads.append(payload)
        return {
            "ok": True,
            "live_order_sent": True,
            "broker_order_id": "ORD-FAKE-INJ",
            "transport_status": "fake",
            "message": "fake success",
        }


def _make_db_patch():
    tmp = tempfile.mkdtemp()
    p = Path(tmp) / "test_pilot.db"
    return patch("core.toss_live_pilot_ledger._db_path", return_value=p)


def _create_pilot():
    from core.toss_live_pilot_ledger import record_live_pilot_preview
    preview = {
        "ok": True, "preview_id": "tlive_inj_test", "symbol": "091180.KS",
        "side": "buy", "quantity": 1, "limit_price": 30000.0,
        "estimated_amount_krw": 30000.0, "blocks": [], "warnings": [],
    }
    return record_live_pilot_preview(preview)


# ── 1. env OFF + Hermes PASS + confirm → resolver None → live_order_sent false ──

class TestEnvOffConfirmBlocked(unittest.TestCase):
    def setUp(self):
        self._db_patch = _make_db_patch()
        self._db_patch.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False
        self._env_patch = patch.dict(os.environ, _CLEARED_ENV)
        self._env_patch.start()
        self._hermes_patch = patch(
            "core.toss_live_pilot_verification.is_verification_passed",
            return_value=(True, [], {}),
        )
        self._hermes_patch.start()

    def tearDown(self):
        self._hermes_patch.stop()
        self._env_patch.stop()
        self._db_patch.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def test_resolver_none_for_disabled_policy(self):
        self.assertIsNone(resolve_live_transport_for_confirm(_DISABLED_POLICY))

    def test_confirm_env_off_live_order_sent_false(self):
        rec = _create_pilot()
        result = handle_live_pilot_callback(f"tlp:confirm:{rec['pilot_id']}")
        self.assertFalse(result["live_order_sent"])
        self.assertFalse(result.get("ok"))


# ── 2. env OFF → LiveTossTransport / DEFAULT transport not called ──

class TestEnvOffTransportNotCalled(unittest.TestCase):
    def test_disabled_policy_does_not_touch_default_transport(self):
        spy = MagicMock(spec=TossLiveTransportBase)
        with patch(
            "core.toss_live_transport.DEFAULT_LIVE_TRANSPORT", spy
        ):
            transport = resolve_live_transport_for_confirm(_DISABLED_POLICY)
        self.assertIsNone(transport)
        spy.send_buy_order.assert_not_called()

    def test_resolver_none_when_default_is_not_configured(self):
        # 운영 기본값: DEFAULT_LIVE_TRANSPORT는 NotConfigured → enabled여도 None
        transport = resolve_live_transport_for_confirm(_ENABLED_POLICY)
        self.assertIsNone(transport)


# ── 3. env ON mock + enabled policy + fake 주입 → resolver callable 반환 ──

class TestEnabledPolicyResolvesInjectedTransport(unittest.TestCase):
    def test_resolver_returns_callable_when_fake_injected(self):
        fake = _FakeSuccessTransport()
        with patch(
            "core.toss_live_transport.DEFAULT_LIVE_TRANSPORT", fake
        ):
            transport = resolve_live_transport_for_confirm(_ENABLED_POLICY)
        self.assertIsNotNone(transport)
        self.assertTrue(callable(transport))

    def test_resolver_callable_invokes_injected_transport(self):
        fake = _FakeSuccessTransport()
        with patch(
            "core.toss_live_transport.DEFAULT_LIVE_TRANSPORT", fake
        ):
            transport = resolve_live_transport_for_confirm(_ENABLED_POLICY)
            result = transport({"symbol": "091180.KS"}, _ENABLED_POLICY)
        self.assertTrue(result["live_order_sent"])
        self.assertEqual(len(fake.called_payloads), 1)

    def test_resolver_none_when_side_mode_not_buy_only(self):
        bad = dict(_ENABLED_POLICY, side_mode="BUY_SELL")
        fake = _FakeSuccessTransport()
        with patch("core.toss_live_transport.DEFAULT_LIVE_TRANSPORT", fake):
            self.assertIsNone(resolve_live_transport_for_confirm(bad))


# ── 4. env ON mock + fake success → 격리 DB에만 live_sent, production 미오염 ──

class TestFakeSuccessIsolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = Path(self.tmp) / "test_pilot.db"
        self._db_patch = patch(
            "core.toss_live_pilot_ledger._db_path", return_value=self.db_path
        )
        self._db_patch.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False
        self._env_patch = patch.dict(os.environ, _ALL_GATES_ENV)
        self._env_patch.start()
        self._hermes_patch = patch(
            "core.toss_live_pilot_verification.is_verification_passed",
            return_value=(True, [], {}),
        )
        self._hermes_patch.start()

    def tearDown(self):
        self._hermes_patch.stop()
        self._env_patch.stop()
        self._db_patch.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def test_fake_injected_transport_records_live_sent_in_temp_db(self):
        from core.toss_live_pilot_ledger import list_live_pilot_records
        rec = _create_pilot()
        fake = _FakeSuccessTransport()
        with patch(
            "core.toss_live_transport.DEFAULT_LIVE_TRANSPORT", fake
        ), patch(
            "core.toss_live_pilot_adapter.can_send_live_pilot_order",
            return_value=(True, []),
        ):
            result = handle_live_pilot_callback(f"tlp:confirm:{rec['pilot_id']}")
        self.assertTrue(result["live_order_sent"])
        self.assertEqual(len(fake.called_payloads), 1)
        # 격리된 임시 DB에만 기록
        records = list_live_pilot_records()
        matched = [r for r in records if r["pilot_id"] == rec["pilot_id"]]
        self.assertEqual(matched[0]["status"], "live_sent")
        # production 경로(db/data)에 쓰지 않음
        self.assertIn(str(self.tmp), str(self.db_path))


# ── 5. env ON mock + 실제 LiveTossTransport → requests.post mock 없이 POST 금지 ──

class TestRealTransportNoPostWithoutMock(unittest.TestCase):
    def test_real_transport_blocked_before_post(self):
        real = LiveTossTransport()
        with patch("core.toss_live_transport.DEFAULT_LIVE_TRANSPORT", real):
            transport = resolve_live_transport_for_confirm(_ENABLED_POLICY)
            self.assertIsNotNone(transport)
            with patch("core.toss_client._get_access_token", return_value=None), \
                 patch("requests.post") as mock_post:
                result = transport(
                    {
                        "symbol": "091180.KS", "side": "buy",
                        "order_type": "limit", "quantity": 1,
                        "limit_price": 30000.0, "estimated_amount_krw": 30000.0,
                    },
                    _ENABLED_POLICY,
                )
        # 토큰 없음 → POST 호출 0, live_order_sent=False
        mock_post.assert_not_called()
        self.assertFalse(result["live_order_sent"])


# ── 6. sell side → env ON mock에서도 BUY_ONLY guard 차단 ──

class TestSellBlockedEvenEnabled(unittest.TestCase):
    def setUp(self):
        self._db_patch = _make_db_patch()
        self._db_patch.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def tearDown(self):
        self._db_patch.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def test_sell_guard_blocks(self):
        from core.toss_live_pilot_adapter import can_send_live_pilot_order
        preview = {
            "ok": True, "symbol": "091180.KS", "side": "sell",
            "quantity": 1, "limit_price": 30000.0,
            "estimated_amount_krw": 30000.0, "blocks": [],
        }
        ok, reasons = can_send_live_pilot_order(
            _ENABLED_POLICY, preview, {"ok": True}
        )
        self.assertFalse(ok)
        self.assertTrue(any("sell_not_allowed" in r for r in reasons))


# ── 7. blocked symbol → env ON mock에서도 차단 ──

class TestBlockedSymbolEvenEnabled(unittest.TestCase):
    def setUp(self):
        self._db_patch = _make_db_patch()
        self._db_patch.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def tearDown(self):
        self._db_patch.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def test_blocked_symbol_guard_blocks(self):
        from core.toss_live_pilot_adapter import can_send_live_pilot_order
        preview = {
            "ok": True, "symbol": "005930.KS", "side": "buy",
            "quantity": 1, "limit_price": 30000.0,
            "estimated_amount_krw": 30000.0, "blocks": [],
        }
        ok, reasons = can_send_live_pilot_order(
            _ENABLED_POLICY, preview, {"ok": True}
        )
        self.assertFalse(ok)
        self.assertTrue(any("blocked_symbol" in r for r in reasons))


# ── 8. amount over limit → env ON mock에서도 차단 ──

class TestAmountOverLimitEvenEnabled(unittest.TestCase):
    def setUp(self):
        self._db_patch = _make_db_patch()
        self._db_patch.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def tearDown(self):
        self._db_patch.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def test_amount_over_limit_guard_blocks(self):
        from core.toss_live_pilot_adapter import can_send_live_pilot_order
        preview = {
            "ok": True, "symbol": "091180.KS", "side": "buy",
            "quantity": 100, "limit_price": 30000.0,
            "estimated_amount_krw": 3_000_000.0, "blocks": [],
        }
        ok, reasons = can_send_live_pilot_order(
            _ENABLED_POLICY, preview, {"ok": True}
        )
        self.assertFalse(ok)
        self.assertTrue(any("amount_over_limit" in r for r in reasons))


# ── 9. runtime arming 안전 불변식 ──

class TestRuntimeArmingSafety(unittest.TestCase):
    """실 transport는 명시적 armed runtime에서만 구성되고, pytest/.env는 unarmed 유지."""

    def test_env_file_has_no_live_gates(self):
        # .env에 gate가 들어가면 settings.py/toss_client.py가 import 시 load_dotenv로
        # pytest까지 armed시킬 수 있으므로 절대 금지.
        env = _ROOT / ".env"
        if not env.exists():
            return
        text = env.read_text(encoding="utf-8")
        for key in (
            "TOSS_LIVE_PILOT_ENABLED",
            "TOSS_LIVE_ORDER_ALLOWED",
            "TOSS_LIVE_ADAPTER_ENABLED",
            "TOSS_LIVE_TRANSPORT_ARMED",
        ):
            offenders = [
                ln for ln in text.splitlines()
                if ln.strip().startswith(key + "=")
                and ln.split("=", 1)[1].strip().lower() == "true"
            ]
            self.assertEqual(offenders, [], f"{key}=true 가 .env에 있음 — pytest armed 위험")

    def test_default_transport_not_configured_in_pytest(self):
        from core.toss_live_transport import (
            DEFAULT_LIVE_TRANSPORT,
            NotConfiguredTossLiveTransport,
        )
        self.assertIsInstance(DEFAULT_LIVE_TRANSPORT, NotConfiguredTossLiveTransport)

    def test_armed_false_without_armed_flag_even_with_gates(self):
        from core.toss_live_transport import _runtime_live_transport_armed
        with patch.dict(os.environ, _ALL_GATES_ENV):
            # gate 3종이 켜져도 TOSS_LIVE_TRANSPORT_ARMED 없으면 unarmed
            self.assertFalse(_runtime_live_transport_armed())

    def test_armed_false_with_only_armed_flag(self):
        from core.toss_live_transport import _runtime_live_transport_armed
        with patch.dict(os.environ, {"TOSS_LIVE_TRANSPORT_ARMED": "true"}, clear=False), \
             patch.dict(os.environ, _CLEARED_ENV):
            self.assertFalse(_runtime_live_transport_armed())

    def test_armed_true_only_with_flag_and_all_gates(self):
        from core.toss_live_transport import _runtime_live_transport_armed
        armed_env = dict(_ALL_GATES_ENV, TOSS_LIVE_TRANSPORT_ARMED="true")
        with patch.dict(os.environ, armed_env):
            self.assertTrue(_runtime_live_transport_armed())


if __name__ == "__main__":
    unittest.main()
