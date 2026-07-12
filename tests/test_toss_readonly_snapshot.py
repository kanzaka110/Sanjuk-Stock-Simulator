from __future__ import annotations

import importlib
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import toss_readonly_snapshot as snap


def _summary() -> dict:
    return {
        "enabled": True,
        "trading_enabled": True,
        "automation_status": "autonomous_live_pilot",
        "live_policy": {"all_live_gates_open": True, "unknownCredential": "leak-me"},
        "account_count": 1,
        "accounts": [{
            "account_seq": "secret-seq",
            "accountNo": "99900001234",
            "account_type": "BROKERAGE",
            "unknownCredential": "leak-me-too",
        }],
        "holdings_count": 1,
        "holdings_items": [{
            "symbol": "NVDA",
            "name": "NVIDIA",
            "quantity": "1",
            "sellableQuantity": "1",
            "currency": "USD",
            "marketCountry": "US",
            "marketValue": {"amount": "100", "purchaseAmount": "80", "client_secret": "leak"},
            "profitLoss": {"amount": "20", "amountAfterCost": "19", "rate": "0.2375"},
            "dailyProfitLoss": {"amount": "2", "rate": "0.02"},
            "unknownNewBrokerField": "must-drop",
        }],
        "market_value": {"krw": 1_000_000, "usd": 100.0, "unknown": "drop"},
        "cash": {"krw": 500_000, "krw_native": 400_000, "usd": 5.0, "usd_krw": 100_000, "source": "Toss", "unknownCredential": "drop"},
        "total_account_value": {
            "krw": 1_500_000,
            "krw_native": 1_300_000,
            "usd": 105.0,
            "usd_krw": 200_000,
            "usd_included": True,
        },
        "exchange_rate": {"base": "USD", "quote": "KRW", "rate": 1400.0},
        "profit_loss": {"krw": 19_000, "rate": 0.2, "profitable_count": 1, "loss_count": 0},
        "today_profit_loss": {"krw": 2_000, "rate": 0.02},
        "client_secret": "top-level-leak",
        "unknownCredential": "top-level-unknown",
        "warnings": [],
        "error": "",
    }


@pytest.fixture
def snapshot_path(tmp_path, monkeypatch):
    path = tmp_path / "toss_readonly_snapshot.json"
    monkeypatch.setenv("TOSS_READONLY_SNAPSHOT_PATH", str(path))
    return path


def test_snapshot_allowlist_roundtrip_atomic_private_and_read_only(snapshot_path):
    result = snap.write_snapshot(
        _summary(),
        {"KR": {"today": {"date": "2026-07-12", "isOpen": False}, "credential": "drop"}},
        now=1_000.0,
    )
    assert result["ok"] is True
    assert snapshot_path.exists()
    assert stat.S_IMODE(snapshot_path.stat().st_mode) == 0o600

    text = snapshot_path.read_text(encoding="utf-8")
    for forbidden in (
        "secret-seq", "99900001234", "leak-me", "client_secret",
        "unknownCredential", "unknownNewBrokerField", "live_policy",
    ):
        assert forbidden not in text
    raw = json.loads(text)
    assert raw["version"] == snap.VERSION
    assert raw["read_only"] is True
    assert raw["order_side_effects"] is False
    assert raw["usable_for_orders"] is False
    assert raw["account_summary"]["accounts"] == [{"account_type": "BROKERAGE"}]
    assert raw["account_summary"]["cash"]["krw_native"] == 400_000
    assert raw["account_summary"]["cash"]["usd_krw"] == 100_000
    assert raw["account_summary"]["total_account_value"]["usd_included"] is True
    assert set(raw["account_summary"]["holdings_items"][0]) <= {
        "symbol", "stockCode", "name", "quantity", "sellableQuantity",
        "lastPrice", "currentPrice", "averagePrice", "currency",
        "marketCountry", "marketValue", "profitLoss", "dailyProfitLoss",
    }

    loaded = snap.load_snapshot(now=1_100.0)
    assert loaded["status"] == "fresh"
    assert loaded["usable_for_decisions"] is True
    assert loaded["usable_for_orders"] is False
    assert loaded["decision_context"]["cash_krw"] == 500_000
    assert loaded["decision_context"]["market_calendar"]["KR"]["today"]["date"] == "2026-07-12"


def test_fresh_snapshot_is_decision_only_never_order_authorization(snapshot_path):
    assert snap.write_snapshot(_summary(), now=time.time())["ok"] is True
    account = snap.account_summary_for_consumer()
    context = snap.decision_context_for_consumer()
    assert account is not None and context is not None
    assert account["snapshot_usable_for_decisions"] is True
    assert account["snapshot_usable_for_orders"] is False
    assert context["data_quality"]["usable_for_decisions"] is True
    assert context["data_quality"]["usable_for_orders"] is False
    assert context["automation"]["live_orders_allowed"] is False


