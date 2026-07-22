"""점수 캘리브레이션 측정 모듈 검증 — DB/네트워크 불필요 (합성 행)."""

import pytest

from core import calibration_report as cr


def _row(score, outcome, r3=None, bucket="BUY"):
    return {"score_total": score, "outcome": outcome, "return_3d": r3,
            "decision_bucket": bucket, "ticker": "T", "side": "BUY"}


def test_win01():
    assert cr._win01("win") == 1.0
    assert cr._win01("loss") == 0.0


def test_spearman_perfect_monotonic():
    xs = [1, 2, 3, 4, 5]
    ys = [10, 20, 30, 40, 50]
    assert cr.spearman(xs, ys) == pytest.approx(1.0)
    assert cr.spearman(xs, list(reversed(ys))) == pytest.approx(-1.0)


def test_spearman_degenerate_returns_zero():
    assert cr.spearman([1, 1, 1], [1, 2, 3]) == 0.0
    assert cr.spearman([1], [1]) == 0.0


def test_score_reliability_buckets_monotonic_winrate():
    # 낮은 점수=대부분 loss, 높은 점수=대부분 win
    rows = [_row(s, "loss") for s in range(0, 20)] + [_row(s, "win") for s in range(80, 100)]
    buckets = cr.score_reliability(rows, n_buckets=4)
    assert len(buckets) == 4
    # 최하위 버킷 승률 < 최상위 버킷 승률
    assert buckets[0]["win_rate"] < buckets[-1]["win_rate"]
    assert sum(b["n"] for b in buckets) == len(rows)


def test_calibration_summary_discriminating_model():
    rows = [_row(s, "loss", r3=-1.0) for s in range(0, 30)] + \
           [_row(s, "win", r3=2.0) for s in range(70, 100)]
    s = cr.calibration_summary(rows)
    assert s["n"] == 60
    assert s["base_win_rate"] == pytest.approx(0.5, abs=0.01)
    assert s["spearman_score_win"] > 0.8            # 점수가 승패를 잘 판별
    assert s["top_minus_bottom_win_rate"] > 0.5     # 상/하위 버킷 승률차 큼
    assert s["monotonic"] is True


def test_calibration_summary_uninformative_model():
    # 점수와 무관하게 승/패 섞임 → 판별력 없음
    rows = []
    for s in range(0, 40):
        rows.append(_row(s, "win" if s % 2 == 0 else "loss"))
    s = cr.calibration_summary(rows)
    assert abs(s["spearman_score_win"]) < 0.3
    assert abs(s["top_minus_bottom_win_rate"]) < 0.3


def test_expired_excluded_from_winrate():
    rows = [_row(10, "win"), _row(20, "loss"), _row(30, "expired")]
    s = cr.calibration_summary(rows)
    assert s["n"] == 2  # expired 제외


def test_calibration_text_empty():
    assert "데이터 없음" in cr.calibration_text({"n": 0})


def test_calibration_text_renders():
    rows = [_row(s, "loss") for s in range(0, 20)] + [_row(s, "win") for s in range(80, 100)]
    txt = cr.calibration_text(cr.calibration_summary(rows))
    assert "점수 캘리브레이션" in txt
    assert "Spearman" in txt
    assert "B1" in txt
