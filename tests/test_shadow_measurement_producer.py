"""Final Toss candidate cohort shadow-producer contract tests."""

from __future__ import annotations

import copy
import importlib
import json
import logging
import math
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


def _candidate(symbol: str = "005930.KS", **overrides) -> dict:
    candidate = {
        "symbol": symbol,
        "side": "buy",
        "market": "KR",
        "currency": "KRW",
        "decision_bucket": "PASS_EXECUTE",
        "quality_score": 75.0,
        "quality_breakdown": {
            "score_total": 75.0,
            "score_momentum": 18.0,
            "decision_bucket": "PASS_EXECUTE",
            "decision_reason": "quality pass",
            "decision_has_stop": True,
        },
        "stock_agent_ready": True,
        "executable_now": True,
        "execution_status": "ready",
        "decision_reason": "quality pass",
        "income_strategy": {
            "income_pass": True,
            "income_grade": "INCOME_PASS",
            "expected_pnl_krw": 12_000.0,
        },
        "account": "must-not-leak",
        "cash_check": {"available": 123456789},
        "broker_raw": {"authorization": "must-not-leak"},
    }
    candidate.update(overrides)
    return candidate


def test_projection_is_deterministic_and_does_not_mutate_candidate():
    from core.shadow_measurement_producer import project_final_candidate

    candidate = _candidate()
    original = copy.deepcopy(candidate)

    first = project_final_candidate(
        candidate,
        cohort_position=0,
        cohort_size=1,
        market_scope="KR",
    )
    second = project_final_candidate(
        candidate,
        cohort_position=0,
        cohort_size=1,
        market_scope="KR",
    )

    assert first == second
    assert candidate == original


@pytest.mark.parametrize(
    "bucket",
    [
        "PASS_EXECUTE",
        "SMALL_PASS",
        "WAIT_PULLBACK",
        "WATCH",
        "CHASE_BLOCK",
        "BLOCK",
    ],
)
def test_projection_accepts_each_raw_final_bucket(bucket):
    from core.shadow_measurement_producer import FEATURE_SET_VERSION, project_final_candidate

    candidate = _candidate(decision_bucket=bucket)
    candidate["quality_breakdown"]["decision_bucket"] = bucket

    projected = project_final_candidate(
        candidate,
        cohort_position=2,
        cohort_size=4,
        market_scope="KR",
    )

    assert FEATURE_SET_VERSION == "toss_final_candidate_v1"
    assert projected["feature_set_version"] == FEATURE_SET_VERSION
    assert projected["symbol"] == "005930.KS"
    assert projected["side"] == "BUY"
    assert projected["market"] == "KR"
    assert projected["currency"] == "KRW"
    assert projected["production_bucket"] == bucket
    assert projected["production_score"] == 75.0
    assert projected["score_proof"]["decision_bucket"] == bucket
    assert projected["cohort_position"] == 2
    assert projected["cohort_size"] == 4
    assert projected["market_scope"] == "KR"


def test_projection_uses_breakdown_score_total_when_quality_score_is_absent():
    from core.shadow_measurement_producer import project_final_candidate

    candidate = _candidate()
    candidate.pop("quality_score")

    projected = project_final_candidate(
        candidate,
        cohort_position=0,
        cohort_size=1,
        market_scope="KR",
    )

    assert projected["production_score"] == 75.0


def _project(candidate: dict, *, position: int = 0, size: int = 1) -> dict:
    from core.shadow_measurement_producer import project_final_candidate

    return project_final_candidate(
        candidate,
        cohort_position=position,
        cohort_size=size,
        market_scope="KR",
    )


