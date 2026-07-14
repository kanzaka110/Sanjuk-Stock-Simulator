from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from core import toss_live_pilot_adapter as adapter
from core import toss_quality_gate as qg


POLICY = {
    "live_pilot_enabled": True,
    "live_order_allowed": True,
    "autonomous_mode": True,
    "autonomous_kill_switch": False,
    "adapter_status": "enabled",
    "allowed_sides": ["buy"],
    "autonomous_allowed_sides": ["buy"],
    "blocked_symbols": ["MU"],
}

PAYLOAD = {
    "symbol": "AAPL",
    "side": "buy",
    "order_type": "limit",
    "quantity": 1,
    "limit_price": 100.0,
    "estimated_amount_krw": 100.0,
    "client_order_id": "tlive_20260714_160000_0001",
    "pilot_id": "tlive_20260714_160000_0001",
}


def _remove(*keys):
    def mutate(payload):
        for key in keys:
            payload.pop(key, None)
    return mutate


@pytest.mark.parametrize(
    "case,mutate",
    [
        ("missing_side", _remove("side")),
        ("invalid_side", lambda p: p.__setitem__("side", "hold")),
        ("blocked_symbol", lambda p: p.__setitem__("symbol", "MU")),
        ("missing_order_ids", _remove("client_order_id", "pilot_id")),
        ("quantity_bool", lambda p: p.__setitem__("quantity", True)),
        ("quantity_string", lambda p: p.__setitem__("quantity", "1")),
        ("quantity_fraction", lambda p: p.__setitem__("quantity", 1.5)),
        ("quantity_nan", lambda p: p.__setitem__("quantity", float("nan"))),
        ("price_infinity", lambda p: p.__setitem__("limit_price", float("inf"))),
    ],
)
def test_autonomous_dispatch_rejects_malformed_contract_before_transport(case, mutate):
    payload = deepcopy(PAYLOAD)
    mutate(payload)
    calls = []

    def fake_transport(order, policy):
        calls.append((order, policy))
        return {"ok": True, "live_order_sent": True, "broker_confirmed": True}

    result = adapter.dispatch_toss_order_live(payload, dict(POLICY), transport=fake_transport)

    assert calls == [], case
    assert result["ok"] is False
    assert result["live_order_sent"] is False
    assert result["reason"] == "dispatch_contract_invalid"


def test_autonomous_dispatch_accepts_complete_exact_contract_with_fake_transport():
    calls = []

    def fake_transport(order, policy):
        calls.append((order, policy))
        return {"ok": True, "live_order_sent": True, "broker_confirmed": True}

    result = adapter.dispatch_toss_order_live(
        deepcopy(PAYLOAD), dict(POLICY), transport=fake_transport,
    )

    assert len(calls) == 1
    assert result["ok"] is True
    assert result["live_order_sent"] is True


@pytest.mark.parametrize(
    "policy_patch,payload_patch",
    [
        ({"blocked_symbols": "MU"}, {"symbol": "MU"}),
        ({"allowed_asset_types": ["KR_STOCK"]}, {"symbol": "AAPL"}),
    ],
)
def test_autonomous_dispatch_rejects_invalid_policy_container_or_asset_scope(
    policy_patch, payload_patch,
):
    policy = {**POLICY, **policy_patch}
    payload = {**PAYLOAD, **payload_patch}
    calls = []

    def fake_transport(order, live_policy):
        calls.append((order, live_policy))
        return {"ok": True, "live_order_sent": True, "broker_confirmed": True}

    result = adapter.dispatch_toss_order_live(payload, policy, transport=fake_transport)

    assert calls == []
    assert result["reason"] == "dispatch_contract_invalid"


def _raw_quality_candidate():
    return {
        "symbol": "AAPL",
        "side": "buy",
        "score": 88.0,
        "market": "US",
        "volume_value": 10_000_000_000.0,
        "risk_reward": 2.0,
        "change_pct": 0.0,
        "quantity": 1,
        "limit_price": 100.0,
        "stop_loss": 90.0,
        "target_price": 120.0,
        "risk_flags": [],
        "blocking_risk_flags": [],
    }


def _scored_quality_candidate():
    candidate = _raw_quality_candidate()
    score = qg.score_candidate(
        candidate,
        regime_obj=None,
        accuracy_stats={},
        expensive_checks=False,
        fetch_budget={"remaining": 0},
    )
    candidate["quality_score"] = score.score_total
    candidate["quality_breakdown"] = score.to_dict()
    candidate["decision_bucket"] = score.decision_bucket
    candidate["decision_reason"] = score.decision_reason
    assert qg.attach_quality_proof(candidate) is True
    return candidate


def _quality_rec(candidate, pilot_id, decision_ref):
    return {
        "side": "buy",
        "pilot_id": pilot_id,
        "decision_ref": decision_ref,
        "symbol": candidate["symbol"],
        "quantity": candidate["quantity"],
        "limit_price": candidate["limit_price"],
        "stop_loss": candidate["stop_loss"],
        "target_price": candidate["target_price"],
    }


def _temp_quality_db(monkeypatch, tmp_path: Path, name: str):
    monkeypatch.setattr(qg, "_outcomes_db_path", lambda: tmp_path / name)
    qg._outcomes_schema_created = False


