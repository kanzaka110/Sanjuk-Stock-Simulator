"""
한국 시장 강화 — KRX/DART 직접 조회

pykrx 대신 KRX/DART API를 직접 호출하여
기관/외국인 매매, 펀더멘털(PER/PBR/배당률) 데이터를 수집한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

from config.settings import KRW_TICKERS, KST

log = logging.getLogger(__name__)

# KRX API (비공식, 공개 데이터)
KRX_OTP_URL = "http://data.krx.co.kr/comm/fileDn/GenerateOTP/generate.cmd"
KRX_DOWNLOAD_URL = "http://data.krx.co.kr/comm/fileDn/download_csv/download.cmd"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "http://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd",
}


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
    """KRX 투자자별 매매 동향 조회.

    Args:
        date: 조회 날짜 (YYYYMMDD). None이면 직전 거래일.

    Returns:
        InstitutionalFlow 리스트 (포트폴리오 종목만)
    """
    if date is None:
        # 직전 거래일 추정 (주말 제외)
        now = datetime.now(KST)
        for offset in range(0, 5):
            d = now - timedelta(days=offset)
            if d.weekday() < 5:  # 월~금
                date = d.strftime("%Y%m%d")
                break

    # 최대 2회 시도 (당일 실패 시 전일 재시도)
    dates_to_try = [date]
    prev = datetime.now(KST) - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    prev_date = prev.strftime("%Y%m%d")
    if prev_date != date:
        dates_to_try.append(prev_date)

    for try_date in dates_to_try:
        try:
            otp_params = {
                "locale": "ko_KR",
                "mktId": "STK",
                "trdDd": try_date,
                "money": "1",
                "csvxls_is498No": "",
                "name": "fileDown",
                "url": "dbms/MDC/STAT/standard/MDCSTAT02203",
            }
            otp_res = requests.post(KRX_OTP_URL, data=otp_params, headers=HEADERS, timeout=10)
            otp = otp_res.text

            csv_res = requests.post(
                KRX_DOWNLOAD_URL,
                data={"code": otp},
                headers=HEADERS,
                timeout=10,
            )

            if csv_res.status_code != 200 or len(csv_res.content) < 100:
                log.warning(f"KRX 데이터 조회 실패 ({try_date}): {csv_res.status_code}")
                continue

            import io
            import pandas as pd

            df = pd.read_csv(io.BytesIO(csv_res.content), encoding="euc-kr")

            results: list[InstitutionalFlow] = []
            portfolio_codes = {_krx_ticker(tk) for tk in KRW_TICKERS}

            for _, row in df.iterrows():
                code = str(row.get("종목코드", "")).strip()
                if code not in portfolio_codes:
                    continue

                name = str(row.get("종목명", "")).strip()
                foreign = float(str(row.get("외국인합계", "0")).replace(",", "") or "0")
                institution = float(str(row.get("기관합계", "0")).replace(",", "") or "0")
                individual = float(str(row.get("개인", "0")).replace(",", "") or "0")

                results.append(InstitutionalFlow(
                    ticker=f"{code}.KS",
                    name=name,
                    foreign_net=foreign,
                    institution_net=institution,
                    individual_net=individual,
                    foreign_label="매수" if foreign > 0 else "매도",
                    institution_label="매수" if institution > 0 else "매도",
                ))

            if results:
                if try_date != date:
                    log.info(f"KRX 당일 데이터 없음 → 전일({try_date}) 데이터 활용")
                return results
        except Exception as e:
            log.warning(f"기관/외국인 매매 조회 실패 ({try_date}): {e}")

    log.warning("KRX 기관/외국인 매매 2회 시도 모두 실패")
    return []


def fetch_fundamentals() -> list[Fundamental]:
    """KRX 종목별 펀더멘털 데이터 조회 (PER/PBR/배당률).

    Returns:
        Fundamental 리스트 (포트폴리오 종목만)
    """
    now = datetime.now(KST)
    date = now.strftime("%Y%m%d")

    try:
        otp_params = {
            "locale": "ko_KR",
            "mktId": "STK",
            "trdDd": date,
            "money": "1",
            "csvxls_isNo": "",
            "name": "fileDown",
            "url": "dbms/MDC/STAT/standard/MDCSTAT03501",
        }
        otp_res = requests.post(KRX_OTP_URL, data=otp_params, headers=HEADERS, timeout=10)
        otp = otp_res.text

        csv_res = requests.post(
            KRX_DOWNLOAD_URL,
            data={"code": otp},
            headers=HEADERS,
            timeout=10,
        )

        if csv_res.status_code != 200 or len(csv_res.content) < 100:
            return []

        import io
        import pandas as pd

        df = pd.read_csv(io.BytesIO(csv_res.content), encoding="euc-kr")

        results: list[Fundamental] = []
        portfolio_codes = {_krx_ticker(tk) for tk in KRW_TICKERS}

        for _, row in df.iterrows():
            code = str(row.get("종목코드", "")).strip()
            if code not in portfolio_codes:
                continue

            def safe_float(val: str | float, default: float = 0.0) -> float:
                try:
                    return float(str(val).replace(",", "").replace("-", "0"))
                except (ValueError, TypeError):
                    return default

            results.append(Fundamental(
                ticker=f"{code}.KS",
                name=str(row.get("종목명", "")).strip(),
                per=safe_float(row.get("PER", 0)),
                pbr=safe_float(row.get("PBR", 0)),
                eps=safe_float(row.get("EPS", 0)),
                bps=safe_float(row.get("BPS", 0)),
                div_yield=safe_float(row.get("DIV", 0)),
                market_cap=safe_float(row.get("시가총액", 0)) / 100_000_000,
            ))

        return results
    except Exception as e:
        log.warning(f"펀더멘털 조회 실패: {e}")
        return []


def fetch_cumulative_flows(days: int = 5) -> dict[str, dict]:
    """N일 누적 외국인/기관 순매수 데이터 — KRX API 다중 호출.

    Returns:
        {ticker: {"foreign_5d": sum, "institution_5d": sum, "foreign_trend": "매수전환/매도지속/..."}}
    """
    now = datetime.now(KST)
    cumulative: dict[str, dict] = {}

    for offset in range(days):
        d = now - timedelta(days=offset)
        # 주말 건너뛰기
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        date_str = d.strftime("%Y%m%d")

        try:
            flows = fetch_institutional_flow(date=date_str)
            for f in flows:
                if f.ticker not in cumulative:
                    cumulative[f.ticker] = {
                        "name": f.name,
                        "foreign_5d": 0.0,
                        "institution_5d": 0.0,
                        "daily_foreign": [],
                    }
                cumulative[f.ticker]["foreign_5d"] += f.foreign_net
                cumulative[f.ticker]["institution_5d"] += f.institution_net
                cumulative[f.ticker]["daily_foreign"].append(f.foreign_net)
        except Exception as e:
            log.debug("누적 수급 %s 조회 실패: %s", date_str, e)
            continue

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


def kr_market_to_text(
    flows: list[InstitutionalFlow],
    fundamentals: list[Fundamental],
    cumulative: dict[str, dict] | None = None,
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
