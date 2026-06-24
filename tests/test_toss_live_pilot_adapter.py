"""tests/test_toss_live_pilot_adapter.py

build_toss_order_payload + dispatch_toss_order_disabled 테스트.
"""

import unittest
from unittest.mock import patch

import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.toss_live_pilot_adapter import (
    build_toss_order_payload,
    dispatch_toss_order_disabled,
    get_adapter_status,
    send_live_pilot_order_stub,
)


_POLICY_SAMPLE = {
    "max_order_krw": 100_000,
    "blocked_symbols": ["161510.KS", "005930.KS"],
    "live_order_allowed": False,
    "adapter_status": "disabled",
}

def _ok_preview(symbol="069500.KS", price=40000, qty=1, side="buy"):
    return {
        "ok": True,
        "symbol": symbol,
        "side": side,
        "quantity": qty,
        "limit_price": float(price),
        "estimated_amount_krw": float(price * qty),
        "currency": "KRW",
        "blocks": [],
    }


# ─── 1. payload 생성 기본 ─────────────────────────────────────────

class TestBuildPayloadBasic(unittest.TestCase):
    def _build(self, **kw):
        preview = _ok_preview(**kw)
        return build_toss_order_payload(preview, policy=_POLICY_SAMPLE)

    def test_ok_true_for_valid(self):
        r = self._build()
        self.assertTrue(r["ok"])

    def test_dry_run_flag(self):
        r = self._build()
        self.assertTrue(r["dry_run"])

    def test_adapter_disabled(self):
        r = self._build()
        self.assertEqual(r["adapter_status"], "disabled")

    def test_live_order_allowed_false(self):
        r = self._build()
        self.assertFalse(r["live_order_allowed"])

    def test_live_order_sent_false(self):
        r = self._build()
        self.assertFalse(r["live_order_sent"])

    def test_payload_present(self):
        r = self._build()
        self.assertIn("payload", r)
        self.assertNotEqual(r["payload"], {})

    def test_payload_symbol(self):
        r = self._build(symbol="069500.KS")
        self.assertEqual(r["payload"]["symbol"], "069500.KS")

    def test_payload_order_type_limit(self):
        r = self._build()
        self.assertEqual(r["payload"]["order_type"], "limit")

    def test_payload_side_buy(self):
        r = self._build(side="buy")
        self.assertEqual(r["payload"]["side"], "buy")

    def test_payload_quantity(self):
        r = self._build(qty=2, price=30000)
        self.assertEqual(r["payload"]["quantity"], 2)

    def test_payload_estimated_amount(self):
        r = self._build(price=40000, qty=1)
        self.assertLessEqual(r["payload"]["estimated_amount_krw"], 100_000)

    def test_warnings_contain_dry_run(self):
        r = self._build()
        self.assertTrue(any("dry-run" in w for w in r["warnings"]))

    def test_warnings_contain_no_order_sent(self):
        r = self._build()
        self.assertTrue(any("아직 주문 전송 안 함" in w for w in r["warnings"]))

    def test_warnings_api_disabled(self):
        r = self._build()
        self.assertTrue(any("비활성" in w for w in r["warnings"]))


# ─── 2. 민감정보 없음 ────────────────────────────────────────────

class TestNoSensitiveInPayload(unittest.TestCase):
    def test_no_account_no_in_payload(self):
        r = build_toss_order_payload(_ok_preview(), policy=_POLICY_SAMPLE)
        payload_str = str(r["payload"])
        self.assertNotIn("accountNo", payload_str)

    def test_no_token_in_payload(self):
        r = build_toss_order_payload(_ok_preview(), policy=_POLICY_SAMPLE)
        payload_str = str(r)
        self.assertNotIn("Bearer ", payload_str)

    def test_no_app_key_in_payload(self):
        r = build_toss_order_payload(_ok_preview(), policy=_POLICY_SAMPLE)
        payload_str = str(r)
        for kw in ("APP_KEY", "APP_SECRET", "KIS_APP"):
            self.assertNotIn(kw, payload_str)


# ─── 3. 차단 케이스 ──────────────────────────────────────────────

