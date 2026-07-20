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

import copy
import json
import logging
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

KST = timezone(timedelta(hours=9))

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import core.toss_autonomous_pipeline as tap


@pytest.fixture(autouse=True)
def _isolated_quality_db(tmp_path, monkeypatch, request):
    from core import toss_quality_gate as qg

    monkeypatch.setattr(qg, "_outcomes_db_path", lambda: tmp_path / "quality.db")
    qg._outcomes_schema_created = False
    if getattr(request.node.cls, "__name__", "") != "TestKrBuySafetyGate":
        monkeypatch.setattr(
            tap,
            "_evaluate_kr_buy_safety",
            lambda *_args, **_kwargs: {"ok": True, "reason": "test_safe_market"},
        )
    yield
    qg._outcomes_schema_created = False


_NOW = datetime(2026, 7, 3, 10, 0, tzinfo=KST)  # 목요일 장중

_POLICY_ON = {
    "mode": "autonomous_live_pilot",
    "autonomous_mode": True,
    "autonomous_kill_switch": False,
    "live_pilot_enabled": True,
    "requires_user_confirmation": False,
    "requires_second_confirmation": False,
    "all_live_gates_open": True,
    "env_live_pilot_enabled": True,
    "env_live_order_allowed": True,
    "env_live_adapter_enabled": True,
    "max_order_krw": 0,          # 무제한
    "blocked_symbols": [],
    "autonomous_allowed_sides": ["buy", "sell"],
    "side_mode": "BUY_SELL",
    "allowed_sides": ["buy", "sell"],
    "sell_allowed": True,
    "adapter_status": "enabled",
    "live_order_allowed": True,
    "live_transport_status": "configured",
}

_BUY_CANDIDATE_SCHEMA = "toss_buy_candidates.v3.dual_income_ev"
_INCOME_GATE_VERSION = "income_v2_dual_ev"
_INCOME_LIVENESS_VERSION = "income_liveness_v1"


def _seal_quality_proof_for_test(candidate: dict) -> None:
    from core import toss_quality_gate as qg

    breakdown = candidate["quality_breakdown"]
    breakdown["decision_bucket"] = candidate["decision_bucket"]
    breakdown["decision_reason"] = candidate["decision_reason"]
    breakdown["score_symbol"] = candidate["symbol"].upper()
    breakdown["score_side"] = candidate["side"].lower()
    breakdown.update({
        "decision_change_pct": float(candidate.get("change_pct") or 0.0),
        "decision_days_to_earnings": -1,
        "decision_has_stop": True,
        "decision_has_target": True,
        "decision_blocking_risk_flags": [],
        "decision_origin_bucket": breakdown["decision_bucket"],
        "decision_origin_reason": breakdown["decision_reason"],
        "score_schema_version": qg.QUALITY_SCORE_SCHEMA_VERSION,
    })
    weight_hash = qg._weight_profile_hash()
    breakdown["weight_profile_hash"] = weight_hash
    breakdown["score_breakdown_sha256"] = qg._score_breakdown_hash(
        breakdown,
        schema_version=qg.QUALITY_SCORE_SCHEMA_VERSION,
        weight_hash=weight_hash,
    )
    assert breakdown["score_breakdown_sha256"]
    assert qg.attach_quality_proof(candidate) is True


def _candidate_feed(items, **scan_summary) -> dict:
    returned_items = items[:10]
    income_pass_count = sum(
        1
        for item in items
        if type(item) is dict
        and type(item.get("income_strategy")) is dict
        and item["income_strategy"].get("income_pass") is True
    )
    income_ready_count = sum(
        1
        for item in items
        if type(item) is dict and item.get("stock_agent_ready") is True
    )
    liveness_status = (
        "healthy"
        if income_ready_count > 0
        else "downstream_blocked"
        if income_pass_count > 0
        else "no_signal"
        if items
        else "idle"
    )
    return {
        "schema": _BUY_CANDIDATE_SCHEMA,
        "items": items,
        "scan_summary": {
            "income_gate_version": _INCOME_GATE_VERSION,
            "income_liveness_version": _INCOME_LIVENESS_VERSION,
            "income_liveness_status": liveness_status,
            "income_pass_count": income_pass_count,
            "income_ready_count": income_ready_count,
            "returned_candidate_count": len(returned_items),
            "returned_income_ready_count": sum(
                1
                for item in returned_items
                if type(item) is dict and item.get("stock_agent_ready") is True
            ),
            "cache_contract_valid": True,
            "dependency_fallback_used": False,
            **scan_summary,
        },
    }


def _candidate(symbol="091180.KS", **kw) -> dict:
    base = {
        "symbol": symbol,
        "side": "buy",
        "market": "KR",
        "currency": "KRW",
        "quantity": 10,
        "limit_price": 30000.0,
        "stop_loss": 28000,
        "target_price": 34000,
        "stock_agent_ready": True,
        "executable_now": True,
        "quality_finalized": True,
        "income_execution_contract_valid": True,
        "missing_fields": [],
        "decision_bucket": "PASS_EXECUTE",
        "decision_reason": "quality pass",
        "quality_score": 75,
        "quality_breakdown": {
            "score_total": 75,
            "score_momentum": 18,
            "score_liquidity": 17,
            "score_risk_reward": 16,
            "score_reliability": 12,
            "score_market_regime": 12,
            "score_supply_demand": 0,
            "penalty_overheat": 0,
            "penalty_duplicate": 0,
            "penalty_event_risk": 0,
            "rr_ratio": 2.0,
            "regime": "강세장",
        },
        "score": 75,
        "risk_reward": 2.0,
        "income_strategy": {
            "income_pass": True,
            "income_grade": "INCOME_PASS",
            "expected_pnl_krw": 12_000,
            "income_edge_ratio": 0.02,
            "decision_expected_pnl_model": "income_exit_lifecycle_v1",
            "decision_expected_pnl_scope": "full_position_threshold_exit",
            "decision_expected_pnl_krw": 12_000,
            "decision_income_edge_ratio": 0.02,
        },
    }
    overrides = dict(kw)
    income_override = overrides.pop("income_strategy", None)
    base.update(overrides)
    if not base["symbol"].endswith((".KS", ".KQ")):
        if "market" not in kw:
            base["market"] = "US"
        if "currency" not in kw:
            base["currency"] = "USD"
    base["income_strategy"].update({
        "planned_entry_price": base["limit_price"],
        "planned_stop_loss": base["stop_loss"],
        "planned_target_price": base["target_price"],
        "planned_quantity": base["quantity"],
    })
    if income_override is not None:
        base["income_strategy"].update(income_override)
        if (
            "expected_pnl_krw" in income_override
            and "decision_expected_pnl_krw" not in income_override
        ):
            base["income_strategy"]["decision_expected_pnl_krw"] = income_override["expected_pnl_krw"]
        if (
            "income_edge_ratio" in income_override
            and "decision_income_edge_ratio" not in income_override
        ):
            base["income_strategy"]["decision_income_edge_ratio"] = income_override["income_edge_ratio"]
    _seal_quality_proof_for_test(base)
    return base


