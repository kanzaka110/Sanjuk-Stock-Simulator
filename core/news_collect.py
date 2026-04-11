"""
뉴스 수집 — 3-Layer 교차 검증 시스템

Layer 1: RSS (한경/매경/연합) → 시장 전체 흐름
Layer 2: 네이버 금융 API → 한국 종목별 뉴스
Layer 3: yfinance 내장 뉴스 → 해외 종목별 뉴스

Claude WebSearch가 Layer 4로 심층 검증 수행 (커맨드 지침에서 처리).
"""

from __future__ import annotations

import json
import logging
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Literal

import yfinance as yf

from config.settings import KR_PORTFOLIO, US_PORTFOLIO, PORTFOLIO

log = logging.getLogger(__name__)

# ─── RSS 피드 소스 ──────────────────────────────────────
RSS_FEEDS_KR: dict[str, str] = {
    "한경 증권": "https://www.hankyung.com/feed/finance",
    "한경 글로벌마켓": "https://www.hankyung.com/feed/international",
    "매경 증권": "https://www.mk.co.kr/rss/30100041/",
    "연합뉴스 경제": "https://www.yna.co.kr/rss/economy.xml",
}

RSS_FEEDS_US: dict[str, str] = {
    "한경 글로벌마켓": "https://www.hankyung.com/feed/international",
    "한경 증권": "https://www.hankyung.com/feed/finance",
}

# ─── 키워드 ─────────────────────────────────────────────
KEYWORDS_KR: list[str] = [
    "삼성전자", "한화에어로", "코스피", "코스닥", "나스닥", "S&P",
    "반도체", "방산", "ETF", "외국인", "기관", "환율", "원달러",
    "금리", "FOMC", "실적", "배당", "AI", "HBM",
]

KEYWORDS_US: list[str] = [
    "엔비디아", "NVDA", "구글", "GOOGL", "마이크론", "MU",
    "록히드", "LMT", "나스닥", "S&P", "반도체", "AI",
    "Fed", "FOMC", "금리", "VIX", "국채", "환율",
    "방산", "빅테크", "실적", "매그니피센트",
]

# ─── 네이버 금융 종목코드 매핑 ──────────────────────────
NAVER_CODES: dict[str, str] = {
    "005930.KS": "005930",
    "012450.KS": "012450",
}


@dataclass(frozen=True)
class NewsItem:
    """단일 뉴스 아이템."""

    title: str
    source: str  # 언론사 또는 피드 이름
    layer: Literal["rss", "naver", "yfinance"]
    keywords: tuple[str, ...] = ()
    ticker: str = ""
    url: str = ""


@dataclass
class NewsReport:
    """수집된 전체 뉴스 리포트."""

    rss_items: list[NewsItem] = field(default_factory=list)
    naver_items: list[NewsItem] = field(default_factory=list)
    yfinance_items: list[NewsItem] = field(default_factory=list)

    @property
    def total_count(self) -> int:
        return len(self.rss_items) + len(self.naver_items) + len(self.yfinance_items)

    def to_text(self) -> str:
        lines: list[str] = []

        if self.rss_items:
            lines.append("【RSS 시장 뉴스】")
            for item in self.rss_items:
                kw = f" [{', '.join(item.keywords)}]" if item.keywords else ""
                lines.append(f"  [{item.source}]{kw} {item.title}")
            lines.append("")

        if self.naver_items:
            lines.append("【네이버 금융 종목뉴스】")
            for item in self.naver_items:
                tag = f" ({item.ticker})" if item.ticker else ""
                lines.append(f"  [{item.source}]{tag} {item.title}")
            lines.append("")

        if self.yfinance_items:
            lines.append("【해외 종목뉴스 (yfinance)】")
            for item in self.yfinance_items:
                tag = f" ({item.ticker})" if item.ticker else ""
                lines.append(f"  [{item.source}]{tag} {item.title}")
            lines.append("")

        if not lines:
            return "(뉴스 수집 결과 없음)"

        lines.append(f"--- 총 {self.total_count}건 수집 완료 ---")
        return "\n".join(lines)


# ─── Layer 1: RSS 수집 ──────────────────────────────────


def _fetch_rss(
    feeds: dict[str, str],
    keywords: list[str],
    max_per_feed: int = 50,
) -> list[NewsItem]:
    """RSS 피드에서 키워드 매칭 뉴스를 수집한다."""
    results: list[NewsItem] = []
    headers = {"User-Agent": "Mozilla/5.0"}

    for feed_name, url in feeds.items():
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
            root = ET.fromstring(data)
            items = root.findall(".//item")[:max_per_feed]

            for item in items:
                title = (item.findtext("title") or "").strip()
                desc = (item.findtext("description") or "").strip()
                link = (item.findtext("link") or "").strip()
                text = title + " " + desc

                hits = tuple(kw for kw in keywords if kw in text)
                if hits:
                    results.append(
                        NewsItem(
                            title=title[:100],
                            source=feed_name,
                            layer="rss",
                            keywords=hits,
                            url=link,
                        )
                    )
        except Exception as e:
            log.warning("RSS 수집 실패 [%s]: %s", feed_name, e)

    # 중복 제목 제거
    seen: set[str] = set()
    unique: list[NewsItem] = []
    for item in results:
        key = item.title[:40]
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


