"""Shadow decision/outcome append-only storage contract tests."""

from __future__ import annotations

import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier


UTC = timezone.utc
KST = timezone(timedelta(hours=9))


def _kst(hour: int) -> datetime:
    return datetime(2026, 7, 15, hour, 0, tzinfo=KST)


def _decision_values() -> dict:
    return {
        "decision_id": "shadow_decision_20260715_005930_buy",
        "decision_ref": "quality:005930:20260715T000000Z",
        "symbol": "005930.KS",
        "side": "BUY",
        "decided_at_utc": _kst(9),
        "production_bucket": "HOLD",
        "production_score": 68.5,
        "feature_set_version": "shadow-v1",
        "features": {"foreign_net_buy_krw": 123_000_000, "fallback_used": False},
        "source_snapshots": [
            {
                "snapshot_id": "srcobs_" + "a" * 64,
                "source": "kis_investor_flow",
                "ingested_at_utc": _kst(8),
                "payload_sha256": "b" * 64,
            }
        ],
        "candidate_snapshot_sha256": "c" * 64,
    }


def test_kst_decision_is_stored_as_utc_and_exact_duplicate_is_idempotent(tmp_path):
    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(10),
    )

    first = store.append_decision(**_decision_values())
    duplicate = store.append_decision(**_decision_values())
    stored = store.get_decision(first.decision_id)

    assert first.inserted is True
    assert duplicate.inserted is False
    assert duplicate.id == first.id
    assert stored is not None
    assert stored.decided_at_utc == "2026-07-15T00:00:00+00:00"
    assert stored.created_at_utc == "2026-07-15T01:00:00+00:00"
    assert stored.source_snapshots[0]["ingested_at_utc"] == "2026-07-14T23:00:00+00:00"


def test_autoincrement_sequence_and_dynamic_readback_match_inserted_rows(tmp_path):
    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    store = ShadowMeasurementStore(db_path, now_fn=lambda: _kst(10))
    first = store.append_decision(**_decision_values())
    second_values = _decision_values()
    second_values.update(
        decision_id="shadow_decision_20260715_000660_buy",
        decision_ref="quality:000660:20260715T000000Z",
        symbol="000660.KS",
        candidate_snapshot_sha256="d" * 64,
    )
    second = store.append_decision(**second_values)
    outcome = store.append_outcome(
        decision_id=first.decision_id,
        horizon="1d",
        evaluated_at_utc=_kst(10),
        outcome={"return_pct_after_cost": 1.0},
    )

    assert (first.id, second.id, outcome.id) == (1, 2, 1)
    stored_second = store.get_decision(second.decision_id)
    stored_outcome = store.get_outcome(first.decision_id, "1d")
    assert stored_second is not None
    assert stored_outcome is not None
    assert stored_second.symbol == "000660.KS"
    assert stored_outcome.outcome == {"return_pct_after_cost": 1.0}
    with sqlite3.connect(db_path) as conn:
        sequences = dict(conn.execute("SELECT name, seq FROM sqlite_sequence"))
    assert sequences == {"shadow_decisions": 2, "shadow_outcomes": 1}


def test_decision_rejects_nested_secret_key(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(10),
    )
    values = _decision_values()
    values["features"] = {"safe": {"api_key": "fixture-secret"}}

    with pytest.raises(ValueError, match="payload_sensitive_key"):
        store.append_decision(**values)


def test_decision_rejects_obfuscated_sensitive_keys(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(10),
    )
    for key in (
        "clientSecret",
        "api key",
        "api_key_hint",
        "hint.api-key",
        "appkey",
        "accountno",
        "clientsecret",
        "privatekey",
    ):
        values = _decision_values()
        values["features"] = {key: "synthetic-fixture"}
        with pytest.raises(ValueError, match="payload_sensitive_key"):
            store.append_decision(**values)


def test_decision_allows_benign_near_miss_keys(tmp_path):
    from core.shadow_measurements import ShadowMeasurementStore

    for index, key in enumerate(
        (
            "account_notice",
            "tokenizer_version",
            "secretary_sentiment",
            "private_keystone",
        )
    ):
        store = ShadowMeasurementStore(
            tmp_path / f"benign-key-{index}.db",
            now_fn=lambda: _kst(10),
        )
        values = _decision_values()
        values["features"] = {key: 1}
        assert store.append_decision(**values).inserted is True


