"""Exact append-only source observation v2 storage contracts."""

from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.source_observations_v2 import SourceObservationStoreV2

UTC = timezone.utc


def _at(day: int, *, hour: int = 12, microsecond: int = 123456) -> datetime:
    return datetime(2026, 7, day, hour, 0, 0, microsecond, tzinfo=UTC)


def _observation(**overrides):
    values = {
        "source": "sec_companyfacts",
        "dataset": "company_facts",
        "source_record_id": "MRVL:2026Q2:revenue",
        "symbol": "MRVL",
        "market": "US",
        "currency_or_unit": "USD",
        "source_as_of": _at(14),
        "source_event_sequence": 0,
        "ingested_at": _at(14),
        "schema_version": 1,
        "transform_version": 1,
        "fallback_used": False,
        "payload": {"revenue": 100, "label": "매출"},
    }
    values.update(overrides)
    return values


def _collection_run(**overrides):
    values = {
        "source": "sec_companyfacts",
        "dataset": "company_facts",
        "run_id": "sec:20260714T120000Z",
        "started_at": _at(14, hour=11),
        "completed_at": _at(14),
        "status": "success",
        "rows_seen": 2,
        "rows_inserted": 1,
        "rows_duplicate": 1,
        "rows_skipped": 0,
        "rows_invalid": 0,
        "error_type": "",
    }
    values.update(overrides)
    return values


def _database_state(db_path):
    contents = db_path.read_bytes()
    stat = db_path.stat()
    sidecars = tuple(
        sorted(path.name for path in db_path.parent.glob(f"{db_path.name}-*"))
    )
    return {
        "contents": contents,
        "sha256": hashlib.sha256(contents).hexdigest(),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sidecars": sidecars,
    }


def _database_family_state(db_path):
    paths = (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm"))
    return {
        path.name: {
            "contents": path.read_bytes(),
            "size": path.stat().st_size,
            "mtime_ns": path.stat().st_mtime_ns,
        }
        for path in paths
        if path.exists()
    }


def _assert_schema_rejected_without_changes(db_path):
    before = _database_state(db_path)
    with pytest.raises(ValueError, match="source_observation_v2_schema_invalid"):
        SourceObservationStoreV2(db_path)
    assert _database_state(db_path) == before


def _execute_sql_and_close(db_path, sql):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


def _replace_schema_sql_and_close(db_path, object_type, name, old, new):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
            (object_type, name),
        ).fetchone()
        assert row is not None and row[0] is not None and old in row[0]
        schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.execute("PRAGMA writable_schema = ON")
        conn.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type = ? AND name = ?",
            (row[0].replace(old, new, 1), object_type, name),
        )
        conn.execute(f"PRAGMA schema_version = {schema_version + 1}")
        conn.execute("PRAGMA writable_schema = OFF")
        conn.commit()
    finally:
        conn.close()


def test_exact_retry_keeps_first_ingestion_and_changed_payload_is_revision(tmp_path):
    db_path = tmp_path / "source_observations_v2.sqlite3"
    store = SourceObservationStoreV2(db_path)

    first = store.append(**_observation())
    duplicate = store.append(
        **_observation(
            ingested_at=_at(15),
            payload={"label": "매출", "revenue": 100},
        )
    )
    revision = store.append(
        **_observation(ingested_at=_at(16), payload={"revenue": 101})
    )

    assert first.inserted is True
    assert duplicate.inserted is False
    assert duplicate.id == first.id
    assert duplicate.snapshot_id == first.snapshot_id
    assert revision.inserted is True
    assert revision.snapshot_id != first.snapshot_id
    assert len(first.snapshot_id) == 64

    latest = store.latest_as_of(
        decision_at=_at(17),
        source="sec_companyfacts",
        dataset="company_facts",
        symbol="MRVL",
        market="US",
    )
    assert latest is not None
    assert latest.payload == {"revenue": 101}
    assert latest.ingested_at == "2026-07-16T12:00:00.123456Z"

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
        first_ingested = conn.execute(
            "SELECT ingested_at FROM observations WHERE id = ?", (first.id,)
        ).fetchone()[0]
    assert first_ingested == "2026-07-14T12:00:00.123456Z"


