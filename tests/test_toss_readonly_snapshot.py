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


def _broker_orders() -> list[dict]:
    return [
        {
            "client_order_id": "tlive_20260712_025100_1234",
            "broker_order_id": "123456789012",
            "broker_order_status": "FILLED",
            "symbol": "005930",
            "side": "BUY",
            "quantity": 1,
            "filled_quantity": 1,
            "filled_price": 61000,
            "ordered_at": "2026-07-12T02:51:00+09:00",
            "filled_at": "2026-07-12T02:51:01+09:00",
            "access_token": "must-drop",
        },
        {
            "client_order_id": "external-secret-value",
            "broker_order_status": "OPEN",
            "symbol": "MU",
            "side": "BUY",
        },
    ]


@pytest.fixture
def snapshot_path(tmp_path, monkeypatch):
    path = tmp_path / "toss_readonly_snapshot.json"
    monkeypatch.setenv("TOSS_READONLY_SNAPSHOT_PATH", str(path))
    return path


def test_snapshot_allowlist_roundtrip_atomic_private_and_read_only(snapshot_path):
    result = snap.write_snapshot(
        _summary(),
        {"KR": {"today": {"date": "2026-07-12", "isOpen": False}, "credential": "drop"}},
        _broker_orders(),
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
    assert raw["broker_orders"][0]["client_order_id"] == "tlive_20260712_025100_1234"
    assert "broker_order_id" not in raw["broker_orders"][0]
    assert "access_token" not in raw["broker_orders"][0]
    assert "client_order_id" not in raw["broker_orders"][1]
    assert set(raw["broker_orders"][0]) <= {
        "client_order_id", "broker_order_status", "symbol", "side",
        "quantity", "filled_quantity", "filled_price", "ordered_at", "filled_at",
    }
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
    assert loaded["broker_orders"][0]["client_order_id"] == "tlive_20260712_025100_1234"


def test_snapshot_rejects_sensitive_assignment_hidden_in_allowed_text_field(snapshot_path):
    result = snap.write_snapshot(
        _summary(),
        broker_orders=[{
            "client_order_id": "tlive_20260712_025100_1234",
            "broker_order_status": "authorization=private-value",
            "symbol": "005930",
            "side": "BUY",
            "quantity": 1,
            "filled_quantity": 1,
            "filled_price": 61_000,
        }],
        now=1_000.0,
    )

    assert result == {"ok": False, "reason": "sensitive_data_detected"}
    assert not snapshot_path.exists()


def test_snapshot_rejects_naked_pat_hidden_in_allowed_text_field(snapshot_path):
    fake_pat = "github_pat_" + "B" * 30
    result = snap.write_snapshot(
        _summary(),
        broker_orders=[{
            "client_order_id": "tlive_20260712_025100_1234",
            "broker_order_status": fake_pat,
            "symbol": "005930",
            "side": "BUY",
            "quantity": 1,
            "filled_quantity": 1,
            "filled_price": 61_000,
        }],
        now=1_000.0,
    )

    assert result == {"ok": False, "reason": "sensitive_data_detected"}
    assert not snapshot_path.exists()


def test_snapshot_v2_without_broker_orders_remains_compatible(snapshot_path):
    assert snap.write_snapshot(_summary(), now=1_000.0)["ok"] is True
    raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
    raw.pop("broker_orders", None)
    snapshot_path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = snap.load_snapshot(now=1_100.0)
    assert loaded["ok"] is True
    assert loaded["broker_orders"] == []


def test_broker_order_consumer_is_read_only_and_snapshot_only(snapshot_path):
    assert snap.write_snapshot(_summary(), broker_orders=_broker_orders(), now=1_000.0)["ok"] is True
    with patch("core.toss_readonly_snapshot.time.time", return_value=1_100.0):
        result = snap.broker_orders_for_consumer()
    assert result["ok"] is True
    assert result["source"] == "stock_bot_snapshot"
    assert result["usable_for_orders"] is False
    assert result["orders"][0]["client_order_id"] == "tlive_20260712_025100_1234"


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


def test_partial_broker_order_query_is_not_published_as_complete_snapshot(monkeypatch):
    from core import toss_client as tc

    monkeypatch.setattr(tc, "get_accounts", lambda: [{
        "accountSeq": "safe-seq", "accountType": "BROKERAGE",
    }])
    monkeypatch.setattr(tc, "get_holdings", lambda _seq: {"items": [], "marketValue": {}})
    monkeypatch.setattr(tc, "get_buying_power", lambda _seq, _currency: {})
    monkeypatch.setattr(tc, "get_exchange_rate", lambda _base, _quote: {})
    monkeypatch.setattr(tc, "get_market_calendar", lambda _market: {})
    monkeypatch.setattr(tc, "is_configured", lambda: True)

    with patch("core.toss_live_order_http.list_orders", side_effect=[
        {"ok": True, "orders": []},
        {"ok": False, "reason": "closed_query_failed", "orders": []},
    ]):
        with pytest.raises(RuntimeError, match="broker_orders_incomplete:CLOSED"):
            snap._raw_account_summary_from_broker()


def test_owner_refresh_uses_direct_projection_without_dashboard_import(snapshot_path, monkeypatch):
    monkeypatch.setenv("TOSS_AUTONOMOUS_MODE", "true")
    monkeypatch.setattr(sys, "argv", ["main.py", "bot"])
    monkeypatch.setattr(snap, "_LAST_REFRESH_MONOTONIC", 0.0)
    with patch("core.toss_readonly_snapshot._raw_account_summary_from_broker", return_value=(_summary(), {}, [])) as fetch:
        result = snap.refresh_snapshot_if_due(force=True)
    fetch.assert_called_once_with()
    assert result["ok"] is True
    assert result["order_side_effects"] is False
    source = (ROOT / "core" / "toss_readonly_snapshot.py").read_text(encoding="utf-8")
    assert "core.dashboard_data" not in source


def test_owner_refresh_syncs_exact_broker_fills_before_snapshot_write(snapshot_path, monkeypatch):
    monkeypatch.setenv(snap.ROLE_ENV, snap.ROLE_OWNER)
    monkeypatch.setattr(snap, "_LAST_REFRESH_MONOTONIC", 0.0)
    orders = _broker_orders()
    with patch(
        "core.toss_readonly_snapshot._raw_account_summary_from_broker",
        return_value=(_summary(), {}, orders),
    ), patch(
        "core.toss_live_pilot_events.sync_live_event_fills_from_broker_orders",
        return_value={"updated": 1, "ambiguous": 0, "rejected": 1},
    ) as sync:
        result = snap.refresh_snapshot_if_due(force=True)

    assert result["ok"] is True
    assert result["fill_sync"] == {"updated": 1, "ambiguous": 0, "rejected": 1}
    sync.assert_called_once_with(orders)


def test_failed_refresh_preserves_last_known_good(snapshot_path, monkeypatch):
    assert snap.write_snapshot(_summary(), now=1_000.0)["ok"] is True
    before = snapshot_path.read_text(encoding="utf-8")
    monkeypatch.setenv(snap.ROLE_ENV, snap.ROLE_OWNER)
    monkeypatch.setattr(snap, "_LAST_REFRESH_MONOTONIC", 0.0)
    with patch("core.toss_readonly_snapshot._raw_account_summary_from_broker", return_value=({}, {}, [])):
        result = snap.refresh_snapshot_if_due(force=True)
    assert result["ok"] is False
    assert snapshot_path.read_text(encoding="utf-8") == before


def test_snapshot_import_smoke_has_no_cycle():
    module = importlib.import_module("core.toss_readonly_snapshot")
    assert module.VERSION == snap.VERSION
    importlib.import_module("core.dashboard_data")
    importlib.import_module("core.toss_client")
    importlib.import_module("core.toss_decision_context")


class TestConsumerBlockWithoutAutonomousEnv:
    """토큰 단일 소유 완성형 — autonomous env 없어도 비소유는 consumer.

    2026-07-15 실측: 크론 브리핑(.env만 로드, TOSS_AUTONOMOUS_MODE 미설정)이
    consumer 차단을 우회해 토큰을 발급 → bot 토큰 무효화 → 401 경쟁 재발.
    비소유 프로세스는 env와 무관하게 항상 snapshot consumer여야 한다.
    """

    def test_briefing_without_autonomous_env_is_consumer(self, monkeypatch):
        import sys
        from core import toss_readonly_snapshot as trs
        monkeypatch.delenv("TOSS_AUTONOMOUS_MODE", raising=False)
        monkeypatch.delenv("TOSS_PROCESS_ROLE", raising=False)
        monkeypatch.setattr(sys, "argv", ["main.py", "briefing"])
        assert trs.should_consume_snapshot() is True

    def test_plain_tool_without_env_is_consumer(self, monkeypatch):
        import sys
        from core import toss_readonly_snapshot as trs
        monkeypatch.delenv("TOSS_AUTONOMOUS_MODE", raising=False)
        monkeypatch.delenv("TOSS_PROCESS_ROLE", raising=False)
        monkeypatch.setattr(sys, "argv", ["some_tool.py"])
        assert trs.should_consume_snapshot() is True

    def test_bot_is_still_owner(self, monkeypatch):
        import sys
        from core import toss_readonly_snapshot as trs
        monkeypatch.delenv("TOSS_AUTONOMOUS_MODE", raising=False)
        monkeypatch.delenv("TOSS_PROCESS_ROLE", raising=False)
        monkeypatch.setattr(sys, "argv", ["main.py", "bot"])
        assert trs.should_consume_snapshot() is False

    def test_explicit_owner_role_respected(self, monkeypatch):
        import sys
        from core import toss_readonly_snapshot as trs
        monkeypatch.setenv("TOSS_PROCESS_ROLE", "broker_owner")
        monkeypatch.setattr(sys, "argv", ["some_tool.py"])
        assert trs.should_consume_snapshot() is False
