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
    "allowed_asset_types": ["US_STOCK", "KR_STOCK"],
    "autonomous_allowed_asset_types": ["US_STOCK", "KR_STOCK"],
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


def _install_authoritative_dispatch(monkeypatch, payload=None):
    order = deepcopy(payload or PAYLOAD)
    record = {
        "pilot_id": order["pilot_id"],
        "symbol": order["symbol"],
        "side": order["side"],
        "quantity": order["quantity"],
        "limit_price": order["limit_price"],
        "status": "verified",
    }
    monkeypatch.setattr(
        adapter,
        "_load_authoritative_dispatch_record",
        lambda pilot_id: deepcopy(record) if pilot_id == record["pilot_id"] else None,
        raising=False,
    )


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


def test_autonomous_dispatch_accepts_complete_exact_contract_with_fake_transport(monkeypatch):
    _install_authoritative_dispatch(monkeypatch)
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


def test_autonomous_dispatch_rejects_format_valid_forged_id_before_transport(monkeypatch):
    monkeypatch.setattr(
        adapter, "_load_authoritative_dispatch_record", lambda pilot_id: None,
        raising=False,
    )
    calls = []

    result = adapter.dispatch_toss_order_live(
        deepcopy(PAYLOAD),
        dict(POLICY),
        transport=lambda order, policy: calls.append((order, policy)) or {
            "ok": True, "live_order_sent": True, "broker_confirmed": True,
        },
    )

    assert calls == []
    assert result["ok"] is False
    assert result["reason"] == "dispatch_contract_invalid"


@pytest.mark.parametrize(
    "policy_patch,payload_patch",
    [
        ({"allowed_asset_types": None}, {}),
        ({}, {"symbol": "NOT/A/TICKER"}),
        ({}, {"symbol": "12345.KS"}),
    ],
)
def test_autonomous_dispatch_rejects_missing_scope_or_unsupported_symbol(
    monkeypatch, policy_patch, payload_patch,
):
    payload = {**PAYLOAD, **payload_patch}
    _install_authoritative_dispatch(monkeypatch, payload)
    policy = {**POLICY, **policy_patch}
    if policy_patch.get("allowed_asset_types", object()) is None:
        policy.pop("allowed_asset_types", None)
    calls = []

    result = adapter.dispatch_toss_order_live(
        payload,
        policy,
        transport=lambda order, live_policy: calls.append((order, live_policy)) or {
            "ok": True, "live_order_sent": True, "broker_confirmed": True,
        },
    )

    assert calls == []
    assert result["reason"] == "dispatch_contract_invalid"


def test_autonomous_dispatch_normalizes_symbol_before_transport(monkeypatch):
    payload = {**PAYLOAD, "symbol": " aapl "}
    _install_authoritative_dispatch(monkeypatch, {**payload, "symbol": "AAPL"})
    calls = []

    result = adapter.dispatch_toss_order_live(
        payload,
        {**POLICY, "allowed_asset_types": ["US_STOCK"]},
        transport=lambda order, policy: calls.append(order) or {
            "ok": True, "live_order_sent": True, "broker_confirmed": True,
        },
    )

    assert result["ok"] is True
    assert calls[0]["symbol"] == "AAPL"


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


def test_quality_db_schema_persists_full_bucket_context(tmp_path, monkeypatch):
    _temp_quality_db(monkeypatch, tmp_path, "context-schema.db")
    conn = qg._outcomes_conn()
    columns = {
        str(row[1]) for row in conn.execute(
            "PRAGMA table_info(quality_gate_decisions)"
        ).fetchall()
    }
    conn.close()

    assert {
        "decision_change_pct",
        "decision_days_to_earnings",
        "decision_has_stop",
        "decision_has_target",
        "decision_blocking_risk_flags",
        "decision_origin_bucket",
        "decision_origin_reason",
    } <= columns


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


def test_missing_side_at_scoring_cannot_be_laundered_into_buy_proof():
    candidate = _raw_quality_candidate()
    candidate.pop("side")
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
    candidate["side"] = "buy"

    assert qg.attach_quality_proof(candidate) is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("change_pct", 9.0),
        ("blocking_risk_flags", ["late_block"]),
    ],
)
def test_record_rejects_bucket_context_mutation_after_scoring(
    tmp_path, monkeypatch, field, value,
):
    _temp_quality_db(monkeypatch, tmp_path, f"context-{field}.db")
    candidate = _scored_quality_candidate()
    candidate[field] = value

    result = qg.record_execution_quality_decision(
        candidate,
        pilot_id="tlive_context_0001",
        decision_ref=f"execution_decision:context_{field}",
    )

    assert result["ok"] is False
    assert result["reason"] in {
        "quality_proof_context_mismatch",
        "quality_proof_breakdown_mismatch",
    }


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


def test_validate_rejects_self_consistent_bucket_context_rewrite(tmp_path, monkeypatch):
    _temp_quality_db(monkeypatch, tmp_path, "context-rewrite.db")
    candidate = _scored_quality_candidate()
    pilot_id = "tlive_context_rewrite_0001"
    decision_ref = "execution_decision:context_rewrite_0001"
    assert qg.record_execution_quality_decision(
        candidate, pilot_id=pilot_id, decision_ref=decision_ref,
    )["ok"] is True

    conn = qg._outcomes_conn()
    row = conn.execute(
        "SELECT * FROM quality_gate_decisions WHERE decision_ref=?",
        (decision_ref,),
    ).fetchone()
    tampered = dict(row)
    tampered["decision_change_pct"] = 9.0
    tampered_hash = qg._score_breakdown_hash(
        tampered,
        schema_version=qg.QUALITY_SCORE_SCHEMA_VERSION,
        weight_hash=tampered["weight_profile_hash"],
    )
    assert tampered_hash
    conn.execute(
        "UPDATE quality_gate_decisions "
        "SET decision_change_pct=?, score_breakdown_sha256=? WHERE decision_ref=?",
        (9.0, tampered_hash, decision_ref),
    )
    conn.commit()
    conn.close()

    result = qg.validate_execution_quality_decision(
        _quality_rec(candidate, pilot_id, decision_ref), pilot_id=pilot_id,
    )
    assert result == {
        "ok": False,
        "reason": "quality_decision_bucket_replay_mismatch",
    }


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


def test_finalizer_rejects_non_executable_bucket_context_mutation():
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

    assert qg.finalize_quality_proof(candidate) is False
    assert candidate["decision_bucket"] == qg.BLOCK
    assert "candidate_snapshot_sha256" not in candidate["quality_breakdown"]