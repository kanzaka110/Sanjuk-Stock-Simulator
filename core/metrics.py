"""
성과 지표 확장 — Jesse-AI 패턴 적용

기본(Sharpe, MDD, 승률)에 더해:
- Sortino ratio (하방 리스크만)
- Calmar ratio (CAGR / MDD)
- CVaR (Expected Shortfall, 최악 5% 시나리오)
- 최대 수중 기간 (가장 긴 낙폭 지속 기간)
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sortino_ratio(
    returns: pd.Series, rf: float = 0.0, annualize: bool = True
) -> float:
    """Sortino 비율 — 하방 변동성만 사용 (Sharpe보다 정확).

    Args:
        returns: 일간 수익률 시리즈
        rf: 무위험 수익률 (일간)
        annualize: 연환산 여부
    """
    excess = returns - rf
    downside = excess[excess < 0]
    if len(downside) < 2 or downside.std() == 0:
        return 0.0
    ratio = float(excess.mean() / downside.std())
    if annualize:
        ratio *= np.sqrt(252)
    return round(ratio, 2)


def calmar_ratio(returns: pd.Series) -> float:
    """Calmar 비율 — CAGR / 최대 낙폭.

    높을수록 리스크 대비 수익 효율이 좋음.
    """
    if len(returns) < 20:
        return 0.0

    cumulative = (1 + returns).cumprod()
    peak = cumulative.cummax()
    drawdown = (cumulative - peak) / peak
    max_dd = float(drawdown.min())

    if max_dd == 0:
        return 0.0

    # CAGR 계산
    total_days = len(returns)
    total_return = float(cumulative.iloc[-1])
    if total_return <= 0:
        return 0.0
    cagr = total_return ** (252 / total_days) - 1

    return round(cagr / abs(max_dd), 2)


def conditional_value_at_risk(
    returns: pd.Series, confidence: float = 0.95
) -> float:
    """CVaR (Expected Shortfall) — 최악 시나리오 평균 손실.

    "최악의 5% 날에 평균 얼마나 잃나?"

    Args:
        returns: 일간 수익률
        confidence: 신뢰 수준 (기본 95%)

    Returns:
        CVaR (음수, 예: -0.032 = 최악 5% 평균 -3.2%)
    """
    if len(returns) < 20:
        return 0.0
    var_threshold = float(np.percentile(returns, (1 - confidence) * 100))
    tail = returns[returns <= var_threshold]
    if len(tail) == 0:
        return var_threshold
    return round(float(tail.mean()), 4)


def max_underwater_period(returns: pd.Series) -> int:
    """최대 수중 기간 — 가장 긴 낙폭 지속 일수.

    "고점 회복까지 최대 몇 거래일 걸렸나?"
    """
    if len(returns) < 5:
        return 0

    cumulative = (1 + returns).cumprod()
    peak = cumulative.cummax()
    underwater = cumulative < peak

    max_period = 0
    current_period = 0
    for is_under in underwater:
        if is_under:
            current_period += 1
            max_period = max(max_period, current_period)
        else:
            current_period = 0

    return max_period


def omega_ratio(returns: pd.Series, threshold: float = 0.0) -> float:
    """Omega 비율 — 기준 수익률 대비 이익/손실 가중 비율.

    1.0 이상이면 기준 대비 이익 > 손실.
    """
    if len(returns) < 10:
        return 0.0
    gains = returns[returns > threshold] - threshold
    losses = threshold - returns[returns <= threshold]

    total_loss = float(losses.sum())
    if total_loss == 0:
        return float("inf") if float(gains.sum()) > 0 else 0.0
    return round(float(gains.sum()) / total_loss, 2)


def compute_all_metrics(returns: pd.Series) -> dict[str, float]:
    """모든 성과 지표를 한번에 계산."""
    if len(returns) < 5:
        return {}

    daily_std = float(returns.std())
    sharpe = float(returns.mean() / daily_std * np.sqrt(252)) if daily_std > 0 else 0.0

    cumulative = (1 + returns).cumprod()
    peak = cumulative.cummax()
    drawdown = (cumulative - peak) / peak
    max_dd = float(drawdown.min())

    total_return = float(cumulative.iloc[-1] - 1) * 100

    return {
        "total_return_pct": round(total_return, 2),
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": sortino_ratio(returns),
        "calmar_ratio": calmar_ratio(returns),
        "omega_ratio": omega_ratio(returns),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "cvar_95": conditional_value_at_risk(returns),
        "max_underwater_days": max_underwater_period(returns),
    }


def metrics_to_text(ticker: str, name: str, metrics: dict[str, float]) -> str:
    """성과 지표를 텍스트로 변환."""
    if not metrics:
        return ""
    cvar = metrics.get("cvar_95", 0)
    return (
        f"  {name}: 수익 {metrics.get('total_return_pct', 0):+.1f}% | "
        f"Sharpe {metrics.get('sharpe_ratio', 0):.2f} | "
        f"Sortino {metrics.get('sortino_ratio', 0):.2f} | "
        f"Calmar {metrics.get('calmar_ratio', 0):.2f} | "
        f"MDD {metrics.get('max_drawdown_pct', 0):.1f}% | "
        f"CVaR(5%) {cvar*100:.1f}% | "
        f"수중 최대 {metrics.get('max_underwater_days', 0)}일"
    )