def _corrupt_income_candidate(case: str) -> dict:
    candidate = _candidate()
    income = candidate["income_strategy"]
    if case == "side_missing":
        candidate.pop("side")
    elif case == "decision_contract_missing":
        for key in (
            "decision_expected_pnl_model",
            "decision_expected_pnl_scope",
            "decision_expected_pnl_krw",
            "decision_income_edge_ratio",
        ):
            income.pop(key)
    elif case == "decision_contract_partial":
        income.pop("decision_income_edge_ratio")
    elif case == "unknown_model":
        income["decision_expected_pnl_model"] = "unknown"
    elif case == "nan_ev":
        income["decision_expected_pnl_krw"] = float("nan")
    elif case == "inf_edge":
        income["decision_income_edge_ratio"] = float("inf")
    elif case == "numeric_strings":
        income["decision_expected_pnl_krw"] = "12000"
        income["decision_income_edge_ratio"] = "0.02"
    return candidate


# ── 1. select_ready_candidates ───────────────────────────────────

class TestSelect(unittest.TestCase):
    def test_api_schema_and_income_gate_version_mismatch_fail_closed(self):
        valid = _candidate_feed([_candidate()])
        bad_envelopes = [
            dict(valid, schema="toss_buy_candidates.v2"),
            dict(valid, schema=None),
            dict(valid, scan_summary={"income_gate_version": "income_v1"}),
            dict(valid, scan_summary={}),
        ]
        for envelope in bad_envelopes:
            with self.subTest(envelope=envelope), patch(
                "core.dashboard_data.toss_buy_candidates_data",
                return_value=envelope,
            ):
                self.assertEqual(tap.select_ready_candidates(), ([], []))

    def test_cached_ready_item_liveness_mismatch_fails_closed(self):
        feed = _candidate_feed([_candidate()])
        feed["scan_summary"].update({
            "income_liveness_status": "no_signal",
            "income_pass_count": 0,
            "income_ready_count": 0,
        })

        with patch(
            "core.dashboard_data.toss_buy_candidates_data",
            return_value=feed,
        ):
            self.assertEqual(tap.select_ready_candidates(), ([], []))

    def test_malformed_ready_contract_fails_closed(self):
        for case in (
            "string_quantity",
            "missing_quality_proof",
            "side_subclass",
            "huge_limit_price",
            "stale_risk_reward",
            "income_entry_mismatch",
            "income_entry_type_mismatch",
            "income_stop_mismatch",
            "income_target_mismatch",
            "income_quantity_mismatch",
        ):
            candidate = _candidate()
            if case == "string_quantity":
                candidate["quantity"] = "10"
            elif case == "side_subclass":
                candidate["side"] = type("Side", (str,), {})("buy")
            elif case == "huge_limit_price":
                candidate["limit_price"] = 10**400
            elif case == "stale_risk_reward":
                from core import toss_quality_gate as qg
                candidate["target_price"] = 30_001
                candidate["income_strategy"]["planned_target_price"] = 30_001
                assert qg.attach_quality_proof(candidate) is True
            elif case == "income_entry_mismatch":
                candidate["income_strategy"]["planned_entry_price"] = 30_001.0
            elif case == "income_entry_type_mismatch":
                candidate["income_strategy"]["planned_entry_price"] = 30_000
            elif case == "income_stop_mismatch":
                candidate["income_strategy"]["planned_stop_loss"] = 27_999
            elif case == "income_target_mismatch":
                candidate["income_strategy"]["planned_target_price"] = 35_000
            elif case == "income_quantity_mismatch":
                candidate["income_strategy"]["planned_quantity"] = 11
            else:
                candidate["quality_breakdown"].pop(
                    "candidate_snapshot_sha256", None
                )
            with self.subTest(case=case), patch(
                "core.dashboard_data.toss_buy_candidates_data",
                return_value=_candidate_feed([candidate]),
            ):
                ready, not_ready = tap.select_ready_candidates()
                self.assertEqual(ready, [])
                self.assertEqual(len(not_ready), 1)
                self.assertTrue(not_ready[0]["reason"].startswith("candidate_"))

    def test_non_dict_candidate_item_fails_closed(self):
        corrupt_items = ["not-a-list", ["not-a-dict"], [type("Item", (dict,), {})()]]
        for items in corrupt_items:
            envelope = _candidate_feed([])
            envelope["items"] = items
            with self.subTest(items=items), patch(
                "core.dashboard_data.toss_buy_candidates_data",
                return_value=envelope,
            ):
                self.assertEqual(tap.select_ready_candidates(), ([], []))

    def test_split_ready_and_not_ready(self):
        items = [
            _candidate("A.KS"),
            _candidate("B.KS", stock_agent_ready=False, block_reason="RR 부족"),
        ]
        with patch("core.dashboard_data.toss_buy_candidates_data",
                   return_value=_candidate_feed(items)):
            ready, not_ready = tap.select_ready_candidates()
        self.assertEqual([r["symbol"] for r in ready], ["A.KS"])
        self.assertEqual(not_ready, [{"symbol": "B.KS", "reason": "RR 부족"}])

    def test_rejects_corrupt_executable_income_contracts(self):
        cases = (
            "side_missing",
            "decision_contract_missing",
            "decision_contract_partial",
            "unknown_model",
            "nan_ev",
            "inf_edge",
            "numeric_strings",
        )
        items = [_corrupt_income_candidate(case) for case in cases]

        with patch("core.dashboard_data.toss_buy_candidates_data", return_value=_candidate_feed(items)):
            ready, not_ready = tap.select_ready_candidates()

        self.assertEqual(ready, [])
        self.assertEqual(len(not_ready), len(cases))
        self.assertTrue(all(row["reason"].startswith("income_contract_") for row in not_ready))

    def test_consumer_receives_bounded_original_order_before_ready_split(self):
        import core.shadow_measurement_producer as producer

        captured_at = datetime(2026, 7, 15, 1, 2, 3, tzinfo=timezone.utc)
        items = [
            _candidate(
                f"{index:06d}.KS",
                stock_agent_ready=index != 1,
                block_reason="blocked" if index == 1 else "",
                income_strategy={
                    "income_pass": index != 1,
                    "expected_pnl_krw": (index + 1) * 1_000,
                    "income_edge_ratio": (index + 1) / 100,
                },
            )
            for index in range(12)
        ]
        consumed = []

        def consumer(cohort):
            self.assertIsInstance(cohort, producer.SanitizedFinalCandidateCohort)
            payloads = [json.loads(item.payload_json) for item in cohort.candidates]
            consumed.append(
                (
                    [item["symbol"] for item in payloads],
                    cohort.market_scope,
                    cohort.captured_at_utc,
                    cohort.fallback_used,
                )
            )

        with patch(
            "core.dashboard_data.toss_buy_candidates_data",
            return_value=_candidate_feed(items, dependency_fallback_used=True),
        ) as feed, patch.object(tap, "_utc_now", return_value=captured_at):
            ready, not_ready = tap.select_ready_candidates(
                limit=99,
                market="KR",
                cohort_consumer=consumer,
            )

        expected_order = [f"{index:06d}.KS" for index in range(10)]
        self.assertEqual(feed.call_args.kwargs["limit"], 10)
        self.assertEqual(consumed, [(expected_order, "KR", captured_at, True)])
        self.assertEqual(
            [item["symbol"] for item in ready],
            list(reversed(expected_order[2:])) + [expected_order[0]],
        )
        self.assertEqual(
            not_ready, [{"symbol": "000001.KS", "reason": "blocked"}]
        )

    def test_consumer_failure_cannot_change_split_sort_or_outputs(self):
        items = [
            _candidate(
                "LOW.KS",
                income_strategy={
                    "income_pass": True,
                    "expected_pnl_krw": 1_000,
                    "income_edge_ratio": 0.01,
                },
            ),
            _candidate(
                "HIGH.KS",
                income_strategy={
                    "income_pass": True,
                    "expected_pnl_krw": 9_000,
                    "income_edge_ratio": 0.09,
                },
            ),
            _candidate("BLOCKED.KS", stock_agent_ready=False, block_reason="blocked"),
        ]
        original = copy.deepcopy(items)

        with patch(
            "core.dashboard_data.toss_buy_candidates_data",
            return_value=_candidate_feed(copy.deepcopy(items)),
        ):
            expected = tap.select_ready_candidates()

        def failing_consumer(cohort):
            self.assertIsInstance(cohort.candidates, tuple)
            raise RuntimeError("consumer-secret-must-not-escape")

        with self.assertLogs(tap.log, level=logging.WARNING) as captured_logs:
            with patch(
                "core.dashboard_data.toss_buy_candidates_data",
                return_value=_candidate_feed(items),
            ):
                observed = tap.select_ready_candidates(
                    cohort_consumer=failing_consumer
                )

        self.assertEqual(observed, expected)
        self.assertEqual(items, original)
        log_output = "\n".join(captured_logs.output)
        self.assertIn("RuntimeError", log_output)
        self.assertNotIn("consumer-secret-must-not-escape", log_output)

    def test_provider_limit_is_bounded_from_zero_through_ten(self):
        for requested, expected in ((-7, 0), (0, 0), (4, 4), (99, 10)):
            with self.subTest(requested=requested), patch(
                "core.dashboard_data.toss_buy_candidates_data",
                return_value=_candidate_feed([]),
            ) as feed:
                tap.select_ready_candidates(limit=requested)
            self.assertEqual(feed.call_args.kwargs["limit"], expected)

    def test_clock_is_captured_immediately_after_provider_returns(self):
        events = []
        captured_at = datetime(2026, 7, 15, 1, 2, 3, tzinfo=timezone.utc)

        def provider(**_kwargs):
            events.append("provider_return")
            return _candidate_feed([_candidate()])

        def clock():
            events.append("clock")
            return captured_at

        def consumer(_cohort):
            events.append("consumer")

        with patch(
            "core.dashboard_data.toss_buy_candidates_data", side_effect=provider
        ), patch.object(tap, "_utc_now", side_effect=clock):
            tap.select_ready_candidates(cohort_consumer=consumer)

        self.assertEqual(events, ["provider_return", "clock", "consumer"])

    def test_malformed_fallback_metadata_is_unknown_without_blocking_ready(self):
        malformed_values = [None, "true", 1, {}]
        for fallback_value in malformed_values:
            consumed = []

            def consumer(cohort):
                consumed.append(cohort.fallback_used)

            with self.subTest(fallback_value=fallback_value), patch(
                "core.dashboard_data.toss_buy_candidates_data",
                return_value=_candidate_feed(
                    [_candidate()], dependency_fallback_used=fallback_value
                ),
            ):
                ready, not_ready = tap.select_ready_candidates(
                    cohort_consumer=consumer
                )

            self.assertEqual([item["symbol"] for item in ready], ["091180.KS"])
            self.assertEqual(not_ready, [])
            self.assertEqual(consumed, [])

    def test_provider_boundary_never_deepcopies_or_traverses_unknown_raw_fields(self):
        import core.shadow_measurement_producer as producer

        deepcopy_calls = []
        consumed = []

        class UnknownRawValue:
            def __deepcopy__(self, _memo):
                deepcopy_calls.append(True)
                raise AssertionError("unknown raw field must not be copied")

        item = _candidate(raw_unknown=UnknownRawValue())

        def consumer(batch):
            self.assertIsInstance(batch, producer.SanitizedFinalCandidateCohort)
            consumed.append(batch)

        with patch(
            "core.dashboard_data.toss_buy_candidates_data",
            return_value=_candidate_feed([item]),
        ):
            ready, not_ready = tap.select_ready_candidates(cohort_consumer=consumer)

        self.assertEqual(deepcopy_calls, [])
        self.assertEqual(len(consumed), 1)
        self.assertIsInstance(consumed[0].candidates, tuple)
        self.assertNotIn("raw_unknown", consumed[0].candidates[0].payload_json)
        self.assertEqual([candidate["symbol"] for candidate in ready], ["091180.KS"])
        self.assertEqual(not_ready, [])

    def test_provider_boundary_rejects_scan_summary_subclass_without_access(self):
        calls = []
        consumed = []

        class HostileSummary(dict):
            def get(self, *_args, **_kwargs):
                calls.append(True)
                raise AssertionError("subclass accessor must not run")

        with patch(
            "core.dashboard_data.toss_buy_candidates_data",
            return_value={
                "schema": _BUY_CANDIDATE_SCHEMA,
                "items": [_candidate()],
                "scan_summary": HostileSummary(),
            },
        ):
            ready, not_ready = tap.select_ready_candidates(
                cohort_consumer=lambda batch: consumed.append(batch)
            )

        self.assertEqual(calls, [])
        self.assertEqual(consumed, [])
        self.assertEqual((ready, not_ready), ([], []))

    def test_provider_boundary_rejects_envelope_and_items_subclasses_without_access(self):
        calls = []

        class HostileEnvelope(dict):
            def get(self, *_args, **_kwargs):
                calls.append("envelope_get")
                raise AssertionError("envelope accessor must not run")

            def __len__(self):
                calls.append("envelope_len")
                raise AssertionError("envelope length must not run")

        class HostileItems(list):
            def __len__(self):
                calls.append("items_len")
                raise AssertionError("items length must not run")

            def __getitem__(self, _key):
                calls.append("items_getitem")
                raise AssertionError("items accessor must not run")

        with patch(
            "core.dashboard_data.toss_buy_candidates_data",
            return_value=HostileEnvelope(items=[_candidate()]),
        ):
            ready, blocked = tap.select_ready_candidates(cohort_consumer=lambda _batch: None)
        self.assertEqual((ready, blocked), ([], []))

        consumed = []
        with patch(
            "core.dashboard_data.toss_buy_candidates_data",
            return_value={
                "schema": _BUY_CANDIDATE_SCHEMA,
                "items": HostileItems([_candidate()]),
                "scan_summary": {
                    "income_gate_version": _INCOME_GATE_VERSION,
                    "dependency_fallback_used": False,
                },
            },
        ):
            ready, blocked = tap.select_ready_candidates(
                cohort_consumer=lambda batch: consumed.append(batch)
            )
        self.assertEqual((ready, blocked), ([], []))
        self.assertEqual(consumed, [])
        self.assertEqual(calls, [])

    def test_unknown_fallback_skips_real_shadow_worker_without_blocking_ready(self):
        import core.shadow_measurement_producer as producer

        with patch(
            "core.dashboard_data.toss_buy_candidates_data",
            return_value=_candidate_feed(
                [_candidate(market="KR", currency="KRW")],
                dependency_fallback_used="true",
            ),
        ), patch.object(producer.threading, "Thread") as thread, self.assertLogs(
            tap.log, level=logging.WARNING
        ) as captured_logs:
            ready, not_ready = tap.select_ready_candidates(
                cohort_consumer=producer.enqueue_sanitized_final_candidate_cohort
            )

        self.assertEqual([item["symbol"] for item in ready], ["091180.KS"])
        self.assertEqual(not_ready, [])
        thread.assert_not_called()
        self.assertIn("ValueError", "\n".join(captured_logs.output))

    def test_utc_now_uses_actual_timezone_aware_utc_clock(self):
        before = datetime.now(timezone.utc)
        observed = tap._utc_now()
        after = datetime.now(timezone.utc)

        self.assertEqual(observed.utcoffset(), timedelta(0))
        self.assertLessEqual(before, observed)
        self.assertLessEqual(observed, after)


