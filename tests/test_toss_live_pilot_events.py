"""tests/test_toss_live_pilot_events.py

Live pilot callback 이벤트 로그 테스트.

1. record_event: 기본 동작
2. symbol_name / symbol_label 자동 생성
3. 민감정보 차단
4. invalid event_type
5. list_events / event_summary
6. live_order_allowed 항상 false
7. 삭제 없음
8. review / cancel / confirm 이벤트 타입 커버
"""

import multiprocessing
import tempfile
import time
import unittest
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _tmp_patch():
    tmp = tempfile.mkdtemp()
    return (
        Path(tmp),
        patch(
            "core.toss_live_pilot_events._db_path",
            return_value=Path(tmp) / "test_events.db",
        ),
    )


def _reset():
    import core.toss_live_pilot_events as m
    m._schema_created = False


def _cross_process_fill_worker(db_path, broker_row, barrier, delay_update, output):
    import core.toss_live_pilot_events as events

    real_connect = events.sqlite3.connect

    class ConnectionProxy:
        def __init__(self, real):
            object.__setattr__(self, "_real", real)
            object.__setattr__(self, "_barrier_used", False)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __setattr__(self, name, value):
            setattr(self._real, name, value)

        def execute(self, sql, *args):
            normalized = " ".join(str(sql).split())
            if normalized.startswith("UPDATE live_pilot_events") and delay_update:
                time.sleep(0.35)
            cursor = self._real.execute(sql, *args)
            if (
                normalized.startswith("SELECT * FROM live_pilot_events WHERE pilot_id")
                and not self._barrier_used
            ):
                object.__setattr__(self, "_barrier_used", True)
                barrier.wait(timeout=5)
            return cursor

    events._db_path = lambda: Path(db_path)
    events._schema_created = True
    events.sqlite3.connect = lambda *args, **kwargs: ConnectionProxy(
        real_connect(*args, **kwargs)
    )
    try:
        output.put(events.sync_live_event_fills_from_broker_orders([broker_row]))
    except Exception as exc:
        output.put({"error_type": type(exc).__name__})


# ── 1. 기본 record_event ──────────────────────────────────

