"""Shadow 검증 하네스 테스트 — DB/네트워크 불필요 (합성 행)."""

import pytest

from core import shadow_calibration as sc


def _row(decided_at, score, rr, entry, stop, target, outcome, ret5):
    return {
        "decided_at": decided_at, "score_total": score, "rr_ratio": rr,
        "entry_price": entry, "stop_loss": stop, "target_price": target,
        "outcome": outcome, "return_5d": ret5, "win_prob": None,
    }


def test_ev_ranking_computes_expected_value():
    fn = sc.ev_ranking(lambda r: 0.5)
    r = _row("t", 60, 2.0, 100, 90, 120, "win", 1.0)
    # ev = 0.5*0.20 - 0.5*0.10 = 0.05
    assert fn(r) == pytest.approx(0.05)


def test_ev_ranking_none_on_bad_prices():
    fn = sc.ev_ranking(lambda r: 0.5)
    assert fn(_row("t", 60, 2, 0, 90, 120, "win", 1.0)) is None


def test_time_split_orders_and_partitions():
    rows = [_row(f"2026-07-{d:02d}", 50, 2, 100, 95, 110, "win", 1.0) for d in (5, 1, 3, 2, 4)]
    train, test = sc.time_split(rows, holdout_frac=0.4)
    assert [r["decided_at"] for r in train] == ["2026-07-01", "2026-07-02", "2026-07-03"]
    assert [r["decided_at"] for r in test] == ["2026-07-04", "2026-07-05"]


def test_evaluate_ranking_perfect_discriminator():
    # 랭킹이 승패를 완벽 정렬
    rows = [_row("t", s, 2, 100, 95, 110, "loss", -1.0) for s in range(0, 10)] + \
           [_row("t", s, 2, 100, 95, 110, "win", 2.0) for s in range(90, 100)]
    m = sc.evaluate_ranking(rows, sc.score_ranking)
    assert m["n"] == 20
    # 이진 win/loss 타이로 Spearman은 <1로 캡되지만 강한 양의 판별
    assert m["rho_win"] > 0.8
    assert m["top_q_win_rate"] == pytest.approx(1.0)  # 상위25% 전부 win = 완벽 판별


def test_evaluate_ranking_empty():
    assert sc.evaluate_ranking([], sc.score_ranking) == {"n": 0}


def test_run_shadow_compares_candidates_on_holdout():
    # train(앞) + holdout(뒤). holdout에서 score가 승과 양의 상관.
    rows = []
    for i in range(40):
        out = "win" if i % 2 == 0 else "loss"
        # 뒤쪽(홀드아웃)에서 score를 승패에 정렬
        score = 70 if out == "win" else 40
        rows.append(_row(f"2026-07-{i + 1:02d}T00:00", score, 2.0, 100, 95, 110, out, 1.0 if out == "win" else -1.0))
    report = sc.run_shadow(rows, {
        "baseline_score": sc.score_ranking,
        "current_ev": sc.ev_ranking(sc.current_winprob),
    }, holdout_frac=0.5)
    assert report["n_test"] == 20
    assert report["results"]["baseline_score"]["rho_win"] > 0.5
    txt = sc.shadow_text(report)
    assert "Shadow" in txt and "baseline_score" in txt


def test_current_winprob_clamped():
    assert 0.42 <= sc.current_winprob({"score_total": 200}) <= 0.72
    assert 0.42 <= sc.current_winprob({"score_total": -50}) <= 0.72
