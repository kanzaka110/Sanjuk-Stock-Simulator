"""tests/test_toss_live_pilot_verification.py

Hermes 교차검증 ledger — CRUD + is_verification_passed 테스트.
- create_verification_request → PENDING
- record_hermes_verification → PASS/HOLD/BLOCK/ERROR
- is_verification_passed: PASS+미만료=True, PASS+만료=False, HOLD/BLOCK/ERROR=False
- live_order_allowed 항상 False
- verification_summary / list_verifications read-only
"""

import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

KST = timezone(timedelta(hours=9))


def _tmp_db_patch():
    tmp = tempfile.mkdtemp()
    return patch(
        "core.toss_live_pilot_verification._db_path",
        return_value=Path(tmp) / "test_verif.db",
    )


def _reset_schema():
    import core.toss_live_pilot_verification as m
    m._schema_created = False


def _make_preview(pilot_id: str, symbol: str = "091180.KS") -> dict:
    return {
        "pilot_id": pilot_id,
        "preview_id": pilot_id,
        "symbol": symbol,
        "side": "buy",
        "quantity": 1,
        "limit_price": 30_000.0,
        "estimated_amount_krw": 30_000.0,
    }


# ── 1. create_verification_request ───────────────────────

class TestCreateVerificationRequest(unittest.TestCase):
    def setUp(self):
        self._p = _tmp_db_patch()
        self._p.start()
        _reset_schema()

    def tearDown(self):
        self._p.stop()
        _reset_schema()

    def test_returns_ok_true(self):
        from core.toss_live_pilot_verification import create_verification_request
        r = create_verification_request(_make_preview("pilot_001"), pilot_id="pilot_001")
        self.assertTrue(r["ok"])

    def test_status_pending(self):
        from core.toss_live_pilot_verification import create_verification_request
        r = create_verification_request(_make_preview("pilot_002"), pilot_id="pilot_002")
        self.assertEqual(r["status"], "PENDING")

    def test_verification_id_format(self):
        from core.toss_live_pilot_verification import create_verification_request
        r = create_verification_request(_make_preview("pilot_003"), pilot_id="pilot_003")
        self.assertTrue(r["verification_id"].startswith("hv_"))

    def test_requested_at_present(self):
        from core.toss_live_pilot_verification import create_verification_request
        r = create_verification_request(_make_preview("pilot_004"), pilot_id="pilot_004")
        self.assertIn("requested_at", r)
        self.assertTrue(r["requested_at"])

    def test_decision_ref_is_derived_and_persisted(self):
        from core.toss_live_pilot_verification import (
            create_verification_request,
            get_verification_for_pilot,
        )
        r = create_verification_request(_make_preview("pilot_trace"), pilot_id="pilot_trace")
        self.assertEqual(r["decision_ref"], "execution_decision:pilot_trace")
        stored = get_verification_for_pilot("pilot_trace")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored["decision_ref"], "execution_decision:pilot_trace")

    def test_invalid_decision_ref_is_rejected(self):
        from core.toss_live_pilot_verification import create_verification_request
        preview = _make_preview("pilot_bad")
        preview["decision_ref"] = "Bearer forbidden secret"
        r = create_verification_request(preview, pilot_id="pilot_bad")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "invalid_decision_ref")


# ── 2. record_hermes_verification ────────────────────────

