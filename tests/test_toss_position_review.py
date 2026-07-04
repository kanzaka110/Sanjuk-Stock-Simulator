"""tests/test_toss_position_review.py

보유 포지션 일일 재평가 → 자동 매도 후보 테스트.

1. evaluate_holdings: 손절/익절 기준 판정, exit watch 담당 심볼 제외, fail-safe
2. execute_sell_candidates: 가드 체인 (autonomous/kill switch/env sell/장중/dedup)
3. run_toss_position_review: 1일 1회 dedup + 시간 게이트
"""

from __future__ import annotations

import json
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

import core.toss_position_review as tpr


_NOW = datetime(2026, 7, 2, 11, 0, tzinfo=KST)  # 목요일 11:00 (익일도 평일)

_POLICY_SELL = {
    "autonomous_mode": True,
    "autonomous_kill_switch": False,
    "autonomous_allowed_sides": ["buy", "sell"],
    "max_order_krw": 0,
    "blocked_symbols": [],
}


def _holding(symbol="123450", pl_amount=-9000, purchase=100000, qty=10,
             last_price=9100, **kw) -> dict:
    base = {
        "symbol": symbol,
        "name": "테스트종목",
        "quantity": qty,
        "lastPrice": last_price,
        "currency": "KRW",
        "profitLoss": {"amount": pl_amount, "amountAfterCost": pl_amount},
        "marketValue": {"purchaseAmount": purchase, "amount": purchase + pl_amount},
    }
    base.update(kw)
    return base


# ── 1. evaluate_holdings ─────────────────────────────────────────

