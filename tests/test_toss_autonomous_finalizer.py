"""tests/test_toss_autonomous_finalizer.py

Autonomous finalizer 핵심 플로우 테스트.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


KST = timezone(timedelta(hours=9))


def _seal_quality_proof_for_test(qg, candidate):
    candidate.setdefault("side", "buy")
    breakdown = candidate["quality_breakdown"]
    breakdown["decision_bucket"] = candidate.get("decision_bucket", "")
    breakdown["decision_reason"] = candidate.get("decision_reason", "")
    breakdown["score_symbol"] = str(candidate.get("symbol") or candidate.get("ticker") or "").upper()
    breakdown["score_side"] = str(candidate.get("side") or "buy").lower()
    event_penalty = float(breakdown.get("penalty_event_risk") or 0.0)
    breakdown.update({
        "decision_change_pct": float(candidate.get("change_pct") or 0.0),
        "decision_days_to_earnings": 0 if event_penalty == -15.0 else (5 if event_penalty == -5.0 else -1),
        "decision_has_stop": bool(candidate.get("stop_loss")),
        "decision_has_target": bool(candidate.get("target_price")),
        "decision_blocking_risk_flags": list(candidate.get("blocking_risk_flags") or []),
        "decision_origin_bucket": breakdown["decision_bucket"],
        "decision_origin_reason": breakdown["decision_reason"],
    })
    breakdown["score_schema_version"] = qg.QUALITY_SCORE_SCHEMA_VERSION
    weight_hash = qg._weight_profile_hash()
    breakdown["weight_profile_hash"] = weight_hash
    breakdown["score_breakdown_sha256"] = qg._score_breakdown_hash(
        breakdown, schema_version=qg.QUALITY_SCORE_SCHEMA_VERSION,
        weight_hash=weight_hash,
    )
    assert breakdown["score_breakdown_sha256"]
    assert qg.attach_quality_proof(candidate) is True


_AUTO_ENV = {
    "TOSS_LIVE_PILOT_ENABLED": "true",
    "TOSS_LIVE_ORDER_ALLOWED": "true",
    "TOSS_LIVE_ADAPTER_ENABLED": "true",
    "TOSS_AUTONOMOUS_MODE": "true",
}

_PILOT_REC = {
    "pilot_id": "tlive_test_pilot_001",
    "decision_ref": "execution_decision:tlive_test_001",
    "symbol": "NVDA",
    "side": "buy",
    "quantity": 1,
    "limit_price": 190.0,
    "estimated_amount_krw": 280_000,
    "status": "previewed",
    "blocks": [],
    "live_order_sent": False,
    "stop_loss": 180.0,
    "target_price": 210.0,
    "invalidation": "below $180",
}


def test_sell_with_invalid_exit_ref_blocks_before_transport(monkeypatch, tmp_path):
    rec = {
        **_PILOT_REC,
        "pilot_id": "sell-invalid-ref",
        "decision_ref": "",
        "side": "sell",
    }
    monkeypatch.setenv("TOSS_EXIT_INTENT_STATE_PATH", str(tmp_path / "intents.json"))
    with patch.dict(os.environ, _AUTO_ENV, clear=False), \
         patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=[rec]), \
         patch("core.toss_live_pilot_verification.is_verification_passed", _mock_verif_pass), \
         patch("core.toss_live_pilot_adapter.can_send_live_pilot_order", return_value=(True, [])), \
         patch("core.toss_live_pilot_telegram.resolve_live_transport_for_confirm") as resolver, \
         patch("core.toss_live_pilot_adapter.dispatch_toss_order_live") as dispatch, \
         patch("core.toss_autonomous_finalizer._record_event"):
        from core.toss_autonomous_finalizer import try_autonomous_finalize
        result = try_autonomous_finalize(rec["pilot_id"])

    assert result["ok"] is False
    assert result["reason"] == "invalid_exit_decision_ref"
    resolver.assert_not_called()
    dispatch.assert_not_called()


def test_sell_ref_bound_to_other_symbol_blocks_before_transport(monkeypatch, tmp_path):
    from core import toss_exit_execution_intent as intent

    rec = {
        **_PILOT_REC,
        "pilot_id": "sell-wrong-symbol-ref",
        "decision_ref": intent.build_exit_decision_ref(
            "MU", "full_exit", datetime.now(KST),
        ),
        "symbol": "LRCX",
        "side": "sell",
    }
    monkeypatch.setenv("TOSS_EXIT_INTENT_STATE_PATH", str(tmp_path / "intents.json"))
    with patch.dict(os.environ, _AUTO_ENV, clear=False), \
         patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=[rec]), \
         patch("core.toss_live_pilot_verification.is_verification_passed", _mock_verif_pass), \
         patch("core.toss_live_pilot_adapter.can_send_live_pilot_order", return_value=(True, [])), \
         patch("core.toss_live_pilot_telegram.resolve_live_transport_for_confirm") as resolver, \
         patch("core.toss_live_pilot_adapter.dispatch_toss_order_live") as dispatch, \
         patch("core.toss_autonomous_finalizer._record_event"):
        from core.toss_autonomous_finalizer import try_autonomous_finalize
        result = try_autonomous_finalize(rec["pilot_id"])

    assert result["ok"] is False
    assert result["reason"] == "invalid_exit_decision_ref"
    resolver.assert_not_called()
    dispatch.assert_not_called()


def test_transport_resolution_failure_releases_unattempted_sell_claim(
    monkeypatch, tmp_path,
):
    from core import toss_exit_execution_intent as intent

    decision_ref = intent.build_exit_decision_ref(
        "LRCX", "full_exit", datetime.now(KST),
    )
    rec = {
        **_PILOT_REC,
        "pilot_id": "sell-resolver-fail",
        "decision_ref": decision_ref,
        "symbol": "LRCX",
        "side": "sell",
    }
    monkeypatch.setenv("TOSS_EXIT_INTENT_STATE_PATH", str(tmp_path / "intents.json"))
    with patch.dict(os.environ, _AUTO_ENV, clear=False), \
         patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=[rec]), \
         patch("core.toss_live_pilot_verification.is_verification_passed", _mock_verif_pass), \
         patch("core.toss_live_pilot_adapter.can_send_live_pilot_order", return_value=(True, [])), \
         patch("core.toss_live_pilot_telegram.resolve_live_transport_for_confirm",
               side_effect=RuntimeError("synthetic")), \
         patch("core.toss_live_pilot_adapter.dispatch_toss_order_live") as dispatch, \
         patch("core.toss_live_pilot_ledger.record_live_send_failed") as record_failed, \
         patch("core.toss_live_pilot_ledger.record_live_send_retryable") as record_retryable, \
         patch("core.toss_autonomous_finalizer._record_event"):
        from core.toss_autonomous_finalizer import try_autonomous_finalize
        result = try_autonomous_finalize(rec["pilot_id"])

    assert result["ok"] is False
    assert result["reason"] == "transport_resolution_failed"
    dispatch.assert_not_called()
    record_failed.assert_not_called()
    record_retryable.assert_called_once()
    assert record_retryable.call_args.kwargs["failure_reason"].startswith(
        "transport_resolution_failed"
    )
    reclaimed = intent.claim_exit_intent(decision_ref, "pilot-b")
    assert reclaimed["ok"] is True
    assert reclaimed["reason"] == "exit_intent_claimed"


def _mock_verif_pass(pilot_id, now=None):
    """Hermes PASS 반환 mock."""
    return (True, [], {"verification_id": "hv_mock", "status": "PASS"})


def _mock_verif_hold(pilot_id, now=None):
    return (False, ["hermes_verification_hold"], {"verification_id": "hv_mock", "status": "HOLD"})


def _mock_verif_pending(pilot_id, now=None):
    return (False, ["hermes_verification_pending"], {"verification_id": "hv_mock", "status": "PENDING"})


def _mock_ledger_records(limit=200):
    return [_PILOT_REC]


def _mock_transport_success(payload, policy):
    return {
        "ok": True,
        "live_order_sent": True,
        "broker_order_id": "mock_order_123",
        "broker_confirmed": True,
        "broker_order_status": "FILLED",
        "filled_quantity": 1.0,
        "filled_price": 190.0,
    }


def _mock_transport_fail(payload, policy):
    return {
        "ok": False,
        "live_order_sent": False,
        "reason": "transport_test_fail",
    }


def _mock_transport_http_422(payload, policy):
    return {
        "ok": False,
        "live_order_sent": False,
        "reason": "http_422",
        "error_body": '{"code":"INVALID_PRICE"}',
        "order_request_preview": {"symbol": "000270", "side": "BUY"},
    }


class TestAutonomousDisabled:
    """autonomous_mode=false → no-op."""

    def test_skipped_when_disabled(self):
        with patch.dict(os.environ, {**_AUTO_ENV, "TOSS_AUTONOMOUS_MODE": "false"}, clear=False):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize("tlive_test_pilot_001")
        assert result["ok"] is False
        assert result.get("skipped") is True
        assert result["reason"] == "autonomous_mode_disabled"


class TestKillSwitch:
    """kill_switch=true → 차단."""

    def test_blocked_by_kill_switch(self):
        env = {**_AUTO_ENV, "TOSS_AUTONOMOUS_KILL_SWITCH": "true"}
        with patch.dict(os.environ, env, clear=False):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize("tlive_test_pilot_001")
        assert result["ok"] is False
        assert result["reason"] == "autonomous_kill_switch_active"


class TestHermesGate:
    """Hermes PASS가 없으면 차단."""

    @patch("core.toss_live_pilot_verification.is_verification_passed", _mock_verif_hold)
    @patch("core.toss_live_pilot_ledger.list_live_pilot_records", _mock_ledger_records)
    def test_blocked_by_hermes_hold(self):
        with patch.dict(os.environ, _AUTO_ENV, clear=False):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize("tlive_test_pilot_001")
        assert result["ok"] is False
        assert result["reason"] == "hermes_verification_required"

    @patch("core.toss_live_pilot_verification.is_verification_passed", _mock_verif_pending)
    @patch("core.toss_live_pilot_ledger.list_live_pilot_records", _mock_ledger_records)
    def test_blocked_by_hermes_pending(self):
        with patch.dict(os.environ, _AUTO_ENV, clear=False):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize("tlive_test_pilot_001")
        assert result["ok"] is False
        assert result["reason"] == "hermes_verification_required"


class TestPilotNotFound:
    """pilot_id 없으면 차단."""

    @patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=[])
    def test_not_found(self, mock_ledger):
        with patch.dict(os.environ, _AUTO_ENV, clear=False):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize("nonexistent")
        assert result["ok"] is False
        assert result["reason"] == "pilot_id_not_found"


class TestAlreadyProcessed:
    """이미 live_sent/cancelled → 스킵."""

    def test_already_sent(self):
        sent_rec = {**_PILOT_REC, "status": "live_sent"}
        with patch.dict(os.environ, _AUTO_ENV, clear=False):
            with patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=[sent_rec]):
                from core.toss_autonomous_finalizer import try_autonomous_finalize
                result = try_autonomous_finalize("tlive_test_pilot_001")
        assert result["ok"] is False
        assert result.get("skipped") is True
        assert "already_processed" in result["reason"]


class TestSuccessfulExecution:
    """PASS + 모든 가드 통과 → 주문 전송."""

    @patch("core.toss_live_pilot_verification.is_verification_passed", _mock_verif_pass)
    @patch("core.toss_live_pilot_ledger.list_live_pilot_records", _mock_ledger_records)
    @patch("core.toss_live_pilot_ledger.record_live_sent")
    @patch("core.toss_live_pilot_telegram.resolve_live_transport_for_confirm")
    @patch("core.toss_live_pilot_telegram.send_autonomous_result_message", return_value=True)
    def test_pass_triggers_order(self, mock_tg, mock_transport, mock_ledger):
        mock_transport.return_value = _mock_transport_success
        with patch.dict(os.environ, _AUTO_ENV, clear=False), \
             patch("core.toss_quality_gate.validate_execution_quality_decision",
                   return_value={"ok": True, "reason": "quality_decision_exact"}):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize("tlive_test_pilot_001")
        assert result["ok"] is True
        assert result["live_order_sent"] is True
        assert result["broker_order_id"] == "mock_order_123"
        mock_ledger.assert_called_once()


class TestExactQualityLastMile:
    """BUY는 dispatch 직전 exact quality row가 없으면 차단."""

    def test_missing_quality_row_blocks_before_dispatch(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg

        pilot_id = "tlive_20260713_missing_qg"
        rec = {**_PILOT_REC, "pilot_id": pilot_id}
        monkeypatch.setattr(qg, "_outcomes_db_path", lambda: tmp_path / "missing_quality.db")
        qg._outcomes_schema_created = False

        with patch.dict(os.environ, _AUTO_ENV, clear=False), \
             patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=[rec]), \
             patch("core.toss_live_pilot_verification.is_verification_passed", _mock_verif_pass), \
             patch("core.toss_live_pilot_adapter.can_send_live_pilot_order", return_value=(True, [])), \
             patch("core.toss_live_pilot_adapter.dispatch_toss_order_live", return_value=_mock_transport_success({}, {})) as dispatch, \
             patch("core.toss_live_pilot_telegram.resolve_live_transport_for_confirm", return_value=object()) as resolver, \
             patch("core.toss_live_pilot_ledger.record_live_sent"), \
             patch("core.toss_live_pilot_telegram.send_autonomous_result_message", return_value=True), \
             patch("core.toss_autonomous_finalizer._record_event"):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize(pilot_id)

        assert result["ok"] is False
        assert result["live_order_sent"] is False
        assert result["reason"] == "quality_decision_missing"
        resolver.assert_not_called()
        dispatch.assert_not_called()

    def test_exact_quality_row_allows_transport_resolution(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg

        monkeypatch.setattr(qg, "_outcomes_db_path", lambda: tmp_path / "quality_exact.db")
        qg._outcomes_schema_created = False
        rec = {
            **_PILOT_REC,
            "pilot_id": "tlive_20260713_qg_exact",
            "decision_ref": "execution_decision:tlive_qg_exact",
        }
        candidate = {
            "symbol": rec["symbol"],
            "side": "buy",
            "quantity": rec["quantity"],
            "limit_price": rec["limit_price"],
            "stop_loss": rec["stop_loss"],
            "target_price": rec["target_price"],
            "risk_reward": 2.0,
            "decision_bucket": "PASS_EXECUTE",
            "decision_reason": "quality pass",
            "quality_score": 82.0,
            "quality_breakdown": {
                "score_total": 82.0,
                "score_momentum": 20.0,
                "score_liquidity": 15.0,
                "score_risk_reward": 15.0,
                "score_reliability": 10.0,
                "score_market_regime": 10.0,
                "score_supply_demand": 12.0,
                "penalty_overheat": 0.0,
                "penalty_duplicate": 0.0,
                "penalty_event_risk": 0.0,
                "rr_ratio": 2.0,
                "regime": "neutral",
            },
        }
        _seal_quality_proof_for_test(qg, candidate)
        created = qg.record_execution_quality_decision(
            candidate,
            pilot_id=rec["pilot_id"],
            decision_ref=rec["decision_ref"],
        )
        assert created["ok"] is True

        with patch.dict(os.environ, _AUTO_ENV, clear=False), \
             patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=[rec]), \
             patch("core.toss_live_pilot_verification.is_verification_passed", _mock_verif_pass), \
             patch("core.toss_live_pilot_adapter.can_send_live_pilot_order", return_value=(True, [])), \
             patch("core.toss_live_pilot_telegram.resolve_live_transport_for_confirm", return_value=object()) as resolver, \
             patch("core.toss_live_pilot_adapter.dispatch_toss_order_live", return_value=_mock_transport_success({}, {})) as dispatch, \
             patch("core.toss_live_pilot_ledger.record_live_sent"), \
             patch("core.toss_live_pilot_telegram.send_autonomous_result_message", return_value=True), \
             patch("core.toss_autonomous_finalizer._record_event"):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize(rec["pilot_id"])

        assert result["ok"] is True
        assert result["live_order_sent"] is True
        resolver.assert_called_once()
        dispatch.assert_called_once()

    def test_quality_lookup_error_blocks_before_transport_resolution(self):
        from core import toss_quality_gate as qg

        rec = {
            **_PILOT_REC,
            "pilot_id": "tlive_20260713_qg_error",
            "decision_ref": "execution_decision:tlive_qg_error",
        }
        with patch.dict(os.environ, _AUTO_ENV, clear=False), \
             patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=[rec]), \
             patch("core.toss_live_pilot_verification.is_verification_passed", _mock_verif_pass), \
             patch("core.toss_live_pilot_adapter.can_send_live_pilot_order", return_value=(True, [])), \
             patch.object(qg, "validate_execution_quality_decision", side_effect=RuntimeError("synthetic")), \
             patch("core.toss_live_pilot_telegram.resolve_live_transport_for_confirm", return_value=object()) as resolver, \
             patch("core.toss_live_pilot_adapter.dispatch_toss_order_live", return_value=_mock_transport_success({}, {})) as dispatch, \
             patch("core.toss_live_pilot_ledger.record_live_sent"), \
             patch("core.toss_autonomous_finalizer._record_event"):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize(rec["pilot_id"])

        assert result["ok"] is False
        assert result["reason"] == "quality_decision_unavailable"
        resolver.assert_not_called()
        dispatch.assert_not_called()


class TestDispatchFailure:
    """transport 실패 → failed 기록."""

    @patch("core.toss_live_pilot_verification.is_verification_passed", _mock_verif_pass)
    @patch("core.toss_live_pilot_ledger.list_live_pilot_records", _mock_ledger_records)
    @patch("core.toss_live_pilot_ledger.record_live_send_failed")
    @patch("core.toss_live_pilot_telegram.resolve_live_transport_for_confirm")
    @patch("core.toss_live_pilot_telegram.send_autonomous_result_message", return_value=True)
    def test_dispatch_fail_recorded(self, mock_tg, mock_transport, mock_ledger):
        mock_transport.return_value = _mock_transport_fail
        with patch.dict(os.environ, _AUTO_ENV, clear=False), \
             patch("core.toss_quality_gate.validate_execution_quality_decision",
                   return_value={"ok": True, "reason": "quality_decision_exact"}):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize("tlive_test_pilot_001")
        assert result["ok"] is False
        assert result["live_order_sent"] is False
        mock_ledger.assert_called_once()


class TestNoTransport:
    """transport=None → dispatch blocked."""

    @patch("core.toss_live_pilot_verification.is_verification_passed", _mock_verif_pass)
    @patch("core.toss_live_pilot_ledger.list_live_pilot_records", _mock_ledger_records)
    @patch("core.toss_live_pilot_ledger.record_live_send_failed")
    @patch("core.toss_live_pilot_telegram.resolve_live_transport_for_confirm", return_value=None)
    @patch("core.toss_live_pilot_telegram.send_autonomous_result_message", return_value=True)
    def test_no_transport_blocked(self, mock_tg, mock_transport, mock_ledger):
        with patch.dict(os.environ, _AUTO_ENV, clear=False), \
             patch("core.toss_quality_gate.validate_execution_quality_decision",
                   return_value={"ok": True, "reason": "quality_decision_exact"}):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize("tlive_test_pilot_001")
        assert result["ok"] is False
        assert result["live_order_sent"] is False


class TestTelegramResultSent:
    """성공/실패 시 Telegram 결과 발송."""

    @patch("core.toss_live_pilot_verification.is_verification_passed", _mock_verif_pass)
    @patch("core.toss_live_pilot_ledger.list_live_pilot_records", _mock_ledger_records)
    @patch("core.toss_live_pilot_ledger.record_live_sent")
    @patch("core.toss_live_pilot_telegram.resolve_live_transport_for_confirm")
    @patch("core.toss_live_pilot_telegram.send_autonomous_result_message", return_value=True)
    def test_telegram_sent_on_success(self, mock_tg, mock_transport, mock_ledger):
        mock_transport.return_value = _mock_transport_success
        with patch.dict(os.environ, _AUTO_ENV, clear=False), \
             patch("core.toss_quality_gate.validate_execution_quality_decision",
                   return_value={"ok": True, "reason": "quality_decision_exact"}):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            try_autonomous_finalize("tlive_test_pilot_001")
        mock_tg.assert_called_once()
        text = mock_tg.call_args[0][0]
        assert "자율실행 체결" in text
        assert "NVDA" in text


class TestHttp422Diagnostics:
    """http_422 broker diagnostics must be persisted and not retried same day."""

    @patch("core.toss_live_pilot_verification.is_verification_passed", _mock_verif_pass)
    @patch("core.toss_live_pilot_ledger.list_live_pilot_records", _mock_ledger_records)
    @patch("core.toss_live_pilot_ledger.record_live_send_failed")
    @patch("core.toss_live_pilot_events.record_event")
    @patch("core.toss_live_pilot_telegram.resolve_live_transport_for_confirm")
    @patch("core.toss_live_pilot_telegram.send_autonomous_result_message", return_value=True)
    def test_http_422_error_body_and_request_preview_recorded(
        self, mock_tg, mock_transport, mock_event, mock_ledger
    ):
        mock_transport.return_value = _mock_transport_http_422
        mock_event.return_value = {"ok": True, "event_id": "tle_mock"}
        with patch.dict(os.environ, _AUTO_ENV, clear=False), \
             patch("core.toss_quality_gate.validate_execution_quality_decision",
                   return_value={"ok": True, "reason": "quality_decision_exact"}):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize("tlive_test_pilot_001")
        assert result["ok"] is False
        assert result["reason"] == "http_422"
        assert "INVALID_PRICE" in result["error_body"]
        assert result["order_request_preview"]["symbol"] == "000270"
        kwargs = mock_event.call_args.kwargs
        assert "INVALID_PRICE" in kwargs["error_body"]
        assert kwargs["order_request_preview"]["side"] == "BUY"
        mock_ledger.assert_called_once()

    @patch("core.toss_live_pilot_verification.is_verification_passed", _mock_verif_pass)
    @patch("core.toss_live_pilot_ledger.record_live_send_blocked")
    @patch("core.toss_live_pilot_adapter.dispatch_toss_order_live")
    @patch("core.toss_live_pilot_telegram.send_autonomous_result_message", return_value=True)
    def test_prior_http_422_same_symbol_side_today_blocks_new_attempt(
        self, mock_tg, mock_dispatch, mock_block
    ):
        today = datetime.now(KST).strftime("%Y-%m-%dT09:01:00+09:00")
        current = {**_PILOT_REC, "pilot_id": "tlive_test_pilot_001", "symbol": "000270.KS"}
        prior = {
            **_PILOT_REC,
            "pilot_id": "old_failed",
            "symbol": "000270.KS",
            "status": "live_send_failed",
            "failure_reason": "http_422: INVALID_PRICE",
            "created_at": today,
        }
        with patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=[current, prior]):
            with patch.dict(os.environ, _AUTO_ENV, clear=False):
                from core.toss_autonomous_finalizer import try_autonomous_finalize
                result = try_autonomous_finalize("tlive_test_pilot_001")
        assert result["ok"] is False
        assert result["reason"] == "prior_http_422_today"
        mock_dispatch.assert_not_called()
        mock_block.assert_called_once()


def test_autonomous_event_receives_decision_ref_and_live_policy_flag():
    from core.toss_autonomous_finalizer import _record_event

    policy = {"adapter_status": "enabled", "live_order_allowed": True}
    with patch("core.toss_live_pilot_events.record_event") as record:
        _record_event(
            pilot_id="tlive_test_pilot_001",
            event_type="autonomous_live_sent",
            status="live_sent",
            verification_id="hv_mock",
            reason="autonomous_execution",
            rec=_PILOT_REC,
            policy=policy,
            live_order_sent=True,
        )
    kwargs = record.call_args.kwargs
    assert kwargs["decision_ref"] == "execution_decision:tlive_test_001"
    assert kwargs["live_order_allowed"] is True
    assert kwargs["adapter_status"] == "enabled"
    assert kwargs["live_order_sent"] is True