# ── 2. process_candidate ──────────────────────────────────────────

class TestProcessCandidate(unittest.TestCase):
    def _run(self, candidate, policy=None, verdict_status="PASS", live_order_sent=False):
        policy = policy or dict(_POLICY_ON)
        recorded = {}

        def fake_record_verif(verification_id, status, reasons, checks, hermes_message=""):
            recorded.update({
                "verification_id": verification_id, "status": status,
                "reasons": reasons, "hermes_message": hermes_message,
            })
            return {
                "ok": True,
                "status": status,
                "finalizer_ok": live_order_sent,
                "live_order_sent": live_order_sent,
            }

        with patch("core.toss_live_pilot_ledger.record_live_pilot_preview",
                   return_value={"ok": True, "pilot_id": "tlive_test_1"}), \
             patch("core.toss_live_pilot_verification.create_verification_request",
                   return_value={"verification_id": "hv_test_1", "status": "PENDING"}), \
             patch("core.toss_live_pilot_verification.record_hermes_verification",
                   side_effect=fake_record_verif), \
             patch("core.toss_live_pilot_hermes_bridge.build_default_hermes_verdict",
                   return_value={"status": verdict_status, "reasons": ["ok"], "checks": {}}) as mock_verdict, \
             patch("core.toss_quality_gate.record_execution_quality_decision",
                   return_value={"ok": True, "id": 1, "created": True}):
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
        self.assertIs(r["live_order_sent"], False)

    def test_projects_exact_live_send_result(self):
        sent, _, _ = self._run(_candidate(), live_order_sent=True)
        self.assertIs(sent["ok"], True)
        self.assertIs(sent["live_order_sent"], True)

        not_sent, _, _ = self._run(_candidate(), live_order_sent=False)
        self.assertIs(not_sent["ok"], True)
        self.assertIs(not_sent["live_order_sent"], False)

    def test_incomplete_or_truthy_policy_stops_before_preview(self):
        bad_policy = {
            "autonomous_mode": 1,
            "autonomous_kill_switch": False,
        }
        with patch("core.toss_live_pilot_preview.build_live_pilot_preview") as preview:
            result = tap.process_candidate(_candidate(), bad_policy)

        self.assertEqual(result["stage"], "policy_blocked")
        self.assertEqual(result["reason"], "policy_contract_invalid")
        preview.assert_not_called()

    def test_corrupt_income_contract_stops_before_preview(self):
        cases = (
            "side_missing",
            "decision_contract_missing",
            "decision_contract_partial",
            "unknown_model",
            "nan_ev",
            "inf_edge",
            "numeric_strings",
        )
        for case in cases:
            with self.subTest(case=case), \
                 patch("core.toss_live_pilot_preview.build_live_pilot_preview") as preview:
                result = tap.process_candidate(_corrupt_income_candidate(case), dict(_POLICY_ON))

            self.assertEqual(result["stage"], "income_contract_blocked")
            self.assertTrue(result["reason"].startswith("income_contract_"))
            preview.assert_not_called()

    def test_malformed_ready_contract_stops_before_all_boundaries(self):
        for case in (
            "string_quantity",
            "missing_quality_proof",
            "side_subclass",
            "huge_limit_price",
            "stale_risk_reward",
            "income_entry_mismatch",
            "income_entry_type_mismatch",
            "income_stop_mismatch",
            "income_target_mismatch",
            "income_quantity_mismatch",
        ):
            candidate = _candidate()
            if case == "string_quantity":
                candidate["quantity"] = "10"
            elif case == "side_subclass":
                candidate["side"] = type("Side", (str,), {})("buy")
            elif case == "huge_limit_price":
                candidate["limit_price"] = 10**400
            elif case == "stale_risk_reward":
                from core import toss_quality_gate as qg
                candidate["target_price"] = 30_001
                candidate["income_strategy"]["planned_target_price"] = 30_001
                assert qg.attach_quality_proof(candidate) is True
            elif case == "income_entry_mismatch":
                candidate["income_strategy"]["planned_entry_price"] = 30_001.0
            elif case == "income_entry_type_mismatch":
                candidate["income_strategy"]["planned_entry_price"] = 30_000
            elif case == "income_stop_mismatch":
                candidate["income_strategy"]["planned_stop_loss"] = 27_999
            elif case == "income_target_mismatch":
                candidate["income_strategy"]["planned_target_price"] = 35_000
            elif case == "income_quantity_mismatch":
                candidate["income_strategy"]["planned_quantity"] = 11
            else:
                candidate["quality_breakdown"].pop(
                    "candidate_snapshot_sha256", None
                )
            with self.subTest(case=case), patch(
                "core.ai_berkshire_toss.evaluate_ai_berkshire_buy_gate",
                return_value={"buy_block": False, "buy_reason": "ai_berkshire_pass"},
            ), patch(
                "core.toss_live_pilot_preview.build_live_pilot_preview"
            ) as preview, patch(
                "core.toss_live_pilot_ledger.record_live_pilot_preview"
            ) as ledger, patch(
                "core.toss_live_pilot_verification.create_verification_request"
            ) as verification:
                result = tap.process_candidate(candidate, dict(_POLICY_ON))

            self.assertEqual(result["stage"], "candidate_contract_blocked")
            preview.assert_not_called()
            ledger.assert_not_called()
            verification.assert_not_called()

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
                   return_value={"status": "HOLD", "reasons": [], "checks": {}}), \
             patch("core.toss_quality_gate.record_execution_quality_decision",
                   return_value={"ok": True, "id": 2, "created": True}):
            tap.process_candidate(candidate, dict(_POLICY_ON))
        ledger_preview = ledger.call_args.args[0]
        verification_preview = verification.call_args.args[0]
        self.assertEqual(ledger_preview["decision_ref"], "prediction:42")
        self.assertEqual(verification_preview["decision_ref"], "prediction:42")

    def test_buy_records_exact_quality_decision_before_verification(self):
        candidate = _candidate()
        with patch("core.toss_live_pilot_ledger.record_live_pilot_preview",
                   return_value={"ok": True, "pilot_id": "tlive_quality_1"}), \
             patch("core.toss_quality_gate.record_execution_quality_decision",
                   return_value={"ok": True, "id": 7, "created": True}) as quality, \
             patch("core.toss_live_pilot_verification.create_verification_request",
                   return_value={"verification_id": "hv_quality_1", "status": "PENDING"}), \
             patch("core.toss_live_pilot_verification.record_hermes_verification",
                   return_value={"ok": True}), \
             patch("core.toss_live_pilot_hermes_bridge.build_default_hermes_verdict",
                   return_value={"status": "HOLD", "reasons": [], "checks": {}}):
            result = tap.process_candidate(candidate, dict(_POLICY_ON))
        assert result["stage"] == "verdict_recorded"
        quality.assert_called_once()
        assert quality.call_args.kwargs["pilot_id"] == "tlive_quality_1"
        assert quality.call_args.kwargs["decision_ref"].startswith("execution_decision:")

    def test_quality_attribution_failure_stops_before_verification(self):
        with patch("core.toss_live_pilot_ledger.record_live_pilot_preview",
                   return_value={"ok": True, "pilot_id": "tlive_quality_2"}), \
             patch("core.toss_quality_gate.record_execution_quality_decision",
                   return_value={"ok": False, "reason": "synthetic"}), \
             patch("core.toss_live_pilot_verification.create_verification_request") as verification:
            result = tap.process_candidate(_candidate(), dict(_POLICY_ON))
        assert result["stage"] == "quality_attribution_failed"
        verification.assert_not_called()

    def test_quality_db_exception_stops_before_verification(self):
        with patch("core.toss_live_pilot_ledger.record_live_pilot_preview",
                   return_value={"ok": True, "pilot_id": "tlive_quality_db_error"}), \
             patch("core.toss_quality_gate.record_execution_quality_decision",
                   side_effect=RuntimeError("quality schema unavailable")), \
             patch("core.toss_live_pilot_verification.create_verification_request") as verification:
            result = tap.process_candidate(_candidate(), dict(_POLICY_ON))

        assert result["stage"] == "quality_attribution_failed"
        assert result["reason"] == "quality_record_exception"
        verification.assert_not_called()

    def test_buy_without_executable_bucket_stops_before_quality_and_verification(self):
        candidate = _candidate()
        candidate.pop("decision_bucket", None)
        with patch("core.toss_live_pilot_preview.build_live_pilot_preview") as preview, \
             patch("core.toss_live_pilot_ledger.record_live_pilot_preview") as ledger, \
             patch("core.toss_quality_gate.record_execution_quality_decision") as quality, \
             patch("core.toss_live_pilot_verification.create_verification_request") as verification:
            result = tap.process_candidate(candidate, dict(_POLICY_ON))

        assert result["stage"] == "candidate_contract_blocked"
        assert result["reason"] == "candidate_identity_invalid"
        preview.assert_not_called()
        ledger.assert_not_called()
        quality.assert_not_called()
        verification.assert_not_called()

    def test_verdict_context_includes_unlimited_and_sides(self):
        _, _, mock_verdict = self._run(_candidate())
        ctx = mock_verdict.call_args[0][0]
        self.assertEqual(ctx["max_order_krw"], 0)
        self.assertEqual(ctx["allowed_sides"], ["buy", "sell"])

    def test_autonomous_sides_env_extends_sides(self):
        policy = dict(_POLICY_ON, autonomous_allowed_sides=["buy", "sell"])
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
                   return_value={"status": "PASS", "reasons": [], "checks": {}}), \
             patch("core.toss_quality_gate.record_execution_quality_decision",
                   return_value={"ok": True, "id": 3, "created": True}):
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


