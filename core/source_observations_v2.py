"""Exact append-only point-in-time source observation storage (v2)."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar


MAX_PAYLOAD_BYTES = 1_048_576
_MARKETS = frozenset({"KR", "US", "GLOBAL"})
_UNITS = frozenset({"KRW", "USD", "SHARES", "PERCENT", "UNITLESS", "MIXED"})
_RUN_STATUSES = frozenset({"success", "partial", "failed", "skipped"})
_EXPECTED_SCHEMA_OBJECTS = frozenset(
    {
        ("table", "observations", "observations"),
        ("table", "collection_runs", "collection_runs"),
        ("table", "sqlite_sequence", "sqlite_sequence"),
        ("index", "ux_observations_snapshot_id", "observations"),
        ("index", "ux_observations_exact_identity", "observations"),
        ("index", "ix_observations_latest_as_of", "observations"),
        ("index", "sqlite_autoindex_collection_runs_1", "collection_runs"),
        ("index", "ix_collection_runs_latest", "collection_runs"),
        ("trigger", "trg_observations_reject_sensitive_insert", "observations"),
        ("trigger", "trg_observations_no_update", "observations"),
        ("trigger", "trg_observations_no_delete", "observations"),
        ("trigger", "trg_collection_runs_no_update", "collection_runs"),
        ("trigger", "trg_collection_runs_no_delete", "collection_runs"),
    }
)
_EXPECTED_SCHEMA_SQL_SHA256 = {
    ("table", "observations"): "614aac533ca55bbbbfff541e9b0833af8a9d914dd824c8ead76339c3ebdc4e07",
    ("table", "collection_runs"): "e0c3d54e1e9f1ac21111474d7ea4221c8f1116d876c47b98dbf21dc5e37907bb",
    ("table", "sqlite_sequence"): "ddb8929a222de51821e7d6a67f9c0c37ccebc858cf153cec65107d24336fe19f",
    ("index", "ux_observations_snapshot_id"): "ad2a5f3c71a66ed32be3e52a39db2aec047e6ff3d43150efae7df6b83ac793b4",
    ("index", "ux_observations_exact_identity"): "98b1c4262abfa71ef9388cd519e30d40f0561cc97cb868da554e08d4f317a6f2",
    ("index", "ix_observations_latest_as_of"): "c1a604e572810c29968dd77dc794ba802d1423b20650401bf9788901e99dfc87",
    ("index", "sqlite_autoindex_collection_runs_1"): None,
    ("index", "ix_collection_runs_latest"): "4e0011008d85aac09b3b9825906c37fdc74c46d8bd108c0e1d392103dd403d6f",
    ("trigger", "trg_observations_reject_sensitive_insert"): "12ee667d0564ec0651454fdc22ef888eede20680578c9d6284303df6b0eb67e8",
    ("trigger", "trg_observations_no_update"): "eec807e662e4eeb5484cafc08ad45b106e3fa788b8d5ee8c988723eb3753746b",
    ("trigger", "trg_observations_no_delete"): "a1ac927097129430e995bbe3e755eacf5a6518ed4dbfe2c8d82d3d609c0aabcb",
    ("trigger", "trg_collection_runs_no_update"): "d96105963fb90f7e0831a614548b24cf7a2e61461a1a3e82b01c8f9a049df8d4",
    ("trigger", "trg_collection_runs_no_delete"): "d960123cafc7833d227d88c8f6abfddd3c5a1fc8989537b14df901f2a061096c",
}
_EXPECTED_TABLE_XINFO = {
    "observations": (
        (0, "id", "INTEGER", 0, None, 1, 0),
        (1, "snapshot_id", "TEXT", 1, None, 0, 0),
        (2, "source", "TEXT", 1, None, 0, 0),
        (3, "dataset", "TEXT", 1, None, 0, 0),
        (4, "source_record_id", "TEXT", 1, None, 0, 0),
        (5, "symbol", "TEXT", 1, None, 0, 0),
        (6, "market", "TEXT", 1, None, 0, 0),
        (7, "currency_or_unit", "TEXT", 1, None, 0, 0),
        (8, "source_as_of", "TEXT", 1, None, 0, 0),
        (9, "source_event_sequence", "INTEGER", 1, None, 0, 0),
        (10, "ingested_at", "TEXT", 1, None, 0, 0),
        (11, "schema_version", "INTEGER", 1, None, 0, 0),
        (12, "transform_version", "INTEGER", 1, None, 0, 0),
        (13, "fallback_used", "INTEGER", 1, None, 0, 0),
        (14, "payload_json", "TEXT", 1, None, 0, 0),
        (15, "payload_sha256", "TEXT", 1, None, 0, 0),
    ),
    "collection_runs": (
        (0, "id", "INTEGER", 0, None, 1, 0),
        (1, "source", "TEXT", 1, None, 0, 0),
        (2, "dataset", "TEXT", 1, None, 0, 0),
        (3, "run_id", "TEXT", 1, None, 0, 0),
        (4, "started_at", "TEXT", 1, None, 0, 0),
        (5, "completed_at", "TEXT", 1, None, 0, 0),
        (6, "status", "TEXT", 1, None, 0, 0),
        (7, "rows_seen", "INTEGER", 1, None, 0, 0),
        (8, "rows_inserted", "INTEGER", 1, None, 0, 0),
        (9, "rows_duplicate", "INTEGER", 1, None, 0, 0),
        (10, "rows_skipped", "INTEGER", 1, None, 0, 0),
        (11, "rows_invalid", "INTEGER", 1, None, 0, 0),
        (12, "error_type", "TEXT", 1, None, 0, 0),
        (13, "fingerprint", "TEXT", 1, None, 0, 0),
    ),
}
_EXPECTED_INDEX_LIST = {
    "observations": {
        "ux_observations_snapshot_id": (1, "c", 0),
        "ux_observations_exact_identity": (1, "c", 0),
        "ix_observations_latest_as_of": (0, "c", 0),
    },
    "collection_runs": {
        "sqlite_autoindex_collection_runs_1": (1, "u", 0),
        "ix_collection_runs_latest": (0, "c", 0),
    },
}
_EXPECTED_INDEX_XINFO = {
    "ux_observations_snapshot_id": (
        (0, 1, "snapshot_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    ),
    "ux_observations_exact_identity": (
        (0, 2, "source", 0, "BINARY", 1),
        (1, 3, "dataset", 0, "BINARY", 1),
        (2, 4, "source_record_id", 0, "BINARY", 1),
        (3, 5, "symbol", 0, "BINARY", 1),
        (4, 6, "market", 0, "BINARY", 1),
        (5, 7, "currency_or_unit", 0, "BINARY", 1),
        (6, 8, "source_as_of", 0, "BINARY", 1),
        (7, 9, "source_event_sequence", 0, "BINARY", 1),
        (8, 11, "schema_version", 0, "BINARY", 1),
        (9, 12, "transform_version", 0, "BINARY", 1),
        (10, 13, "fallback_used", 0, "BINARY", 1),
        (11, 15, "payload_sha256", 0, "BINARY", 1),
        (12, -1, None, 0, "BINARY", 0),
    ),
    "ix_observations_latest_as_of": (
        (0, 2, "source", 0, "BINARY", 1),
        (1, 3, "dataset", 0, "BINARY", 1),
        (2, 5, "symbol", 0, "BINARY", 1),
        (3, 6, "market", 0, "BINARY", 1),
        (4, 8, "source_as_of", 1, "BINARY", 1),
        (5, 9, "source_event_sequence", 1, "BINARY", 1),
        (6, 10, "ingested_at", 1, "BINARY", 1),
        (7, 0, "id", 1, "BINARY", 1),
        (8, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex_collection_runs_1": (
        (0, 1, "source", 0, "BINARY", 1),
        (1, 2, "dataset", 0, "BINARY", 1),
        (2, 3, "run_id", 0, "BINARY", 1),
        (3, -1, None, 0, "BINARY", 0),
    ),
    "ix_collection_runs_latest": (
        (0, 1, "source", 0, "BINARY", 1),
        (1, 2, "dataset", 0, "BINARY", 1),
        (2, 5, "completed_at", 1, "BINARY", 1),
        (3, 0, "id", 1, "BINARY", 1),
        (4, -1, None, 0, "BINARY", 0),
    ),
}
_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_SOURCE_RECORD_ID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9_][A-Z0-9._:\-]{0,31}$")
_ERROR_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.:\-]{0,127}$")
_UTC_TEXT_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)
_T = TypeVar("_T")
_SENSITIVE_ALIASES = frozenset(
    {
        "accountno",
        "accountnumber",
        "apikey",
        "appkey",
        "appsecret",
        "authorization",
        "clientsecret",
        "crtfcKey".lower(),
        "password",
        "privatekey",
        "secret",
        "token",
    }
)
_KNOWN_SECRET_RE = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|"
    r"AIza[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._~+/-]{12,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"-----BEGIN(?: [A-Z]+)* PRIVATE KEY-----)",
    re.IGNORECASE,
)


def _collapsed(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _sensitive_key(value: str) -> bool:
    collapsed = _collapsed(value)
    return any(collapsed == alias or collapsed.endswith(alias) for alias in _SENSITIVE_ALIASES)


def _sensitive_text(value: str) -> bool:
    if _KNOWN_SECRET_RE.search(value):
        return True
    assignment_text = re.sub(r"[\s_-]+", "", value.lower())
    return any(
        f"{alias}=" in assignment_text or f"{alias}:" in assignment_text
        for alias in _SENSITIVE_ALIASES
    )


def _normalized_schema_sql_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    normalized: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(value):
        character = value[index]
        if quote is not None:
            normalized.append(character)
            if quote == "[":
                if character == "]":
                    quote = None
            elif character == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    normalized.append(value[index + 1])
                    index += 1
                else:
                    quote = None
        elif character in "'\"`[":
            quote = character
            normalized.append(character)
        elif not character.isspace():
            normalized.append(character.lower())
        index += 1
    return hashlib.sha256("".join(normalized).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AppendResult:
    """Result of an idempotent observation append."""

    id: int
    snapshot_id: str
    payload_sha256: str
    inserted: bool


@dataclass(frozen=True)
class SourceObservation:
    """A persisted source observation."""

    id: int
    snapshot_id: str
    source: str
    dataset: str
    source_record_id: str
    symbol: str
    market: str
    currency_or_unit: str
    source_as_of: str
    source_event_sequence: int
    ingested_at: str
    schema_version: int
    transform_version: int
    fallback_used: bool
    payload: dict[str, Any]
    payload_sha256: str


@dataclass(frozen=True)
class CollectionRunResult:
    """Result of an idempotent collection-run append."""

    id: int
    source: str
    dataset: str
    run_id: str
    status: str
    fingerprint: str
    inserted: bool


@dataclass(frozen=True)
class CollectionRun:
    """A persisted source collection run."""

    id: int
    source: str
    dataset: str
    run_id: str
    started_at: str
    completed_at: str
    status: str
    rows_seen: int
    rows_inserted: int
    rows_duplicate: int
    rows_skipped: int
    rows_invalid: int
    error_type: str
    fingerprint: str


class SourceObservationStoreV2:
    """SQLite source-observation store with immutable snapshot identity."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        try:
            existing_nonempty = self.db_path.stat().st_size > 0
        except FileNotFoundError:
            existing_nonempty = False
        if existing_nonempty:
            self._preflight_existing()
            with closing(sqlite3.connect(self.db_path, timeout=0.75)) as conn:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA busy_timeout = 750")
                conn.execute("PRAGMA foreign_keys = ON")
            self._preflight_existing()
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path, timeout=0.75)) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 750")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    dataset TEXT NOT NULL,
                    source_record_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    currency_or_unit TEXT NOT NULL,
                    source_as_of TEXT NOT NULL,
                    source_event_sequence INTEGER NOT NULL,
                    ingested_at TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    transform_version INTEGER NOT NULL,
                    fallback_used INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_observations_snapshot_id
                    ON observations(snapshot_id);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_observations_exact_identity
                    ON observations(
                        source, dataset, source_record_id, symbol, market,
                        currency_or_unit, source_as_of, source_event_sequence,
                        schema_version, transform_version, fallback_used,
                        payload_sha256
                    );
                CREATE INDEX IF NOT EXISTS ix_observations_latest_as_of
                    ON observations(
                        source, dataset, symbol, market,
                        source_as_of DESC, source_event_sequence DESC,
                        ingested_at DESC, id DESC
                    );
                CREATE TRIGGER IF NOT EXISTS trg_observations_reject_sensitive_insert
                BEFORE INSERT ON observations
                WHEN EXISTS (
                    SELECT 1 FROM json_tree(NEW.payload_json) AS item
                    WHERE item.key IS NOT NULL
                      AND (
                        lower(replace(replace(replace(replace(replace(replace(CAST(item.key AS TEXT), '_', ''), '-', ''), ' ', ''), char(9), ''), char(10), ''), char(13), ''))
                          IN ('accountno','accountnumber','apikey','appkey','appsecret',
                              'authorization','clientsecret','crtfckey','password',
                              'privatekey','secret','token')
                        OR lower(replace(replace(replace(replace(replace(replace(CAST(item.key AS TEXT), '_', ''), '-', ''), ' ', ''), char(9), ''), char(10), ''), char(13), '')) LIKE '%accountno'
                        OR lower(replace(replace(replace(replace(replace(replace(CAST(item.key AS TEXT), '_', ''), '-', ''), ' ', ''), char(9), ''), char(10), ''), char(13), '')) LIKE '%accountnumber'
                        OR lower(replace(replace(replace(replace(replace(replace(CAST(item.key AS TEXT), '_', ''), '-', ''), ' ', ''), char(9), ''), char(10), ''), char(13), '')) LIKE '%apikey'
                        OR lower(replace(replace(replace(replace(replace(replace(CAST(item.key AS TEXT), '_', ''), '-', ''), ' ', ''), char(9), ''), char(10), ''), char(13), '')) LIKE '%appkey'
                        OR lower(replace(replace(replace(replace(replace(replace(CAST(item.key AS TEXT), '_', ''), '-', ''), ' ', ''), char(9), ''), char(10), ''), char(13), '')) LIKE '%appsecret'
                        OR lower(replace(replace(replace(replace(replace(replace(CAST(item.key AS TEXT), '_', ''), '-', ''), ' ', ''), char(9), ''), char(10), ''), char(13), '')) LIKE '%authorization'
                        OR lower(replace(replace(replace(replace(replace(replace(CAST(item.key AS TEXT), '_', ''), '-', ''), ' ', ''), char(9), ''), char(10), ''), char(13), '')) LIKE '%clientsecret'
                        OR lower(replace(replace(replace(replace(replace(replace(CAST(item.key AS TEXT), '_', ''), '-', ''), ' ', ''), char(9), ''), char(10), ''), char(13), '')) LIKE '%crtfckey'
                        OR lower(replace(replace(replace(replace(replace(replace(CAST(item.key AS TEXT), '_', ''), '-', ''), ' ', ''), char(9), ''), char(10), ''), char(13), '')) LIKE '%password'
                        OR lower(replace(replace(replace(replace(replace(replace(CAST(item.key AS TEXT), '_', ''), '-', ''), ' ', ''), char(9), ''), char(10), ''), char(13), '')) LIKE '%privatekey'
                        OR lower(replace(replace(replace(replace(replace(replace(CAST(item.key AS TEXT), '_', ''), '-', ''), ' ', ''), char(9), ''), char(10), ''), char(13), '')) LIKE '%secret'
                        OR lower(replace(replace(replace(replace(replace(replace(CAST(item.key AS TEXT), '_', ''), '-', ''), ' ', ''), char(9), ''), char(10), ''), char(13), '')) LIKE '%token'
                      )
                )
                OR EXISTS (
                    SELECT 1
                    FROM json_tree(NEW.payload_json) AS item
                    JOIN (
                        SELECT 'accountno' AS alias
                        UNION ALL SELECT 'accountnumber'
                        UNION ALL SELECT 'apikey'
                        UNION ALL SELECT 'appkey'
                        UNION ALL SELECT 'appsecret'
                        UNION ALL SELECT 'authorization'
                        UNION ALL SELECT 'clientsecret'
                        UNION ALL SELECT 'crtfckey'
                        UNION ALL SELECT 'password'
                        UNION ALL SELECT 'privatekey'
                        UNION ALL SELECT 'secret'
                        UNION ALL SELECT 'token'
                    ) AS sensitive_alias
                    ON (
                        lower(replace(replace(replace(replace(replace(replace(CAST(item.value AS TEXT), '_', ''), '-', ''), ' ', ''), char(9), ''), char(10), ''), char(13), ''))
                            LIKE '%' || sensitive_alias.alias || '=%'
                        OR lower(replace(replace(replace(replace(replace(replace(CAST(item.value AS TEXT), '_', ''), '-', ''), ' ', ''), char(9), ''), char(10), ''), char(13), ''))
                            LIKE '%' || sensitive_alias.alias || ':%'
                    )
                    WHERE item.type = 'text'
                )
                OR EXISTS (
                    SELECT 1 FROM json_tree(NEW.payload_json) AS item
                    WHERE item.type = 'text'
                      AND (
                        lower(CAST(item.value AS TEXT)) GLOB
                            '*bearer [a-z0-9._~+/-][a-z0-9._~+/-][a-z0-9._~+/-][a-z0-9._~+/-][a-z0-9._~+/-][a-z0-9._~+/-][a-z0-9._~+/-][a-z0-9._~+/-][a-z0-9._~+/-][a-z0-9._~+/-][a-z0-9._~+/-][a-z0-9._~+/-]*'
                        OR CAST(item.value AS TEXT) GLOB '*eyJ????????.????????.????????*'
                        OR lower(CAST(item.value AS TEXT))
                            LIKE '%-----begin%private key-----%'
                        OR lower(CAST(item.value AS TEXT)) GLOB
                            '*ghp_[a-z0-9][a-z0-9][a-z0-9][a-z0-9][a-z0-9][a-z0-9][a-z0-9][a-z0-9][a-z0-9][a-z0-9][a-z0-9][a-z0-9][a-z0-9][a-z0-9][a-z0-9][a-z0-9][a-z0-9][a-z0-9][a-z0-9][a-z0-9]*'
                        OR lower(CAST(item.value AS TEXT)) GLOB
                            '*github_pat_[a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_][a-z0-9_]*'
                      )
                )
                BEGIN
                    SELECT RAISE(ABORT, 'observations_sensitive_data');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_observations_no_update
                BEFORE UPDATE ON observations
                BEGIN
                    SELECT RAISE(ABORT, 'observations_append_only');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_observations_no_delete
                BEFORE DELETE ON observations
                BEGIN
                    SELECT RAISE(ABORT, 'observations_append_only');
                END;
                CREATE TABLE IF NOT EXISTS collection_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    dataset TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    started_at TEXT NOT NULL
                        CHECK (
                            length(started_at) = 27
                            AND started_at GLOB
                                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
                        ),
                    completed_at TEXT NOT NULL
                        CHECK (
                            length(completed_at) = 27
                            AND completed_at GLOB
                                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
                        ),
                    status TEXT NOT NULL
                        CHECK (status IN ('success', 'partial', 'failed', 'skipped')),
                    rows_seen INTEGER NOT NULL
                        CHECK (typeof(rows_seen) = 'integer' AND rows_seen >= 0),
                    rows_inserted INTEGER NOT NULL
                        CHECK (typeof(rows_inserted) = 'integer' AND rows_inserted >= 0),
                    rows_duplicate INTEGER NOT NULL
                        CHECK (typeof(rows_duplicate) = 'integer' AND rows_duplicate >= 0),
                    rows_skipped INTEGER NOT NULL
                        CHECK (typeof(rows_skipped) = 'integer' AND rows_skipped >= 0),
                    rows_invalid INTEGER NOT NULL
                        CHECK (typeof(rows_invalid) = 'integer' AND rows_invalid >= 0),
                    error_type TEXT NOT NULL
                        CHECK (
                            error_type = ''
                            OR (
                                length(error_type) BETWEEN 1 AND 128
                                AND substr(error_type, 1, 1) GLOB '[a-z]'
                                AND error_type NOT GLOB '*[^a-z0-9_.:-]*'
                            )
                        ),
                    fingerprint TEXT NOT NULL
                        CHECK (
                            length(fingerprint) = 64
                            AND fingerprint NOT GLOB '*[^0-9a-f]*'
                        ),
                    UNIQUE(source, dataset, run_id),
                    CHECK (started_at <= completed_at),
                    CHECK (
                        rows_seen = rows_inserted + rows_duplicate
                            + rows_skipped + rows_invalid
                    ),
                    CHECK (status <> 'success' OR error_type = ''),
                    CHECK (status <> 'failed' OR error_type <> '')
                );
                CREATE INDEX IF NOT EXISTS ix_collection_runs_latest
                    ON collection_runs(source, dataset, completed_at DESC, id DESC);
                CREATE TRIGGER IF NOT EXISTS trg_collection_runs_no_update
                BEFORE UPDATE ON collection_runs
                BEGIN
                    SELECT RAISE(ABORT, 'collection_runs_append_only');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_collection_runs_no_delete
                BEFORE DELETE ON collection_runs
                BEGIN
                    SELECT RAISE(ABORT, 'collection_runs_append_only');
                END;
                PRAGMA user_version = 2;
                COMMIT;
                """
            )
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._preflight_existing()

    def _database_family_snapshot(self) -> tuple[bytes, bytes | None, tuple[Any, ...]]:
        main_path = self.db_path
        wal_path = Path(f"{self.db_path}-wal")

        def signature() -> tuple[Any, ...]:
            result = []
            for path in (main_path, wal_path):
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    result.append(None)
                else:
                    result.append(
                        (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                    )
            return tuple(result)

        for _attempt in range(3):
            before = signature()
            try:
                main_bytes = main_path.read_bytes()
                wal_bytes = wal_path.read_bytes() if before[1] is not None else None
            except (FileNotFoundError, OSError):
                continue
            after = signature()
            if before == after:
                return main_bytes, wal_bytes, after
        raise ValueError("source_observation_v2_schema_invalid:unreadable")

    def _preflight_existing(self) -> None:
        for _attempt in range(3):
            main_bytes, wal_bytes, source_signature = self._database_family_snapshot()
            try:
                with tempfile.TemporaryDirectory(prefix="source-observation-v2-preflight-") as directory:
                    snapshot_path = Path(directory) / self.db_path.name
                    snapshot_path.write_bytes(main_bytes)
                    if wal_bytes is not None:
                        Path(f"{snapshot_path}-wal").write_bytes(wal_bytes)
                    with sqlite3.connect(snapshot_path, timeout=0.75) as conn:
                        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
                        schema_rows = conn.execute(
                            "SELECT type, name, tbl_name, sql FROM sqlite_master"
                        ).fetchall()
                        schema_objects = frozenset(
                            (row[0], row[1], row[2]) for row in schema_rows
                        )
                        schema_sql_sha256 = {
                            (row[0], row[1]): _normalized_schema_sql_sha256(row[3])
                            for row in schema_rows
                        }
                        table_xinfo = {
                            table: tuple(conn.execute(f"PRAGMA table_xinfo({table})"))
                            for table in _EXPECTED_TABLE_XINFO
                        }
                        index_list = {
                            table: {
                                row[1]: (row[2], row[3], row[4])
                                for row in conn.execute(f"PRAGMA index_list({table})")
                            }
                            for table in _EXPECTED_INDEX_LIST
                        }
                        index_xinfo = {
                            index: tuple(conn.execute(f"PRAGMA index_xinfo({index})"))
                            for index in _EXPECTED_INDEX_XINFO
                        }
            except sqlite3.Error as exc:
                raise ValueError("source_observation_v2_schema_invalid:unreadable") from exc
            if self._database_family_snapshot()[2] == source_signature:
                break
        else:
            raise ValueError("source_observation_v2_schema_invalid:unreadable")
        if user_version != 2:
            raise ValueError("source_observation_v2_schema_invalid:user_version")
        if schema_objects != _EXPECTED_SCHEMA_OBJECTS:
            raise ValueError("source_observation_v2_schema_invalid:objects")
        if schema_sql_sha256 != _EXPECTED_SCHEMA_SQL_SHA256:
            raise ValueError("source_observation_v2_schema_invalid:sql")
        if table_xinfo != _EXPECTED_TABLE_XINFO:
            raise ValueError("source_observation_v2_schema_invalid:table_xinfo")
        if index_list != _EXPECTED_INDEX_LIST:
            raise ValueError("source_observation_v2_schema_invalid:index_list")
        if index_xinfo != _EXPECTED_INDEX_XINFO:
            raise ValueError("source_observation_v2_schema_invalid:index_xinfo")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=0.75)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 750")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def atomic_write(self, callback: Callable[[sqlite3.Connection], _T]) -> _T:
        """Run a callback in one immediate transaction and return its result."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = callback(conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _utc_text(value: datetime) -> str:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("timestamp_must_be_timezone_aware")
        if value.utcoffset() is None:
            raise ValueError("timestamp_must_be_timezone_aware")
        try:
            utc_value = value.astimezone(timezone.utc)
        except (OverflowError, ValueError) as exc:
            raise ValueError("timestamp_out_of_range") from exc
        return (
            f"{utc_value.year:04d}-{utc_value.month:02d}-{utc_value.day:02d}T"
            f"{utc_value.hour:02d}:{utc_value.minute:02d}:{utc_value.second:02d}."
            f"{utc_value.microsecond:06d}Z"
        )

    @staticmethod
    def _is_canonical_utc_text(value: Any) -> bool:
        if type(value) is not str or not _UTC_TEXT_RE.fullmatch(value):
            return False
        try:
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError:
            return False
        return True

    @classmethod
    def _validate_payload(cls, value: Any, path: str = "$") -> None:
        if type(value) is dict:
            for key, nested in value.items():
                if type(key) is not str:
                    raise ValueError(f"payload_key_not_string:{path}")
                if _sensitive_key(key):
                    raise ValueError(f"payload_sensitive_key:{path}.{key}")
                cls._validate_payload(nested, f"{path}.{key}")
            return
        if type(value) is list:
            for index, nested in enumerate(value):
                cls._validate_payload(nested, f"{path}[{index}]")
            return
        if type(value) is float and not math.isfinite(value):
            raise ValueError(f"payload_non_finite:{path}")
        if type(value) is str and _sensitive_text(value):
            raise ValueError(f"payload_sensitive_value:{path}")
        if value is None or type(value) in {str, int, float, bool}:
            return
        raise ValueError(f"payload_type_unsupported:{path}")

    @classmethod
    def _canonical_payload(cls, payload: dict[str, Any]) -> tuple[str, str]:
        if type(payload) is not dict:
            raise ValueError("payload_must_be_object")
        cls._validate_payload(payload)
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(payload_json.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError("payload_too_large")
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        return payload_json, payload_sha256

    @staticmethod
    def _validate_metadata(
        *,
        source: str,
        dataset: str,
        source_record_id: str,
        symbol: str,
        market: str,
        currency_or_unit: str,
        source_event_sequence: int,
        schema_version: int,
        transform_version: int,
        fallback_used: bool,
    ) -> None:
        if type(source) is not str or not _NAME_RE.fullmatch(source):
            raise ValueError("source_invalid")
        if _sensitive_text(source):
            raise ValueError("source_sensitive")
        if type(dataset) is not str or not _NAME_RE.fullmatch(dataset):
            raise ValueError("dataset_invalid")
        if _sensitive_text(dataset):
            raise ValueError("dataset_sensitive")
        if (
            type(source_record_id) is not str
            or not _SOURCE_RECORD_ID_RE.fullmatch(source_record_id)
        ):
            raise ValueError("source_record_id_invalid")
        if _sensitive_text(source_record_id):
            raise ValueError("source_record_id_sensitive")
        if type(symbol) is not str or not _SYMBOL_RE.fullmatch(symbol):
            raise ValueError("symbol_invalid")
        if _sensitive_text(symbol):
            raise ValueError("symbol_sensitive")
        if type(market) is not str or market not in _MARKETS:
            raise ValueError("market_invalid")
        if type(currency_or_unit) is not str or currency_or_unit not in _UNITS:
            raise ValueError("currency_or_unit_invalid")
        if type(source_event_sequence) is not int or source_event_sequence < 0:
            raise ValueError("source_event_sequence_invalid")
        if type(schema_version) is not int or schema_version < 1:
            raise ValueError("schema_version_invalid")
        if type(transform_version) is not int or transform_version < 1:
            raise ValueError("transform_version_invalid")
        if type(fallback_used) is not bool:
            raise ValueError("fallback_used_invalid")

    def append(
        self,
        *,
        source: str,
        dataset: str,
        source_record_id: str,
        symbol: str,
        market: str,
        currency_or_unit: str,
        source_as_of: datetime,
        source_event_sequence: int,
        ingested_at: datetime,
        schema_version: int,
        transform_version: int,
        fallback_used: bool,
        payload: dict[str, Any],
        _conn: sqlite3.Connection | None = None,
    ) -> AppendResult:
        self._validate_metadata(
            source=source,
            dataset=dataset,
            source_record_id=source_record_id,
            symbol=symbol,
            market=market,
            currency_or_unit=currency_or_unit,
            source_event_sequence=source_event_sequence,
            schema_version=schema_version,
            transform_version=transform_version,
            fallback_used=fallback_used,
        )
        source_as_of_text = self._utc_text(source_as_of)
        ingested_at_text = self._utc_text(ingested_at)
        if source_as_of > ingested_at:
            raise ValueError("source_as_of_after_ingested_at")
        payload_json, payload_sha256 = self._canonical_payload(payload)
        identity = {
            "currency_or_unit": currency_or_unit,
            "dataset": dataset,
            "fallback_used": fallback_used,
            "market": market,
            "payload_sha256": payload_sha256,
            "schema_version": schema_version,
            "source": source,
            "source_as_of": source_as_of_text,
            "source_event_sequence": source_event_sequence,
            "source_record_id": source_record_id,
            "symbol": symbol,
            "transform_version": transform_version,
        }
        identity_json = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        snapshot_id = hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
        own_connection = _conn is None
        conn = _conn if _conn is not None else self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO observations (
                    snapshot_id, source, dataset, source_record_id, symbol, market,
                    currency_or_unit, source_as_of, source_event_sequence, ingested_at,
                    schema_version, transform_version, fallback_used,
                    payload_json, payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id) DO NOTHING
                """,
                (
                    snapshot_id,
                    source,
                    dataset,
                    source_record_id,
                    symbol,
                    market,
                    currency_or_unit,
                    source_as_of_text,
                    source_event_sequence,
                    ingested_at_text,
                    schema_version,
                    transform_version,
                    int(fallback_used),
                    payload_json,
                    payload_sha256,
                ),
            )
            row = conn.execute(
                "SELECT * FROM observations WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("source_observation_v2_readback_failed")
            expected_immutable = {
                "source": source,
                "dataset": dataset,
                "source_record_id": source_record_id,
                "symbol": symbol,
                "market": market,
                "currency_or_unit": currency_or_unit,
                "source_as_of": source_as_of_text,
                "source_event_sequence": source_event_sequence,
                "schema_version": schema_version,
                "transform_version": transform_version,
                "fallback_used": int(fallback_used),
                "payload_json": payload_json,
                "payload_sha256": payload_sha256,
            }
            stored_ingested_at = row["ingested_at"]
            if (
                any(row[key] != value for key, value in expected_immutable.items())
                or not self._is_canonical_utc_text(stored_ingested_at)
                or stored_ingested_at < source_as_of_text
                or stored_ingested_at > ingested_at_text
            ):
                raise ValueError("source_observation_v2_conflict")
            if own_connection:
                conn.commit()
            return AppendResult(
                id=int(row["id"]),
                snapshot_id=snapshot_id,
                payload_sha256=payload_sha256,
                inserted=cursor.rowcount == 1,
            )
        except Exception:
            if own_connection:
                conn.rollback()
            raise
        finally:
            if own_connection:
                conn.close()

    def record_collection_run(
        self,
        *,
        source: str,
        dataset: str,
        run_id: str,
        started_at: datetime,
        completed_at: datetime,
        status: str,
        rows_seen: int,
        rows_inserted: int,
        rows_duplicate: int,
        rows_skipped: int,
        rows_invalid: int,
        error_type: str,
        _conn: sqlite3.Connection | None = None,
    ) -> CollectionRunResult:
        """Append one immutable collection-run ledger row."""
        if type(source) is not str or not _NAME_RE.fullmatch(source):
            raise ValueError("source_invalid")
        if _sensitive_text(source):
            raise ValueError("collection_run_source_sensitive")
        if type(dataset) is not str or not _NAME_RE.fullmatch(dataset):
            raise ValueError("dataset_invalid")
        if _sensitive_text(dataset):
            raise ValueError("collection_run_dataset_sensitive")
        if type(run_id) is not str or not _SOURCE_RECORD_ID_RE.fullmatch(run_id):
            raise ValueError("collection_run_id_invalid")
        if _sensitive_text(run_id):
            raise ValueError("collection_run_id_sensitive")
        started_at_text = self._utc_text(started_at)
        completed_at_text = self._utc_text(completed_at)
        if started_at_text > completed_at_text:
            raise ValueError("collection_run_time_invalid")
        if type(status) is not str or status not in _RUN_STATUSES:
            raise ValueError("collection_run_status_invalid")
        counts = (
            rows_seen,
            rows_inserted,
            rows_duplicate,
            rows_skipped,
            rows_invalid,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("collection_run_count_invalid")
        if rows_seen != rows_inserted + rows_duplicate + rows_skipped + rows_invalid:
            raise ValueError("collection_run_count_mismatch")
        if type(error_type) is not str:
            raise ValueError("collection_run_error_type_invalid")
        if _sensitive_text(error_type):
            raise ValueError("collection_run_error_type_sensitive")
        if error_type and not _ERROR_TYPE_RE.fullmatch(error_type):
            raise ValueError("collection_run_error_type_invalid")
        if status == "success" and error_type:
            raise ValueError("collection_run_success_error_type")
        if status == "failed" and not error_type:
            raise ValueError("collection_run_failed_error_type")

        values = {
            "source": source,
            "dataset": dataset,
            "run_id": run_id,
            "started_at": started_at_text,
            "completed_at": completed_at_text,
            "status": status,
            "rows_seen": rows_seen,
            "rows_inserted": rows_inserted,
            "rows_duplicate": rows_duplicate,
            "rows_skipped": rows_skipped,
            "rows_invalid": rows_invalid,
            "error_type": error_type,
        }
        canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        expected = {**values, "fingerprint": fingerprint}

        own_connection = _conn is None
        conn = _conn if _conn is not None else self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO collection_runs (
                    source, dataset, run_id, started_at, completed_at, status,
                    rows_seen, rows_inserted, rows_duplicate, rows_skipped,
                    rows_invalid, error_type, fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, dataset, run_id) DO NOTHING
                """,
                tuple(expected[key] for key in (
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
                )),
            )
            row = conn.execute(
                """
                SELECT * FROM collection_runs
                WHERE source = ? AND dataset = ? AND run_id = ?
                """,
                (source, dataset, run_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("collection_run_readback_failed")
            if any(row[key] != value for key, value in expected.items()):
                raise ValueError("collection_run_conflict")
            if own_connection:
                conn.commit()
            return CollectionRunResult(
                id=int(row["id"]),
                source=source,
                dataset=dataset,
                run_id=run_id,
                status=status,
                fingerprint=fingerprint,
                inserted=cursor.rowcount == 1,
            )
        except BaseException:
            if own_connection:
                conn.rollback()
            raise
        finally:
            if own_connection:
                conn.close()

    def latest_collection_run(self, source: str, dataset: str) -> CollectionRun | None:
        """Return the latest completed run for one source/dataset pair."""
        if type(source) is not str or not _NAME_RE.fullmatch(source):
            raise ValueError("source_invalid")
        if type(dataset) is not str or not _NAME_RE.fullmatch(dataset):
            raise ValueError("dataset_invalid")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM collection_runs
                WHERE source = ? AND dataset = ?
                ORDER BY completed_at DESC, id DESC
                LIMIT 1
                """,
                (source, dataset),
            ).fetchone()
        if row is None:
            return None
        return CollectionRun(
            id=int(row["id"]),
            source=str(row["source"]),
            dataset=str(row["dataset"]),
            run_id=str(row["run_id"]),
            started_at=str(row["started_at"]),
            completed_at=str(row["completed_at"]),
            status=str(row["status"]),
            rows_seen=int(row["rows_seen"]),
            rows_inserted=int(row["rows_inserted"]),
            rows_duplicate=int(row["rows_duplicate"]),
            rows_skipped=int(row["rows_skipped"]),
            rows_invalid=int(row["rows_invalid"]),
            error_type=str(row["error_type"]),
            fingerprint=str(row["fingerprint"]),
        )

    def latest_as_of(
        self,
        *,
        decision_at: datetime,
        source: str,
        dataset: str,
        symbol: str,
        market: str,
    ) -> SourceObservation | None:
        cutoff = self._utc_text(decision_at)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM observations
                WHERE source = ? AND dataset = ? AND symbol = ? AND market = ?
                  AND source_as_of <= ? AND ingested_at <= ?
                ORDER BY source_as_of DESC, source_event_sequence DESC,
                         ingested_at DESC, id DESC
                LIMIT 1
                """,
                (source, dataset, symbol, market, cutoff, cutoff),
            ).fetchone()
        if row is None:
            return None
        source_as_of_text = row["source_as_of"]
        ingested_at_text = row["ingested_at"]
        if (
            not self._is_canonical_utc_text(source_as_of_text)
            or not self._is_canonical_utc_text(ingested_at_text)
            or source_as_of_text > ingested_at_text
        ):
            raise ValueError("source_observation_v2_conflict")
        return SourceObservation(
            id=int(row["id"]),
            snapshot_id=str(row["snapshot_id"]),
            source=str(row["source"]),
            dataset=str(row["dataset"]),
            source_record_id=str(row["source_record_id"]),
            symbol=str(row["symbol"]),
            market=str(row["market"]),
            currency_or_unit=str(row["currency_or_unit"]),
            source_as_of=str(row["source_as_of"]),
            source_event_sequence=int(row["source_event_sequence"]),
            ingested_at=str(row["ingested_at"]),
            schema_version=int(row["schema_version"]),
            transform_version=int(row["transform_version"]),
            fallback_used=bool(row["fallback_used"]),
            payload=json.loads(str(row["payload_json"])),
            payload_sha256=str(row["payload_sha256"]),
        )


SourceObservationStore = SourceObservationStoreV2
