"""백테스트 비용모델(슬리피지) + 다중폴드 워크포워드 검증 — 네트워크 불필요."""

import numpy as np
import pandas as pd
import pytest

from core import backtest as bt


def _series(vals, start="2023-01-02"):
    idx = pd.date_range(start, periods=len(vals), freq="B")
    return pd.Series([float(v) for v in vals], index=idx, dtype=float)


# ── 비용 모델 ──────────────────────────────────────────────
def test_cost_includes_slippage_by_default():
    kr = bt._round_trip_cost_pct("005930.KS")
    us = bt._round_trip_cost_pct("NVDA")
    assert kr == pytest.approx(bt._COMMISSION_TAX_PCT["KR"] + bt._SLIPPAGE_PCT["KR"])
    assert us == pytest.approx(bt._COMMISSION_TAX_PCT["US"] + bt._SLIPPAGE_PCT["US"])
    assert kr > us  # 한국 왕복비용이 더 큼


def test_cost_exclude_slippage_matches_commission_tax():
    assert bt._round_trip_cost_pct("005930.KS", include_slippage=False) == pytest.approx(
        bt._COMMISSION_TAX_PCT["KR"]
    )
    assert bt._round_trip_cost_pct("NVDA", include_slippage=False) == pytest.approx(
        bt._COMMISSION_TAX_PCT["US"]
    )


def test_kosdaq_treated_as_kr():
    assert bt._market_of("035900.KQ") == "KR"
    assert bt._market_of("AAPL") == "US"


def test_slippage_makes_backtest_more_conservative():
    close = _series([100, 100, 110, 110])  # +10% 왕복
    sig = pd.Series([1, 0, -1, 0], index=close.index)
    with_slip = bt._collect_trade_returns(close, sig, bt._round_trip_cost_pct("NVDA"))
    no_slip = bt._collect_trade_returns(
        close, sig, bt._round_trip_cost_pct("NVDA", include_slippage=False)
    )
    assert len(with_slip) == 1 and len(no_slip) == 1
    assert with_slip[0] < no_slip[0]
    assert no_slip[0] - with_slip[0] == pytest.approx(bt._SLIPPAGE_PCT["US"])


def test_collect_trade_returns_open_position_closed_at_end():
    close = _series([100, 105, 108])
    sig = pd.Series([1, 0, 0], index=close.index)  # 진입만 → 마지막 종가로 청산
    trades = bt._collect_trade_returns(close, sig, 0.0)
    assert len(trades) == 1
    assert trades[0] == pytest.approx((108 - 100) / 100 * 100)


def test_collect_trade_returns_no_signal_no_trade():
    close = _series([100, 101, 102])
    sig = pd.Series([0, 0, 0], index=close.index)
    assert bt._collect_trade_returns(close, sig, 0.0) == []


# ── 워크포워드 ─────────────────────────────────────────────
def test_walk_forward_rsi_on_returns_oos_result():
    rng = np.arange(200)
    vals = 100 + 15 * np.sin(rng / 6.0) + rng * 0.05  # 오실레이팅 → RSI 시그널
    close = _series(vals.tolist())
    res = bt.walk_forward_rsi_on(close, ticker="TEST", name="t", period="test", folds=3)
    assert res is not None
    assert res.optimized_params.get("validation") == "walk_forward_3fold"
    assert res.optimized_params.get("folds", 0) >= 1
    assert len(res.optimized_params.get("params_per_fold", [])) == res.optimized_params["folds"]
    assert res.total_trades >= 1
    assert "워크포워드" in res.strategy


def test_walk_forward_rsi_on_too_short_returns_none():
    close = _series([100 + i for i in range(50)])
    assert bt.walk_forward_rsi_on(close, folds=3) is None


def test_walk_forward_rsi_on_uses_conservative_cost():
    # KR 티커는 더 큰 비용 → 동일 시리즈에서 OOS 총수익이 US보다 낮거나 같아야
    rng = np.arange(200)
    vals = (100 + 15 * np.sin(rng / 6.0) + rng * 0.05).tolist()
    kr = bt.walk_forward_rsi_on(_series(vals), ticker="005930.KS", folds=3)
    us = bt.walk_forward_rsi_on(_series(vals), ticker="NVDA", folds=3)
    assert kr is not None and us is not None
    # 동일 거래 수라면 비용이 큰 KR의 총수익이 더 낮다
    if kr.total_trades == us.total_trades and kr.total_trades > 0:
        assert kr.total_return_pct <= us.total_return_pct
