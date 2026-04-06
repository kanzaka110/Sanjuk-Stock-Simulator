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


# ═══════════════════════════════════════════════════════
# 실현 가능 매매 사전 필터링
# ═══════════════════════════════════════════════════════
def compute_allowed_actions(
    current_prices: dict[str, float],
) -> dict[str, dict]:
    """종목별 실현 가능한 매매 액션 계산.

    AI 종합 판단에 전달하여 불가능한 추천을 사전 차단.

    Returns:
        {ticker: {
            "can_buy": bool,
            "max_buy_shares": int,
            "max_buy_value": float,
            "can_sell": bool,
            "held_shares": int,
            "held_avg_price": float,
            "unrealized_pnl_pct": float,
        }}
    """
    cash, positions = get_portfolio_summary()
    held_map = {p.ticker: p for p in positions}

    result: dict[str, dict] = {}
    for ticker, price in current_prices.items():
        if price <= 0:
            continue

        pos = held_map.get(ticker)
        held_shares = pos.shares if pos else 0
        avg_price = pos.avg_price if pos else 0
        unrealized = ((price - avg_price) / avg_price * 100) if avg_price > 0 else 0

        max_buy_shares = int(cash / price) if price > 0 else 0

        result[ticker] = {
            "can_buy": max_buy_shares > 0,
            "max_buy_shares": max_buy_shares,
            "max_buy_value": round(cash, 0),
            "can_sell": held_shares > 0,
            "held_shares": held_shares,
            "held_avg_price": round(avg_price, 2),
            "unrealized_pnl_pct": round(unrealized, 2),
        }

    result["_cash"] = {"available": round(cash, 0)}
    return result


def constraints_to_text(
    constraints: dict[str, dict],
    ticker_names: dict[str, str],
) -> str:
    """매매 제약을 텍스트로 변환 (프롬프트 삽입용)."""
    cash = constraints.get("_cash", {}).get("available", 0)
    lines = [f"【매매 제약 조건】 예수금: ₩{cash:,.0f}"]

    for tk, info in constraints.items():
        if tk == "_cash":
            continue
        name = ticker_names.get(tk, tk)
        parts = [name]

        if info.get("can_sell"):
            parts.append(
                f"보유 {info['held_shares']}주 "
                f"(평단 {info['held_avg_price']:,.0f}, "
                f"수익 {info['unrealized_pnl_pct']:+.1f}%)"
            )
        else:
            parts.append("미보유")

        if info.get("can_buy"):
            parts.append(f"최대 매수 {info['max_buy_shares']}주 가능")
        else:
            parts.append("매수 불가 (예수금 부족)")

        lines.append(f"  {' | '.join(parts)}")

    return "\n".join(lines)