def test_decision_rejects_secret_like_metadata_values(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    token = "ghp_" + "A" * 24
    cases = {
        "decision_id": lambda value: value.update(decision_id=token),
        "decision_ref": lambda value: value.update(decision_ref=token),
        "symbol": lambda value: value.update(symbol=token.upper()),
        "production_bucket": lambda value: value.update(production_bucket=token),
        "feature_set_version": lambda value: value.update(feature_set_version=token),
        "source": lambda value: value["source_snapshots"][0].update(
            source=token.lower()
        ),
    }
    for field, mutate in cases.items():
        values = _decision_values()
        mutate(values)
        store = ShadowMeasurementStore(
            tmp_path / f"sensitive-{field}.db",
            now_fn=lambda: _kst(10),
        )
        with pytest.raises(ValueError, match=f"{field}_sensitive"):
            store.append_decision(**values)

    for index, sensitive_ref in enumerate(
        (
            "account_no:12345678",
            "app_key:synthetic-value",
            "accountno:12345678",
            "privatekey:synthetic-value",
        )
    ):
        values = _decision_values()
        values["decision_ref"] = sensitive_ref
        store = ShadowMeasurementStore(
            tmp_path / f"sensitive-ref-{index}.db",
            now_fn=lambda: _kst(10),
        )
        with pytest.raises(ValueError, match="decision_ref_sensitive"):
            store.append_decision(**values)


def test_decision_rejects_numeric_json_key_with_typed_error(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(10),
    )
    values = _decision_values()
    values["features"] = {"safe": {1: "ambiguous"}}

    with pytest.raises(ValueError, match="payload_key_not_string"):
        store.append_decision(**values)


def test_decision_rejects_secret_like_nested_string_value(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(10),
    )
    for sensitive_value in (
        "ghp_" + "A" * 36,
        "app_key=synthetic-value",
        "account_no:12345678",
        "private_key=synthetic-value",
    ):
        values = _decision_values()
        values["features"] = {"note": sensitive_value}
        with pytest.raises(ValueError, match="payload_sensitive_value"):
            store.append_decision(**values)


def test_outcome_is_append_only_fk_bound_and_exact_duplicate_is_idempotent(tmp_path):
    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(11),
    )
    decision = store.append_decision(**_decision_values())
    values = {
        "decision_id": decision.decision_id,
        "horizon": "1d",
        "evaluated_at_utc": _kst(10),
        "outcome": {
            "return_pct_after_cost": 1.25,
            "mfe_pct": 2.0,
            "mae_pct": -0.4,
            "source_snapshot_id": "srcobs_" + "d" * 64,
        },
    }

    first = store.append_outcome(**values)
    duplicate = store.append_outcome(**values)
    stored = store.get_outcome(decision.decision_id, "1d")

    assert first.inserted is True
    assert duplicate.inserted is False
    assert duplicate.id == first.id
    assert stored is not None
    assert stored.evaluated_at_utc == "2026-07-15T01:00:00+00:00"
    assert stored.outcome["return_pct_after_cost"] == 1.25


def test_outcome_rejects_horizon_outside_project_allowlist(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(11),
    )
    decision = store.append_decision(**_decision_values())

    with pytest.raises(ValueError, match="outcome_horizon_invalid"):
        store.append_outcome(
            decision_id=decision.decision_id,
            horizon="2d",
            evaluated_at_utc=_kst(10),
            outcome={"return_pct_after_cost": 1.0},
        )


def test_outcome_rejects_invalid_decision_identity_before_lookup(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(12),
    )
    with pytest.raises(ValueError, match="decision_id_invalid"):
        store.append_outcome(
            decision_id="../invalid",
            horizon="1d",
            evaluated_at_utc=_kst(10),
            outcome={"return_pct": 1.25},
        )


