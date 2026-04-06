"""
기술 지표 자동 계산 — RSI, MACD, 볼린저밴드, OBV + 합류 점수

yfinance 히스토리 데이터를 기반으로 기술 지표를 계산하고
매수/매도 신호의 합류(confluence) 점수를 산출한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import yfinance as yf

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndicatorResult:
    """단일 종목 기술 지표 결과."""

    ticker: str
    name: str

    # RSI
    rsi: float = 50.0
    rsi_signal: int = 0  # -1 매도, 0 중립, 1 매수

    # MACD
    macd: float = 0.0
    macd_signal_line: float = 0.0
    macd_histogram: float = 0.0
    macd_signal: int = 0

    # 볼린저밴드
    bb_upper: float = 0.0
    bb_middle: float = 0.0
    bb_lower: float = 0.0
    bb_position: float = 0.5  # 0=하단, 1=상단
    bb_signal: int = 0

    # OBV
    obv_trend: int = 0  # -1 감소, 0 횡보, 1 증가
    obv_signal: int = 0

    # 합류 점수
    confluence_score: int = 0  # -4 ~ +4
    confluence_label: str = "중립"

    def to_text(self) -> str:
        """텍스트 요약."""
        return (
            f"  RSI: {self.rsi:.1f} ({_signal_str(self.rsi_signal)}) | "
            f"MACD: {self.macd_histogram:+.2f} ({_signal_str(self.macd_signal)}) | "
            f"BB: {self.bb_position:.0%} ({_signal_str(self.bb_signal)}) | "
            f"OBV: {_signal_str(self.obv_signal)} | "
            f"합류: {self.confluence_score:+d} [{self.confluence_label}]"
        )


def _signal_str(sig: int) -> str:
    if sig > 0:
        return "매수"
    if sig < 0:
        return "매도"
    return "중립"


def calculate_indicators(
    ticker: str, name: str = "", period: str = "3mo"
) -> IndicatorResult | None:
    """yfinance 데이터로 기술 지표 계산.

    Returns:
        IndicatorResult 또는 데이터 부족 시 None
    """
    try:
        hist = yf.Ticker(ticker).history(period=period)
        if len(hist) < 26:
            return None

        close = hist["Close"]
        volume = hist["Volume"]

        # ─── RSI (14일) ────────────────────────────
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
        rs = gain / loss.replace(0, float("inf"))
        rsi_series = 100 - (100 / (1 + rs))
        rsi = float(rsi_series.iloc[-1])

        rsi_signal = 0
        if rsi < 30:
            rsi_signal = 1  # 과매도 → 매수
        elif rsi > 70:
            rsi_signal = -1  # 과매수 → 매도

        # ─── MACD (12, 26, 9) ──────────────────────
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        histogram = macd_line - signal_line

        macd_val = float(macd_line.iloc[-1])
        signal_val = float(signal_line.iloc[-1])
        hist_val = float(histogram.iloc[-1])
        hist_prev = float(histogram.iloc[-2])

        macd_signal = 0
        if hist_val > 0 and hist_prev <= 0:
            macd_signal = 1  # 골든크로스
        elif hist_val < 0 and hist_prev >= 0:
            macd_signal = -1  # 데드크로스
        elif hist_val > 0 and hist_val > hist_prev:
            macd_signal = 1  # 상승 가속
        elif hist_val < 0 and hist_val < hist_prev:
            macd_signal = -1  # 하락 가속

        # ─── 볼린저밴드 (20일, 2σ) ─────────────────
        bb_mid = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        bb_up = bb_mid + 2 * bb_std
        bb_low = bb_mid - 2 * bb_std

        bb_upper_val = float(bb_up.iloc[-1])
        bb_middle_val = float(bb_mid.iloc[-1])
        bb_lower_val = float(bb_low.iloc[-1])

        current_price = float(close.iloc[-1])
        bb_range = bb_upper_val - bb_lower_val
        bb_pos = (current_price - bb_lower_val) / bb_range if bb_range > 0 else 0.5

        bb_signal = 0
        if bb_pos < 0.05:
            bb_signal = 1  # 하단 이탈 → 매수
        elif bb_pos > 0.95:
            bb_signal = -1  # 상단 이탈 → 매도

        # ─── OBV ───────────────────────────────────
        obv = (volume * close.diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))).cumsum()
        obv_sma = obv.rolling(20).mean()

        obv_current = float(obv.iloc[-1])
        obv_sma_val = float(obv_sma.iloc[-1])

        obv_trend = 0
        if obv_current > obv_sma_val * 1.05:
            obv_trend = 1
        elif obv_current < obv_sma_val * 0.95:
            obv_trend = -1

        # OBV가 가격 방향과 일치하면 확인, 다르면 다이버전스
        price_trend = 1 if float(close.iloc[-1]) > float(close.iloc[-5]) else -1
        obv_signal = 0
        if obv_trend == 1 and price_trend == -1:
            obv_signal = 1  # 강세 다이버전스
        elif obv_trend == -1 and price_trend == 1:
            obv_signal = -1  # 약세 다이버전스
        elif obv_trend == 1:
            obv_signal = 1  # 거래량 확인
        elif obv_trend == -1:
            obv_signal = -1

        # ─── 합류 점수 ─────────────────────────────
        score = rsi_signal + macd_signal + bb_signal + obv_signal

        if score >= 3:
            label = "강력매수"
        elif score >= 2:
            label = "매수"
        elif score >= 1:
            label = "약매수"
        elif score <= -3:
            label = "강력매도"
        elif score <= -2:
            label = "매도"
        elif score <= -1:
            label = "약매도"
        else:
            label = "중립"

        return IndicatorResult(
            ticker=ticker,
            name=name,
            rsi=rsi,
            rsi_signal=rsi_signal,
            macd=macd_val,
            macd_signal_line=signal_val,
            macd_histogram=hist_val,
            macd_signal=macd_signal,
            bb_upper=bb_upper_val,
            bb_middle=bb_middle_val,
            bb_lower=bb_lower_val,
            bb_position=bb_pos,
            bb_signal=bb_signal,
            obv_trend=obv_trend,
            obv_signal=obv_signal,
            confluence_score=score,
            confluence_label=label,
        )
    except Exception as e:
        log.warning(f"기술 지표 계산 실패 ({ticker}): {e}")
        return None


def calculate_all(
    tickers: dict[str, str], period: str = "3mo"
) -> dict[str, IndicatorResult]:
    """포트폴리오 전체 기술 지표 계산.

    Args:
        tickers: {ticker: name} 딕셔너리
        period: yfinance 기간 (기본 3개월)

    Returns:
        {ticker: IndicatorResult} 딕셔너리
    """
    results: dict[str, IndicatorResult] = {}
    for tk, nm in tickers.items():
        result = calculate_indicators(tk, nm, period)
        if result is not None:
            results[tk] = result
    return results


def indicators_to_text(indicators: dict[str, IndicatorResult]) -> str:
    """기술 지표 결과를 텍스트로 변환 (프롬프트 삽입용)."""
    if not indicators:
        return "(기술 지표 데이터 없음)"

    lines = ["【기술 지표 분석】"]
    for tk, ind in indicators.items():
        lines.append(f"{ind.name} ({tk}):")
        lines.append(ind.to_text())
    return "\n".join(lines)
