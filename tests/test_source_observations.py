"""Append-only point-in-time source observation store contracts."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from core.source_observations import SourceObservationStore
from tools.source_observation_report import build_source_observation_report

UTC = timezone.utc


def _ts(day: int) -> datetime:
    return datetime(2026, 7, day, 12, 0, tzinfo=UTC)


def test_exact_duplicate_is_idempotent_but_changed_payload_appends_revision(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    common = {
        "source": "naver_frgn",
        "source_record_id": "005930:20260714",
        "symbol": "005930.KS",
        "market": "KR",
        "currency": "KRW",
        "source_as_of": _ts(14),
        "ingested_at": _ts(14),
        "schema_version": 1,
        "fallback_used": True,
    }

    first = store.append(payload={"close": 100.0, "foreign_shares": 20.0}, **common)
    duplicate = store.append(
        payload={"foreign_shares": 20.0, "close": 100.0},
        **{**common, "ingested_at": _ts(15)},
    )
    revision = store.append(payload={"close": 101.0, "foreign_shares": 25.0}, **common)
    currency_revision = store.append(
        payload={"close": 101.0, "foreign_shares": 25.0},
        **{**common, "currency": "USD"},
    )

    assert first.inserted is True
    assert duplicate.inserted is False
    assert duplicate.id == first.id
    assert duplicate.snapshot_id == first.snapshot_id
    assert revision.inserted is True
    assert revision.id != first.id
    assert revision.snapshot_id != first.snapshot_id
    assert currency_revision.inserted is True
    assert currency_revision.snapshot_id != revision.snapshot_id
    assert store.count(source="naver_frgn", symbol="005930.KS") == 3


def test_point_in_time_query_excludes_revision_ingested_after_decision(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    common = {
        "source": "opendart_xbrl",
        "source_record_id": "005930:2026Q2:revenue",
        "symbol": "005930.KS",
        "market": "KR",
        "currency": "KRW",
        "source_as_of": _ts(14),
        "schema_version": 1,
        "fallback_used": False,
    }
    store.append(payload={"revenue": 100}, ingested_at=_ts(14), **common)
    store.append(payload={"revenue": 110}, ingested_at=_ts(16), **common)

    before_revision = store.latest_as_of(
        source="opendart_xbrl", symbol="005930.KS", decision_at=_ts(15)
    )
    after_revision = store.latest_as_of(
        source="opendart_xbrl", symbol="005930.KS", decision_at=_ts(17)
    )

    assert before_revision is not None
    assert before_revision.currency == "KRW"
    assert before_revision.payload == {"revenue": 100}
    assert after_revision is not None
    assert after_revision.payload == {"revenue": 110}


def test_append_requires_timezone_aware_ordered_timestamps(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    common = {
        "source": "sec_companyfacts",
        "source_record_id": "MRVL:2026Q2:revenue",
        "symbol": "MRVL",
        "market": "US",
        "currency": "USD",
        "schema_version": 1,
        "fallback_used": False,
        "payload": {"revenue": 100},
    }

    with pytest.raises(ValueError, match="timezone_aware"):
        store.append(
            source_as_of=datetime(2026, 7, 14, 12, 0),
            ingested_at=_ts(14),
            **common,
        )

    with pytest.raises(ValueError, match="source_as_of_after_ingested_at"):
        store.append(
            source_as_of=_ts(15),
            ingested_at=_ts(14),
            **common,
        )


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"close": float("nan")}, "payload_non_finite"),
        ({"nested": {"api_key": "must-not-persist"}}, "payload_sensitive_key"),
        ({"accessToken": "not-a-real-token"}, "payload_sensitive_key"),
        ({"crtfc_key": "not-a-real-token"}, "payload_sensitive_key"),
        ({"dart_api_key": "not-a-real-token"}, "payload_sensitive_key"),
        ({"kis_app_secret": "not-a-real-token"}, "payload_sensitive_key"),
        ({"openai_api_key": "not-a-real-token"}, "payload_sensitive_key"),
        (
            {"description": "ghp_" + "A" * 36},
            "payload_sensitive_value",
        ),
        (
            {"url": "https://example.invalid/?crtfc_key=must-not-persist"},
            "payload_sensitive_value",
        ),
    ],
)
def test_append_rejects_unsafe_payloads(tmp_path, payload, reason):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    with pytest.raises(ValueError, match=reason):
        store.append(
            source="kis_quote",
            source_record_id="005930:20260714T120000Z",
            symbol="005930.KS",
            market="KR",
            currency="KRW",
            source_as_of=_ts(14),
            ingested_at=_ts(14),
            schema_version=1,
            fallback_used=False,
            payload=payload,
        )


def test_database_rejects_update_and_delete_of_observations(tmp_path):
    db_path = tmp_path / "source_observations.db"
    store = SourceObservationStore(db_path)
    result = store.append(
        source="fred",
        source_record_id="DGS10:20260714",
        symbol="__MACRO__",
        market="GLOBAL",
        currency="N/A",
        source_as_of=_ts(14),
        ingested_at=_ts(14),
        schema_version=1,
        fallback_used=False,
        payload={"series_id": "DGS10", "value": 4.1},
    )

    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="source_observations_append_only"):
            conn.execute(
                "UPDATE source_observations SET payload_json = '{}' WHERE id = ?",
                (result.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="source_observations_append_only"):
            conn.execute("DELETE FROM source_observations WHERE id = ?", (result.id,))

    assert store.count(source="fred", symbol="__MACRO__") == 1


def test_concurrent_exact_duplicates_converge_to_one_row(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")

    def append_once(_index):
        return store.append(
            source="sec_submissions",
            source_record_id="0001835632-26-000123",
            symbol="MRVL",
            market="US",
            currency="USD",
            source_as_of=_ts(14),
            ingested_at=_ts(14),
            schema_version=1,
            fallback_used=False,
            payload={"form": "8-K", "items": ["2.02"]},
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(append_once, range(24)))

    assert sum(result.inserted for result in results) == 1
    assert len({result.id for result in results}) == 1
    assert store.count(source="sec_submissions", symbol="MRVL") == 1


def test_append_rejects_existing_snapshot_with_forged_immutable_payload(tmp_path):
    db_path = tmp_path / "source_observations.db"
    store = SourceObservationStore(db_path)
    values = {
        "source": "sec_submissions",
        "source_record_id": "0001835632-26-000123",
        "symbol": "MRVL",
        "market": "US",
        "currency": "USD",
        "source_as_of": _ts(14),
        "ingested_at": _ts(14),
        "schema_version": 1,
        "fallback_used": False,
        "payload": {"form": "8-K", "items": ["2.02"]},
    }
    first = store.append(**values)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            DROP TRIGGER trg_source_observations_no_update;
            UPDATE source_observations
            SET payload_json = '{"form":"forged"}',
                payload_sha256 = 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff';
            CREATE TRIGGER trg_source_observations_no_update
            BEFORE UPDATE ON source_observations
            BEGIN
                SELECT RAISE(ABORT, 'source_observations_append_only');
            END;
            """
        )

    with pytest.raises(ValueError, match="source_observation_conflict"):
        store.append(**values)
    assert store.count(source="sec_submissions", symbol="MRVL") == 1
    assert first.inserted is True


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"source": "SEC Submissions"}, "source_invalid"),
        ({"source_record_id": ""}, "source_record_id_invalid"),
        ({"symbol": "mrvl"}, "symbol_invalid"),
        ({"market": "UNKNOWN"}, "market_invalid"),
        ({"currency": "usd"}, "currency_invalid"),
        ({"currency": "USDT"}, "currency_invalid"),
        ({"schema_version": True}, "schema_version_invalid"),
        ({"schema_version": 0}, "schema_version_invalid"),
        ({"fallback_used": "false"}, "fallback_used_invalid"),
        ({"payload": [1, 2, 3]}, "payload_must_be_object"),
        (
            {"source_record_id": "ghp_" + "A" * 36},
            "source_record_id_sensitive",
        ),
        (
            {"source_record_id": "record?crtfc_key=must-not-persist"},
            "source_record_id_sensitive",
        ),
    ],
)
def test_append_rejects_malformed_metadata(tmp_path, overrides, reason):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    values = {
        "source": "sec_submissions",
        "source_record_id": "0001835632-26-000123",
        "symbol": "MRVL",
        "market": "US",
        "currency": "USD",
        "source_as_of": _ts(14),
        "ingested_at": _ts(14),
        "schema_version": 1,
        "fallback_used": False,
        "payload": {"form": "8-K"},
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=reason):
        store.append(**values)