def test_point_in_time_filters_late_ingestion_and_orders_sequence_then_revision(tmp_path):
    store = SourceObservationStoreV2(tmp_path / "pit.sqlite3")
    store.append(**_observation(payload={"value": "known"}))
    store.append(
        **_observation(
            source_event_sequence=1,
            ingested_at=_at(15),
            payload={"value": "sequence-1-first"},
        )
    )
    store.append(
        **_observation(
            source_event_sequence=1,
            ingested_at=_at(16),
            payload={"value": "sequence-1-revision"},
        )
    )

    before_late_rows = store.latest_as_of(
        decision_at=_at(14),
        source="sec_companyfacts",
        dataset="company_facts",
        symbol="MRVL",
        market="US",
    )
    between_revisions = store.latest_as_of(
        decision_at=_at(15),
        source="sec_companyfacts",
        dataset="company_facts",
        symbol="MRVL",
        market="US",
    )
    after_revisions = store.latest_as_of(
        decision_at=_at(17),
        source="sec_companyfacts",
        dataset="company_facts",
        symbol="MRVL",
        market="US",
    )

    assert before_late_rows is not None
    assert before_late_rows.payload == {"value": "known"}
    assert between_revisions is not None
    assert between_revisions.payload == {"value": "sequence-1-first"}
    assert after_revisions is not None
    assert after_revisions.payload == {"value": "sequence-1-revision"}


def test_append_rejects_source_timestamp_after_ingestion(tmp_path):
    store = SourceObservationStoreV2(tmp_path / "ordered.sqlite3")

    with pytest.raises(ValueError, match="source_as_of_after_ingested_at"):
        store.append(**_observation(source_as_of=_at(15), ingested_at=_at(14)))


@pytest.mark.parametrize("sink", ["observation", "collection-run"])
def test_year_one_aware_datetimes_persist_as_four_digit_canonical_utc(tmp_path, sink):
    db_path = tmp_path / f"year-one-{sink}.sqlite3"
    store = SourceObservationStoreV2(db_path)
    ancient = datetime(1, 1, 1, 0, 0, 0, 1, tzinfo=UTC)

    if sink == "observation":
        store.append(**_observation(source_as_of=ancient, ingested_at=ancient))
        table = "observations"
        columns = ("source_as_of", "ingested_at")
    else:
        store.record_collection_run(
            **_collection_run(started_at=ancient, completed_at=ancient)
        )
        table = "collection_runs"
        columns = ("started_at", "completed_at")

    with sqlite3.connect(db_path) as conn:
        stored = conn.execute(
            f"SELECT {', '.join(columns)} FROM {table}"
        ).fetchone()
    assert stored == (
        "0001-01-01T00:00:00.000001Z",
        "0001-01-01T00:00:00.000001Z",
    )
    assert all(len(value) == 27 for value in stored)
    assert all(
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00") == ancient
        for value in stored
    )


@pytest.mark.parametrize(
    "currency_or_unit",
    ["KRW", "USD", "SHARES", "PERCENT", "UNITLESS", "MIXED"],
)
def test_all_exact_units_are_accepted(tmp_path, currency_or_unit):
    store = SourceObservationStoreV2(tmp_path / "units.sqlite3")
    result = store.append(
        **_observation(
            source_record_id=f"MRVL:{currency_or_unit}",
            currency_or_unit=currency_or_unit,
        )
    )
    assert result.inserted is True


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"source": "SEC"}, "source_invalid"),
        ({"dataset": "company facts"}, "dataset_invalid"),
        ({"source_record_id": ""}, "source_record_id_invalid"),
        ({"symbol": "mrvl"}, "symbol_invalid"),
        ({"market": "EU"}, "market_invalid"),
        ({"currency_or_unit": "EUR"}, "currency_or_unit_invalid"),
        ({"source_event_sequence": True}, "source_event_sequence_invalid"),
        ({"source_event_sequence": -1}, "source_event_sequence_invalid"),
        ({"schema_version": True}, "schema_version_invalid"),
        ({"schema_version": 0}, "schema_version_invalid"),
        ({"transform_version": True}, "transform_version_invalid"),
        ({"transform_version": 0}, "transform_version_invalid"),
        ({"fallback_used": 0}, "fallback_used_invalid"),
        ({"payload": [1, 2]}, "payload_must_be_object"),
        ({"source_as_of": datetime(2026, 7, 14)}, "timezone_aware"),
    ],
)
def test_strict_observation_validators(tmp_path, overrides, reason):
    store = SourceObservationStoreV2(tmp_path / "validators.sqlite3")
    with pytest.raises(ValueError, match=reason):
        store.append(**_observation(**overrides))


