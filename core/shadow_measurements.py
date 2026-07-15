"""Append-only shadow decision/outcome measurement storage.

0A storage contract only: this module is not wired to collectors, monitors,
scoring, orders, cron, dashboards, or services.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "account_no",
        "account_number",
        "api_key",
        "app_key",
        "app_secret",
        "authorization",
        "client_secret",
        "crtfc_key",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|"
    r"AIza[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{12,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT_NAMES = "|".join(
    r"[\s._-]*".join(re.escape(part) for part in key.split("_"))
    for key in sorted(_SENSITIVE_KEYS, key=lambda item: (-len(item), item))
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    rf"(?:{_SENSITIVE_ASSIGNMENT_NAMES})\s*(?:=|:)\s*[^&\s,;]{{4,}}",
    re.IGNORECASE,
)
_OUTCOME_HORIZONS = frozenset({"5m", "30m", "1d", "3d", "5d", "10d"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SNAPSHOT_ID_RE = re.compile(r"^srcobs_[0-9a-f]{64}$")
_DECISION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DECISION_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
_BUCKET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_FEATURE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DECISION_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS shadow_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
_OUTCOME_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS shadow_outcomes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id TEXT NOT NULL,
        horizon TEXT NOT NULL,
        evaluated_at_utc TEXT NOT NULL,
        outcome_json TEXT NOT NULL,
        immutable_payload_sha256 TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        UNIQUE(decision_id, horizon),
        FOREIGN KEY(decision_id)
            REFERENCES shadow_decisions(decision_id)
    )
"""
_SENSITIVE_KEY_SEQUENCES = tuple(
    tuple(key.split("_")) for key in sorted(_SENSITIVE_KEYS)
)
_SENSITIVE_COLLAPSED_KEYS = tuple(
    sorted(
        {key.replace("_", "") for key in _SENSITIVE_KEYS},
        key=lambda item: (-len(item), item),
    )
)


def _key_tokens(value: str) -> tuple[str, ...]:
    camel_split = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value.strip())
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", camel_split)
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", camel_split).strip("_").lower()
    return tuple(part for part in normalized.split("_") if part)


def _is_sensitive_key(value: str) -> bool:
    tokens = _key_tokens(value)
    return any(token in _SENSITIVE_COLLAPSED_KEYS for token in tokens) or any(
        tokens[index : index + len(sequence)] == sequence
        for sequence in _SENSITIVE_KEY_SEQUENCES
        for index in range(len(tokens) - len(sequence) + 1)
    )


def _contains_sensitive_value(value: str) -> bool:
    return bool(
        _SENSITIVE_VALUE_RE.search(value) or _SENSITIVE_ASSIGNMENT_RE.search(value)
    )


def _reject_sensitive_text(name: str, value: str) -> None:
    if _contains_sensitive_value(value):
        raise ValueError(f"{name}_sensitive")


_APPEND_ONLY_TRIGGER_SQL = {
    "trg_shadow_decisions_no_update": """
        CREATE TRIGGER IF NOT EXISTS trg_shadow_decisions_no_update
        BEFORE UPDATE ON shadow_decisions
        BEGIN
            SELECT RAISE(ABORT, 'shadow_decisions_append_only');
        END
    """,
    "trg_shadow_decisions_no_delete": """
        CREATE TRIGGER IF NOT EXISTS trg_shadow_decisions_no_delete
        BEFORE DELETE ON shadow_decisions
        BEGIN
            SELECT RAISE(ABORT, 'shadow_decisions_append_only');
        END
    """,
    "trg_shadow_outcomes_no_update": """
        CREATE TRIGGER IF NOT EXISTS trg_shadow_outcomes_no_update
        BEFORE UPDATE ON shadow_outcomes
        BEGIN
            SELECT RAISE(ABORT, 'shadow_outcomes_append_only');
        END
    """,
    "trg_shadow_outcomes_no_delete": """
        CREATE TRIGGER IF NOT EXISTS trg_shadow_outcomes_no_delete
        BEFORE DELETE ON shadow_outcomes
        BEGIN
            SELECT RAISE(ABORT, 'shadow_outcomes_append_only');
        END
    """,
}


@dataclass(frozen=True)
class DecisionAppendResult:
    id: int
    decision_id: str
    immutable_payload_sha256: str
    inserted: bool


