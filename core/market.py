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
def _get_quote(ticker: str) -> Quote | None:
    """단일 티커 시세 조회."""
    try:
        h = yf.Ticker(ticker).history(period="5d")
        if len(h) >= 2:
            c = float(h["Close"].iloc[-1])
            p = float(h["Close"].iloc[-2])
            return Quote(
                ticker=ticker,
                name=PORTFOLIO.get(ticker, INDICES.get(ticker, MACRO.get(ticker, ticker))),
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
                name=PORTFOLIO.get(ticker, ticker),
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


def fetch_market() -> MarketSnapshot:
    """전체 시장 데이터 수집 — 포트폴리오 + 지수 + 매크로 + 뉴스."""
    stocks: dict[str, Quote] = {}
    for tk in PORTFOLIO:
        q = _get_quote(tk)
        if q is not None:
            stocks[tk] = q
        time.sleep(0.12)

    indices: dict[str, Quote] = {}
    for tk, nm in INDICES.items():
        q = _get_quote(tk)
        if q is not None:
            indices[nm] = q

    macro: dict[str, Quote] = {}
    for tk, nm in MACRO.items():
        q = _get_quote(tk)
        if q is not None:
            macro[nm] = q

    news: dict[str, list[str]] = {}
    for tk in ["NVDA", "GOOGL", "MU", "LMT", "005930.KS", "012450.KS"]:
        h = _get_ticker_news(tk, 3)
        if h:
            news[PORTFOLIO.get(tk, tk)] = h
        time.sleep(0.15)

    from datetime import datetime

    from config.settings import KST

    return MarketSnapshot(
        stocks=stocks,
        indices=indices,
        macro=macro,
        news=news,
        timestamp=datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
    )
