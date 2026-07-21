from __future__ import annotations

import json
import sqlite3

from src import toss_after_cost_probability as probability
from src.toss_after_cost_probability import calibrate_after_cost_probability


def _calibration(outcomes, **overrides):
    value = {
        "schema": "toss_execution_calibration.v1",
        "status": "ok",
        "mode": "observability_only",
        "decision_usable": False,
        "attribution_model": "symbol_fifo_v1",
        "attribution_verified": False,
        "cost_model": "decision_buffer_v1_not_broker_statement",
        "lineage_status": "complete",
        "lineage_reasons": [],
        "outcomes": outcomes,
    }
    value.update(overrides)
    return value


def _outcome(pilot_id: str, net: float, *, exits: int = 1):
    return {
        "buy_pilot_id": pilot_id,
        "symbol": "005930",
        "entry_quantity": 1,
        "exit_count": exits,
        "net_return_pct": net,
        "outcome": "win" if net > 0 else "loss" if net < 0 else "flat",
        "entered_at": "2026-07-01T09:00:00+09:00",
        "closed_at": "2026-07-02T09:00:00+09:00",
    }


def _score(pilot_id: str, score: float):
    return {
        "pilot_id": pilot_id,
        "score_total": score,
        "decision_bucket": "PASS_EXECUTE",
        "side": "BUY",
        "quality_score_authority": "quality_breakdown.score_total",
        "score_schema_version": 1,
        "weight_profile_hash": "a" * 64,
        "score_breakdown_sha256": "b" * 64,
        "candidate_snapshot_sha256": "c" * 64,
        "decision_ref": f"execution_decision:{pilot_id}",
    }


def test_fits_shadow_isotonic_probability_for_after_cost_target():
    outcomes = []
    scores = []
    for i in range(20):
        pilot_id = f"tlive_fit_{i}"
        low_group = i < 10
        positive = i < 8 if low_group else i < 12
        outcomes.append(_outcome(pilot_id, 1.0 if positive else -1.0))
        scores.append(_score(pilot_id, 60.0 if low_group else 70.0))

    result = calibrate_after_cost_probability(
        _calibration(outcomes),
        scores,
        min_samples=20,
        min_bin_samples=5,
    )

    assert result["schema"] == "toss_after_cost_probability.v1"
    assert result["mode"] == "shadow_observability_only"
    assert result["decision_usable"] is False
    assert result["target"] == "net_return_pct_gt_zero"
    assert result["exit_contract"] == "all_liquidation_single_exit_v1"
    assert result["cost_model"] == "decision_buffer_v1_not_broker_statement"
    assert result["eligible_count"] == 20
    assert result["positive_count"] == 10
    assert result["model_fitted"] is True
    assert result["promotion_eligible"] is False
    assert result["bins"] == [
        {
            "score_min": 60.0,
            "score_max": 70.0,
            "sample_count": 20,
            "positive_count": 10,
            "empirical_probability": 0.5,
            "calibrated_probability": 0.5,
        }
    ]
    assert result["raw_brier_score"] == 0.305
    assert result["calibrated_brier_score"] == 0.25
    assert "attribution_unverified" in result["promotion_block_reasons"]
    assert "shadow_only" in result["promotion_block_reasons"]


def test_minimum_sample_gate_emits_no_probability_model():
    outcomes = [_outcome(f"tlive_{i}", 1.0 if i % 2 else -1.0) for i in range(5)]
    scores = [_score(f"tlive_{i}", 60.0 + i) for i in range(5)]

    result = calibrate_after_cost_probability(
        _calibration(outcomes), scores, min_samples=20
    )

    assert result["status"] == "insufficient_samples"
    assert result["eligible_count"] == 5
    assert result["model_fitted"] is False
    assert result["bins"] == []
    assert result["calibrated_brier_score"] is None
    assert "minimum_sample_not_reached" in result["promotion_block_reasons"]


def test_only_single_full_liquidation_exit_is_eligible():
    outcomes = [
        _outcome("tlive_single", 1.0, exits=1),
        _outcome("tlive_partial_path", 1.0, exits=2),
    ]
    scores = [_score("tlive_single", 60.0), _score("tlive_partial_path", 70.0)]

    result = calibrate_after_cost_probability(
        _calibration(outcomes), scores, min_samples=20
    )

    assert result["eligible_count"] == 1
    assert result["excluded_counts"] == {"exit_contract_mismatch": 1}
    assert result["model_fitted"] is False
    assert "target_class_degenerate" in result["promotion_block_reasons"]


