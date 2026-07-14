"""tests/test_toss_live_transport_config.py

Toss live transport 설정 상태 테스트.
- LIVE_TRANSPORT_STATUS = "not_configured"
- NotConfiguredTossLiveTransport.send_buy_order → blocked
- get_transport_status → not_configured
- live_order_sent 항상 False
- 민감정보 없음
- 실제 HTTP write 없음
- API policy에 live_transport_status 표시
"""

import re
import unittest
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── 1. LIVE_TRANSPORT_STATUS ─────────────────────────────

class TestLiveTransportStatus(unittest.TestCase):
    def test_status_not_configured(self):
        from core.toss_live_transport import LIVE_TRANSPORT_STATUS
        self.assertEqual(LIVE_TRANSPORT_STATUS, "not_configured")

    def test_get_transport_status_dict(self):
        from core.toss_live_transport import get_transport_status
        s = get_transport_status()
        self.assertIsInstance(s, dict)
        self.assertEqual(s["status"], "not_configured")

    def test_endpoint_confirmed_false(self):
        from core.toss_live_transport import get_transport_status
        s = get_transport_status()
        self.assertFalse(s["endpoint_confirmed"])

    def test_live_order_sent_possible_false(self):
        from core.toss_live_transport import get_transport_status
        s = get_transport_status()
        self.assertFalse(s["live_order_sent_possible"])


# ── 2. NotConfiguredTossLiveTransport ─────────────────────

class TestNotConfiguredTransport(unittest.TestCase):
    def setUp(self):
        from core.toss_live_transport import NotConfiguredTossLiveTransport
        self.transport = NotConfiguredTossLiveTransport()

    def test_send_buy_order_ok_false(self):
        result = self.transport.send_buy_order({"symbol": "091180.KS", "side": "buy"})
        self.assertFalse(result["ok"])

    def test_send_buy_order_blocked_true(self):
        result = self.transport.send_buy_order({"symbol": "091180.KS"})
        self.assertTrue(result["blocked"])

    def test_send_buy_order_live_order_sent_false(self):
        result = self.transport.send_buy_order({"symbol": "091180.KS"})
        self.assertFalse(result["live_order_sent"])

    def test_send_buy_order_reason_not_configured(self):
        result = self.transport.send_buy_order({"symbol": "091180.KS"})
        self.assertEqual(result["reason"], "toss_live_transport_not_configured")

    def test_send_buy_order_message_contains_safe_text(self):
        result = self.transport.send_buy_order({"symbol": "091180.KS"})
        self.assertIn("아직 주문 전송 안 함", result["message"])
        self.assertIn("transport 미설정", result["message"])

    def test_send_buy_order_no_sensitive_in_result(self):
        result = self.transport.send_buy_order({"symbol": "091180.KS"})
        for kw in ("accountNo", "Bearer", "APP_KEY", "APP_SECRET"):
            self.assertNotIn(kw, str(result))


# ── 3. DEFAULT_LIVE_TRANSPORT ─────────────────────────────

class TestDefaultLiveTransport(unittest.TestCase):
    def test_default_is_not_configured(self):
        from core.toss_live_transport import DEFAULT_LIVE_TRANSPORT, NotConfiguredTossLiveTransport
        self.assertIsInstance(DEFAULT_LIVE_TRANSPORT, NotConfiguredTossLiveTransport)

    def test_default_send_buy_blocked(self):
        from core.toss_live_transport import DEFAULT_LIVE_TRANSPORT
        result = DEFAULT_LIVE_TRANSPORT.send_buy_order({"symbol": "091180.KS"})
        self.assertFalse(result["live_order_sent"])


# ── 4. policy에 live_transport_status 포함 ────────────────

class TestPolicyTransportStatus(unittest.TestCase):
    def test_policy_has_transport_status(self):
        from unittest.mock import patch
        with patch.dict(
            __import__("os").environ,
            {"TOSS_LIVE_PILOT_ENABLED": "", "TOSS_LIVE_ORDER_ALLOWED": "",
             "TOSS_LIVE_ADAPTER_ENABLED": ""},
        ):
            from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
            p = compute_toss_live_pilot_policy()
        self.assertIn("live_transport_status", p)
        self.assertEqual(p["live_transport_status"], "not_configured")


