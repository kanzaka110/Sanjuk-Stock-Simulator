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
    """fast_info로 실시간 시세 조회. 실패 시 일봉 폴백.

    기존 방식(1분봉 + 5일봉 2회 호출)에서 fast_info 단일 호출로 변경.
    속도 개선 + 가격 갭 해소.
    """
    try:
        t = yf.Ticker(ticker)
        name = PORTFOLIO.get(ticker, INDICES.get(ticker, MACRO.get(ticker, ticker)))
        fi = t.fast_info

        price = float(fi.last_price)
        prev_close = float(fi.regular_market_previous_close or fi.previous_close)

        if price <= 0 or prev_close <= 0:
            return _get_quote_daily(ticker)

        return Quote(
            ticker=ticker,
            name=name,
            price=round(price, 2),
            change=round(price - prev_close, 2),
            pct=round((price - prev_close) / prev_close * 100, 2),
            high=round(float(fi.day_high), 2) if fi.day_high else round(price, 2),
            low=round(float(fi.day_low), 2) if fi.day_low else round(price, 2),
        )
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


def _batch_quotes(ticker_map: dict[str, str]) -> dict[str, Quote]:
    """yf.download 배치로 여러 종목 시세를 한 번에 조회.

    Args:
        ticker_map: {ticker: display_name} 매핑

    Returns:
        {ticker_or_name: Quote} 딕셔너리
    """
    if not ticker_map:
        return {}

    tickers = list(ticker_map.keys())
    results: dict[str, Quote] = {}

    try:
        df = yf.download(
            tickers,
            period="1d",
            interval="1m",
            progress=False,
            threads=True,
        )

        for tk in tickers:
            try:
                if len(tickers) == 1:
                    col = df
                else:
                    col = df[tk] if tk in df.columns.get_level_values(0) else None

                if col is None or col.empty or col["Close"].dropna().empty:
                    # 배치 실패 → 개별 폴백
                    q = _get_quote_realtime(tk)
                    if q:
                        results[tk] = q
                    continue

                close_series = col["Close"].dropna()
                price = round(float(close_series.iloc[-1]), 2)

                # 전일 종가는 fast_info에서
                fi = yf.Ticker(tk).fast_info
                prev_close = float(
                    fi.regular_market_previous_close or fi.previous_close,
                )

                if prev_close <= 0:
                    continue

                name = ticker_map[tk]
                results[tk] = Quote(
                    ticker=tk,
                    name=name,
                    price=price,
                    change=round(price - prev_close, 2),
                    pct=round((price - prev_close) / prev_close * 100, 2),
                    high=round(float(col["High"].max()), 2),
                    low=round(float(col["Low"].min()), 2),
                )
            except Exception:
                q = _get_quote_realtime(tk)
                if q:
                    results[tk] = q
    except Exception:
        # 배치 전체 실패 → 개별 조회 폴백
        for tk in tickers:
            q = _get_quote_realtime(tk)
            if q:
                results[tk] = q

    return results


def fetch_market(briefing_type: str = "MANUAL") -> MarketSnapshot:
    """시장 데이터 수집 — briefing_type에 따라 시장별 필터링.

    배치 다운로드로 속도 최적화 (N개 종목 → 1회 호출).

    Args:
        briefing_type: KR_BEFORE(한국 중심), US_BEFORE(미국 중심), MANUAL(전체)
    """
    from datetime import datetime

    from config.settings import KST, get_market_config

    portfolio, indices_cfg, macro_cfg = get_market_config(briefing_type)

    # 배치로 종목 시세 조회
    stocks = _batch_quotes(portfolio)

    # 지수·매크로는 개별 조회 (종목 수 적음)
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