def test_duplicate_score_rows_quarantine_exact_pilot_join():
    outcomes = [_outcome("tlive_dup", 1.0), _outcome("tlive_ok", -1.0)]
    scores = [
        _score("tlive_dup", 60.0),
        _score("tlive_dup", 61.0),
        _score("tlive_ok", 70.0),
    ]

    result = calibrate_after_cost_probability(
        _calibration(outcomes), scores, min_samples=20
    )

    assert result["eligible_count"] == 1
    assert result["excluded_counts"] == {"score_join_conflict": 1}
    assert result["score_conflict_count"] == 1
    assert result["model_fitted"] is False


def test_cost_contract_mismatch_blocks_before_model_fit():
    result = calibrate_after_cost_probability(
        _calibration(
            [_outcome("tlive_a", 1.0)],
            cost_model="different_cost_contract",
        ),
        [_score("tlive_a", 60.0)],
        min_samples=20,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "cost_model_mismatch"
    assert result["eligible_count"] == 0
    assert result["model_fitted"] is False
    assert result["decision_usable"] is False


def _create_loader_databases(
    tmp_path, *, exact_quality_index=True, wrong_index_predicate=False
):
    events = tmp_path / "events.db"
    ledger = tmp_path / "ledger.db"
    quality = tmp_path / "quality.db"
    with sqlite3.connect(events) as connection:
        connection.execute(
            """CREATE TABLE live_pilot_events (
                   event_id TEXT PRIMARY KEY,
                   pilot_id TEXT NOT NULL,
                   event_type TEXT NOT NULL,
                   side TEXT,
                   symbol TEXT,
                   quantity INTEGER,
                   estimated_amount_krw REAL,
                   created_at TEXT,
                   broker_order_status TEXT,
                   filled_quantity REAL,
                   filled_price REAL,
                   live_order_sent INTEGER,
                   adapter_status TEXT,
                   live_order_allowed INTEGER
               )"""
        )
        connection.executemany(
            "INSERT INTO live_pilot_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "e1", "tlive_buy-1", "autonomous_live_sent", "buy",
                    "005930.KS", 1, 100_000, "2026-07-15T09:00:00+09:00",
                    "FILLED", 1, 100_000, 1, "enabled", 1,
                ),
                (
                    "e2", "tlive_sell-1", "autonomous_live_sent", "sell",
                    "005930.KS", 1, 105_000, "2026-07-15T10:00:00+09:00",
                    "FILLED", 1, 105_000, 1, "enabled", 1,
                ),
            ],
        )
    with sqlite3.connect(ledger) as connection:
        connection.execute(
            "CREATE TABLE live_pilot_ledger (pilot_id TEXT PRIMARY KEY, reason TEXT)"
        )
        connection.executemany(
            "INSERT INTO live_pilot_ledger VALUES (?,?)",
            [
                ("tlive_buy-1", "auto_pipeline"),
                ("tlive_sell-1", "position_review_sell"),
            ],
        )
    with sqlite3.connect(quality) as connection:
        connection.execute(
            """CREATE TABLE quality_gate_decisions (
                   pilot_id TEXT NOT NULL,
                   score_total REAL,
                   decision_bucket TEXT,
                   side TEXT,
                   quality_score_authority TEXT,
                   score_schema_version INTEGER,
                   weight_profile_hash TEXT,
                   score_breakdown_sha256 TEXT,
                   candidate_snapshot_sha256 TEXT,
                   decision_ref TEXT
               )"""
        )
        if exact_quality_index:
            connection.execute(
                """CREATE UNIQUE INDEX idx_qg_pilot_id_exact
                   ON quality_gate_decisions(pilot_id) WHERE """
                + ("pilot_id = 'x'" if wrong_index_predicate else "pilot_id <> ''")
            )
        connection.execute(
            "INSERT INTO quality_gate_decisions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "tlive_buy-1", 65.0, "PASS_EXECUTE", "BUY",
                "quality_breakdown.score_total", 1,
                "a" * 64, "b" * 64, "c" * 64,
                "execution_decision:buy-1",
            ),
        )
    return events, ledger, quality


def test_loader_and_main_join_exact_score_read_only(tmp_path, capsys):
    events, ledger, quality = _create_loader_databases(tmp_path)
    before = {path: path.read_bytes() for path in (events, ledger, quality)}

    result = probability.load_after_cost_probability(
        events_path=events,
        ledger_path=ledger,
        quality_path=quality,
        min_samples=20,
        min_bin_samples=1,
    )

    assert result["eligible_count"] == 1
    assert result["source_outcomes_loaded"] == 1
    assert result["quality_rows_loaded"] == 1
    assert result["model_fitted"] is False
    assert result["decision_usable"] is False
    assert {path: path.read_bytes() for path in before} == before

    assert probability._main([
        "--events-db", str(events),
        "--ledger-db", str(ledger),
        "--quality-db", str(quality),
        "--min-samples", "20",
        "--min-bin-samples", "5",
    ]) == 0
    cli = json.loads(capsys.readouterr().out)
    assert cli["eligible_count"] == 1
    assert cli["decision_usable"] is False
    assert {path: path.read_bytes() for path in before} == before


