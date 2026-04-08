"""
시장 데이터 수집 — KIS API (국내) + yfinance (해외/폴백)

폴백 체인 + 서킷 브레이커 + 리트라이 적용 (claw-code 패턴).
"""

from __future__ import annotations

import logging
import time

import yfinance as yf

from config.settings import (
    INDICES,
    KRW_TICKERS,
    MACRO,
    PORTFOLIO,
)
from core.models import MarketSnapshot, Quote
from core.recovery import (
    fallback_chain,
    kis_breaker,
    retry,
    yfinance_breaker,
)

logger = logging.getLogger(__name__)


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
def _get_quote_extended(ticker: str) -> Quote | None:
    """yfinance info로 시간외(프리/애프터마켓) 가격 조회.

    미국 정규장 마감 후 시간외 가격이 있으면 반환.
    """
    if ticker in KRW_TICKERS or ticker.startswith("^") or "=" in ticker:
        return None

    if not yfinance_breaker.is_available:
        return None

    try:
        info = yf.Ticker(ticker).info
        regular = float(info.get("regularMarketPrice", 0))

        # 애프터마켓 → 프리마켓 순서로 확인
        ext_price = info.get("postMarketPrice") or info.get("preMarketPrice")
        if not ext_price or float(ext_price) <= 0:
            return None

        ext_price = float(ext_price)

        # 시간외 가격이 정규장과 같으면 의미 없음
        if abs(ext_price - regular) < 0.01:
            return None

        change = ext_price - regular
        pct = (change / regular * 100) if regular > 0 else 0.0
        name = PORTFOLIO.get(ticker, ticker)

        yfinance_breaker.record_success()
        return Quote(
            ticker=ticker,
            name=name,
            price=round(ext_price, 2),
            change=round(change, 2),
            pct=round(pct, 2),
            high=round(ext_price, 2),
            low=round(ext_price, 2),
        )
    except Exception:
        yfinance_breaker.record_failure()
        return None


def _get_quote_kis(ticker: str) -> Quote | None:
    """KIS API로 시세 조회 (국내 + 해외). 실패 시 None."""
    if not kis_breaker.is_available:
        return None

    try:
        if ticker in KRW_TICKERS:
            from core.market_kis import get_domestic_price

            result = get_domestic_price(ticker)
            if result:
                kis_breaker.record_success()
            return result

        # 해외 종목 (지수/매크로 제외)
        if not ticker.startswith("^") and "=" not in ticker:
            from core.market_kis import get_overseas_price

            result = get_overseas_price(ticker)
            if result:
                kis_breaker.record_success()
            return result
    except Exception as e:
        kis_breaker.record_failure()
        logger.debug("KIS 시세 실패 [%s]: %s", ticker, e)
    return None


def _get_quote_yf_live(ticker: str) -> Quote | None:
    """yfinance fast_info 실시간 시세 조회."""
    if not yfinance_breaker.is_available:
        return None

    try:
        t = yf.Ticker(ticker)
        name = PORTFOLIO.get(ticker, INDICES.get(ticker, MACRO.get(ticker, ticker)))
        fi = t.fast_info

        price = float(fi.last_price)
        prev_close = float(fi.regular_market_previous_close or fi.previous_close)

        if price <= 0 or prev_close <= 0:
            return None

        yfinance_breaker.record_success()
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
        yfinance_breaker.record_failure()
        return None


def _get_quote_daily(ticker: str) -> Quote | None:
    """일봉 기반 시세 조회 (최종 폴백)."""
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


def _get_quote_realtime(ticker: str) -> Quote | None:
    """시세 조회 — 구조화된 폴백 체인.

    해외: 시간외 → KIS → yfinance live → 일봉
    국내: KIS → yfinance live → 일봉
    지수/매크로: yfinance live → 일봉
    """
    is_us = ticker not in KRW_TICKERS and not ticker.startswith("^") and "=" not in ticker
    is_kr = ticker in KRW_TICKERS

    steps: list[tuple[str, object]] = []

    if is_us:
        steps.append(("시간외", lambda t=ticker: _get_quote_extended(t)))
    if is_kr or is_us:
        steps.append(("KIS", lambda t=ticker: _get_quote_kis(t)))
    steps.append(("yfinance_live", lambda t=ticker: _get_quote_yf_live(t)))
    steps.append(("yfinance_daily", lambda t=ticker: _get_quote_daily(t)))

    result = fallback_chain(steps, ticker=ticker)
    return result.value


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
    """KIS (국내) + yf.download (해외) 배치로 시세 조회.

    Args:
        ticker_map: {ticker: display_name} 매핑

    Returns:
        {ticker_or_name: Quote} 딕셔너리
    """
    if not ticker_map:
        return {}

    tickers = list(ticker_map.keys())
    results: dict[str, Quote] = {}

    # 해외 종목: 시간외 가격 우선 확인
    us_tickers = [
        tk for tk in tickers
        if tk not in KRW_TICKERS and not tk.startswith("^") and "=" not in tk
    ]
    for tk in us_tickers:
        ext = _get_quote_extended(tk)
        if ext is not None:
            results[tk] = ext

    # KIS 배치 조회 (국내 + 시간외 미조회 해외)
    kr_tickers = [tk for tk in tickers if tk in KRW_TICKERS]
    us_tickers = [tk for tk in us_tickers if tk not in results]

    if kr_tickers and kis_breaker.is_available:
        try:
            from core.market_kis import get_domestic_prices

            kis_kr = get_domestic_prices(kr_tickers)
            results.update(kis_kr)
            kis_breaker.record_success()
            logger.info("KIS 국내 배치: %d/%d 성공", len(kis_kr), len(kr_tickers))
        except Exception as e:
            kis_breaker.record_failure()
            logger.warning("KIS 국내 배치 실패, yfinance 폴백: %s", e)

    if us_tickers and kis_breaker.is_available:
        try:
            from core.market_kis import get_overseas_prices

            kis_us = get_overseas_prices(us_tickers)
            results.update(kis_us)
            kis_breaker.record_success()
            logger.info("KIS 해외 배치: %d/%d 성공", len(kis_us), len(us_tickers))
        except Exception as e:
            kis_breaker.record_failure()
            logger.warning("KIS 해외 배치 실패, yfinance 폴백: %s", e)

    # KIS에서 조회 실패한 국내 + 해외 종목은 yfinance로
    remaining = [tk for tk in tickers if tk not in results]
    if not remaining:
        return results

    ticker_map_remaining = {tk: ticker_map[tk] for tk in remaining}

    try:
        df = yf.download(
            remaining,
            period="1d",
            interval="1m",
            progress=False,
            threads=True,
        )

        for tk in remaining:
            try:
                if len(remaining) == 1:
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
        for tk in remaining:
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