# ─── Layer 2: 네이버 금융 API ───────────────────────────


def _fetch_naver_stock_news(
    ticker_codes: dict[str, str],
    max_per_stock: int = 5,
) -> list[NewsItem]:
    """네이버 금융 모바일 API에서 종목별 뉴스를 수집한다."""
    results: list[NewsItem] = []
    headers = {"User-Agent": "Mozilla/5.0"}

    for ticker, code in ticker_codes.items():
        url = f"https://m.stock.naver.com/api/news/stock/{code}?pageSize={max_per_stock}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            # 응답이 카테고리별 리스트
            all_items: list[dict] = []
            if isinstance(data, list):
                for cat in data:
                    if isinstance(cat, dict):
                        all_items.extend(cat.get("items", []))
            elif isinstance(data, dict):
                all_items = data.get("items", [])

            name = PORTFOLIO.get(ticker, ticker)
            for item in all_items[:max_per_stock]:
                title = item.get("title", "").strip()
                office = item.get("officeName", "")
                article_id = item.get("articleId", "")
                office_id = item.get("officeId", "")
                link = (
                    f"https://n.news.naver.com/mnews/article/{office_id}/{article_id}"
                    if office_id and article_id
                    else ""
                )
                if title:
                    results.append(
                        NewsItem(
                            title=title[:100],
                            source=office,
                            layer="naver",
                            ticker=name,
                            url=link,
                        )
                    )
        except Exception as e:
            log.warning("네이버 뉴스 수집 실패 [%s]: %s", ticker, e)

    return results


# ─── Layer 3: yfinance 내장 뉴스 ────────────────────────


def _fetch_yfinance_news(
    tickers: dict[str, str],
    max_per_stock: int = 3,
) -> list[NewsItem]:
    """yfinance의 내장 뉴스 기능으로 해외 종목 뉴스를 수집한다."""
    results: list[NewsItem] = []

    for ticker, name in tickers.items():
        try:
            t = yf.Ticker(ticker)
            news = t.news if hasattr(t, "news") else []
            for item in news[:max_per_stock]:
                # yfinance 버전에 따라 구조가 다름
                if isinstance(item, dict):
                    title = item.get(
                        "title",
                        item.get("content", {}).get("title", "N/A"),
                    )
                    provider = item.get(
                        "publisher",
                        item.get("content", {}).get("provider", {}).get(
                            "displayName", ""
                        ),
                    )
                    link = item.get(
                        "link",
                        item.get("content", {}).get("canonicalUrl", {}).get(
                            "url", ""
                        ),
                    )
                else:
                    continue

                if title and title != "N/A":
                    results.append(
                        NewsItem(
                            title=title[:100],
                            source=provider or "yfinance",
                            layer="yfinance",
                            ticker=name,
                            url=link,
                        )
                    )
        except Exception as e:
            log.warning("yfinance 뉴스 수집 실패 [%s]: %s", ticker, e)

    return results


# ─── 통합 수집 함수 ─────────────────────────────────────


def collect_news_kr() -> NewsReport:
    """한국장 브리핑용 뉴스 수집 (RSS + 네이버 + yfinance)."""
    report = NewsReport()

    # Layer 1: RSS — 한국 시장 전체 흐름
    report.rss_items = _fetch_rss(RSS_FEEDS_KR, KEYWORDS_KR)

    # Layer 2: 네이버 금융 — 한국 보유 종목 뉴스
    kr_codes = {tk: tk.replace(".KS", "") for tk in KR_PORTFOLIO}
    report.naver_items = _fetch_naver_stock_news(kr_codes, max_per_stock=5)

    # Layer 3: yfinance — 미국 보유 종목 뉴스 (한국장에도 영향)
    report.yfinance_items = _fetch_yfinance_news(US_PORTFOLIO, max_per_stock=2)

    return report


def collect_news_us() -> NewsReport:
    """미국장 브리핑용 뉴스 수집 (RSS + 네이버 + yfinance)."""
    report = NewsReport()

    # Layer 1: RSS — 글로벌 마켓 흐름
    report.rss_items = _fetch_rss(RSS_FEEDS_US, KEYWORDS_US)

    # Layer 2: 네이버 금융 — 한국 종목 뉴스 (교차 영향)
    kr_codes = {tk: tk.replace(".KS", "") for tk in KR_PORTFOLIO}
    report.naver_items = _fetch_naver_stock_news(kr_codes, max_per_stock=3)

    # Layer 3: yfinance — 미국 보유 종목 뉴스 (메인)
    report.yfinance_items = _fetch_yfinance_news(US_PORTFOLIO, max_per_stock=5)

    return report
