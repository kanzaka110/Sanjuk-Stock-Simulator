"""
포트폴리오 관리 — 보유 종목, 매매 시뮬레이션, 손익 계산
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from config.settings import DEFAULT_CASH, KST
from core.models import PortfolioPosition, TradeRecord
from db.store import (
    get_cash,
    get_positions,
    get_trades,
    save_cash,
    save_position,
    save_trade,
    delete_position,
)


def get_portfolio_summary() -> tuple[float, list[PortfolioPosition]]:
    """현재 예수금 + 보유 포지션 반환."""
    cash = get_cash()
    positions = get_positions()
    return cash, positions


def execute_buy(
    ticker: str,
    name: str,
    price: float,
    shares: int,
    reason: str = "",
) -> TradeRecord:
    """매수 시뮬레이션 실행.

    Raises:
        ValueError: 예수금 부족
    """
    total_cost = price * shares
    cash = get_cash()

    if total_cost > cash:
        raise ValueError(
            f"예수금 부족: 필요 ₩{total_cost:,.0f}, 보유 ₩{cash:,.0f}"
        )

    # 예수금 차감
    new_cash = cash - total_cost
    save_cash(new_cash)

    # 기존 포지션 업데이트 또는 신규 생성
    positions = get_positions()
    existing = next((p for p in positions if p.ticker == ticker), None)

    if existing is not None:
        new_shares = existing.shares + shares
        new_avg = (existing.avg_price * existing.shares + price * shares) / new_shares
        save_position(
            PortfolioPosition(
                ticker=ticker,
                name=name,
                shares=new_shares,
                avg_price=round(new_avg, 2),
            )
        )
    else:
        save_position(
            PortfolioPosition(
                ticker=ticker,
                name=name,
                shares=shares,
                avg_price=price,
            )
        )

    # 매매 기록 저장
    record = TradeRecord(
        ticker=ticker,
        name=name,
        action="buy",
        price=price,
        shares=shares,
        reason=reason,
        created_at=datetime.now(KST).isoformat(),
    )
    save_trade(record)
    return record


def execute_sell(
    ticker: str,
    name: str,
    price: float,
    shares: int,
    reason: str = "",
) -> TradeRecord:
    """매도 시뮬레이션 실행.

    Raises:
        ValueError: 보유 수량 부족
    """
    positions = get_positions()
    existing = next((p for p in positions if p.ticker == ticker), None)

    if existing is None or existing.shares < shares:
        held = existing.shares if existing else 0
        raise ValueError(
            f"보유 수량 부족: 매도 {shares}주, 보유 {held}주"
        )

    # 예수금 증가
    total_value = price * shares
    cash = get_cash()
    save_cash(cash + total_value)

    # 포지션 업데이트
    remaining = existing.shares - shares
    if remaining == 0:
        delete_position(ticker)
    else:
        save_position(
            PortfolioPosition(
                ticker=ticker,
                name=name,
                shares=remaining,
                avg_price=existing.avg_price,
            )
        )

    record = TradeRecord(
        ticker=ticker,
        name=name,
        action="sell",
        price=price,
        shares=shares,
        reason=reason,
        created_at=datetime.now(KST).isoformat(),
    )
    save_trade(record)
    return record


def get_trade_history(limit: int = 20) -> list[TradeRecord]:
    """최근 매매 기록 조회."""
    return get_trades(limit)