@pytest.mark.parametrize(
    "key",
    [
        "appkey",
        "App_Key",
        "kis-app key",
        "accountNo",
        "broker_account_no",
        "clientSecret",
        "PRIVATE-KEY",
        "authorization",
        "access_token",
        "dbPassword",
    ],
)
def test_sensitive_key_collapsed_aliases_are_rejected(tmp_path, key):
    store = SourceObservationStoreV2(tmp_path / "sensitive-key.sqlite3")
    with pytest.raises(ValueError, match="payload_sensitive_key"):
        store.append(**_observation(payload={"nested": {key: "redacted"}}))


@pytest.mark.parametrize(
    "value",
    [
        "appKey=must-not-persist",
        "ACCOUNT_NO: 12345678-01",
        "client-secret = must-not-persist",
        "private key: must-not-persist",
        "Authorization: Bearer abcdefghijklmnop",
        "token=must-not-persist",
        "password : must-not-persist",
        "ghp_" + "A" * 36,
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_sensitive_values_and_assignments_are_rejected(tmp_path, value):
    store = SourceObservationStoreV2(tmp_path / "sensitive-value.sqlite3")
    with pytest.raises(ValueError, match="payload_sensitive_value"):
        store.append(**_observation(payload={"description": value}))


def test_sensitive_guard_allows_benign_near_misses(tmp_path):
    store = SourceObservationStoreV2(tmp_path / "benign.sqlite3")
    result = store.append(
        **_observation(
            payload={
                "app_keynote": "theme",
                "account_notice": "published",
                "client_secretary": "office",
                "private_keyboard": "layout",
                "authorization_rate": 0.9,
                "tokenized_count": 12,
                "password_policy": "rotation enabled",
            }
        )
    )
    assert result.inserted is True


@pytest.mark.parametrize(
    "field",
    ["source", "dataset", "source_record_id", "symbol"],
)
def test_observation_rejects_naked_synthetic_pat_in_every_free_text_sink(
    tmp_path, field
):
    synthetic_pat = "ghp_" + ("A" if field == "symbol" else "a") * 24
    if field == "symbol":
        synthetic_pat = synthetic_pat.upper()
    store = SourceObservationStoreV2(tmp_path / f"observation-pat-{field}.sqlite3")

    with pytest.raises(ValueError, match="sensitive"):
        store.append(**_observation(**{field: synthetic_pat}))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_payload_numbers_are_rejected(tmp_path, value):
    store = SourceObservationStoreV2(tmp_path / "finite.sqlite3")
    with pytest.raises(ValueError, match="payload_non_finite"):
        store.append(**_observation(payload={"nested": [value]}))


@pytest.mark.parametrize(
    "payload_json",
    [
        '{"clientSecret":"must-not-persist"}',
        '{"description":"appKey=must-not-persist"}',
        '{"description":"client\\tsecret\\t=must-not-persist"}',
        '{"description":"Bearer abcdefghijkl"}',
        '{"description":"eyJabcdefgh.abcdefgh.abcdefgh"}',
        '{"description":"-----BEGIN PRIVATE KEY-----"}',
    ],
)
def test_raw_sqlite_cannot_persist_sensitive_payload(tmp_path, payload_json):
    db_path = tmp_path / "raw-secret.sqlite3"
    SourceObservationStoreV2(db_path)
    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="sensitive"):
            conn.execute(
                """
                INSERT INTO observations (
                    snapshot_id, source, dataset, source_record_id, symbol, market,
                    currency_or_unit, source_as_of, source_event_sequence, ingested_at,
                    schema_version, transform_version, fallback_used,
                    payload_json, payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "a" * 64,
                    "sec_companyfacts",
                    "company_facts",
                    "MRVL:raw",
                    "MRVL",
                    "US",
                    "USD",
                    "2026-07-14T12:00:00.123456Z",
                    0,
                    "2026-07-14T12:00:00.123456Z",
                    1,
                    1,
                    0,
                    payload_json,
                    "b" * 64,
                ),
            )
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_raw_sqlite_sensitive_guard_allows_benign_near_misses(tmp_path):
    db_path = tmp_path / "raw-benign.sqlite3"
    SourceObservationStoreV2(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO observations (
                snapshot_id, source, dataset, source_record_id, symbol, market,
                currency_or_unit, source_as_of, source_event_sequence, ingested_at,
                schema_version, transform_version, fallback_used,
                payload_json, payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "a" * 64,
                "sec_companyfacts",
                "company_facts",
                "MRVL:raw-benign",
                "MRVL",
                "US",
                "USD",
                "2026-07-14T12:00:00.123456Z",
                0,
                "2026-07-14T12:00:00.123456Z",
                1,
                1,
                0,
                (
                    '{"description":"bearer market commentary",'
                    '"assignment":"app keynote=theme",'
                    '"short":"eyJshort.not.jwt",'
                    '"key_type":"-----BEGIN PUBLIC KEY-----"}'
                ),
                "b" * 64,
            ),
        )
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1


@pytest.mark.parametrize("pat_kind", ["classic", "fine-grained"])
def test_raw_sqlite_rejects_naked_synthetic_pat_payload(tmp_path, pat_kind):
    prefix = "ghp_" if pat_kind == "classic" else "github_pat_"
    payload_json = '{"note":"' + prefix + "A" * 24 + '"}'
    db_path = tmp_path / f"raw-pat-{pat_kind}.sqlite3"
    SourceObservationStoreV2(db_path)

    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="sensitive"):
            conn.execute(
                """
                INSERT INTO observations (
                    snapshot_id, source, dataset, source_record_id, symbol, market,
                    currency_or_unit, source_as_of, source_event_sequence, ingested_at,
                    schema_version, transform_version, fallback_used,
                    payload_json, payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "c" * 64,
                    "sec_companyfacts",
                    "company_facts",
                    "MRVL:raw-pat",
                    "MRVL",
                    "US",
                    "USD",
                    "2026-07-14T12:00:00.123456Z",
                    0,
                    "2026-07-14T12:00:00.123456Z",
                    1,
                    1,
                    0,
                    payload_json,
                    "d" * 64,
                ),
            )
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_observations_are_append_only_even_via_raw_sqlite(tmp_path):
    db_path = tmp_path / "append-only.sqlite3"
    store = SourceObservationStoreV2(db_path)
    result = store.append(**_observation())
    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="observations_append_only"):
            conn.execute(
                "UPDATE observations SET payload_json = '{}' WHERE id = ?",
                (result.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="observations_append_only"):
            conn.execute("DELETE FROM observations WHERE id = ?", (result.id,))


def test_concurrent_exact_duplicates_converge_to_one_row(tmp_path):
    db_path = tmp_path / "concurrent.sqlite3"
    store = SourceObservationStoreV2(db_path)

    def append_once(_index):
        return store.append(**_observation())

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(append_once, range(24)))

    assert sum(result.inserted for result in results) == 1
    assert len({result.id for result in results}) == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("set_clause", "forged_value"),
    [
        ("source = ?", "forged_source"),
        ("payload_json = ?", '{ "revenue": 100, "label": "매출" }'),
        ("payload_sha256 = ?", "f" * 64),
        ("ingested_at = ?", "2026-07-15T12:00:00.123456Z"),
    ],
)
def test_forged_snapshot_collision_is_rejected(tmp_path, set_clause, forged_value):
    db_path = tmp_path / "forged.sqlite3"
    store = SourceObservationStoreV2(db_path)
    values = _observation()
    first = store.append(**values)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TRIGGER trg_observations_no_update")
        conn.execute(f"UPDATE observations SET {set_clause}", (forged_value,))
        conn.execute(
            """
            CREATE TRIGGER trg_observations_no_update
            BEFORE UPDATE ON observations
            BEGIN
                SELECT RAISE(ABORT, 'observations_append_only');
            END
            """
        )

    with pytest.raises(ValueError, match="source_observation_v2_conflict"):
        store.append(**values)
    assert first.inserted is True


