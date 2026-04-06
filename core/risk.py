"""
리스크 관리 모듈 — 서킷 브레이커 + 변동성 조정 + 상관관계 승수

Freqtrade 보호 플러그인 + ai-hedge-fund 리스크 매니저 패턴 적용.
- 서킷 브레이커: 연속 손실 시 매매 잠금, 낙폭 초과 시 전체 중단
- 변동성 조정: 저변동 종목 25%, 고변동 종목 10% 자동 배분
- 상관관계 승수: 높은 상관(>0.8)이면 포지션 0.7배 축소
- 수수료 반영: 한국 주식 0.25% 거래세 포함
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import yfinance as yf
import pandas as pd
import numpy as np

log = logging.getLogger(__name__)

# 한국 주식 거래세 + 수수료 (편도)
KR_FEE_RATE = 0.003  # 0.3% (거래세 0.23% + 증권사 수수료 ~0.07%)
US_FEE_RATE = 0.0005  # 0.05% (증권사 수수료)


# ═══════════════════════════════════════════════════════
# 서킷 브레이커
# ═══════════════════════════════════════════════════════
@dataclass(frozen=True)
class CircuitBreakerStatus:
    """서킷 브레이커 상태."""

    is_locked: bool = False
    reason: str = ""
    locked_tickers: tuple[str, ...] = ()
    portfolio_locked: bool = False  # 전체 매매 중단


def check_circuit_breaker(
    recent_outcomes: list[str],
    portfolio_drawdown_pct: float,
    ticker_outcomes: dict[str, list[str]] | None = None,
    max_consecutive_losses: int = 3,
    max_portfolio_drawdown: float = -5.0,
) -> CircuitBreakerStatus:
    """서킷 브레이커 체크.

    Args:
        recent_outcomes: 최근 매매 결과 리스트 ["win", "loss", ...]
        portfolio_drawdown_pct: 포트폴리오 전체 낙폭 (%)
        ticker_outcomes: 종목별 최근 결과 {ticker: ["win", "loss", ...]}
        max_consecutive_losses: 연속 손실 허용 횟수
        max_portfolio_drawdown: 포트폴리오 최대 낙폭 허용치 (%)
    """
    reasons: list[str] = []
    locked_tickers: list[str] = []
    portfolio_locked = False

    # 포트폴리오 전체 낙폭 체크
    if portfolio_drawdown_pct <= max_portfolio_drawdown:
        portfolio_locked = True
        reasons.append(
            f"포트폴리오 낙폭 {portfolio_drawdown_pct:.1f}% "
            f"(한도 {max_portfolio_drawdown}%) 초과 → 전체 매매 중단"
        )

    # 전체 연속 손실 체크
    consecutive = 0
    for outcome in reversed(recent_outcomes):
        if outcome == "loss":
            consecutive += 1
        else:
            break
    if consecutive >= max_consecutive_losses:
        portfolio_locked = True
        reasons.append(
            f"연속 {consecutive}회 손실 → 전체 매매 중단"
        )

    # 종목별 연속 손실 체크
    if ticker_outcomes:
        for tk, outcomes in ticker_outcomes.items():
            tk_consecutive = 0
            for outcome in reversed(outcomes):
                if outcome == "loss":
                    tk_consecutive += 1
                else:
                    break
            if tk_consecutive >= max_consecutive_losses:
                locked_tickers.append(tk)
                reasons.append(f"{tk}: 연속 {tk_consecutive}회 손실 → 종목 잠금")

    is_locked = portfolio_locked or len(locked_tickers) > 0
    return CircuitBreakerStatus(
        is_locked=is_locked,
        reason=" | ".join(reasons) if reasons else "",
        locked_tickers=tuple(locked_tickers),
        portfolio_locked=portfolio_locked,
    )


# ═══════════════════════════════════════════════════════
# 변동성 조정 포지션 한도
# ═══════════════════════════════════════════════════════
def calculate_volatility_adjusted_limit(
    annualized_vol: float,
) -> float:
    """변동성에 따른 최대 포지션 비율 반환.

    저변동(<15%): 25%, 중변동(15-30%): ~18%,
    고변동(30-50%): ~12%, 초고변동(>50%): 10%
    """
    if annualized_vol < 0.15:
        return 0.25
    if annualized_vol < 0.30:
        return 0.20 - (annualized_vol - 0.15) * 0.4  # 20% → 14%
    if annualized_vol < 0.50:
        return 0.14 - (annualized_vol - 0.30) * 0.2  # 14% → 10%
    return 0.10


def calculate_correlation_multiplier(avg_corr: float) -> float:
    """상관관계 기반 포지션 승수.

    높은 상관(>=0.8): 0.7배, 중간(0.5-0.8): 1.0배,
    낮은(<0.2): 1.1배
    """
    if avg_corr >= 0.8:
        return 0.7
    if avg_corr >= 0.5:
        return 1.0 - (avg_corr - 0.5) * 0.33  # 1.0 → 0.9
    if avg_corr < 0.2:
        return 1.1
    return 1.0


def get_annualized_volatility(ticker: str, period: str = "6mo") -> float:
    """연환산 변동성 계산."""
    try:
        hist = yf.Ticker(ticker).history(period=period)
        if len(hist) < 20:
            return 0.30  # 기본값
        daily_returns = hist["Close"].pct_change().dropna()
        return float(daily_returns.std() * np.sqrt(252))
    except Exception:
        return 0.30


def get_avg_correlation(
    ticker: str,
    corr_matrix: pd.DataFrame,
) -> float:
    """특정 종목의 포트폴리오 내 평균 상관관계."""
    if ticker not in corr_matrix.columns:
        return 0.0
    row = corr_matrix.loc[ticker].drop(ticker, errors="ignore")
    if row.empty:
        return 0.0
    return float(row.abs().mean())


# ═══════════════════════════════════════════════════════
# 켈리 기준 포지션
# ═══════════════════════════════════════════════════════
def kelly_fraction(win_rate: float, avg_win_loss_ratio: float) -> float:
    """켈리 기준: f* = W - (1-W)/R. 하프 켈리 적용.

    Args:
        win_rate: 승률 (0~1)
        avg_win_loss_ratio: 평균 수익/평균 손실 비율

    Returns:
        최적 베팅 비율 (0~1, 하프 켈리)
    """
    if avg_win_loss_ratio <= 0 or win_rate <= 0:
        return 0.0
    full_kelly = win_rate - ((1 - win_rate) / avg_win_loss_ratio)
    half_kelly = max(0.0, min(0.25, full_kelly * 0.5))  # 최대 25% 캡
    return round(half_kelly, 4)


# ═══════════════════════════════════════════════════════
# ATR 기반 포지션 사이징 (강화 버전)
# ═══════════════════════════════════════════════════════
@dataclass(frozen=True)
class PositionSize:
    """포지션 사이징 결과."""

    ticker: str
    name: str
    current_price: float
    atr: float
    atr_pct: float
    recommended_shares: int
    position_value: float
    risk_per_share: float
    stop_loss_price: float
    # 강화 필드
    vol_adjusted_limit: float = 0.20  # 변동성 조정 한도
    corr_multiplier: float = 1.0  # 상관관계 승수
    kelly_fraction: float = 0.0  # 켈리 기준 비율
    fee_adjusted: bool = False  # 수수료 반영 여부
    max_stop_distance_pct: float = 0.0  # 손절 거리 (%)


def calculate_position_size(
    ticker: str,
    name: str,
    total_capital: float,
    risk_pct: float = 0.02,
    period: str = "3mo",
    corr_matrix: pd.DataFrame | None = None,
    memory_stats: dict | None = None,
) -> PositionSize | None:
    """변동성+상관관계+켈리+수수료 통합 포지션 사이징."""
    try:
        hist = yf.Ticker(ticker).history(period=period)
        if len(hist) < 15:
            return None

        current_price = float(hist["Close"].iloc[-1])
        is_kr = ".KS" in ticker or ".KQ" in ticker
        fee_rate = KR_FEE_RATE if is_kr else US_FEE_RATE

        # ATR 계산
        high, low, close = hist["High"], hist["Low"], hist["Close"]
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])

        # 변동성 조정
        ann_vol = get_annualized_volatility(ticker, period)
        vol_limit = calculate_volatility_adjusted_limit(ann_vol)

        # 상관관계 승수
        corr_mult = 1.0
        if corr_matrix is not None:
            avg_corr = get_avg_correlation(ticker, corr_matrix)
            corr_mult = calculate_correlation_multiplier(avg_corr)

        # 켈리 기준
        kelly = 0.0
        if memory_stats and ticker in memory_stats:
            stats = memory_stats[ticker]
            wr = stats.get("win_rate", 0) / 100
            avg_pnl = stats.get("avg_pnl", 0)
            if wr > 0 and avg_pnl != 0:
                kelly = kelly_fraction(wr, abs(avg_pnl / max(1, 100 - stats.get("win_rate", 50))))

        # 주당 리스크 (수수료 포함)
        risk_per_share = 2 * atr + current_price * fee_rate * 2  # 왕복 수수료

        # 손절 거리 캡 (최대 10%)
        max_stop_pct = 0.10
        if risk_per_share > current_price * max_stop_pct:
            risk_per_share = current_price * max_stop_pct

        # 리스크 기반 수량
        risk_amount = total_capital * risk_pct
        if risk_per_share > 0:
            shares = max(1, int(risk_amount / risk_per_share))
        else:
            shares = 1

        # 변동성 조정 한도 적용
        max_position = total_capital * vol_limit * corr_mult
        if shares * current_price > max_position:
            shares = max(1, int(max_position / current_price))

        # 켈리 기반 추가 제약
        if kelly > 0:
            kelly_max = total_capital * kelly
            if shares * current_price > kelly_max:
                shares = max(1, int(kelly_max / current_price))

        stop_distance_pct = round(risk_per_share / current_price * 100, 2)

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
            vol_adjusted_limit=round(vol_limit, 3),
            corr_multiplier=round(corr_mult, 2),
            kelly_fraction=kelly,
            fee_adjusted=True,
            max_stop_distance_pct=stop_distance_pct,
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
    correlation: float
    risk_level: str


def analyze_correlation(
    tickers: dict[str, str], period: str = "6mo", threshold: float = 0.7
) -> tuple[pd.DataFrame, list[CorrelationAlert]]:
    """포트폴리오 종목 간 상관관계 분석."""
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
                level = "높음" if abs(corr) >= 0.85 else "보통"
                alerts.append(CorrelationAlert(
                    ticker_a=tk_a, name_a=tickers.get(tk_a, tk_a),
                    ticker_b=tk_b, name_b=tickers.get(tk_b, tk_b),
                    correlation=round(corr, 3), risk_level=level,
                ))

    return corr_matrix, alerts


# ═══════════════════════════════════════════════════════
# 최대 낙폭
# ═══════════════════════════════════════════════════════
@dataclass(frozen=True)
class DrawdownInfo:
    """낙폭 정보."""

    ticker: str
    name: str
    max_drawdown_pct: float
    current_drawdown_pct: float
    peak_price: float
    current_price: float
    risk_level: str


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
            ticker=ticker, name=name,
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
    circuit_breaker: CircuitBreakerStatus = field(default_factory=CircuitBreakerStatus)
    overall_risk: str = "보통"


def generate_risk_report(
    tickers: dict[str, str],
    total_capital: float,
    memory_stats: dict | None = None,
    recent_outcomes: list[str] | None = None,
) -> RiskReport:
    """포트폴리오 전체 리스크 리포트 생성."""
    # 상관관계 먼저 (포지션 사이징에 필요)
    corr_matrix, corr_alerts = analyze_correlation(tickers)

    # 포지션 사이징 (변동성+상관관계+켈리 통합)
    positions: list[PositionSize] = []
    for tk, nm in tickers.items():
        ps = calculate_position_size(
            tk, nm, total_capital,
            corr_matrix=corr_matrix,
            memory_stats=memory_stats,
        )
        if ps:
            positions.append(ps)

    # 낙폭
    drawdowns: list[DrawdownInfo] = []
    for tk, nm in tickers.items():
        dd = calculate_drawdown(tk, nm)
        if dd:
            drawdowns.append(dd)

    # 서킷 브레이커
    avg_drawdown = np.mean([d.current_drawdown_pct for d in drawdowns]) if drawdowns else 0
    cb = check_circuit_breaker(
        recent_outcomes=recent_outcomes or [],
        portfolio_drawdown_pct=avg_drawdown,
    )

    # 전체 리스크 레벨
    danger_count = sum(1 for d in drawdowns if d.risk_level == "위험")
    high_corr_count = sum(1 for a in corr_alerts if a.risk_level == "높음")

    if cb.portfolio_locked or danger_count >= 3 or high_corr_count >= 3:
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
        circuit_breaker=cb,
        overall_risk=overall,
    )


def risk_report_to_text(report: RiskReport) -> str:
    """리스크 리포트를 텍스트로 변환 (프롬프트 삽입용)."""
    lines = [f"【리스크 분석】 전체 리스크: {report.overall_risk}"]

    # 서킷 브레이커
    if report.circuit_breaker.is_locked:
        lines.append(f"\n  🚨 [서킷 브레이커 발동] {report.circuit_breaker.reason}")

    if report.position_sizes:
        lines.append("\n  [포지션 사이징 (변동성+상관관계+수수료 조정)]")
        for ps in report.position_sizes:
            lines.append(
                f"  {ps.name}: ATR={ps.atr:,.0f} ({ps.atr_pct:.1f}%) | "
                f"추천 {ps.recommended_shares}주 (₩{ps.position_value:,.0f}) | "
                f"손절 {ps.stop_loss_price:,.0f} (거리 {ps.max_stop_distance_pct:.1f}%) | "
                f"변동성한도 {ps.vol_adjusted_limit:.0%} × 상관승수 {ps.corr_multiplier:.1f}"
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
