"""tests/test_dart_monitor.py

DART 공시 모니터 테스트.

1. fetch_recent_disclosures: 키 없음/HTTP 오류/DART status 처리
2. screen_disclosures: 보유종목 + 리스크 키워드 필터
3. run_dart_monitor: 시간 게이트/스로틀/dedup/발송
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

KST = timezone(timedelta(hours=9))

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import core.dart_monitor as dm  # noqa: E402
from core.source_observations import SourceObservationStore  # noqa: E402

_NOW = datetime(2026, 7, 2, 11, 0, tzinfo=KST)  # 목요일 11:00

_DISCLOSURE = {
    "rcept_no": "20260702000123",
    "rcept_dt": "20260702",
    "stock_code": "123450",
    "corp_name": "테스트기업",
    "report_nm": "주요사항보고서(유상증자결정)",
}


# ── 1. fetch ─────────────────────────────────────────────────────

class TestFetch(unittest.TestCase):
    def test_no_api_key(self):
        with patch.dict("os.environ", {"DART_API_KEY": ""}):
            r = dm.fetch_recent_disclosures(now=_NOW)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no_api_key")

    def _fetch(self, status_code=200, payload=None):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = payload or {}
        with patch.dict("os.environ", {"DART_API_KEY": "k"}), \
             patch("requests.get", return_value=resp):
            return dm.fetch_recent_disclosures(now=_NOW)

    def test_http_error(self):
        r = self._fetch(status_code=500)
        self.assertFalse(r["ok"])

    def test_dart_no_data_ok_empty(self):
        r = self._fetch(payload={"status": "013"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["items"], [])

    def test_dart_error_status(self):
        r = self._fetch(payload={"status": "020"})
        self.assertFalse(r["ok"])

    def test_parses_items(self):
        r = self._fetch(payload={"status": "000", "list": [dict(_DISCLOSURE)]})
        self.assertTrue(r["ok"])
        self.assertEqual(r["items"][0]["stock_code"], "123450")
        self.assertEqual(r["items"][0]["rcept_no"], "20260702000123")


# ── 2. screen ────────────────────────────────────────────────────

class TestScreen(unittest.TestCase):
    def test_risk_keyword_hit(self):
        hits = dm.screen_disclosures([dict(_DISCLOSURE)], {"123450"})
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["keyword"], "유상증자")
        self.assertEqual(hits[0]["severity"], "high")
        self.assertIn("20260702000123", hits[0]["url"])

    def test_not_held_excluded(self):
        hits = dm.screen_disclosures([dict(_DISCLOSURE)], {"999999"})
        self.assertEqual(hits, [])

    def test_benign_report_excluded(self):
        item = dict(_DISCLOSURE, report_nm="분기보고서 (2026.03)")
        hits = dm.screen_disclosures([item], {"123450"})
        self.assertEqual(hits, [])

    def test_empty_stock_code_excluded(self):
        item = dict(_DISCLOSURE, stock_code="")
        hits = dm.screen_disclosures([item], {"123450"})
        self.assertEqual(hits, [])

    def test_medium_severity(self):
        item = dict(_DISCLOSURE, report_nm="전환사채권발행결정")
        hits = dm.screen_disclosures([item], {"123450"})
        self.assertEqual(hits[0]["severity"], "medium")


# ── 3. run_dart_monitor ──────────────────────────────────────────

class TestRun(unittest.TestCase):
    def _run(self, tmp, now=_NOW, force=False, state=None,
             codes=None, fetched=None, send_ok=True):
        state_path = Path(tmp) / "state.json"
        if state is not None:
            state_path.write_text(json.dumps(state), encoding="utf-8")
        fetched = fetched if fetched is not None else {
            "ok": True, "items": [dict(_DISCLOSURE)],
        }
        with patch.dict("os.environ", {"DART_API_KEY": "k"}), \
             patch.object(dm, "_state_path", return_value=state_path), \
             patch.object(dm, "_toss_holding_codes",
                          return_value=codes if codes is not None else {"123450"}), \
             patch.object(dm, "fetch_recent_disclosures", return_value=fetched), \
             patch("core.telegram.send_simple_message", return_value=send_ok) as mock_send:
            result = dm.run_dart_monitor(now=now, force=force)
        return result, mock_send, state_path

    def test_no_api_key_skips(self):
        with patch.dict("os.environ", {"DART_API_KEY": ""}):
            r = dm.run_dart_monitor(now=_NOW)
        self.assertEqual(r["skipped"], "no_api_key")

    def test_weekend_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, _, _ = self._run(tmp, now=datetime(2026, 7, 4, 11, 0, tzinfo=KST))
        self.assertEqual(r["skipped"], "weekend")

    def test_outside_hours_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, _, _ = self._run(tmp, now=datetime(2026, 7, 2, 7, 0, tzinfo=KST))
        self.assertEqual(r["skipped"], "outside_hours")

    def test_throttled(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = {"last_checked_at": (_NOW - timedelta(minutes=10)).isoformat()}
            r, _, _ = self._run(tmp, state=state)
        self.assertEqual(r["skipped"], "throttled")

    def test_new_hit_sends_and_dedups(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, mock_send, state_path = self._run(tmp)
            self.assertTrue(r["sent"])
            self.assertEqual(r["new_hit_count"], 1)
            mock_send.assert_called_once()
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("20260702000123", saved["seen_rcept_nos"])
            # 같은 공시 재실행 → 발송 없음
            r2, mock_send2, _ = self._run(tmp, force=True)
            self.assertEqual(r2["new_hit_count"], 0)
            self.assertFalse(r2["sent"])
            mock_send2.assert_not_called()

    def test_send_failure_no_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, _, state_path = self._run(tmp, send_ok=False)
            self.assertFalse(r["sent"])
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("20260702000123",
                             saved.get("seen_rcept_nos") or [])

    def test_no_holdings_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, _, _ = self._run(tmp, codes=set())
        self.assertEqual(r["skipped"], "no_holdings")

    def test_fetch_failed_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = (_NOW - timedelta(minutes=31)).isoformat()
            r, _, state_path = self._run(
                tmp,
                state={"last_checked_at": previous},
                fetched={"ok": False, "reason": "http_500", "items": []},
            )
            saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(r["skipped"], "http_500")
        self.assertEqual(saved["last_checked_at"], previous)

    def test_later_page_no_data_is_failed_without_alert_or_throttle_advance(self):
        page1 = MagicMock(status_code=200)
        page1.json.return_value = {
            "status": "000",
            "total_page": 2,
            "total_count": 1,
            "list": [dict(_DISCLOSURE)],
        }
        page2 = MagicMock(status_code=200)
        page2.json.return_value = {"status": "013"}
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            previous = (_NOW - timedelta(minutes=31)).isoformat()
            state_path.write_text(
                json.dumps({"last_checked_at": previous}), encoding="utf-8"
            )
            store = SourceObservationStore(Path(tmp) / "observations.db")
            with patch.dict("os.environ", {"DART_API_KEY": "k"}), \
                 patch.object(dm, "_state_path", return_value=state_path), \
                 patch.object(dm, "_toss_holding_codes", return_value={"123450"}), \
                 patch("requests.get", side_effect=[page1, page2]), \
                 patch("core.telegram.send_simple_message") as send:
                result = dm.run_dart_monitor(
                    now=_NOW,
                    force=True,
                    observation_store=store,
                )
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            run = store.latest_collection_run(source="opendart_disclosures")

        self.assertEqual(result["skipped"], "dart_status_013")
        self.assertEqual(result["observation_run_status"], "failed")
        self.assertEqual(saved["last_checked_at"], previous)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run.error_type, "dart_status_013")
        send.assert_not_called()

    def test_no_risk_hits_no_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = dict(_DISCLOSURE, report_nm="분기보고서")
            r, mock_send, _ = self._run(tmp, fetched={"ok": True, "items": [item]})
            self.assertTrue(r["checked"])
            self.assertEqual(r["hit_count"], 0)
            mock_send.assert_not_called()


# ── 4. 메시지 ────────────────────────────────────────────────────

class TestMessage(unittest.TestCase):
    def test_message_format(self):
        hit = dm.screen_disclosures([dict(_DISCLOSURE)], {"123450"})[0]
        msg = dm._format_alert_message([hit])
        self.assertIn("DART 공시 알림", msg)
        self.assertIn("테스트기업", msg)
        self.assertIn("유상증자", msg)
        self.assertIn("자동 매도는 발동하지 않음", msg)


if __name__ == "__main__":
    unittest.main()
