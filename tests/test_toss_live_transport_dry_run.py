"""tests/test_toss_live_transport_dry_run.py

DryRunTossLiveTransport + dispatch 주입 테스트.

1. send_buy_order: request preview 생성, live_order_sent=False, blocked=True
2. requests.post 호출 없음 (소스 검사)
3. 검증 실패 시 blocked
4. dispatch_toss_order_live에 DryRun 주입해도 live_order_sent=False
5. 민감정보 없음
6. transport_status=dry_run_schema_ready
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.toss_live_transport import DryRunTossLiveTransport
from core.toss_live_pilot_adapter import dispatch_toss_order_live


def _payload(**kw) -> dict:
    base = {
        "symbol": "091180.KS",
        "side": "buy",
        "order_type": "limit",
        "quantity": 1,
        "limit_price": 30850,
        "estimated_amount_krw": 30850,
        "client_order_id": "tlive_dryrun_001",
    }
    base.update(kw)
    return base


_POLICY = {
    "live_pilot_enabled": True,
    "live_order_allowed": True,
    "adapter_status": "enabled",
    "max_order_krw": 100_000,
}


# ── 1. send_buy_order 동작 ───────────────────────────────

class TestDryRunSendBuyOrder(unittest.TestCase):
    def setUp(self):
        self.transport = DryRunTossLiveTransport()

    def test_ok_true_on_valid(self):
        r = self.transport.send_buy_order(_payload())
        self.assertTrue(r["ok"])

    def test_blocked_true_even_when_ok(self):
        r = self.transport.send_buy_order(_payload())
        self.assertTrue(r["blocked"])

    def test_live_order_sent_false(self):
        r = self.transport.send_buy_order(_payload())
        self.assertFalse(r["live_order_sent"])

    def test_transport_status_dry_run(self):
        r = self.transport.send_buy_order(_payload())
        self.assertEqual(r["transport_status"], "dry_run_schema_ready")

    def test_order_request_preview_present(self):
        r = self.transport.send_buy_order(_payload())
        self.assertIn("order_request_preview", r)
        prev = r["order_request_preview"]
        self.assertEqual(prev["symbol"], "091180")
        self.assertEqual(prev["side"], "BUY")
        self.assertEqual(prev["orderType"], "LIMIT")
        self.assertEqual(prev["quantity"], "1")
        self.assertEqual(prev["price"], "30850")
        self.assertEqual(prev["timeInForce"], "DAY")
        self.assertFalse(prev["confirmHighValueOrder"])

    def test_message_says_not_sent(self):
        r = self.transport.send_buy_order(_payload())
        self.assertIn("아직 주문 전송 안 함", r["message"])
        self.assertIn("실제 주문 아님", r["message"])

    def test_no_success_cta(self):
        r = self.transport.send_buy_order(_payload())
        for bad in ("자동매매 시작", "주문 실행", "매수하기", "실주문: 활성"):
            self.assertNotIn(bad, str(r))


# ── 2. 검증 실패 시 차단 ─────────────────────────────────

class TestDryRunBlocked(unittest.TestCase):
    def setUp(self):
        self.transport = DryRunTossLiveTransport()

    def test_invalid_side_blocked(self):
        r = self.transport.send_buy_order(_payload(side="short"))
        self.assertFalse(r["ok"])
        self.assertTrue(r["blocked"])
        self.assertFalse(r["live_order_sent"])
        self.assertNotIn("order_request_preview", r)

    def test_sell_allowed_but_dry_run_stays_blocked(self):
        # BUY+SELL 지정가 지원 — sell도 schema ok, 단 dry-run이므로 전송은 안 됨
        r = self.transport.send_buy_order(_payload(side="sell"))
        self.assertTrue(r["ok"])
        self.assertTrue(r["blocked"])
        self.assertFalse(r["live_order_sent"])
        self.assertEqual(r["order_request_preview"]["side"], "SELL")

    def test_over_limit_blocked_with_explicit_cap(self):
        # 기본 cap은 0(없음) — payload에 max_order_krw를 명시한 경우만 한도 차단
        r = self.transport.send_buy_order(
            _payload(
                limit_price=150000,
                estimated_amount_krw=150000,
                max_order_krw=100_000,
            )
        )
        self.assertFalse(r["ok"])
        self.assertFalse(r["live_order_sent"])

    def test_no_default_cap_allows_amount(self):
        r = self.transport.send_buy_order(
            _payload(limit_price=150000, estimated_amount_krw=150000)
        )
        self.assertTrue(r["ok"])
        self.assertFalse(r["live_order_sent"])


# ── 3. requests.post 호출 없음 (소스 검사) ───────────────

class TestNoHttpInDryRun(unittest.TestCase):
    def test_no_requests_post_in_source(self):
        src = (_ROOT / "core" / "toss_live_transport.py").read_text(encoding="utf-8")
        src_no_doc = re.sub(r'"""[\s\S]*?"""', "", src)
        src_no_doc = re.sub(r"#[^\n]*", "", src_no_doc)
        self.assertNotIn("requests.post", src_no_doc)
        self.assertNotIn("requests.put", src_no_doc)

    def test_no_order_endpoint_path_in_source(self):
        src = (_ROOT / "core" / "toss_live_transport.py").read_text(encoding="utf-8")
        for path in ("/api/v1/orders", "/api/v1/buy", "/api/v1/sell", "/trade"):
            self.assertNotIn(path, src)


# ── 4. dispatch 주입 ─────────────────────────────────────

class TestDispatchWithDryRun(unittest.TestCase):
    def _dispatch(self, **payload_kw):
        transport = DryRunTossLiveTransport()

        def transport_callable(payload, policy):
            return transport.send_buy_order(payload)

        return dispatch_toss_order_live(
            _payload(**payload_kw), _POLICY, transport=transport_callable
        )

    def test_dispatch_live_order_sent_false(self):
        r = self._dispatch()
        self.assertFalse(r["live_order_sent"])

    def test_dispatch_ok_false(self):
        # dry-run은 live_order_sent=False이므로 dispatch는 전송 성공으로 보지 않음
        r = self._dispatch()
        self.assertFalse(r["ok"])

    def test_dispatch_no_sensitive(self):
        r = self._dispatch()
        for kw in ("accountNo", "Bearer", "Authorization", "APP_SECRET"):
            self.assertNotIn(kw, str(r))

    def test_dispatch_message_not_sent(self):
        r = self._dispatch()
        self.assertIn("주문 전송", r["message"])


# ── 5. 민감정보 없음 ─────────────────────────────────────

class TestNoSensitiveInResult(unittest.TestCase):
    def test_result_no_sensitive(self):
        r = DryRunTossLiveTransport().send_buy_order(_payload())
        s = str(r)
        for kw in ("accountNo", "Bearer", "Authorization",
                   "X-Tossinvest-Account", "APP_KEY", "APP_SECRET"):
            self.assertNotIn(kw, s)


if __name__ == "__main__":
    unittest.main()
