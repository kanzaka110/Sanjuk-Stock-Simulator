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

    def test_income_target_hit_is_marked_for_full_exit(self):
        alerts = tow.check_exit_levels(
            now=_NOW,
            records=[_ledger_record(reason="auto_pipeline")],
            price_fn=lambda s: 35000.0,
        )
        self.assertIs(alerts[0]["income_managed"], True)

    def test_income_uses_fixed_one_point_five_profit_and_two_point_five_stop(self):
        record = _ledger_record(reason="auto_pipeline")

        profit = tow.check_exit_levels(
            now=_NOW, records=[record], price_fn=lambda s: 30_450.0,
        )
        loss = tow.check_exit_levels(
            now=_NOW, records=[record], price_fn=lambda s: 29_250.0,
        )
        manual = tow.check_exit_levels(
            now=_NOW, records=[_ledger_record()], price_fn=lambda s: 30_450.0,
        )

        self.assertEqual(profit[0]["type"], "target_hit")
        self.assertEqual(profit[0]["target_price"], 30_450.0)
        self.assertEqual(profit[0]["stop_loss"], 29_250.0)
        self.assertEqual(loss[0]["type"], "stop_loss_hit")
        self.assertEqual(manual, [])

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
        self.assertIn("매도 여부 직접 판단 필요", msg)

    def test_message_promoted_sell_shown(self):
        exits = tow.check_exit_levels(
            now=_NOW, records=[_ledger_record()], price_fn=lambda s: 27000.0
        )
        promos = {"091180.KS:stop_loss_hit": {
            "ok": True, "live_order_sent": True,
            "symbol": "091180.KS", "stage": "verdict_recorded",
            "verdict": "PASS", "sell_quantity": 3, "exit_type": "stop_loss_hit",
        }}
        msg = tow.format_watch_message([], exits, promos)
        self.assertIn("자동 매도 전송 완료", msg)
        self.assertIn("전량 손절 3주", msg)
        self.assertNotIn("매도 여부 직접 판단 필요", msg)

    def test_message_skipped_promotion_asks_manual(self):
        exits = tow.check_exit_levels(
            now=_NOW, records=[_ledger_record()], price_fn=lambda s: 35000.0
        )
        promos = {"091180.KS:target_hit": {
            "symbol": "091180.KS", "stage": "skipped",
            "reason": "sell_not_allowed_by_env",
        }}
        msg = tow.format_watch_message([], exits, promos)
        self.assertIn("자동 매도 스킵: sell_not_allowed_by_env", msg)
        self.assertIn("매도 여부 직접 판단 필요", msg)

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
                  "stop_loss": 28000, "target_price": 34000, "quantity": 5},
             ]), \
             patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value={"autonomous_mode": False}):
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

    def test_market_closed_exit_does_not_consume_daily_dedup(self):
        alert = {
            "pilot_id": "p-us", "symbol": "LRCX", "type": "stop_loss_hit",
            "current_price": 302.0, "entry_price": 328.62,
            "stop_loss": 314.818, "target_price": 361.48, "quantity": 1,
        }
        policy = {
            "autonomous_mode": True,
            "autonomous_kill_switch": False,
            "autonomous_allowed_sides": ["buy", "sell"],
        }
        process_result = {
            "ok": True, "live_order_sent": True,
            "symbol": "LRCX", "stage": "verdict_recorded", "verdict": "PASS",
            "pilot_id": "tlive_sell_lrcx",
        }
        closed_at = datetime(2026, 7, 2, 22, 29, tzinfo=KST)
        opened_at = closed_at + timedelta(minutes=1)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            with patch.object(tow, "_state_path", return_value=state_path), \
                 patch.object(tow, "check_stale_open_orders", return_value=[]), \
                 patch.object(tow, "check_exit_levels", return_value=[alert]), \
                 patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                       return_value=policy):
                closed = tow.run_toss_order_watch(now=closed_at, send=False)
            self.assertEqual(closed["exit_count"], 0)
            self.assertEqual(closed["deferred_exit_count"], 1)

            with patch.object(tow, "_state_path", return_value=state_path), \
                 patch.object(tow, "check_stale_open_orders", return_value=[]), \
                 patch.object(tow, "check_exit_levels", return_value=[alert]), \
                 patch.object(tow, "_held_quantity", return_value=1.0), \
                 patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                       return_value=policy), \
                 patch("core.toss_autonomous_pipeline.process_candidate",
                       return_value=process_result) as process:
                opened = tow.run_toss_order_watch(now=opened_at, send=False)
            self.assertEqual(opened["exit_count"], 1)
            self.assertTrue(
                opened["promotions"]["LRCX:stop_loss_hit"]["live_order_sent"],
            )
            process.assert_called_once()

    def test_transient_holding_lookup_failure_retries_same_day(self):
        alert = {
            "pilot_id": "p-us", "symbol": "LRCX", "type": "stop_loss_hit",
            "current_price": 302.0, "entry_price": 328.62,
            "stop_loss": 314.818, "target_price": 361.48, "quantity": 1,
        }
        policy = {
            "autonomous_mode": True,
            "autonomous_kill_switch": False,
            "autonomous_allowed_sides": ["buy", "sell"],
        }
        now = datetime(2026, 7, 2, 22, 30, tzinfo=KST)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            common = (
                patch.object(tow, "_state_path", return_value=state_path),
                patch.object(tow, "check_stale_open_orders", return_value=[]),
                patch.object(tow, "check_exit_levels", return_value=[alert]),
                patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                      return_value=policy),
            )
            with common[0], common[1], common[2], common[3], \
                 patch.object(tow, "_held_quantity", return_value=0.0), \
                 patch("core.toss_autonomous_pipeline.process_candidate") as process:
                failed = tow.run_toss_order_watch(now=now, send=False)
            self.assertFalse(
                failed["promotions"]["LRCX:stop_loss_hit"].get("live_order_sent", False),
            )
            process.assert_not_called()

            sent_result = {
                "ok": True, "live_order_sent": True, "verdict": "PASS",
                "stage": "verdict_recorded", "pilot_id": "tlive_sell_retry",
            }
            with patch.object(tow, "_state_path", return_value=state_path), \
                 patch.object(tow, "check_stale_open_orders", return_value=[]), \
                 patch.object(tow, "check_exit_levels", return_value=[alert]), \
                 patch.object(tow, "_held_quantity", return_value=1.0), \
                 patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                       return_value=policy), \
                 patch("core.toss_autonomous_pipeline.process_candidate",
                       return_value=sent_result) as process:
                retried = tow.run_toss_order_watch(
                    now=now + timedelta(minutes=31), send=False,
                )
            self.assertEqual(retried["exit_count"], 1)
            self.assertTrue(
                retried["promotions"]["LRCX:stop_loss_hit"]["live_order_sent"],
            )
            process.assert_called_once()

    def test_verdict_pass_without_live_send_does_not_consume_dedup(self):
        alert = {
            "pilot_id": "p-us", "symbol": "LRCX", "type": "stop_loss_hit",
            "current_price": 302.0, "entry_price": 328.62,
            "stop_loss": 314.818, "target_price": 361.48, "quantity": 1,
        }
        policy = {
            "autonomous_mode": True,
            "autonomous_kill_switch": False,
            "autonomous_allowed_sides": ["buy", "sell"],
        }
        now = datetime(2026, 7, 2, 22, 30, tzinfo=KST)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            results = [
                {"ok": True, "live_order_sent": False, "verdict": "PASS",
                 "stage": "verdict_recorded", "pilot_id": "tlive_not_sent"},
                {"ok": True, "live_order_sent": True, "verdict": "PASS",
                 "stage": "verdict_recorded", "pilot_id": "tlive_sent"},
            ]
            with patch.object(tow, "_state_path", return_value=state_path), \
                 patch.object(tow, "check_stale_open_orders", return_value=[]), \
                 patch.object(tow, "check_exit_levels", return_value=[alert]), \
                 patch.object(tow, "_held_quantity", return_value=1.0), \
                 patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                       return_value=policy), \
                 patch("core.toss_autonomous_pipeline.process_candidate",
                       side_effect=results) as process:
                first = tow.run_toss_order_watch(now=now, send=False)
                second = tow.run_toss_order_watch(
                    now=now + timedelta(minutes=31), send=False,
                )
            self.assertFalse(
                first["promotions"]["LRCX:stop_loss_hit"]["live_order_sent"],
            )
            self.assertTrue(
                second["promotions"]["LRCX:stop_loss_hit"]["live_order_sent"],
            )
            self.assertEqual(process.call_count, 2)

    def test_us_completion_dedup_survives_kst_midnight(self):
        alert = {
            "pilot_id": "p-us", "symbol": "LRCX", "type": "stop_loss_hit",
            "current_price": 302.0, "entry_price": 328.62,
            "stop_loss": 314.818, "target_price": 361.48, "quantity": 1,
        }
        policy = {
            "autonomous_mode": True,
            "autonomous_kill_switch": False,
            "autonomous_allowed_sides": ["buy", "sell"],
        }
        sent_result = {
            "ok": True, "live_order_sent": True, "verdict": "PASS",
            "stage": "verdict_recorded", "pilot_id": "tlive_sent",
        }
        before_midnight = datetime(2026, 7, 2, 23, 50, tzinfo=KST)
        after_midnight = datetime(2026, 7, 3, 0, 21, tzinfo=KST)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(tow, "_state_path", return_value=Path(tmp) / "state.json"), \
             patch.object(tow, "check_stale_open_orders", return_value=[]), \
             patch.object(tow, "check_exit_levels", return_value=[alert]), \
             patch.object(tow, "_held_quantity", return_value=1.0), \
             patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=policy), \
             patch("core.toss_autonomous_pipeline.process_candidate",
                   return_value=sent_result) as process:
            first = tow.run_toss_order_watch(now=before_midnight, send=False)
            second = tow.run_toss_order_watch(now=after_midnight, send=False)
        self.assertTrue(first["promotions"]["LRCX:stop_loss_hit"]["live_order_sent"])
        self.assertEqual(second["exit_count"], 0)
        self.assertEqual(process.call_count, 1)

    def test_us_next_market_day_can_retry_within_24_hours(self):
        alert = {
            "pilot_id": "p-us", "symbol": "LRCX", "type": "stop_loss_hit",
            "current_price": 302.0, "entry_price": 328.62,
            "stop_loss": 314.818, "target_price": 361.48, "quantity": 1,
        }
        policy = {
            "autonomous_mode": True,
            "autonomous_kill_switch": False,
            "autonomous_allowed_sides": ["buy", "sell"],
        }
        sent_result = {
            "ok": True, "live_order_sent": True, "verdict": "PASS",
            "stage": "verdict_recorded", "pilot_id": "tlive_sent",
        }
        first_day = datetime(2026, 7, 7, 23, 50, tzinfo=KST)
        next_day = datetime(2026, 7, 8, 22, 30, tzinfo=KST)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(tow, "_state_path", return_value=Path(tmp) / "state.json"), \
             patch.object(tow, "check_stale_open_orders", return_value=[]), \
             patch.object(tow, "check_exit_levels", return_value=[alert]), \
             patch.object(tow, "_held_quantity", return_value=1.0), \
             patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=policy), \
             patch("core.toss_autonomous_pipeline.process_candidate",
                   return_value=sent_result) as process:
            first = tow.run_toss_order_watch(now=first_day, send=False)
            second = tow.run_toss_order_watch(now=next_day, send=False)
        self.assertEqual(first["exit_count"], 1)
        self.assertEqual(second["exit_count"], 1)
        self.assertEqual(process.call_count, 2)

    def test_digit_only_kr_symbol_uses_kst_market_day(self):
        morning = datetime(2026, 7, 2, 9, 30, tzinfo=KST)
        afternoon = datetime(2026, 7, 2, 14, 0, tzinfo=KST)
        self.assertEqual(
            tow._market_day("005930", morning),
            tow._market_day("005930", afternoon),
        )

    def test_nested_state_corruption_blocks_submit(self):
        now = datetime(2026, 7, 2, 22, 30, tzinfo=KST)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text('{"alerted":"truthy","reservations":{}}', encoding="utf-8")
            with patch.object(tow, "_state_path", return_value=state_path), \
                 patch("core.toss_autonomous_pipeline.process_candidate") as process:
                result = tow.run_toss_order_watch(now=now, send=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "state_load_failed")
        process.assert_not_called()

    def test_abandoned_reservation_retries_after_lease(self):
        alert = {
            "pilot_id": "p-us", "symbol": "LRCX", "type": "stop_loss_hit",
            "current_price": 302.0, "entry_price": 328.62,
            "stop_loss": 314.818, "target_price": 361.48, "quantity": 1,
        }
        policy = {
            "autonomous_mode": True,
            "autonomous_kill_switch": False,
            "autonomous_allowed_sides": ["buy", "sell"],
        }
        now = datetime(2026, 7, 2, 23, 1, tzinfo=KST)
        reserved_at = now - timedelta(minutes=31)
        state = {
            "last_run": reserved_at.isoformat(),
            "last_market_open": True,
            "alerted": {},
            "reservations": {
                "exit_reserved:LRCX:stop_loss_hit": reserved_at.isoformat(),
            },
        }
        sent_result = {
            "ok": True, "live_order_sent": True, "verdict": "PASS",
            "stage": "verdict_recorded", "pilot_id": "tlive_retry",
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            with patch.object(tow, "_state_path", return_value=state_path), \
                 patch.object(tow, "check_stale_open_orders", return_value=[]), \
                 patch.object(tow, "check_exit_levels", return_value=[alert]), \
                 patch.object(tow, "_held_quantity", return_value=1.0), \
                 patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                       return_value=policy), \
                 patch("core.toss_autonomous_pipeline.process_candidate",
                       return_value=sent_result) as process:
                result = tow.run_toss_order_watch(now=now, send=False)
        self.assertEqual(result["exit_count"], 1)
        process.assert_called_once()

    def test_corrupt_state_blocks_submit(self):
        alert = {
            "pilot_id": "p-us", "symbol": "LRCX", "type": "stop_loss_hit",
            "current_price": 302.0, "entry_price": 328.62,
            "stop_loss": 314.818, "target_price": 361.48, "quantity": 1,
        }
        policy = {
            "autonomous_mode": True,
            "autonomous_kill_switch": False,
            "autonomous_allowed_sides": ["buy", "sell"],
        }
        now = datetime(2026, 7, 2, 22, 30, tzinfo=KST)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text("{broken", encoding="utf-8")
            with patch.object(tow, "_state_path", return_value=state_path), \
                 patch.object(tow, "check_stale_open_orders", return_value=[]), \
                 patch.object(tow, "check_exit_levels", return_value=[alert]), \
                 patch.object(tow, "_held_quantity", return_value=1.0), \
                 patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                       return_value=policy), \
                 patch("core.toss_autonomous_pipeline.process_candidate") as process:
                result = tow.run_toss_order_watch(now=now, send=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "state_load_failed")
        process.assert_not_called()

    def test_finalize_save_failure_retries_after_lease(self):
        import copy

        alert = {
            "pilot_id": "p-us", "symbol": "LRCX", "type": "stop_loss_hit",
            "current_price": 302.0, "entry_price": 328.62,
            "stop_loss": 314.818, "target_price": 361.48, "quantity": 1,
        }
        policy = {
            "autonomous_mode": True,
            "autonomous_kill_switch": False,
            "autonomous_allowed_sides": ["buy", "sell"],
        }
        sent_result = {
            "ok": True, "live_order_sent": True, "verdict": "PASS",
            "stage": "verdict_recorded", "pilot_id": "tlive_sent",
        }
        now = datetime(2026, 7, 2, 22, 30, tzinfo=KST)
        durable = {}
        save_calls = 0

        def fake_save(state):
            nonlocal save_calls, durable
            save_calls += 1
            if save_calls == 1:
                durable = copy.deepcopy(state)
                return True
            if save_calls == 2:
                return False
            durable = copy.deepcopy(state)
            return True

        def fake_load():
            return copy.deepcopy(durable)

        with patch.object(tow, "_load_state", side_effect=fake_load), \
             patch.object(tow, "_save_state", side_effect=fake_save), \
             patch.object(tow, "check_stale_open_orders", return_value=[]), \
             patch.object(tow, "check_exit_levels", return_value=[alert]), \
             patch.object(tow, "_held_quantity", return_value=1.0), \
             patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=policy), \
             patch("core.toss_autonomous_pipeline.process_candidate",
                   return_value=sent_result) as process:
            first = tow.run_toss_order_watch(now=now, send=False)
            second = tow.run_toss_order_watch(
                now=now + timedelta(minutes=31), send=False,
            )
        self.assertFalse(first["ok"])
        self.assertEqual(first["reason"], "state_finalize_failed")
        self.assertEqual(second["exit_count"], 1)
        self.assertEqual(process.call_count, 2)

    def test_state_reservation_save_failure_blocks_submit(self):
        alert = {
            "pilot_id": "p-us", "symbol": "LRCX", "type": "stop_loss_hit",
            "current_price": 302.0, "entry_price": 328.62,
            "stop_loss": 314.818, "target_price": 361.48, "quantity": 1,
        }
        policy = {
            "autonomous_mode": True,
            "autonomous_kill_switch": False,
            "autonomous_allowed_sides": ["buy", "sell"],
        }
        now = datetime(2026, 7, 2, 22, 30, tzinfo=KST)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(tow, "_state_path", return_value=Path(tmp) / "state.json"), \
             patch.object(tow, "check_stale_open_orders", return_value=[]), \
             patch.object(tow, "check_exit_levels", return_value=[alert]), \
             patch.object(tow, "_held_quantity", return_value=1.0), \
             patch.object(tow, "_save_state", return_value=False), \
             patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=policy), \
             patch("core.toss_autonomous_pipeline.process_candidate") as process:
            result = tow.run_toss_order_watch(now=now, send=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "state_reservation_failed")
        process.assert_not_called()