class TestRecordHermesVerification(unittest.TestCase):
    def setUp(self):
        self._p = _tmp_db_patch()
        self._p.start()
        _reset_schema()
        from core.toss_live_pilot_verification import create_verification_request
        res = create_verification_request(_make_preview("pilot_rh"), pilot_id="pilot_rh")
        self.verification_id = res["verification_id"]

    def tearDown(self):
        self._p.stop()
        _reset_schema()

    def test_pass_ok(self):
        from core.toss_live_pilot_verification import record_hermes_verification
        r = record_hermes_verification(self.verification_id, "PASS", ["ok"], {})
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "PASS")

    def test_pass_projects_exact_finalizer_send_result(self):
        from core import toss_live_pilot_verification as verification
        finalizer = {
            "ok": True,
            "live_order_sent": True,
            "broker_order_id": "safe-order-id",
        }
        with patch.object(
            verification, "_try_trigger_autonomous_finalize", return_value=finalizer,
        ):
            result = verification.record_hermes_verification(
                self.verification_id, "PASS", ["ok"], {},
            )
        self.assertIs(result["finalizer_ok"], True)
        self.assertIs(result["live_order_sent"], True)

    def test_pass_does_not_launder_truthy_finalizer_values(self):
        from core import toss_live_pilot_verification as verification
        finalizer = {"ok": 1, "live_order_sent": "true"}
        with patch.object(
            verification, "_try_trigger_autonomous_finalize", return_value=finalizer,
        ):
            result = verification.record_hermes_verification(
                self.verification_id, "PASS", ["ok"], {},
            )
        self.assertIs(result["finalizer_ok"], False)
        self.assertIs(result["live_order_sent"], False)

    def test_pass_has_expires_at(self):
        from core.toss_live_pilot_verification import record_hermes_verification
        r = record_hermes_verification(self.verification_id, "PASS", [], {}, ttl_minutes=10)
        self.assertIsNotNone(r.get("expires_at"))

    def test_hold_no_expires_at(self):
        from core.toss_live_pilot_verification import record_hermes_verification
        r = record_hermes_verification(self.verification_id, "HOLD", ["price_stale"], {})
        self.assertIsNone(r.get("expires_at"))

    def test_block_status(self):
        from core.toss_live_pilot_verification import record_hermes_verification
        r = record_hermes_verification(self.verification_id, "BLOCK", ["symbol_blocked"], {})
        self.assertEqual(r["status"], "BLOCK")

    def test_error_status(self):
        from core.toss_live_pilot_verification import record_hermes_verification
        r = record_hermes_verification(self.verification_id, "ERROR", ["timeout"], {})
        self.assertEqual(r["status"], "ERROR")

    def test_live_order_allowed_always_false(self):
        from core.toss_live_pilot_verification import record_hermes_verification
        r = record_hermes_verification(self.verification_id, "PASS", [], {})
        self.assertFalse(r["live_order_allowed"])

    def test_invalid_status_rejected(self):
        from core.toss_live_pilot_verification import record_hermes_verification
        r = record_hermes_verification(self.verification_id, "APPROVED", [], {})
        self.assertFalse(r.get("ok", True))

    def test_nonexistent_verification_id(self):
        from core.toss_live_pilot_verification import record_hermes_verification
        r = record_hermes_verification("hv_nonexistent", "PASS", [], {})
        self.assertFalse(r.get("ok", True))


# ── 3. get_verification_for_pilot ────────────────────────

class TestGetVerificationForPilot(unittest.TestCase):
    def setUp(self):
        self._p = _tmp_db_patch()
        self._p.start()
        _reset_schema()

    def tearDown(self):
        self._p.stop()
        _reset_schema()

    def test_none_for_unknown_pilot(self):
        from core.toss_live_pilot_verification import get_verification_for_pilot
        self.assertIsNone(get_verification_for_pilot("no_such_pilot"))

    def test_returns_dict_after_create(self):
        from core.toss_live_pilot_verification import (
            create_verification_request,
            get_verification_for_pilot,
        )
        create_verification_request(_make_preview("pilot_gv"), pilot_id="pilot_gv")
        rec = get_verification_for_pilot("pilot_gv")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "PENDING")

    def test_reasons_deserialized_as_list(self):
        from core.toss_live_pilot_verification import (
            create_verification_request,
            record_hermes_verification,
            get_verification_for_pilot,
        )
        res = create_verification_request(_make_preview("pilot_gv2"), pilot_id="pilot_gv2")
        record_hermes_verification(res["verification_id"], "HOLD", ["reason_a"], {})
        rec = get_verification_for_pilot("pilot_gv2")
        self.assertIsInstance(rec["reasons"], list)
        self.assertIn("reason_a", rec["reasons"])


# ── 4. is_verification_passed ────────────────────────────