def test_stale_snapshot_is_display_only_and_fails_closed(snapshot_path):
    assert snap.write_snapshot(_summary(), now=1_000.0)["ok"] is True
    loaded = snap.load_snapshot(now=2_000.0)
    assert loaded["status"] == "stale"
    assert loaded["usable_for_decisions"] is False
    assert loaded["usable_for_orders"] is False

    with patch("core.toss_readonly_snapshot.time.time", return_value=2_000.0):
        context = snap.decision_context_for_consumer()
    assert context is not None
    assert context["data_quality"]["toss_available"] is False
    assert context["data_quality"]["cash_available"] is False
    assert context["snapshot_usable_for_decisions"] is False
    assert context["snapshot_usable_for_orders"] is False


def test_expired_snapshot_withholds_payload(snapshot_path):
    assert snap.write_snapshot(_summary(), now=1_000.0)["ok"] is True
    loaded = snap.load_snapshot(now=5_000.1)
    assert loaded["ok"] is False
    assert loaded["status"] == "expired"
    assert "account_summary" not in loaded


def test_future_timestamp_over_skew_is_invalid(snapshot_path):
    assert snap.write_snapshot(_summary(), now=1_121.0)["ok"] is True
    invalid = snap.load_snapshot(now=1_000.0)
    assert invalid == {"ok": False, "status": "invalid", "reason": "snapshot_from_future"}


def test_future_timestamp_at_skew_boundary_is_fresh(snapshot_path):
    assert snap.write_snapshot(_summary(), now=1_120.0)["ok"] is True
    loaded = snap.load_snapshot(now=1_000.0)
    assert loaded["ok"] is True
    assert loaded["status"] == "fresh"


def test_older_writer_cannot_overwrite_newer_snapshot(snapshot_path):
    assert snap.write_snapshot(_summary(), now=2_000.0)["ok"] is True
    before = snapshot_path.read_text(encoding="utf-8")
    result = snap.write_snapshot(_summary(), now=1_000.0)
    assert result["skipped"] is True
    assert result["reason"] == "newer_snapshot_exists"
    assert snapshot_path.read_text(encoding="utf-8") == before


def test_replace_failure_preserves_last_known_good(snapshot_path):
    assert snap.write_snapshot(_summary(), now=1_000.0)["ok"] is True
    before = snapshot_path.read_text(encoding="utf-8")
    with patch("core.toss_readonly_snapshot.os.replace", side_effect=OSError("synthetic")):
        with pytest.raises(OSError):
            snap.write_snapshot(_summary(), now=2_000.0)
    assert snapshot_path.read_text(encoding="utf-8") == before


def test_directory_fsync_runs_after_replace(snapshot_path):
    with patch("core.toss_readonly_snapshot._fsync_directory") as sync_dir:
        assert snap.write_snapshot(_summary(), now=1_000.0)["ok"] is True
    sync_dir.assert_called_once_with(snapshot_path.parent)