# ── 4.5 자동 매도 승격 ───────────────────────────────────────────

_POLICY_AUTO_SELL = {
    "autonomous_mode": True,
    "autonomous_kill_switch": False,
    "autonomous_allowed_sides": ["buy", "sell"],
    "max_order_krw": 0,
    "blocked_symbols": [],
}

_ALERT_STOP = {
    "pilot_id": "p1", "symbol": "091180.KS", "type": "stop_loss_hit",
    "current_price": 27000, "entry_price": 30000,
    "stop_loss": 28000, "target_price": 34000, "quantity": 10,
}


class TestPromoteExitToSell(unittest.TestCase):
    def test_sell_quantity_stop_full_target_half(self):
        self.assertEqual(tow.compute_exit_sell_quantity(_ALERT_STOP, held_qty=10), 10)
        target = dict(_ALERT_STOP, type="target_hit")
        self.assertEqual(tow.compute_exit_sell_quantity(target, held_qty=10), 5)
        income_target = dict(target, income_managed=True)
        self.assertEqual(tow.compute_exit_sell_quantity(income_target, held_qty=10), 10)
        # 실보유가 더 적으면 실보유 기준
        self.assertEqual(tow.compute_exit_sell_quantity(_ALERT_STOP, held_qty=4), 4)
        # 미보유 → 0
        self.assertEqual(tow.compute_exit_sell_quantity(_ALERT_STOP, held_qty=0), 0)
        # 목표 분할은 최소 1주
        target1 = dict(_ALERT_STOP, type="target_hit", quantity=1)
        self.assertEqual(tow.compute_exit_sell_quantity(target1, held_qty=1), 1)

    def _promote(self, alert=None, policy=None, held=10.0, market_open=True,
                 process_result=None):
        alert = alert or dict(_ALERT_STOP)
        policy = policy if policy is not None else dict(_POLICY_AUTO_SELL)
        process_result = process_result or {
            "symbol": alert["symbol"], "stage": "verdict_recorded", "verdict": "PASS",
            "pilot_id": "tlive_sell_1",
        }
        with patch.object(tow, "_held_quantity", return_value=held), \
             patch.object(tow, "_market_open_for_symbol", return_value=market_open), \
             patch("core.toss_autonomous_pipeline.process_candidate",
                   return_value=process_result) as mock_pc:
            r = tow.promote_exit_to_sell(alert, policy, now=_NOW)
        return r, mock_pc

    def test_promotes_stop_loss_full_sell(self):
        r, mock_pc = self._promote()
        self.assertEqual(r["verdict"], "PASS")
        self.assertEqual(r["sell_quantity"], 10)
        cand = mock_pc.call_args[0][0]
        self.assertEqual(cand["side"], "sell")
        self.assertEqual(cand["quantity"], 10)
        self.assertEqual(cand["limit_price"], 27000)
        self.assertIn(".full_exit", cand["decision_ref"])
        self.assertEqual(mock_pc.call_args.kwargs.get("reason"), "auto_exit_sell")

    def test_target_hit_partial_sell(self):
        alert = dict(_ALERT_STOP, type="target_hit", current_price=35000)
        r, mock_pc = self._promote(alert=alert)
        self.assertEqual(r["sell_quantity"], 5)
        self.assertEqual(mock_pc.call_args[0][0]["quantity"], 5)
        self.assertIn(".partial_exit", mock_pc.call_args[0][0]["decision_ref"])

    def test_income_target_hit_full_sell(self):
        alert = dict(
            _ALERT_STOP,
            type="target_hit",
            current_price=35000,
            income_managed=True,
        )
        r, mock_pc = self._promote(alert=alert)
        self.assertEqual(r["sell_quantity"], 10)
        self.assertEqual(mock_pc.call_args[0][0]["quantity"], 10)
        self.assertIn(".full_exit", mock_pc.call_args[0][0]["decision_ref"])

    def test_one_share_target_is_full_exit_intent(self):
        alert = dict(
            _ALERT_STOP, type="target_hit", current_price=35000, quantity=1,
        )
        _, mock_pc = self._promote(alert=alert, held=1.0)
        self.assertIn(".full_exit", mock_pc.call_args[0][0]["decision_ref"])

    def test_autonomous_off_skips(self):
        policy = dict(_POLICY_AUTO_SELL, autonomous_mode=False)
        r, mock_pc = self._promote(policy=policy)
        self.assertEqual(r["stage"], "skipped")
        self.assertEqual(r["reason"], "autonomous_mode_disabled")
        mock_pc.assert_not_called()

    def test_kill_switch_skips(self):
        policy = dict(_POLICY_AUTO_SELL, autonomous_kill_switch=True)
        r, _ = self._promote(policy=policy)
        self.assertEqual(r["reason"], "kill_switch_active")

    def test_sell_not_allowed_env_skips(self):
        policy = dict(_POLICY_AUTO_SELL, autonomous_allowed_sides=["buy"])
        r, mock_pc = self._promote(policy=policy)
        self.assertEqual(r["reason"], "sell_not_allowed_by_env")
        mock_pc.assert_not_called()

    def test_market_closed_skips(self):
        r, _ = self._promote(market_open=False)
        self.assertEqual(r["reason"], "market_closed")

    def test_no_holding_skips(self):
        r, mock_pc = self._promote(held=0.0)
        self.assertIn("no_confirmed_holding", r["reason"])
        mock_pc.assert_not_called()


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
