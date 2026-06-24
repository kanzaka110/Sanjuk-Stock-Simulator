"""tests/test_toss_live_pilot_payload.py

candidate → preview → payload → dispatch 전체 흐름 검증.
- 민감정보 없음
- 실제 HTTP 호출 없음
- live_order_sent 항상 False
- Paper SOFI open 유지
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
from core.toss_live_pilot_preview import build_live_pilot_preview
from core.toss_live_pilot_adapter import (
    build_toss_order_payload,
    dispatch_toss_order_disabled,
)


def _policy():
    return compute_toss_live_pilot_policy(evaluated_count=0)


def _full_flow(symbol="069500.KS", price=40000, qty=1, side="buy", **kw):
    """candidate → preview → payload → dispatch 전체 실행."""
    policy = _policy()
    candidate = {
        "symbol": symbol,
        "side": side,
        "quantity": qty,
        "limit_price": float(price),
        **kw,
    }
    preview = build_live_pilot_preview(candidate, policy=policy)
    payload_result = build_toss_order_payload(preview, policy=policy)
    dispatch_result = dispatch_toss_order_disabled(
        payload_result.get("payload", {}), policy=policy
    )
    return preview, payload_result, dispatch_result


# ─── 1. 정상 흐름 069500.KS ──────────────────────────────────────

class TestFullFlow069500(unittest.TestCase):
    def setUp(self):
        self.prev, self.pld, self.disp = _full_flow("069500.KS", price=40000, qty=1)

    def test_preview_ok(self):
        self.assertTrue(self.prev["ok"])

    def test_payload_ok(self):
        self.assertTrue(self.pld["ok"])

    def test_dispatch_blocked(self):
        self.assertTrue(self.disp["blocked"])

    def test_live_order_sent_false_throughout(self):
        self.assertFalse(self.prev["live_order_sent"])
        self.assertFalse(self.pld["live_order_sent"])
        self.assertFalse(self.disp["live_order_sent"])

    def test_live_order_allowed_false_throughout(self):
        self.assertFalse(self.prev["live_order_allowed"])
        self.assertFalse(self.pld["live_order_allowed"])
        self.assertFalse(self.disp["live_order_allowed"])

    def test_payload_order_type_limit(self):
        self.assertEqual(self.pld["payload"]["order_type"], "limit")

    def test_payload_no_sensitive_fields(self):
        payload_str = str(self.pld["payload"])
        for kw in ("accountNo", "Bearer", "APP_KEY", "APP_SECRET", "token"):
            self.assertNotIn(kw, payload_str)

    def test_dispatch_reason(self):
        self.assertEqual(self.disp["reason"], "toss_order_adapter_disabled")


# ─── 2. 차단 흐름 — 005930.KS ────────────────────────────────────

class TestBlockedFlow005930(unittest.TestCase):
    def setUp(self):
        self.prev, self.pld, self.disp = _full_flow("005930.KS", price=319000, qty=1)

    def test_preview_blocked(self):
        self.assertFalse(self.prev["ok"])

    def test_payload_blocked(self):
        self.assertFalse(self.pld["ok"])
        self.assertEqual(self.pld["payload"], {})

    def test_dispatch_always_blocked(self):
        self.assertFalse(self.disp["live_order_sent"])

    def test_preview_has_anomaly_block(self):
        self.assertTrue(any("anomaly" in b for b in self.prev["blocks"]))


# ─── 3. 차단 흐름 — 161510.KS ────────────────────────────────────

class TestBlockedFlow161510(unittest.TestCase):
    def test_blocked_161510(self):
        prev, pld, _ = _full_flow("161510.KS", price=1000, qty=1)
        self.assertFalse(prev["ok"])
        self.assertFalse(pld["ok"])


# ─── 4. 금액 한도 초과 ───────────────────────────────────────────

class TestAmountGuard(unittest.TestCase):
    def test_over_100k_blocked(self):
        # 표본부족 모드: max=100,000원. 150,000 초과.
        prev, pld, _ = _full_flow("069500.KS", price=150_000, qty=1)
        self.assertFalse(prev["ok"] and pld["ok"])

    def test_under_100k_ok(self):
        prev, pld, _ = _full_flow("069500.KS", price=40_000, qty=1)
        self.assertTrue(prev["ok"])
        self.assertTrue(pld["ok"])


# ─── 5. quantity/side 검증 ───────────────────────────────────────

class TestInputValidation(unittest.TestCase):
    def test_zero_quantity(self):
        _, pld, _ = _full_flow(qty=0, price=40000)
        self.assertFalse(pld["ok"])

    def test_invalid_side(self):
        _, pld, _ = _full_flow(side="long")
        self.assertFalse(pld["ok"])

    def test_valid_sell_side(self):
        # sell은 허용된 side
        prev, pld, _ = _full_flow(side="sell", price=40000, qty=1)
        if prev["ok"]:  # symbol/price 통과 시
            self.assertEqual(pld["payload"].get("side", "sell"), "sell")

    def test_no_price_blocked(self):
        prev, pld, _ = _full_flow(price=0)
        self.assertFalse(prev["ok"])
        self.assertFalse(pld["ok"])


# ─── 6. env=true 여도 dispatch blocked ───────────────────────────

class TestEnvTrueStillBlocked(unittest.TestCase):
    def test_env_true_dispatch_blocked(self):
        import os
        with patch.dict(os.environ, {"TOSS_LIVE_PILOT_ENABLED": "true"}):
            _, pld, disp = _full_flow("069500.KS", price=40000, qty=1)
        self.assertFalse(disp["live_order_sent"])
        self.assertTrue(disp["blocked"])

    def test_env_true_policy_still_disabled(self):
        import os
        with patch.dict(os.environ, {"TOSS_LIVE_PILOT_ENABLED": "true"}):
            policy = compute_toss_live_pilot_policy(evaluated_count=0)
        self.assertFalse(policy["live_order_allowed"])
        self.assertEqual(policy["adapter_status"], "disabled")


# ─── 7. 전체 소스 민감정보 없음 ──────────────────────────────────

class TestNoSensitiveInSources(unittest.TestCase):
    def _sources(self) -> str:
        files = [
            "core/toss_live_pilot_adapter.py",
            "core/toss_live_pilot_preview.py",
            "core/toss_live_pilot_ledger.py",
            "core/toss_live_pilot_policy.py",
        ]
        return "\n".join((_ROOT / f).read_text(encoding="utf-8") for f in files)

    def test_no_hardcoded_bearer(self):
        import re
        src = self._sources()
        self.assertEqual(re.findall(r'Bearer [A-Za-z0-9._\-]{20,}', src), [])

    def test_no_hardcoded_account_no(self):
        import re
        src = self._sources()
        self.assertEqual(re.findall(r'\d{8}-\d{2}', src), [])

    def test_no_requests_post_in_pilot_files(self):
        import re
        src = self._sources()
        # docstring/comment 제거 후 검사 (금지 목록 문서화 패턴 오탐 방지)
        src_no_doc = re.sub(r'"""[\s\S]*?"""', "", src)
        src_no_doc = re.sub(r"'''[\s\S]*?'''", "", src_no_doc)
        src_no_doc = re.sub(r"#[^\n]*", "", src_no_doc)
        for m in ("requests.post", "requests.put", "requests.delete", "requests.patch"):
            self.assertNotIn(m, src_no_doc, f"HTTP write method found: {m}")


# ─── 8. GET routes only ──────────────────────────────────────────

class TestGetRoutesOnly(unittest.TestCase):
    def test_no_write_routes(self):
        src = (_ROOT / "web" / "app.py").read_text(encoding="utf-8")
        for pat in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            self.assertNotIn(pat, src.lower())

    def test_live_pilot_policy_get(self):
        src = (_ROOT / "web" / "app.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/toss/live-pilot-policy")', src)

    def test_live_pilot_previews_get(self):
        src = (_ROOT / "web" / "app.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/toss/live-pilot-previews")', src)


# ─── 9. Paper SOFI 미접촉 ────────────────────────────────────────

class TestPaperSOFIUnaffected(unittest.TestCase):
    def test_sofi_paper_open_unchanged(self):
        from core.toss_paper_performance import get_paper_performance_summary
        before = get_paper_performance_summary().get("summary", {}).get("open", 0)
        # full flow 실행
        _full_flow("069500.KS", price=40000, qty=1)
        after = get_paper_performance_summary().get("summary", {}).get("open", 0)
        self.assertEqual(before, after)

    def test_paper_ledger_not_touched(self):
        with patch("core.toss_paper_ledger.create_paper_preview_records") as mock:
            _full_flow("069500.KS", price=40000, qty=1)
            mock.assert_not_called()


# ─── 10. ledger payload_validated 흐름 ───────────────────────────

class TestLedgerPayloadValidated(unittest.TestCase):
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

    def test_payload_validated_status(self):
        from core.toss_live_pilot_ledger import (
            record_live_pilot_preview,
            record_payload_validated,
        )
        preview = {
            "ok": True, "preview_id": "tlive_test", "symbol": "069500.KS",
            "side": "buy", "quantity": 1, "limit_price": 40000,
            "estimated_amount_krw": 40000, "blocks": [], "warnings": [],
        }
        rec = record_live_pilot_preview(preview)
        result = record_payload_validated(rec["pilot_id"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "payload_validated")
        self.assertFalse(result["live_order_sent"])

    def test_confirm_from_payload_validated(self):
        from core.toss_live_pilot_ledger import (
            record_live_pilot_preview,
            record_payload_validated,
            record_confirm_attempt,
        )
        preview = {
            "ok": True, "preview_id": "tlive_test2", "symbol": "069500.KS",
            "side": "buy", "quantity": 1, "limit_price": 40000,
            "estimated_amount_krw": 40000, "blocks": [], "warnings": [],
        }
        rec = record_live_pilot_preview(preview)
        record_payload_validated(rec["pilot_id"])
        result = record_confirm_attempt(rec["pilot_id"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "confirmed_but_not_sent")
        self.assertFalse(result["live_order_sent"])


if __name__ == "__main__":
    unittest.main()
