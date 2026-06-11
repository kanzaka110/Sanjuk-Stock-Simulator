"""
백테스팅 엔진 — pandas 기반 전략 검증

AI 추천 전략을 과거 데이터로 검증하여
"이 전략이 지난 N개월간 적용됐으면 수익률 X%" 제공.

외부 의존성 없이 pandas + yfinance만 사용.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

import pandas as pd
import yfinance as yf

from config.settings import KRW_TICKERS

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BacktestResult:
    """백테스트 결과."""

    ticker: str
    name: str
    strategy: str  # 전략 이름
    period: str  # 테스트 기간
    total_return_pct: float  # 총 수익률 (%)
    buy_hold_return_pct: float  # 바이앤홀드 수익률 (%)
    excess_return_pct: float  # 초과 수익률 (전략 - 바이앤홀드)
    win_rate_pct: float  # 승률 (%)
    total_trades: int  # 총 거래 횟수
    max_drawdown_pct: float  # 최대 낙폭 (%)
    sharpe_ratio: float  # 샤프 비율 (연환산)
    profit_factor: float  # 수익 팩터 (총이익/총손실)
    # 확장 지표
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    cvar_95: float = 0.0  # 최악 5% 평균 손실
    max_underwater_days: int = 0
    optimized_params: dict = field(default_factory=dict)


def backtest_rsi(
    ticker: str,
    name: str = "",
    period: str = "1y",
    rsi_buy: float = 30.0,
    rsi_sell: float = 70.0,
) -> BacktestResult | None:
    """RSI 전략 백테스트.

    RSI < rsi_buy → 매수, RSI > rsi_sell → 매도.
    """
    try:
        hist = yf.Ticker(ticker).history(period=period)
        if len(hist) < 30:
            return None

        close = hist["Close"]

        # RSI 계산
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
        # division by zero 방어: loss가 0이면 RSI=100으로 처리
        with np.errstate(divide="ignore", invalid="ignore"):
            rs = gain / loss.replace(0, np.nan)
            rs = rs.fillna(100.0)
        rsi = 100 - (100 / (1 + rs))

        return _simulate(
            close, rsi, rsi_buy, rsi_sell,
            ticker, name, f"RSI({rsi_buy:.0f}/{rsi_sell:.0f})", period,
        )
    except Exception as e:
        log.warning(f"RSI 백테스트 실패 ({ticker}): {e}")
        return None


def backtest_macd(
    ticker: str,
    name: str = "",
    period: str = "1y",
) -> BacktestResult | None:
    """MACD 전략 백테스트.

    MACD 히스토그램 양전환 → 매수, 음전환 → 매도.
    """
    try:
        hist = yf.Ticker(ticker).history(period=period)
        if len(hist) < 35:
            return None

        close = hist["Close"]

        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        histogram = macd_line - signal_line

        # 시그널: 히스토그램 부호 변화
        positions = pd.Series(0, index=close.index)
        for i in range(1, len(histogram)):
            if histogram.iloc[i] > 0 and histogram.iloc[i - 1] <= 0:
                positions.iloc[i] = 1  # 매수
            elif histogram.iloc[i] < 0 and histogram.iloc[i - 1] >= 0:
                positions.iloc[i] = -1  # 매도

        return _simulate_from_signals(
            close, positions, ticker, name, "MACD(12,26,9)", period,
        )
    except Exception as e:
        log.warning(f"MACD 백테스트 실패 ({ticker}): {e}")
        return None


def backtest_bollinger(
    ticker: str,
    name: str = "",
    period: str = "1y",
) -> BacktestResult | None:
    """볼린저밴드 전략 백테스트.

    하단 이탈 → 매수, 상단 이탈 → 매도.
    """
    try:
        hist = yf.Ticker(ticker).history(period=period)
        if len(hist) < 25:
            return None

        close = hist["Close"]
        bb_mid = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        bb_upper = bb_mid + 2 * bb_std
        bb_lower = bb_mid - 2 * bb_std

        # 시그널
        positions = pd.Series(0, index=close.index)
        for i in range(20, len(close)):
            if close.iloc[i] <= bb_lower.iloc[i]:
                positions.iloc[i] = 1
            elif close.iloc[i] >= bb_upper.iloc[i]:
                positions.iloc[i] = -1

        return _simulate_from_signals(
            close, positions, ticker, name, "Bollinger(20,2)", period,
        )
    except Exception as e:
        log.warning(f"볼린저 백테스트 실패 ({ticker}): {e}")
        return None


def _simulate(
    close: pd.Series,
    indicator: pd.Series,
    buy_threshold: float,
    sell_threshold: float,
    ticker: str,
    name: str,
    strategy: str,
    period: str,
) -> BacktestResult:
    """지표 기반 매매 시뮬레이션."""
    positions = pd.Series(0, index=close.index)
    for i in range(14, len(indicator)):
        if indicator.iloc[i] < buy_threshold:
            positions.iloc[i] = 1
        elif indicator.iloc[i] > sell_threshold:
            positions.iloc[i] = -1

    return _simulate_from_signals(close, positions, ticker, name, strategy, period)


def _round_trip_cost_pct(ticker: str) -> float:
    """왕복 거래비용(%) — 한국: 매도세 0.18% + 수수료 ~0.05%, 미국: 수수료+SEC fee ~0.1%.

    백테스트에 비용 미반영 시 단타 전략 성과가 과대평가됨 (거래 많을수록 심각).
    """
    return 0.23 if ticker.endswith((".KS", ".KQ")) else 0.10


def _simulate_from_signals(
    close: pd.Series,
    signals: pd.Series,
    ticker: str,
    name: str,
    strategy: str,
    period: str,
) -> BacktestResult:
    """시그널 시리즈로 매매 시뮬레이션 (왕복 거래비용 차감)."""
    cost = _round_trip_cost_pct(ticker)

    # 포지션 상태 추적
    holding = False
    entry_price = 0.0
    trades: list[float] = []  # 각 거래의 수익률 (비용 차감 후)

    for i in range(len(close)):
        if signals.iloc[i] == 1 and not holding:
            holding = True
            entry_price = float(close.iloc[i])
        elif signals.iloc[i] == -1 and holding:
            holding = False
            exit_price = float(close.iloc[i])
            if entry_price > 0:
                pnl_pct = (exit_price - entry_price) / entry_price * 100 - cost
                trades.append(pnl_pct)

    # 미청산 포지션 처리
    if holding and entry_price > 0:
        exit_price = float(close.iloc[-1])
        pnl_pct = (exit_price - entry_price) / entry_price * 100 - cost
        trades.append(pnl_pct)

    # 바이앤홀드
    start_valid = close.dropna()
    if len(start_valid) < 2 or float(start_valid.iloc[0]) == 0:
        bnh = 0.0
    else:
        bnh = (float(start_valid.iloc[-1]) - float(start_valid.iloc[0])) / float(start_valid.iloc[0]) * 100

    # 통계 계산
    total_return = sum(trades) if trades else 0.0
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    win_rate = (len(wins) / len(trades) * 100) if trades else 0.0

    total_gains = sum(wins) if wins else 0.0
    total_losses = abs(sum(losses)) if losses else 0.001
    profit_factor = total_gains / total_losses

    # 확장 지표
    from core.metrics import compute_all_metrics

    daily_returns = close.pct_change().dropna()
    metrics = compute_all_metrics(daily_returns)

    return BacktestResult(
        ticker=ticker,
        name=name,
        strategy=strategy,
        period=period,
        total_return_pct=round(total_return, 2),
        buy_hold_return_pct=round(bnh, 2),
        excess_return_pct=round(total_return - bnh, 2),
        win_rate_pct=round(win_rate, 1),
        total_trades=len(trades),
        max_drawdown_pct=metrics.get("max_drawdown_pct", 0),
        sharpe_ratio=metrics.get("sharpe_ratio", 0),
        profit_factor=round(profit_factor, 2),
        sortino_ratio=metrics.get("sortino_ratio", 0),
        calmar_ratio=metrics.get("calmar_ratio", 0),
        cvar_95=metrics.get("cvar_95", 0),
        max_underwater_days=metrics.get("max_underwater_days", 0),
    )


# ═══════════════════════════════════════════════════════
# 종합 백테스트
# ═══════════════════════════════════════════════════════
def backtest_breakout(
    ticker: str,
    name: str = "",
    period: str = "1y",
    lookback: int = 50,
    trail_pct: float = 10.0,
) -> BacktestResult | None:
    """신고가 돌파 + 트레일링 스탑 전략 백테스트 (추세추종).

    스캐너 발굴 종목(모멘텀 주도주) 검증용 — 역추세(RSI/볼린저)와 달리
    "오르는 것을 사서 추세가 꺾일 때까지 보유"를 검증한다.

    매수: 종가가 직전 lookback일 최고가 돌파
    매도: 매수 후 고점 대비 trail_pct% 하락 (트레일링 스탑)
    """
    try:
        hist = yf.Ticker(ticker).history(period=period)
        if len(hist) < lookback + 10:
            return None

        close = hist["Close"]
        rolling_high = close.rolling(lookback).max().shift(1)  # 직전 N일 고점

        positions = pd.Series(0, index=close.index)
        in_position = False
        peak = 0.0
        for i in range(lookback, len(close)):
            price = float(close.iloc[i])
            if not in_position:
                if price > float(rolling_high.iloc[i]):
                    positions.iloc[i] = 1
                    in_position = True
                    peak = price
            else:
                peak = max(peak, price)
                if price <= peak * (1 - trail_pct / 100):
                    positions.iloc[i] = -1
                    in_position = False

        return _simulate_from_signals(
            close, positions, ticker, name,
            f"돌파({lookback}d)+트레일{trail_pct:.0f}%", period,
        )
    except Exception as e:
        log.warning(f"돌파 백테스트 실패 ({ticker}): {e}")
        return None


def backtest_ma_trend(
    ticker: str,
    name: str = "",
    period: str = "1y",
) -> BacktestResult | None:
    """MA 정배열 추세추종 백테스트.

    매수: 종가 > MA20 > MA60 (정배열 형성)
    매도: 종가 < MA20 (단기 추세 이탈)
    """
    try:
        hist = yf.Ticker(ticker).history(period=period)
        if len(hist) < 70:
            return None

        close = hist["Close"]
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()

        positions = pd.Series(0, index=close.index)
        in_position = False
        for i in range(60, len(close)):
            price = float(close.iloc[i])
            m20, m60 = float(ma20.iloc[i]), float(ma60.iloc[i])
            if not in_position and price > m20 > m60:
                positions.iloc[i] = 1
                in_position = True
            elif in_position and price < m20:
                positions.iloc[i] = -1
                in_position = False

        return _simulate_from_signals(
            close, positions, ticker, name, "MA정배열(20/60)", period,
        )
    except Exception as e:
        log.warning(f"MA추세 백테스트 실패 ({ticker}): {e}")
        return None


def backtest_all_strategies(
    ticker: str, name: str = "", period: str = "1y"
) -> list[BacktestResult]:
    """5개 전략(RSI, MACD, 볼린저 + 돌파, MA추세) 백테스트 실행.

    역추세 3종 + 추세추종 2종 — 종목 성격(횡보 vs 추세)에 맞는 전략 식별.
    """
    results: list[BacktestResult] = []

    for fn in (backtest_rsi, backtest_macd, backtest_bollinger, backtest_breakout, backtest_ma_trend):
        r = fn(ticker, name, period)
        if r is not None:
            results.append(r)

    return results


def _rsi_simulate_on(
    close: pd.Series, buy_th: float, sell_th: float,
    ticker: str, name: str, strategy: str, period: str,
) -> BacktestResult | None:
    """주어진 종가 시리즈에 RSI 전략 시뮬레이션 (데이터 재다운로드 없음)."""
    if len(close) < 30:
        return None
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = gain / loss.replace(0, np.nan)
        rs = rs.fillna(100.0)
    rsi = 100 - (100 / (1 + rs))
    return _simulate(close, rsi, buy_th, sell_th, ticker, name, strategy, period)


def optimize_rsi_params(
    ticker: str, name: str = "", period: str = "1y",
    buy_range: tuple[int, ...] = (20, 25, 30, 35),
    sell_range: tuple[int, ...] = (65, 70, 75, 80),
) -> BacktestResult | None:
    """RSI 파라미터 워크포워드 최적화 — 과적합 방지.

    이전 방식(같은 기간에 최적화+평가)은 항상 좋아 보이는 과적합 함정.
    수정: 앞 75% (in-sample)에서 그리드 탐색 → 뒤 25% (out-of-sample)에서 평가.
    반환 결과의 수익률/승률은 **미래 구간 성과** — 실전 기대치에 근접.
    """
    try:
        hist = yf.Ticker(ticker).history(period=period)
        if len(hist) < 120:  # 워크포워드에 최소 ~6개월 필요
            return None
        close = hist["Close"]
    except Exception as e:
        log.warning(f"RSI 최적화 데이터 실패 ({ticker}): {e}")
        return None

    split = int(len(close) * 0.75)
    in_sample = close.iloc[:split]
    out_sample = close.iloc[split - 14:]  # RSI 워밍업 14일 겹침

    # 1) in-sample 그리드 탐색
    best_params: tuple[float, float] | None = None
    best_sharpe = float("-inf")
    for buy_th in buy_range:
        for sell_th in sell_range:
            if buy_th >= sell_th:
                continue
            r = _rsi_simulate_on(in_sample, float(buy_th), float(sell_th),
                                 ticker, name, "tmp", period)
            if r is not None and r.total_trades > 0 and r.sharpe_ratio > best_sharpe:
                best_sharpe = r.sharpe_ratio
                best_params = (float(buy_th), float(sell_th))

    if best_params is None:
        return None

    # 2) out-of-sample 평가 — 이 성과가 보고됨
    buy_th, sell_th = best_params
    result = _rsi_simulate_on(
        out_sample, buy_th, sell_th, ticker, name,
        f"RSI워크포워드({buy_th:.0f}/{sell_th:.0f})", period,
    )
    if result is None:
        return None

    from dataclasses import replace
    return replace(
        result,
        optimized_params={
            "rsi_buy": buy_th, "rsi_sell": sell_th,
            "validation": "walk_forward_75_25",
        },
    )


def backtest_regime_aware(
    ticker: str,
    name: str = "",
    period: str = "1y",
    regime: str = "횡보장",
) -> BacktestResult | None:
    """레짐에 따라 다른 파라미터로 RSI 백테스트.

    강세장: 40/80 (추세 따라 늦게 팔기)
    약세장: 20/60 (보수적 진입, 빨리 탈출)
    횡보장: 30/70 (기본)
    위기: 15/50 (극보수적)
    """
    params = {
        "강세장": (40, 80),
        "약세장": (20, 60),
        "횡보장": (30, 70),
        "위기": (15, 50),
    }
    buy_th, sell_th = params.get(regime, (30, 70))

    r = backtest_rsi(ticker, name, period, float(buy_th), float(sell_th))
    if r is None:
        return None

    from dataclasses import replace
    return replace(
        r,
        strategy=f"RSI레짐({regime}:{buy_th}/{sell_th})",
        optimized_params={"regime": regime, "rsi_buy": buy_th, "rsi_sell": sell_th},
    )


def backtest_to_text(results: list[BacktestResult]) -> str:
    """백테스트 결과를 텍스트로 변환."""
    if not results:
        return "(백테스트 데이터 없음)"

    lines = ["【백테스트 결과】"]
    for r in results:
        lines.append(
            f"  {r.name} [{r.strategy}] {r.period}: "
            f"수익 {r.total_return_pct:+.1f}% vs B&H {r.buy_hold_return_pct:+.1f}% "
            f"(초과 {r.excess_return_pct:+.1f}%) | 승률 {r.win_rate_pct:.0f}% | "
            f"거래 {r.total_trades}회 | MDD {r.max_drawdown_pct:.1f}% | "
            f"Sharpe {r.sharpe_ratio:.2f} | Sortino {r.sortino_ratio:.2f} | "
            f"CVaR(5%) {r.cvar_95*100:.1f}% | 수중 {r.max_underwater_days}일"
        )
    return "\n".join(lines)
