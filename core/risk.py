"""
리스크 관리 모듈 — ATR 포지션 사이징, 상관관계, 최대 낙폭

포지션 사이징: "얼마나 살 것인가"를 결정
상관관계: 종목 간 집중 리스크 감지
최대 낙폭: 포트폴리오 리스크 수준 추적
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import yfinance as yf
import pandas as pd
import numpy as np

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# ATR 기반 포지션 사이징
# ═══════════════════════════════════════════════════════
@dataclass(frozen=True)
class PositionSize:
    """포지션 사이징 결과."""

    ticker: str
    name: str
    current_price: float
    atr: float  # 14일 ATR
    atr_pct: float  # ATR / 현재가 (%)
    recommended_shares: int  # 추천 수량
    position_value: float  # 추천 포지션 금액
    risk_per_share: float  # 주당 리스크 (2 × ATR)
    stop_loss_price: float  # ATR 기반 손절가


def calculate_atr(ticker: str, period: str = "3mo") -> float | None:
    """14일 ATR(Average True Range) 계산."""
    try:
        hist = yf.Ticker(ticker).history(period=period)
        if len(hist) < 15:
            return None

        high = hist["High"]
        low = hist["Low"]
        close = hist["Close"]

        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        return float(tr.rolling(14).mean().iloc[-1])
    except Exception as e:
        log.warning(f"ATR 계산 실패 ({ticker}): {e}")
        return None


def calculate_position_size(
    ticker: str,
    name: str,
    total_capital: float,
    risk_pct: float = 0.02,
    period: str = "3mo",
) -> PositionSize | None:
    """ATR 기반 포지션 사이징.

    Args:
        ticker: 종목 코드
        name: 종목명
        total_capital: 총 투자 자본
        risk_pct: 1회 매매 최대 리스크 비율 (기본 2%)
        period: 데이터 기간

    Returns:
        PositionSize 또는 데이터 부족 시 None
    """
    try:
        hist = yf.Ticker(ticker).history(period=period)
        if len(hist) < 15:
            return None

        current_price = float(hist["Close"].iloc[-1])

        high = hist["High"]
        low = hist["Low"]
        close = hist["Close"]

        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])

        # 리스크 금액 = 총 자본 × 리스크 비율
        risk_amount = total_capital * risk_pct

        # 주당 리스크 = 2 × ATR (손절 기준)
        risk_per_share = 2 * atr

        # 추천 수량 = 리스크 금액 / 주당 리스크
        if risk_per_share > 0:
            shares = max(1, int(risk_amount / risk_per_share))
        else:
            shares = 1

        # 최대 포지션 금액 제한 (총 자본의 20%)
        max_position = total_capital * 0.20
        if shares * current_price > max_position:
            shares = max(1, int(max_position / current_price))

        return PositionSize(
            ticker=ticker,
            name=name,
            current_price=round(current_price, 2),
            atr=round(atr, 2),
            atr_pct=round(atr / current_price * 100, 2),
            recommended_shares=shares,
            position_value=round(shares * current_price, 0),
            risk_per_share=round(risk_per_share, 2),
            stop_loss_price=round(current_price - risk_per_share, 2),
        )
    except Exception as e:
        log.warning(f"포지션 사이징 실패 ({ticker}): {e}")
        return None


# ═══════════════════════════════════════════════════════
# 포트폴리오 상관관계 분석
# ═══════════════════════════════════════════════════════
@dataclass(frozen=True)
class CorrelationAlert:
    """높은 상관관계 경고."""

    ticker_a: str
    name_a: str
    ticker_b: str
    name_b: str
    correlation: float  # -1 ~ +1
    risk_level: str  # 높음/보통/낮음


def analyze_correlation(
    tickers: dict[str, str], period: str = "6mo", threshold: float = 0.7
) -> tuple[pd.DataFrame, list[CorrelationAlert]]:
    """포트폴리오 종목 간 상관관계 분석.

    Args:
        tickers: {ticker: name}
        period: 분석 기간
        threshold: 경고 임계치 (기본 0.7)

    Returns:
        (상관관계 매트릭스 DataFrame, 높은 상관관계 경고 리스트)
    """
    closes: dict[str, pd.Series] = {}
    for tk in tickers:
        try:
            hist = yf.Ticker(tk).history(period=period)
            if len(hist) >= 20:
                closes[tk] = hist["Close"]
        except Exception:
            continue

    if len(closes) < 2:
        return pd.DataFrame(), []

    df = pd.DataFrame(closes)
    # 수익률 기반 상관관계
    returns = df.pct_change().dropna()
    corr_matrix = returns.corr()

    alerts: list[CorrelationAlert] = []
    seen: set[tuple[str, str]] = set()

    for i, tk_a in enumerate(corr_matrix.columns):
        for j, tk_b in enumerate(corr_matrix.columns):
            if i >= j:
                continue
            pair = (tk_a, tk_b)
            if pair in seen:
                continue
            seen.add(pair)

            corr = float(corr_matrix.loc[tk_a, tk_b])
            if abs(corr) >= threshold:
                if abs(corr) >= 0.85:
                    level = "높음"
                elif abs(corr) >= threshold:
                    level = "보통"
                else:
                    level = "낮음"

                alerts.append(CorrelationAlert(
                    ticker_a=tk_a,
                    name_a=tickers.get(tk_a, tk_a),
                    ticker_b=tk_b,
                    name_b=tickers.get(tk_b, tk_b),
                    correlation=round(corr, 3),
                    risk_level=level,
                ))

    return corr_matrix, alerts


# ═══════════════════════════════════════════════════════
# 최대 낙폭 (Maximum Drawdown)
# ═══════════════════════════════════════════════════════
@dataclass(frozen=True)
class DrawdownInfo:
    """낙폭 정보."""

    ticker: str
    name: str
    max_drawdown_pct: float  # 최대 낙폭 (%)
    current_drawdown_pct: float  # 현재 낙폭 (고점 대비)
    peak_price: float  # 기간 내 고점
    current_price: float
    risk_level: str  # 위험/주의/안전


def calculate_drawdown(
    ticker: str, name: str = "", period: str = "6mo"
) -> DrawdownInfo | None:
    """최대 낙폭 및 현재 낙폭 계산."""
    try:
        hist = yf.Ticker(ticker).history(period=period)
        if len(hist) < 5:
            return None

        close = hist["Close"]
        peak = close.cummax()
        drawdown = (close - peak) / peak * 100

        max_dd = float(drawdown.min())
        current_dd = float(drawdown.iloc[-1])
        peak_price = float(peak.iloc[-1])
        current_price = float(close.iloc[-1])

        if current_dd <= -20:
            level = "위험"
        elif current_dd <= -10:
            level = "주의"
        else:
            level = "안전"

        return DrawdownInfo(
            ticker=ticker,
            name=name,
            max_drawdown_pct=round(max_dd, 2),
            current_drawdown_pct=round(current_dd, 2),
            peak_price=round(peak_price, 2),
            current_price=round(current_price, 2),
            risk_level=level,
        )
    except Exception as e:
        log.warning(f"낙폭 계산 실패 ({ticker}): {e}")
        return None


# ═══════════════════════════════════════════════════════
# 통합 리스크 리포트
# ═══════════════════════════════════════════════════════
@dataclass(frozen=True)
class RiskReport:
    """포트폴리오 리스크 종합 리포트."""

    position_sizes: tuple[PositionSize, ...] = ()
    correlation_alerts: tuple[CorrelationAlert, ...] = ()
    drawdowns: tuple[DrawdownInfo, ...] = ()
    overall_risk: str = "보통"  # 낮음/보통/높음/위험


def generate_risk_report(
    tickers: dict[str, str],
    total_capital: float,
) -> RiskReport:
    """포트폴리오 전체 리스크 리포트 생성."""
    # 포지션 사이징
    positions: list[PositionSize] = []
    for tk, nm in tickers.items():
        ps = calculate_position_size(tk, nm, total_capital)
        if ps:
            positions.append(ps)

    # 상관관계
    _, corr_alerts = analyze_correlation(tickers)

    # 낙폭
    drawdowns: list[DrawdownInfo] = []
    for tk, nm in tickers.items():
        dd = calculate_drawdown(tk, nm)
        if dd:
            drawdowns.append(dd)

    # 전체 리스크 레벨 판단
    danger_count = sum(1 for d in drawdowns if d.risk_level == "위험")
    high_corr_count = sum(1 for a in corr_alerts if a.risk_level == "높음")

    if danger_count >= 3 or high_corr_count >= 3:
        overall = "위험"
    elif danger_count >= 1 or high_corr_count >= 2:
        overall = "높음"
    elif any(d.risk_level == "주의" for d in drawdowns):
        overall = "보통"
    else:
        overall = "낮음"

    return RiskReport(
        position_sizes=tuple(positions),
        correlation_alerts=tuple(corr_alerts),
        drawdowns=tuple(drawdowns),
        overall_risk=overall,
    )


def risk_report_to_text(report: RiskReport) -> str:
    """리스크 리포트를 텍스트로 변환 (프롬프트 삽입용)."""
    lines = [f"【리스크 분석】 전체 리스크: {report.overall_risk}"]

    if report.position_sizes:
        lines.append("\n  [ATR 포지션 사이징]")
        for ps in report.position_sizes:
            lines.append(
                f"  {ps.name}: ATR={ps.atr:,.0f} ({ps.atr_pct:.1f}%) | "
                f"추천 {ps.recommended_shares}주 (₩{ps.position_value:,.0f}) | "
                f"손절 {ps.stop_loss_price:,.0f}"
            )

    if report.correlation_alerts:
        lines.append("\n  [높은 상관관계 경고]")
        for ca in report.correlation_alerts:
            lines.append(
                f"  ⚠️ {ca.name_a} ↔ {ca.name_b}: {ca.correlation:.3f} [{ca.risk_level}]"
            )

    if report.drawdowns:
        lines.append("\n  [낙폭 현황]")
        for dd in report.drawdowns:
            lines.append(
                f"  {dd.name}: 현재 {dd.current_drawdown_pct:+.1f}% "
                f"(최대 {dd.max_drawdown_pct:.1f}%) [{dd.risk_level}]"
            )

    return "\n".join(lines)