class TestRecordEventBasic(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def test_record_returns_ok(self):
        from core.toss_live_pilot_events import record_event
        r = record_event(
            pilot_id="tlive_test_001",
            event_type="reviewed",
            status="reviewed",
        )
        self.assertTrue(r["ok"])

    def test_event_id_generated(self):
        from core.toss_live_pilot_events import record_event
        r = record_event("tlive_t", "reviewed", "reviewed")
        self.assertTrue(r["event_id"].startswith("tle_"))

    def test_live_order_sent_false_by_default(self):
        from core.toss_live_pilot_events import record_event
        r = record_event("tlive_t", "reviewed", "reviewed")
        self.assertFalse(r["live_order_sent"])

    def test_live_order_allowed_always_false(self):
        from core.toss_live_pilot_events import record_event
        r = record_event("tlive_t", "reviewed", "reviewed")
        self.assertFalse(r["live_order_allowed"])

    def test_created_at_in_result(self):
        from core.toss_live_pilot_events import record_event
        r = record_event("tlive_t", "reviewed", "reviewed")
        self.assertIn("created_at", r)

    def test_event_persisted_in_list(self):
        from core.toss_live_pilot_events import record_event, list_events
        record_event("tlive_persist", "cancelled", "cancelled")
        events = list_events(limit=10)
        found = [e for e in events if e["pilot_id"] == "tlive_persist"]
        self.assertTrue(len(found) > 0)

    def test_event_type_in_list(self):
        from core.toss_live_pilot_events import record_event, list_events
        record_event("tlive_t2", "cancelled", "cancelled")
        events = list_events(limit=10)
        types = {e["event_type"] for e in events}
        self.assertIn("cancelled", types)

    def test_decision_ref_persisted_in_event(self):
        from core.toss_live_pilot_events import record_event, list_events
        ref = "execution_decision:tlive_trace"
        result = record_event(
            "tlive_trace", "reviewed", "reviewed", decision_ref=ref
        )
        self.assertEqual(result["decision_ref"], ref)
        found = [e for e in list_events(limit=10) if e["pilot_id"] == "tlive_trace"]
        self.assertEqual(found[0]["decision_ref"], ref)

    def test_invalid_decision_ref_rejected(self):
        from core.toss_live_pilot_events import record_event
        result = record_event(
            "tlive_bad_ref", "reviewed", "reviewed",
            decision_ref="Bearer forbidden secret",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid_decision_ref")

    def test_error_diagnostics_persisted_in_list(self):
        from core.toss_live_pilot_events import record_event, list_events
        record_event(
            "tlive_diag",
            "autonomous_send_failed",
            "live_send_failed",
            reason="http_422",
            error_body='{"code":"INVALID_PRICE"}',
            order_request_preview={"symbol": "000270", "side": "BUY"},
        )
        found = [e for e in list_events(limit=10) if e["pilot_id"] == "tlive_diag"]
        self.assertTrue(found)
        self.assertIn("INVALID_PRICE", found[0]["error_body"])
        self.assertIn('\"symbol\": \"000270\"', found[0]["order_request_preview"])

    def test_list_events_exception_logs_type_only(self):
        from core import toss_live_pilot_events as events

        synthetic_marker = "Bearer list-events-private"
        with patch.object(
            events, "_conn", side_effect=RuntimeError(synthetic_marker)
        ):
            with self.assertLogs(events.log, level="WARNING") as captured:
                result = events.list_events(limit=5)
        self.assertEqual(result, [])
        self.assertNotIn(synthetic_marker, "\n".join(captured.output))


# ── 2. symbol_name / symbol_label ────────────────────────

class TestSymbolLabel(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def test_symbol_name_auto_filled(self):
        from core.toss_live_pilot_events import record_event, list_events
        record_event("tlive_sym", "reviewed", "reviewed", symbol="091180.KS")
        events = list_events(limit=5)
        found = [e for e in events if e["pilot_id"] == "tlive_sym"]
        self.assertEqual(found[0]["symbol_name"], "KODEX 자동차")

    def test_symbol_label_format(self):
        from core.toss_live_pilot_events import record_event, list_events
        record_event("tlive_sym2", "reviewed", "reviewed", symbol="091180.KS")
        events = list_events(limit=5)
        found = [e for e in events if e["pilot_id"] == "tlive_sym2"]
        self.assertEqual(found[0]["symbol_label"], "KODEX 자동차 (091180.KS)")

    def test_unknown_symbol_label_is_ticker(self):
        from core.toss_live_pilot_events import record_event, list_events
        record_event("tlive_unk", "reviewed", "reviewed", symbol="UNKNOWN.KS")
        events = list_events(limit=5)
        found = [e for e in events if e["pilot_id"] == "tlive_unk"]
        self.assertEqual(found[0]["symbol_label"], "UNKNOWN.KS")

    def test_360750_label(self):
        from core.toss_live_pilot_events import record_event, list_events
        record_event("tlive_360", "reviewed", "reviewed", symbol="360750.KS")
        events = list_events(limit=5)
        found = [e for e in events if e["pilot_id"] == "tlive_360"]
        self.assertEqual(found[0]["symbol_label"], "TIGER 미국S&P500 (360750.KS)")

    def test_mu_label(self):
        from core.toss_live_pilot_events import record_event, list_events
        record_event("tlive_mu", "reviewed", "reviewed", symbol="MU")
        events = list_events(limit=5)
        found = [e for e in events if e["pilot_id"] == "tlive_mu"]
        self.assertEqual(found[0]["symbol_label"], "Micron Technology (MU)")


# ── 3. 민감정보 차단 ──────────────────────────────────────

class TestSensitiveBlocked(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def test_accountNo_in_reason_blocked(self):
        from core.toss_live_pilot_events import record_event
        r = record_event("tlive_sens", "reviewed", "reviewed", reason="accountNo: 12345678-01")
        self.assertFalse(r["ok"])

    def test_bearer_in_message_blocked(self):
        from core.toss_live_pilot_events import record_event
        r = record_event("tlive_sens2", "reviewed", "reviewed", message="Bearer abc123token")
        self.assertFalse(r["ok"])

    def test_app_key_blocked(self):
        from core.toss_live_pilot_events import record_event
        r = record_event("tlive_sens3", "reviewed", "reviewed", reason="APP_KEY=xyz")
        self.assertFalse(r["ok"])

    def test_lowercase_and_nested_credentials_blocked_in_all_text_fields(self):
        from core.toss_live_pilot_events import record_event

        cases = [
            {"status": "authorization=private-value"},
            {"preview_id": "accessToken:private-value"},
            {"broker_order_status": "password=private-value"},
            {"symbol": "client_secret=private-value"},
            {"order_request_preview": {"nested": {"PaSsWoRd": "private-value"}}},
        ]
        for index, kwargs in enumerate(cases):
            params = {
                "pilot_id": f"tlive_sensitive_{index}",
                "event_type": "reviewed",
                "status": "reviewed",
                **kwargs,
            }
            result = record_event(**params)
            self.assertEqual(
                result,
                {"ok": False, "reason": "sensitive_field"},
                msg=f"case {index} was not blocked",
            )

    def test_naked_pat_is_blocked_from_every_persisted_text_boundary(self):
        from core.toss_live_pilot_events import record_event

        fake_pat = "ghp_" + "A" * 40
        fields = (
            "pilot_id", "status", "preview_id", "verification_id",
            "symbol", "side", "adapter_status", "reason", "message",
            "broker_order_id", "broker_order_status", "error_body",
            "order_request_preview",
        )
        for index, field in enumerate(fields):
            params = {
                "pilot_id": f"tlive_naked_{index}",
                "event_type": "reviewed",
                "status": "reviewed",
                field: fake_pat,
            }
            result = record_event(**params)
            self.assertEqual(result, {"ok": False, "reason": "sensitive_field"})
        decision_result = record_event(
            "tlive_naked_ref", "reviewed", "reviewed",
            decision_ref="prediction:" + fake_pat,
        )
        self.assertEqual(
            decision_result, {"ok": False, "reason": "sensitive_field"}
        )


class TestStrictEventIdentityAndBooleanContract(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def test_string_false_cannot_become_real_event(self):
        from core.toss_live_pilot_events import record_event

        result = record_event(
            "tlive_strict_false", "autonomous_live_sent", "sent",
            adapter_status="enabled",
            live_order_sent="false",
            live_order_allowed="false",
        )
        self.assertEqual(
            result, {"ok": False, "reason": "live_boolean_contract_invalid"}
        )

    def test_overlong_decision_ref_is_rejected_without_truncation(self):
        from core.toss_live_pilot_events import record_event

        ref = "prediction:" + "A" * 200
        result = record_event(
            "tlive_long_ref", "reviewed", "reviewed", decision_ref=ref
        )
        self.assertEqual(result, {"ok": False, "reason": "invalid_decision_ref"})

    def test_non_finite_or_boolean_financial_values_are_rejected(self):
        from core.toss_live_pilot_events import record_event

        cases: tuple[dict[str, Any], ...] = (
            {"quantity": True},
            {"quantity": float("nan")},
            {"limit_price": float("inf")},
            {"estimated_amount_krw": float("-inf")},
            {"filled_quantity": float("nan")},
            {"filled_price": True},
        )
        for index, kwargs in enumerate(cases):
            with self.subTest(kwargs=kwargs):
                result = record_event(
                    f"tlive_invalid_number_{index}",
                    "reviewed",
                    "reviewed",
                    **kwargs,
                )
                self.assertEqual(
                    result, {"ok": False, "reason": "event_numeric_contract_invalid"}
                )


# ── 4. invalid event_type ─────────────────────────────────

class TestInvalidEventType(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def test_invalid_type_returns_ok_false(self):
        from core.toss_live_pilot_events import record_event
        r = record_event("tlive_inv", "BUY_NOW", "ok")
        self.assertFalse(r["ok"])
        self.assertIn("invalid", r.get("reason", ""))


# ── 5. event_summary ──────────────────────────────────────

class TestEventSummary(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def test_summary_has_event_types(self):
        from core.toss_live_pilot_events import record_event, event_summary
        record_event("tlive_s1", "reviewed", "reviewed")
        record_event("tlive_s2", "cancelled", "cancelled")
        summ = event_summary()
        self.assertIn("reviewed", summ["summary"])
        self.assertIn("cancelled", summ["summary"])

    def test_live_order_sent_total_zero_for_non_sent(self):
        from core.toss_live_pilot_events import record_event, event_summary
        record_event("tlive_s3", "reviewed", "reviewed")
        summ = event_summary()
        self.assertEqual(summ["live_order_sent_total"], 0)

    def test_live_order_allowed_false_in_summary(self):
        from core.toss_live_pilot_events import event_summary
        summ = event_summary()
        self.assertFalse(summ["live_order_allowed"])

    def test_bare_live_sent_is_artifact_not_real(self):
        # adapter enabled + live_order_allowed 없이 들어온 live_sent는
        # artifact로 강등되어 real total을 올리지 않는다 (오염 방지).
        from core.toss_live_pilot_events import record_event, event_summary
        r = record_event("tlive_ls", "live_sent", "live_sent", live_order_sent=True)
        self.assertEqual(r["event_type"], "live_sent_artifact")
        summ = event_summary()
        self.assertEqual(summ["live_order_sent_total"], 0)
        self.assertEqual(summ["live_sent_real"], 0)
        self.assertGreater(summ["live_sent_mock_or_artifact"], 0)

    def test_real_live_sent_counts_when_fully_gated(self):
        # adapter enabled + live_order_allowed=true + sent 일 때만 real로 카운트.
        from core.toss_live_pilot_events import record_event, event_summary
        r = record_event(
            "tlive_real", "live_sent", "live_sent",
            live_order_sent=True, adapter_status="enabled", live_order_allowed=True,
        )
        self.assertEqual(r["event_type"], "live_sent")
        self.assertTrue(r["is_real_live_sent"])
        summ = event_summary()
        self.assertEqual(summ["live_sent_real"], 1)
        self.assertEqual(summ["live_order_sent_total"], 1)

    def test_autonomous_live_sent_uses_same_production_invariants(self):
        from core.toss_live_pilot_events import record_event, event_summary
        ref = "execution_decision:tlive_auto"
        r = record_event(
            "tlive_auto", "autonomous_live_sent", "live_sent",
            decision_ref=ref, live_order_sent=True,
            adapter_status="enabled", live_order_allowed=True,
        )
        self.assertEqual(r["event_type"], "autonomous_live_sent")
        self.assertTrue(r["is_real_live_sent"])
        self.assertEqual(r["decision_ref"], ref)
        summ = event_summary()
        self.assertEqual(summ["live_sent_real"], 1)
        self.assertEqual(summ["live_order_sent_total"], 1)

    def test_ungated_autonomous_live_sent_is_artifact(self):
        from core.toss_live_pilot_events import record_event, event_summary
        r = record_event(
            "tlive_auto_bad", "autonomous_live_sent", "live_sent",
            live_order_sent=True, adapter_status="enabled", live_order_allowed=False,
        )
        self.assertEqual(r["event_type"], "live_sent_artifact")
        self.assertFalse(r["is_real_live_sent"])
        self.assertEqual(event_summary()["live_sent_real"], 0)


# ── 6. 각 이벤트 타입 커버 ────────────────────────────────

class TestAllEventTypes(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def _record(self, et: str) -> dict:
        from core.toss_live_pilot_events import record_event
        return record_event(f"tlive_{et[:8]}", et, et)

    def test_reviewed(self):
        self.assertTrue(self._record("reviewed")["ok"])

    def test_cancelled(self):
        self.assertTrue(self._record("cancelled")["ok"])

    def test_confirm_blocked_hermes(self):
        self.assertTrue(self._record("confirm_blocked_hermes")["ok"])

    def test_confirm_blocked_policy(self):
        self.assertTrue(self._record("confirm_blocked_policy")["ok"])

    def test_confirm_blocked_transport(self):
        self.assertTrue(self._record("confirm_blocked_transport")["ok"])

    def test_confirmed_but_not_sent(self):
        self.assertTrue(self._record("confirmed_but_not_sent")["ok"])

    def test_live_send_blocked(self):
        self.assertTrue(self._record("live_send_blocked")["ok"])

    def test_live_sent(self):
        r = self._record("live_sent")
        self.assertTrue(r["ok"])

    def test_live_send_failed(self):
        self.assertTrue(self._record("live_send_failed")["ok"])


# ── 7. 삭제 없음 확인 ─────────────────────────────────────

class TestNoDeleteInSource(unittest.TestCase):
    def test_no_delete_from_in_source(self):
        src = (_ROOT / "core" / "toss_live_pilot_events.py").read_text(encoding="utf-8")
        self.assertNotIn("DELETE FROM", src)
        self.assertNotIn("DROP TABLE", src)

    def test_no_sensitive_in_source(self):
        import re
        src = (_ROOT / "core" / "toss_live_pilot_events.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'\d{8}-\d{2}', src), [])
        self.assertEqual(re.findall(r'Bearer [A-Za-z0-9._\-]{20,}', src), [])


if __name__ == "__main__":
    unittest.main()


def test_autonomous_send_retryable_event_type_is_accepted(tmp_path, monkeypatch):
    from core import toss_live_pilot_events as ev
    monkeypatch.setattr(ev, "_db_path", lambda: tmp_path / "events.db")
    ev._schema_created = False
    r = ev.record_event(
        pilot_id="p_retry", event_type="autonomous_send_retryable",
        status="live_send_retryable", symbol="042660.KS", side="sell",
        quantity=1, limit_price=1000, live_order_sent=False,
    )
    assert r["ok"] is True


def test_existing_sqlite_ledgers_gain_decision_ref_column(tmp_path, monkeypatch):
    from core import toss_live_pilot_events as events
    from core import toss_live_pilot_ledger as ledger
    from core import toss_live_pilot_verification as verification

    cases = (
        (events, "live_pilot_events", "events.db"),
        (ledger, "live_pilot_ledger", "ledger.db"),
        (verification, "live_pilot_verification", "verification.db"),
    )
    for module, table, filename in cases:
        path = tmp_path / filename
        conn = sqlite3.connect(path)
        conn.execute(f"CREATE TABLE {table} (id TEXT)")
        conn.commit()
        conn.close()
        monkeypatch.setattr(module, "_db_path", lambda p=path: p)
        setattr(module, "_schema_created", False)
        migrated = getattr(module, "_conn")()
        columns = {
            str(row[1]) for row in migrated.execute(f"PRAGMA table_info({table})").fetchall()
        }
        migrated.close()
        assert "decision_ref" in columns


def test_event_schema_migration_failure_does_not_mark_schema_ready(tmp_path, monkeypatch):
    import pytest
    from core import toss_live_pilot_events as events

    path = tmp_path / "events_locked.db"
    real_connect = sqlite3.connect
    legacy = real_connect(path)
    legacy.execute("CREATE TABLE live_pilot_events (event_id TEXT PRIMARY KEY)")
    legacy.commit()
    legacy.close()
    fail_fill_column = {"enabled": True}

    class ConnectionProxy:
        def __init__(self, connection):
            object.__setattr__(self, "_connection", connection)

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def __setattr__(self, name, value):
            setattr(self._connection, name, value)

        def execute(self, sql, *args):
            if (
                fail_fill_column["enabled"]
                and sql.startswith("ALTER TABLE")
                and "fill_updated_at" in sql
            ):
                raise sqlite3.OperationalError("database is locked")
            return self._connection.execute(sql, *args)

    monkeypatch.setattr(events, "_db_path", lambda: path)
    monkeypatch.setattr(
        events.sqlite3,
        "connect",
        lambda *args, **kwargs: ConnectionProxy(real_connect(*args, **kwargs)),
    )
    events._schema_created = False

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        events._conn()
    assert events._schema_created is False

    fail_fill_column["enabled"] = False
    migrated = events._conn()
    columns = {
        str(row[1])
        for row in migrated.execute("PRAGMA table_info(live_pilot_events)").fetchall()
    }
    migrated.close()
    assert "fill_updated_at" in columns
    assert events._schema_created is True


def test_event_partial_schema_migrates_complete_insert_contract(tmp_path, monkeypatch):
    from core import toss_live_pilot_events as events

    path = tmp_path / "events_partial.db"
    legacy = sqlite3.connect(path)
    legacy.execute("CREATE TABLE live_pilot_events (event_id TEXT PRIMARY KEY)")
    legacy.commit()
    legacy.close()
    monkeypatch.setattr(events, "_db_path", lambda: path)
    events._schema_created = False

    migrated = events._conn()
    columns = {
        str(row[1])
        for row in migrated.execute("PRAGMA table_info(live_pilot_events)").fetchall()
    }
    migrated.close()
    required = {
        "event_id", "pilot_id", "preview_id", "verification_id", "decision_ref",
        "event_type", "status", "symbol", "symbol_name", "symbol_label", "side",
        "quantity", "limit_price", "estimated_amount_krw", "live_order_sent",
        "adapter_status", "live_order_allowed", "reason", "message",
        "broker_order_id", "broker_order_status", "filled_quantity", "filled_price",
        "fill_updated_at", "error_body", "order_request_preview", "created_at",
        "delivered_to_hermes",
    }
    assert required <= columns
    recorded = events.record_event("tlive_partial_schema", "reviewed", "reviewed")
    assert recorded["ok"] is True


def test_live_pilot_ledger_persists_immutable_decision_ref(tmp_path, monkeypatch):
    from core import toss_live_pilot_ledger as ledger

    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "ledger_trace.db")
    ledger._schema_created = False
    preview = {
        "ok": True,
        "preview_id": "tlive_trace",
        "decision_ref": "execution_decision:tlive_trace",
        "symbol": "MU",
        "side": "buy",
        "quantity": 1,
        "limit_price": 100.0,
        "estimated_amount_krw": 150_000.0,
        "blocks": [],
        "warnings": [],
    }
    result = ledger.record_live_pilot_preview(preview)
    assert result["ok"] is True
    stored = ledger.list_live_pilot_records(limit=1)[0]
    assert stored["decision_ref"] == "execution_decision:tlive_trace"

    preview["decision_ref"] = "Bearer forbidden secret"
    assert ledger.record_live_pilot_preview(preview) == {
        "ok": False,
        "reason": "invalid_decision_ref",
    }


def test_exact_broker_fill_monotonically_updates_pending_autonomous_event(tmp_path, monkeypatch):
    from core import toss_live_pilot_events as ev

    monkeypatch.setattr(ev, "_db_path", lambda: tmp_path / "events_fill.db")
    ev._schema_created = False
    pilot_id = "tlive_20260713_100000_1234"
    created = ev.record_event(
        pilot_id=pilot_id,
        event_type="autonomous_live_sent",
        status="live_sent",
        verification_id="hv_20260713_100000_1234",
        decision_ref="execution_decision:tlive_20260713_100000_1234",
        symbol="035720.KS",
        side="sell",
        quantity=2,
        limit_price=35_900,
        live_order_sent=True,
        adapter_status="enabled",
        live_order_allowed=True,
        broker_order_status="PENDING",
        filled_quantity=0,
        filled_price=35_900,
    )
    assert created["ok"] is True

    result = ev.sync_live_event_fills_from_broker_orders([{
        "client_order_id": pilot_id,
        "symbol": "035720",
        "side": "SELL",
        "quantity": 2,
        "broker_order_status": "FILLED",
        "filled_quantity": 2,
        "filled_price": 35_850,
    }])

    assert result == {"updated": 1, "ambiguous": 0, "rejected": 0}
    row = next(item for item in ev.list_events(limit=10) if item["event_id"] == created["event_id"])
    assert row["filled_quantity"] == 2
    assert row["filled_price"] == 35_850
    assert row["broker_order_status"] == "FILLED"
    assert row["fill_updated_at"]
    assert ev.latest_fill_for_pilot(pilot_id)["filled_quantity"] == 2


def test_initial_filled_event_sets_fill_updated_at(tmp_path, monkeypatch):
    from core import toss_live_pilot_events as ev

    monkeypatch.setattr(ev, "_db_path", lambda: tmp_path / "events_initial_fill.db")
    ev._schema_created = False
    created = ev.record_event(
        pilot_id="tlive_20260713_100007_1234",
        event_type="autonomous_live_sent",
        status="live_sent",
        decision_ref="execution_decision:tlive_20260713_100007_1234",
        symbol="035720.KS",
        side="buy",
        quantity=2,
        live_order_sent=True,
        adapter_status="enabled",
        live_order_allowed=True,
        broker_order_status="FILLED",
        filled_quantity=2,
        filled_price=35_900,
    )

    row = next(item for item in ev.list_events(limit=10)
               if item["event_id"] == created["event_id"])
    assert row["fill_updated_at"]


def test_broker_status_cannot_regress_from_filled_to_pending(tmp_path, monkeypatch):
    from core import toss_live_pilot_events as ev

    monkeypatch.setattr(ev, "_db_path", lambda: tmp_path / "events_status_regression.db")
    ev._schema_created = False
    pilot_id = "tlive_20260713_100008_1234"
    ev.record_event(
        pilot_id=pilot_id,
        event_type="autonomous_live_sent",
        status="live_sent",
        decision_ref=f"execution_decision:{pilot_id}",
        symbol="035720.KS",
        side="buy",
        quantity=2,
        live_order_sent=True,
        adapter_status="enabled",
        live_order_allowed=True,
        broker_order_status="FILLED",
        filled_quantity=2,
        filled_price=35_900,
    )

    result = ev.sync_live_event_fills_from_broker_orders([{
        "client_order_id": pilot_id,
        "symbol": "035720",
        "side": "BUY",
        "quantity": 2,
        "filled_quantity": 2,
        "filled_price": 35_900,
        "broker_order_status": "PENDING",
    }])

    assert result == {"updated": 0, "ambiguous": 0, "rejected": 1}
    row = next(item for item in ev.list_events(limit=10)
               if item["pilot_id"] == pilot_id)
    assert row["broker_order_status"] == "FILLED"


def test_broker_fill_requires_exact_original_order_quantity(tmp_path, monkeypatch):
    from core import toss_live_pilot_events as ev

    monkeypatch.setattr(ev, "_db_path", lambda: tmp_path / "events_quantity.db")
    ev._schema_created = False
    pilot_id = "tlive_20260713_100004_1234"
    ev.record_event(
        pilot_id=pilot_id,
        event_type="autonomous_live_sent",
        status="live_sent",
        decision_ref=f"execution_decision:{pilot_id}",
        symbol="035720.KS",
        side="buy",
        quantity=2,
        live_order_sent=True,
        adapter_status="enabled",
        live_order_allowed=True,
    )
    common = {
        "client_order_id": pilot_id,
        "symbol": "035720",
        "side": "BUY",
        "filled_quantity": 1,
        "filled_price": 35_900,
        "broker_order_status": "PARTIAL",
    }

    mismatch = ev.sync_live_event_fills_from_broker_orders([
        {**common, "quantity": 99},
    ])
    missing = ev.sync_live_event_fills_from_broker_orders([common])
    accepted = ev.sync_live_event_fills_from_broker_orders([
        {**common, "quantity": 2},
    ])

    assert mismatch == {"updated": 0, "ambiguous": 0, "rejected": 1}
    assert missing == {"updated": 0, "ambiguous": 0, "rejected": 1}
    assert accepted == {"updated": 1, "ambiguous": 0, "rejected": 0}


def test_broker_fill_sync_is_monotonic_across_processes(tmp_path, monkeypatch):
    from core import toss_live_pilot_events as ev

    db_path = tmp_path / "events_cross_process.db"
    monkeypatch.setattr(ev, "_db_path", lambda: db_path)
    ev._schema_created = False
    pilot_id = "tlive_20260713_atomic_1234"
    created = ev.record_event(
        pilot_id,
        "autonomous_live_sent",
        "sent",
        symbol="316140.KS",
        side="buy",
        quantity=3,
        live_order_sent=True,
        adapter_status="enabled",
        live_order_allowed=True,
    )
    assert created["ok"] is True

    common = {
        "client_order_id": pilot_id,
        "symbol": "316140",
        "side": "buy",
        "quantity": 3,
        "broker_order_status": "PARTIAL",
        "filled_at": "2026-07-13T12:00:00+09:00",
    }
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    output = ctx.Queue()
    low = ctx.Process(
        target=_cross_process_fill_worker,
        args=(str(db_path), {**common, "filled_quantity": 1, "filled_price": 101},
              barrier, True, output),
    )
    high = ctx.Process(
        target=_cross_process_fill_worker,
        args=(str(db_path), {**common, "filled_quantity": 2, "filled_price": 102},
              barrier, False, output),
    )
    low.start()
    high.start()
    low.join(10)
    high.join(10)
    assert low.exitcode == 0 and high.exitcode == 0
    outcomes = [output.get(timeout=2), output.get(timeout=2)]
    assert all("error_type" not in outcome for outcome in outcomes), outcomes

    row = next(
        item for item in ev.list_events(limit=10) if item["pilot_id"] == pilot_id
    )
    assert row["filled_quantity"] == 2
    assert row["filled_price"] == 102


def test_cas_conflict_cannot_rollback_prior_pilot_update(tmp_path, monkeypatch):
    from core import toss_live_pilot_events as ev

    db_path = tmp_path / "events_cas_rollback.db"
    monkeypatch.setattr(ev, "_db_path", lambda: db_path)
    ev._schema_created = False
    pilots = (
        "tlive_20260713_casfirst_1234",
        "tlive_20260713_cassecond_1234",
    )
    for pilot_id, symbol in zip(pilots, ("316140.KS", "035420.KS")):
        result = ev.record_event(
            pilot_id,
            "autonomous_live_sent",
            "sent",
            symbol=symbol,
            side="buy",
            quantity=3,
            live_order_sent=True,
            adapter_status="enabled",
            live_order_allowed=True,
        )
        assert result["ok"] is True
    event_ids = {
        row["pilot_id"]: row["event_id"] for row in ev.list_events(limit=10)
    }
    blocked_event_id = event_ids[pilots[1]]
    real_connect = sqlite3.connect

    class ZeroRowCursor:
        rowcount = 0

    class ConnectionProxy:
        def __init__(self, real):
            object.__setattr__(self, "_real", real)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __setattr__(self, name, value):
            setattr(self._real, name, value)

        def execute(self, sql, *args):
            normalized = " ".join(str(sql).split())
            params = args[0] if args else ()
            if (
                normalized.startswith("UPDATE live_pilot_events")
                and len(params) > 4
                and params[4] == blocked_event_id
            ):
                return ZeroRowCursor()
            return self._real.execute(sql, *args)

    def conflict_conn():
        conn = real_connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return ConnectionProxy(conn)

    monkeypatch.setattr(ev, "_conn", conflict_conn)
    orders = [
        {
            "client_order_id": pilots[0], "symbol": "316140", "side": "buy",
            "quantity": 3, "filled_quantity": 1, "filled_price": 101,
            "broker_order_status": "PARTIAL",
            "filled_at": "2026-07-13T12:00:00+09:00",
        },
        {
            "client_order_id": pilots[1], "symbol": "035420", "side": "buy",
            "quantity": 3, "filled_quantity": 1, "filled_price": 202,
            "broker_order_status": "PARTIAL",
            "filled_at": "2026-07-13T12:00:01+09:00",
        },
    ]
    outcome = ev.sync_live_event_fills_from_broker_orders(orders)
    assert outcome == {"updated": 1, "ambiguous": 1, "rejected": 0}

    verify = real_connect(db_path)
    first_fill = verify.execute(
        "SELECT filled_quantity FROM live_pilot_events WHERE pilot_id=?",
        (pilots[0],),
    ).fetchone()[0]
    verify.close()
    assert first_fill == 1


def test_broker_fill_sync_rejects_symbol_or_client_id_mismatch(tmp_path, monkeypatch):
    from core import toss_live_pilot_events as ev

    monkeypatch.setattr(ev, "_db_path", lambda: tmp_path / "events_mismatch.db")
    ev._schema_created = False
    pilot_id = "tlive_20260713_100001_1234"
    ev.record_event(
        pilot_id=pilot_id,
        event_type="autonomous_live_sent",
        status="live_sent",
        decision_ref="execution_decision:tlive_20260713_100001_1234",
        symbol="035720.KS",
        side="sell",
        quantity=1,
        live_order_sent=True,
        adapter_status="enabled",
        live_order_allowed=True,
    )
    result = ev.sync_live_event_fills_from_broker_orders([{
        "client_order_id": pilot_id,
        "symbol": "096770",
        "side": "SELL",
        "quantity": 1,
        "filled_quantity": 1,
        "filled_price": 100,
    }, {
        "client_order_id": "external_order",
        "symbol": "035720",
        "side": "SELL",
        "quantity": 1,
        "filled_quantity": 1,
        "filled_price": 100,
    }, {
        "client_order_id": pilot_id,
        "symbol": "035720",
        "side": "SELL",
        "quantity": 1,
        "filled_quantity": 1,
        "filled_price": 100,
    }])
    assert result == {"updated": 0, "ambiguous": 1, "rejected": 1}
    row = ev.list_events(limit=1)[0]
    assert row["filled_quantity"] == 0

    quantity_unknown = ev.sync_live_event_fills_from_broker_orders([{
        "client_order_id": pilot_id,
        "symbol": "035720",
        "side": "SELL",
        "filled_quantity": 1,
        "filled_price": 100,
    }])
    assert quantity_unknown == {"updated": 0, "ambiguous": 0, "rejected": 1}


def test_duplicate_exact_broker_rows_require_ordered_cumulative_truth(tmp_path, monkeypatch):
    from core import toss_live_pilot_events as ev

    monkeypatch.setattr(ev, "_db_path", lambda: tmp_path / "events_duplicates.db")
    ev._schema_created = False

    def create(pilot_id: str):
        return ev.record_event(
            pilot_id=pilot_id,
            event_type="autonomous_live_sent",
            status="live_sent",
            decision_ref=f"execution_decision:{pilot_id}",
            symbol="035720.KS",
            side="buy",
            quantity=3,
            live_order_sent=True,
            adapter_status="enabled",
            live_order_allowed=True,
        )

    ambiguous_id = "tlive_20260713_120002_1234"
    create(ambiguous_id)
    common = {
        "client_order_id": ambiguous_id,
        "symbol": "035720",
        "side": "BUY",
        "quantity": 3,
        "broker_order_status": "PARTIAL",
    }
    result = ev.sync_live_event_fills_from_broker_orders([
        {**common, "filled_quantity": 1, "filled_price": 35_900},
        {**common, "filled_quantity": 2, "filled_price": 35_850},
    ])
    assert result == {"updated": 0, "ambiguous": 1, "rejected": 0}
    row = next(r for r in ev.list_events(limit=10) if r["pilot_id"] == ambiguous_id)
    assert row["filled_quantity"] == 0

    ordered_id = "tlive_20260713_120003_1234"
    create(ordered_id)
    common["client_order_id"] = ordered_id
    result = ev.sync_live_event_fills_from_broker_orders([
        {**common, "filled_quantity": 1, "filled_price": 35_900,
         "filled_at": "2026-07-13T12:00:01+09:00"},
        {**common, "filled_quantity": 2, "filled_price": 35_850,
         "filled_at": "2026-07-13T12:00:02+09:00"},
    ])
    assert result == {"updated": 1, "ambiguous": 0, "rejected": 0}
    row = next(r for r in ev.list_events(limit=10) if r["pilot_id"] == ordered_id)
    assert row["filled_quantity"] == 2
    assert row["filled_price"] == 35_850


def test_duplicate_broker_revisions_reject_invalid_time_or_cumulative_regression(tmp_path, monkeypatch):
    from core import toss_live_pilot_events as ev

    monkeypatch.setattr(ev, "_db_path", lambda: tmp_path / "events_revision_regression.db")
    ev._schema_created = False

    def create(pilot_id: str):
        ev.record_event(
            pilot_id=pilot_id,
            event_type="autonomous_live_sent",
            status="live_sent",
            decision_ref=f"execution_decision:{pilot_id}",
            symbol="035720.KS",
            side="buy",
            quantity=3,
            live_order_sent=True,
            adapter_status="enabled",
            live_order_allowed=True,
        )

    regressed_id = "tlive_20260713_120005_1234"
    create(regressed_id)
    common = {
        "client_order_id": regressed_id,
        "symbol": "035720",
        "side": "BUY",
        "quantity": 3,
        "broker_order_status": "PARTIAL",
    }
    regressed = ev.sync_live_event_fills_from_broker_orders([
        {**common, "filled_quantity": 2, "filled_price": 35_900,
         "filled_at": "2026-07-13T12:00:01+09:00"},
        {**common, "filled_quantity": 1, "filled_price": 35_850,
         "filled_at": "2026-07-13T12:00:02+09:00"},
    ])
    assert regressed == {"updated": 0, "ambiguous": 1, "rejected": 0}

    invalid_time_id = "tlive_20260713_120006_1234"
    create(invalid_time_id)
    common["client_order_id"] = invalid_time_id
    invalid_time = ev.sync_live_event_fills_from_broker_orders([
        {**common, "filled_quantity": 1, "filled_price": 35_900,
         "filled_at": "not-a-time"},
        {**common, "filled_quantity": 2, "filled_price": 35_850,
         "filled_at": "also-not-a-time"},
    ])
    assert invalid_time == {"updated": 0, "ambiguous": 1, "rejected": 0}