# ── 3. KR 신규 BUY 시장 안전 게이트 ──────────────────────────────

class TestKrBuySafetyGate(unittest.TestCase):
    @staticmethod
    def _market(kospi=-0.5, kosdaq=0.2):
        as_of = datetime(2026, 7, 16, 10, 30, tzinfo=KST).timestamp()
        return {
            "indices": {
                "KOSPI": {"pct": kospi, "source": "yf_batch", "as_of": as_of},
                "KOSDAQ": {"pct": kosdaq, "source": "yf_batch", "as_of": as_of},
            }
        }

    def test_blocks_kr_buy_during_first_fifteen_minutes(self):
        gate = tap._evaluate_kr_buy_safety(
            datetime(2026, 7, 16, 9, 3, tzinfo=KST),
            market_snapshot=self._market(),
        )

        self.assertIs(gate["ok"], False)
        self.assertEqual(gate["reason"], "kr_open_cooldown")

    def test_blocks_kr_buy_on_authoritative_local_index_crash(self):
        gate = tap._evaluate_kr_buy_safety(
            datetime(2026, 7, 16, 10, 30, tzinfo=KST),
            market_snapshot=self._market(kospi=-6.46, kosdaq=-4.09),
        )

        self.assertIs(gate["ok"], False)
        self.assertEqual(gate["reason"], "kr_market_shock")
        self.assertEqual(gate["trigger_index"], "KOSPI")
        self.assertEqual(gate["trigger_pct"], -6.46)

    def test_allows_kr_buy_after_cooldown_when_indices_are_healthy(self):
        gate = tap._evaluate_kr_buy_safety(
            datetime(2026, 7, 16, 10, 30, tzinfo=KST),
            market_snapshot=self._market(),
        )

        self.assertIs(gate["ok"], True)
        self.assertEqual(gate["reason"], "ok")

    def test_missing_market_snapshot_fails_closed(self):
        gate = tap._evaluate_kr_buy_safety(
            datetime(2026, 7, 16, 10, 30, tzinfo=KST),
            market_snapshot={},
        )

        self.assertIs(gate["ok"], False)
        self.assertEqual(gate["reason"], "kr_market_snapshot_unavailable")

    def test_daily_fallback_market_snapshot_fails_closed(self):
        snapshot = self._market()
        for row in snapshot["indices"].values():
            row["source"] = "yf_daily"

        gate = tap._evaluate_kr_buy_safety(
            datetime(2026, 7, 16, 10, 30, tzinfo=KST),
            market_snapshot=snapshot,
        )

        self.assertIs(gate["ok"], False)
        self.assertEqual(gate["reason"], "kr_market_snapshot_untrusted")

    def test_stale_market_snapshot_fails_closed(self):
        snapshot = self._market()
        stale_as_of = datetime(2026, 7, 16, 10, 0, tzinfo=KST).timestamp()
        for row in snapshot["indices"].values():
            row["as_of"] = stale_as_of

        gate = tap._evaluate_kr_buy_safety(
            datetime(2026, 7, 16, 10, 30, tzinfo=KST),
            market_snapshot=snapshot,
        )

        self.assertIs(gate["ok"], False)
        self.assertEqual(gate["reason"], "kr_market_snapshot_stale")

    def test_nonfinite_market_pct_fails_closed(self):
        for nonfinite in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(nonfinite=nonfinite):
                snapshot = self._market(kospi=nonfinite)
                gate = tap._evaluate_kr_buy_safety(
                    datetime(2026, 7, 16, 10, 30, tzinfo=KST),
                    market_snapshot=snapshot,
                )
                self.assertIs(gate["ok"], False)
                self.assertEqual(gate["reason"], "kr_market_snapshot_unavailable")

    def test_nonfinite_market_as_of_fails_closed(self):
        for nonfinite in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(nonfinite=nonfinite):
                snapshot = self._market()
                snapshot["indices"]["KOSPI"]["as_of"] = nonfinite
                gate = tap._evaluate_kr_buy_safety(
                    datetime(2026, 7, 16, 10, 30, tzinfo=KST),
                    market_snapshot=snapshot,
                )
                self.assertIs(gate["ok"], False)
                self.assertEqual(gate["reason"], "kr_market_snapshot_stale")