class TestEvaluateHoldings(unittest.TestCase):
    def _eval(self, items, exit_covered=None):
        with patch.object(tpr, "_symbols_with_active_exit_levels",
                          return_value=exit_covered or set()):
            return tpr.evaluate_holdings(items)

    def test_stop_loss_full_sell(self):
        # -9% → 손절, 전량
        out = self._eval([_holding(pl_amount=-9000)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "stop_loss")
        self.assertEqual(out[0]["quantity"], 10)
        self.assertEqual(out[0]["symbol"], "123450.KS")
        self.assertEqual(out[0]["pl_pct"], -9.0)

    def test_take_profit_partial_sell(self):
        # +16% → 분할 익절 절반
        out = self._eval([_holding(pl_amount=16000)])
        self.assertEqual(out[0]["action"], "take_profit")
        self.assertEqual(out[0]["quantity"], 5)

    def test_in_range_no_candidate(self):
        # -3% → 대상 아님
        self.assertEqual(self._eval([_holding(pl_amount=-3000)]), [])

    def test_exit_covered_symbol_excluded(self):
        out = self._eval([_holding(symbol="123450", pl_amount=-9000)],
                         exit_covered={"123450.KS"})
        self.assertEqual(out, [])

    def test_missing_purchase_amount_fail_safe(self):
        item = _holding(pl_amount=-9000)
        item["marketValue"] = {}
        self.assertEqual(self._eval([item]), [])

    def test_zero_quantity_or_price_skipped(self):
        self.assertEqual(self._eval([_holding(qty=0)]), [])
        self.assertEqual(self._eval([_holding(last_price=0)]), [])

    def test_us_symbol_kept_as_is(self):
        out = self._eval([_holding(symbol="NVDA", pl_amount=-9000, currency="USD")])
        self.assertEqual(out[0]["symbol"], "NVDA")
        self.assertEqual(out[0]["currency"], "USD")

    def test_env_thresholds(self):
        with patch.dict("os.environ", {"TOSS_REVIEW_STOP_LOSS_PCT": "-3"}):
            out = self._eval([_holding(pl_amount=-4000)])
        self.assertEqual(out[0]["action"], "stop_loss")

    def test_take_profit_min_one_share(self):
        out = self._eval([_holding(pl_amount=16000, qty=1)])
        self.assertEqual(out[0]["quantity"], 1)


# ── 2. execute_sell_candidates ───────────────────────────────────

_CAND = {
    "symbol": "123450.KS", "name": "테스트종목", "pl_pct": -9.0,
    "action": "stop_loss", "quantity": 10, "held_quantity": 10,
    "last_price": 9100, "currency": "KRW",
}


class TestExecuteSell(unittest.TestCase):
    def _exec(self, policy=None, attempted=None, market_open=True,
              process_result=None):
        policy = policy if policy is not None else dict(_POLICY_SELL)
        attempted = attempted if attempted is not None else {}
        process_result = process_result or {
            "symbol": "123450.KS", "stage": "verdict_recorded", "verdict": "PASS",
        }
        with patch.object(tpr, "_market_open_for_symbol", return_value=market_open), \
             patch("core.toss_autonomous_pipeline.process_candidate",
                   return_value=process_result) as mock_pc:
            results = tpr.execute_sell_candidates(
                [dict(_CAND)], policy, _NOW, attempted)
        return results, mock_pc, attempted

    def test_executes_sell(self):
        results, mock_pc, attempted = self._exec()
        self.assertEqual(results[0]["verdict"], "PASS")
        self.assertEqual(results[0]["action"], "stop_loss")
        cand = mock_pc.call_args[0][0]
        self.assertEqual(cand["side"], "sell")
        self.assertEqual(cand["quantity"], 10)
        self.assertEqual(mock_pc.call_args.kwargs.get("reason"),
                         "position_review_sell")
        self.assertIn("123450.KS", attempted)

    def test_autonomous_off_skips(self):
        policy = dict(_POLICY_SELL, autonomous_mode=False)
        results, mock_pc, _ = self._exec(policy=policy)
        self.assertEqual(results[0]["reason"], "autonomous_mode_disabled")
        mock_pc.assert_not_called()

    def test_kill_switch_skips(self):
        policy = dict(_POLICY_SELL, autonomous_kill_switch=True)
        results, _, _ = self._exec(policy=policy)
        self.assertEqual(results[0]["reason"], "kill_switch_active")

    def test_sell_env_off_skips(self):
        policy = dict(_POLICY_SELL, autonomous_allowed_sides=["buy"])
        results, mock_pc, _ = self._exec(policy=policy)
        self.assertEqual(results[0]["reason"], "sell_not_allowed_by_env")
        mock_pc.assert_not_called()

    def test_market_closed_skips(self):
        results, _, _ = self._exec(market_open=False)
        self.assertEqual(results[0]["reason"], "market_closed")

    def test_already_attempted_dedup(self):
        results, mock_pc, _ = self._exec(attempted={"123450.KS": {"at": "10:00"}})
        self.assertEqual(results[0]["reason"], "already_attempted_today")
        mock_pc.assert_not_called()


# ── 3. run_toss_position_review ──────────────────────────────────

class TestRunReview(unittest.TestCase):
    def _run(self, tmp, now=_NOW, force=False, candidates=None, state=None):
        candidates = candidates if candidates is not None else [dict(_CAND)]
        state_path = Path(tmp) / "state.json"
        if state is not None:
            state_path.write_text(json.dumps(state), encoding="utf-8")
        with patch.object(tpr, "_state_path", return_value=state_path), \
             patch.object(tpr, "evaluate_holdings", return_value=candidates), \
             patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=dict(_POLICY_SELL)), \
             patch.object(tpr, "execute_sell_candidates",
                          return_value=[{"symbol": "123450.KS",
                                         "stage": "verdict_recorded",
                                         "verdict": "PASS"}]), \
             patch("core.telegram.send_simple_message", return_value=True):
            return tpr.run_toss_position_review(now=now, force=force)

    def test_runs_and_sends(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp)
            self.assertTrue(r["reviewed"])
            self.assertEqual(r["candidate_count"], 1)
            self.assertTrue(r["sent"])

    def test_dedup_same_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            r2 = self._run(tmp)
            self.assertEqual(r2.get("skipped"), "already_reviewed_today")

    def test_next_day_runs_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            r2 = self._run(tmp, now=_NOW + timedelta(days=1))
            self.assertTrue(r2.get("reviewed"))

    def test_before_hour_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, now=datetime(2026, 7, 2, 9, 30, tzinfo=KST))
            self.assertEqual(r.get("skipped"), "before_review_hour")

    def test_weekend_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, now=datetime(2026, 7, 4, 11, 0, tzinfo=KST))
            self.assertEqual(r.get("skipped"), "weekend")

    def test_no_candidates_no_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, candidates=[])
            self.assertTrue(r["reviewed"])
            self.assertEqual(r["candidate_count"], 0)
            self.assertFalse(r["sent"])


# ── 4. 메시지 ────────────────────────────────────────────────────

class TestMessage(unittest.TestCase):
    def test_message_pass(self):
        msg = tpr._format_review_message(
            [dict(_CAND)],
            [{"symbol": "123450.KS", "stage": "verdict_recorded", "verdict": "PASS"}],
        )
        self.assertIn("손절 기준 도달", msg)
        self.assertIn("자동 매도 발동", msg)
        self.assertIn("-9.0%", msg)

    def test_message_skipped(self):
        msg = tpr._format_review_message(
            [dict(_CAND)],
            [{"symbol": "123450.KS", "stage": "skipped",
              "reason": "sell_not_allowed_by_env"}],
        )
        self.assertIn("자동 매도 스킵: sell_not_allowed_by_env", msg)


if __name__ == "__main__":
    unittest.main()