def test_database_blocks_update_and_delete_for_decisions_and_outcomes(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    store = ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))
    decision = store.append_decision(**_decision_values())
    store.append_outcome(
        decision_id=decision.decision_id,
        horizon="1d",
        evaluated_at_utc=_kst(10),
        outcome={"return_pct_after_cost": 1.0},
    )

    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="shadow_decisions_append_only"):
            conn.execute(
                "UPDATE shadow_decisions SET production_score = 0 WHERE decision_id = ?",
                (decision.decision_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="shadow_decisions_append_only"):
            conn.execute(
                "DELETE FROM shadow_decisions WHERE decision_id = ?",
                (decision.decision_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="shadow_outcomes_append_only"):
            conn.execute(
                "UPDATE shadow_outcomes SET horizon = '3d' WHERE decision_id = ?",
                (decision.decision_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="shadow_outcomes_append_only"):
            conn.execute(
                "DELETE FROM shadow_outcomes WHERE decision_id = ?",
                (decision.decision_id,),
            )


def test_precreated_malformed_schema_fails_closed(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE shadow_decisions (id INTEGER PRIMARY KEY, decision_id TEXT)"
        )

    with pytest.raises(RuntimeError, match="shadow_measurements_schema_incompatible"):
        ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))


def test_precreated_decision_table_without_unique_identity_fails_closed(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE shadow_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL,
                decision_ref TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                decided_at_utc TEXT NOT NULL,
                production_bucket TEXT NOT NULL,
                production_score REAL NOT NULL,
                feature_set_version TEXT NOT NULL,
                features_json TEXT NOT NULL,
                source_snapshots_json TEXT NOT NULL,
                candidate_snapshot_sha256 TEXT NOT NULL,
                immutable_payload_sha256 TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            )
            """
        )

    with pytest.raises(
        RuntimeError,
        match="shadow_measurements_schema_incompatible:decision_unique",
    ):
        ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))


def test_precreated_nocase_unique_identity_fails_closed(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE shadow_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL COLLATE NOCASE UNIQUE,
                decision_ref TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                decided_at_utc TEXT NOT NULL,
                production_bucket TEXT NOT NULL,
                production_score REAL NOT NULL,
                feature_set_version TEXT NOT NULL,
                features_json TEXT NOT NULL,
                source_snapshots_json TEXT NOT NULL,
                candidate_snapshot_sha256 TEXT NOT NULL,
                immutable_payload_sha256 TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            )
            """
        )

    with pytest.raises(
        RuntimeError,
        match="shadow_measurements_schema_incompatible:decision_unique",
    ):
        ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))


def test_precreated_extra_unique_constraint_fails_closed(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE UNIQUE INDEX hostile_unique_symbol ON shadow_decisions(symbol)"
        )

    with pytest.raises(
        RuntimeError,
        match="shadow_measurements_schema_incompatible:decision_unique",
    ):
        ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))


def test_precreated_decision_table_with_default_fails_closed(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE shadow_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL UNIQUE,
                decision_ref TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                decided_at_utc TEXT NOT NULL,
                production_bucket TEXT NOT NULL,
                production_score REAL NOT NULL DEFAULT 0,
                feature_set_version TEXT NOT NULL,
                features_json TEXT NOT NULL,
                source_snapshots_json TEXT NOT NULL,
                candidate_snapshot_sha256 TEXT NOT NULL,
                immutable_payload_sha256 TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            )
            """
        )

    with pytest.raises(
        RuntimeError,
        match="shadow_measurements_schema_incompatible:decisions",
    ):
        ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))


def test_precreated_decision_table_without_autoincrement_fails_closed(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE shadow_decisions (
                id INTEGER PRIMARY KEY,
                decision_id TEXT NOT NULL UNIQUE,
                decision_ref TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                decided_at_utc TEXT NOT NULL,
                production_bucket TEXT NOT NULL,
                production_score REAL NOT NULL,
                feature_set_version TEXT NOT NULL,
                features_json TEXT NOT NULL,
                source_snapshots_json TEXT NOT NULL,
                candidate_snapshot_sha256 TEXT NOT NULL,
                immutable_payload_sha256 TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            )
            """
        )

    with pytest.raises(
        RuntimeError,
        match="shadow_measurements_schema_incompatible:decision_table_sql",
    ):
        ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))


def test_extra_partial_expression_unique_and_hidden_column_fail_closed(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    mutations = {
        "partial": (
            "CREATE UNIQUE INDEX extra_partial ON shadow_decisions(symbol) "
            "WHERE side = 'BUY'",
            "decision_unique",
        ),
        "expression": (
            "CREATE UNIQUE INDEX extra_expression ON shadow_decisions(lower(symbol))",
            "decision_unique",
        ),
        "hidden": (
            "ALTER TABLE shadow_decisions ADD COLUMN hidden_marker TEXT "
            "GENERATED ALWAYS AS ('x') VIRTUAL",
            "decisions",
        ),
    }
    for label, (sql, expected_error) in mutations.items():
        db_path = tmp_path / f"{label}.db"
        ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))
        with sqlite3.connect(db_path) as conn:
            conn.execute(sql)
        with pytest.raises(
            RuntimeError,
            match=f"shadow_measurements_schema_incompatible:{expected_error}",
        ):
            ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))


