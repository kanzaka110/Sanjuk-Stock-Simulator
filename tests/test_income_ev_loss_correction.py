"""EV 손실 가정 교정 flag 검증 (기본 OFF = 동작 불변, 실제 손절가 미변경)."""

import pytest

from core import toss_income_strategy as tis


def _cand():
    # rr≥2, stop 4%(≤4.5), 큰 주문(수수료 바닥값 희석), Toss lifecycle 모델
    return {
        "symbol": "005930.KS", "side": "buy",
        "limit_price": 1_000_000.0, "entry_price": 1_000_000.0, "price": 1_000_000.0,
        "target_price": 1_100_000.0, "stop_loss": 960_000.0,
        "quantity": 1, "estimated_amount_krw": 1_000_000.0,
        "risk_reward": 2.5, "score": 60, "decision_bucket": "PASS_EXECUTE",
        "income_exit_model": "toss_position_review_v2",
    }


def test_expected_loss_pct_env_parsing(monkeypatch):
    monkeypatch.delenv("TOSS_INCOME_EXPECTED_LOSS_PCT", raising=False)
    assert tis._income_expected_loss_pct() is None
    monkeypatch.setenv("TOSS_INCOME_EXPECTED_LOSS_PCT", "0.8")
    assert tis._income_expected_loss_pct() == pytest.approx(0.8)
    monkeypatch.setenv("TOSS_INCOME_EXPECTED_LOSS_PCT", "0")
    assert tis._income_expected_loss_pct() is None
    monkeypatch.setenv("TOSS_INCOME_EXPECTED_LOSS_PCT", "-1")
    assert tis._income_expected_loss_pct() is None


def test_default_off_blocks_by_ev(monkeypatch):
    # 기본(미설정): 풀 손절 -2.5% 가정 → EV<0 → income_pass=False (현재 동작)
    monkeypatch.delenv("TOSS_INCOME_EXPECTED_LOSS_PCT", raising=False)
    r = tis.compute_income_edge(_cand(), pending_orders=[])
    assert r.get("income_pass") is False


def test_loss_correction_flips_income_pass(monkeypatch):
    # 손실 가정을 0.5%로 교정 → EV>0 → income_pass=True
    monkeypatch.setenv("TOSS_INCOME_EXPECTED_LOSS_PCT", "0.5")
    r = tis.compute_income_edge(_cand(), pending_orders=[])
    assert r.get("income_pass") is True


def test_correction_capped_at_stop_distance(monkeypatch):
    # env가 실제 손절폭(2.5%)보다 크면 scale은 1.0로 캡 → 기본과 동일(더 나빠지지 않음)
    monkeypatch.setenv("TOSS_INCOME_EXPECTED_LOSS_PCT", "5.0")
    r = tis.compute_income_edge(_cand(), pending_orders=[])
    assert r.get("income_pass") is False  # 기본과 같음