@pytest.mark.parametrize(
    "hostile_ingested_at",
    [
        "2026-07-14T12:00:00.123456+00:00",
        "2026-07-13T12:00:00.123456Z",
    ],
    ids=["noncanonical-utc", "before-source-as-of"],
)
def test_duplicate_readback_rejects_hostile_stored_ingested_at(
    tmp_path, hostile_ingested_at
):
    db_path = tmp_path / "hostile-duplicate-time.sqlite3"
    store = SourceObservationStoreV2(db_path)
    values = _observation()
    first = store.append(**values)

    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TRIGGER trg_observations_no_update")
        conn.execute(
            "UPDATE observations SET ingested_at = ? WHERE id = ?",
            (hostile_ingested_at, first.id),
        )
        conn.execute(
            """
            CREATE TRIGGER trg_observations_no_update
            BEFORE UPDATE ON observations
            BEGIN
                SELECT RAISE(ABORT, 'observations_append_only');
            END
            """
        )

    with pytest.raises(ValueError, match="source_observation_v2_conflict"):
        store.latest_as_of(
            decision_at=_at(20),
            source=values["source"],
            dataset=values["dataset"],
            symbol=values["symbol"],
            market=values["market"],
        )
    with pytest.raises(ValueError, match="source_observation_v2_conflict"):
        store.append(**values)