def test_concurrent_readers_never_observe_partial_json(snapshot_path):
    errors: list[str] = []

    def writer(start: int):
        for offset in range(20):
            result = snap.write_snapshot(_summary(), now=float(start + offset))
            if not result.get("ok"):
                errors.append(f"writer:{result}")

    def reader():
        for _ in range(100):
            if not snapshot_path.exists():
                continue
            try:
                value = json.loads(snapshot_path.read_text(encoding="utf-8"))
                if value.get("version") != snap.VERSION:
                    errors.append("schema")
            except Exception as exc:
                errors.append(type(exc).__name__)

    threads = [threading.Thread(target=writer, args=(1_000 + index * 100,)) for index in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


def test_process_role_contract(monkeypatch):
    monkeypatch.setenv("TOSS_AUTONOMOUS_MODE", "true")
    monkeypatch.delenv(snap.ROLE_ENV, raising=False)

    monkeypatch.setattr(sys, "argv", ["main.py", "bot"])
    assert snap.process_role() == snap.ROLE_OWNER
    assert snap.should_consume_snapshot() is False

    monkeypatch.setattr(sys, "argv", ["main.py", "monitor"])
    assert snap.process_role() == snap.ROLE_OWNER

    monkeypatch.setattr(sys, "argv", ["main.py", "briefing"])
    assert snap.process_role() == snap.ROLE_CONSUMER
    assert snap.should_consume_snapshot() is True

    monkeypatch.setattr(sys, "argv", ["python", "diagnostic.py"])
    assert snap.process_role() == snap.ROLE_CONSUMER
    monkeypatch.setenv(snap.ROLE_ENV, snap.ROLE_OWNER)
    assert snap.process_role() == snap.ROLE_OWNER


def test_dashboard_uses_snapshot_without_broker_fallback(snapshot_path, monkeypatch):
    import core.dashboard_data as dd

    assert snap.write_snapshot(_summary(), now=time.time())["ok"] is True
    monkeypatch.setenv("TOSS_AUTONOMOUS_MODE", "true")
    monkeypatch.setattr(sys, "argv", ["main.py", "dashboard"])
    with patch("core.toss_client.get_accounts", side_effect=AssertionError("broker GET forbidden")):
        result = dd._fetch_toss_account_summary_raw()
    assert result["account_count"] == 1
    assert result["snapshot_status"] == "fresh"
    assert result["snapshot_usable_for_decisions"] is True
    assert result["snapshot_usable_for_orders"] is False


def test_missing_snapshot_has_no_broker_fallback(snapshot_path, monkeypatch):
    import core.dashboard_data as dd

    monkeypatch.setenv("TOSS_AUTONOMOUS_MODE", "true")
    monkeypatch.setattr(sys, "argv", ["main.py", "dashboard"])
    with patch("core.toss_client.get_accounts", side_effect=AssertionError("broker GET forbidden")):
        result = dd._fetch_toss_account_summary_raw()
    assert result["error"] == "stock_bot_snapshot_unavailable"


def test_briefing_context_uses_snapshot_without_broker_fallback(snapshot_path, monkeypatch):
    import core.toss_decision_context as dc

    assert snap.write_snapshot(_summary(), {"KR": {"isOpen": True}}, now=time.time())["ok"] is True
    monkeypatch.setenv("TOSS_AUTONOMOUS_MODE", "true")
    monkeypatch.setattr(sys, "argv", ["main.py", "briefing"])
    monkeypatch.setattr(dc, "_cache_data", None)
    monkeypatch.setattr(dc, "_cache_ts", 0.0)
    with patch("core.toss_client.get_accounts", side_effect=AssertionError("broker GET forbidden")):
        context = dc.get_toss_decision_context()
    assert context["data_quality"]["source"] == "stock_bot_snapshot"
    assert context["data_quality"]["usable_for_orders"] is False


def test_non_owner_token_network_is_blocked(monkeypatch):
    import core.toss_client as tc

    monkeypatch.setenv("TOSS_AUTONOMOUS_MODE", "true")
    monkeypatch.setattr(sys, "argv", ["main.py", "briefing"])
    tc._mem_token, tc._mem_expires = "", 0.0
    with patch("core.toss_client.requests.post", side_effect=AssertionError("token network")):
        assert tc._get_access_token() is None


def test_owner_refresh_uses_direct_projection_without_dashboard_import(snapshot_path, monkeypatch):
    monkeypatch.setenv("TOSS_AUTONOMOUS_MODE", "true")
    monkeypatch.setattr(sys, "argv", ["main.py", "bot"])
    monkeypatch.setattr(snap, "_LAST_REFRESH_MONOTONIC", 0.0)
    with patch("core.toss_readonly_snapshot._raw_account_summary_from_broker", return_value=(_summary(), {})) as fetch:
        result = snap.refresh_snapshot_if_due(force=True)
    fetch.assert_called_once_with()
    assert result["ok"] is True
    assert result["order_side_effects"] is False
    source = (ROOT / "core" / "toss_readonly_snapshot.py").read_text(encoding="utf-8")
    assert "core.dashboard_data" not in source


def test_failed_refresh_preserves_last_known_good(snapshot_path, monkeypatch):
    assert snap.write_snapshot(_summary(), now=1_000.0)["ok"] is True
    before = snapshot_path.read_text(encoding="utf-8")
    monkeypatch.setenv(snap.ROLE_ENV, snap.ROLE_OWNER)
    monkeypatch.setattr(snap, "_LAST_REFRESH_MONOTONIC", 0.0)
    with patch("core.toss_readonly_snapshot._raw_account_summary_from_broker", return_value=({}, {})):
        result = snap.refresh_snapshot_if_due(force=True)
    assert result["ok"] is False
    assert snapshot_path.read_text(encoding="utf-8") == before


def test_snapshot_import_smoke_has_no_cycle():
    module = importlib.import_module("core.toss_readonly_snapshot")
    assert module.VERSION == snap.VERSION
    importlib.import_module("core.dashboard_data")
    importlib.import_module("core.toss_client")
    importlib.import_module("core.toss_decision_context")