# ── 4-6. run_toss_autonomous_pipeline ────────────────────────────

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

    def test_cached_liveness_mismatch_stops_before_preview(self):
        feed = _candidate_feed([_candidate()])
        feed["scan_summary"].update({
            "income_liveness_status": "no_signal",
            "income_pass_count": 0,
            "income_ready_count": 0,
        })
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            with patch.object(
                tap, "_state_path", return_value=state_path
            ), patch.object(
                tap, "_active_execution_market", return_value="KR"
            ), patch(
                "core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                return_value=dict(_POLICY_ON),
            ), patch(
                "core.dashboard_data.toss_buy_candidates_data",
                return_value=feed,
            ), patch.object(
                tap,
                "retry_retryable_orders",
                return_value={"retried": 0, "sent": 0, "exhausted": 0},
            ), patch.object(
                tap, "process_candidate"
            ) as process, patch(
                "core.toss_live_pilot_preview.build_live_pilot_preview"
            ) as preview:
                result = tap.run_toss_autonomous_pipeline(now=_NOW, force=True)

        self.assertEqual(result["attempted"], 0)
        process.assert_not_called()
        preview.assert_not_called()

    def test_incomplete_or_truthy_policy_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(
                tmp,
                policy={
                    "autonomous_mode": 1,
                    "autonomous_kill_switch": False,
                },
            )
        self.assertEqual(result, {"skipped": "policy_contract_invalid"})

    def test_force_does_not_bypass_closed_market(self):
        with patch.object(tap, "_active_execution_market", return_value=None), patch.object(
            tap, "select_ready_candidates"
        ) as select, patch.object(tap, "retry_retryable_orders") as retry:
            result = tap.run_toss_autonomous_pipeline(now=_NOW, force=True)

        self.assertEqual(result, {"skipped": "market_closed"})
        select.assert_not_called()
        retry.assert_not_called()

    def test_unknown_active_market_fails_closed(self):
        with patch.object(tap, "_active_execution_market", return_value="JP"), patch.object(
            tap, "select_ready_candidates"
        ) as select, patch.object(tap, "retry_retryable_orders") as retry:
            result = tap.run_toss_autonomous_pipeline(now=_NOW, force=True)

        self.assertEqual(result, {"skipped": "invalid_active_market"})
        select.assert_not_called()
        retry.assert_not_called()

    def test_kr_market_safety_block_stops_before_candidate_processing(self):
        blocked = {
            "ok": False,
            "reason": "kr_market_shock",
            "trigger_index": "KOSPI",
            "trigger_pct": -6.46,
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            with patch.object(tap, "_state_path", return_value=state_path), patch.object(
                tap, "_active_execution_market", return_value="KR"
            ), patch(
                "core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                return_value=dict(_POLICY_ON),
            ), patch.object(
                tap, "_evaluate_kr_buy_safety", return_value=blocked
            ) as gate, patch.object(
                tap, "select_ready_candidates"
            ) as select, patch.object(
                tap, "retry_retryable_orders",
                return_value={"retried": 1, "sent": 1, "exhausted": 0},
            ) as retry, patch.object(tap, "process_candidate") as process:
                result = tap.run_toss_autonomous_pipeline(now=_NOW)

        self.assertEqual(result["attempted"], 0)
        self.assertEqual(result["buy_safety_gate"], blocked)
        gate.assert_called_once_with(_NOW)
        select.assert_not_called()
        process.assert_not_called()
        self.assertEqual(retry.call_args.kwargs["allowed_markets"], {"KR"})
        self.assertEqual(retry.call_args.kwargs["blocked_buy_markets"], {"KR"})

    def test_all_market_shock_filters_kr_and_keeps_us_candidates(self):
        blocked = {
            "ok": False,
            "reason": "kr_market_shock",
            "trigger_index": "KOSPI",
            "trigger_pct": -6.46,
        }
        us_candidate = _candidate("NVDA", market="US", currency="USD")
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            with patch.object(tap, "_state_path", return_value=state_path), patch.object(
                tap, "_active_execution_market", return_value="ALL"
            ), patch(
                "core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                return_value=dict(_POLICY_ON),
            ), patch.object(
                tap, "_evaluate_kr_buy_safety", return_value=blocked
            ), patch.object(
                tap, "select_ready_candidates", return_value=([us_candidate], [])
            ) as select, patch.object(
                tap, "retry_retryable_orders",
                return_value={"retried": 0, "sent": 0, "exhausted": 0},
            ) as retry, patch.object(
                tap, "process_candidate",
                return_value={"symbol": "NVDA", "stage": "verdict_recorded", "verdict": "PASS"},
            ):
                result = tap.run_toss_autonomous_pipeline(now=_NOW)

        self.assertEqual(select.call_args.kwargs["market"], "US")
        self.assertEqual(result["active_market"], "US")
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["buy_safety_gate"], blocked)
        self.assertEqual(retry.call_args.kwargs["allowed_markets"], {"KR", "US"})
        self.assertEqual(retry.call_args.kwargs["blocked_buy_markets"], {"KR"})

    def test_production_run_injects_bounded_shadow_enqueue_consumer(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            with patch.object(tap, "_state_path", return_value=state_path), \
                 patch("core.market_hours.is_kr_market_open", return_value=True), \
                 patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                       return_value=dict(_POLICY_ON)), \
                 patch.object(tap, "select_ready_candidates", return_value=([], [])) as select, \
                 patch.object(tap, "retry_retryable_orders",
                              return_value={"retried": 0, "sent": 0, "exhausted": 0}), \
                 patch("core.shadow_measurement_producer.enqueue_sanitized_final_candidate_cohort_with_market_observations") as enqueue:
                tap.run_toss_autonomous_pipeline(now=_NOW, force=True)

        self.assertIs(select.call_args.kwargs["cohort_consumer"], enqueue)
        self.assertEqual(select.call_args.kwargs["market"], "KR")

    def test_shadow_consumer_failure_preserves_pipeline_candidate_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            items = [
                _candidate(
                    "LOW.KS",
                    income_strategy={"income_pass": True, "expected_pnl_krw": 1_000},
                ),
                _candidate(
                    "HIGH.KS",
                    income_strategy={"income_pass": True, "expected_pnl_krw": 9_000},
                ),
                _candidate(
                    "MID.KS",
                    income_strategy={"income_pass": True, "expected_pnl_krw": 5_000},
                ),
            ]
            original = copy.deepcopy(items)
            processed = []

            def failing_consumer(cohort):
                from core.shadow_measurement_producer import (
                    SanitizedFinalCandidateCohort,
                )

                self.assertIs(type(cohort), SanitizedFinalCandidateCohort)
                self.assertIs(type(cohort.candidates), tuple)
                raise RuntimeError("consumer-order-secret")

            def process(candidate, _policy):
                processed.append(candidate["symbol"])
                return {"symbol": candidate["symbol"], "stage": "verdict_recorded"}

            with patch.object(tap, "_state_path", return_value=state_path), patch(
                "core.market_hours.is_kr_market_open", return_value=True
            ), patch(
                "core.market_hours.get_market_session",
                return_value={"kr": "KR_REGULAR", "us": "CLOSED"},
            ), patch(
                "core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                return_value=dict(_POLICY_ON),
            ), patch(
                "core.dashboard_data.toss_buy_candidates_data",
                return_value=_candidate_feed(items),
            ), patch.object(
                tap,
                "retry_retryable_orders",
                return_value={"retried": 0, "sent": 0, "exhausted": 0},
            ), patch.object(
                tap, "process_candidate", side_effect=process
            ), patch(
                "core.shadow_measurement_producer.enqueue_sanitized_final_candidate_cohort_with_market_observations",
                side_effect=failing_consumer,
            ):
                result = tap.run_toss_autonomous_pipeline(now=_NOW, force=True)

        self.assertEqual(result["attempted"], 3)
        self.assertEqual(processed, ["HIGH.KS", "MID.KS", "LOW.KS"])
        self.assertEqual(items, original)

    def test_shadow_producer_import_failure_preserves_pipeline_candidate_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            items = [
                _candidate(
                    "LOW.KS",
                    income_strategy={"income_pass": True, "expected_pnl_krw": 1_000},
                ),
                _candidate(
                    "HIGH.KS",
                    income_strategy={"income_pass": True, "expected_pnl_krw": 9_000},
                ),
                _candidate(
                    "MID.KS",
                    income_strategy={"income_pass": True, "expected_pnl_krw": 5_000},
                ),
            ]
            processed = []
            real_import = __import__

            def import_without_producer(name, *args, **kwargs):
                if name == "core.shadow_measurement_producer":
                    raise ImportError("producer-import-secret")
                return real_import(name, *args, **kwargs)

            def process(candidate, _policy):
                processed.append(candidate["symbol"])
                return {"symbol": candidate["symbol"], "stage": "verdict_recorded"}

            with self.assertLogs(tap.log, level=logging.WARNING) as captured_logs:
                with patch.object(
                    tap, "_state_path", return_value=state_path
                ), patch(
                    "core.market_hours.is_kr_market_open", return_value=True
                ), patch(
                    "core.market_hours.get_market_session",
                    return_value={"kr": "KR_REGULAR", "us": "CLOSED"},
                ), patch(
                    "core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                    return_value=dict(_POLICY_ON),
                ), patch(
                    "core.dashboard_data.toss_buy_candidates_data",
                    return_value=_candidate_feed(items),
                ), patch.object(
                    tap,
                    "retry_retryable_orders",
                    return_value={"retried": 0, "sent": 0, "exhausted": 0},
                ), patch.object(
                    tap, "process_candidate", side_effect=process
                ), patch(
                    "builtins.__import__", side_effect=import_without_producer
                ):
                    result = tap.run_toss_autonomous_pipeline(now=_NOW, force=True)

        self.assertEqual(result["attempted"], 3)
        self.assertEqual(processed, ["HIGH.KS", "MID.KS", "LOW.KS"])
        log_output = "\n".join(captured_logs.output)
        self.assertIn("ImportError", log_output)
        self.assertNotIn("producer-import-secret", log_output)

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
        with patch("core.dashboard_data.toss_buy_candidates_data", return_value=_candidate_feed(items)) as mock_feed:
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
        "decision_ref": "execution_decision:tlive_origin_1",
        "preview_id": "tlive_origin_1",
        "failure_reason": "network_error",
    }

    def _sweep(
        self, state, records=None, verdict_status="PASS", *, now=_NOW,
        finalizer_result=None, **retry_kwargs,
    ):
        retry_kwargs.setdefault("blocked_buy_markets", set())
        records = records if records is not None else [dict(self._RETRYABLE)]
        finalize_calls = []
        finalizer_result = finalizer_result or {"ok": True, "live_order_sent": True}

        def fake_finalize(pilot_id, allow_retry=False):
            finalize_calls.append((pilot_id, allow_retry))
            return dict(finalizer_result)

        failed_calls = []
        self.retry_preview = None

        def fake_create_verification(preview, pilot_id=""):
            self.retry_preview = dict(preview)
            return {"verification_id": "hv_retry_1"}

        with patch("core.toss_live_pilot_ledger.list_live_pilot_records",
                   return_value=records), \
             patch("core.toss_live_pilot_ledger.record_live_send_failed",
                   side_effect=lambda pid, failure_reason="": failed_calls.append((pid, failure_reason)) or {"ok": True}), \
             patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
                   return_value=dict(_POLICY_ON)), \
             patch("core.toss_live_pilot_verification.create_verification_request",
                   side_effect=fake_create_verification), \
             patch("core.toss_live_pilot_verification.record_hermes_verification",
                   return_value={"ok": True}), \
             patch("core.toss_live_pilot_hermes_bridge.build_default_hermes_verdict",
                   return_value={"status": verdict_status, "reasons": [], "checks": {}}), \
             patch("core.toss_autonomous_finalizer.try_autonomous_finalize",
                   side_effect=fake_finalize):
            r = tap.retry_retryable_orders(now=now, state=state, **retry_kwargs)
        return r, finalize_calls, failed_calls

    def test_retry_pass_finalizes_with_allow_retry(self):
        state = {}
        r, finalize_calls, _ = self._sweep(state)
        self.assertEqual(r["retried"], 1)
        self.assertEqual(r["sent"], 1)
        self.assertEqual(finalize_calls, [("tlive_retry_1", True)])
        self.assertEqual(state["retry_counts"]["tlive_retry_1"], 1)

    def test_retry_sent_counter_requires_exact_true_pair(self):
        result, calls, _ = self._sweep(
            {}, finalizer_result={"ok": True, "live_order_sent": "false"},
        )
        self.assertEqual(calls, [("tlive_retry_1", True)])
        self.assertEqual(result["sent"], 0)

    def test_us_retry_survives_kst_midnight_with_count_continuity(self):
        now = datetime(2026, 7, 3, 0, 20, tzinfo=KST)
        record = dict(
            self._RETRYABLE,
            pilot_id="us-sell",
            symbol="LRCX",
            side="sell",
            created_at="2026-07-02T23:50:00+09:00",
            decision_ref="execution_decision:exit.LRCX.20260702.full_exit",
        )
        state = {"retry_date": "2026-07-02", "retry_counts": {"us-sell": 1}}
        result, calls, _ = self._sweep(state, records=[record], now=now)
        self.assertEqual(calls, [("us-sell", True)])
        self.assertEqual(result["retried"], 1)
        self.assertEqual(state["retry_counts"]["us-sell"], 2)

    def test_reconcile_required_sell_does_not_exhaust_while_waiting_for_broker_truth(self):
        now = datetime(2026, 7, 3, 0, 20, tzinfo=KST)
        record = dict(
            self._RETRYABLE,
            pilot_id="us-reconcile",
            symbol="LRCX",
            side="sell",
            created_at="2026-07-02T23:50:00+09:00",
            decision_ref="execution_decision:exit.LRCX.20260702.full_exit",
            failure_reason="reconcile_required: transport_exception",
        )
        for reason in ("exit_intent_reserved", "exit_intent_reconcile_unavailable"):
            with self.subTest(reason=reason):
                state = {"retry_counts": {"us-reconcile": tap._MAX_RETRIES}}
                result, calls, failed = self._sweep(
                    state,
                    records=[record],
                    now=now,
                    finalizer_result={
                        "ok": False,
                        "live_order_sent": False,
                        "reason": reason,
                    },
                )
                self.assertEqual(calls, [("us-reconcile", True)])
                self.assertEqual(result["retried"], 1)
                self.assertEqual(result["exhausted"], 0)
                self.assertEqual(failed, [])
                self.assertEqual(
                    state["retry_counts"].get("us-reconcile", 0), 0,
                )

    def test_retry_invalid_policy_stops_before_ledger_fetch(self):
        bad_policy = {
            "autonomous_mode": 1,
            "autonomous_kill_switch": False,
        }
        with patch(
            "core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
            return_value=bad_policy,
        ), patch(
            "core.toss_live_pilot_ledger.list_live_pilot_records",
        ) as records:
            result = tap.retry_retryable_orders(now=_NOW, state={})

        self.assertEqual(result, {
            "retried": 0,
            "sent": 0,
            "exhausted": 0,
            "policy_blocked": "policy_contract_invalid",
        })
        records.assert_not_called()

    def test_retry_preserves_original_direct_attribution_keys(self):
        self._sweep({})
        preview = self.retry_preview
        self.assertIsNotNone(preview)
        assert preview is not None
        self.assertEqual(
            preview.get("decision_ref"),
            "execution_decision:tlive_origin_1",
        )
        self.assertEqual(preview.get("preview_id"), "tlive_origin_1")
        self.assertEqual(preview.get("symbol"), "091180.KS")
        self.assertEqual(preview.get("side"), "buy")

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

    def test_kr_shock_blocks_buy_retry_but_preserves_kr_sell_retry(self):
        records = [
            dict(self._RETRYABLE, pilot_id="kr_buy", side="buy"),
            dict(self._RETRYABLE, pilot_id="kr_sell", side="sell"),
            dict(self._RETRYABLE, pilot_id="us_buy", symbol="NVDA", side="buy"),
        ]

        result, finalize_calls, _ = self._sweep(
            {},
            records=records,
            allowed_markets={"KR"},
            blocked_buy_markets={"KR"},
        )

        self.assertEqual(finalize_calls, [("kr_sell", True)])
        self.assertEqual(result["retried"], 1)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["skipped_buy_safety"], 1)
        self.assertEqual(result["skipped_market"], 1)

    def test_default_retry_scope_is_currently_open_markets_only(self):
        records = [
            dict(self._RETRYABLE, pilot_id="kr", symbol="091180.KS", side="sell"),
            dict(self._RETRYABLE, pilot_id="us", symbol="LRCX", side="sell"),
        ]
        with patch.object(tap, "_active_execution_market", return_value="KR"):
            result, finalize_calls, _ = self._sweep({}, records=records)

        self.assertEqual(finalize_calls, [("kr", True)])
        self.assertEqual(result["retried"], 1)
        self.assertEqual(result["skipped_market"], 1)

    def test_default_retry_scope_blocks_buys_when_caller_omits_safety_scope(self):
        records = [
            dict(self._RETRYABLE, pilot_id="kr-buy", symbol="091180.KS", side="buy"),
            dict(self._RETRYABLE, pilot_id="kr-sell", symbol="091180.KS", side="sell"),
        ]
        with patch.object(tap, "_active_execution_market", return_value="KR"):
            result, finalize_calls, _ = self._sweep(
                {}, records=records, blocked_buy_markets=None,
            )

        self.assertEqual(finalize_calls, [("kr-sell", True)])
        self.assertEqual(result["retried"], 1)
        self.assertEqual(result["skipped_buy_safety"], 1)

    def test_retry_market_classifier_rejects_unknown_values(self):
        self.assertEqual(tap._retry_record_market({"market": "JP", "symbol": "AAPL"}), "")
        self.assertEqual(tap._retry_record_market({"symbol": "UNKNOWN"}), "")
        self.assertEqual(tap._retry_record_market({"symbol": "NVDA"}), "US")
        self.assertEqual(tap._retry_record_market({"symbol": "005930"}), "KR")

    def test_retry_sweep_skips_unknown_market_without_finalizing(self):
        records = [
            dict(self._RETRYABLE, pilot_id="jp", market="JP", symbol="AAPL"),
            dict(self._RETRYABLE, pilot_id="ambiguous", market="", symbol="UNKNOWN"),
        ]

        result, finalize_calls, _ = self._sweep({}, records=records)

        self.assertEqual(finalize_calls, [])
        self.assertEqual(result["retried"], 0)
        self.assertEqual(result["skipped_invalid"], 2)


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
              state=None, sent_return=True, outcome_error=None):
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
             patch("core.toss_quality_gate.evaluate_outcomes",
                   return_value={"evaluated": 2, "errors": 0},
                   side_effect=outcome_error) as outcomes, \
             patch("core.telegram.send_simple_message", side_effect=_fake_send):
            result = tap.send_daily_pipeline_report(now=now, force=force)
        result["_outcome_calls"] = outcomes.call_count
        return result, sent_messages, state_path

    def test_sends_after_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, msgs, _ = self._send(tmp)
            self.assertTrue(r["sent"])
            self.assertEqual(len(msgs), 1)
            self.assertIn("자본 가동률: 70.0%", msgs[0])
            self.assertEqual(r["_outcome_calls"], 1)
            self.assertEqual(r["outcomes"], {"evaluated": 2, "errors": 0})
            self.assertIn("5일 outcome 평가: 2건", msgs[0])

    def test_outcome_evaluation_failure_still_sends_visible_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, msgs, _ = self._send(tmp, outcome_error=RuntimeError("synthetic"))
            self.assertTrue(r["sent"])
            self.assertEqual(r["outcomes"]["evaluated"], 0)
            self.assertEqual(r["outcomes"]["errors"], 1)
            self.assertIn("5일 outcome 평가: 0건 / 오류 1건", msgs[0])

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
        _candidate(
            "B.KS",
            stock_agent_ready=False,
            income_strategy={
                "income_pass": False,
                "income_block_reason": "expected_pnl_below_threshold",
            },
        ),
    ]
    with patch("core.dashboard_data.toss_buy_candidates_data", return_value=_candidate_feed(items)):
        ready, not_ready = tap.select_ready_candidates()
    assert [r["symbol"] for r in ready] == ["A.KS"]
    assert not_ready == [{"symbol": "B.KS", "reason": "expected_pnl_below_threshold"}]


