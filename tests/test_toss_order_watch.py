"""tests/test_toss_order_watch.py

Toss 미체결/exit 감시 (read-only 알림) 테스트.

1. 미체결 주문: stale 판정 / 신선한 주문 제외 / 조회 실패 시 빈 목록
2. exit 레벨: 손절/익절 도달 판정, lookback 필터, live_sent만
3. 메시지 조립: 자동 취소/매도 문구 없음
4. run_toss_order_watch: 스로틀 + 1일 1회 dedup
5. 소스에 주문 실행/취소 코드 없음
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

KST = timezone(timedelta(hours=9))

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import core.toss_order_watch as tow


_NOW = datetime(2026, 7, 2, 14, 0, tzinfo=KST)


def _open_order(minutes_ago: int, **kw) -> dict:
    ts = (_NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    base = {
        "broker_order_id": "ord***1",
        "symbol": "091180",
        "side": "buy",
        "quantity": 1.0,
        "ordered_at": ts,
    }
    base.update(kw)
    return base


def _ledger_record(**kw) -> dict:
    base = {
        "pilot_id": "tlive_x_1",
        "status": "live_sent",
        "side": "buy",
        "symbol": "091180.KS",
        "limit_price": 30000,
        "stop_loss": "28000",
        "target_price": "34000",
        "sent_at": (_NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S+09:00"),
    }
    base.update(kw)
    return base


# ── 1. 미체결 주문 ────────────────────────────────────────────────

class TestStaleOpenOrders(unittest.TestCase):
    def test_stale_order_detected(self):
        fn = lambda status: {"ok": True, "orders": [_open_order(90)]}
        alerts = tow.check_stale_open_orders(now=_NOW, list_orders_fn=fn)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["type"], "stale_open_order")
        self.assertEqual(alerts[0]["age_minutes"], 90)

    def test_fresh_order_not_alerted(self):
        fn = lambda status: {"ok": True, "orders": [_open_order(10)]}
        self.assertEqual(tow.check_stale_open_orders(now=_NOW, list_orders_fn=fn), [])

    def test_fetch_failure_returns_empty(self):
        fn = lambda status: {"ok": False, "reason": "account_unavailable", "orders": []}
        self.assertEqual(tow.check_stale_open_orders(now=_NOW, list_orders_fn=fn), [])

    def test_unparseable_ordered_at_skipped(self):
        fn = lambda status: {"ok": True, "orders": [_open_order(90, ordered_at="???")]}
        self.assertEqual(tow.check_stale_open_orders(now=_NOW, list_orders_fn=fn), [])


# ── 2. exit 레벨 ─────────────────────────────────────────────────

class TestExitLevels(unittest.TestCase):
    def test_stop_loss_hit(self):
        alerts = tow.check_exit_levels(
            now=_NOW, records=[_ledger_record()], price_fn=lambda s: 27500.0
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["type"], "stop_loss_hit")

    def test_target_hit(self):
        alerts = tow.check_exit_levels(
            now=_NOW, records=[_ledger_record()], price_fn=lambda s: 35000.0
        )
        self.assertEqual(alerts[0]["type"], "target_hit")

    def test_in_range_no_alert(self):
        alerts = tow.check_exit_levels(
            now=_NOW, records=[_ledger_record()], price_fn=lambda s: 31000.0
        )
        self.assertEqual(alerts, [])

    def test_non_live_sent_skipped(self):
        alerts = tow.check_exit_levels(
            now=_NOW,
            records=[_ledger_record(status="live_send_failed")],
            price_fn=lambda s: 27000.0,
        )
        self.assertEqual(alerts, [])

    def test_old_record_skipped(self):
        old = (_NOW - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S+09:00")
        alerts = tow.check_exit_levels(
            now=_NOW, records=[_ledger_record(sent_at=old)], price_fn=lambda s: 27000.0
        )
        self.assertEqual(alerts, [])

    def test_no_levels_skipped(self):
        alerts = tow.check_exit_levels(
            now=_NOW,
            records=[_ledger_record(stop_loss=None, target_price=None)],
            price_fn=lambda s: 27000.0,
        )
        self.assertEqual(alerts, [])

    def test_price_unavailable_skipped(self):
        alerts = tow.check_exit_levels(
            now=_NOW, records=[_ledger_record()], price_fn=lambda s: 0.0
        )
        self.assertEqual(alerts, [])


# ── 3. 메시지 ────────────────────────────────────────────────────

class TestMessage(unittest.TestCase):
    def test_message_no_auto_action_cta(self):
        stale = tow.check_stale_open_orders(
            now=_NOW, list_orders_fn=lambda s: {"ok": True, "orders": [_open_order(90)]}
        )
        exits = tow.check_exit_levels(
            now=_NOW, records=[_ledger_record()], price_fn=lambda s: 27000.0
        )
        msg = tow.format_watch_message(stale, exits)
        self.assertIn("직접 판단 필요", msg)
        for bad in ("자동매매 시작", "매수하기", "매도하기", "주문 실행"):
            self.assertNotIn(bad, msg)
        self.assertIn("자동 취소 안 함", msg)
        self.assertIn("자동 매도 안 함", msg)

    def test_empty_when_no_alerts(self):
        self.assertEqual(tow.format_watch_message([], []), "")


# ── 4. run_toss_order_watch ──────────────────────────────────────

class TestRunWatch(unittest.TestCase):
    def _run(self, tmp, now=_NOW, **kw):
        state_path = Path(tmp) / "state.json"
        with patch.object(tow, "_state_path", return_value=state_path), \
             patch.object(tow, "check_stale_open_orders", return_value=[]), \
             patch.object(tow, "check_exit_levels", return_value=[
                 {"pilot_id": "p1", "symbol": "091180.KS", "type": "target_hit",
                  "current_price": 35000, "entry_price": 30000,
                  "stop_loss": 28000, "target_price": 34000},
             ]):
            return tow.run_toss_order_watch(now=now, send=False, **kw)

    def test_first_run_alerts(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp)
            self.assertEqual(r["exit_count"], 1)
            self.assertIn("목표가 도달", r["message"])

    def test_throttled_second_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            r2 = self._run(tmp, now=_NOW + timedelta(minutes=5))
            self.assertEqual(r2.get("skipped"), "throttled")

    def test_same_day_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            r2 = self._run(tmp, now=_NOW + timedelta(hours=2))
            self.assertEqual(r2["exit_count"], 0)

    def test_next_day_realerts(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            r2 = self._run(tmp, now=_NOW + timedelta(days=1))
            self.assertEqual(r2["exit_count"], 1)


# ── 5. 소스 안전성 ───────────────────────────────────────────────

class TestSourceSafety(unittest.TestCase):
    def test_no_order_execution_in_source(self):
        src = (_ROOT / "core" / "toss_order_watch.py").read_text(encoding="utf-8")
        src_clean = re.sub(r'"""[\s\S]*?"""', "", src)
        src_clean = re.sub(r"#[^\n]*", "", src_clean)
        self.assertNotIn("requests.post", src_clean)
        self.assertNotIn("submit_order", src_clean)
        self.assertNotIn("cancel_order", src_clean)
        self.assertNotIn("DELETE", src_clean)


if __name__ == "__main__":
    unittest.main()
