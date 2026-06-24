"""tests/test_toss_live_pilot_events.py

Live pilot callback 이벤트 로그 테스트.

1. record_event: 기본 동작
2. symbol_name / symbol_label 자동 생성
3. 민감정보 차단
4. invalid event_type
5. list_events / event_summary
6. live_order_allowed 항상 false
7. 삭제 없음
8. review / cancel / confirm 이벤트 타입 커버
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _tmp_patch():
    tmp = tempfile.mkdtemp()
    return (
        Path(tmp),
        patch(
            "core.toss_live_pilot_events._db_path",
            return_value=Path(tmp) / "test_events.db",
        ),
    )


def _reset():
    import core.toss_live_pilot_events as m
    m._schema_created = False


# ── 1. 기본 record_event ──────────────────────────────────

class TestRecordEventBasic(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def test_record_returns_ok(self):
        from core.toss_live_pilot_events import record_event
        r = record_event(
            pilot_id="tlive_test_001",
            event_type="reviewed",
            status="reviewed",
        )
        self.assertTrue(r["ok"])

    def test_event_id_generated(self):
        from core.toss_live_pilot_events import record_event
        r = record_event("tlive_t", "reviewed", "reviewed")
        self.assertTrue(r["event_id"].startswith("tle_"))

    def test_live_order_sent_false_by_default(self):
        from core.toss_live_pilot_events import record_event
        r = record_event("tlive_t", "reviewed", "reviewed")
        self.assertFalse(r["live_order_sent"])

    def test_live_order_allowed_always_false(self):
        from core.toss_live_pilot_events import record_event
        r = record_event("tlive_t", "reviewed", "reviewed")
        self.assertFalse(r["live_order_allowed"])

    def test_created_at_in_result(self):
        from core.toss_live_pilot_events import record_event
        r = record_event("tlive_t", "reviewed", "reviewed")
        self.assertIn("created_at", r)

    def test_event_persisted_in_list(self):
        from core.toss_live_pilot_events import record_event, list_events
        record_event("tlive_persist", "cancelled", "cancelled")
        events = list_events(limit=10)
        found = [e for e in events if e["pilot_id"] == "tlive_persist"]
        self.assertTrue(len(found) > 0)

    def test_event_type_in_list(self):
        from core.toss_live_pilot_events import record_event, list_events
        record_event("tlive_t2", "cancelled", "cancelled")
        events = list_events(limit=10)
        types = {e["event_type"] for e in events}
        self.assertIn("cancelled", types)


# ── 2. symbol_name / symbol_label ────────────────────────

class TestSymbolLabel(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def test_symbol_name_auto_filled(self):
        from core.toss_live_pilot_events import record_event, list_events
        record_event("tlive_sym", "reviewed", "reviewed", symbol="091180.KS")
        events = list_events(limit=5)
        found = [e for e in events if e["pilot_id"] == "tlive_sym"]
        self.assertEqual(found[0]["symbol_name"], "KODEX 자동차")

    def test_symbol_label_format(self):
        from core.toss_live_pilot_events import record_event, list_events
        record_event("tlive_sym2", "reviewed", "reviewed", symbol="091180.KS")
        events = list_events(limit=5)
        found = [e for e in events if e["pilot_id"] == "tlive_sym2"]
        self.assertEqual(found[0]["symbol_label"], "KODEX 자동차 (091180.KS)")

    def test_unknown_symbol_label_is_ticker(self):
        from core.toss_live_pilot_events import record_event, list_events
        record_event("tlive_unk", "reviewed", "reviewed", symbol="UNKNOWN.KS")
        events = list_events(limit=5)
        found = [e for e in events if e["pilot_id"] == "tlive_unk"]
        self.assertEqual(found[0]["symbol_label"], "UNKNOWN.KS")

    def test_360750_label(self):
        from core.toss_live_pilot_events import record_event, list_events
        record_event("tlive_360", "reviewed", "reviewed", symbol="360750.KS")
        events = list_events(limit=5)
        found = [e for e in events if e["pilot_id"] == "tlive_360"]
        self.assertEqual(found[0]["symbol_label"], "TIGER 미국S&P500 (360750.KS)")

    def test_mu_label(self):
        from core.toss_live_pilot_events import record_event, list_events
        record_event("tlive_mu", "reviewed", "reviewed", symbol="MU")
        events = list_events(limit=5)
        found = [e for e in events if e["pilot_id"] == "tlive_mu"]
        self.assertEqual(found[0]["symbol_label"], "Micron Technology (MU)")


# ── 3. 민감정보 차단 ──────────────────────────────────────

class TestSensitiveBlocked(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def test_accountNo_in_reason_blocked(self):
        from core.toss_live_pilot_events import record_event
        r = record_event("tlive_sens", "reviewed", "reviewed", reason="accountNo: 12345678-01")
        self.assertFalse(r["ok"])

    def test_bearer_in_message_blocked(self):
        from core.toss_live_pilot_events import record_event
        r = record_event("tlive_sens2", "reviewed", "reviewed", message="Bearer abc123token")
        self.assertFalse(r["ok"])

    def test_app_key_blocked(self):
        from core.toss_live_pilot_events import record_event
        r = record_event("tlive_sens3", "reviewed", "reviewed", reason="APP_KEY=xyz")
        self.assertFalse(r["ok"])


# ── 4. invalid event_type ─────────────────────────────────

class TestInvalidEventType(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def test_invalid_type_returns_ok_false(self):
        from core.toss_live_pilot_events import record_event
        r = record_event("tlive_inv", "BUY_NOW", "ok")
        self.assertFalse(r["ok"])
        self.assertIn("invalid", r.get("reason", ""))


# ── 5. event_summary ──────────────────────────────────────

class TestEventSummary(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def test_summary_has_event_types(self):
        from core.toss_live_pilot_events import record_event, event_summary
        record_event("tlive_s1", "reviewed", "reviewed")
        record_event("tlive_s2", "cancelled", "cancelled")
        summ = event_summary()
        self.assertIn("reviewed", summ["summary"])
        self.assertIn("cancelled", summ["summary"])

    def test_live_order_sent_total_zero_for_non_sent(self):
        from core.toss_live_pilot_events import record_event, event_summary
        record_event("tlive_s3", "reviewed", "reviewed")
        summ = event_summary()
        self.assertEqual(summ["live_order_sent_total"], 0)

    def test_live_order_allowed_false_in_summary(self):
        from core.toss_live_pilot_events import event_summary
        summ = event_summary()
        self.assertFalse(summ["live_order_allowed"])

    def test_live_sent_increments_total(self):
        from core.toss_live_pilot_events import record_event, event_summary
        record_event("tlive_ls", "live_sent", "live_sent", live_order_sent=True)
        summ = event_summary()
        self.assertGreater(summ["live_order_sent_total"], 0)


# ── 6. 각 이벤트 타입 커버 ────────────────────────────────

class TestAllEventTypes(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def _record(self, et: str) -> dict:
        from core.toss_live_pilot_events import record_event
        return record_event(f"tlive_{et[:8]}", et, et)

    def test_reviewed(self):
        self.assertTrue(self._record("reviewed")["ok"])

    def test_cancelled(self):
        self.assertTrue(self._record("cancelled")["ok"])

    def test_confirm_blocked_hermes(self):
        self.assertTrue(self._record("confirm_blocked_hermes")["ok"])

    def test_confirm_blocked_policy(self):
        self.assertTrue(self._record("confirm_blocked_policy")["ok"])

    def test_confirm_blocked_transport(self):
        self.assertTrue(self._record("confirm_blocked_transport")["ok"])

    def test_confirmed_but_not_sent(self):
        self.assertTrue(self._record("confirmed_but_not_sent")["ok"])

    def test_live_send_blocked(self):
        self.assertTrue(self._record("live_send_blocked")["ok"])

    def test_live_sent(self):
        r = self._record("live_sent")
        self.assertTrue(r["ok"])

    def test_live_send_failed(self):
        self.assertTrue(self._record("live_send_failed")["ok"])


# ── 7. 삭제 없음 확인 ─────────────────────────────────────

class TestNoDeleteInSource(unittest.TestCase):
    def test_no_delete_from_in_source(self):
        src = (_ROOT / "core" / "toss_live_pilot_events.py").read_text(encoding="utf-8")
        self.assertNotIn("DELETE FROM", src)
        self.assertNotIn("DROP TABLE", src)

    def test_no_sensitive_in_source(self):
        import re
        src = (_ROOT / "core" / "toss_live_pilot_events.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'\d{8}-\d{2}', src), [])
        self.assertEqual(re.findall(r'Bearer [A-Za-z0-9._\-]{20,}', src), [])


if __name__ == "__main__":
    unittest.main()
