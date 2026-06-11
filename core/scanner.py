"""
시장 기회 스캐너 — 워치리스트 밖 급등/주도 종목 탐지

배경: SNDK가 6개월 +580% 오르는 동안 워치리스트(고정 11종목)만 보던
시스템은 전혀 감지하지 못함 (2026-06-11). 설정된 유니버스(미국 ~110 +
한국 ~55 유동성 상위)를 매 브리핑마다 스캔하여 다음을 포착한다:

  1. 모멘텀 리더 — 60일 수익률 상위
  2. 단기 급등 — 20일 수익률 상위
  3. 거래량 급증 — 5일 평균 거래량이 60일 평균의 2배+ (가격 상승 동반)
  4. 52주 신고가 근접 — 고점 3% 이내 (돌파 추세)
  5. 과매도 반전 후보 — RSI 32 이하 + 직전 반등 캔들

전부 yfinance 일괄 다운로드 — API 비용 $0.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanHit:
    """스캔 결과 한 종목."""

    ticker: str
    name: str
    price: float
    ret_20d: float  # 20일 수익률 %
    ret_60d: float  # 60일 수익률 %
    rsi: float
    vol_surge: float  # 5일 평균 거래량 / 60일 평균 거래량
    pct_from_52w_high: float  # 52주(데이터 범위) 고점 대비 % (음수)
    tags: tuple[str, ...] = ()  # 모멘텀리더/급등/거래량급증/신고가/과매도반전
    is_held: bool = False
    in_watchlist: bool = False


@dataclass(frozen=True)
class ScanReport:
    """시장 스캔 종합 결과."""

    market: str  # KR / US
    hits: tuple[ScanHit, ...] = ()
    scanned: int = 0
    failed: int = 0

    def to_text(self) -> str:
        if not self.hits:
            return f"【시장 스캐너 — {self.market}】 특이 종목 없음 (스캔 {self.scanned}종목)"
        lines = [
            f"【시장 스캐너 — {self.market}】 유니버스 {self.scanned}종목 스캔, 시그널 {len(self.hits)}건",
            "  (워치리스트 밖 시장 주도주/기회 탐지 — 신규 매수 후보 검토 대상)",
        ]
        unit = "₩" if self.market == "KR" else "$"
        for h in self.hits:
            flags = []
            if h.is_held:
                flags.append("보유중")
            if h.in_watchlist:
                flags.append("WL")
            flag_str = f" ({','.join(flags)})" if flags else ""
            lines.append(
                f"  [{'/'.join(h.tags)}] {h.name}({h.ticker}){flag_str}: "
                f"{unit}{h.price:,.0f} | 20일 {h.ret_20d:+.1f}% | 60일 {h.ret_60d:+.1f}% | "
                f"RSI {h.rsi:.0f} | 거래량 x{h.vol_surge:.1f} | 고점대비 {h.pct_from_52w_high:+.1f}%"
            )
        return "\n".join(lines)


def _held_and_watchlist() -> tuple[set[str], set[str]]:
    from config.settings import (
        HOLDINGS_GENERAL,
        HOLDINGS_IRP,
        HOLDINGS_ISA,
        HOLDINGS_PENSION,
        HOLDINGS_RIA,
        WATCHLIST,
    )

    held: set[str] = set()
    for h in (HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA, HOLDINGS_IRP, HOLDINGS_PENSION):
        held.update(h.keys())
    return held, set(WATCHLIST.keys())


def scan_market(market: str = "US", top_n: int = 6) -> ScanReport:
    """유니버스 스캔 실행.

    Args:
        market: "US" 또는 "KR"
        top_n: 카테고리별 상위 표시 수

    Returns:
        ScanReport (실패 시 빈 hits)
    """
    from config.settings import SCAN_UNIVERSE_KR, SCAN_UNIVERSE_US

    universe = SCAN_UNIVERSE_KR if market == "KR" else SCAN_UNIVERSE_US
    if not universe:
        return ScanReport(market=market)

    try:
        import pandas as pd
        import yfinance as yf

        from core.indicators import compute_rsi

        tickers = list(universe.keys())
        data = yf.download(
            tickers, period="1y", interval="1d",
            group_by="ticker", progress=False, threads=True,
        )
    except Exception as e:
        log.warning("시장 스캔 다운로드 실패 (%s): %s", market, e)
        return ScanReport(market=market)

    held, watchlist = _held_and_watchlist()

    rows: list[dict] = []
    failed = 0
    for tk in tickers:
        try:
            df = data[tk] if len(tickers) > 1 else data
            close = df["Close"].dropna()
            volume = df["Volume"].dropna()
            if len(close) < 70:
                failed += 1
                continue

            cur = float(close.iloc[-1])
            ret_20 = (cur / float(close.iloc[-21]) - 1) * 100
            ret_60 = (cur / float(close.iloc[-61]) - 1) * 100
            rsi = compute_rsi(close)
            vol_5 = float(volume.tail(5).mean())
            vol_60 = float(volume.tail(60).mean())
            vol_surge = vol_5 / vol_60 if vol_60 > 0 else 1.0
            high_52w = float(close.max())
            from_high = (cur / high_52w - 1) * 100
            # 직전 반등 캔들 (과매도 반전 판정용)
            bounced = len(close) >= 2 and cur > float(close.iloc[-2])

            rows.append({
                "ticker": tk, "name": universe[tk], "price": cur,
                "ret_20d": ret_20, "ret_60d": ret_60, "rsi": rsi,
                "vol_surge": vol_surge, "from_high": from_high,
                "bounced": bounced,
            })
        except Exception:
            failed += 1
            continue

    if not rows:
        return ScanReport(market=market, scanned=len(tickers), failed=failed)

    # 카테고리별 태깅
    tags: dict[str, list[str]] = {r["ticker"]: [] for r in rows}

    for r in sorted(rows, key=lambda x: x["ret_60d"], reverse=True)[:top_n]:
        if r["ret_60d"] >= 25:
            tags[r["ticker"]].append("모멘텀리더")
    for r in sorted(rows, key=lambda x: x["ret_20d"], reverse=True)[:top_n]:
        if r["ret_20d"] >= 12:
            tags[r["ticker"]].append("급등")
    for r in rows:
        if r["vol_surge"] >= 2.0 and r["ret_20d"] > 0:
            tags[r["ticker"]].append("거래량급증")
        if r["from_high"] >= -3.0 and r["ret_60d"] > 0:
            tags[r["ticker"]].append("신고가권")
        if r["rsi"] <= 32 and r["bounced"]:
            tags[r["ticker"]].append("과매도반전")

    hits = [
        ScanHit(
            ticker=r["ticker"], name=r["name"], price=r["price"],
            ret_20d=round(r["ret_20d"], 1), ret_60d=round(r["ret_60d"], 1),
            rsi=round(r["rsi"], 0), vol_surge=round(r["vol_surge"], 1),
            pct_from_52w_high=round(r["from_high"], 1),
            tags=tuple(dict.fromkeys(tags[r["ticker"]])),
            is_held=r["ticker"] in held,
            in_watchlist=r["ticker"] in watchlist,
        )
        for r in rows
        if tags[r["ticker"]]
    ]
    # 60일 수익률 순 정렬, 과도한 출력 방지
    hits.sort(key=lambda h: h.ret_60d, reverse=True)
    hits = hits[: top_n * 3]

    log.info("시장 스캔 (%s): %d종목 중 시그널 %d건", market, len(tickers), len(hits))
    return ScanReport(market=market, hits=tuple(hits), scanned=len(tickers), failed=failed)


# ═══════════════════════════════════════════════════════
# 전시장 발굴 (Discovery) — 유니버스 밖 전체 시장 스크리닝
# ═══════════════════════════════════════════════════════
# 유니버스 스캔 = "아는 종목 정밀 감시", 발굴 = "모르는 종목 포착".
# 미국: Yahoo 전체 시장 스크리너 (~5,000종목 대상)
# 한국: 네이버 금융 상승률/거래량 상위 (KOSPI+KOSDAQ 전체 ~2,700종목)

_MIN_US_MCAP = 2_000_000_000  # $2B 미만 소형주 제외 (정보 비대칭/변동성)
_MIN_US_PRICE = 5.0  # 페니스톡 제외
_MIN_KR_PRICE = 3_000  # 동전주 제외
_MIN_KR_VALUE_KRW = 30_000_000_000  # 거래대금 300억+ (유동성)


@dataclass(frozen=True)
class DiscoveryHit:
    """전시장 발굴 종목."""

    ticker: str
    name: str
    price: float
    change_pct: float  # 당일 등락률
    volume_value: float  # 거래대금 (또는 시총, 시장별)
    source: str  # 급등상위/거래량상위
    ret_60d: float = 0.0  # 보강 조회 시
    rsi: float = 0.0


def discover_us(top_n: int = 10) -> list[DiscoveryHit]:
    """미국 전체 시장 스크리닝 — Yahoo day_gainers + most_actives.

    시총 $2B+, 주가 $5+ 필터. 실패 시 빈 리스트.
    """
    import yfinance as yf

    hits: dict[str, DiscoveryHit] = {}
    for screener, label in (("day_gainers", "급등상위"), ("most_actives", "거래량상위")):
        try:
            result = yf.screen(screener, count=25)
            for q in result.get("quotes", []):
                sym = q.get("symbol", "")
                mcap = q.get("marketCap") or 0
                price = q.get("regularMarketPrice") or 0
                if not sym or mcap < _MIN_US_MCAP or price < _MIN_US_PRICE:
                    continue
                if sym in hits:
                    continue
                hits[sym] = DiscoveryHit(
                    ticker=sym,
                    name=(q.get("shortName") or sym)[:24],
                    price=float(price),
                    change_pct=round(float(q.get("regularMarketChangePercent") or 0), 1),
                    volume_value=float(mcap),
                    source=label,
                )
        except Exception as e:
            log.warning("US 발굴 스크리너 실패 (%s): %s", screener, e)

    # 급등 위주 정렬, 상위만
    out = sorted(hits.values(), key=lambda h: h.change_pct, reverse=True)[: top_n * 2]
    return _enrich_momentum(out)


def discover_kr(top_n: int = 10) -> list[DiscoveryHit]:
    """한국 전체 시장 스크리닝 — 네이버 금융 상승률 상위 (KOSPI+KOSDAQ).

    주가 3,000원+, 거래대금 300억+ 필터. 실패 시 빈 리스트.
    """
    import io

    import pandas as pd
    import requests

    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"}
    hits: list[DiscoveryHit] = []

    for sosok, suffix in (("0", ".KS"), ("1", ".KQ")):
        try:
            r = requests.get(
                f"https://finance.naver.com/sise/sise_rise.naver?sosok={sosok}",
                headers=headers, timeout=10,
            )
            r.encoding = "euc-kr"
            import re

            code_map = dict(re.findall(
                r'<a href="/item/main\.naver\?code=(\d{6})"[^>]*>([^<]+)</a>', r.text,
            ))
            name_to_code = {v: k for k, v in code_map.items()}

            tables = pd.read_html(io.StringIO(r.text))
            df = max(tables, key=len).dropna(subset=["종목명"])
            for _, row in df.head(60).iterrows():
                try:
                    name = str(row["종목명"]).strip()
                    code = name_to_code.get(name)
                    if not code:
                        continue
                    price = float(row["현재가"])
                    pct = float(str(row["등락률"]).replace("%", "").replace("+", ""))
                    volume = float(row["거래량"])
                    value = price * volume  # 거래대금 근사
                    if price < _MIN_KR_PRICE or value < _MIN_KR_VALUE_KRW:
                        continue
                    hits.append(DiscoveryHit(
                        ticker=f"{code}{suffix}",
                        name=name[:24],
                        price=price,
                        change_pct=round(pct, 1),
                        volume_value=value,
                        source="급등상위",
                    ))
                except (ValueError, KeyError, TypeError):
                    continue
        except Exception as e:
            log.warning("KR 발굴 스크리너 실패 (sosok=%s): %s", sosok, e)

    out = sorted(hits, key=lambda h: h.change_pct, reverse=True)[: top_n * 2]
    return _enrich_momentum(out)


def _enrich_momentum(hits: list[DiscoveryHit]) -> list[DiscoveryHit]:
    """발굴 종목에 60일 모멘텀 + RSI 보강 (yfinance 일괄)."""
    if not hits:
        return []
    try:
        import yfinance as yf
        from dataclasses import replace

        from core.indicators import compute_rsi

        tickers = [h.ticker for h in hits]
        data = yf.download(
            tickers, period="6mo", interval="1d",
            group_by="ticker", progress=False, threads=True,
        )
        enriched = []
        for h in hits:
            try:
                df = data[h.ticker] if len(tickers) > 1 else data
                close = df["Close"].dropna()
                if len(close) < 61:
                    enriched.append(h)
                    continue
                ret_60 = (float(close.iloc[-1]) / float(close.iloc[-61]) - 1) * 100
                enriched.append(replace(h, ret_60d=round(ret_60, 1), rsi=round(compute_rsi(close), 0)))
            except Exception:
                enriched.append(h)
        return enriched
    except Exception as e:
        log.warning("발굴 모멘텀 보강 실패: %s", e)
        return hits


def _fetch_fundamentals_brief(tickers: list[str], max_workers: int = 8) -> dict[str, str]:
    """발굴 종목 펀더멘털 요약 일괄 조회 (섹터/시총/PER/매출성장/사업 한줄).

    AI가 '무슨 회사인지' 데이터 기반으로 설명하기 위한 보강 —
    할루시네이션 방지가 목적. 실패 종목은 누락 (조회 안 됨 표기).
    """
    from concurrent.futures import ThreadPoolExecutor

    import yfinance as yf

    def _one(tk: str) -> tuple[str, str]:
        try:
            info = yf.Ticker(tk).info
            sector = info.get("sector") or "?"
            industry = info.get("industry") or "?"
            mcap = info.get("marketCap") or 0
            if mcap >= 1e12:
                mcap_str = f"{mcap/1e12:.1f}조" if tk.endswith((".KS", ".KQ")) else f"${mcap/1e12:.2f}T"
            elif mcap >= 1e9:
                mcap_str = f"{mcap/1e8:.0f}억" if tk.endswith((".KS", ".KQ")) else f"${mcap/1e9:.1f}B"
            else:
                mcap_str = "-"
            per = info.get("trailingPE") or info.get("forwardPE")
            per_str = f"PER {per:.1f}" if per else "PER -"
            rg = info.get("revenueGrowth")
            rg_str = f"매출 {rg*100:+.0f}%" if rg is not None else ""
            summary = (info.get("longBusinessSummary") or "")[:140]
            parts = [f"{sector}/{industry}", f"시총 {mcap_str}", per_str]
            if rg_str:
                parts.append(rg_str)
            line = " | ".join(parts)
            if summary:
                line += f"\n      사업: {summary}"
            return tk, line
        except Exception:
            return tk, ""

    out: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for tk, line in ex.map(_one, tickers):
            if line:
                out[tk] = line
    return out


def discovery_to_text(market: str) -> str:
    """전시장 발굴 결과 텍스트 (프롬프트 주입용). 펀더멘털 요약 포함."""
    hits = discover_kr() if market == "KR" else discover_us()
    if not hits:
        return f"【전시장 발굴 — {market}】 발굴 종목 없음 (스크리너 응답 없음)"

    # 주간 사후 추적용 기록 ("그때 발굴한 종목이 이후 어떻게 됐나")
    try:
        from core.weekly_report import record_discoveries
        record_discoveries(hits, market)
    except Exception as e:
        log.debug("발굴 기록 스킵: %s", e)

    held, watchlist = _held_and_watchlist()
    # 펀더멘털 보강 — 상위 12개만 (조회 비용 절약)
    fund = _fetch_fundamentals_brief([h.ticker for h in hits[:12]])

    unit = "₩" if market == "KR" else "$"
    lines = [
        f"【전시장 발굴 — {market} 전체 시장 스크리닝】 {len(hits)}건",
        "  (유니버스/워치리스트 밖 — 시장 전체에서 당일 급등·거래량 주도주 발굴)",
    ]
    for h in hits:
        flags = []
        if h.ticker in held:
            flags.append("보유중")
        if h.ticker in watchlist:
            flags.append("WL")
        flag = f" ({','.join(flags)})" if flags else ""
        extra = ""
        if h.ret_60d:
            extra = f" | 60일 {h.ret_60d:+.1f}% | RSI {h.rsi:.0f}"
        lines.append(
            f"  [{h.source}] {h.name}({h.ticker}){flag}: "
            f"{unit}{h.price:,.0f} ({h.change_pct:+.1f}%){extra}"
        )
        if h.ticker in fund:
            lines.append(f"      {fund[h.ticker]}")
    return "\n".join(lines)


def scan_to_text(briefing_type: str = "MANUAL") -> str:
    """briefing_type에 맞는 시장 스캔 + 전시장 발굴 텍스트 (프롬프트 주입용)."""
    parts: list[str] = []
    if briefing_type in ("KR_BEFORE", "KR_NIGHT"):
        markets = ["KR"]
    elif briefing_type in ("US_BEFORE", "US_NIGHT", "US_CLOSE"):
        markets = ["US"]
    else:
        markets = ["KR", "US"]

    for m in markets:
        try:
            parts.append(scan_market(m).to_text())
        except Exception as e:
            log.warning("유니버스 스캔 실패 (%s): %s", m, e)
        try:
            parts.append(discovery_to_text(m))
        except Exception as e:
            log.warning("전시장 발굴 실패 (%s): %s", m, e)
    return "\n\n".join(p for p in parts if p)
