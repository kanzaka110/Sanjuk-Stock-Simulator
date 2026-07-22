"""트랙레코드 리포트 검증 — DB/네트워크 불필요 (합성 행)."""

import pytest

from core import track_record as tr


def _exec(status, side="buy", created="2026-07-01T00:00:00+09:00"):
    return {"status": status, "side": side, "live_order_sent": 1, "broker_order_id": "x", "created_at": created}


def test_summarize_execution_send_success_rate():
    rows = [_exec("live_sent")] * 6 + [_exec("live_send_failed")] * 4 + [_exec("previewed")] * 10
    s = tr.summarize_execution(rows)
    assert s["n"] == 20
    assert s["sent"] == 6 and s["failed"] == 4
    assert s["send_success_rate"] == pytest.approx(0.6)  # 6/(6+4)
    assert s["buy"] == 20


def test_summarize_execution_empty():
    assert tr.summarize_execution([]) == {"n": 0}


def test_proxy_performance_expectancy_and_pf():
    rows = [{"return_5d": 3.0}, {"return_5d": 3.0}, {"return_5d": -1.0}, {"return_5d": -1.0}]
    p = tr.proxy_performance(rows)
    assert p["n"] == 4
    assert p["win_rate"] == pytest.approx(0.5)
    assert p["expectancy"] == pytest.approx(1.0)      # (3+3-1-1)/4
    assert p["avg_win"] == pytest.approx(3.0)
    assert p["avg_loss"] == pytest.approx(-1.0)
    assert p["profit_factor"] == pytest.approx(6.0 / 2.0)
    assert p["cum_return"] == pytest.approx(4.0)


def test_proxy_performance_all_wins_infinite_pf():
    p = tr.proxy_performance([{"return_5d": 1.0}, {"return_5d": 2.0}])
    assert p["profit_factor"] == float("inf")


def test_proxy_performance_empty():
    assert tr.proxy_performance([{"foo": 1}]) == {"n": 0}


def test_weekly_breakdown_groups_by_iso_week():
    rows = [
        {"decided_at": "2026-06-29T10:00:00+09:00", "return_5d": 1.0},  # ISO week A
        {"decided_at": "2026-06-30T10:00:00+09:00", "return_5d": -1.0},
        {"decided_at": "2026-07-06T10:00:00+09:00", "return_5d": 2.0},  # next week
    ]
    wk = tr.weekly_breakdown(rows)
    assert len(wk) == 2
    assert wk[0]["n"] == 2 and wk[0]["win_rate"] == pytest.approx(0.5)
    assert wk[1]["n"] == 1 and wk[1]["mean_return"] == pytest.approx(2.0)


def test_track_record_text_flags_missing_pnl():
    txt = tr.track_record_text(
        tr.summarize_execution([_exec("live_sent")]),
        tr.proxy_performance([{"return_5d": 1.0}]),
        tr.weekly_breakdown([{"decided_at": "2026-07-01T00:00:00+09:00", "return_5d": 1.0}]),
    )
    assert "트랙레코드" in txt
    assert "실현 P&L 미저장" in txt  # 진짜 P&L 없음을 명시