def test_public_execution_binder_does_not_mint_proof_for_manual_breakdown():
    candidate = _raw_quality_candidate()
    candidate["quality_breakdown"] = {
        "score_total": 45.0,
        "score_momentum": 25.0,
        "score_liquidity": 5.0,
        "score_risk_reward": 5.0,
        "score_reliability": 5.0,
        "score_market_regime": 5.0,
        "score_supply_demand": 0.0,
        "penalty_overheat": 0.0,
        "penalty_duplicate": 0.0,
        "penalty_event_risk": 0.0,
        "rr_ratio": 2.0,
        "regime": "synthetic",
        "decision_bucket": "PASS_EXECUTE",
        "decision_reason": "synthetic",
    }
    candidate["decision_bucket"] = "PASS_EXECUTE"

    assert qg.attach_quality_proof(candidate) is False
    assert "score_breakdown_sha256" not in candidate["quality_breakdown"]
    assert "candidate_snapshot_sha256" not in candidate["quality_breakdown"]


def test_post_proof_breakdown_mutation_is_rejected(tmp_path, monkeypatch):
    _temp_quality_db(monkeypatch, tmp_path, "mutation.db")
    candidate = _scored_quality_candidate()
    candidate["quality_breakdown"]["score_momentum"] -= 1.0
    candidate["quality_breakdown"]["score_liquidity"] += 1.0

    result = qg.record_execution_quality_decision(
        candidate,
        pilot_id="tlive_20260714_160001_0001",
        decision_ref="execution_decision:hermes_mutation_0001",
    )

    assert result == {"ok": False, "reason": "quality_proof_breakdown_mismatch"}


def test_weight_profile_change_between_score_and_bind_is_rejected(monkeypatch):
    weights_a = {key: 1.0 for key in qg._DEFAULT_WEIGHTS}
    weights_b = {key: 1.5 for key in qg._DEFAULT_WEIGHTS}
    calls = {"count": 0}

    def rotating_weights():
        calls["count"] += 1
        return dict(weights_a if calls["count"] == 1 else weights_b)

    monkeypatch.setattr(qg, "get_score_weights", rotating_weights)
    candidate = _raw_quality_candidate()
    score = qg.score_candidate(
        candidate, regime_obj=None, accuracy_stats={}, expensive_checks=False,
        fetch_budget={"remaining": 0},
    )
    candidate["quality_score"] = score.score_total
    candidate["quality_breakdown"] = score.to_dict()
    candidate["decision_bucket"] = score.decision_bucket
    candidate["decision_reason"] = score.decision_reason

    assert qg.attach_quality_proof(candidate) is False
    assert "candidate_snapshot_sha256" not in candidate["quality_breakdown"]


def test_final_stop_change_rescores_rr_and_record_validate_pass(tmp_path, monkeypatch):
    _temp_quality_db(monkeypatch, tmp_path, "finalized.db")
    candidate = _scored_quality_candidate()
    old_rr_score = candidate["quality_breakdown"]["score_risk_reward"]
    candidate["stop_loss"] = 95.0

    assert qg.finalize_quality_proof(candidate) is True
    assert candidate["risk_reward"] == pytest.approx(4.0)
    assert candidate["quality_breakdown"]["rr_ratio"] == pytest.approx(4.0)
    assert candidate["quality_breakdown"]["score_risk_reward"] > old_rr_score

    pilot_id = "tlive_20260714_160002_0001"
    decision_ref = "execution_decision:hermes_finalized_0001"
    recorded = qg.record_execution_quality_decision(
        candidate, pilot_id=pilot_id, decision_ref=decision_ref,
    )
    validated = qg.validate_execution_quality_decision(
        _quality_rec(candidate, pilot_id, decision_ref), pilot_id=pilot_id,
    )

    assert recorded["ok"] is True
    assert validated["ok"] is True


def test_fractional_score_schema_version_is_rejected_at_validate(tmp_path, monkeypatch):
    _temp_quality_db(monkeypatch, tmp_path, "version.db")
    candidate = _scored_quality_candidate()
    pilot_id = "tlive_20260714_160003_0001"
    decision_ref = "execution_decision:hermes_version_0001"
    assert qg.record_execution_quality_decision(
        candidate, pilot_id=pilot_id, decision_ref=decision_ref,
    )["ok"] is True

    conn = qg._outcomes_conn()
    conn.execute(
        "UPDATE quality_gate_decisions SET score_schema_version=2.5 WHERE decision_ref=?",
        (decision_ref,),
    )
    conn.commit()
    conn.close()

    result = qg.validate_execution_quality_decision(
        _quality_rec(candidate, pilot_id, decision_ref), pilot_id=pilot_id,
    )
    assert result["ok"] is False
    assert result["reason"] == "quality_decision_proof_missing"


@pytest.mark.parametrize(
    "field,value",
    [("symbol", "MSFT"), ("side", "sell")],
)
def test_finalizer_rejects_score_identity_change(field, value):
    candidate = _scored_quality_candidate()
    candidate[field] = value

    assert qg.finalize_quality_proof(candidate) is False
    assert "candidate_snapshot_sha256" not in candidate["quality_breakdown"]


def test_finalizer_never_promotes_existing_non_executable_bucket():
    candidate = _raw_quality_candidate()
    candidate["blocking_risk_flags"] = ["deterministic_block"]
    score = qg.score_candidate(
        candidate, regime_obj=None, accuracy_stats={}, expensive_checks=False,
        fetch_budget={"remaining": 0},
    )
    candidate["quality_score"] = score.score_total
    candidate["quality_breakdown"] = score.to_dict()
    candidate["decision_bucket"] = score.decision_bucket
    candidate["decision_reason"] = score.decision_reason
    assert candidate["decision_bucket"] == qg.BLOCK
    assert qg.attach_quality_proof(candidate) is True

    candidate["blocking_risk_flags"] = []
    candidate["stop_loss"] = 95.0

    assert qg.finalize_quality_proof(candidate) is True
    assert candidate["decision_bucket"] == qg.BLOCK