@dataclass(frozen=True)
class ShadowDecision:
    id: int
    decision_id: str
    decision_ref: str
    symbol: str
    side: str
    decided_at_utc: str
    production_bucket: str
    production_score: float
    feature_set_version: str
    features: dict[str, Any]
    source_snapshots: list[dict[str, Any]]
    candidate_snapshot_sha256: str
    immutable_payload_sha256: str
    created_at_utc: str


@dataclass(frozen=True)
class OutcomeAppendResult:
    id: int
    decision_id: str
    horizon: str
    immutable_payload_sha256: str
    inserted: bool


@dataclass(frozen=True)
class ShadowOutcome:
    id: int
    decision_id: str
    horizon: str
    evaluated_at_utc: str
    outcome: dict[str, Any]
    immutable_payload_sha256: str
    created_at_utc: str


class ShadowMeasurementStore:
    """SQLite store for immutable production-baseline and shadow measurements."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 1000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_DECISION_TABLE_SQL)
            conn.execute(_OUTCOME_TABLE_SQL)
            for trigger_sql in _APPEND_ONLY_TRIGGER_SQL.values():
                conn.execute(trigger_sql)
            self._verify_schema(conn)

    @staticmethod
    def _table_metadata(
        conn: sqlite3.Connection, table: str
    ) -> list[tuple[str, str, int, str | None, int, int]]:
        return [
            (
                str(row[1]),
                str(row[2]).upper(),
                int(row[3]),
                None if row[4] is None else str(row[4]),
                int(row[5]),
                int(row[6]),
            )
            for row in conn.execute(f"PRAGMA table_xinfo({table})")
        ]

    @staticmethod
    def _unique_index_signatures(
        conn: sqlite3.Connection, table: str
    ) -> list[tuple[str, int, tuple[tuple[str, str, int], ...]]]:
        result: list[tuple[str, int, tuple[tuple[str, str, int], ...]]] = []
        for row in conn.execute(f"PRAGMA index_list({table})"):
            if int(row[2]) != 1:
                continue
            index_name = str(row[1]).replace('"', '""')
            signature = tuple(
                (
                    "<expression>" if info[2] is None else str(info[2]),
                    str(info[4]).upper(),
                    int(info[3]),
                )
                for info in conn.execute(f'PRAGMA index_xinfo("{index_name}")')
                if int(info[5]) == 1
            )
            result.append((str(row[3]).lower(), int(row[4]), signature))
        return sorted(result)

    @staticmethod
    def _normalize_schema_sql(sql: str) -> str:
        normalized = " ".join(sql.strip().rstrip(";").lower().split())
        return normalized.replace("create table if not exists", "create table")

    @staticmethod
    def _normalize_trigger_sql(sql: str) -> str:
        normalized = " ".join(sql.strip().rstrip(";").lower().split())
        return normalized.replace("create trigger if not exists", "create trigger")

    @classmethod
    def _verify_schema(cls, conn: sqlite3.Connection) -> None:
        expected_decisions = [
            ("id", "INTEGER", 0, None, 1, 0),
            ("decision_id", "TEXT", 1, None, 0, 0),
            ("decision_ref", "TEXT", 1, None, 0, 0),
            ("symbol", "TEXT", 1, None, 0, 0),
            ("side", "TEXT", 1, None, 0, 0),
            ("decided_at_utc", "TEXT", 1, None, 0, 0),
            ("production_bucket", "TEXT", 1, None, 0, 0),
            ("production_score", "REAL", 1, None, 0, 0),
            ("feature_set_version", "TEXT", 1, None, 0, 0),
            ("features_json", "TEXT", 1, None, 0, 0),
            ("source_snapshots_json", "TEXT", 1, None, 0, 0),
            ("candidate_snapshot_sha256", "TEXT", 1, None, 0, 0),
            ("immutable_payload_sha256", "TEXT", 1, None, 0, 0),
            ("created_at_utc", "TEXT", 1, None, 0, 0),
        ]
        expected_outcomes = [
            ("id", "INTEGER", 0, None, 1, 0),
            ("decision_id", "TEXT", 1, None, 0, 0),
            ("horizon", "TEXT", 1, None, 0, 0),
            ("evaluated_at_utc", "TEXT", 1, None, 0, 0),
            ("outcome_json", "TEXT", 1, None, 0, 0),
            ("immutable_payload_sha256", "TEXT", 1, None, 0, 0),
            ("created_at_utc", "TEXT", 1, None, 0, 0),
        ]
        if cls._table_metadata(conn, "shadow_decisions") != expected_decisions:
            raise RuntimeError("shadow_measurements_schema_incompatible:decisions")
        if cls._table_metadata(conn, "shadow_outcomes") != expected_outcomes:
            raise RuntimeError("shadow_measurements_schema_incompatible:outcomes")
        if cls._unique_index_signatures(conn, "shadow_decisions") != [
            ("u", 0, (("decision_id", "BINARY", 0),))
        ]:
            raise RuntimeError(
                "shadow_measurements_schema_incompatible:decision_unique"
            )
        if cls._unique_index_signatures(conn, "shadow_outcomes") != [
            (
                "u",
                0,
                (
                    ("decision_id", "BINARY", 0),
                    ("horizon", "BINARY", 0),
                ),
            )
        ]:
            raise RuntimeError(
                "shadow_measurements_schema_incompatible:outcome_unique"
            )

        outcome_foreign_keys = [
            (
                str(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]).upper(),
                str(row[6]).upper(),
                str(row[7]).upper(),
            )
            for row in conn.execute("PRAGMA foreign_key_list(shadow_outcomes)")
        ]
        if outcome_foreign_keys != [
            (
                "shadow_decisions",
                "decision_id",
                "decision_id",
                "NO ACTION",
                "NO ACTION",
                "NONE",
            )
        ]:
            raise RuntimeError(
                "shadow_measurements_schema_incompatible:outcome_foreign_key"
            )

        table_sql = {
            str(row[0]): str(row[1] or "")
            for row in conn.execute(
                """
                SELECT name, sql FROM sqlite_master
                WHERE type = 'table' AND name IN ('shadow_decisions', 'shadow_outcomes')
                """
            )
        }
        for table, expected_sql, error_name in (
            ("shadow_decisions", _DECISION_TABLE_SQL, "decision_table_sql"),
            ("shadow_outcomes", _OUTCOME_TABLE_SQL, "outcome_table_sql"),
        ):
            if cls._normalize_schema_sql(table_sql.get(table, "")) != (
                cls._normalize_schema_sql(expected_sql)
            ):
                raise RuntimeError(
                    f"shadow_measurements_schema_incompatible:{error_name}"
                )

        actual_triggers = {
            str(row[0]): str(row[1] or "")
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        if set(actual_triggers) != set(_APPEND_ONLY_TRIGGER_SQL):
            raise RuntimeError("shadow_measurements_schema_incompatible:trigger_set")
        for name, expected_sql in _APPEND_ONLY_TRIGGER_SQL.items():
            if cls._normalize_trigger_sql(actual_triggers.get(name, "")) != (
                cls._normalize_trigger_sql(expected_sql)
            ):
                raise RuntimeError(
                    f"shadow_measurements_schema_incompatible:trigger:{name}"
                )

    @staticmethod
    def _require_aware(name: str, value: datetime) -> None:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name}_must_be_timezone_aware")

    @classmethod
    def _utc_text(cls, name: str, value: datetime) -> str:
        cls._require_aware(name, value)
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _validate_json_contract(cls, value: Any, path: str = "$") -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise ValueError(f"payload_key_not_string:{path}")
                if _is_sensitive_key(key):
                    raise ValueError(f"payload_sensitive_key:{path}.{key}")
                cls._validate_json_contract(nested, f"{path}.{key}")
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                cls._validate_json_contract(nested, f"{path}[{index}]")
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"payload_non_finite:{path}")
        elif isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            raise ValueError(f"payload_string_boolean:{path}")
        elif isinstance(value, str) and _contains_sensitive_value(value):
            raise ValueError(f"payload_sensitive_value:{path}")
        elif value is None or isinstance(value, (str, int, float, bool)):
            return
        else:
            raise ValueError(f"payload_type_unsupported:{path}")

    @staticmethod
    def _validate_decision_metadata(
        *,
        decision_id: str,
        decision_ref: str,
        symbol: str,
        side: str,
        production_bucket: str,
        feature_set_version: str,
    ) -> None:
        if type(decision_id) is not str or not _DECISION_ID_RE.fullmatch(decision_id):
            raise ValueError("decision_id_invalid")
        if type(decision_ref) is not str or not _DECISION_REF_RE.fullmatch(
            decision_ref
        ):
            raise ValueError("decision_ref_invalid")
        if type(symbol) is not str or not _SYMBOL_RE.fullmatch(symbol):
            raise ValueError("symbol_invalid")
        if type(side) is not str or side not in {"BUY", "SELL"}:
            raise ValueError("side_invalid")
        if type(production_bucket) is not str or not _BUCKET_RE.fullmatch(
            production_bucket
        ):
            raise ValueError("production_bucket_invalid")
        if type(feature_set_version) is not str or not _FEATURE_VERSION_RE.fullmatch(
            feature_set_version
        ):
            raise ValueError("feature_set_version_invalid")
        for name, value in (
            ("decision_id", decision_id),
            ("decision_ref", decision_ref),
            ("symbol", symbol),
            ("production_bucket", production_bucket),
            ("feature_set_version", feature_set_version),
        ):
            _reject_sensitive_text(name, value)

    @staticmethod
    def _decision_immutable_payload(
        *,
        decision_id: str,
        decision_ref: str,
        symbol: str,
        side: str,
        decided_at_utc: str,
        production_bucket: str,
        production_score: float,
        feature_set_version: str,
        features: dict[str, Any],
        source_snapshots: list[dict[str, Any]],
        candidate_snapshot_sha256: str,
    ) -> dict[str, Any]:
        return {
            "decision_id": decision_id,
            "decision_ref": decision_ref,
            "symbol": symbol,
            "side": side,
            "decided_at_utc": decided_at_utc,
            "production_bucket": production_bucket,
            "production_score": production_score,
            "feature_set_version": feature_set_version,
            "features": features,
            "source_snapshots": source_snapshots,
            "candidate_snapshot_sha256": candidate_snapshot_sha256,
        }

    @staticmethod
    def _outcome_immutable_payload(
        *,
        decision_id: str,
        horizon: str,
        evaluated_at_utc: str,
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "decision_id": decision_id,
            "horizon": horizon,
            "evaluated_at_utc": evaluated_at_utc,
            "outcome": outcome,
        }

    @classmethod
    def _normalize_source_snapshots(
        cls,
        source_snapshots: list[dict[str, Any]],
        *,
        decided_at_utc: datetime,
    ) -> list[dict[str, Any]]:
        if not isinstance(source_snapshots, list) or not source_snapshots:
            raise ValueError("source_snapshots_required")
        normalized: list[dict[str, Any]] = []
        seen_snapshot_ids: set[str] = set()
        for item in source_snapshots:
            if not isinstance(item, dict):
                raise ValueError("source_snapshot_must_be_object")
            required = {
                "snapshot_id",
                "source",
                "ingested_at_utc",
                "payload_sha256",
            }
            if set(item) != required:
                raise ValueError("source_snapshot_fields_invalid")
            snapshot_id = item["snapshot_id"]
            if type(snapshot_id) is not str or not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
                raise ValueError("source_snapshot_id_invalid")
            if snapshot_id in seen_snapshot_ids:
                raise ValueError("source_snapshot_duplicate")
            seen_snapshot_ids.add(snapshot_id)
            source = item["source"]
            if type(source) is not str or not _SOURCE_RE.fullmatch(source):
                raise ValueError("source_invalid")
            _reject_sensitive_text("source", source)
            payload_sha256 = item["payload_sha256"]
            if type(payload_sha256) is not str or not _SHA256_RE.fullmatch(
                payload_sha256
            ):
                raise ValueError("source_payload_sha256_invalid")
            ingested = item["ingested_at_utc"]
            cls._require_aware("source_snapshot_ingested_at_utc", ingested)
            if ingested > decided_at_utc:
                raise ValueError("source_snapshot_from_future")
            normalized.append(
                {
                    "snapshot_id": snapshot_id,
                    "source": source,
                    "ingested_at_utc": cls._utc_text(
                        "source_snapshot_ingested_at_utc", ingested
                    ),
                    "payload_sha256": payload_sha256,
                }
            )
        normalized.sort(key=lambda item: (item["source"], item["snapshot_id"]))
        return normalized

    def append_decision(
        self,
        *,
        decision_id: str,
        decision_ref: str,
        symbol: str,
        side: str,
        decided_at_utc: datetime,
        production_bucket: str,
        production_score: float,
        feature_set_version: str,
        features: dict[str, Any],
        source_snapshots: list[dict[str, Any]],
        candidate_snapshot_sha256: str,
    ) -> DecisionAppendResult:
        self._validate_decision_metadata(
            decision_id=decision_id,
            decision_ref=decision_ref,
            symbol=symbol,
            side=side,
            production_bucket=production_bucket,
            feature_set_version=feature_set_version,
        )
        self._require_aware("decided_at_utc", decided_at_utc)
        created_at = self._now_fn()
        self._require_aware("created_at_utc", created_at)
        if created_at < decided_at_utc:
            raise ValueError("created_at_before_decision")
        if not isinstance(features, dict):
            raise ValueError("features_must_be_object")
        self._validate_json_contract(features)
        if (
            type(candidate_snapshot_sha256) is not str
            or not _SHA256_RE.fullmatch(candidate_snapshot_sha256)
        ):
            raise ValueError("candidate_snapshot_sha256_invalid")
        if isinstance(production_score, bool) or not isinstance(
            production_score, (int, float)
        ) or not math.isfinite(float(production_score)):
            raise ValueError("production_score_invalid")

        normalized_sources = self._normalize_source_snapshots(
            source_snapshots,
            decided_at_utc=decided_at_utc,
        )
        decided_text = self._utc_text("decided_at_utc", decided_at_utc)
        created_text = self._utc_text("created_at_utc", created_at)
        features_json = self._canonical_json(features)
        source_snapshots_json = self._canonical_json(normalized_sources)
        immutable = self._decision_immutable_payload(
            decision_id=decision_id,
            decision_ref=decision_ref,
            symbol=symbol,
            side=side,
            decided_at_utc=decided_text,
            production_bucket=production_bucket,
            production_score=float(production_score),
            feature_set_version=feature_set_version,
            features=features,
            source_snapshots=normalized_sources,
            candidate_snapshot_sha256=candidate_snapshot_sha256,
        )
        immutable_json = self._canonical_json(immutable)
        immutable_hash = hashlib.sha256(immutable_json.encode("utf-8")).hexdigest()

        values = (
            decision_id,
            decision_ref,
            symbol,
            side,
            decided_text,
            production_bucket,
            float(production_score),
            feature_set_version,
            features_json,
            source_snapshots_json,
            candidate_snapshot_sha256,
            immutable_hash,
            created_text,
        )
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO shadow_decisions (
                    decision_id, decision_ref, symbol, side, decided_at_utc,
                    production_bucket, production_score, feature_set_version,
                    features_json, source_snapshots_json,
                    candidate_snapshot_sha256, immutable_payload_sha256,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            row = conn.execute(
                "SELECT * FROM shadow_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("shadow_decision_insert_readback_failed")
            validated = self._decision_from_row(row)
            observed = tuple(row[key] for key in (
                "decision_id",
                "decision_ref",
                "symbol",
                "side",
                "decided_at_utc",
                "production_bucket",
                "production_score",
                "feature_set_version",
                "features_json",
                "source_snapshots_json",
                "candidate_snapshot_sha256",
                "immutable_payload_sha256",
            ))
            if observed != values[:-1]:
                raise ValueError("shadow_decision_conflict")
            return DecisionAppendResult(
                id=validated.id,
                decision_id=validated.decision_id,
                immutable_payload_sha256=validated.immutable_payload_sha256,
                inserted=cur.rowcount == 1,
            )

    @classmethod
    def _decision_from_row(cls, row: sqlite3.Row) -> ShadowDecision:
        try:
            decision_id = str(row["decision_id"])
            decision_ref = str(row["decision_ref"])
            symbol = str(row["symbol"])
            side = str(row["side"])
            production_bucket = str(row["production_bucket"])
            feature_set_version = str(row["feature_set_version"])
            cls._validate_decision_metadata(
                decision_id=decision_id,
                decision_ref=decision_ref,
                symbol=symbol,
                side=side,
                production_bucket=production_bucket,
                feature_set_version=feature_set_version,
            )

            score = row["production_score"]
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise ValueError("production_score_invalid")
            production_score = float(score)
            if not math.isfinite(production_score):
                raise ValueError("production_score_invalid")

            features_json = str(row["features_json"])
            features = json.loads(features_json)
            if not isinstance(features, dict):
                raise ValueError("features_must_be_object")
            cls._validate_json_contract(features)
            if cls._canonical_json(features) != features_json:
                raise ValueError("features_json_not_canonical")

            decided_text = str(row["decided_at_utc"])
            decided_at = datetime.fromisoformat(decided_text)
            cls._require_aware("decided_at_utc", decided_at)
            if cls._utc_text("decided_at_utc", decided_at) != decided_text:
                raise ValueError("decided_at_utc_not_canonical")

            created_text = str(row["created_at_utc"])
            created_at = datetime.fromisoformat(created_text)
            cls._require_aware("created_at_utc", created_at)
            if cls._utc_text("created_at_utc", created_at) != created_text:
                raise ValueError("created_at_utc_not_canonical")
            if created_at < decided_at:
                raise ValueError("created_at_before_decision")

            source_snapshots_json = str(row["source_snapshots_json"])
            raw_sources = json.loads(source_snapshots_json)
            if not isinstance(raw_sources, list):
                raise ValueError("source_snapshots_required")
            parsed_sources: list[dict[str, Any]] = []
            for item in raw_sources:
                if not isinstance(item, dict):
                    raise ValueError("source_snapshot_must_be_object")
                ingested_text = item.get("ingested_at_utc")
                if type(ingested_text) is not str:
                    raise ValueError("source_snapshot_ingested_at_utc_invalid")
                parsed_item = dict(item)
                parsed_item["ingested_at_utc"] = datetime.fromisoformat(ingested_text)
                parsed_sources.append(parsed_item)
            normalized_sources = cls._normalize_source_snapshots(
                parsed_sources,
                decided_at_utc=decided_at,
            )
            if cls._canonical_json(normalized_sources) != source_snapshots_json:
                raise ValueError("source_snapshots_json_not_canonical")

            candidate_hash = row["candidate_snapshot_sha256"]
            if type(candidate_hash) is not str or not _SHA256_RE.fullmatch(
                candidate_hash
            ):
                raise ValueError("candidate_snapshot_sha256_invalid")
            immutable = cls._decision_immutable_payload(
                decision_id=decision_id,
                decision_ref=decision_ref,
                symbol=symbol,
                side=side,
                decided_at_utc=decided_text,
                production_bucket=production_bucket,
                production_score=production_score,
                feature_set_version=feature_set_version,
                features=features,
                source_snapshots=normalized_sources,
                candidate_snapshot_sha256=candidate_hash,
            )
            expected_hash = hashlib.sha256(
                cls._canonical_json(immutable).encode("utf-8")
            ).hexdigest()
            stored_hash = row["immutable_payload_sha256"]
            if (
                type(stored_hash) is not str
                or not _SHA256_RE.fullmatch(stored_hash)
                or stored_hash != expected_hash
            ):
                raise ValueError("immutable_payload_sha256_invalid")
        except Exception as exc:
            raise RuntimeError("shadow_decision_corrupt") from exc

        return ShadowDecision(
            id=int(row["id"]),
            decision_id=decision_id,
            decision_ref=decision_ref,
            symbol=symbol,
            side=side,
            decided_at_utc=decided_text,
            production_bucket=production_bucket,
            production_score=production_score,
            feature_set_version=feature_set_version,
            features=features,
            source_snapshots=normalized_sources,
            candidate_snapshot_sha256=candidate_hash,
            immutable_payload_sha256=stored_hash,
            created_at_utc=created_text,
        )

    def get_decision(self, decision_id: str) -> ShadowDecision | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        return self._decision_from_row(row)

    def append_outcome(
        self,
        *,
        decision_id: str,
        horizon: str,
        evaluated_at_utc: datetime,
        outcome: dict[str, Any],
    ) -> OutcomeAppendResult:
        if type(decision_id) is not str or not _DECISION_ID_RE.fullmatch(decision_id):
            raise ValueError("decision_id_invalid")
        _reject_sensitive_text("decision_id", decision_id)
        if type(horizon) is not str or horizon not in _OUTCOME_HORIZONS:
            raise ValueError("outcome_horizon_invalid")
        self._require_aware("evaluated_at_utc", evaluated_at_utc)
        if not isinstance(outcome, dict):
            raise ValueError("outcome_must_be_object")
        self._validate_json_contract(outcome)
        created_at = self._now_fn()
        self._require_aware("created_at_utc", created_at)
        if created_at < evaluated_at_utc:
            raise ValueError("created_at_before_outcome")

        decision = self.get_decision(decision_id)
        if decision is None:
            raise ValueError("shadow_outcome_decision_missing")
        decided_at = datetime.fromisoformat(decision.decided_at_utc)
        if evaluated_at_utc < decided_at:
            raise ValueError("outcome_before_decision")

        evaluated_text = self._utc_text("evaluated_at_utc", evaluated_at_utc)
        created_text = self._utc_text("created_at_utc", created_at)
        outcome_json = self._canonical_json(outcome)
        immutable = self._outcome_immutable_payload(
            decision_id=decision_id,
            horizon=horizon,
            evaluated_at_utc=evaluated_text,
            outcome=outcome,
        )
        immutable_json = self._canonical_json(immutable)
        immutable_hash = hashlib.sha256(immutable_json.encode("utf-8")).hexdigest()
        values = (
            decision_id,
            horizon,
            evaluated_text,
            outcome_json,
            immutable_hash,
            created_text,
        )

        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO shadow_outcomes (
                    decision_id, horizon, evaluated_at_utc, outcome_json,
                    immutable_payload_sha256, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            row = conn.execute(
                """
                SELECT * FROM shadow_outcomes
                WHERE decision_id = ? AND horizon = ?
                """,
                (decision_id, horizon),
            ).fetchone()
            if row is None:
                raise RuntimeError("shadow_outcome_insert_readback_failed")
            validated = self._outcome_from_row(row)
            observed = tuple(row[key] for key in (
                "decision_id",
                "horizon",
                "evaluated_at_utc",
                "outcome_json",
                "immutable_payload_sha256",
            ))
            if observed != values[:-1]:
                raise ValueError("shadow_outcome_conflict")
            return OutcomeAppendResult(
                id=validated.id,
                decision_id=validated.decision_id,
                horizon=validated.horizon,
                immutable_payload_sha256=validated.immutable_payload_sha256,
                inserted=cur.rowcount == 1,
            )

    def _outcome_from_row(self, row: sqlite3.Row) -> ShadowOutcome:
        try:
            decision_id = str(row["decision_id"])
            if not _DECISION_ID_RE.fullmatch(decision_id):
                raise ValueError("decision_id_invalid")
            horizon = str(row["horizon"])
            if horizon not in _OUTCOME_HORIZONS:
                raise ValueError("outcome_horizon_invalid")

            outcome_json = str(row["outcome_json"])
            outcome = json.loads(outcome_json)
            if not isinstance(outcome, dict):
                raise ValueError("outcome_must_be_object")
            self._validate_json_contract(outcome)
            if self._canonical_json(outcome) != outcome_json:
                raise ValueError("outcome_json_not_canonical")

            evaluated_text = str(row["evaluated_at_utc"])
            evaluated_at = datetime.fromisoformat(evaluated_text)
            self._require_aware("evaluated_at_utc", evaluated_at)
            if self._utc_text("evaluated_at_utc", evaluated_at) != evaluated_text:
                raise ValueError("evaluated_at_utc_not_canonical")

            created_text = str(row["created_at_utc"])
            created_at = datetime.fromisoformat(created_text)
            self._require_aware("created_at_utc", created_at)
            if self._utc_text("created_at_utc", created_at) != created_text:
                raise ValueError("created_at_utc_not_canonical")
            if created_at < evaluated_at:
                raise ValueError("created_at_before_outcome")

            decision = self.get_decision(decision_id)
            if decision is None:
                raise ValueError("shadow_outcome_decision_missing")
            if evaluated_at < datetime.fromisoformat(decision.decided_at_utc):
                raise ValueError("outcome_before_decision")

            immutable = self._outcome_immutable_payload(
                decision_id=decision_id,
                horizon=horizon,
                evaluated_at_utc=evaluated_text,
                outcome=outcome,
            )
            expected_hash = hashlib.sha256(
                self._canonical_json(immutable).encode("utf-8")
            ).hexdigest()
            stored_hash = row["immutable_payload_sha256"]
            if (
                type(stored_hash) is not str
                or not _SHA256_RE.fullmatch(stored_hash)
                or stored_hash != expected_hash
            ):
                raise ValueError("immutable_payload_sha256_invalid")
        except Exception as exc:
            raise RuntimeError("shadow_outcome_corrupt") from exc

        return ShadowOutcome(
            id=int(row["id"]),
            decision_id=decision_id,
            horizon=horizon,
            evaluated_at_utc=evaluated_text,
            outcome=outcome,
            immutable_payload_sha256=stored_hash,
            created_at_utc=created_text,
        )

    def get_outcome(self, decision_id: str, horizon: str) -> ShadowOutcome | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM shadow_outcomes
                WHERE decision_id = ? AND horizon = ?
                """,
                (decision_id, horizon),
            ).fetchone()
        if row is None:
            return None
        return self._outcome_from_row(row)