class TestIsVerificationPassed(unittest.TestCase):
    def setUp(self):
        self._p = _tmp_db_patch()
        self._p.start()
        _reset_schema()

    def tearDown(self):
        self._p.stop()
        _reset_schema()

    def _create_and_record(self, pilot_id: str, status: str, ttl: int = 10) -> str:
        from core.toss_live_pilot_verification import (
            create_verification_request,
            record_hermes_verification,
        )
        res = create_verification_request(_make_preview(pilot_id), pilot_id=pilot_id)
        vid = res["verification_id"]
        record_hermes_verification(vid, status, [], {}, ttl_minutes=ttl)
        return vid

    def test_no_record_fails(self):
        from core.toss_live_pilot_verification import is_verification_passed
        ok, reasons, rec = is_verification_passed("nonexistent_pilot")
        self.assertFalse(ok)
        self.assertIn("hermes_verification_not_found", reasons)

    def test_pending_fails(self):
        from core.toss_live_pilot_verification import (
            create_verification_request,
            is_verification_passed,
        )
        create_verification_request(_make_preview("pilot_pending"), pilot_id="pilot_pending")
        ok, reasons, _ = is_verification_passed("pilot_pending")
        self.assertFalse(ok)
        self.assertIn("hermes_verification_pending", reasons)

    def test_hold_fails(self):
        from core.toss_live_pilot_verification import is_verification_passed
        self._create_and_record("pilot_hold", "HOLD")
        ok, reasons, _ = is_verification_passed("pilot_hold")
        self.assertFalse(ok)
        self.assertIn("hermes_verification_hold", reasons)

    def test_block_fails(self):
        from core.toss_live_pilot_verification import is_verification_passed
        self._create_and_record("pilot_block", "BLOCK")
        ok, reasons, _ = is_verification_passed("pilot_block")
        self.assertFalse(ok)
        self.assertIn("hermes_verification_block", reasons)

    def test_error_fails(self):
        from core.toss_live_pilot_verification import is_verification_passed
        self._create_and_record("pilot_error", "ERROR")
        ok, reasons, _ = is_verification_passed("pilot_error")
        self.assertFalse(ok)
        self.assertIn("hermes_verification_error", reasons)

    def test_pass_not_expired_succeeds(self):
        from core.toss_live_pilot_verification import is_verification_passed
        self._create_and_record("pilot_pass_ok", "PASS", ttl=10)
        now_fresh = datetime.now(KST)  # before expiry
        ok, reasons, rec = is_verification_passed("pilot_pass_ok", now=now_fresh)
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_pass_expired_fails_stale(self):
        from core.toss_live_pilot_verification import is_verification_passed
        self._create_and_record("pilot_pass_stale", "PASS", ttl=1)
        # 2분 후로 시뮬레이션
        future = datetime.now(KST) + timedelta(minutes=2)
        ok, reasons, _ = is_verification_passed("pilot_pass_stale", now=future)
        self.assertFalse(ok)
        self.assertIn("hermes_verification_stale", reasons)

    def test_returns_record_dict(self):
        from core.toss_live_pilot_verification import is_verification_passed
        self._create_and_record("pilot_pass_rec", "PASS", ttl=10)
        _, _, rec = is_verification_passed("pilot_pass_rec")
        self.assertIsInstance(rec, dict)
        self.assertIn("verification_id", rec)


# ── 5. build_hermes_verification_context ─────────────────

class TestBuildHermesVerificationContext(unittest.TestCase):
    def _policy(self):
        return {
            "adapter_status": "disabled",
            "live_order_allowed": False,
            "max_order_krw": 100_000,
            "blocked_symbols": ["005930.KS", "MU"],
        }

    def test_fields_present(self):
        from core.toss_live_pilot_verification import build_hermes_verification_context
        ctx = build_hermes_verification_context(_make_preview("p_ctx"), self._policy())
        for k in ("symbol", "side", "quantity", "limit_price", "adapter_status", "checks"):
            self.assertIn(k, ctx)

    def test_live_order_allowed_false(self):
        from core.toss_live_pilot_verification import build_hermes_verification_context
        ctx = build_hermes_verification_context(_make_preview("p_ctx2"), self._policy())
        self.assertFalse(ctx["live_order_allowed"])

    def test_amount_guard_ok(self):
        from core.toss_live_pilot_verification import build_hermes_verification_context
        ctx = build_hermes_verification_context(_make_preview("p_ctx3"), self._policy())
        self.assertEqual(ctx["checks"]["amount_guard"], "ok")

    def test_autonomous_policy_does_not_require_user_final_approval(self):
        from core.toss_live_pilot_verification import build_hermes_verification_context
        policy = {
            **self._policy(),
            "requires_user_confirmation": False,
            "requires_second_confirmation": False,
            "autonomous_mode": True,
        }
        ctx = build_hermes_verification_context(_make_preview("p_auto"), policy)
        self.assertEqual(ctx["checks"]["user_final_approval_required"], "false")
        self.assertEqual(
            ctx["checks"]["execution_policy"],
            "autonomous_after_hermes_pass_and_deterministic_gates",
        )

    def test_manual_policy_keeps_user_final_approval(self):
        from core.toss_live_pilot_verification import build_hermes_verification_context
        policy = {
            **self._policy(),
            "requires_user_confirmation": True,
            "requires_second_confirmation": True,
            "autonomous_mode": False,
        }
        ctx = build_hermes_verification_context(_make_preview("p_manual"), policy)
        self.assertEqual(ctx["checks"]["user_final_approval_required"], "true")
        self.assertEqual(ctx["checks"]["execution_policy"], "manual_user_confirmation")

    def test_blocked_symbol_detected(self):
        from core.toss_live_pilot_verification import build_hermes_verification_context
        preview = _make_preview("p_blocked", symbol="MU")
        ctx = build_hermes_verification_context(preview, self._policy())
        self.assertIn("FAIL", ctx["checks"]["blocked_symbol"])


