"""
데이터 모델 — frozen dataclass로 불변성 보장
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Quote:
    """단일 종목 시세."""

    ticker: str
    name: str
    price: float
    change: float = 0.0
    pct: float = 0.0
    high: float = 0.0
    low: float = 0.0


@dataclass(frozen=True)
class Signal:
    """AI 매매 신호."""

    ticker: str
    name: str
    signal: str  # 매수 | 매도 | 홀딩 | 관망
    reason: str = ""
    entry_price: str = ""
    target_price: str = ""
    stop_loss: str = ""
    urgency: str = ""
    shares: str = ""
    timing: str = ""
    split_plan: str = ""


@dataclass(frozen=True)
class MarketSnapshot:
    """시장 전체 스냅샷."""

    stocks: dict[str, Quote] = field(default_factory=dict)
    indices: dict[str, Quote] = field(default_factory=dict)
    macro: dict[str, Quote] = field(default_factory=dict)
    news: dict[str, list[str]] = field(default_factory=dict)
    timestamp: str = ""


@dataclass(frozen=True)
class BriefingResult:
    """AI 분석 결과."""

    title: str = ""
    market_status: str = "혼조"
    investment_decision: str = "관망"
    market_summary: str = ""
    portfolio_signals: tuple[Signal, ...] = ()
    buy_signals: tuple[Signal, ...] = ()
    sell_signals: tuple[Signal, ...] = ()
    advisor_verdict: str = ""
    advisor_oneliner: str = ""
    advisor_conclusion: str = ""
    strategy_summary: str = ""
    raw_json: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TradeRecord:
    """매매 기록."""

    id: int = 0
    ticker: str = ""
    name: str = ""
    action: str = ""  # buy | sell
    price: float = 0.0
    shares: int = 0
    reason: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class PortfolioPosition:
    """보유 종목 포지션."""

    ticker: str
    name: str
    shares: int
    avg_price: float
    current_price: float = 0.0

    @property
    def total_cost(self) -> float:
        return self.avg_price * self.shares

    @property
    def current_value(self) -> float:
        return self.current_price * self.shares

    @property
    def pnl(self) -> float:
        return self.current_value - self.total_cost

    @property
    def pnl_pct(self) -> float:
        if self.total_cost == 0:
            return 0.0
        return (self.pnl / self.total_cost) * 100