# ── 5. 소스 코드에 실제 HTTP write 없음 ──────────────────

class TestNoHttpWriteInTransport(unittest.TestCase):
    def test_no_requests_post(self):
        src = (_ROOT / "core" / "toss_live_transport.py").read_text(encoding="utf-8")
        src_no_doc = re.sub(r'"""[\s\S]*?"""', "", src)
        src_no_doc = re.sub(r"#[^\n]*", "", src_no_doc)
        self.assertNotIn("requests.post", src_no_doc)
        self.assertNotIn("requests.put", src_no_doc)
        self.assertNotIn("requests.delete", src_no_doc)
        self.assertNotIn("requests.patch", src_no_doc)

    def test_no_hardcoded_endpoints_in_transport(self):
        src = (_ROOT / "core" / "toss_live_transport.py").read_text(encoding="utf-8")
        # 추측 endpoint가 없어야 함
        for path in ("/api/v1/orders", "/api/v1/buy", "/api/v1/sell", "/trade"):
            self.assertNotIn(path, src)

    def test_no_sensitive_in_transport_source(self):
        src = (_ROOT / "core" / "toss_live_transport.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'\d{8}-\d{2}', src), [])
        self.assertEqual(re.findall(r'Bearer [A-Za-z0-9._\-]{20,}', src), [])


# ── 6. TossLiveTransportBase 인터페이스 ──────────────────

class TestTransportBaseInterface(unittest.TestCase):
    def test_base_raises_not_implemented(self):
        from core.toss_live_transport import TossLiveTransportBase
        base = TossLiveTransportBase()
        with self.assertRaises(NotImplementedError):
            base.send_buy_order({"symbol": "091180.KS"})


# ── 7. dispatch에서 NotConfiguredTransport 사용 시 차단 ──

class TestDispatchWithNotConfiguredTransport(unittest.TestCase):
    def test_not_configured_transport_blocked_in_dispatch(self):
        from core.toss_live_transport import NotConfiguredTossLiveTransport
        from core.toss_live_pilot_adapter import dispatch_toss_order_live

        transport = NotConfiguredTossLiveTransport()

        # transport callable로 주입: send_buy_order가 아닌 callable 인터페이스
        # dispatch_toss_order_live는 transport(payload, policy) 형태 호출
        # NotConfiguredTossLiveTransport는 send_buy_order이므로 wrapper 필요
        def transport_callable(payload, policy):
            return transport.send_buy_order(payload)

        payload = {
            "symbol": "091180.KS", "side": "buy", "order_type": "limit", "quantity": 1,
            "limit_price": 30000.0, "estimated_amount_krw": 30000.0,
        }
        policy = {
            "live_order_allowed": True, "adapter_status": "enabled",
            "live_pilot_enabled": True, "autonomous_mode": False,
        }
        result = dispatch_toss_order_live(payload, policy, transport=transport_callable)
        self.assertFalse(result["live_order_sent"])
        self.assertEqual(result.get("reason") or result.get("failure_reason", ""),
                         "toss_live_transport_not_configured")

    def test_not_configured_message_in_dispatch(self):
        from core.toss_live_transport import NotConfiguredTossLiveTransport
        from core.toss_live_pilot_adapter import dispatch_toss_order_live

        transport = NotConfiguredTossLiveTransport()

        def transport_callable(payload, policy):
            return transport.send_buy_order(payload)

        payload = {
            "symbol": "091180.KS", "side": "buy", "order_type": "limit", "quantity": 1,
            "limit_price": 30000.0, "estimated_amount_krw": 30000.0,
        }
        policy = {"live_order_allowed": True, "adapter_status": "enabled",
                  "live_pilot_enabled": True, "autonomous_mode": False}
        result = dispatch_toss_order_live(payload, policy, transport=transport_callable)
        # transport의 message 또는 failure_reason에 not_configured 반영
        combined = str(result)
        self.assertIn("not_configured", combined)


if __name__ == "__main__":
    unittest.main()
