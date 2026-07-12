"""tests/test_toss_autonomous_pipeline.py

자율 파이프라인 (PASS_EXECUTE → 자동 preview/검증/판정) 테스트.

1. select_ready_candidates: ready/not_ready 분리
2. process_candidate: preview→ledger→verification→판정 연결
3. run 게이트: pipeline off / 장외 / autonomous off / kill switch / 스로틀
4. 심볼당 1일 1회 dedup
5. no_action_diagnosis 기록
6. verdict 규칙: 무제한 한도(0)에서 큰 금액도 PASS
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

import core.toss_autonomous_pipeline as tap


_NOW = datetime(2026, 7, 3, 10, 0, tzinfo=KST)  # 목요일 장중

_POLICY_ON = {
    "autonomous_mode": True,
    "autonomous_kill_switch": False,
    "max_order_krw": 0,          # 무제한
    "blocked_symbols": [],
    "autonomous_allowed_sides": ["buy"],
    "adapter_status": "enabled",
    "live_order_allowed": True,
}


def _candidate(symbol="091180.KS", **kw) -> dict:
    base = {
        "symbol": symbol,
        "side": "buy",
        "quantity": 10,
        "limit_price": 30000.0,
        "stop_loss": 28000,
        "target_price": 34000,
        "stock_agent_ready": True,
        "decision_bucket": "PASS_EXECUTE",
        "score": 75,
        "risk_reward": 2.0,
        "income_strategy": {
            "income_pass": True,
            "income_grade": "INCOME_PASS",
            "expected_pnl_krw": 12_000,
            "income_edge_ratio": 0.02,
        },
    }
    base.update(kw)
    return base


# ── 1. select_ready_candidates ───────────────────────────────────

class TestSelect(unittest.TestCase):
    def test_split_ready_and_not_ready(self):
        items = [
            _candidate("A.KS"),
            _candidate("B.KS", stock_agent_ready=False, block_reason="RR 부족"),
        ]
        with patch("core.dashboard_data.toss_buy_candidates_data",
                   return_value={"items": items}):
            ready, not_ready = tap.select_ready_candidates()
        self.assertEqual([r["symbol"] for r in ready], ["A.KS"])
        self.assertEqual(not_ready, [{"symbol": "B.KS", "reason": "RR 부족"}])


# ── 2. process_candidate ─────────────────────────────────────────

class TestProcessCandidate(unittest.TestCase):
    def _run(self, candidate, policy=None, verdict_status="PASS"):
        policy = policy or dict(_POLICY_ON)
        recorded = {}

        def fake_record_verif(verification_id, status, reasons, checks, hermes_message=""):
            recorded.update({
                "verification_id": verification_id, "status": status,
                "reasons": reasons, "hermes_message": hermes_message,
            })
            return {"ok": True, "status": status}

        with patch("core.toss_live_pilot_ledger.record_live_pilot_preview",
                   return_value={"ok": True, "pilot_id": "tlive_test_1"}), \
             patch("core.toss_live_pilot_verification.create_verification_request",
                   return_value={"verification_id": "hv_test_1", "status": "PENDING"}), \
             patch("core.toss_live_pilot_verification.record_hermes_verification",
                   side_effect=fake_record_verif), \
             patch("core.toss_live_pilot_hermes_bridge.build_default_hermes_verdict",
                   return_value={"status": verdict_status, "reasons": ["ok"], "checks": {}}) as mock_verdict:
            r = tap.process_candidate(candidate, policy)
        return r, recorded, mock_verdict

    def test_full_chain_pass(self):
        r, recorded, _ = self._run(_candidate())
        self.assertEqual(r["stage"], "verdict_recorded")
        self.assertEqual(r["verdict"], "PASS")
        self.assertEqual(r["pilot_id"], "tlive_test_1")
        self.assertEqual(recorded["status"], "PASS")
        self.assertIn("auto_verifier", recorded["hermes_message"])
        self.assertIn("bucket=PASS_EXECUTE", recorded["hermes_message"])

    def test_prediction_ref_flows_preview_ledger_and_verification(self):
        candidate = _candidate()
        candidate["source_prediction_id"] = 42
        with patch("core.toss_live_pilot_ledger.record_live_pilot_preview",
                   return_value={"ok": True, "pilot_id": "tlive_trace"}) as ledger, \
             patch("core.toss_live_pilot_verification.create_verification_request",
                   return_value={"verification_id": "hv_trace", "status": "PENDING"}) as verification, \
             patch("core.toss_live_pilot_verification.record_hermes_verification",
                   return_value={"ok": True}), \
             patch("core.toss_live_pilot_hermes_bridge.build_default_hermes_verdict",
                   return_value={"status": "HOLD", "reasons": [], "checks": {}}):
            tap.process_candidate(candidate, dict(_POLICY_ON))
        ledger_preview = ledger.call_args.args[0]
        verification_preview = verification.call_args.args[0]
        self.assertEqual(ledger_preview["decision_ref"], "prediction:42")
        self.assertEqual(verification_preview["decision_ref"], "prediction:42")

    def test_verdict_context_includes_unlimited_and_sides(self):
        _, _, mock_verdict = self._run(_candidate())
        ctx = mock_verdict.call_args[0][0]
        self.assertEqual(ctx["max_order_krw"], 0)
        self.assertEqual(ctx["allowed_sides"], ["buy"])

    def test_autonomous_sides_env_extends_sides(self):
        policy = dict(_POLICY_ON, autonomous_allowed_sides=["BUY", "SELL"])
        _, _, mock_verdict = self._run(_candidate(), policy=policy)
        ctx = mock_verdict.call_args[0][0]
        self.assertEqual(ctx["allowed_sides"], ["buy", "sell"])

    def test_custom_reason_and_note(self):
        _, recorded, _ = self._run(_candidate())
        self.assertIn("auto_verifier(auto_pipeline)", recorded["hermes_message"])
        with patch("core.toss_live_pilot_ledger.record_live_pilot_preview",
                   return_value={"ok": True, "pilot_id": "tlive_test_2"}) as mock_ledger, \
             patch("core.toss_live_pilot_verification.create_verification_request",
                   return_value={"verification_id": "hv_test_2", "status": "PENDING"}), \
             patch("core.toss_live_pilot_verification.record_hermes_verification",
                   return_value={"ok": True}) as mock_rec, \
             patch("core.toss_live_pilot_hermes_bridge.build_default_hermes_verdict",
                   return_value={"status": "PASS", "reasons": [], "checks": {}}):
            tap.process_candidate(
                _candidate(), dict(_POLICY_ON),
                reason="auto_exit_sell", note="exit_type=stop_loss_hit",
            )
        self.assertEqual(mock_ledger.call_args.kwargs.get("reason"), "auto_exit_sell")
        self.assertIn("auto_verifier(auto_exit_sell): exit_type=stop_loss_hit",
                      mock_rec.call_args.kwargs.get("hermes_message", ""))

    def test_preview_blocked_stops_early(self):
        policy = dict(_POLICY_ON, blocked_symbols=["091180.KS"])
        with patch("core.toss_live_pilot_ledger.record_live_pilot_preview") as mock_ledger:
            r = tap.process_candidate(_candidate(), policy)
        self.assertEqual(r["stage"], "preview_blocked")
        mock_ledger.assert_not_called()


# ── 3-5. run_toss_autonomous_pipeline ────────────────────────────

class TestRun(unittest.TestCase):
    def _run(self, tmp, now=_NOW, policy=None, ready=None, not_ready=None,
             force=False, process_result=None):
        policy = policy if policy is not None else dict(_POLICY_ON)
        ready = ready if ready is not None else [_candidate()]
        not_ready = not_ready or []
        process_result = process_result or {
            "symbol": "091180.KS", "stage": "verdict_recorded", "verdict": "PASS",
        }
        state_path = Path(tmp) / "state.json"
        with patch.object(tap, "_state_path", return_value=state_path), \
             patch("core.market_hours.is_kr_market_open", return_value=True), \
             patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=policy), \
             patch.object(tap, "select_ready_candidates",
                          return_value=(ready, not_ready)), \
             patch.object(tap, "retry_retryable_orders",
                          return_value={"retried": 0, "sent": 0, "exhausted": 0}), \
             patch.object(tap, "process_candidate", return_value=process_result):
            return tap.run_toss_autonomous_pipeline(now=now, force=force)

    def test_first_run_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp)
            self.assertEqual(r["attempted"], 1)
            self.assertEqual(r["pass_count"], 1)

    def test_throttled(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            r2 = self._run(tmp, now=_NOW + timedelta(minutes=5))
            self.assertEqual(r2.get("skipped"), "throttled")

    def test_throttle_default_10min(self):
        # 기본 스로틀 10분 — 11분 후에는 재실행 (throttled 아님)
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            r2 = self._run(tmp, now=_NOW + timedelta(minutes=11))
            self.assertNotEqual(r2.get("skipped"), "throttled")

    def test_throttle_env_override(self):
        with patch.dict("os.environ", {"TOSS_PIPELINE_INTERVAL_MIN": "20"}):
            self.assertEqual(tap._throttle_minutes(), 20)

    def test_throttle_env_floor_5min(self):
        with patch.dict("os.environ", {"TOSS_PIPELINE_INTERVAL_MIN": "1"}):
            self.assertEqual(tap._throttle_minutes(), 5)

    def test_throttle_env_invalid_falls_back(self):
        with patch.dict("os.environ", {"TOSS_PIPELINE_INTERVAL_MIN": "abc"}):
            self.assertEqual(tap._throttle_minutes(), 10)

    def test_same_day_symbol_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            r2 = self._run(tmp, now=_NOW + timedelta(minutes=30))
            self.assertEqual(r2["attempted"], 0)
            self.assertEqual(
                r2["no_action_diagnosis"]["reason"],
                "all_ready_candidates_already_attempted_today",
            )

    def test_next_day_reattempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            r2 = self._run(tmp, now=_NOW + timedelta(days=1))
            self.assertEqual(r2["attempted"], 1)

    def test_no_ready_diagnosis(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, ready=[],
                          not_ready=[{"symbol": "X.KS", "reason": "RR 부족"}])
            self.assertEqual(r["attempted"], 0)
            diag = r["no_action_diagnosis"]
            self.assertEqual(diag["reason"], "no_ready_candidates")
            self.assertEqual(diag["not_ready"][0]["reason"], "RR 부족")

    def test_autonomous_mode_off_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = dict(_POLICY_ON, autonomous_mode=False)
            r = self._run(tmp, policy=policy)
            self.assertEqual(r.get("skipped"), "autonomous_mode_disabled")

    def test_kill_switch_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = dict(_POLICY_ON, autonomous_kill_switch=True)
            r = self._run(tmp, policy=policy)
            self.assertEqual(r.get("skipped"), "kill_switch_active")

    def test_pipeline_env_off_skips(self):
        with patch.dict("os.environ", {"TOSS_AUTO_PIPELINE_ENABLED": "false"}):
            r = tap.run_toss_autonomous_pipeline(now=_NOW)
            self.assertEqual(r.get("skipped"), "pipeline_disabled")

    def test_market_closed_skips(self):
        with patch("core.market_hours.is_kr_market_open", return_value=False):
            r = tap.run_toss_autonomous_pipeline(now=_NOW)
            self.assertEqual(r.get("skipped"), "market_closed")

    def test_us_tradeable_session_processes_us_candidate_when_kr_closed(self):
        us_now = datetime(2026, 7, 3, 23, 0, tzinfo=KST)  # KST Friday, US regular session
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            us_candidate = _candidate("NVDA", limit_price=190.0, estimated_amount_krw=287000)
            with patch.object(tap, "_state_path", return_value=state_path),                  patch("core.market_hours.get_market_session", return_value={"kr": "CLOSED", "us": "US_REGULAR"}),                  patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy", return_value=dict(_POLICY_ON, allowed_asset_types=["US_STOCK", "KR_STOCK"])),                  patch.object(tap, "select_ready_candidates", return_value=([us_candidate], [])) as mock_select,                  patch.object(tap, "retry_retryable_orders", return_value={"retried": 0, "sent": 0, "exhausted": 0}),                  patch.object(tap, "process_candidate", return_value={"symbol": "NVDA", "stage": "verdict_recorded", "verdict": "PASS"}):
                r = tap.run_toss_autonomous_pipeline(now=us_now)
        self.assertEqual(r.get("attempted"), 1)
        self.assertEqual(mock_select.call_args.kwargs.get("market"), "US")

    def test_select_ready_candidates_passes_market_to_candidate_feed(self):
        items = [_candidate("NVDA")]
        with patch("core.dashboard_data.toss_buy_candidates_data", return_value={"items": items}) as mock_feed:
            ready, _ = tap.select_ready_candidates(market="US")
        self.assertEqual([r["symbol"] for r in ready], ["NVDA"])
        self.assertEqual(mock_feed.call_args.kwargs.get("market"), "US")


# ── 6. retry sweep ───────────────────────────────────────────────

class TestRetrySweep(unittest.TestCase):
    _RETRYABLE = {
        "pilot_id": "tlive_retry_1",
        "status": "live_send_retryable",
        "created_at": _NOW.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "symbol": "091180.KS",
        "side": "buy",
        "quantity": 10,
        "limit_price": 30000,
        "estimated_amount_krw": 300000,
        "failure_reason": "network_error",
    }

    def _sweep(self, state, records=None, verdict_status="PASS"):
        records = records if records is not None else [dict(self._RETRYABLE)]
        finalize_calls = []

        def fake_finalize(pilot_id, allow_retry=False):
            finalize_calls.append((pilot_id, allow_retry))
            return {"ok": True, "live_order_sent": True}

        failed_calls = []
        with patch("core.toss_live_pilot_ledger.list_live_pilot_records",
                   return_value=records), \
             patch("core.toss_live_pilot_ledger.record_live_send_failed",
                   side_effect=lambda pid, failure_reason="": failed_calls.append((pid, failure_reason)) or {"ok": True}), \
             patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=dict(_POLICY_ON)), \
             patch("core.toss_live_pilot_verification.create_verification_request",
                   return_value={"verification_id": "hv_retry_1"}), \
             patch("core.toss_live_pilot_verification.record_hermes_verification",
                   return_value={"ok": True}), \
             patch("core.toss_live_pilot_hermes_bridge.build_default_hermes_verdict",
                   return_value={"status": verdict_status, "reasons": [], "checks": {}}), \
             patch("core.toss_autonomous_finalizer.try_autonomous_finalize",
                   side_effect=fake_finalize):
            r = tap.retry_retryable_orders(now=_NOW, state=state)
        return r, finalize_calls, failed_calls

    def test_retry_pass_finalizes_with_allow_retry(self):
        state = {}
        r, finalize_calls, _ = self._sweep(state)
        self.assertEqual(r["retried"], 1)
        self.assertEqual(r["sent"], 1)
        self.assertEqual(finalize_calls, [("tlive_retry_1", True)])
        self.assertEqual(state["retry_counts"]["tlive_retry_1"], 1)

    def test_retry_exhausted_becomes_failed(self):
        state = {"retry_date": _NOW.strftime("%Y-%m-%d"),
                 "retry_counts": {"tlive_retry_1": 3}}
        r, finalize_calls, failed_calls = self._sweep(state)
        self.assertEqual(r["exhausted"], 1)
        self.assertEqual(r["retried"], 0)
        self.assertEqual(finalize_calls, [])
        self.assertIn("retry_exhausted", failed_calls[0][1])

    def test_non_retryable_records_skipped(self):
        state = {}
        r, finalize_calls, _ = self._sweep(
            state, records=[dict(self._RETRYABLE, status="live_sent")])
        self.assertEqual(r["retried"], 0)
        self.assertEqual(finalize_calls, [])

    def test_hold_verdict_no_finalize(self):
        state = {}
        r, finalize_calls, _ = self._sweep(state, verdict_status="HOLD")
        self.assertEqual(r["retried"], 1)
        self.assertEqual(r["sent"], 0)
        self.assertEqual(finalize_calls, [])


# ── 7. 실패 사유 의무화 ──────────────────────────────────────────

class TestFailureReasonMandatory(unittest.TestCase):
    def test_empty_reason_replaced(self):
        import core.toss_live_pilot_ledger as ledger
        with patch.object(ledger, "_conn") as mock_conn, \
             patch.object(ledger, "_db_lock"):
            conn = mock_conn.return_value
            conn.execute.return_value.fetchone.return_value = {"status": "previewed"}
            ledger.record_live_send_failed("p1", failure_reason="  ")
            update_args = conn.execute.call_args_list[-1][0][1]
        self.assertEqual(update_args[0], "unspecified_failure")


# ── 8. verdict 무제한 한도 ───────────────────────────────────────

class TestVerdictUnlimited(unittest.TestCase):
    def test_zero_max_krw_means_unlimited(self):
        from core.toss_live_pilot_hermes_bridge import build_default_hermes_verdict
        ctx = {
            "symbol": "091180.KS", "side": "buy",
            "limit_price": 30000, "estimated_amount_krw": 3_000_000,
            "max_order_krw": 0,  # 무제한
            "allowed_sides": ["buy"],
            "blocked_symbols": "",
        }
        v = build_default_hermes_verdict(ctx)
        self.assertEqual(v["status"], "PASS")

    def test_missing_max_krw_defaults_100k(self):
        from core.toss_live_pilot_hermes_bridge import build_default_hermes_verdict
        ctx = {
            "symbol": "091180.KS", "side": "buy",
            "limit_price": 30000, "estimated_amount_krw": 3_000_000,
            "allowed_sides": ["buy"],
            "blocked_symbols": "",
        }
        v = build_default_hermes_verdict(ctx)
        self.assertEqual(v["status"], "BLOCK")


# ── 9. 자본 가동률 KPI ───────────────────────────────────────────

class TestDeploymentKpi(unittest.TestCase):
    def _kpi(self, summary):
        with patch("core.dashboard_data.toss_account_summary", return_value=summary):
            return tap.compute_deployment_kpi()

    def test_in_range(self):
        kpi = self._kpi({"market_value": {"krw": 7_000_000}, "cash": {"krw": 3_000_000}})
        self.assertTrue(kpi["ok"])
        self.assertEqual(kpi["deployment_rate"], 0.7)
        self.assertEqual(kpi["status"], "in_range")

    def test_below_target(self):
        kpi = self._kpi({"market_value": {"krw": 2_000_000}, "cash": {"krw": 8_000_000}})
        self.assertEqual(kpi["status"], "below_target")

    def test_above_target(self):
        kpi = self._kpi({"market_value": {"krw": 9_000_000}, "cash": {"krw": 1_000_000}})
        self.assertEqual(kpi["status"], "above_target")

    def test_missing_values_fail_safe(self):
        kpi = self._kpi({"market_value": {}, "cash": {}, "error": "Toss account unavailable"})
        self.assertFalse(kpi["ok"])
        self.assertIn("unavailable", kpi["reason"])

    def test_zero_total_fail_safe(self):
        kpi = self._kpi({"market_value": {"krw": 0}, "cash": {"krw": 0}})
        self.assertFalse(kpi["ok"])

    def test_summary_exception_fail_safe(self):
        with patch("core.dashboard_data.toss_account_summary",
                   side_effect=RuntimeError("boom")):
            kpi = tap.compute_deployment_kpi()
        self.assertFalse(kpi["ok"])


# ── 10. 일일 리포트 ──────────────────────────────────────────────

_AFTER_CLOSE = datetime(2026, 7, 3, 16, 5, tzinfo=KST)  # 금요일 아님, 목 16:05


class TestDailyReport(unittest.TestCase):
    def _send(self, tmp, now=_AFTER_CLOSE, force=False, kpi=None,
              state=None, sent_return=True):
        kpi = kpi or {
            "ok": True, "deployment_rate": 0.7, "market_value_krw": 7_000_000,
            "cash_krw": 3_000_000, "total_krw": 10_000_000,
            "target_min": 0.6, "target_max": 0.8, "status": "in_range",
        }
        state_path = Path(tmp) / "state.json"
        if state is not None:
            state_path.write_text(json.dumps(state), encoding="utf-8")
        sent_messages = []

        def _fake_send(text):
            sent_messages.append(text)
            return sent_return

        with patch.object(tap, "_state_path", return_value=state_path), \
             patch.object(tap, "compute_deployment_kpi", return_value=kpi), \
             patch("core.telegram.send_simple_message", side_effect=_fake_send):
            result = tap.send_daily_pipeline_report(now=now, force=force)
        return result, sent_messages, state_path

    def test_sends_after_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, msgs, _ = self._send(tmp)
            self.assertTrue(r["sent"])
            self.assertEqual(len(msgs), 1)
            self.assertIn("자본 가동률: 70.0%", msgs[0])

    def test_before_report_hour_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, msgs, _ = self._send(tmp, now=datetime(2026, 7, 3, 14, 0, tzinfo=KST))
            self.assertEqual(r.get("skipped"), "before_report_hour")
            self.assertEqual(msgs, [])

    def test_weekend_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, _, _ = self._send(tmp, now=datetime(2026, 7, 4, 16, 5, tzinfo=KST))
            self.assertEqual(r.get("skipped"), "weekend")

    def test_dedup_same_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            r1, _, _ = self._send(tmp)
            r2, msgs2, _ = self._send(tmp)
            self.assertTrue(r1["sent"])
            self.assertEqual(r2.get("skipped"), "already_sent_today")
            self.assertEqual(msgs2, [])

    def test_send_failure_no_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            r1, _, state_path = self._send(tmp, sent_return=False)
            self.assertFalse(r1["sent"])
            saved = json.loads(state_path.read_text()) if state_path.exists() else {}
            self.assertNotEqual(saved.get("report_date"), "2026-07-03")

    def test_report_includes_pipeline_results_and_diagnosis(self):
        state = {
            "attempted_date": "2026-07-03",
            "attempted": {"091180.KS": {"at": "10:00", "stage": "verdict_recorded", "verdict": "PASS"}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            _, msgs, _ = self._send(tmp, state=state)
            self.assertIn("091180.KS: PASS", msgs[0])
            self.assertIn("시도: 1건 / PASS 1건", msgs[0])

    def test_no_action_diagnosis_in_report(self):
        state = {
            "attempted_date": "2026-07-03",
            "attempted": {},
            "no_action_diagnosis": {
                "reason": "no_ready_candidates",
                "not_ready": [{"symbol": "X.KS", "reason": "RR 부족"}],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            _, msgs, _ = self._send(tmp, state=state)
            self.assertIn("미거래 사유: no_ready_candidates", msgs[0])
            self.assertIn("X.KS: RR 부족", msgs[0])

    def test_kpi_failure_still_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, msgs, _ = self._send(
                tmp, kpi={"ok": False, "reason": "toss_api_cooldown"})
            self.assertTrue(r["sent"])
            self.assertIn("조회 실패", msgs[0])




def test_select_ready_candidates_requires_income_pass():
    items = [
        _candidate("A.KS", income_strategy={"income_pass": True, "expected_pnl_krw": 9000, "income_edge_ratio": 0.02}),
        _candidate("B.KS", income_strategy={"income_pass": False, "income_block_reason": "expected_pnl_below_threshold"}),
    ]
    with patch("core.dashboard_data.toss_buy_candidates_data", return_value={"items": items}):
        ready, not_ready = tap.select_ready_candidates()
    assert [r["symbol"] for r in ready] == ["A.KS"]
    assert not_ready == [{"symbol": "B.KS", "reason": "expected_pnl_below_threshold"}]


def test_select_ready_candidates_sorts_by_expected_income():
    items = [
        _candidate("LOW.KS", income_strategy={"income_pass": True, "expected_pnl_krw": 8_000, "income_edge_ratio": 0.01}),
        _candidate("HIGH.KS", income_strategy={"income_pass": True, "expected_pnl_krw": 18_000, "income_edge_ratio": 0.03}),
    ]
    with patch("core.dashboard_data.toss_buy_candidates_data", return_value={"items": items}):
        ready, _ = tap.select_ready_candidates()
    assert [r["symbol"] for r in ready] == ["HIGH.KS", "LOW.KS"]


if __name__ == "__main__":
    unittest.main()