def test_collection_run_exact_retry_conflict_and_latest(tmp_path):
    db_path = tmp_path / "collection-runs.sqlite3"
    store = SourceObservationStoreV2(db_path)
    kst = timezone(timedelta(hours=9))
    values = _collection_run(
        started_at=datetime(2026, 7, 14, 20, 0, 0, 123456, tzinfo=kst),
        completed_at=datetime(2026, 7, 14, 21, 0, 0, 123456, tzinfo=kst),
    )

    first = store.record_collection_run(**values)
    duplicate = store.record_collection_run(**values)

    assert first.inserted is True
    assert duplicate.inserted is False
    assert duplicate.id == first.id
    assert duplicate.fingerprint == first.fingerprint

    latest = store.latest_collection_run("sec_companyfacts", "company_facts")
    assert latest is not None
    assert latest.run_id == values["run_id"]
    assert latest.started_at == "2026-07-14T11:00:00.123456Z"
    assert latest.completed_at == "2026-07-14T12:00:00.123456Z"
    assert latest.status == "success"
    assert latest.rows_seen == 2
    assert latest.rows_inserted == 1
    assert latest.rows_duplicate == 1
    assert latest.error_type == ""
    assert latest.fingerprint == first.fingerprint
    with pytest.raises(FrozenInstanceError):
        setattr(first, "status", "failed")
    with pytest.raises(FrozenInstanceError):
        setattr(latest, "status", "failed")

    with pytest.raises(ValueError, match="collection_run_conflict"):
        store.record_collection_run(
            **_collection_run(rows_inserted=0, rows_duplicate=2)
        )

    with sqlite3.connect(db_path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(collection_runs)")]
    assert columns == [
        "id",
        "source",
        "dataset",
        "run_id",
        "started_at",
        "completed_at",
        "status",
        "rows_seen",
        "rows_inserted",
        "rows_duplicate",
        "rows_skipped",
        "rows_invalid",
        "error_type",
        "fingerprint",
    ]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"status": "unknown"}, "collection_run_status_invalid"),
        ({"rows_seen": True}, "collection_run_count_invalid"),
        ({"rows_invalid": -1}, "collection_run_count_invalid"),
        ({"rows_seen": 3}, "collection_run_count_mismatch"),
        (
            {"started_at": _at(15), "completed_at": _at(14)},
            "collection_run_time_invalid",
        ),
        ({"error_type": "unexpected"}, "collection_run_success_error_type"),
        (
            {"status": "failed", "rows_seen": 0, "rows_inserted": 0, "rows_duplicate": 0},
            "collection_run_failed_error_type",
        ),
        (
            {
                "status": "failed",
                "rows_seen": 0,
                "rows_inserted": 0,
                "rows_duplicate": 0,
                "error_type": "token=must-not-persist",
            },
            "collection_run_error_type_sensitive",
        ),
    ],
)
def test_collection_run_python_validation(tmp_path, overrides, reason):
    store = SourceObservationStoreV2(tmp_path / "run-validation.sqlite3")
    with pytest.raises(ValueError, match=reason):
        store.record_collection_run(**_collection_run(**overrides))