def test_source_summaries_expose_coverage_fallback_and_latest_ingestion(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    for symbol, fallback, day in (
        ("MRVL", False, 14),
        ("LMT", False, 15),
        ("MRVL", True, 16),
    ):
        store.append(
            source="sec_submissions",
            source_record_id=f"{symbol}:202607{day}",
            symbol=symbol,
            market="US",
            currency="USD",
            source_as_of=_ts(day),
            ingested_at=_ts(day),
            schema_version=1,
            fallback_used=fallback,
            payload={"day": day},
        )

    summaries = {summary.source: summary for summary in store.summaries()}
    sec = summaries["sec_submissions"]
    assert sec.observation_count == 3
    assert sec.symbol_count == 2
    assert sec.fallback_count == 1
    assert sec.latest_source_as_of == _ts(16).isoformat()
    assert sec.latest_ingested_at == _ts(16).isoformat()


def test_collection_run_metrics_are_idempotent_and_immutable(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    values = {
        "source": "sec_submissions",
        "run_id": "sec:20260714T123000Z",
        "started_at": _ts(14),
        "completed_at": _ts(15),
        "rows_seen": 3,
        "rows_inserted": 1,
        "rows_duplicate": 1,
        "rows_skipped": 0,
        "rows_invalid": 1,
        "error_type": "",
    }

    first = store.record_collection_run(**values)
    duplicate = store.record_collection_run(**values)

    assert first.inserted is True
    assert first.status == "partial"
    assert duplicate.inserted is False
    assert duplicate.id == first.id

    latest = store.latest_collection_run(source="sec_submissions")
    assert latest is not None
    assert latest.run_id == "sec:20260714T123000Z"
    assert latest.rows_seen == 3
    assert latest.rows_skipped == 0
    assert latest.rows_invalid == 1

    with pytest.raises(ValueError, match="collection_run_conflict"):
        store.record_collection_run(
            **{**values, "rows_duplicate": 2, "rows_invalid": 0}
        )


def test_store_uses_wal_and_short_busy_timeout(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")

    with store._connect() as conn:
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        busy_timeout_ms = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])

    assert journal_mode == "wal"
    assert busy_timeout_ms <= 1000


def test_collection_run_rejects_sensitive_error_type(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    with pytest.raises(ValueError, match="collection_run_error_type_sensitive"):
        store.record_collection_run(
            source="opendart_disclosures",
            run_id="dart:sensitive-error",
            started_at=_ts(14),
            completed_at=_ts(14),
            rows_seen=0,
            rows_inserted=0,
            rows_duplicate=0,
            rows_skipped=0,
            rows_invalid=0,
            error_type=(
                "fetch_error:https://example.invalid/?crtfc_key=must-not-persist"
            ),
        )


@pytest.mark.parametrize(
    "run_id",
    ["ghp_" + "A" * 36, "run?crtfc_key=must-not-persist"],
)
def test_collection_run_rejects_sensitive_run_id(tmp_path, run_id):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    with pytest.raises(ValueError, match="collection_run_id_sensitive"):
        store.record_collection_run(
            source="opendart_disclosures",
            run_id=run_id,
            started_at=_ts(14),
            completed_at=_ts(14),
            rows_seen=0,
            rows_inserted=0,
            rows_duplicate=0,
            rows_skipped=0,
            rows_invalid=0,
            error_type="fetch_failed",
        )


def test_source_health_includes_failed_source_with_zero_observations(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    store.record_collection_run(
        source="opendart_disclosures",
        run_id="dart:missing-key",
        started_at=_ts(14),
        completed_at=_ts(14),
        rows_seen=0,
        rows_inserted=0,
        rows_duplicate=0,
        rows_skipped=0,
        rows_invalid=0,
        error_type="no_api_key",
    )

    health = {row.source: row for row in store.source_health()}
    dart = health["opendart_disclosures"]
    assert dart.observation_count == 0
    assert dart.symbol_count == 0
    assert dart.latest_ingested_at is None
    assert dart.latest_run_status == "failed"
    assert dart.latest_error_type == "no_api_key"


def test_source_observation_report_is_json_ready(tmp_path):
    db_path = tmp_path / "source_observations.db"
    store = SourceObservationStore(db_path)
    store.record_collection_run(
        source="sec_submissions",
        run_id="sec:empty-success",
        started_at=_ts(14),
        completed_at=_ts(14),
        rows_seen=0,
        rows_inserted=0,
        rows_duplicate=0,
        rows_skipped=0,
        rows_invalid=0,
        error_type="",
    )

    report = build_source_observation_report(db_path)

    assert report["schema_version"] == 1
    assert report["source_count"] == 1
    assert report["sources"][0]["source"] == "sec_submissions"
    assert report["sources"][0]["latest_run_status"] == "success"


def test_store_rejects_legacy_schema_without_currency(tmp_path):
    db_path = tmp_path / "source_observations.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE source_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                source_as_of TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                fallback_used INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL
            )
            """
        )

    with pytest.raises(RuntimeError, match="schema_incompatible:missing_currency"):
        SourceObservationStore(db_path)


def test_store_rejects_schema_without_snapshot_unique_and_append_only_triggers(
    tmp_path,
):
    db_path = tmp_path / "source_observations.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE source_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id TEXT NOT NULL,
                source TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                currency TEXT NOT NULL,
                source_as_of TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                fallback_used INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL
            )
            """
        )

    with pytest.raises(RuntimeError, match="schema_incompatible"):
        SourceObservationStore(db_path)


def test_report_does_not_initialize_or_mutate_empty_database_file(tmp_path):
    db_path = tmp_path / "empty.db"
    db_path.write_bytes(b"")
    before = db_path.stat().st_size

    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        build_source_observation_report(db_path)

    assert db_path.stat().st_size == before
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall() == []


def _replace_observation_table_for_schema_probe(
    db_path,
    *,
    fallback_type: str,
    unique_index_sql: str,
):
    assert fallback_type in {"INTEGER", "TEXT"}
    assert unique_index_sql in {
        "CREATE UNIQUE INDEX probe_snapshot_unique "
        "ON source_observations(snapshot_id);",
        "CREATE UNIQUE INDEX probe_snapshot_unique "
        "ON source_observations(snapshot_id COLLATE NOCASE);",
    }
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            f"""
            DROP TRIGGER trg_source_observations_no_update;
            DROP TRIGGER trg_source_observations_no_delete;
            ALTER TABLE source_observations RENAME TO source_observations_old;
            CREATE TABLE source_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id TEXT NOT NULL,
                source TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                currency TEXT NOT NULL,
                source_as_of TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                fallback_used {fallback_type} NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL
            );
            {unique_index_sql}
            CREATE TRIGGER trg_source_observations_no_update
            BEFORE UPDATE ON source_observations
            BEGIN
                SELECT RAISE(ABORT, 'source_observations_append_only');
            END;
            CREATE TRIGGER trg_source_observations_no_delete
            BEFORE DELETE ON source_observations
            BEGIN
                SELECT RAISE(ABORT, 'source_observations_append_only');
            END;
            DROP TABLE source_observations_old;
            """
        )


