"""
한국투자증권 KIS Open API — 국내주식 실시간 시세 제공자

- OAuth 토큰 자동 관리 (24시간 캐시)
- 국내주식 현재가 조회 (FHKST01010100)
- 실패 시 None 반환 → 호출측에서 yfinance 폴백
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import requests

from config.settings import (
    DB_DIR,
    KIS_APP_KEY,
    KIS_APP_SECRET,
    KIS_BASE_URL,
    KRW_TICKERS,
    PORTFOLIO,
)
from core.models import Quote

logger = logging.getLogger(__name__)

# ─── 토큰 캐시 (메모리 + 파일) ─────────────────────
_TOKEN_LOCK = threading.Lock()
_TOKEN_FILE = DB_DIR / "kis_token.json"

_mem_token: str = ""
_mem_expires: float = 0.0


def _is_kis_configured() -> bool:
    """KIS API 키가 설정되어 있는지 확인."""
    return bool(KIS_APP_KEY and KIS_APP_SECRET)


def _load_token_from_file() -> tuple[str, float]:
    """파일에서 캐시된 토큰 로드."""
    try:
        if _TOKEN_FILE.exists():
            data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
            return data.get("token", ""), float(data.get("expires_at", 0))
    except (json.JSONDecodeError, OSError):
        pass
    return "", 0.0


def _save_token_to_file(token: str, expires_at: float) -> None:
    """토큰을 파일에 캐시."""
    try:
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(
            json.dumps({"token": token, "expires_at": expires_at}),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("KIS 토큰 파일 저장 실패: %s", e)


def _get_access_token() -> str | None:
    """OAuth 접근 토큰 발급 (메모리 → 파일 → API 순서로 조회).

    토큰 유효기간: 약 24시간. 만료 1시간 전에 갱신.
    KIS는 토큰 발급을 분당 1회로 제한하므로 파일 캐시 필수.
    """
    global _mem_token, _mem_expires

    now = time.time()

    # 1) 메모리 캐시 확인
    with _TOKEN_LOCK:
        if _mem_token and now < _mem_expires:
            return _mem_token

    # 2) 파일 캐시 확인
    file_token, file_expires = _load_token_from_file()
    if file_token and now < file_expires:
        with _TOKEN_LOCK:
            _mem_token = file_token
            _mem_expires = file_expires
        return file_token

    # 3) API로 신규 발급
    try:
        resp = requests.post(
            f"{KIS_BASE_URL}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": KIS_APP_KEY,
                "appsecret": KIS_APP_SECRET,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        token = data.get("access_token", "")
        expires_in = int(data.get("expires_in", 86400))

        if not token:
            logger.warning("KIS 토큰 발급 실패: 응답에 access_token 없음")
            return None

        expires_at = now + expires_in - 3600  # 만료 1시간 전 갱신

        with _TOKEN_LOCK:
            _mem_token = token
            _mem_expires = expires_at

        _save_token_to_file(token, expires_at)
        logger.info("KIS 접근 토큰 발급 성공 (만료: %ds)", expires_in)
        return token

    except requests.RequestException as e:
        logger.warning("KIS 토큰 발급 실패: %s", e)
        return None


def _ticker_to_kis_code(ticker: str) -> str:
    """yfinance 티커 → KIS 종목코드 변환.

    005930.KS → 005930
    """
    return ticker.replace(".KS", "").replace(".KQ", "")


# ─── 국내주식 현재가 조회 ──────────────────────────
def get_domestic_price(ticker: str) -> Quote | None:
    """KIS API로 국내주식 현재가 조회.

    Args:
        ticker: yfinance 형식 티커 (예: 005930.KS)

    Returns:
        Quote 또는 실패 시 None
    """
    if not _is_kis_configured():
        return None

    token = _get_access_token()
    if not token:
        return None

    stock_code = _ticker_to_kis_code(ticker)

    try:
        resp = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers={
                "authorization": f"Bearer {token}",
                "appkey": KIS_APP_KEY,
                "appsecret": KIS_APP_SECRET,
                "tr_id": "FHKST01010100",
                "content-type": "application/json; charset=utf-8",
            },
            params={
                "FID_COND_MRKT_DIV_CODE": "J",  # 주식
                "FID_INPUT_ISCD": stock_code,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("rt_cd") != "0":
            logger.warning(
                "KIS 시세 조회 실패 [%s]: %s",
                ticker,
                data.get("msg1", "unknown error"),
            )
            return None

        output = data.get("output", {})
        price = float(output.get("stck_prpr", 0))  # 현재가
        prev_close = float(output.get("stck_sdpr", 0))  # 전일 종가
        high = float(output.get("stck_hgpr", 0))  # 최고가
        low = float(output.get("stck_lwpr", 0))  # 최저가

        if price <= 0:
            return None

        change = price - prev_close if prev_close > 0 else 0.0
        pct = (change / prev_close * 100) if prev_close > 0 else 0.0

        name = PORTFOLIO.get(ticker, ticker)

        return Quote(
            ticker=ticker,
            name=name,
            price=round(price, 0),
            change=round(change, 0),
            pct=round(pct, 2),
            high=round(high, 0),
            low=round(low, 0),
        )

    except requests.RequestException as e:
        logger.warning("KIS 시세 조회 네트워크 오류 [%s]: %s", ticker, e)
        return None
    except (KeyError, ValueError, TypeError) as e:
        logger.warning("KIS 시세 파싱 오류 [%s]: %s", ticker, e)
        return None


# ─── 해외주식 현재가 조회 ──────────────────────────
# 거래소 코드 매핑 (yfinance 티커 → KIS 거래소 코드)
_US_EXCHANGE_MAP: dict[str, str] = {
    "NVDA": "NAS",
    "GOOGL": "NAS",
    "GOOG": "NAS",
    "MU": "NAS",
    "LMT": "NYS",
    "AAPL": "NAS",
    "MSFT": "NAS",
    "AMZN": "NAS",
    "TSLA": "NAS",
    "META": "NAS",
    "AMD": "NAS",
    "INTC": "NAS",
    "AVGO": "NAS",
    "QCOM": "NAS",
    "COST": "NAS",
    "NFLX": "NAS",
    "JPM": "NYS",
    "V": "NYS",
    "JNJ": "NYS",
    "WMT": "NYS",
    "BA": "NYS",
    "DIS": "NYS",
    "KO": "NYS",
    "PG": "NYS",
    "XOM": "NYS",
    "CVX": "NYS",
    "RTX": "NYS",
    "GD": "NYS",
    "NOC": "NYS",
}

# NAS 우선 시도 → NYS 폴백 순서
_EXCHANGE_FALLBACK = ["NAS", "NYS", "AMS"]


def _resolve_exchange(ticker: str) -> str | None:
    """티커의 거래소 코드를 결정. 매핑에 없으면 순차 시도."""
    if ticker in _US_EXCHANGE_MAP:
        return _US_EXCHANGE_MAP[ticker]
    return None


def get_overseas_price(ticker: str) -> Quote | None:
    """KIS API로 해외주식 현재가 조회.

    Args:
        ticker: yfinance 형식 티커 (예: NVDA, LMT)

    Returns:
        Quote 또는 실패 시 None
    """
    if not _is_kis_configured():
        return None

    token = _get_access_token()
    if not token:
        return None

    # 거래소 코드 결정
    known_excd = _resolve_exchange(ticker)
    exchanges = [known_excd] if known_excd else _EXCHANGE_FALLBACK

    for excd in exchanges:
        quote = _fetch_overseas_quote(ticker, excd, token)
        if quote is not None:
            # 매핑에 없었으면 학습
            if ticker not in _US_EXCHANGE_MAP:
                _US_EXCHANGE_MAP[ticker] = excd
            return quote

    return None


def _fetch_overseas_quote(
    ticker: str, excd: str, token: str,
) -> Quote | None:
    """KIS 해외주식 현재가 API 호출."""
    try:
        resp = requests.get(
            f"{KIS_BASE_URL}/uapi/overseas-price/v1/quotations/price",
            headers={
                "authorization": f"Bearer {token}",
                "appkey": KIS_APP_KEY,
                "appsecret": KIS_APP_SECRET,
                "tr_id": "HHDFS00000300",
                "content-type": "application/json; charset=utf-8",
            },
            params={
                "AUTH": "",
                "EXCD": excd,
                "SYMB": ticker,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("rt_cd") != "0":
            return None

        output = data.get("output", {})
        price = float(output.get("last", 0))  # 현재가
        prev_close = float(output.get("base", 0))  # 전일 종가
        high = float(output.get("high", 0))  # 최고가
        low = float(output.get("low", 0))  # 최저가

        if price <= 0:
            return None

        change = price - prev_close if prev_close > 0 else 0.0
        pct = (change / prev_close * 100) if prev_close > 0 else 0.0

        name = PORTFOLIO.get(ticker, ticker)

        return Quote(
            ticker=ticker,
            name=name,
            price=round(price, 2),
            change=round(change, 2),
            pct=round(pct, 2),
            high=round(high, 2),
            low=round(low, 2),
        )

    except requests.RequestException:
        return None
    except (KeyError, ValueError, TypeError):
        return None


def get_overseas_prices(tickers: list[str]) -> dict[str, Quote]:
    """여러 해외 종목 시세를 KIS API로 조회.

    Args:
        tickers: 해외 티커 목록 (예: ["NVDA", "GOOGL"])

    Returns:
        {ticker: Quote} 딕셔너리 (실패한 종목은 제외)
    """
    results: dict[str, Quote] = {}

    if not _is_kis_configured():
        return results

    for tk in tickers:
        q = get_overseas_price(tk)
        if q is not None:
            results[tk] = q
        time.sleep(0.06)

    return results


def get_domestic_prices(tickers: list[str]) -> dict[str, Quote]:
    """여러 국내 종목 시세를 KIS API로 조회.

    Args:
        tickers: yfinance 형식 티커 목록 (예: ["005930.KS", "012450.KS"])

    Returns:
        {ticker: Quote} 딕셔너리 (실패한 종목은 제외)
    """
    results: dict[str, Quote] = {}

    if not _is_kis_configured():
        return results

    for tk in tickers:
        q = get_domestic_price(tk)
        if q is not None:
            results[tk] = q
        # KIS rate limit: 초당 20건, 안전하게 간격 유지
        time.sleep(0.06)

    return results


def is_available() -> bool:
    """KIS API 사용 가능 여부 (키 설정 + 토큰 발급 성공)."""
    if not _is_kis_configured():
        return False
    return _get_access_token() is not None


# ─── 국내주식 차트 (분봉/일봉) ─────────────────────
def get_domestic_chart(
    ticker: str, period: str = "1d", interval: str = "5m"
) -> dict | None:
    """KIS API로 국내주식 OHLCV 차트 조회. 실패 시 None.

    period/interval 매핑:
      1d/5m  → 당일 분봉 (FHKST03010200)
      1mo/1d → 일봉 30일 (FHKST03010100)
      3mo/1d → 일봉 90일 (FHKST03010100)
      5d/15m → 미지원 → None (yfinance fallback)
    """
    if not _is_kis_configured():
        return None

    token = _get_access_token()
    if not token:
        return None

    stock_code = _ticker_to_kis_code(ticker)
    is_intraday = interval in ("5m", "15m")

    # 5d/15m은 KIS에서 직접 지원 어려움 → None
    if period == "5d":
        return None

    try:
        if is_intraday:
            return _fetch_domestic_minute_chart(token, stock_code, ticker)
        else:
            days = 30 if period == "1mo" else 90
            return _fetch_domestic_daily_chart(token, stock_code, ticker, days)
    except Exception as e:
        logger.warning("KIS 차트 조회 실패 [%s]: %s", ticker, e)
        return None


def _fetch_domestic_minute_chart(
    token: str, stock_code: str, ticker: str
) -> dict | None:
    """당일 분봉 조회 (FHKST03010200)."""
    from datetime import datetime, timezone, timedelta
    KST = timezone(timedelta(hours=9))
    now = datetime.now(KST)
    time_str = now.strftime("%H%M%S")

    resp = requests.get(
        f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
        headers={
            "authorization": f"Bearer {token}",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
            "tr_id": "FHKST03010200",
            "content-type": "application/json; charset=utf-8",
        },
        params={
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_HOUR_1": time_str,
            "FID_PW_DATA_INCU_YN": "Y",
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("rt_cd") != "0":
        return None

    output2 = data.get("output2", [])
    if not output2:
        return None

    points: list[dict] = []
    for item in reversed(output2):
        t = item.get("stck_cntg_hour", "")
        if len(t) >= 4:
            time_label = f"{t[:2]}:{t[2:4]}"
        else:
            continue
        o = float(item.get("stck_oprc", 0))
        h = float(item.get("stck_hgpr", 0))
        lo = float(item.get("stck_lwpr", 0))
        c = float(item.get("stck_prpr", 0))
        v = int(item.get("cntg_vol", 0))
        if c <= 0:
            continue
        points.append({
            "time": time_label, "open": round(o),
            "high": round(h), "low": round(lo),
            "close": round(c), "volume": v,
        })

    if not points:
        return None

    last = points[-1]["close"]
    first = points[0]["open"]
    day_pct = round((last - first) / first * 100, 2) if first else 0.0

    return {
        "points": points,
        "current_price": last,
        "day_pct": day_pct,
        "source": "KIS",
    }


def _fetch_domestic_daily_chart(
    token: str, stock_code: str, ticker: str, days: int = 30
) -> dict | None:
    """일봉 조회 (FHKST03010100)."""
    from datetime import datetime, timezone, timedelta
    KST = timezone(timedelta(hours=9))
    now = datetime.now(KST)
    end_date = now.strftime("%Y%m%d")
    start_date = (now - timedelta(days=days + 10)).strftime("%Y%m%d")

    resp = requests.get(
        f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        headers={
            "authorization": f"Bearer {token}",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
            "tr_id": "FHKST03010100",
            "content-type": "application/json; charset=utf-8",
        },
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("rt_cd") != "0":
        return None

    output2 = data.get("output2", [])
    if not output2:
        return None

    points: list[dict] = []
    for item in reversed(output2[:days]):
        d = item.get("stck_bsop_date", "")
        if len(d) >= 8:
            time_label = f"{d[4:6]}-{d[6:8]}"
        else:
            continue
        o = float(item.get("stck_oprc", 0))
        h = float(item.get("stck_hgpr", 0))
        lo = float(item.get("stck_lwpr", 0))
        c = float(item.get("stck_clpr", 0))
        v = int(item.get("acml_vol", 0))
        if c <= 0:
            continue
        points.append({
            "time": time_label, "open": round(o),
            "high": round(h), "low": round(lo),
            "close": round(c), "volume": v,
        })

    if not points:
        return None

    last = points[-1]["close"]
    first = points[0]["open"]
    day_pct = round((last - first) / first * 100, 2) if first else 0.0

    return {
        "points": points,
        "current_price": last,
        "day_pct": day_pct,
        "source": "KIS",
    }


# ─── 국내주식 호가 (read-only) ─────────────────────
def get_domestic_orderbook(ticker: str) -> dict | None:
    """KIS API 국내주식 호가 조회 (FHKST01010200). read-only 판단 보조용.

    실패 시 None. 실제 주문/자동매매에 사용 금지.
    """
    if not _is_kis_configured():
        return None

    token = _get_access_token()
    if not token:
        return None

    stock_code = _ticker_to_kis_code(ticker)

    try:
        resp = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            headers={
                "authorization": f"Bearer {token}",
                "appkey": KIS_APP_KEY,
                "appsecret": KIS_APP_SECRET,
                "tr_id": "FHKST01010200",
                "content-type": "application/json; charset=utf-8",
            },
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": stock_code,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("rt_cd") != "0":
            return None

        output = data.get("output1", {})
        if not output:
            return None

        asks = []
        for i in range(1, 6):
            p = float(output.get(f"askp{i}", 0))
            s = int(output.get(f"askp_rsqn{i}", 0))
            if p > 0:
                asks.append({"price": round(p), "size": s})

        bids = []
        for i in range(1, 6):
            p = float(output.get(f"bidp{i}", 0))
            s = int(output.get(f"bidp_rsqn{i}", 0))
            if p > 0:
                bids.append({"price": round(p), "size": s})

        best_ask = asks[0]["price"] if asks else 0
        best_bid = bids[0]["price"] if bids else 0
        spread = best_ask - best_bid if best_ask and best_bid else 0
        mid_price = (best_ask + best_bid) / 2 if best_ask and best_bid else 0
        spread_pct = round(spread / mid_price * 100, 3) if mid_price else 0

        total_bid = sum(b["size"] for b in bids)
        total_ask = sum(a["size"] for a in asks)
        total = total_bid + total_ask
        imbalance_pct = round((total_bid - total_ask) / total * 100, 1) if total else 0

        if spread_pct <= 0.2:
            exec_label = "체결 리스크 낮음"
            liq_label = "유동성 양호"
        elif spread_pct <= 0.7:
            exec_label = "스프레드 주의"
            liq_label = "유동성 보통"
        else:
            exec_label = "유동성 주의"
            liq_label = "호가 얇음"

        from datetime import datetime, timezone, timedelta
        KST = timezone(timedelta(hours=9))

        return {
            "ticker": ticker,
            "source": "KIS",
            "updated_at": datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S"),
            "bids": bids,
            "asks": asks,
            "spread": spread,
            "spread_pct": spread_pct,
            "mid_price": round(mid_price),
            "total_bid_size": total_bid,
            "total_ask_size": total_ask,
            "imbalance_pct": imbalance_pct,
            "liquidity_label": liq_label,
            "execution_risk_label": exec_label,
            "error": "",
        }

    except requests.RequestException as e:
        logger.warning("KIS 호가 조회 실패 [%s]: %s", ticker, e)
        return None