def test_wrong_trigger_timing_operation_table_and_condition_fail_closed(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    trigger_name = "trg_shadow_decisions_no_update"
    variants = {
        "timing": """
            CREATE TRIGGER trg_shadow_decisions_no_update
            AFTER UPDATE ON shadow_decisions
            BEGIN SELECT RAISE(ABORT, 'shadow_decisions_append_only'); END
        """,
        "operation": """
            CREATE TRIGGER trg_shadow_decisions_no_update
            BEFORE INSERT ON shadow_decisions
            BEGIN SELECT RAISE(ABORT, 'shadow_decisions_append_only'); END
        """,
        "table": """
            CREATE TRIGGER trg_shadow_decisions_no_update
            BEFORE UPDATE ON shadow_outcomes
            BEGIN SELECT RAISE(ABORT, 'shadow_decisions_append_only'); END
        """,
        "condition": """
            CREATE TRIGGER trg_shadow_decisions_no_update
            BEFORE UPDATE ON shadow_decisions WHEN OLD.id > 0
            BEGIN SELECT RAISE(ABORT, 'shadow_decisions_append_only'); END
        """,
    }
    for label, sql in variants.items():
        db_path = tmp_path / f"trigger-{label}.db"
        ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))
        with sqlite3.connect(db_path) as conn:
            conn.execute(f"DROP TRIGGER {trigger_name}")
            conn.execute(sql)
        with pytest.raises(
            RuntimeError,
            match=f"shadow_measurements_schema_incompatible:trigger:{trigger_name}",
        ):
            ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))


def test_precreated_forged_append_only_trigger_fails_closed(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            DROP TRIGGER trg_shadow_decisions_no_update;
            CREATE TRIGGER trg_shadow_decisions_no_update
            BEFORE UPDATE ON shadow_decisions
            BEGIN
                SELECT 1;
            END;
            """
        )

    with pytest.raises(
        RuntimeError,
        match="shadow_measurements_schema_incompatible:trigger",
    ):
        ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))


def test_precreated_extra_trigger_fails_closed(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER hostile_shadow_insert
            BEFORE INSERT ON shadow_decisions
            BEGIN
                SELECT RAISE(ABORT, 'hostile');
            END
            """
        )

    with pytest.raises(
        RuntimeError,
        match="shadow_measurements_schema_incompatible:trigger_set",
    ):
        ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))