class TestPayloadBlocked(unittest.TestCase):
    def _build_blocked(self, **kw):
        preview = _ok_preview(**kw)
        return build_toss_order_payload(preview, policy=_POLICY_SAMPLE)

    def test_blocked_symbol_161510(self):
        preview = _ok_preview(symbol="161510.KS", price=1000, qty=1)
        preview["blocks"] = ["위험_저신뢰_종목"]
        preview["ok"] = False
        r = build_toss_order_payload(preview, policy=_POLICY_SAMPLE)
        self.assertFalse(r["ok"])
        self.assertEqual(r["payload"], {})

    def test_blocked_symbol_005930(self):
        preview = _ok_preview(symbol="005930.KS", price=319000, qty=1)
        preview["blocks"] = ["price_anomaly_history"]
        preview["ok"] = False
        r = build_toss_order_payload(preview, policy=_POLICY_SAMPLE)
        self.assertFalse(r["ok"])

    def test_quantity_zero_blocked(self):
        preview = _ok_preview(qty=0)
        r = build_toss_order_payload(preview, policy=_POLICY_SAMPLE)
        self.assertFalse(r["ok"])
        self.assertTrue(any("quantity" in b for b in r["blocks"]))

    def test_quantity_negative_blocked(self):
        preview = _ok_preview()
        preview["quantity"] = -1
        r = build_toss_order_payload(preview, policy=_POLICY_SAMPLE)
        self.assertFalse(r["ok"])

    def test_invalid_price_blocked(self):
        preview = _ok_preview(price=0)
        r = build_toss_order_payload(preview, policy=_POLICY_SAMPLE)
        self.assertFalse(r["ok"])
        self.assertTrue(any("price" in b for b in r["blocks"]))

    def test_invalid_side_blocked(self):
        preview = _ok_preview(side="long")
        r = build_toss_order_payload(preview, policy=_POLICY_SAMPLE)
        self.assertFalse(r["ok"])
        self.assertTrue(any("side" in b for b in r["blocks"]))

    def test_amount_over_limit_blocked(self):
        # 150,000 > 100,000 한도
        preview = _ok_preview(price=150_000, qty=1)
        r = build_toss_order_payload(preview, policy=_POLICY_SAMPLE)
        self.assertFalse(r["ok"])
        self.assertTrue(any("한도" in b for b in r["blocks"]))


# ─── 4. dispatch 항상 차단 ───────────────────────────────────────

class TestDispatchAlwaysBlocked(unittest.TestCase):
    def _dispatch(self):
        payload = {"symbol": "069500.KS", "side": "buy", "quantity": 1, "limit_price": 40000}
        return dispatch_toss_order_disabled(payload, policy=_POLICY_SAMPLE)

    def test_ok_false(self):
        self.assertFalse(self._dispatch()["ok"])

    def test_blocked_true(self):
        self.assertTrue(self._dispatch()["blocked"])

    def test_live_order_sent_false(self):
        self.assertFalse(self._dispatch()["live_order_sent"])

    def test_reason_adapter_disabled(self):
        self.assertEqual(self._dispatch()["reason"], "toss_order_adapter_disabled")

    def test_adapter_status_disabled(self):
        self.assertEqual(self._dispatch()["adapter_status"], "disabled")

    def test_live_order_allowed_false(self):
        self.assertFalse(self._dispatch()["live_order_allowed"])

    def test_message_no_order_sent(self):
        self.assertIn("아직 주문 전송 안 함", self._dispatch()["message"])

    def test_env_true_still_blocked(self):
        """TOSS_LIVE_PILOT_ENABLED=true 여도 dispatch는 blocked."""
        import os
        with patch.dict(os.environ, {"TOSS_LIVE_PILOT_ENABLED": "true"}):
            r = self._dispatch()
        self.assertFalse(r["live_order_sent"])
        self.assertTrue(r["blocked"])

    def test_dispatch_no_http_calls(self):
        """dispatch 내부에서 requests.post/put/delete 호출 없음 — static 확인 (docstring 제외)."""
        import re
        src = (_ROOT / "core" / "toss_live_pilot_adapter.py").read_text(encoding="utf-8")
        # docstring/comment 제거 후 검사
        src_no_doc = re.sub(r'"""[\s\S]*?"""', "", src)
        src_no_doc = re.sub(r"'''[\s\S]*?'''", "", src_no_doc)
        src_no_doc = re.sub(r"#[^\n]*", "", src_no_doc)
        for method in ("requests.post", "requests.put", "requests.delete", "requests.patch"):
            self.assertNotIn(method, src_no_doc)


# ─── 5. get_adapter_status ───────────────────────────────────────

class TestAdapterStatusInfo(unittest.TestCase):
    def test_status_disabled(self):
        self.assertEqual(get_adapter_status()["status"], "disabled")

    def test_live_order_not_allowed(self):
        self.assertFalse(get_adapter_status()["live_order_allowed"])


if __name__ == "__main__":
    unittest.main()