def test_select_ready_candidates_sorts_by_decision_expected_income():
    items = [
        _candidate("NEXT_HIGH.KS", income_strategy={
            "income_pass": True, "expected_pnl_krw": 18_000,
            "decision_expected_pnl_krw": 1_000, "decision_income_edge_ratio": 0.001,
        }),
        _candidate("DECISION_HIGH.KS", income_strategy={
            "income_pass": True, "expected_pnl_krw": -5_000,
            "decision_expected_pnl_krw": 9_000, "decision_income_edge_ratio": 0.009,
        }),
    ]
    with patch("core.dashboard_data.toss_buy_candidates_data", return_value=_candidate_feed(items)):
        ready, _ = tap.select_ready_candidates()
    assert [r["symbol"] for r in ready] == ["DECISION_HIGH.KS", "NEXT_HIGH.KS"]


if __name__ == "__main__":
    unittest.main()


class TestExactBoolReadiness(unittest.TestCase):
    """실행 진입 게이트는 exact bool만 신뢰한다 — 문자열 'false'/'true' 거부."""

    def _select(self, items):
        with patch("core.dashboard_data.toss_buy_candidates_data",
                   return_value=_candidate_feed(items)):
            ready, not_ready = tap.select_ready_candidates()
        return ready, not_ready

    def test_string_false_ready_is_rejected(self):
        ready, not_ready = self._select([_candidate("A.KS", stock_agent_ready="false")])
        self.assertEqual(ready, [])

    def test_string_true_ready_is_rejected(self):
        # 문자열 "true"도 신뢰하지 않는다 — 직렬화 경로 오염 신호
        ready, _ = self._select([_candidate("A.KS", stock_agent_ready="true")])
        self.assertEqual(ready, [])

    def test_int_one_ready_is_rejected(self):
        ready, _ = self._select([_candidate("A.KS", stock_agent_ready=1)])
        self.assertEqual(ready, [])

    def test_string_income_pass_is_rejected(self):
        item = _candidate("A.KS")
        item["income_strategy"] = {"income_pass": "false"}
        ready, _ = self._select([item])
        self.assertEqual(ready, [])

    def test_exact_true_still_ready(self):
        ready, _ = self._select([_candidate("A.KS", stock_agent_ready=True)])
        self.assertEqual([r["symbol"] for r in ready], ["A.KS"])

    def test_direct_buy_processor_rejects_string_income_before_preview(self):
        item = _candidate("A.KS")
        item["income_strategy"] = {"income_pass": "false"}
        with patch(
            "core.ai_berkshire_toss.evaluate_ai_berkshire_buy_gate",
            return_value={"buy_block": False, "buy_reason": "ok"},
        ), patch(
            "core.toss_live_pilot_preview.build_live_pilot_preview",
            return_value={"ok": False, "block_summary": "should_not_reach"},
        ) as preview:
            result = tap.process_candidate(item, dict(_POLICY_ON))

        self.assertEqual(result["stage"], "income_gate_blocked")
        preview.assert_not_called()