def test_precreated_outcome_table_without_foreign_key_fails_closed(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            DROP TRIGGER trg_shadow_outcomes_no_update;
            DROP TRIGGER trg_shadow_outcomes_no_delete;
            DROP TABLE shadow_outcomes;
            CREATE TABLE shadow_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL,
                horizon TEXT NOT NULL,
                evaluated_at_utc TEXT NOT NULL,
                outcome_json TEXT NOT NULL,
                immutable_payload_sha256 TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                UNIQUE(decision_id, horizon)
            );
            """
        )

    with pytest.raises(
        RuntimeError,
        match="shadow_measurements_schema_incompatible:outcome_foreign_key",
    ):
        ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))


def test_decision_rejects_invalid_candidate_snapshot_hash(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(10),
    )
    values = _decision_values()
    values["candidate_snapshot_sha256"] = "not-a-sha256"

    with pytest.raises(ValueError, match="candidate_snapshot_sha256_invalid"):
        store.append_decision(**values)


def test_decision_rejects_invalid_source_snapshot_identity(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(10),
    )
    values = _decision_values()
    values["source_snapshots"][0]["snapshot_id"] = "latest"

    with pytest.raises(ValueError, match="source_snapshot_id_invalid"):
        store.append_decision(**values)


def test_decision_rejects_invalid_source_payload_hash(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(10),
    )
    values = _decision_values()
    values["source_snapshots"][0]["payload_sha256"] = "missing"

    with pytest.raises(ValueError, match="source_payload_sha256_invalid"):
        store.append_decision(**values)


def test_naive_decision_time_is_rejected(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(10),
    )
    values = _decision_values()
    values["decided_at_utc"] = datetime(2026, 7, 15, 9, 0)

    with pytest.raises(ValueError, match="decided_at_utc_must_be_timezone_aware"):
        store.append_decision(**values)


def test_future_source_snapshot_rejects_whole_decision(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(11),
    )
    values = _decision_values()
    values["source_snapshots"][0]["ingested_at_utc"] = _kst(10)

    with pytest.raises(ValueError, match="source_snapshot_from_future"):
        store.append_decision(**values)
    with sqlite3.connect(tmp_path / "shadow_measurements.db") as conn:
        assert conn.execute("SELECT count(*) FROM shadow_decisions").fetchone()[0] == 0


def test_timestamp_equality_is_allowed_and_one_microsecond_reversal_is_rejected(
    tmp_path,
):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    boundary = _kst(9)
    equal_store = ShadowMeasurementStore(
        tmp_path / "equal.db",
        now_fn=lambda: boundary,
    )
    equal_values = _decision_values()
    equal_values["source_snapshots"][0]["ingested_at_utc"] = boundary
    decision = equal_store.append_decision(**equal_values)
    outcome = equal_store.append_outcome(
        decision_id=decision.decision_id,
        horizon="5m",
        evaluated_at_utc=boundary,
        outcome={"return_pct_after_cost": 0.0},
    )
    assert decision.inserted is True
    assert outcome.inserted is True

    early_store = ShadowMeasurementStore(
        tmp_path / "early-decision.db",
        now_fn=lambda: boundary - timedelta(microseconds=1),
    )
    with pytest.raises(ValueError, match="created_at_before_decision"):
        early_store.append_decision(**_decision_values())

    future_source_store = ShadowMeasurementStore(
        tmp_path / "future-source.db",
        now_fn=lambda: boundary + timedelta(hours=1),
    )
    future_source_values = _decision_values()
    future_source_values["source_snapshots"][0]["ingested_at_utc"] = (
        boundary + timedelta(microseconds=1)
    )
    with pytest.raises(ValueError, match="source_snapshot_from_future"):
        future_source_store.append_decision(**future_source_values)

    clock = [boundary]
    early_outcome_store = ShadowMeasurementStore(
        tmp_path / "early-outcome.db",
        now_fn=lambda: clock[0],
    )
    early_decision = early_outcome_store.append_decision(**_decision_values())
    clock[0] = boundary + timedelta(hours=1) - timedelta(microseconds=1)
    with pytest.raises(ValueError, match="created_at_before_outcome"):
        early_outcome_store.append_outcome(
            decision_id=early_decision.decision_id,
            horizon="30m",
            evaluated_at_utc=boundary + timedelta(hours=1),
            outcome={"return_pct_after_cost": 0.1},
        )


def test_outcome_before_decision_is_rejected(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(11),
    )
    decision = store.append_decision(**_decision_values())

    with pytest.raises(ValueError, match="outcome_before_decision"):
        store.append_outcome(
            decision_id=decision.decision_id,
            horizon="1d",
            evaluated_at_utc=_kst(8),
            outcome={"return_pct_after_cost": 1.0},
        )


def test_changed_outcome_evaluation_time_is_a_conflict(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(11),
    )
    decision = store.append_decision(**_decision_values())
    store.append_outcome(
        decision_id=decision.decision_id,
        horizon="1d",
        evaluated_at_utc=_kst(10),
        outcome={"return_pct_after_cost": 1.0},
    )

    with pytest.raises(ValueError, match="shadow_outcome_conflict"):
        store.append_outcome(
            decision_id=decision.decision_id,
            horizon="1d",
            evaluated_at_utc=datetime(2026, 7, 15, 10, 30, tzinfo=KST),
            outcome={"return_pct_after_cost": 1.0},
        )


def test_direct_sql_orphan_outcome_is_blocked_by_foreign_key(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    ShadowMeasurementStore(db_path, now_fn=lambda: _kst(11))
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                """
                INSERT INTO shadow_outcomes (
                    decision_id, horizon, evaluated_at_utc, outcome_json,
                    immutable_payload_sha256, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "missing-decision",
                    "1d",
                    "2026-07-15T01:00:00+00:00",
                    "{}",
                    "e" * 64,
                    "2026-07-15T02:00:00+00:00",
                ),
            )


def test_decision_rejects_non_finite_feature_with_typed_error(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(10),
    )
    values = _decision_values()
    values["features"] = {"score": float("nan")}

    with pytest.raises(ValueError, match="payload_non_finite"):
        store.append_decision(**values)


def test_decision_rejects_string_boolean_feature(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(10),
    )
    values = _decision_values()
    values["features"] = {"fallback_used": "false"}

    with pytest.raises(ValueError, match="payload_string_boolean"):
        store.append_decision(**values)


def test_concurrent_identical_decisions_converge_to_one_row(tmp_path):
    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    store = ShadowMeasurementStore(db_path, now_fn=lambda: _kst(10))
    barrier = Barrier(8)

    def append_once():
        barrier.wait()
        return store.append_decision(**_decision_values())

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: append_once(), range(8)))

    assert sum(result.inserted for result in results) == 1
    assert len({result.id for result in results}) == 1
    assert len({result.immutable_payload_sha256 for result in results}) == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM shadow_decisions").fetchone()[0] == 1


def test_concurrent_conflicting_decisions_return_typed_conflicts(tmp_path):
    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    store = ShadowMeasurementStore(db_path, now_fn=lambda: _kst(10))
    barrier = Barrier(8)

    def append_variant(index: int):
        values = _decision_values()
        values["features"] = {"variant": index}
        barrier.wait()
        try:
            return "ok", store.append_decision(**values)
        except Exception as exc:
            return "error", exc

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(append_variant, range(8)))

    successes = [value for status, value in results if status == "ok"]
    errors = [value for status, value in results if status == "error"]
    assert len(successes) == 1
    success = successes[0]
    assert not isinstance(success, Exception)
    assert success.inserted is True
    assert len(errors) == 7
    assert all(type(error) is ValueError for error in errors)
    assert all(str(error) == "shadow_decision_conflict" for error in errors)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM shadow_decisions").fetchone()[0] == 1


