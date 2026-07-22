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


# ── 거래비용 모델 ────────────────────────────────────────
# 왕복(round-trip) 기준. commission_tax = 수수료+세금, slippage = 체결 슬리피지/스프레드.
_COMMISSION_TAX_PCT = {"KR": 0.23, "US": 0.10}  # 한국: 매도세0.18%+수수료~0.05% / 미국: 수수료+SEC~0.1%
_SLIPPAGE_PCT = {"KR": 0.10, "US": 0.06}         # 왕복 체결 슬리피지 추정(유동 종목, 편도 ~3~5bps×2)


def _market_of(ticker: str) -> str:
    return "KR" if ticker.endswith((".KS", ".KQ")) else "US"


def _round_trip_cost_pct(ticker: str, include_slippage: bool = True) -> float:
    """왕복 거래비용(%) = 수수료+세금 (+ 슬리피지).

    비용/슬리피지 미반영 시 단타 전략 성과가 과대평가됨 (거래 많을수록 심각).
    include_slippage=False 는 수수료+세금만 반환 (구 동작 호환).
    """
    mkt = _market_of(ticker)
    cost = _COMMISSION_TAX_PCT[mkt]
    if include_slippage:
        cost += _SLIPPAGE_PCT[mkt]
    return cost


def _rsi_positions(close: pd.Series, buy_th: float, sell_th: float) -> pd.Series:
    """RSI 진입/청산 시그널 시리즈 (1=매수, -1=매도)."""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = gain / loss.replace(0, np.nan)
        rs = rs.fillna(100.0)
    rsi = 100 - (100 / (1 + rs))
    positions = pd.Series(0, index=close.index)
    for i in range(14, len(rsi)):
        if rsi.iloc[i] < buy_th:
            positions.iloc[i] = 1
        elif rsi.iloc[i] > sell_th:
            positions.iloc[i] = -1
    return positions


def _collect_trade_returns(
    close: pd.Series, signals: pd.Series, cost: float
) -> list[float]:
    """시그널대로 매매했을 때 각 왕복 거래의 수익률(%) 리스트 (비용 차감 후)."""
    holding = False
    entry_price = 0.0
    trades: list[float] = []
    for i in range(len(close)):
        if signals.iloc[i] == 1 and not holding:
            holding = True
            entry_price = float(close.iloc[i])
        elif signals.iloc[i] == -1 and holding:
            holding = False
            exit_price = float(close.iloc[i])
            if entry_price > 0:
                trades.append((exit_price - entry_price) / entry_price * 100 - cost)
    # 미청산 포지션은 마지막 종가로 청산
    if holding and entry_price > 0:
        exit_price = float(close.iloc[-1])
        trades.append((exit_price - entry_price) / entry_price * 100 - cost)
    return trades


def _stats_from_trades(
    trades: list[float],
    close: pd.Series,
    ticker: str,
    name: str,
    strategy: str,
    period: str,
) -> BacktestResult:
    """거래 수익률 리스트 + 종가로 BacktestResult 조립."""
    start_valid = close.dropna()
    if len(start_valid) < 2 or float(start_valid.iloc[0]) == 0:
        bnh = 0.0
    else:
        bnh = (float(start_valid.iloc[-1]) - float(start_valid.iloc[0])) / float(start_valid.iloc[0]) * 100

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


def _simulate_from_signals(
    close: pd.Series,
    signals: pd.Series,
    ticker: str,
    name: str,
    strategy: str,
    period: str,
) -> BacktestResult:
    """시그널 시리즈로 매매 시뮬레이션 (왕복 거래비용+슬리피지 차감)."""
    cost = _round_trip_cost_pct(ticker)
    trades = _collect_trade_returns(close, signals, cost)
    return _stats_from_trades(trades, close, ticker, name, strategy, period)


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


def walk_forward_rsi_on(
    close: pd.Series,
    ticker: str = "",
    name: str = "",
    period: str = "",
    folds: int = 3,
    buy_range: tuple[int, ...] = (20, 25, 30, 35),
    sell_range: tuple[int, ...] = (65, 70, 75, 80),
) -> BacktestResult | None:
    """다중 폴드 앵커드 워크포워드 RSI 검증 (네트워크 불필요).

    단일 75/25 분할(optimize_rsi_params)보다 강건: N개 폴드마다
    누적 in-sample으로 파라미터 선택 → 다음 OOS 블록에서 평가 → OOS 거래 통합.
    보고 성과 = 통합 out-of-sample (과적합 최소화, 실전 기대치에 더 근접).
    """
    n = len(close)
    if folds < 1 or n < 120:
        return None
    block = n // (folds + 1)
    if block < 25:
        return None

    all_oos: list[float] = []
    per_fold: list[tuple[float, float]] = []
    cost = _round_trip_cost_pct(ticker)

    for i in range(1, folds + 1):
        in_end = block * i
        in_sample = close.iloc[:in_end]
        oos_start = max(0, in_end - 14)  # RSI 워밍업 14일 겹침
        oos_end = n if i == folds else block * (i + 1)
        oos = close.iloc[oos_start:oos_end]
        if len(in_sample) < 30 or len(oos) < 20:
            continue

        # in-sample 그리드 탐색 (Sharpe 최대)
        best: tuple[float, float] | None = None
        best_sharpe = float("-inf")
        for b in buy_range:
            for s in sell_range:
                if b >= s:
                    continue
                r = _rsi_simulate_on(in_sample, float(b), float(s), ticker, name, "tmp", period)
                if r is not None and r.total_trades > 0 and r.sharpe_ratio > best_sharpe:
                    best_sharpe = r.sharpe_ratio
                    best = (float(b), float(s))
        if best is None:
            continue

        # 선택 파라미터로 OOS 거래 수집
        signals = _rsi_positions(oos, best[0], best[1])
        all_oos.extend(_collect_trade_returns(oos, signals, cost))
        per_fold.append(best)

    if not all_oos or not per_fold:
        return None

    from dataclasses import replace

    result = _stats_from_trades(
        all_oos, close, ticker, name,
        f"RSI워크포워드×{len(per_fold)}fold", period,
    )
    return replace(
        result,
        optimized_params={
            "folds": len(per_fold),
            "params_per_fold": per_fold,
            "validation": f"walk_forward_{folds}fold",
        },
    )


def walk_forward_rsi(
    ticker: str, name: str = "", period: str = "1y", folds: int = 3,
) -> BacktestResult | None:
    """티커 다운로드 후 다중 폴드 워크포워드 RSI 검증."""
    try:
        hist = yf.Ticker(ticker).history(period=period)
        if len(hist) < 120:
            return None
        close = hist["Close"]
    except Exception as e:
        log.warning(f"워크포워드 RSI 데이터 실패 ({ticker}): {e}")
        return None
    return walk_forward_rsi_on(close, ticker, name, period, folds)


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
