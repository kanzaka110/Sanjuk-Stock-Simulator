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

_AUTO_ENV = {
    "TOSS_LIVE_PILOT_ENABLED": "true",
    "TOSS_LIVE_ORDER_ALLOWED": "true",
    "TOSS_LIVE_ADAPTER_ENABLED": "true",
    "TOSS_AUTONOMOUS_MODE": "true",
}

_PILOT_REC = {
    "pilot_id": "test_pilot_001",
    "symbol": "NVDA",
    "side": "buy",
    "quantity": 1,
    "limit_price": 190.0,
    "estimated_amount_krw": 280_000,
    "status": "previewed",
    "blocks": [],
    "live_order_sent": False,
    "stop_loss": 180.0,
    "invalidation": "below $180",
}


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


class TestAutonomousDisabled:
    """autonomous_mode=false → no-op."""

    def test_skipped_when_disabled(self):
        with patch.dict(os.environ, {**_AUTO_ENV, "TOSS_AUTONOMOUS_MODE": "false"}, clear=False):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize("test_pilot_001")
        assert result["ok"] is False
        assert result.get("skipped") is True
        assert result["reason"] == "autonomous_mode_disabled"


class TestKillSwitch:
    """kill_switch=true → 차단."""

    def test_blocked_by_kill_switch(self):
        env = {**_AUTO_ENV, "TOSS_AUTONOMOUS_KILL_SWITCH": "true"}
        with patch.dict(os.environ, env, clear=False):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize("test_pilot_001")
        assert result["ok"] is False
        assert result["reason"] == "autonomous_kill_switch_active"


class TestHermesGate:
    """Hermes PASS가 없으면 차단."""

    @patch("core.toss_live_pilot_verification.is_verification_passed", _mock_verif_hold)
    @patch("core.toss_live_pilot_ledger.list_live_pilot_records", _mock_ledger_records)
    def test_blocked_by_hermes_hold(self):
        with patch.dict(os.environ, _AUTO_ENV, clear=False):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize("test_pilot_001")
        assert result["ok"] is False
        assert result["reason"] == "hermes_verification_required"

    @patch("core.toss_live_pilot_verification.is_verification_passed", _mock_verif_pending)
    @patch("core.toss_live_pilot_ledger.list_live_pilot_records", _mock_ledger_records)
    def test_blocked_by_hermes_pending(self):
        with patch.dict(os.environ, _AUTO_ENV, clear=False):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize("test_pilot_001")
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
                result = try_autonomous_finalize("test_pilot_001")
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
        with patch.dict(os.environ, _AUTO_ENV, clear=False):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize("test_pilot_001")
        assert result["ok"] is True
        assert result["live_order_sent"] is True
        assert result["broker_order_id"] == "mock_order_123"
        mock_ledger.assert_called_once()


class TestDispatchFailure:
    """transport 실패 → failed 기록."""

    @patch("core.toss_live_pilot_verification.is_verification_passed", _mock_verif_pass)
    @patch("core.toss_live_pilot_ledger.list_live_pilot_records", _mock_ledger_records)
    @patch("core.toss_live_pilot_ledger.record_live_send_failed")
    @patch("core.toss_live_pilot_telegram.resolve_live_transport_for_confirm")
    @patch("core.toss_live_pilot_telegram.send_autonomous_result_message", return_value=True)
    def test_dispatch_fail_recorded(self, mock_tg, mock_transport, mock_ledger):
        mock_transport.return_value = _mock_transport_fail
        with patch.dict(os.environ, _AUTO_ENV, clear=False):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize("test_pilot_001")
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
        with patch.dict(os.environ, _AUTO_ENV, clear=False):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            result = try_autonomous_finalize("test_pilot_001")
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
        with patch.dict(os.environ, _AUTO_ENV, clear=False):
            from core.toss_autonomous_finalizer import try_autonomous_finalize
            try_autonomous_finalize("test_pilot_001")
        mock_tg.assert_called_once()
        text = mock_tg.call_args[0][0]
        assert "자율실행 체결" in text
        assert "NVDA" in text
