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
        rs = gain / loss.replace(0, float("inf"))
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


def _simulate_from_signals(
    close: pd.Series,
    signals: pd.Series,
    ticker: str,
    name: str,
    strategy: str,
    period: str,
) -> BacktestResult:
    """시그널 시리즈로 매매 시뮬레이션."""
    # 포지션 상태 추적
    holding = False
    entry_price = 0.0
    trades: list[float] = []  # 각 거래의 수익률

    for i in range(len(close)):
        if signals.iloc[i] == 1 and not holding:
            holding = True
            entry_price = float(close.iloc[i])
        elif signals.iloc[i] == -1 and holding:
            holding = False
            exit_price = float(close.iloc[i])
            pnl_pct = (exit_price - entry_price) / entry_price * 100
            trades.append(pnl_pct)

    # 미청산 포지션 처리
    if holding:
        exit_price = float(close.iloc[-1])
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        trades.append(pnl_pct)

    # 바이앤홀드
    start_valid = close.dropna()
    if len(start_valid) < 2:
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

    # 최대 낙폭
    peak = close.cummax()
    drawdown = (close - peak) / peak * 100
    max_dd = float(drawdown.min())

    # 샤프 비율 (연환산)
    daily_returns = close.pct_change().dropna()
    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe = float(daily_returns.mean() / daily_returns.std() * (252 ** 0.5))
    else:
        sharpe = 0.0

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
        max_drawdown_pct=round(max_dd, 2),
        sharpe_ratio=round(sharpe, 2),
        profit_factor=round(profit_factor, 2),
    )


# ═══════════════════════════════════════════════════════
# 종합 백테스트
# ═══════════════════════════════════════════════════════
def backtest_all_strategies(
    ticker: str, name: str = "", period: str = "1y"
) -> list[BacktestResult]:
    """3개 전략(RSI, MACD, 볼린저) 백테스트 실행."""
    results: list[BacktestResult] = []

    for fn in (backtest_rsi, backtest_macd, backtest_bollinger):
        r = fn(ticker, name, period)
        if r is not None:
            results.append(r)

    return results


def backtest_to_text(results: list[BacktestResult]) -> str:
    """백테스트 결과를 텍스트로 변환."""
    if not results:
        return "(백테스트 데이터 없음)"

    lines = ["【백테스트 결과】"]
    for r in results:
        excess = f"{r.excess_return_pct:+.1f}%"
        lines.append(
            f"  {r.name} [{r.strategy}] {r.period}: "
            f"수익 {r.total_return_pct:+.1f}% vs B&H {r.buy_hold_return_pct:+.1f}% "
            f"(초과 {excess}) | 승률 {r.win_rate_pct:.0f}% | "
            f"거래 {r.total_trades}회 | MDD {r.max_drawdown_pct:.1f}% | "
            f"샤프 {r.sharpe_ratio:.2f}"
        )
    return "\n".join(lines)
