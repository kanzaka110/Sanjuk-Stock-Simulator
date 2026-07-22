"""P2b: win_prob R:R 할인 feature-flag 검증 (기본 OFF = 동작 불변)."""

import pytest

from core import toss_income_strategy as tis


def _cand(score=70, rr=4.0, bucket="PASS_EXECUTE"):
    return {"symbol": "TEST", "score": score, "risk_reward": rr, "decision_bucket": bucket}


def test_default_off_is_current_behavior(monkeypatch):
    # env 미설정 → 기존 공식 그대로 (score70=0.57 + PASS_EXECUTE 0.025 = 0.595)
    monkeypatch.delenv("TOSS_WINPROB_RR_DISCOUNT", raising=False)
    assert tis.estimate_win_prob(_cand(score=70, rr=4.0)) == pytest.approx(0.595)


def test_flag_zero_is_noop(monkeypatch):
    monkeypatch.setenv("TOSS_WINPROB_RR_DISCOUNT", "0.0")
    assert tis.estimate_win_prob(_cand(score=70, rr=4.0)) == pytest.approx(0.595)


def test_flag_on_discounts_high_rr(monkeypatch):
    monkeypatch.setenv("TOSS_WINPROB_RR_DISCOUNT", "0.03")
    # 0.595 - 0.03*(4-2) = 0.535
    assert tis.estimate_win_prob(_cand(score=70, rr=4.0)) == pytest.approx(0.535)


def test_flag_no_discount_at_or_below_pivot(monkeypatch):
    monkeypatch.setenv("TOSS_WINPROB_RR_DISCOUNT", "0.03")
    assert tis.estimate_win_prob(_cand(score=70, rr=1.5)) == pytest.approx(0.595)
    assert tis.estimate_win_prob(_cand(score=70, rr=2.0)) == pytest.approx(0.595)


def test_flag_respects_clamp_floor(monkeypatch):
    monkeypatch.setenv("TOSS_WINPROB_RR_DISCOUNT", "0.2")
    assert tis.estimate_win_prob(_cand(score=70, rr=10.0)) == pytest.approx(0.42)


def test_higher_rr_gets_lower_prob_when_enabled(monkeypatch):
    monkeypatch.setenv("TOSS_WINPROB_RR_DISCOUNT", "0.03")
    lo_rr = tis.estimate_win_prob(_cand(score=75, rr=2.5))
    hi_rr = tis.estimate_win_prob(_cand(score=75, rr=5.0))
    assert hi_rr < lo_rr  # R:R 클수록 win_prob 낮음


def test_negative_env_treated_as_off(monkeypatch):
    monkeypatch.setenv("TOSS_WINPROB_RR_DISCOUNT", "-1")
    assert tis.estimate_win_prob(_cand(score=70, rr=4.0)) == pytest.approx(0.595)
