"""Append-only storage for point-in-time market-source observations."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SENSITIVE_PAYLOAD_KEYS = frozenset(
    {
        "account_no",
        "account_number",
        "api_key",
        "app_key",
        "app_secret",
        "authorization",
        "crtfc_key",
        "client_secret",
        "private_key",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "token",
    }
)
_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_SOURCE_RECORD_ID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9_][A-Z0-9._:\-]{0,31}$")
_CURRENCY_RE = re.compile(r"^(?:[A-Z]{3}|N/A)$")
_SENSITIVE_VALUE_RE = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|"
    r"AIza[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{12,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?:api[_-]?key|crtfc[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"authorization|password|secret|token)\s*(?:=|:)\s*[^&\s,;]{4,}",
    re.IGNORECASE,
)
_MARKETS = frozenset({"GLOBAL", "KR", "US"})


def _contains_sensitive_text(value: str) -> bool:
    return bool(
        _SENSITIVE_VALUE_RE.search(value) or _SENSITIVE_ASSIGNMENT_RE.search(value)
    )


def _is_sensitive_key(value: str) -> bool:
    return value in _SENSITIVE_PAYLOAD_KEYS or any(
        value.endswith(f"_{suffix}") for suffix in _SENSITIVE_PAYLOAD_KEYS
    )


@dataclass(frozen=True)
class AppendResult:
    id: int
    snapshot_id: str
    payload_sha256: str
    inserted: bool


@dataclass(frozen=True)
class SourceObservation:
    id: int
    snapshot_id: str
    source: str
    source_record_id: str
    symbol: str
    market: str
    currency: str
    source_as_of: str
    ingested_at: str
    schema_version: int
    fallback_used: bool
    payload: dict[str, Any]
    payload_sha256: str


@dataclass(frozen=True)
class SourceSummary:
    source: str
    observation_count: int
    symbol_count: int
    fallback_count: int
    latest_source_as_of: str
    latest_ingested_at: str


@dataclass(frozen=True)
class SourceHealth:
    source: str
    observation_count: int
    symbol_count: int
    fallback_count: int
    latest_source_as_of: str | None
    latest_ingested_at: str | None
    latest_run_status: str | None
    latest_run_completed_at: str | None
    latest_rows_seen: int | None
    latest_rows_inserted: int | None
    latest_rows_duplicate: int | None
    latest_rows_skipped: int | None
    latest_rows_invalid: int | None
    latest_error_type: str


@dataclass(frozen=True)
class CollectionRunResult:
    id: int
    run_id: str
    status: str
    inserted: bool


@dataclass(frozen=True)
class CollectionRun:
    id: int
    source: str
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


class SourceObservationStore:
    """SQLite append-only observation store with exact-payload idempotency."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        initialize: bool = True,
        read_only: bool = False,
    ):
        self.db_path = Path(db_path)
        self._read_only = read_only
        self._local = threading.local()
        if initialize:
            if read_only:
                raise ValueError("read_only_store_cannot_initialize")
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_schema()
        else:
            if not self.db_path.is_file():
                raise FileNotFoundError(self.db_path)
            with self._connect() as conn:
                self._verify_schema(conn)

    @classmethod
    def open_read_only(cls, db_path: str | Path) -> "SourceObservationStore":
        return cls(db_path, initialize=False, read_only=True)

    def _connect(self) -> sqlite3.Connection:
        if self._read_only:
            conn = sqlite3.connect(
                self.db_path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=1.0,
            )
            conn.execute("PRAGMA query_only = ON")
        else:
            conn = sqlite3.connect(str(self.db_path), timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 1000")
        return conn

    @contextmanager
    def _write_connection(self):
        active = getattr(self._local, "transaction_conn", None)
        if active is not None:
            yield active
            return
        with self._connect() as conn:
            yield conn

    @contextmanager
    def atomic_write(self):
        if self._read_only:
            raise RuntimeError("read_only_store")
        if getattr(self._local, "transaction_conn", None) is not None:
            raise RuntimeError("nested_source_observation_transaction")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._local.transaction_conn = conn
            yield self
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._local.transaction_conn = None
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS source_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id TEXT NOT NULL UNIQUE,
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
                CREATE INDEX IF NOT EXISTS idx_source_observations_lookup
                    ON source_observations(source, symbol, source_as_of, ingested_at);
                CREATE TRIGGER IF NOT EXISTS trg_source_observations_no_update
                BEFORE UPDATE ON source_observations
                BEGIN
                    SELECT RAISE(ABORT, 'source_observations_append_only');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_source_observations_no_delete
                BEFORE DELETE ON source_observations
                BEGIN
                    SELECT RAISE(ABORT, 'source_observations_append_only');
                END;
                CREATE TABLE IF NOT EXISTS source_collection_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    rows_seen INTEGER NOT NULL,
                    rows_inserted INTEGER NOT NULL,
                    rows_duplicate INTEGER NOT NULL,
                    rows_skipped INTEGER NOT NULL,
                    rows_invalid INTEGER NOT NULL,
                    error_type TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    UNIQUE(source, run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_source_collection_runs_latest
                    ON source_collection_runs(source, completed_at DESC);
                CREATE TRIGGER IF NOT EXISTS trg_source_collection_runs_no_update
                BEFORE UPDATE ON source_collection_runs
                BEGIN
                    SELECT RAISE(ABORT, 'source_collection_runs_append_only');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_source_collection_runs_no_delete
                BEFORE DELETE ON source_collection_runs
                BEGIN
                    SELECT RAISE(ABORT, 'source_collection_runs_append_only');
                END;
                """
            )
            self._verify_schema(conn)

    @staticmethod
    def _index_columns(conn: sqlite3.Connection, table: str) -> list[list[str]]:
        unique_indexes: list[list[str]] = []
        for row in conn.execute(
            'SELECT name, "unique" AS is_unique, partial FROM pragma_index_list(?)',
            (table,),
        ):
            if int(row["is_unique"]) != 1 or int(row["partial"]) != 0:
                continue
            name = str(row["name"])
            key_columns = [
                info
                for info in conn.execute(
                    "SELECT cid, name, desc, coll, key FROM pragma_index_xinfo(?)",
                    (name,),
                )
                if int(info["key"]) == 1
            ]
            if not key_columns or any(
                int(info["cid"]) < 0
                or info["name"] is None
                or int(info["desc"]) != 0
                or str(info["coll"]).upper() != "BINARY"
                for info in key_columns
            ):
                continue
            unique_indexes.append([str(info["name"]) for info in key_columns])
        return unique_indexes

    @classmethod
    def _verify_schema(cls, conn: sqlite3.Connection) -> None:
        observation_info = list(conn.execute("PRAGMA table_info(source_observations)"))
        observation_columns = {str(row["name"]) for row in observation_info}
        required_observation_metadata = [
            ("id", "INTEGER", 0, 1),
            ("snapshot_id", "TEXT", 1, 0),
            ("source", "TEXT", 1, 0),
            ("source_record_id", "TEXT", 1, 0),
            ("symbol", "TEXT", 1, 0),
            ("market", "TEXT", 1, 0),
            ("currency", "TEXT", 1, 0),
            ("source_as_of", "TEXT", 1, 0),
            ("ingested_at", "TEXT", 1, 0),
            ("schema_version", "INTEGER", 1, 0),
            ("fallback_used", "INTEGER", 1, 0),
            ("payload_json", "TEXT", 1, 0),
            ("payload_sha256", "TEXT", 1, 0),
        ]
        if "currency" not in observation_columns and observation_columns:
            raise RuntimeError("source_observations_schema_incompatible:missing_currency")
        observed_metadata = [
            (
                str(row["name"]),
                str(row["type"]).upper(),
                int(row["notnull"]),
                int(row["pk"]),
            )
            for row in observation_info
        ]
        if observed_metadata != required_observation_metadata:
            raise RuntimeError("source_observations_schema_incompatible:column_metadata")

        run_info = list(conn.execute("PRAGMA table_info(source_collection_runs)"))
        required_run_metadata = [
            ("id", "INTEGER", 0, 1),
            ("source", "TEXT", 1, 0),
            ("run_id", "TEXT", 1, 0),
            ("started_at", "TEXT", 1, 0),
            ("completed_at", "TEXT", 1, 0),
            ("status", "TEXT", 1, 0),
            ("rows_seen", "INTEGER", 1, 0),
            ("rows_inserted", "INTEGER", 1, 0),
            ("rows_duplicate", "INTEGER", 1, 0),
            ("rows_skipped", "INTEGER", 1, 0),
            ("rows_invalid", "INTEGER", 1, 0),
            ("error_type", "TEXT", 1, 0),
            ("fingerprint", "TEXT", 1, 0),
        ]
        observed_run_metadata = [
            (
                str(row["name"]),
                str(row["type"]).upper(),
                int(row["notnull"]),
                int(row["pk"]),
            )
            for row in run_info
        ]
        if observed_run_metadata != required_run_metadata:
            raise RuntimeError("source_observations_schema_incompatible:run_metadata")
        if ["snapshot_id"] not in cls._index_columns(conn, "source_observations"):
            raise RuntimeError("source_observations_schema_incompatible:snapshot_unique")
        if ["source", "run_id"] not in cls._index_columns(
            conn, "source_collection_runs"
        ):
            raise RuntimeError("source_observations_schema_incompatible:run_unique")
        trigger_rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
        trigger_sql = {str(row["name"]): str(row["sql"] or "") for row in trigger_rows}
        required_triggers = {
            "trg_source_observations_no_update": """
                CREATE TRIGGER trg_source_observations_no_update
                BEFORE UPDATE ON source_observations
                BEGIN
                    SELECT RAISE(ABORT, 'source_observations_append_only');
                END
            """,
            "trg_source_observations_no_delete": """
                CREATE TRIGGER trg_source_observations_no_delete
                BEFORE DELETE ON source_observations
                BEGIN
                    SELECT RAISE(ABORT, 'source_observations_append_only');
                END
            """,
            "trg_source_collection_runs_no_update": """
                CREATE TRIGGER trg_source_collection_runs_no_update
                BEFORE UPDATE ON source_collection_runs
                BEGIN
                    SELECT RAISE(ABORT, 'source_collection_runs_append_only');
                END
            """,
            "trg_source_collection_runs_no_delete": """
                CREATE TRIGGER trg_source_collection_runs_no_delete
                BEFORE DELETE ON source_collection_runs
                BEGIN
                    SELECT RAISE(ABORT, 'source_collection_runs_append_only');
                END
            """,
        }

        def normalize_trigger_sql(sql: str) -> str:
            normalized = " ".join(sql.strip().rstrip(";").lower().split())
            return normalized.replace("create trigger if not exists", "create trigger")

        for name, expected_sql in required_triggers.items():
            if normalize_trigger_sql(trigger_sql.get(name, "")) != normalize_trigger_sql(
                expected_sql
            ):
                raise RuntimeError(
                    f"source_observations_schema_incompatible:trigger:{name}"
                )

    @staticmethod
    def _require_aware(name: str, value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name}_must_be_timezone_aware")

    @staticmethod
    def _utc_text(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _database_bool(value: Any) -> bool:
        if type(value) is not int or value not in (0, 1):
            raise RuntimeError("source_observation_fallback_used_invalid")
        return value == 1

    @classmethod
    def _validate_payload(cls, value: Any, path: str = "$") -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise ValueError(f"payload_key_not_string:{path}")
                snake_key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key.strip())
                normalized = snake_key.lower().replace("-", "_").replace(" ", "_")
                if _is_sensitive_key(normalized):
                    raise ValueError(f"payload_sensitive_key:{path}.{key}")
                cls._validate_payload(nested, f"{path}.{key}")
            return
        if isinstance(value, list):
            for index, nested in enumerate(value):
                cls._validate_payload(nested, f"{path}[{index}]")
            return
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"payload_non_finite:{path}")
        if isinstance(value, str) and _contains_sensitive_text(value):
            raise ValueError(f"payload_sensitive_value:{path}")
        if value is None or isinstance(value, (str, int, float, bool)):
            return
        raise ValueError(f"payload_type_unsupported:{path}")

    @staticmethod
    def _validate_metadata(
        *,
        source: str,
        source_record_id: str,
        symbol: str,
        market: str,
        currency: str,
        schema_version: int,
        fallback_used: bool,
        payload: Any,
    ) -> None:
        if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source):
            raise ValueError("source_invalid")
        if (
            not isinstance(source_record_id, str)
            or not _SOURCE_RECORD_ID_RE.fullmatch(source_record_id)
        ):
            raise ValueError("source_record_id_invalid")
        if _contains_sensitive_text(source_record_id):
            raise ValueError("source_record_id_sensitive")
        if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
            raise ValueError("symbol_invalid")
        if type(market) is not str or market not in _MARKETS:
            raise ValueError("market_invalid")
        if type(currency) is not str or not _CURRENCY_RE.fullmatch(currency):
            raise ValueError("currency_invalid")
        if type(schema_version) is not int or schema_version <= 0:
            raise ValueError("schema_version_invalid")
        if type(fallback_used) is not bool:
            raise ValueError("fallback_used_invalid")
        if not isinstance(payload, dict):
            raise ValueError("payload_must_be_object")

    def append(
        self,
        *,
        source: str,
        source_record_id: str,
        symbol: str,
        market: str,
        currency: str,
        source_as_of: datetime,
        ingested_at: datetime,
        schema_version: int,
        fallback_used: bool,
        payload: dict[str, Any],
    ) -> AppendResult:
        self._validate_metadata(
            source=source,
            source_record_id=source_record_id,
            symbol=symbol,
            market=market,
            currency=currency,
            schema_version=schema_version,
            fallback_used=fallback_used,
            payload=payload,
        )
        self._require_aware("source_as_of", source_as_of)
        self._require_aware("ingested_at", ingested_at)
        if source_as_of > ingested_at:
            raise ValueError("source_as_of_after_ingested_at")
        self._validate_payload(payload)
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        identity = "\x1f".join(
            (
                source,
                source_record_id,
                symbol,
                market,
                currency,
                self._utc_text(source_as_of),
                str(schema_version),
                str(int(fallback_used)),
                payload_sha256,
            )
        )
        snapshot_id = "srcobs_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()

        with self._write_connection() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO source_observations (
                    snapshot_id, source, source_record_id, symbol, market, currency,
                    source_as_of, ingested_at, schema_version, fallback_used,
                    payload_json, payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    source,
                    source_record_id,
                    symbol,
                    market,
                    currency,
                    self._utc_text(source_as_of),
                    self._utc_text(ingested_at),
                    schema_version,
                    int(fallback_used),
                    payload_json,
                    payload_sha256,
                ),
            )
            row = conn.execute(
                """
                SELECT id, source, source_record_id, symbol, market, currency,
                       source_as_of, schema_version, fallback_used,
                       payload_json, payload_sha256
                FROM source_observations
                WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("source_observation_insert_readback_failed")
            expected = (
                source,
                source_record_id,
                symbol,
                market,
                currency,
                self._utc_text(source_as_of),
                schema_version,
                int(fallback_used),
                payload_json,
                payload_sha256,
            )
            observed = (
                row["source"],
                row["source_record_id"],
                row["symbol"],
                row["market"],
                row["currency"],
                row["source_as_of"],
                row["schema_version"],
                row["fallback_used"],
                row["payload_json"],
                row["payload_sha256"],
            )
            if observed != expected:
                raise ValueError("source_observation_conflict")
            return AppendResult(
                id=int(row["id"]),
                snapshot_id=snapshot_id,
                payload_sha256=payload_sha256,
                inserted=cur.rowcount == 1,
            )

    def latest_as_of(
        self,
        *,
        source: str,
        symbol: str,
        decision_at: datetime,
    ) -> SourceObservation | None:
        self._require_aware("decision_at", decision_at)
        cutoff = self._utc_text(decision_at)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM source_observations
                WHERE source = ?
                  AND symbol = ?
                  AND source_as_of <= ?
                  AND ingested_at <= ?
                ORDER BY source_as_of DESC, ingested_at DESC, id DESC
                LIMIT 1
                """,
                (source, symbol, cutoff, cutoff),
            ).fetchone()
        if row is None:
            return None
        return SourceObservation(
            id=int(row["id"]),
            snapshot_id=str(row["snapshot_id"]),
            source=str(row["source"]),
            source_record_id=str(row["source_record_id"]),
            symbol=str(row["symbol"]),
            market=str(row["market"]),
            currency=str(row["currency"]),
            source_as_of=str(row["source_as_of"]),
            ingested_at=str(row["ingested_at"]),
            schema_version=int(row["schema_version"]),
            fallback_used=self._database_bool(row["fallback_used"]),
            payload=json.loads(str(row["payload_json"])),
            payload_sha256=str(row["payload_sha256"]),
        )

    def count(self, *, source: str, symbol: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM source_observations WHERE source = ? AND symbol = ?",
                (source, symbol),
            ).fetchone()
        return int(row["n"])

    def summaries(self) -> list[SourceSummary]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    source,
                    COUNT(*) AS observation_count,
                    COUNT(DISTINCT symbol) AS symbol_count,
                    SUM(fallback_used) AS fallback_count,
                    MAX(source_as_of) AS latest_source_as_of,
                    MAX(ingested_at) AS latest_ingested_at
                FROM source_observations
                GROUP BY source
                ORDER BY source
                """
            ).fetchall()
        return [
            SourceSummary(
                source=str(row["source"]),
                observation_count=int(row["observation_count"]),
                symbol_count=int(row["symbol_count"]),
                fallback_count=int(row["fallback_count"] or 0),
                latest_source_as_of=str(row["latest_source_as_of"]),
                latest_ingested_at=str(row["latest_ingested_at"]),
            )
            for row in rows
        ]

    def record_collection_run(
        self,
        *,
        source: str,
        run_id: str,
        started_at: datetime,
        completed_at: datetime,
        rows_seen: int,
        rows_inserted: int,
        rows_duplicate: int,
        rows_skipped: int,
        rows_invalid: int,
        error_type: str,
    ) -> CollectionRunResult:
        if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source):
            raise ValueError("source_invalid")
        if not isinstance(run_id, str) or not _SOURCE_RECORD_ID_RE.fullmatch(run_id):
            raise ValueError("run_id_invalid")
        if _contains_sensitive_text(run_id):
            raise ValueError("collection_run_id_sensitive")
        self._require_aware("started_at", started_at)
        self._require_aware("completed_at", completed_at)
        if started_at > completed_at:
            raise ValueError("collection_run_time_invalid")
        counts = (
            rows_seen,
            rows_inserted,
            rows_duplicate,
            rows_skipped,
            rows_invalid,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("collection_run_count_invalid")
        classified_rows = rows_inserted + rows_duplicate + rows_skipped + rows_invalid
        if (not error_type and classified_rows != rows_seen) or classified_rows > rows_seen:
            raise ValueError("collection_run_count_mismatch")
        if (
            not isinstance(error_type, str)
            or len(error_type) > 128
            or any(ord(char) < 32 for char in error_type)
        ):
            raise ValueError("collection_run_error_type_invalid")
        if _contains_sensitive_text(error_type):
            raise ValueError("collection_run_error_type_sensitive")

        status = "failed" if error_type else ("partial" if rows_invalid else "success")
        values = {
            "source": source,
            "run_id": run_id,
            "started_at": self._utc_text(started_at),
            "completed_at": self._utc_text(completed_at),
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

        with self._write_connection() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO source_collection_runs (
                    source, run_id, started_at, completed_at, status,
                    rows_seen, rows_inserted, rows_duplicate, rows_skipped, rows_invalid,
                    error_type, fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    run_id,
                    values["started_at"],
                    values["completed_at"],
                    status,
                    rows_seen,
                    rows_inserted,
                    rows_duplicate,
                    rows_skipped,
                    rows_invalid,
                    error_type,
                    fingerprint,
                ),
            )
            row = conn.execute(
                """
                SELECT id, status, fingerprint
                FROM source_collection_runs
                WHERE source = ? AND run_id = ?
                """,
                (source, run_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("source_collection_run_insert_readback_failed")
            if str(row["fingerprint"]) != fingerprint:
                raise ValueError("collection_run_conflict")
            return CollectionRunResult(
                id=int(row["id"]),
                run_id=run_id,
                status=str(row["status"]),
                inserted=cur.rowcount == 1,
            )

    def latest_collection_run(self, *, source: str) -> CollectionRun | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM source_collection_runs
                WHERE source = ?
                ORDER BY completed_at DESC, id DESC
                LIMIT 1
                """,
                (source,),
            ).fetchone()
        if row is None:
            return None
        return CollectionRun(
            id=int(row["id"]),
            source=str(row["source"]),
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
        )

    def source_health(self) -> list[SourceHealth]:
        with self._connect() as conn:
            conn.execute("BEGIN")
            summary_rows = conn.execute(
                """
                SELECT source, COUNT(*) AS observation_count,
                       COUNT(DISTINCT symbol) AS symbol_count,
                       SUM(fallback_used) AS fallback_count,
                       MAX(source_as_of) AS latest_source_as_of,
                       MAX(ingested_at) AS latest_ingested_at
                FROM source_observations
                GROUP BY source
                """
            ).fetchall()
            latest_run_rows = conn.execute(
                """
                SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY source ORDER BY completed_at DESC, id DESC
                    ) AS row_number
                    FROM source_collection_runs
                ) WHERE row_number = 1
                """
            ).fetchall()
            source_rows = conn.execute(
                """
                SELECT source FROM source_observations
                UNION
                SELECT source FROM source_collection_runs
                ORDER BY source
                """
            ).fetchall()
            conn.commit()

        summaries = {str(row["source"]): row for row in summary_rows}
        latest_runs = {str(row["source"]): row for row in latest_run_rows}
        health: list[SourceHealth] = []
        for row in source_rows:
            source = str(row["source"])
            summary = summaries.get(source)
            run = latest_runs.get(source)
            health.append(
                SourceHealth(
                    source=source,
                    observation_count=int(summary["observation_count"]) if summary else 0,
                    symbol_count=int(summary["symbol_count"]) if summary else 0,
                    fallback_count=int(summary["fallback_count"] or 0) if summary else 0,
                    latest_source_as_of=(
                        str(summary["latest_source_as_of"]) if summary else None
                    ),
                    latest_ingested_at=(
                        str(summary["latest_ingested_at"]) if summary else None
                    ),
                    latest_run_status=str(run["status"]) if run else None,
                    latest_run_completed_at=(
                        str(run["completed_at"]) if run else None
                    ),
                    latest_rows_seen=int(run["rows_seen"]) if run else None,
                    latest_rows_inserted=int(run["rows_inserted"]) if run else None,
                    latest_rows_duplicate=int(run["rows_duplicate"]) if run else None,
                    latest_rows_skipped=int(run["rows_skipped"]) if run else None,
                    latest_rows_invalid=int(run["rows_invalid"]) if run else None,
                    latest_error_type=str(run["error_type"]) if run else "",
                )
            )
        return health
