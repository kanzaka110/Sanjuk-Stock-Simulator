from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from core.shadow_measurements import ShadowMeasurementStore
from core.source_observations_v2 import SourceObservationStoreV2
from src.shadow_outcome_evaluator import evaluate_shadow_outcomes

UTC = timezone.utc
ACTIVATION = datetime(2026, 7, 21, 0, 0, tzinfo=UTC)
DECIDED = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
EVALUATED = datetime(2026, 7, 23, 22, 0, tzinfo=UTC)


def _decision(store, *, decision_id="decision_1", decided_at=DECIDED):
    return store.append_decision(
        decision_id=decision_id,
        decision_ref=f"shadow:{decision_id}",
        symbol="MU",
        side="BUY",
        decided_at_utc=decided_at,
        production_bucket="WATCH",
        production_score=70.0,
        feature_set_version="test_v1",
        features={"market": "US", "currency": "USD"},
        source_snapshots=[{
            "snapshot_id": "srcobs_" + ("a" * 64),
            "source": "toss_final_candidate",
            "ingested_at_utc": decided_at,
            "payload_sha256": "b" * 64,
        }],
        candidate_snapshot_sha256="c" * 64,
    )


def _quote(
    store,
    *,
    record_id,
    source_at,
    ingested_at=None,
    price,
    high=None,
    low=None,
):
    return store.append(
        source="yfinance",
        dataset="market_close_quote",
        source_record_id=record_id,
        symbol="MU",
        market="US",
        currency_or_unit="USD",
        source_as_of=source_at,
        source_event_sequence=0,
        ingested_at=ingested_at or source_at,
        schema_version=1,
        transform_version=1,
        fallback_used=True,
        payload={
            "change": 0.0,
            "high": high if high is not None else price,
            "low": low if low is not None else price,
            "price": price,
            "quote_source": "yf_daily",
        },
    )


def _stores(tmp_path):
    shadow_path = tmp_path / "shadow.db"
    source_path = tmp_path / "source.db"
    shadow = ShadowMeasurementStore(shadow_path, now_fn=lambda: EVALUATED)
    source = SourceObservationStoreV2(source_path)
    return shadow, source


def test_evaluator_appends_mature_after_cost_outcome_with_lineage(tmp_path):
    shadow, source = _stores(tmp_path)
    _decision(shadow)
    entry = _quote(
        source,
        record_id="entry",
        source_at=datetime(2026, 7, 21, 21, 0, tzinfo=UTC),
        price=100.0,
        high=102.0,
        low=99.0,
    )
    exit_quote = _quote(
        source,
        record_id="exit",
        source_at=datetime(2026, 7, 22, 21, 0, tzinfo=UTC),
        price=110.0,
        high=112.0,
        low=98.0,
    )
    source_before = source.db_path.read_bytes()

    result = evaluate_shadow_outcomes(
        shadow_db_path=shadow.db_path,
        source_db_path=source.db_path,
        decision_not_before_utc=ACTIVATION,
        evaluated_at_utc=EVALUATED,
        horizons=("1d",),
        shadow_store=shadow,
    )

    assert result == {
        "decisions_seen": 1,
        "labels_considered": 1,
        "inserted": 1,
        "duplicate": 0,
        "pending": 0,
        "invalid": 0,
    }
    assert source.db_path.read_bytes() == source_before
    outcome = shadow.get_outcome("decision_1", "1d")
    assert outcome is not None
    assert outcome.outcome == {
        "contract_version": "close_to_close_after_cost_v1",
        "currency": "USD",
        "decision_usable": False,
        "entry_price": 100.0,
        "entry_snapshot_id": entry.snapshot_id,
        "entry_source_as_of_utc": "2026-07-21T21:00:00.000000Z",
        "exit_price": 110.0,
        "exit_snapshot_id": exit_quote.snapshot_id,
        "exit_source_as_of_utc": "2026-07-22T21:00:00.000000Z",
        "fallback_used": True,
        "gross_return_pct": 10.0,
        "horizon_sessions": 1,
        "mae_pct": -2.0,
        "market": "US",
        "mfe_pct": 12.0,
        "return_pct_after_cost": 9.9,
        "round_trip_cost_model": "backtest_round_trip_v1",
        "round_trip_cost_pct": 0.1,
        "source_dataset": "market_close_quote",
    }


def test_evaluator_does_not_label_before_horizon_or_with_late_ingestion(tmp_path):
    shadow, source = _stores(tmp_path)
    _decision(shadow)
    _quote(
        source,
        record_id="entry",
        source_at=datetime(2026, 7, 21, 21, 0, tzinfo=UTC),
        price=100.0,
    )
    _quote(
        source,
        record_id="late-exit",
        source_at=datetime(2026, 7, 22, 21, 0, tzinfo=UTC),
        ingested_at=datetime(2026, 7, 24, 0, 0, tzinfo=UTC),
        price=110.0,
    )

    result = evaluate_shadow_outcomes(
        shadow_db_path=shadow.db_path,
        source_db_path=source.db_path,
        decision_not_before_utc=ACTIVATION,
        evaluated_at_utc=EVALUATED,
        horizons=("1d", "3d"),
        shadow_store=shadow,
    )

    assert result["inserted"] == 0
    assert result["pending"] == 2
    assert shadow.get_outcome("decision_1", "1d") is None
    assert shadow.get_outcome("decision_1", "3d") is None


def test_activation_cutoff_prevents_retroactive_labels(tmp_path):
    shadow, source = _stores(tmp_path)
    _decision(
        shadow,
        decision_id="old_decision",
        decided_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    )
    _quote(
        source,
        record_id="entry",
        source_at=datetime(2026, 7, 21, 21, 0, tzinfo=UTC),
        price=100.0,
    )
    _quote(
        source,
        record_id="exit",
        source_at=datetime(2026, 7, 22, 21, 0, tzinfo=UTC),
        price=101.0,
    )

    result = evaluate_shadow_outcomes(
        shadow_db_path=shadow.db_path,
        source_db_path=source.db_path,
        decision_not_before_utc=ACTIVATION,
        evaluated_at_utc=EVALUATED,
        horizons=("1d",),
        shadow_store=shadow,
    )

    assert result["decisions_seen"] == 0
    assert result["inserted"] == 0


def test_evaluator_is_idempotent_for_same_immutable_inputs(tmp_path):
    shadow, source = _stores(tmp_path)
    _decision(shadow)
    _quote(
        source,
        record_id="entry",
        source_at=datetime(2026, 7, 21, 21, 0, tzinfo=UTC),
        price=100.0,
    )
    _quote(
        source,
        record_id="exit",
        source_at=datetime(2026, 7, 22, 21, 0, tzinfo=UTC),
        price=101.0,
    )
    kwargs = {
        "shadow_db_path": shadow.db_path,
        "source_db_path": source.db_path,
        "decision_not_before_utc": ACTIVATION,
        "evaluated_at_utc": EVALUATED,
        "horizons": ("1d",),
        "shadow_store": shadow,
    }

    first = evaluate_shadow_outcomes(**kwargs)
    second = evaluate_shadow_outcomes(
        **{**kwargs, "evaluated_at_utc": EVALUATED.replace(day=24)}
    )

    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["duplicate"] == 1
    with sqlite3.connect(shadow.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM shadow_outcomes").fetchone()[0] == 1
