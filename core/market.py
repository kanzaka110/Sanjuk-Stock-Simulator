"""
시장 데이터 수집 — yfinance 기반
Stock_bot/scripts/briefing.py의 fetch_market() 로직 추출
"""

from __future__ import annotations

import time

import yfinance as yf

from config.settings import (
    INDICES,
    KRW_TICKERS,
    MACRO,
    PORTFOLIO,
)
from core.models import MarketSnapshot, Quote


# ─── 통화 포맷 유틸 ─────────────────────────────────
def fmt_price(ticker: str, price: float) -> str:
    """티커에 따라 ₩ 또는 $ 표기."""
    if ticker in KRW_TICKERS:
        return f"₩{price:,.0f}"
    if ticker == "USDKRW=X":
        return f"₩{price:,.2f}"
    return f"${price:,.2f}"


def fmt_change(ticker: str, change: float) -> str:
    arrow = "▲" if change >= 0 else "▼"
    abs_c = abs(change)
    if ticker in KRW_TICKERS:
        return f"{arrow} ₩{abs_c:,.0f}"
    if ticker == "USDKRW=X":
        return f"{arrow} ₩{abs_c:,.2f}"
    return f"{arrow} ${abs_c:,.2f}"


def pct_bar(pct: float) -> str:
    if pct >= 3:
        return "▲▲▲"
    if pct >= 1:
        return "▲▲"
    if pct >= 0:
        return "▲"
    if pct >= -1:
        return "▼"
    if pct >= -3:
        return "▼▼"
    return "▼▼▼"


def signal_badge(signal: str) -> str:
    badges = {
        "매수": "🟢 매수",
        "매도": "🔴 매도",
        "홀딩": "🔵 홀딩",
        "관망": "⚪ 관망",
        "강력매수": "🔥 강력매수",
        "강력매도": "⛔ 강력매도",
    }
    return badges.get(signal, signal)


# ─── 데이터 수집 ────────────────────────────────────
def _get_quote_realtime(ticker: str) -> Quote | None:
    """장중 1분봉으로 실시간에 가까운 시세 조회. 실패 시 일봉 폴백."""
    try:
        t = yf.Ticker(ticker)
        name = PORTFOLIO.get(ticker, INDICES.get(ticker, MACRO.get(ticker, ticker)))

        # 1분봉 시도 (장중일 때 최신 가격 제공)
        intra = t.history(period="1d", interval="1m")
        if len(intra) >= 2:
            c = float(intra["Close"].iloc[-1])
            day_high = round(float(intra["High"].max()), 2)
            day_low = round(float(intra["Low"].min()), 2)

            # 전일 종가: 일봉에서 가져옴
            daily = t.history(period="5d")
            if len(daily) >= 2:
                prev_close = float(daily["Close"].iloc[-2])
            else:
                prev_close = float(intra["Open"].iloc[0])

            return Quote(
                ticker=ticker,
                name=name,
                price=round(c, 2),
                change=round(c - prev_close, 2),
                pct=round((c - prev_close) / prev_close * 100, 2),
                high=day_high,
                low=day_low,
            )

        # 1분봉 데이터 없음 (장 마감) → 일봉 폴백
        return _get_quote_daily(ticker)
    except Exception:
        return _get_quote_daily(ticker)


def _get_quote_daily(ticker: str) -> Quote | None:
    """일봉 기반 시세 조회 (폴백용)."""
    try:
        h = yf.Ticker(ticker).history(period="5d")
        name = PORTFOLIO.get(ticker, INDICES.get(ticker, MACRO.get(ticker, ticker)))
        if len(h) >= 2:
            c = float(h["Close"].iloc[-1])
            p = float(h["Close"].iloc[-2])
            return Quote(
                ticker=ticker,
                name=name,
                price=round(c, 2),
                change=round(c - p, 2),
                pct=round((c - p) / p * 100, 2),
                high=round(float(h["High"].iloc[-1]), 2),
                low=round(float(h["Low"].iloc[-1]), 2),
            )
        if len(h) == 1:
            c = float(h["Close"].iloc[-1])
            return Quote(
                ticker=ticker,
                name=name,
                price=c,
                high=c,
                low=c,
            )
    except Exception:
        pass
    return None


def _get_ticker_news(ticker: str, n: int = 3) -> list[str]:
    """종목별 최신 뉴스 헤드라인."""
    try:
        items = yf.Ticker(ticker).news or []
        out: list[str] = []
        for i in items[:n]:
            t = (i.get("content", {}).get("title") or i.get("title", "")).strip()
            if t:
                out.append(t)
        return out
    except Exception:
        return []


def fetch_market(briefing_type: str = "MANUAL") -> MarketSnapshot:
    """시장 데이터 수집 — briefing_type에 따라 시장별 필터링.

    Args:
        briefing_type: KR_BEFORE(한국 중심), US_BEFORE(미국 중심), MANUAL(전체)
    """
    from datetime import datetime

    from config.settings import KST, get_market_config

    portfolio, indices_cfg, macro_cfg = get_market_config(briefing_type)

    stocks: dict[str, Quote] = {}
    for tk in portfolio:
        q = _get_quote_realtime(tk)
        if q is not None:
            stocks[tk] = q
        time.sleep(0.12)

    indices: dict[str, Quote] = {}
    for tk, nm in indices_cfg.items():
        q = _get_quote_realtime(tk)
        if q is not None:
            indices[nm] = q

    macro: dict[str, Quote] = {}
    for tk, nm in macro_cfg.items():
        q = _get_quote_realtime(tk)
        if q is not None:
            macro[nm] = q

    # 뉴스: 현재 포트폴리오 종목만
    news: dict[str, list[str]] = {}
    for tk in portfolio:
        h = _get_ticker_news(tk, 3)
        if h:
            news[portfolio.get(tk, tk)] = h
        time.sleep(0.15)

    return MarketSnapshot(
        stocks=stocks,
        indices=indices,
        macro=macro,
        news=news,
        timestamp=datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
    )