def test_decision_immutable_hash_binds_every_baseline_and_source_field(tmp_path):
    from core.shadow_measurements import ShadowMeasurementStore

    def stored_hash(label: str, values: dict) -> str:
        store = ShadowMeasurementStore(
            tmp_path / f"{label}.db",
            now_fn=lambda: _kst(10),
        )
        return store.append_decision(**values).immutable_payload_sha256

    baseline_values = _decision_values()
    baseline_hash = stored_hash("baseline", baseline_values)
    mutations = {
        "decision_id": lambda value: value.update(
            decision_id="shadow_decision_20260715_005930_sell"
        ),
        "decision_ref": lambda value: value.update(decision_ref="quality:changed"),
        "symbol": lambda value: value.update(symbol="000660.KS"),
        "side": lambda value: value.update(side="SELL"),
        "decided_at": lambda value: value.update(
            decided_at_utc=_kst(9) + timedelta(minutes=1)
        ),
        "production_bucket": lambda value: value.update(production_bucket="BUY"),
        "production_score": lambda value: value.update(production_score=68.6),
        "feature_set_version": lambda value: value.update(
            feature_set_version="shadow-v2"
        ),
        "features": lambda value: value.update(features={"changed": 1}),
        "source_snapshot_id": lambda value: value["source_snapshots"][0].update(
            snapshot_id="srcobs_" + "e" * 64
        ),
        "source": lambda value: value["source_snapshots"][0].update(
            source="naver_investor_flow"
        ),
        "source_ingested_at": lambda value: value["source_snapshots"][0].update(
            ingested_at_utc=_kst(8) + timedelta(minutes=1)
        ),
        "source_payload_hash": lambda value: value["source_snapshots"][0].update(
            payload_sha256="f" * 64
        ),
        "candidate_hash": lambda value: value.update(
            candidate_snapshot_sha256="d" * 64
        ),
    }

    observed_hashes = []
    for label, mutate in mutations.items():
        values = copy.deepcopy(baseline_values)
        mutate(values)
        observed_hashes.append(stored_hash(label, values))

    assert all(value != baseline_hash for value in observed_hashes)
    assert len(set(observed_hashes)) == len(mutations)


def test_decision_rejects_invalid_identity_metadata(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    invalid_cases = {
        "decision_id": (
            lambda value: value.update(decision_id=""),
            "decision_id_invalid",
        ),
        "decision_ref": (
            lambda value: value.update(decision_ref=""),
            "decision_ref_invalid",
        ),
        "symbol": (
            lambda value: value.update(symbol="../005930"),
            "symbol_invalid",
        ),
        "side": (
            lambda value: value.update(side="HOLD"),
            "side_invalid",
        ),
        "production_bucket": (
            lambda value: value.update(production_bucket=""),
            "production_bucket_invalid",
        ),
        "feature_set_version": (
            lambda value: value.update(feature_set_version=""),
            "feature_set_version_invalid",
        ),
        "source": (
            lambda value: value["source_snapshots"][0].update(source=""),
            "source_invalid",
        ),
    }

    for label, (mutate, expected_error) in invalid_cases.items():
        values = _decision_values()
        mutate(values)
        store = ShadowMeasurementStore(
            tmp_path / f"invalid-{label}.db",
            now_fn=lambda: _kst(10),
        )
        with pytest.raises(ValueError, match=expected_error):
            store.append_decision(**values)


def test_source_lineage_order_is_canonical_for_idempotency(tmp_path):
    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(10),
    )
    values = _decision_values()
    values["source_snapshots"].append(
        {
            "snapshot_id": "srcobs_" + "e" * 64,
            "source": "naver_investor_flow",
            "ingested_at_utc": _kst(8) + timedelta(minutes=30),
            "payload_sha256": "f" * 64,
        }
    )

    first = store.append_decision(**values)
    reversed_values = copy.deepcopy(values)
    reversed_values["source_snapshots"].reverse()
    second = store.append_decision(**reversed_values)

    assert first.inserted is True
    assert second.inserted is False
    assert second.id == first.id
    assert second.immutable_payload_sha256 == first.immutable_payload_sha256


def test_duplicate_source_snapshot_identity_is_rejected(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(10),
    )
    values = _decision_values()
    values["source_snapshots"].append(copy.deepcopy(values["source_snapshots"][0]))

    with pytest.raises(ValueError, match="source_snapshot_duplicate"):
        store.append_decision(**values)