@pytest.mark.parametrize("field", ["source", "dataset", "run_id", "error_type"])
def test_collection_run_rejects_naked_synthetic_pat_in_every_free_text_sink(
    tmp_path, field
):
    synthetic_pat = "github_pat_" + "a" * 24
    overrides = {field: synthetic_pat}
    if field == "error_type":
        overrides["status"] = "partial"
    store = SourceObservationStoreV2(tmp_path / f"run-pat-{field}.sqlite3")

    with pytest.raises(ValueError, match="sensitive"):
        store.record_collection_run(**_collection_run(**overrides))


def test_collection_run_statuses_include_partial_failed_and_skipped(tmp_path):
    store = SourceObservationStoreV2(tmp_path / "run-statuses.sqlite3")
    partial = store.record_collection_run(
        **_collection_run(
            run_id="partial-run",
            status="partial",
            rows_duplicate=0,
            rows_invalid=1,
            error_type="row_invalid",
        )
    )
    failed = store.record_collection_run(
        **_collection_run(
            run_id="failed-run",
            status="failed",
            rows_seen=0,
            rows_inserted=0,
            rows_duplicate=0,
            error_type="fetch_failed",
        )
    )
    skipped = store.record_collection_run(
        **_collection_run(
            run_id="skipped-run",
            status="skipped",
            rows_seen=1,
            rows_inserted=0,
            rows_duplicate=0,
            rows_skipped=1,
        )
    )
    assert (partial.status, failed.status, skipped.status) == (
        "partial",
        "failed",
        "skipped",
    )


