"""win_prob 관측 기록(P2①) 검증 — temp DB, live DB 미접촉."""

import sqlite3

import pytest

from core import toss_quality_gate as qg


def _qs():
    return qg.QualityScore(
        ticker="TEST",
        score_total=60.0,
        score_momentum=12.0,
        score_liquidity=5.0,
        score_risk_reward=18.0,
        score_reliability=7.5,
        score_market_regime=14.0,
        penalty_overheat=0.0,
        penalty_duplicate=0.0,
        penalty_event_risk=0.0,
        risk_flags=(),
        decision_bucket="PASS_EXECUTE",
        decision_reason="test",
        rr_ratio=2.5,
        regime="횡보장",
        scored_at="2026-07-22T00:00:00+09:00",
    )


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    dbp = tmp_path / "qg.db"
    monkeypatch.setattr(qg, "_outcomes_db_path", lambda: dbp)
    monkeypatch.setattr(qg, "_outcomes_schema_created", False, raising=False)
    yield dbp


def test_win_prob_column_migrated_and_stored(temp_db):
    rid = qg.record_quality_decision(
        _qs(), entry_price=100.0, stop_loss=96.0, target_price=110.0, win_prob=0.57
    )
    assert rid > 0
    con = sqlite3.connect(str(temp_db))
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(quality_gate_decisions)")}
    assert "win_prob" in cols
    row = con.execute(
        "SELECT win_prob FROM quality_gate_decisions WHERE id=?", (rid,)
    ).fetchone()
    assert row["win_prob"] == pytest.approx(0.57)


def test_win_prob_none_stored_as_null(temp_db):
    rid = qg.record_quality_decision(_qs(), 100.0, 96.0, 110.0, win_prob=None)
    con = sqlite3.connect(str(temp_db))
    v = con.execute(
        "SELECT win_prob FROM quality_gate_decisions WHERE id=?", (rid,)
    ).fetchone()[0]
    assert v is None


def test_record_backward_compatible_without_win_prob(temp_db):
    # 기존 호출(win_prob 미지정)도 그대로 동작 — 결정/기록 로직 미변경
    rid = qg.record_quality_decision(_qs(), 100.0, 96.0, 110.0)
    assert rid > 0
    con = sqlite3.connect(str(temp_db))
    v = con.execute(
        "SELECT win_prob FROM quality_gate_decisions WHERE id=?", (rid,)
    ).fetchone()[0]
    assert v is None


def test_calibration_report_reads_win_prob_when_present(temp_db):
    # win_prob 기록 후 calibration_report가 이를 실제 캘리브레이션에 쓸 수 있는 형태인지 스모크
    qg.record_quality_decision(_qs(), 100.0, 96.0, 110.0, win_prob=0.60)
    con = sqlite3.connect(str(temp_db))
    con.row_factory = sqlite3.Row
    row = dict(con.execute("SELECT win_prob, score_total FROM quality_gate_decisions").fetchone())
    assert row["win_prob"] == pytest.approx(0.60)
    assert row["score_total"] == pytest.approx(60.0)


def test_win_prob_candidate_column_and_stored(temp_db):
    rid = qg.record_quality_decision(
        _qs(), 100.0, 96.0, 110.0, win_prob=0.55, win_prob_candidate=0.50
    )
    con = sqlite3.connect(str(temp_db))
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(quality_gate_decisions)")}
    assert "win_prob_candidate" in cols
    row = con.execute(
        "SELECT win_prob, win_prob_candidate FROM quality_gate_decisions WHERE id=?", (rid,)
    ).fetchone()
    assert row["win_prob"] == pytest.approx(0.55)
    assert row["win_prob_candidate"] == pytest.approx(0.50)
