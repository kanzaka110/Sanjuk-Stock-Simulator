"""
한국 시장 강화 — 네이버 금융 조회

KRX data.krx.co.kr가 투자자별 매매/펀더멘털 데이터를 로그인 게이트로 막아
(OTP 응답 "LOGOUT") 직접 호출이 불가능해졌다. 로그인 불필요한 네이버 금융을
스크래핑하여 기관/외국인 순매매와 펀더멘털(PER/PBR/배당률)을 수집한다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import requests

from config.settings import KRW_TICKERS

log = logging.getLogger(__name__)

# 네이버 금융 (로그인 불필요, 공개 데이터)
NAVER_FRGN_URL = "https://finance.naver.com/item/frgn.naver"
NAVER_MAIN_URL = "https://finance.naver.com/item/main.naver"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
}

# 종목코드 → 종목명 매핑 (네이버는 이름을 안 주므로 설정에서 보강)
_NAME_MAP: dict[str, str] = {}


def _name_for(code: str) -> str:
    """KRX 종목코드(6자리)로 종목명 조회. 설정의 포트폴리오/관심/RIA 맵 통합."""
    global _NAME_MAP
    if not _NAME_MAP:
        from config.settings import PORTFOLIO, RIA_ALLOWED_TICKERS, WATCHLIST

        for src in (PORTFOLIO, WATCHLIST, RIA_ALLOWED_TICKERS):
            for tk, nm in src.items():
                _NAME_MAP[_krx_ticker(tk)] = nm
    return _NAME_MAP.get(code, code)


# 네이버 frgn 페이지 일별 시리즈 캐시 (프로세스 수명 동안 — 브리핑은 단명 프로세스)
_FRGN_CACHE: dict[str, list[dict]] = {}


def _fetch_naver_frgn(code: str) -> list[dict]:
    """네이버 금융 외국인·기관 순매매 일별 시리즈 조회.

    Returns:
        최신순 dict 리스트: [{"date": "YYYYMMDD", "close": float,
        "inst_shares": float, "foreign_shares": float}, ...]
        실패 시 빈 리스트.
    """
    if code in _FRGN_CACHE:
        return _FRGN_CACHE[code]

    import io

    import pandas as pd

    result: list[dict] = []
    try:
        r = requests.get(
            NAVER_FRGN_URL, params={"code": code}, headers=HEADERS, timeout=10
        )
        if r.status_code != 200:
            log.warning(f"네이버 수급 조회 실패 ({code}): HTTP {r.status_code}")
            return []

        tables = pd.read_html(io.StringIO(r.text))
        # 외국인/기관 일별 매매 테이블: 9개 컬럼(날짜·종가·전일비·등락률·거래량·기관순매매·외국인순매매·보유주수·보유율)
        target = None
        for t in tables:
            if t.shape[1] == 9 and t.shape[0] > 3:
                target = t
                break
        if target is None:
            log.warning(f"네이버 수급 테이블 미발견 ({code})")
            return []

        for _, row in target.iterrows():
            vals = list(row)
            raw_date = str(vals[0]).strip()
            m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", raw_date)
            if not m:
                continue
            try:
                close = float(str(vals[1]).replace(",", ""))
                inst = float(str(vals[5]).replace(",", ""))
                foreign = float(str(vals[6]).replace(",", ""))
            except (ValueError, TypeError):
                continue
            result.append({
                "date": f"{m.group(1)}{m.group(2)}{m.group(3)}",
                "close": close,
                "inst_shares": inst,
                "foreign_shares": foreign,
            })
    except Exception as e:
        log.warning(f"네이버 수급 파싱 실패 ({code}): {e}")
        return []

    _FRGN_CACHE[code] = result
    return result


def _flow_from_row(code: str, row: dict) -> "InstitutionalFlow":
    """네이버 일별 행(주식수) → InstitutionalFlow(백만원 환산)."""
    close = row["close"]
    # 순매매량(주) × 종가(원) / 1e6 = 순매매대금(백만원)
    foreign_won = row["foreign_shares"] * close / 1_000_000
    inst_won = row["inst_shares"] * close / 1_000_000
    return InstitutionalFlow(
        ticker=f"{code}.KS",
        name=_name_for(code),
        foreign_net=foreign_won,
        institution_net=inst_won,
        individual_net=0.0,  # 네이버 frgn 페이지는 개인 순매매 미제공
        foreign_label="매수" if foreign_won > 0 else "매도",
        institution_label="매수" if inst_won > 0 else "매도",
    )


@dataclass(frozen=True)
class InstitutionalFlow:
    """기관/외국인 매매 동향."""

    ticker: str
    name: str
    foreign_net: float  # 외국인 순매수 (금액)
    institution_net: float  # 기관 순매수 (금액)
    individual_net: float  # 개인 순매수 (금액)
    foreign_label: str  # 매수/매도
    institution_label: str


@dataclass(frozen=True)
class Fundamental:
    """종목 펀더멘털."""

    ticker: str
    name: str
    per: float = 0.0  # PER
    pbr: float = 0.0  # PBR
    eps: float = 0.0  # EPS
    bps: float = 0.0  # BPS
    div_yield: float = 0.0  # 배당수익률 (%)
    market_cap: float = 0.0  # 시가총액 (억원)


def _krx_ticker(ticker: str) -> str:
    """yfinance 티커를 KRX 종목코드로 변환. 예: 005930.KS → 005930"""
    return ticker.replace(".KS", "").replace(".KQ", "")


def fetch_institutional_flow(date: str | None = None) -> list[InstitutionalFlow]:
    """네이버 금융 투자자별 매매 동향 조회 (포트폴리오 종목).

    Args:
        date: 조회 날짜 (YYYYMMDD). None이면 각 종목의 최신 거래일.

    Returns:
        InstitutionalFlow 리스트 (외국인/기관 순매매를 백만원으로 환산)
    """
    results: list[InstitutionalFlow] = []
    codes = {_krx_ticker(tk) for tk in KRW_TICKERS}

    for code in codes:
        series = _fetch_naver_frgn(code)
        if not series:
            continue

        row = None
        if date is not None:
            row = next((r for r in series if r["date"] == date), None)
        if row is None:
            row = series[0]  # 최신 거래일

        results.append(_flow_from_row(code, row))

    if not results:
        log.warning("네이버 기관/외국인 매매 조회 실패 (전 종목)")
    return results


def _fetch_naver_fundamental(code: str) -> Fundamental | None:
    """네이버 금융 종목 페이지에서 PER/EPS/PBR/배당률 조회."""
    try:
        r = requests.get(
            NAVER_MAIN_URL, params={"code": code}, headers=HEADERS, timeout=10
        )
        if r.status_code != 200:
            return None
        r.encoding = "euc-kr"
        html = r.text

        def from_id(eid: str) -> float:
            m = re.search(rf'id="{eid}"[^>]*>([^<]+)<', html)
            if not m:
                return 0.0
            try:
                return float(m.group(1).replace(",", "").strip())
            except ValueError:
                return 0.0

        def from_label(label: str) -> float:
            # 투자정보 테이블의 "배당수익률 N.NN%" 형태
            m = re.search(rf'{label}[^0-9\-]*?([\-]?[\d,]+\.?\d*)', html)
            if not m:
                return 0.0
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                return 0.0

        per = from_id("_per")
        eps = from_id("_eps")
        pbr = from_id("_pbr")
        div_yield = from_label("배당수익률")

        if per == 0.0 and pbr == 0.0 and eps == 0.0:
            return None  # 데이터 미확보 → 폴백 유도

        return Fundamental(
            ticker=f"{code}.KS",
            name=_name_for(code),
            per=per,
            pbr=pbr,
            eps=eps,
            bps=0.0,  # 네이버 메인 페이지 미제공
            div_yield=div_yield,
            market_cap=0.0,  # 네이버 메인 페이지 미제공
        )
    except Exception as e:
        log.warning(f"네이버 펀더멘털 조회 실패 ({code}): {e}")
        return None


def fetch_fundamentals() -> list[Fundamental]:
    """네이버 금융 종목별 펀더멘털 데이터 조회 (PER/PBR/배당률).

    Returns:
        Fundamental 리스트 (포트폴리오 종목만)
    """
    results: list[Fundamental] = []
    codes = {_krx_ticker(tk) for tk in KRW_TICKERS}
    for code in codes:
        f = _fetch_naver_fundamental(code)
        if f:
            results.append(f)
    return results


def fetch_cumulative_flows(days: int = 5) -> dict[str, dict]:
    """N일 누적 외국인/기관 순매수 데이터 — 네이버 종목별 1회 조회.

    Returns:
        {ticker: {"foreign_5d": sum, "institution_5d": sum, "foreign_trend": "매수전환/매도지속/..."}}
    """
    cumulative: dict[str, dict] = {}
    codes = {_krx_ticker(tk) for tk in KRW_TICKERS}

    for code in codes:
        series = _fetch_naver_frgn(code)
        if not series:
            continue

        ticker = f"{code}.KS"
        # 네이버는 최신순 → 최근 days일을 일별 환산(백만원)
        recent = [_flow_from_row(code, r) for r in series[:days]]
        if not recent:
            continue

        cumulative[ticker] = {
            "name": recent[0].name,
            "foreign_5d": sum(f.foreign_net for f in recent),
            "institution_5d": sum(f.institution_net for f in recent),
            "daily_foreign": [f.foreign_net for f in recent],  # 최신순
        }

    # 추세 레이블 생성
    for ticker, data in cumulative.items():
        daily = data.get("daily_foreign", [])
        cum = data["foreign_5d"]

        if len(daily) < 2:
            data["foreign_trend"] = "데이터부족"
        elif cum > 0 and daily[0] > 0:
            data["foreign_trend"] = "매수지속" if all(d > 0 for d in daily) else "매수전환"
        elif cum < 0 and daily[0] < 0:
            data["foreign_trend"] = "매도지속" if all(d < 0 for d in daily) else "매도전환"
        elif cum > 0:
            data["foreign_trend"] = "매수전환"
        elif cum < 0:
            data["foreign_trend"] = "매도전환"
        else:
            data["foreign_trend"] = "중립"

        # daily_foreign은 텍스트 생성에 불필요하므로 제거
        del data["daily_foreign"]

    return cumulative


def fetch_short_selling(days: int = 10) -> dict[str, dict]:
    """KIS 공매도 일별추이 기반 종목별 공매도 요약 (포트폴리오 KR 종목).

    KRX 잔고 데이터는 로그인 게이트라 불가 → KIS '거래' 기반 지표 사용.

    Returns:
        {ticker: {"name", "short_ratio_pct"(당일), "avg5_ratio_pct"(5일 평균),
                  "trend"(급증/증가/보통/감소), "date"}}
    """
    from datetime import datetime, timedelta, timezone

    from core.market_kis import get_domestic_short_sale

    KST = timezone(timedelta(hours=9))
    end = datetime.now(KST)
    start = end - timedelta(days=days + 7)  # 휴장일 여유

    results: dict[str, dict] = {}
    for tk in KRW_TICKERS:
        series = get_domestic_short_sale(
            tk, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        )
        if not series:
            continue
        latest = series[0]
        ratios = [r["short_ratio_pct"] for r in series[:5]]
        avg5 = sum(ratios) / len(ratios) if ratios else 0.0
        today = latest["short_ratio_pct"]

        if avg5 >= 1 and today >= avg5 * 2 and today >= 8:
            trend = "급증"
        elif today > avg5 * 1.3 and today >= 5:
            trend = "증가"
        elif today < avg5 * 0.6:
            trend = "감소"
        else:
            trend = "보통"

        results[tk] = {
            "name": _name_for(_krx_ticker(tk)),
            "short_ratio_pct": today,
            "avg5_ratio_pct": round(avg5, 2),
            "trend": trend,
            "date": latest["date"],
        }
    return results


def kr_market_to_text(
    flows: list[InstitutionalFlow],
    fundamentals: list[Fundamental],
    cumulative: dict[str, dict] | None = None,
    short_selling: dict[str, dict] | None = None,
) -> str:
    """한국 시장 데이터를 텍스트로 변환."""
    lines = ["【한국 시장 심층 데이터】"]

    if flows:
        lines.append("\n  [기관/외국인 매매 동향]")
        for f in flows:
            cum_info = ""
            if cumulative and f.ticker in cumulative:
                c = cumulative[f.ticker]
                cum_info = (
                    f" | 5일 누적 외국인 {c['foreign_5d']:+,.0f}백만 ({c['foreign_trend']})"
                    f" | 5일 누적 기관 {c['institution_5d']:+,.0f}백만"
                )
            lines.append(
                f"  {f.name}: 외국인 {f.foreign_net:+,.0f}백만 ({f.foreign_label}) | "
                f"기관 {f.institution_net:+,.0f}백만 ({f.institution_label}){cum_info}"
            )

    if short_selling:
        lines.append("\n  [공매도 거래 비중 — KIS 일별추이 (잔고 아님)]")
        for tk, s in sorted(
            short_selling.items(),
            key=lambda kv: kv[1]["short_ratio_pct"],
            reverse=True,
        ):
            mark = " ⚠️" if s["trend"] in ("급증", "증가") else ""
            lines.append(
                f"  {s['name']}: 당일 {s['short_ratio_pct']:.1f}% | "
                f"5일 평균 {s['avg5_ratio_pct']:.1f}% | {s['trend']}{mark}"
            )

    if fundamentals:
        lines.append("\n  [펀더멘털]")
        for f in fundamentals:
            lines.append(
                f"  {f.name}: PER {f.per:.1f} | PBR {f.pbr:.2f} | "
                f"EPS {f.eps:,.0f} | 배당률 {f.div_yield:.1f}% | "
                f"시총 {f.market_cap:,.0f}억"
            )

    if len(lines) == 1:
        lines.append("  (데이터 없음 — 비거래일 또는 조회 실패)")

    return "\n".join(lines)
