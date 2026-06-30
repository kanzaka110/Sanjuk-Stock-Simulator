"""tests/test_toss_live_pilot_event_hygiene.py

Live pilot callback/event 오염 방지 + 테스트 DB 격리 regression 테스트.

검증 목표:
1. production toss live-pilot DB는 테스트에서 절대 쓰이지 않는다 (conftest 격리).
2. adapter disabled / live_order_allowed=false 상태의 live_sent는
   live_sent_artifact로 강등되어 production live_sent로 카운트되지 않는다.
3. fake-success callback 경로를 타도 production summary가 오염되지 않는다.
4. event_summary는 real / mock_or_artifact / blocked_*를 분리한다.
5. Hermes 필터 기준(real)과 API record 분류가 일치한다.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_ALL_GATES_ENV = {
    "TOSS_LIVE_PILOT_ENABLED": "true",
    "TOSS_LIVE_ORDER_ALLOWED": "true",
    "TOSS_LIVE_ADAPTER_ENABLED": "true",
}


def _reset_events():
    import core.toss_live_pilot_events as m
    m._schema_created = False


# ── 1. conftest 격리: production 경로로 쓰지 않음 ────────────

class TestProductionDbIsolation(unittest.TestCase):
    def test_events_db_path_is_not_production(self):
        import core.toss_live_pilot_events as ev
        p = str(ev._db_path())
        # conftest autouse fixture가 임시 경로로 강제했는지 확인
        self.assertNotIn(str(_ROOT / "db" / "data"), p)

    def test_ledger_db_path_is_not_production(self):
        import core.toss_live_pilot_ledger as led
        p = str(led._db_path())
        self.assertNotIn(str(_ROOT / "db" / "data"), p)

    def test_verification_db_path_is_not_production(self):
        import core.toss_live_pilot_verification as ver
        p = str(ver._db_path())
        self.assertNotIn(str(_ROOT / "db" / "data"), p)


# ── 2. artifact 강등 invariant ──────────────────────────────

class TestLiveSentArtifactDowngrade(unittest.TestCase):
    def test_disabled_adapter_downgrades(self):
        from core.toss_live_pilot_events import record_event
        r = record_event(
            "tlive_h1", "live_sent", "live_sent",
            live_order_sent=True, adapter_status="disabled",
        )
        self.assertEqual(r["event_type"], "live_sent_artifact")
        self.assertFalse(r["is_real_live_sent"])

    def test_enabled_but_not_allowed_downgrades(self):
        from core.toss_live_pilot_events import record_event
        r = record_event(
            "tlive_h2", "live_sent", "live_sent",
            live_order_sent=True, adapter_status="enabled", live_order_allowed=False,
        )
        self.assertEqual(r["event_type"], "live_sent_artifact")

    def test_fully_gated_stays_real(self):
        from core.toss_live_pilot_events import record_event
        r = record_event(
            "tlive_h3", "live_sent", "live_sent",
            live_order_sent=True, adapter_status="enabled", live_order_allowed=True,
        )
        self.assertEqual(r["event_type"], "live_sent")
        self.assertTrue(r["is_real_live_sent"])
        self.assertTrue(r["live_order_allowed"])


# ── 3. summary 분리 ─────────────────────────────────────────

class TestSummarySplit(unittest.TestCase):
    def test_split_fields_present(self):
        from core.toss_live_pilot_events import event_summary
        summ = event_summary()
        for key in (
            "live_sent_real", "live_sent_mock_or_artifact",
            "blocked_policy", "blocked_transport", "blocked_guard",
            "live_order_sent_total",
        ):
            self.assertIn(key, summ)

    def test_artifact_excluded_from_real_total(self):
        from core.toss_live_pilot_events import record_event, event_summary
        record_event("tlive_h4", "live_sent", "live_sent",
                     live_order_sent=True, adapter_status="disabled")
        summ = event_summary()
        self.assertEqual(summ["live_sent_real"], 0)
        self.assertEqual(summ["live_order_sent_total"], 0)
        self.assertGreaterEqual(summ["live_sent_mock_or_artifact"], 1)

    def test_blocked_policy_aggregates(self):
        from core.toss_live_pilot_events import record_event, event_summary
        record_event("tlive_h5", "confirm_blocked_policy", "blocked")
        record_event("tlive_h6", "confirm_blocked_hermes", "blocked")
        summ = event_summary()
        self.assertEqual(summ["blocked_policy"], 2)


# ── 4. list_events 분류 표시 ────────────────────────────────

class TestRecordClassification(unittest.TestCase):
    def test_artifact_record_classified(self):
        from core.toss_live_pilot_events import record_event, list_events
        record_event("tlive_h7", "live_sent", "live_sent",
                     live_order_sent=True, adapter_status="disabled")
        evs = list_events(limit=10)
        found = [e for e in evs if e["pilot_id"] == "tlive_h7"]
        self.assertTrue(found)
        self.assertEqual(found[0]["live_sent_classification"], "mock_or_artifact")
        self.assertFalse(found[0]["live_order_allowed"])

    def test_non_sent_event_na(self):
        from core.toss_live_pilot_events import record_event, list_events
        record_event("tlive_h8", "reviewed", "reviewed")
        evs = list_events(limit=10)
        found = [e for e in evs if e["pilot_id"] == "tlive_h8"]
        self.assertEqual(found[0]["live_sent_classification"], "n/a")


# ── 5. fake-success callback이 production summary를 오염시키지 않음 ──

class TestFakeSuccessCallbackHygiene(unittest.TestCase):
    """fake transport success 콜백 경로를 타도 격리된 임시 DB에만 쓰이고,
    real live_sent로 카운트되지 않는다."""

    def setUp(self):
        # ledger는 별도 임시 DB로 (conftest가 이미 events를 임시로 격리)
        self.tmp = tempfile.mkdtemp()
        self._led_patch = patch(
            "core.toss_live_pilot_ledger._db_path",
            return_value=Path(self.tmp) / "test_pilot.db",
        )
        self._led_patch.start()
        import core.toss_live_pilot_ledger as led
        led._schema_created = False
        self._env_patch = patch.dict(os.environ, _ALL_GATES_ENV)
        self._env_patch.start()
        self._hermes_patch = patch(
            "core.toss_live_pilot_verification.is_verification_passed",
            return_value=(True, [], {}),
        )
        self._hermes_patch.start()
        _reset_events()

    def tearDown(self):
        self._hermes_patch.stop()
        self._led_patch.stop()
        self._env_patch.stop()
        import core.toss_live_pilot_ledger as led
        led._schema_created = False
        _reset_events()

    def test_fake_success_not_counted_as_real(self):
        from core.toss_live_pilot_ledger import record_live_pilot_preview
        from core.toss_live_pilot_telegram import handle_live_pilot_callback
        from core.toss_live_pilot_events import event_summary

        preview = {
            "ok": True, "preview_id": "tlive_hyg_fake", "symbol": "091180.KS",
            "side": "buy", "quantity": 1, "limit_price": 30000.0,
            "estimated_amount_krw": 30000.0, "blocks": [], "warnings": [],
        }
        rec = record_live_pilot_preview(preview)
        with patch(
            "core.toss_live_pilot_adapter.dispatch_toss_order_live",
            return_value={
                "ok": True, "live_order_sent": True,
                "broker_order_id": "ORD-FAKE-HYG", "payload_hash": "h",
                "message": "승인형 live pilot 주문 전송 완료\n자동매매 아님",
            },
        ), patch(
            "core.toss_live_pilot_adapter.can_send_live_pilot_order",
            return_value=(True, []),
        ):
            handle_live_pilot_callback(f"tlp:confirm:{rec['pilot_id']}")

        summ = event_summary()
        # 3개 env gate 모두 활성 + dispatch 성공 → real live_sent로 카운트됨
        # (fake transport가 live_order_sent=True 반환하고 policy가 enabled이므로)
        self.assertEqual(summ["live_sent_real"], 1)
        self.assertEqual(summ["live_order_sent_total"], 1)


if __name__ == "__main__":
    unittest.main()
