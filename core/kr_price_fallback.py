"""
국내 주식 가격 fallback — 네이버 금융 (read-only)

- KIS/yfinance가 이상치를 반환할 때 사용 (예: 분할 전/후 가격 불일치)
- main.naver 현재가 파싱 → frgn.naver 최근 종가 순으로 시도
- 실제 주문 0건. read-only.
- 대량 호출 금지 — 단일 종목 단건 조회만
"""

from __future__ import annotations

import logging
import re

import requests

logger = logging.getLogger(__name__)

_NAVER_MAIN_URL = "https://finance.naver.com/item/main.naver"
_NAVER_FRGN_URL = "https://finance.naver.com/item/frgn.naver"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# 현재가 파싱 패턴 (우선순위 순)
# Naver Finance main.naver HTML 구조 (실측):
#   <p class="no_today">
#     <em class="no_down">
#       <span class="blind">310,000</span>   ← 현재가 (full number)
#       <span class="no3">3</span>...        ← digit-by-digit spans
_PRICE_PATTERNS = [
    # Primary: no_today 섹션 안의 첫 blind span (현재가 전체 숫자)
    r'class="no_today"[\s\S]{0,400}?<span class="blind">([\d,]+)</span>',
    # Fallback 1: no_today 섹션 안의 XX,XXX 형태 숫자
    r'class=["\']no_today["\'][\s\S]{0,400}?([\d]{2,3},[0-9]{3})',
    # Fallback 2: 구버전 id 속성
    r'id=["\']_?nowVal["\'][^>]*>([\d,]+)',
]


def _to_krx_code(ticker: str) -> str:
    """yfinance 티커 → KRX 6자리 코드. 예: 005930.KS → 005930"""
    return ticker.replace(".KS", "").replace(".KQ", "").strip()


def is_kr_ticker(ticker: str) -> bool:
    """국내 주식 티커 여부 판별."""
    if ticker.endswith(".KS") or ticker.endswith(".KQ"):
        return True
    # 6자리 숫자만 있는 경우
    t = ticker.strip()
    return bool(re.fullmatch(r"\d{6}", t))


def _parse_naver_main_price(html: str) -> float | None:
    """Naver Finance main.naver HTML에서 현재가 파싱. 실패 시 None."""
    for pattern in _PRICE_PATTERNS:
        m = re.search(pattern, html)
        if m:
            try:
                price = float(m.group(1).replace(",", ""))
                if price > 0:
                    return price
            except (ValueError, AttributeError):
                continue
    return None


def _naver_main_price(code: str) -> float | None:
    """Naver Finance 현재가 — main.naver 단건 조회."""
    try:
        r = requests.get(
            _NAVER_MAIN_URL,
            params={"code": code},
            headers=_HEADERS,
            timeout=5,
        )
        if r.status_code != 200:
            logger.debug("naver_main HTTP %s [%s]", r.status_code, code)
            return None
        r.encoding = "euc-kr"
        return _parse_naver_main_price(r.text)
    except Exception as exc:
        logger.debug("naver_main 실패 [%s]: %s", code, exc)
        return None


def _naver_frgn_recent_close(code: str) -> float | None:
    """Naver Finance frgn.naver에서 최근 거래일 종가 조회 (fallback).

    pandas read_html 사용. 가격이 있으면 최신 거래일 종가를 반환.
    """
    try:
        import io
        import pandas as pd

        r = requests.get(
            _NAVER_FRGN_URL,
            params={"code": code},
            headers=_HEADERS,
            timeout=8,
        )
        if r.status_code != 200:
            return None
        r.encoding = "euc-kr"
        tables = pd.read_html(io.StringIO(r.text))
        for t in tables:
            if t.shape[1] == 9 and t.shape[0] > 1:
                try:
                    close = float(str(t.iloc[0, 1]).replace(",", ""))
                    if close > 0:
                        return close
                except (ValueError, TypeError):
                    continue
    except Exception as exc:
        logger.debug("naver_frgn 실패 [%s]: %s", code, exc)
    return None


def get_kr_stock_price_fallback(ticker: str) -> dict:
    """국내 종목 가격 fallback.

    순서: naver_main → naver_frgn_close
    반환:
        {
            "price": float | None,
            "source": str,
            "ok": bool,
            "warning": str | None,
        }
    """
    if not is_kr_ticker(ticker):
        return {"price": None, "source": "skip_non_kr", "ok": False, "warning": "국내 종목 아님"}

    code = _to_krx_code(ticker)

    # 1) 네이버 현재가 (main.naver)
    try:
        price = _naver_main_price(code)
        if price:
            return {"price": price, "source": "naver_current", "ok": True, "warning": None}
    except Exception as exc:
        logger.debug("naver_main_price exception [%s]: %s", code, exc)

    # 2) 네이버 최근 종가 (frgn.naver 테이블)
    try:
        price = _naver_frgn_recent_close(code)
        if price:
            return {"price": price, "source": "naver_recent_close", "ok": True, "warning": None}
    except Exception as exc:
        logger.debug("naver_frgn_recent_close exception [%s]: %s", code, exc)

    return {"price": None, "source": "naver_unavailable", "ok": False, "warning": "Naver 가격 조회 실패"}