def test_concurrent_identical_outcomes_converge_to_one_row(tmp_path):
    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    store = ShadowMeasurementStore(db_path, now_fn=lambda: _kst(12))
    decision_values = _decision_values()
    store.append_decision(**decision_values)
    decision_id = decision_values["decision_id"]
    barrier = Barrier(8)

    def append_once():
        barrier.wait()
        return store.append_outcome(
            decision_id=decision_id,
            horizon="1d",
            evaluated_at_utc=_kst(10),
            outcome={"return_pct": 1.25},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: append_once(), range(8)))

    assert sum(result.inserted for result in results) == 1
    assert len({result.id for result in results}) == 1
    assert len({result.immutable_payload_sha256 for result in results}) == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM shadow_outcomes").fetchone()[0] == 1


def test_concurrent_conflicting_outcomes_return_typed_conflicts(tmp_path):
    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    store = ShadowMeasurementStore(db_path, now_fn=lambda: _kst(12))
    decision_values = _decision_values()
    store.append_decision(**decision_values)
    decision_id = decision_values["decision_id"]
    barrier = Barrier(8)

    def append_variant(index: int):
        barrier.wait()
        try:
            return "ok", store.append_outcome(
                decision_id=decision_id,
                horizon="1d",
                evaluated_at_utc=_kst(10),
                outcome={"variant": index},
            )
        except Exception as exc:
            return "error", exc

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(append_variant, range(8)))

    successes = [value for status, value in results if status == "ok"]
    errors = [value for status, value in results if status == "error"]
    assert len(successes) == 1
    success = successes[0]
    assert not isinstance(success, Exception)
    assert success.inserted is True
    assert len(errors) == 7
    assert all(type(error) is ValueError for error in errors)
    assert all(str(error) == "shadow_outcome_conflict" for error in errors)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM shadow_outcomes").fetchone()[0] == 1


def test_outcome_immutable_hash_binds_identity_time_and_payload(tmp_path):
    from core.shadow_measurements import ShadowMeasurementStore

    def stored_hash(
        label: str,
        *,
        decision_id: str,
        horizon: str,
        evaluated_at_utc: datetime,
        outcome: dict,
    ) -> str:
        store = ShadowMeasurementStore(
            tmp_path / f"outcome-{label}.db",
            now_fn=lambda: _kst(12),
        )
        decision_values = _decision_values()
        decision_values["decision_id"] = decision_id
        store.append_decision(**decision_values)
        return store.append_outcome(
            decision_id=decision_id,
            horizon=horizon,
            evaluated_at_utc=evaluated_at_utc,
            outcome=outcome,
        ).immutable_payload_sha256

    baseline_args = {
        "decision_id": _decision_values()["decision_id"],
        "horizon": "1d",
        "evaluated_at_utc": _kst(10),
        "outcome": {"return_pct": 1.25},
    }
    baseline_hash = stored_hash("baseline", **baseline_args)
    mutations = {
        "decision_id": {**baseline_args, "decision_id": "shadow_decision_changed"},
        "horizon": {**baseline_args, "horizon": "3d"},
        "evaluated_at": {
            **baseline_args,
            "evaluated_at_utc": _kst(10) + timedelta(minutes=1),
        },
        "outcome": {**baseline_args, "outcome": {"return_pct": 1.26}},
    }

    observed_hashes = [
        stored_hash(label, **values) for label, values in mutations.items()
    ]
    assert all(value != baseline_hash for value in observed_hashes)
    assert len(set(observed_hashes)) == len(mutations)