def test_collection_runs_are_append_only_even_via_raw_sqlite(tmp_path):
    db_path = tmp_path / "run-append-only.sqlite3"
    store = SourceObservationStoreV2(db_path)
    result = store.record_collection_run(**_collection_run())
    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="collection_runs_append_only"):
            conn.execute(
                "UPDATE collection_runs SET status = 'failed' WHERE id = ?",
                (result.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="collection_runs_append_only"):
            conn.execute("DELETE FROM collection_runs WHERE id = ?", (result.id,))


@pytest.mark.parametrize(
    "override",
    [
        {"status": "unknown"},
        {"rows_seen": -1, "rows_inserted": -1},
        {"rows_seen": 3},
        {"status": "success", "error_type": "unexpected"},
        {"status": "failed", "error_type": ""},
        {"status": "failed", "error_type": "unsafe error"},
        {
            "started_at": "2026-07-15T12:00:00.123456Z",
            "completed_at": "2026-07-14T12:00:00.123456Z",
        },
        {
            "started_at": "2026-07-14T11:00:00.123456+00:00",
            "completed_at": "2026-07-14T12:00:00.123456+00:00",
        },
        {"rows_inserted": 1.5, "rows_seen": 2.5},
    ],
)
def test_collection_run_direct_sql_checks(tmp_path, override):
    db_path = tmp_path / "run-db-checks.sqlite3"
    SourceObservationStoreV2(db_path)
    values = {
        "source": "sec_companyfacts",
        "dataset": "company_facts",
        "run_id": "raw-run",
        "started_at": "2026-07-14T11:00:00.123456Z",
        "completed_at": "2026-07-14T12:00:00.123456Z",
        "status": "success",
        "rows_seen": 2,
        "rows_inserted": 1,
        "rows_duplicate": 1,
        "rows_skipped": 0,
        "rows_invalid": 0,
        "error_type": "",
        "fingerprint": "a" * 64,
    }
    values.update(override)
    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            conn.execute(
                """
                INSERT INTO collection_runs (
                    source, dataset, run_id, started_at, completed_at, status,
                    rows_seen, rows_inserted, rows_duplicate, rows_skipped,
                    rows_invalid, error_type, fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(values[key] for key in (
                    "source", "dataset", "run_id", "started_at", "completed_at",
                    "status", "rows_seen", "rows_inserted", "rows_duplicate",
                    "rows_skipped", "rows_invalid", "error_type", "fingerprint",
                )),
            )


def test_concurrent_collection_run_duplicates_converge_to_one_row(tmp_path):
    db_path = tmp_path / "concurrent-runs.sqlite3"
    store = SourceObservationStoreV2(db_path)

    def record_once(_index):
        return store.record_collection_run(**_collection_run())

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(record_once, range(24)))

    assert sum(result.inserted for result in results) == 1
    assert len({result.id for result in results}) == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 1


def test_atomic_write_commits_observation_and_success_run(tmp_path):
    db_path = tmp_path / "atomic-commit.sqlite3"
    store = SourceObservationStoreV2(db_path)

    def write_batch(conn):
        observation = store.append(**_observation(), _conn=conn)
        run = store.record_collection_run(**_collection_run(), _conn=conn)
        return observation, run

    observation, run = store.atomic_write(write_batch)
    assert observation.inserted is True
    assert run.inserted is True
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 1


def test_atomic_write_rolls_back_both_then_failed_run_records_separately(tmp_path):
    db_path = tmp_path / "atomic-rollback.sqlite3"
    store = SourceObservationStoreV2(db_path)

    def failing_batch(conn):
        store.append(**_observation(), _conn=conn)
        store.record_collection_run(**_collection_run(), _conn=conn)
        raise RuntimeError("callback_failed")

    with pytest.raises(RuntimeError, match="callback_failed"):
        store.atomic_write(failing_batch)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 0

    failed = store.record_collection_run(
        **_collection_run(
            status="failed",
            rows_seen=0,
            rows_inserted=0,
            rows_duplicate=0,
            error_type="callback_failed",
        )
    )
    assert failed.inserted is True
    assert failed.status == "failed"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 1


def test_zero_byte_database_creates_exact_v2_schema(tmp_path):
    db_path = tmp_path / "empty.sqlite3"
    db_path.touch()
    assert db_path.stat().st_size == 0

    SourceObservationStoreV2(db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        objects = {
            (object_type, name)
            for object_type, name in conn.execute(
                "SELECT type, name FROM sqlite_master"
            )
        }
    assert objects == {
        ("table", "observations"),
        ("table", "sqlite_sequence"),
        ("table", "collection_runs"),
        ("index", "ux_observations_snapshot_id"),
        ("index", "ux_observations_exact_identity"),
        ("index", "ix_observations_latest_as_of"),
        ("index", "sqlite_autoindex_collection_runs_1"),
        ("index", "ix_collection_runs_latest"),
        ("trigger", "trg_observations_reject_sensitive_insert"),
        ("trigger", "trg_observations_no_update"),
        ("trigger", "trg_observations_no_delete"),
        ("trigger", "trg_collection_runs_no_update"),
        ("trigger", "trg_collection_runs_no_delete"),
    }


def test_existing_exact_schema_reopens(tmp_path):
    db_path = tmp_path / "valid-reopen.sqlite3"
    first = SourceObservationStoreV2(db_path)
    first.append(**_observation())

    reopened = SourceObservationStoreV2(db_path)

    assert reopened.latest_as_of(
        decision_at=_at(15),
        source="sec_companyfacts",
        dataset="company_facts",
        symbol="MRVL",
        market="US",
    ) is not None


def test_existing_database_with_wrong_user_version_is_rejected_read_only(tmp_path):
    db_path = tmp_path / "wrong-version.sqlite3"
    SourceObservationStoreV2(db_path)
    _execute_sql_and_close(db_path, "PRAGMA user_version = 1")

    _assert_schema_rejected_without_changes(db_path)


@pytest.mark.parametrize(
    "constant_name,mutated_key,error_suffix",
    [
        ("_EXPECTED_TABLE_XINFO", "observations", "table_xinfo"),
        ("_EXPECTED_INDEX_LIST", "observations", "index_list"),
        (
            "_EXPECTED_INDEX_XINFO",
            "ix_observations_latest_as_of",
            "index_xinfo",
        ),
    ],
)
def test_existing_preflight_compares_physical_schema_metadata_read_only(
    tmp_path, monkeypatch, constant_name, mutated_key, error_suffix
):
    import core.source_observations_v2 as module

    db_path = tmp_path / f"physical-{error_suffix}.sqlite3"
    SourceObservationStoreV2(db_path)
    before = _database_state(db_path)
    mutated = dict(getattr(module, constant_name))
    mutated[mutated_key] = ()
    monkeypatch.setattr(module, constant_name, mutated)

    with pytest.raises(
        ValueError,
        match=rf"source_observation_v2_schema_invalid:{error_suffix}",
    ):
        SourceObservationStoreV2(db_path)

    assert _database_state(db_path) == before


@pytest.mark.parametrize(
    "mutation_sql",
    [
        "CREATE TABLE unexpected_table (id INTEGER)",
        "CREATE INDEX unexpected_index ON observations(payload_json)",
        (
            "CREATE TRIGGER unexpected_trigger AFTER INSERT ON observations "
            "BEGIN SELECT 1; END"
        ),
        "CREATE VIEW unexpected_view AS SELECT 1 AS value",
        "DROP TABLE collection_runs",
        "DROP TRIGGER trg_observations_no_delete",
    ],
    ids=[
        "extra-table",
        "extra-index",
        "extra-trigger",
        "extra-view",
        "missing-run-table",
        "missing-trigger",
    ],
)
def test_existing_database_with_wrong_object_roster_is_rejected_read_only(
    tmp_path, mutation_sql
):
    db_path = tmp_path / "wrong-objects.sqlite3"
    SourceObservationStoreV2(db_path)
    _execute_sql_and_close(db_path, mutation_sql)

    _assert_schema_rejected_without_changes(db_path)


@pytest.mark.parametrize(
    ("object_type", "name", "old", "new"),
    [
        (
            "table",
            "observations",
            "snapshot_id TEXT NOT NULL",
            "snapshot_id BLOB NOT NULL",
        ),
        (
            "table",
            "observations",
            "snapshot_id TEXT NOT NULL",
            "snapshot_id TEXT NOT NULL DEFAULT ''",
        ),
        (
            "table",
            "observations",
            "payload_sha256 TEXT NOT NULL",
            (
                "payload_sha256 TEXT NOT NULL, "
                "generated_probe TEXT GENERATED ALWAYS AS (source) VIRTUAL"
            ),
        ),
        (
            "table",
            "observations",
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            "id INTEGER PRIMARY KEY DESC AUTOINCREMENT",
        ),
        (
            "table",
            "observations",
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            "id INTEGER PRIMARY KEY",
        ),
        (
            "table",
            "collection_runs",
            "rows_seen = rows_inserted + rows_duplicate",
            "rows_seen >= rows_inserted + rows_duplicate",
        ),
        (
            "index",
            "ux_observations_exact_identity",
            "source, dataset, source_record_id",
            "dataset, source, source_record_id",
        ),
        (
            "trigger",
            "trg_observations_no_update",
            "observations_append_only",
            "observations_append_only_tampered",
        ),
    ],
    ids=[
        "wrong-column-type",
        "unexpected-default",
        "generated-column",
        "ipk-desc",
        "no-autoincrement",
        "altered-check",
        "wrong-index-order",
        "altered-trigger",
    ],
)
def test_existing_database_with_mutated_schema_sql_is_rejected_read_only(
    tmp_path, object_type, name, old, new
):
    db_path = tmp_path / "mutated-schema.sqlite3"
    SourceObservationStoreV2(db_path)
    _replace_schema_sql_and_close(db_path, object_type, name, old, new)

    _assert_schema_rejected_without_changes(db_path)


@pytest.mark.parametrize(
    ("mutation_sql", "probe_sql", "expected"),
    [
        ("PRAGMA user_version = 1", "PRAGMA user_version", 1),
        (
            "CREATE TABLE wal_only_extra (id INTEGER)",
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'wal_only_extra'",
            1,
        ),
        (
            "DROP TRIGGER trg_observations_no_delete",
            (
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'trigger' AND name = 'trg_observations_no_delete'"
            ),
            0,
        ),
    ],
    ids=["user-version", "extra-table", "missing-append-trigger"],
)
def test_active_wal_only_schema_mutation_is_rejected_without_touching_database_family(
    tmp_path, mutation_sql, probe_sql, expected
):
    db_path = tmp_path / "active-wal-invalid.sqlite3"
    store = SourceObservationStoreV2(db_path)
    store.append(**_observation())
    main_before_mutation = db_path.read_bytes()

    with sqlite3.connect(db_path) as wal_owner:
        wal_owner.execute("PRAGMA wal_autocheckpoint = 0")
        wal_owner.execute(mutation_sql)
        wal_owner.commit()
        assert wal_owner.execute(probe_sql).fetchone()[0] == expected
        assert db_path.read_bytes() == main_before_mutation
        assert Path(f"{db_path}-wal").stat().st_size > 0
        before = _database_family_state(db_path)

        with pytest.raises(ValueError, match="source_observation_v2_schema_invalid"):
            SourceObservationStoreV2(db_path)

        assert _database_family_state(db_path) == before