# ── 6. format_hermes_verification_request ────────────────

class TestFormatHermesVerificationRequest(unittest.TestCase):
    def test_block_delimiters(self):
        from core.toss_live_pilot_verification import (
            build_hermes_verification_context,
            format_hermes_verification_request,
        )
        policy = {"adapter_status": "disabled", "live_order_allowed": False,
                  "max_order_krw": 100_000, "blocked_symbols": []}
        ctx = build_hermes_verification_context(_make_preview("p_fmt"), policy)
        text = format_hermes_verification_request(ctx)
        self.assertIn("[HERMES_LIVE_PILOT_VERIFY]", text)
        self.assertIn("[/HERMES_LIVE_PILOT_VERIFY]", text)

    def test_no_sensitive_info(self):
        from core.toss_live_pilot_verification import (
            build_hermes_verification_context,
            format_hermes_verification_request,
        )
        policy = {"adapter_status": "disabled", "live_order_allowed": False,
                  "max_order_krw": 100_000, "blocked_symbols": []}
        ctx = build_hermes_verification_context(_make_preview("p_fmt2"), policy)
        text = format_hermes_verification_request(ctx)
        for kw in ("accountNo", "Bearer", "APP_KEY", "APP_SECRET"):
            self.assertNotIn(kw, text)

    def test_live_order_allowed_false_in_block(self):
        from core.toss_live_pilot_verification import (
            build_hermes_verification_context,
            format_hermes_verification_request,
        )
        policy = {"adapter_status": "disabled", "live_order_allowed": False,
                  "max_order_krw": 100_000, "blocked_symbols": []}
        ctx = build_hermes_verification_context(_make_preview("p_fmt3"), policy)
        text = format_hermes_verification_request(ctx)
        self.assertIn("live_order_allowed: false", text)


# ── 7. verification_summary ──────────────────────────────

class TestVerificationSummary(unittest.TestCase):
    def setUp(self):
        self._p = _tmp_db_patch()
        self._p.start()
        _reset_schema()

    def tearDown(self):
        self._p.stop()
        _reset_schema()

    def test_returns_dict(self):
        from core.toss_live_pilot_verification import verification_summary
        r = verification_summary()
        self.assertIsInstance(r, dict)
        self.assertIn("summary", r)

    def test_live_order_allowed_false(self):
        from core.toss_live_pilot_verification import verification_summary
        r = verification_summary()
        self.assertFalse(r["live_order_allowed"])

    def test_stale_count_present(self):
        from core.toss_live_pilot_verification import verification_summary
        r = verification_summary()
        self.assertIn("STALE", r["summary"])


# ── 8. list_verifications ────────────────────────────────

class TestListVerifications(unittest.TestCase):
    def setUp(self):
        self._p = _tmp_db_patch()
        self._p.start()
        _reset_schema()

    def tearDown(self):
        self._p.stop()
        _reset_schema()

    def test_empty_list_on_fresh_db(self):
        from core.toss_live_pilot_verification import list_verifications
        self.assertEqual(list_verifications(), [])

    def test_returns_list_after_insert(self):
        from core.toss_live_pilot_verification import (
            create_verification_request,
            list_verifications,
        )
        create_verification_request(_make_preview("pilot_lv"), pilot_id="pilot_lv")
        recs = list_verifications(limit=10)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["pilot_id"], "pilot_lv")


if __name__ == "__main__":
    unittest.main()
