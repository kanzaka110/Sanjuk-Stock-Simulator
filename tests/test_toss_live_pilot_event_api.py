"""tests/test_toss_live_pilot_event_api.py

GET /api/toss/live-pilot-events API 엔드포인트 테스트.

1. 200 응답, 기본 구조
2. limit 파라미터
3. events 필드 타입
4. live_order_allowed 항상 false
5. summary 포함
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _make_mock_events_data(events=None, summary=None):
    return {
        "events": events or [],
        "summary": summary or {},
        "live_order_sent_total": 0,
        "live_order_allowed": False,
        "error": "",
    }


class TestLivePilotEventsApiStructure(unittest.TestCase):
    """GET /api/toss/live-pilot-events 응답 구조 검증."""

    def _get(self, limit=50):
        from fastapi.testclient import TestClient
        from web.app import app

        mock_data = _make_mock_events_data(
            events=[
                {
                    "event_id": "tle_20260624_120000_1234",
                    "pilot_id": "tlp_test_001",
                    "event_type": "reviewed",
                    "status": "reviewed",
                    "symbol": "091180.KS",
                    "symbol_name": "KODEX 자동차",
                    "symbol_label": "KODEX 자동차 (091180.KS)",
                    "side": "buy",
                    "quantity": 10,
                    "limit_price": 13200.0,
                    "estimated_amount_krw": 132000.0,
                    "live_order_sent": False,
                    "live_order_allowed": False,
                    "adapter_status": "disabled",
                    "reason": "",
                    "message": "",
                    "created_at": "2026-06-24T12:00:00+09:00",
                    "delivered_to_hermes": 0,
                }
            ],
            summary={"reviewed": 1},
        )
        with patch("core.dashboard_data.toss_live_pilot_events_data", return_value=mock_data):
            client = TestClient(app)
            url = "/api/toss/live-pilot-events"
            if limit != 50:
                url += f"?limit={limit}"
            return client.get(url)

    def test_status_200(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)

    def test_response_has_events_key(self):
        r = self._get()
        self.assertIn("events", r.json())

    def test_response_has_summary_key(self):
        r = self._get()
        self.assertIn("summary", r.json())

    def test_response_has_live_order_allowed_false(self):
        r = self._get()
        self.assertFalse(r.json()["live_order_allowed"])

    def test_response_has_error_key(self):
        r = self._get()
        self.assertIn("error", r.json())

    def test_events_is_list(self):
        r = self._get()
        self.assertIsInstance(r.json()["events"], list)

    def test_event_fields_present(self):
        r = self._get()
        event = r.json()["events"][0]
        for field in ("event_id", "pilot_id", "event_type", "status",
                      "symbol", "symbol_label", "live_order_sent",
                      "live_order_allowed", "created_at"):
            self.assertIn(field, event)

    def test_event_live_order_allowed_false(self):
        r = self._get()
        event = r.json()["events"][0]
        self.assertFalse(event["live_order_allowed"])

    def test_event_live_order_sent_is_bool(self):
        r = self._get()
        event = r.json()["events"][0]
        self.assertIsInstance(event["live_order_sent"], bool)


class TestLivePilotEventsApiLimit(unittest.TestCase):
    """limit 파라미터 처리 검증."""

    def _get_with_mock(self, limit=50):
        from fastapi.testclient import TestClient
        from web.app import app

        captured = {}

        def mock_events_data(lim):
            captured["limit"] = lim
            return _make_mock_events_data()

        with patch("core.dashboard_data.toss_live_pilot_events_data",
                   side_effect=mock_events_data):
            client = TestClient(app)
            client.get(f"/api/toss/live-pilot-events?limit={limit}")
        return captured

    def test_default_limit_50(self):
        from fastapi.testclient import TestClient
        from web.app import app

        captured = {}

        def mock_events_data(lim=50):
            captured["limit"] = lim
            return _make_mock_events_data()

        with patch("core.dashboard_data.toss_live_pilot_events_data",
                   side_effect=mock_events_data):
            client = TestClient(app)
            client.get("/api/toss/live-pilot-events")
        # default param of 50 is passed
        self.assertIn(captured.get("limit", 50), [50])

    def test_limit_capped_at_200(self):
        captured = self._get_with_mock(limit=999)
        self.assertLessEqual(captured.get("limit", 0), 200)

    def test_limit_10_passed_through(self):
        captured = self._get_with_mock(limit=10)
        self.assertEqual(captured.get("limit"), 10)


class TestLivePilotEventsApiEmpty(unittest.TestCase):
    """빈 이벤트 목록 응답."""

    def test_empty_events_ok(self):
        from fastapi.testclient import TestClient
        from web.app import app

        with patch("core.dashboard_data.toss_live_pilot_events_data",
                   return_value=_make_mock_events_data()):
            client = TestClient(app)
            r = client.get("/api/toss/live-pilot-events")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["events"], [])
        self.assertFalse(body["live_order_allowed"])

    def test_summary_empty_dict_ok(self):
        from fastapi.testclient import TestClient
        from web.app import app

        with patch("core.dashboard_data.toss_live_pilot_events_data",
                   return_value=_make_mock_events_data()):
            client = TestClient(app)
            r = client.get("/api/toss/live-pilot-events")
        self.assertIsInstance(r.json()["summary"], dict)


if __name__ == "__main__":
    unittest.main()