def test_store_rejects_noncanonical_column_affinity(tmp_path):
    db_path = tmp_path / "source_observations.db"
    SourceObservationStore(db_path)
    _replace_observation_table_for_schema_probe(
        db_path,
        fallback_type="TEXT",
        unique_index_sql=(
            "CREATE UNIQUE INDEX probe_snapshot_unique "
            "ON source_observations(snapshot_id);"
        ),
    )

    with pytest.raises(RuntimeError, match="schema_incompatible:column_metadata"):
        SourceObservationStore(db_path)


def test_store_rejects_nocase_snapshot_unique_index(tmp_path):
    db_path = tmp_path / "source_observations.db"
    SourceObservationStore(db_path)
    _replace_observation_table_for_schema_probe(
        db_path,
        fallback_type="INTEGER",
        unique_index_sql=(
            "CREATE UNIQUE INDEX probe_snapshot_unique "
            "ON source_observations(snapshot_id COLLATE NOCASE);"
        ),
    )

    with pytest.raises(RuntimeError, match="schema_incompatible:snapshot_unique"):
        SourceObservationStore(db_path)


def test_store_rejects_partial_unique_index_that_cannot_enforce_idempotency(tmp_path):
    db_path = tmp_path / "source_observations.db"
    SourceObservationStore(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            DROP TRIGGER trg_source_observations_no_update;
            DROP TRIGGER trg_source_observations_no_delete;
            ALTER TABLE source_observations RENAME TO source_observations_old;
            CREATE TABLE source_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id TEXT NOT NULL,
                source TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                currency TEXT NOT NULL,
                source_as_of TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                fallback_used INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL
            );
            CREATE UNIQUE INDEX deceptive_snapshot_unique
            ON source_observations(snapshot_id) WHERE 0;
            CREATE TRIGGER trg_source_observations_no_update
            BEFORE UPDATE ON source_observations
            BEGIN
                SELECT RAISE(ABORT, 'source_observations_append_only');
            END;
            CREATE TRIGGER trg_source_observations_no_delete
            BEFORE DELETE ON source_observations
            BEGIN
                SELECT RAISE(ABORT, 'source_observations_append_only');
            END;
            DROP TABLE source_observations_old;
            """
        )

    with pytest.raises(RuntimeError, match="schema_incompatible:snapshot_unique"):
        SourceObservationStore(db_path)


def test_store_rejects_noop_append_only_trigger_with_expected_marker(tmp_path):
    db_path = tmp_path / "source_observations.db"
    SourceObservationStore(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            DROP TRIGGER trg_source_observations_no_update;
            CREATE TRIGGER trg_source_observations_no_update
            BEFORE UPDATE ON source_observations
            BEGIN
                SELECT 'source_observations_append_only';
            END;
            """
        )

    with pytest.raises(
        RuntimeError,
        match="schema_incompatible:trigger:trg_source_observations_no_update",
    ):
        SourceObservationStore(db_path)