def test_source_outcome_and_created_timestamps_require_timezone_awareness(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    source_store = ShadowMeasurementStore(
        tmp_path / "naive-source.db",
        now_fn=lambda: _kst(12),
    )
    source_values = _decision_values()
    source_values["source_snapshots"][0]["ingested_at_utc"] = datetime(
        2026, 7, 15, 8
    )
    with pytest.raises(
        ValueError,
        match="source_snapshot_ingested_at_utc_must_be_timezone_aware",
    ):
        source_store.append_decision(**source_values)

    outcome_store = ShadowMeasurementStore(
        tmp_path / "naive-outcome.db",
        now_fn=lambda: _kst(12),
    )
    decision = outcome_store.append_decision(**_decision_values())
    with pytest.raises(ValueError, match="evaluated_at_utc_must_be_timezone_aware"):
        outcome_store.append_outcome(
            decision_id=decision.decision_id,
            horizon="1d",
            evaluated_at_utc=datetime(2026, 7, 15, 10),
            outcome={"return_pct": 1.25},
        )

    created_store = ShadowMeasurementStore(
        tmp_path / "naive-created.db",
        now_fn=lambda: datetime(2026, 7, 15, 12),
    )
    with pytest.raises(ValueError, match="created_at_utc_must_be_timezone_aware"):
        created_store.append_decision(**_decision_values())


def test_outcome_rejects_nested_secret_sink(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    store = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _kst(12),
    )
    decision = store.append_decision(**_decision_values())
    with pytest.raises(ValueError, match="payload_sensitive_key"):
        store.append_outcome(
            decision_id=decision.decision_id,
            horizon="1d",
            evaluated_at_utc=_kst(10),
            outcome={"nested": {"authorization": "fixture-secret"}},
        )


def test_get_decision_rejects_hostile_preloaded_forged_hash(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    store = ShadowMeasurementStore(db_path, now_fn=lambda: _kst(12))
    features = {"safe": 1}
    sources = [
        {
            "snapshot_id": "srcobs_" + "a" * 64,
            "source": "kis_investor_flow",
            "ingested_at_utc": "2026-07-14T23:00:00+00:00",
            "payload_sha256": "b" * 64,
        }
    ]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO shadow_decisions (
                decision_id, decision_ref, symbol, side, decided_at_utc,
                production_bucket, production_score, feature_set_version,
                features_json, source_snapshots_json,
                candidate_snapshot_sha256, immutable_payload_sha256,
                created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "hostile_decision",
                "quality:hostile",
                "005930.KS",
                "BUY",
                "2026-07-15T00:00:00+00:00",
                "HOLD",
                68.5,
                "shadow-v1",
                json.dumps(features, sort_keys=True, separators=(",", ":")),
                json.dumps(sources, sort_keys=True, separators=(",", ":")),
                "c" * 64,
                "0" * 64,
                "2026-07-15T01:00:00+00:00",
            ),
        )

    with pytest.raises(RuntimeError, match="shadow_decision_corrupt"):
        store.get_decision("hostile_decision")


def test_get_outcome_rejects_hostile_preloaded_forged_hash(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    store = ShadowMeasurementStore(db_path, now_fn=lambda: _kst(12))
    decision = store.append_decision(**_decision_values())
    outcome = {"return_pct": 1.25}
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            INSERT INTO shadow_outcomes (
                decision_id, horizon, evaluated_at_utc, outcome_json,
                immutable_payload_sha256, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                decision.decision_id,
                "1d",
                "2026-07-15T01:00:00+00:00",
                json.dumps(outcome, sort_keys=True, separators=(",", ":")),
                "0" * 64,
                "2026-07-15T03:00:00+00:00",
            ),
        )

    with pytest.raises(RuntimeError, match="shadow_outcome_corrupt"):
        store.get_outcome(decision.decision_id, "1d")


def test_duplicate_decision_revalidates_existing_complete_row(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    store = ShadowMeasurementStore(db_path, now_fn=lambda: _kst(12))
    values = _decision_values()
    store.append_decision(**values)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            DROP TRIGGER trg_shadow_decisions_no_update;
            UPDATE shadow_decisions SET created_at_utc = 'not-a-time';
            CREATE TRIGGER trg_shadow_decisions_no_update
            BEFORE UPDATE ON shadow_decisions
            BEGIN
                SELECT RAISE(ABORT, 'shadow_decisions_append_only');
            END;
            """
        )

    with pytest.raises(RuntimeError, match="shadow_decision_corrupt"):
        store.append_decision(**values)


def test_duplicate_outcome_revalidates_existing_complete_row(tmp_path):
    import pytest

    from core.shadow_measurements import ShadowMeasurementStore

    db_path = tmp_path / "shadow_measurements.db"
    store = ShadowMeasurementStore(db_path, now_fn=lambda: _kst(12))
    decision = store.append_decision(**_decision_values())
    outcome_values = {
        "decision_id": decision.decision_id,
        "horizon": "1d",
        "evaluated_at_utc": _kst(10),
        "outcome": {"return_pct": 1.25},
    }
    store.append_outcome(**outcome_values)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            DROP TRIGGER trg_shadow_outcomes_no_update;
            UPDATE shadow_outcomes SET created_at_utc = 'not-a-time';
            CREATE TRIGGER trg_shadow_outcomes_no_update
            BEFORE UPDATE ON shadow_outcomes
            BEGIN
                SELECT RAISE(ABORT, 'shadow_outcomes_append_only');
            END;
            """
        )

    with pytest.raises(RuntimeError, match="shadow_outcome_corrupt"):
        store.append_outcome(**outcome_values)
