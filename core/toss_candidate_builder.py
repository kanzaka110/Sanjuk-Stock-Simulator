"""
Toss 자동거래 후보 생성 — stub

이번 단계에서는 수동/테스트용 후보만 생성.
브리핑 추천 자동 연결 금지. 실시간 주문 트리거 cron 금지.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


def build_manual_candidate(
    symbol: str,
    side: str,
    quantity: int,
    limit_price: float,
    market: str = "",
    reason: str = "",
    confidence: float = 0.5,
    source_signal: str = "manual",
    quote_age_sec: int = 0,
) -> dict:
    """수동 입력 후보 생성. 가드레일 평가 전 단계."""
    return {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "limit_price": limit_price,
        "estimated_amount_krw": round(quantity * limit_price, 2),
        "market": market,
        "reason": reason,
        "confidence": confidence,
        "source_signal": source_signal,
        "quote_age_sec": quote_age_sec,
        "created_at": datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00"),
    }
