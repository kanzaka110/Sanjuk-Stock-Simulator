"""
재무 데이터 + 이벤트 캘린더 -- yfinance 기반

가치투자 페르소나가 실질적 분석을 할 수 있도록
EPS, 매출, 영업이익, 실적 발표일 등을 수집한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import yfinance as yf

from config.settings import KST, PORTFOLIO

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FinancialData:
    """종목 재무 데이터."""

    ticker: str
    name: str
    # 밸류에이션
    per: float = 0.0
    pbr: float = 0.0
    psr: float = 0.0
    market_cap: float = 0.0  # 시가총액 (억원/$M)
    # 수익성
    eps: float = 0.0
    revenue: float = 0.0  # 매출 (억원/$M)
    revenue_growth: float = 0.0  # 매출 성장률 (%)
    profit_margin: float = 0.0  # 순이익률 (%)
    operating_margin: float = 0.0  # 영업이익률 (%)
    roe: float = 0.0  # ROE (%)
    # 배당
    dividend_yield: float = 0.0  # 배당수익률 (%)
    # 실적 발표
    earnings_date: str = ""  # 다음 실적 발표일
    days_to_earnings: int = -1  # 실적까지 남은 일수


@dataclass(frozen=True)
class EarningsEvent:
    """실적/경제 이벤트."""

    date: str
    name: str
    event_type: str  # earnings/economic/fed


def fetch_financial_data(ticker: str, name: str = "") -> FinancialData | None:
    """yfinance에서 재무 데이터 수집."""
    try:
        tk = yf.Ticker(ticker)
        info = tk.info or {}

        # 실적 발표일
        earnings_date = ""
        days_to = -1
        try:
            cal = tk.calendar
            if cal is not None:
                if isinstance(cal, dict):
                    ed = cal.get("Earnings Date")
                    if ed:
                        if isinstance(ed, list) and len(ed) > 0:
                            earnings_date = str(ed[0])[:10]
                        else:
                            earnings_date = str(ed)[:10]
                elif hasattr(cal, "iloc"):
                    earnings_date = str(cal.iloc[0, 0])[:10] if len(cal) > 0 else ""

            if earnings_date:
                try:
                    ed_dt = datetime.strptime(earnings_date[:10], "%Y-%m-%d")
                    now = datetime.now()
                    days_to = (ed_dt - now).days
                except ValueError:
                    pass
        except Exception:
            pass

        return FinancialData(
            ticker=ticker,
            name=name,
            per=round(float(info.get("trailingPE", 0) or 0), 1),
            pbr=round(float(info.get("priceToBook", 0) or 0), 2),
            psr=round(float(info.get("priceToSalesTrailing12Months", 0) or 0), 2),
            market_cap=round(float(info.get("marketCap", 0) or 0) / 1e8, 0),
            eps=round(float(info.get("trailingEps", 0) or 0), 2),
            revenue=round(float(info.get("totalRevenue", 0) or 0) / 1e8, 0),
            revenue_growth=round(float(info.get("revenueGrowth", 0) or 0) * 100, 1),
            profit_margin=round(float(info.get("profitMargins", 0) or 0) * 100, 1),
            operating_margin=round(float(info.get("operatingMargins", 0) or 0) * 100, 1),
            roe=round(float(info.get("returnOnEquity", 0) or 0) * 100, 1),
            dividend_yield=round(float(info.get("dividendYield", 0) or 0) * 100, 2),
            earnings_date=earnings_date,
            days_to_earnings=days_to,
        )
    except Exception as e:
        log.warning(f"재무 데이터 조회 실패 ({ticker}): {e}")
        return None


def fetch_all_fundamentals(
    tickers: dict[str, str] | None = None,
) -> list[FinancialData]:
    """포트폴리오 전체 재무 데이터 수집."""
    if tickers is None:
        tickers = PORTFOLIO

    results: list[FinancialData] = []
    for tk, nm in tickers.items():
        fd = fetch_financial_data(tk, nm)
        if fd:
            results.append(fd)
    return results


def fundamentals_to_text(data: list[FinancialData]) -> str:
    """재무 데이터를 텍스트로 변환 (프롬프트 삽입용)."""
    if not data:
        return ""

    lines = ["【재무 데이터 (yfinance)】"]

    # 실적 임박 종목 먼저
    upcoming = [d for d in data if 0 <= d.days_to_earnings <= 7]
    if upcoming:
        lines.append("\n  [!! 실적 발표 임박 !!]")
        for d in upcoming:
            lines.append(
                f"  {d.name}: {d.earnings_date} ({d.days_to_earnings}일 후)"
            )

    lines.append("\n  [밸류에이션]")
    for d in data:
        parts = [d.name]
        if d.per > 0:
            parts.append(f"PER {d.per:.1f}")
        if d.pbr > 0:
            parts.append(f"PBR {d.pbr:.2f}")
        if d.psr > 0:
            parts.append(f"PSR {d.psr:.1f}")
        if d.market_cap > 0:
            parts.append(f"시총 {d.market_cap:,.0f}억")
        lines.append(f"  {' | '.join(parts)}")

    lines.append("\n  [수익성]")
    for d in data:
        if d.eps == 0 and d.revenue == 0:
            continue
        parts = [d.name]
        if d.eps != 0:
            parts.append(f"EPS {d.eps:,.2f}")
        if d.revenue > 0:
            parts.append(f"매출 {d.revenue:,.0f}억")
        if d.revenue_growth != 0:
            parts.append(f"매출성장 {d.revenue_growth:+.1f}%")
        if d.operating_margin != 0:
            parts.append(f"영업이익률 {d.operating_margin:.1f}%")
        if d.roe != 0:
            parts.append(f"ROE {d.roe:.1f}%")
        lines.append(f"  {' | '.join(parts)}")

    return "\n".join(lines)