def test_canonical_hash_is_full_sha256_and_changes_with_decision_inputs():
    from core.shadow_measurement_producer import (
        candidate_snapshot_sha256,
        canonical_json,
    )

    base = _candidate()
    score_changed = _candidate(quality_score=76.0)
    bucket_changed = _candidate(decision_bucket="SMALL_PASS")
    bucket_changed["quality_breakdown"]["decision_bucket"] = "SMALL_PASS"
    feature_changed = _candidate()
    feature_changed["quality_breakdown"]["score_momentum"] = 19.0

    projections = [
        _project(base, position=0, size=2),
        _project(score_changed, position=0, size=2),
        _project(bucket_changed, position=0, size=2),
        _project(feature_changed, position=0, size=2),
        _project(base, position=1, size=2),
    ]
    hashes = [candidate_snapshot_sha256(item) for item in projections]

    assert len(set(hashes)) == len(hashes)
    assert all(len(value) == 64 for value in hashes)
    assert canonical_json(projections[0]) == json.dumps(
        projections[0],
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def test_projection_excludes_raw_account_cash_broker_and_unknown_fields():
    projected = _project(_candidate(api_key="also-must-not-leak", random_raw={"x": 1}))
    serialized = json.dumps(projected, sort_keys=True)

    for forbidden in (
        "account",
        "cash_check",
        "broker_raw",
        "authorization",
        "api_key",
        "random_raw",
        "must-not-leak",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    "mutate, error",
    [
        (lambda item: item.__setitem__("stock_agent_ready", "true"), "string_boolean|invalid"),
        (lambda item: item.__setitem__("quality_score", math.nan), "non_finite"),
        (
            lambda item: item["quality_breakdown"].__setitem__("score_momentum", math.inf),
            "non_finite",
        ),
        (
            lambda item: item["quality_breakdown"].__setitem__("decision_has_stop", "false"),
            "invalid",
        ),
    ],
)
def test_projection_rejects_string_booleans_and_nonfinite_values(mutate, error):
    candidate = _candidate()
    mutate(candidate)

    with pytest.raises(ValueError, match=error):
        _project(candidate)


@pytest.mark.parametrize(
    "field_path,sensitive_text",
    [
        (("decision_reason",), "appKey=abcd1234"),
        (("block_reason",), "accountNo:12345678"),
        (("quality_breakdown", "decision_reason"), "clientSecret=abcd1234"),
        (("income_strategy", "income_block_reason"), "privateKey:abcd1234"),
        (("income_strategy", "income_block_label"), "authorization=abcd1234"),
        (("income_strategy", "income_grade"), "Bearer ABCDEFGHIJKL"),
        (("decision_reason",), "token=abcd1234"),
        (("block_reason",), "password:abcd1234"),
        (("decision_reason",), "eyJabcdefgh.ijklmnop.qrstuvwx"),
    ],
)
def test_projection_reuses_shadow_contract_to_reject_allowlisted_secret_values(
    field_path, sensitive_text
):
    candidate = _candidate()
    target = candidate
    for part in field_path[:-1]:
        target = target[part]
    target[field_path[-1]] = sensitive_text

    with pytest.raises(ValueError, match="payload_sensitive_value"):
        _project(candidate)


@pytest.mark.parametrize(
    "field_path",
    [
        ("decision_reason",),
        ("block_reason",),
        ("quality_breakdown", "decision_reason"),
        ("quality_breakdown", "risk_flags"),
        ("income_strategy", "income_block_reason"),
        ("income_strategy", "income_block_label"),
        ("missing_fields",),
    ],
)
def test_projection_rejects_naked_broker_account_in_every_free_text_shape(field_path):
    candidate = _candidate()
    target = candidate
    for part in field_path[:-1]:
        target = target[part]
    target[field_path[-1]] = (
        ["broker reference 12345678-01"]
        if field_path[-1] in {"risk_flags", "missing_fields"}
        else "broker reference 12345678-01"
    )

    with pytest.raises(ValueError, match="broker_account_number"):
        _project(candidate)


@pytest.mark.parametrize(
    "candidate,market_scope,error",
    [
        (_candidate(market="US", currency="USD"), "KR", "market_scope_mismatch"),
        (_candidate(), "US", "market_scope_mismatch"),
        (_candidate(currency="USD"), "KR", "market_currency_mismatch"),
        (_candidate(market="US", currency="KRW"), "ALL", "market_currency_mismatch"),
        (_candidate(market="kr"), "KR", "market_invalid"),
        (_candidate(currency="krw"), "KR", "currency_invalid"),
        (
            _candidate("005930.KS", market="US", currency="USD"),
            "ALL",
            "symbol_market_mismatch",
        ),
        (
            _candidate("AAPL", market="KR", currency="KRW"),
            "ALL",
            "symbol_market_mismatch",
        ),
        (
            _candidate("005930", market="US", currency="USD"),
            "ALL",
            "symbol_market_mismatch",
        ),
    ],
)
def test_projection_rejects_market_scope_and_currency_contract_mismatches(
    candidate, market_scope, error
):
    from core.shadow_measurement_producer import project_final_candidate

    with pytest.raises(ValueError, match=error):
        project_final_candidate(
            candidate,
            cohort_position=0,
            cohort_size=1,
            market_scope=market_scope,
        )


@pytest.mark.parametrize(
    "candidate,market_scope",
    [
        (_candidate("035720.KQ"), "KR"),
        (_candidate("BRK.B", market="US", currency="USD"), "US"),
    ],
)
def test_projection_accepts_valid_kr_and_us_symbol_conventions(candidate, market_scope):
    from core.shadow_measurement_producer import project_final_candidate

    projected = project_final_candidate(
        candidate,
        cohort_position=0,
        cohort_size=1,
        market_scope=market_scope,
    )
    assert projected["symbol"] == candidate["symbol"]


def test_fallback_is_strict_batch_metadata_and_changes_projection_hash():
    from core.shadow_measurement_producer import sanitize_final_candidate_cohort

    without_fallback = sanitize_final_candidate_cohort(
        [_candidate()],
        market_scope="KR",
        captured_at_utc=_CAPTURED_AT,
        fallback_used=False,
    )
    with_fallback = sanitize_final_candidate_cohort(
        [_candidate()],
        market_scope="KR",
        captured_at_utc=_CAPTURED_AT,
        fallback_used=True,
    )

    assert without_fallback.fallback_used is False
    assert with_fallback.fallback_used is True
    assert json.loads(without_fallback.candidates[0].payload_json)["fallback_used"] is False
    assert json.loads(with_fallback.candidates[0].payload_json)["fallback_used"] is True
    assert (
        without_fallback.candidates[0].candidate_snapshot_sha256
        != with_fallback.candidates[0].candidate_snapshot_sha256
    )


def test_direct_sanitize_rejects_string_fallback_boolean():
    from core.shadow_measurement_producer import sanitize_final_candidate_cohort

    with pytest.raises(ValueError, match="fallback_used_invalid"):
        sanitize_final_candidate_cohort(
            [_candidate()],
            market_scope="KR",
            captured_at_utc=_CAPTURED_AT,
            fallback_used="false",
        )


def test_secret_candidate_is_rejected_before_worker_batch_boundary():
    from core.shadow_measurement_producer import sanitize_final_candidate_cohort

    batch = sanitize_final_candidate_cohort(
        [
            _candidate("005930.KS", decision_reason="token=not-for-worker"),
            _candidate("035720.KS", decision_reason="reference 12345678-01"),
            _candidate("000660.KS"),
        ],
        market_scope="KR",
        captured_at_utc=_CAPTURED_AT,
        fallback_used=False,
    )

    assert batch.rejected_count == 2
    assert len(batch.candidates) == 1
    assert "not-for-worker" not in repr(batch)
    assert "12345678-01" not in repr(batch)


_CAPTURED_AT = datetime(2026, 7, 15, 3, 4, 5, 678901, tzinfo=timezone.utc)


def _real_stores(tmp_path):
    from core.shadow_measurements import ShadowMeasurementStore
    from core.source_observations import SourceObservationStore

    source = SourceObservationStore(tmp_path / "source_observations.db")
    shadow = ShadowMeasurementStore(
        tmp_path / "shadow_measurements.db",
        now_fn=lambda: _CAPTURED_AT + timedelta(seconds=1),
    )
    return source, shadow


def test_naked_account_candidate_creates_no_source_or_shadow_sqlite_rows(tmp_path):
    from core.shadow_measurement_producer import (
        persist_final_candidate_cohort,
        sanitize_final_candidate_cohort,
    )

    source, shadow = _real_stores(tmp_path)
    batch = sanitize_final_candidate_cohort(
        [_candidate(decision_reason="broker reference 12345678-01")],
        market_scope="KR",
        captured_at_utc=_CAPTURED_AT,
        fallback_used=False,
    )

    assert batch.rejected_count == 1
    assert batch.candidates == ()
    assert persist_final_candidate_cohort(
        batch, source_store=source, shadow_store=shadow
    ) == {"seen": 0, "persisted": 0, "failed": 0}
    with sqlite3.connect(source.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 0
    with sqlite3.connect(shadow.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM shadow_decisions").fetchone()[0] == 0


def test_real_sqlite_persistence_links_equal_source_and_candidate_hashes(tmp_path):
    from core.shadow_measurement_producer import (
        decision_id_for_source_snapshot,
        persist_final_candidate_cohort,
        sanitize_final_candidate_cohort,
    )

    source, shadow = _real_stores(tmp_path)
    batch = sanitize_final_candidate_cohort(
        [_candidate()],
        market_scope="KR",
        captured_at_utc=_CAPTURED_AT,
        fallback_used=True,
    )

    result = persist_final_candidate_cohort(
        batch,
        source_store=source,
        shadow_store=shadow,
    )

    assert result["persisted"] == 1
    assert len(batch.candidates) == 1
    sanitized = batch.candidates[0]
    observation = source.latest_as_of(
        source="toss_final_candidate",
        symbol="005930.KS",
        decision_at=_CAPTURED_AT,
    )
    assert observation is not None
    assert observation.source_as_of == _CAPTURED_AT.isoformat()
    assert observation.ingested_at == _CAPTURED_AT.isoformat()
    assert observation.fallback_used is True
    assert observation.payload_sha256 == sanitized.candidate_snapshot_sha256
    assert len(observation.payload_sha256) == 64

    expected_id = decision_id_for_source_snapshot(observation.snapshot_id)
    decision = shadow.get_decision(expected_id)
    assert decision is not None
    assert decision.decision_id == expected_id
    assert decision.decision_ref == f"shadow:{expected_id}"
    assert decision.candidate_snapshot_sha256 == observation.payload_sha256
    assert decision.features["fallback_used"] is True
    assert decision.source_snapshots == [
        {
            "snapshot_id": observation.snapshot_id,
            "source": "toss_final_candidate",
            "ingested_at_utc": _CAPTURED_AT.isoformat(),
            "payload_sha256": observation.payload_sha256,
        }
    ]


def test_decision_identity_uses_feature_version_and_exact_source_snapshot():
    import hashlib

    from core.shadow_measurement_producer import (
        FEATURE_SET_VERSION,
        canonical_json,
        decision_id_for_source_snapshot,
    )

    snapshot_id = "srcobs_" + "a" * 64
    expected = "tossq_" + hashlib.sha256(
        canonical_json(
            {
                "feature_set_version": FEATURE_SET_VERSION,
                "source_snapshot_id": snapshot_id,
            }
        ).encode("utf-8")
    ).hexdigest()

    assert decision_id_for_source_snapshot(snapshot_id) == expected
    assert len(expected.removeprefix("tossq_")) == 64


def test_exact_batch_retry_is_idempotent_in_both_real_sqlite_stores(tmp_path):
    from core.shadow_measurement_producer import (
        persist_final_candidate_cohort,
        sanitize_final_candidate_cohort,
    )

    source, shadow = _real_stores(tmp_path)
    batch = sanitize_final_candidate_cohort(
        [_candidate()],
        market_scope="KR",
        captured_at_utc=_CAPTURED_AT,
        fallback_used=False,
    )

    first = persist_final_candidate_cohort(
        batch,
        source_store=source,
        shadow_store=shadow,
    )
    second = persist_final_candidate_cohort(
        batch,
        source_store=source,
        shadow_store=shadow,
    )

    assert first["persisted"] == 1
    assert second["persisted"] == 1
    with sqlite3.connect(source.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 1
    with sqlite3.connect(shadow.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM shadow_decisions").fetchone()[0] == 1


def test_shadow_failure_leaves_source_orphan_and_continues_with_next_candidate(
    tmp_path, caplog
):
    from core.shadow_measurement_producer import (
        decision_id_for_source_snapshot,
        persist_final_candidate_cohort,
        sanitize_final_candidate_cohort,
    )

    source, real_shadow = _real_stores(tmp_path)

    class FailFirstShadowAppend:
        def __init__(self, inner):
            self.inner = inner
            self.calls = 0

        def append_decision(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise sqlite3.OperationalError("secret-payload-must-not-be-logged")
            return self.inner.append_decision(**kwargs)

    batch = sanitize_final_candidate_cohort(
        [_candidate("005930.KS"), _candidate("000660.KS")],
        market_scope="KR",
        captured_at_utc=_CAPTURED_AT,
        fallback_used=False,
    )
    with caplog.at_level(logging.WARNING):
        result = persist_final_candidate_cohort(
            batch,
            source_store=source,
            shadow_store=FailFirstShadowAppend(real_shadow),
        )

    assert result == {"seen": 2, "persisted": 1, "failed": 1}
    assert source.count(source="toss_final_candidate", symbol="005930.KS") == 1
    assert source.count(source="toss_final_candidate", symbol="000660.KS") == 1
    first_source = source.latest_as_of(
        source="toss_final_candidate",
        symbol="005930.KS",
        decision_at=_CAPTURED_AT,
    )
    second_source = source.latest_as_of(
        source="toss_final_candidate",
        symbol="000660.KS",
        decision_at=_CAPTURED_AT,
    )
    assert first_source is not None and second_source is not None
    assert real_shadow.get_decision(
        decision_id_for_source_snapshot(first_source.snapshot_id)
    ) is None
    assert real_shadow.get_decision(
        decision_id_for_source_snapshot(second_source.snapshot_id)
    ) is not None
    assert "OperationalError" in caplog.text
    assert "failed_count=1" in caplog.text
    assert "secret-payload-must-not-be-logged" not in caplog.text


def test_enqueue_busy_drops_with_a_nonblocking_lock_attempt(monkeypatch):
    import core.shadow_measurement_producer as producer

    class BusyLock:
        def __init__(self):
            self.blocking_values = []

        def acquire(self, blocking=True):
            self.blocking_values.append(blocking)
            return False

        def release(self):  # pragma: no cover - busy path must not release
            raise AssertionError("busy lock must not be released by the dropper")

    busy_lock = BusyLock()
    monkeypatch.setattr(producer, "_WORKER_LOCK", busy_lock)

    accepted = producer.enqueue_final_candidate_cohort(
        [_candidate()],
        market_scope="KR",
        captured_at_utc=_CAPTURED_AT,
        fallback_used=False,
    )

    assert accepted is False
    assert busy_lock.blocking_values == [False]


def test_enqueue_starts_one_daemon_with_only_an_immutable_sanitized_batch(
    tmp_path, monkeypatch
):
    import core.shadow_measurement_producer as producer

    source, shadow = _real_stores(tmp_path)
    started = []

    class InlineThread:
        def __init__(self, *, target, args, daemon, name):
            started.append(
                {"target": target, "args": args, "daemon": daemon, "name": name}
            )

        def start(self):
            call = started[-1]
            call["target"](*call["args"])

    monkeypatch.setattr(producer.threading, "Thread", InlineThread)
    raw = _candidate(raw_candidate_secret="must-never-reach-worker")

    accepted = producer.enqueue_final_candidate_cohort(
        [raw],
        market_scope="KR",
        captured_at_utc=_CAPTURED_AT,
        fallback_used=False,
        source_store=source,
        shadow_store=shadow,
    )

    assert accepted is True
    assert len(started) == 1
    assert started[0]["daemon"] is True
    batch = started[0]["args"][0]
    assert isinstance(batch, producer.SanitizedFinalCandidateCohort)
    assert isinstance(batch.candidates, tuple)
    assert "must-never-reach-worker" not in repr(batch)
    assert source.count(source="toss_final_candidate", symbol="005930.KS") == 1


def test_real_thread_returns_before_db_completion_and_never_writes_on_main_thread():
    import core.shadow_measurement_producer as producer

    main_thread_id = threading.get_ident()
    source_entered = threading.Event()
    release_source = threading.Event()
    shadow_finished = threading.Event()
    observed = {}

    class BlockingSource:
        def append(self, **kwargs):
            observed["source_thread_id"] = threading.get_ident()
            observed["source_payload"] = kwargs["payload"]
            source_entered.set()
            assert release_source.wait(timeout=2)
            return SimpleNamespace(
                snapshot_id="srcobs_" + "a" * 64,
                payload_sha256=producer.candidate_snapshot_sha256(kwargs["payload"]),
            )

    class RecordingShadow:
        def append_decision(self, **kwargs):
            observed["shadow_thread_id"] = threading.get_ident()
            observed["shadow_features"] = kwargs["features"]
            shadow_finished.set()

    accepted = producer.enqueue_final_candidate_cohort(
        [_candidate(raw_account="must-not-reach-worker")],
        market_scope="KR",
        captured_at_utc=_CAPTURED_AT,
        fallback_used=False,
        source_store=BlockingSource(),
        shadow_store=RecordingShadow(),
    )

    assert accepted is True
    assert source_entered.wait(timeout=1)
    assert shadow_finished.is_set() is False
    assert observed["source_thread_id"] != main_thread_id
    assert "must-not-reach-worker" not in repr(observed["source_payload"])
    release_source.set()
    assert shadow_finished.wait(timeout=1)
    assert observed["shadow_thread_id"] != main_thread_id
    assert "must-not-reach-worker" not in repr(observed["shadow_features"])


def test_enqueue_thread_start_failure_releases_lock_without_logging_exception_text(
    monkeypatch, caplog
):
    import core.shadow_measurement_producer as producer

    class FailingThread:
        def __init__(self, *, target, args, daemon, name):
            assert daemon is True

        def start(self):
            raise RuntimeError("secret-start-error-must-not-be-logged")

    monkeypatch.setattr(producer.threading, "Thread", FailingThread)

    with caplog.at_level(logging.WARNING):
        accepted = producer.enqueue_final_candidate_cohort(
            [_candidate()],
            market_scope="KR",
            captured_at_utc=_CAPTURED_AT,
            fallback_used=False,
        )

    assert accepted is False
    assert producer._WORKER_LOCK.acquire(blocking=False) is True
    producer._WORKER_LOCK.release()
    assert "RuntimeError" in caplog.text
    assert "failed_count=1" in caplog.text
    assert "secret-start-error-must-not-be-logged" not in caplog.text


def test_import_and_pure_projection_do_not_construct_default_databases(
    tmp_path, monkeypatch
):
    import config.settings as settings

    db_dir = tmp_path / "lazy-default-db"
    monkeypatch.setattr(settings, "DB_DIR", db_dir)
    sys.modules.pop("core.shadow_measurement_producer", None)

    producer = importlib.import_module("core.shadow_measurement_producer")
    assert not db_dir.exists()

    producer.project_final_candidate(
        _candidate(),
        cohort_position=0,
        cohort_size=1,
        market_scope="KR",
    )

    assert not db_dir.exists()