def test_loader_blocks_quality_db_without_exact_pilot_index(tmp_path):
    events, ledger, quality = _create_loader_databases(
        tmp_path, exact_quality_index=False
    )

    result = probability.load_after_cost_probability(
        events_path=events,
        ledger_path=ledger,
        quality_path=quality,
        min_samples=20,
        min_bin_samples=1,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "quality_score_source_unavailable"
    assert result["error_type"] == "DatabaseError"
    assert result["decision_usable"] is False


def test_loader_blocks_wrong_partial_index_predicate(tmp_path):
    events, ledger, quality = _create_loader_databases(
        tmp_path, wrong_index_predicate=True
    )

    result = probability.load_after_cost_probability(
        events_path=events,
        ledger_path=ledger,
        quality_path=quality,
        min_samples=20,
        min_bin_samples=1,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "quality_score_source_unavailable"
    assert result["error_type"] == "DatabaseError"


def test_upstream_execution_source_unavailable_is_not_sample_insufficiency(tmp_path):
    quality = tmp_path / "quality.db"
    with sqlite3.connect(quality) as connection:
        connection.execute(
            "CREATE TABLE quality_gate_decisions (pilot_id TEXT NOT NULL)"
        )
        connection.execute(
            """CREATE UNIQUE INDEX idx_qg_pilot_id_exact
               ON quality_gate_decisions(pilot_id) WHERE pilot_id <> ''"""
        )

    result = probability.load_after_cost_probability(
        events_path=tmp_path / "missing-events.db",
        ledger_path=tmp_path / "missing-ledger.db",
        quality_path=quality,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "execution_calibration_source_unavailable"
    assert result["model_fitted"] is False


def test_equal_scores_are_never_split_across_probability_bins():
    outcomes = [
        _outcome(f"tlive_equal_{i}", 1.0 if i % 2 == 0 else -1.0)
        for i in range(10)
    ] + [
        _outcome(f"tlive_high_{i}", 1.0 if i % 2 == 0 else -1.0)
        for i in range(10)
    ]
    scores = [
        _score(f"tlive_equal_{i}", 60.0) for i in range(10)
    ] + [
        _score(f"tlive_high_{i}", 70.0) for i in range(10)
    ]

    result = calibrate_after_cost_probability(
        _calibration(outcomes),
        scores,
        min_samples=20,
        min_bin_samples=5,
    )

    assert result["model_fitted"] is True
    assert all(
        row["score_min"] != row["score_max"] or row["sample_count"] == 10
        for row in result["bins"]
    )
    ranges = [(row["score_min"], row["score_max"]) for row in result["bins"]]
    assert len(ranges) == len(set(ranges))


def test_valid_and_malformed_duplicate_score_rows_quarantine_pilot():
    valid = _score("tlive_mixed_dup", 65.0)
    malformed = dict(valid, quality_score_authority="wrong")
    result = calibrate_after_cost_probability(
        _calibration([
            _outcome("tlive_mixed_dup", 1.0),
            _outcome("tlive_other", -1.0),
        ]),
        [valid, malformed, _score("tlive_other", 70.0)],
        min_samples=20,
    )

    assert result["eligible_count"] == 1
    assert result["score_conflict_count"] == 1
    assert result["score_invalid_row_count"] == 1
    assert result["excluded_counts"] == {"score_join_conflict": 1}


def test_heterogeneous_score_schema_or_weight_blocks_model():
    outcomes = []
    scores = []
    for i in range(20):
        pilot_id = f"tlive_lineage_{i}"
        outcomes.append(_outcome(pilot_id, 1.0 if i % 2 else -1.0))
        row = _score(pilot_id, 60.0 + i)
        if i == 19:
            row["weight_profile_hash"] = "d" * 64
        scores.append(row)

    result = calibrate_after_cost_probability(
        _calibration(outcomes), scores, min_samples=20
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "score_lineage_heterogeneous"
    assert result["model_fitted"] is False
    assert result["decision_usable"] is False


def test_malformed_upstream_contract_returns_explicit_block():
    malformed = _calibration([], lineage_reasons="not-a-list")

    result = calibrate_after_cost_probability(malformed, [], min_samples=20)

    assert result["status"] == "blocked"
    assert result["reason"] == "calibration_contract_invalid"
    assert result["model_fitted"] is False
    assert result["decision_usable"] is False


def test_cli_rejects_min_samples_below_twenty():
    import pytest

    with pytest.raises(SystemExit) as exc:
        probability._main(["--min-samples", "19"])

    assert exc.value.code == 2
