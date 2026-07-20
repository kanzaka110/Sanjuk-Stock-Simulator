"""
한국투자증권 KIS Open API — 국내주식 실시간 시세 제공자

- OAuth 토큰 자동 관리 (24시간 캐시)
- 국내주식 현재가 조회 (FHKST01010100)
- 실패 시 None 반환 → 호출측에서 yfinance 폴백
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests

from config.settings import (
    DB_DIR,
    KIS_APP_KEY,
    KIS_APP_SECRET,
    KIS_BASE_URL,
    PORTFOLIO,
)
from core.market_data_fetch import CacheSource, FetchErrorType, FetchResult, FetchStatus
from core.models import Quote

logger = logging.getLogger(__name__)

# ─── 토큰 캐시 (메모리 + 파일) ─────────────────────
_TOKEN_LOCK = threading.Lock()
_TOKEN_FILE = DB_DIR / "kis_token.json"

_mem_token: str = ""
_mem_expires: float = 0.0
# 토큰 발급 실패 negative cache — KIS 장애 시 quote 호출마다 10초 재시도로
# 대시보드 GET 전체가 타임아웃 되는 것을 방지 (fail-fast)
_TOKEN_FAIL_COOLDOWN_SEC = 60.0
_mem_token_fail_until: float = 0.0
_mem_token_fail_error_type: FetchErrorType | None = None


class _KISTokenFetchError(RuntimeError):
    """Typed token failure marker carrying only a safe classification."""

    def __init__(self, error_type: FetchErrorType) -> None:
        super().__init__("typed token fetch failed")
        self.error_type = error_type


class _KISTokenNetworkError(_KISTokenFetchError):
    """Backward-compatible typed marker for token transport failures."""

    def __init__(self) -> None:
        super().__init__(FetchErrorType.NETWORK)


def _raise_typed_token_failure(error_type: FetchErrorType) -> None:
    if error_type is FetchErrorType.NETWORK:
        raise _KISTokenNetworkError from None
    raise _KISTokenFetchError(error_type) from None


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
    except OSError as exc:
        logger.warning(
            "KIS 토큰 파일 저장 실패: exception_class=%s",
            type(exc).__name__,
        )


def _get_access_token(*, raise_on_network: bool = False) -> str | None:
    """OAuth 접근 토큰 발급 (메모리 → 파일 → API 순서로 조회).

    토큰 유효기간: 약 24시간. 만료 1시간 전에 갱신.
    KIS는 토큰 발급을 분당 1회로 제한하므로 파일 캐시 필수.
    """
    global _mem_token, _mem_expires, _mem_token_fail_until
    global _mem_token_fail_error_type

    now = time.time()

    # 1) 메모리 캐시 확인
    with _TOKEN_LOCK:
        if _mem_token and now < _mem_expires:
            return _mem_token
        if now < _mem_token_fail_until:
            # 최근 발급 실패 — 쿨다운 동안 분류를 보존해 즉시 반환한다.
            if raise_on_network and _mem_token_fail_error_type is not None:
                _raise_typed_token_failure(_mem_token_fail_error_type)
            return None

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
            with _TOKEN_LOCK:
                _mem_token_fail_until = time.time() + _TOKEN_FAIL_COOLDOWN_SEC
                _mem_token_fail_error_type = FetchErrorType.AUTH
            return None

        expires_at = now + expires_in - 3600  # 만료 1시간 전 갱신

        with _TOKEN_LOCK:
            _mem_token = token
            _mem_expires = expires_at
            _mem_token_fail_until = 0.0
            _mem_token_fail_error_type = None

        _save_token_to_file(token, expires_at)
        logger.info("KIS 접근 토큰 발급 성공 (만료: %ds)", expires_in)
        return token

    except requests.HTTPError as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        error_type = (
            FetchErrorType.AUTH
            if status_code in {401, 403}
            else FetchErrorType.HTTP
        )
        logger.warning(
            "KIS 토큰 발급 실패: error_type=%s exception_class=%s",
            error_type.value,
            type(exc).__name__,
        )
        with _TOKEN_LOCK:
            _mem_token_fail_until = time.time() + _TOKEN_FAIL_COOLDOWN_SEC
            _mem_token_fail_error_type = error_type
        if raise_on_network:
            _raise_typed_token_failure(error_type)
        return None
    except requests.RequestException as exc:
        logger.warning(
            "KIS 토큰 발급 실패: error_type=network exception_class=%s",
            type(exc).__name__,
        )
        with _TOKEN_LOCK:
            _mem_token_fail_until = time.time() + _TOKEN_FAIL_COOLDOWN_SEC
            _mem_token_fail_error_type = FetchErrorType.NETWORK
        if raise_on_network:
            _raise_typed_token_failure(FetchErrorType.NETWORK)
        return None


def _get_typed_access_token() -> str | None:
    return _get_access_token(raise_on_network=True)


def _ticker_to_kis_code(ticker: str) -> str:
    """yfinance 티커 → KIS 종목코드 변환.

    005930.KS → 005930
    """
    return ticker.replace(".KS", "").replace(".KQ", "")


class _KISParseError(ValueError):
    """원문을 노출하지 않고 typed fetcher가 분류할 수 있는 파싱 오류."""

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


def _payload_number(container: dict, key: str, *, nonnegative: bool = False) -> int | float:
    if key not in container:
        raise _KISParseError("malformed")
    raw = container[key]
    if isinstance(raw, bool) or raw is None:
        raise _KISParseError("numeric")
    text = str(raw).replace(",", "").strip()
    if not text:
        raise _KISParseError("numeric")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        raise _KISParseError("numeric") from None
    if not number.is_finite() or (nonnegative and number < 0):
        raise _KISParseError("numeric")
    return int(number) if number == number.to_integral_value() else float(number)


def _payload_share_count(
    container: dict, key: str, *, nonnegative: bool = False
) -> int:
    number = _payload_number(container, key, nonnegative=nonnegative)
    if not isinstance(number, int):
        raise _KISParseError("numeric")
    return number


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("fetch_completed_at_utc must be timezone-aware")
    return value.astimezone(timezone.utc)


def parse_kis_orderbook_payload(
    payload: dict, ticker: str, venue: str, fetch_completed_at_utc: datetime
) -> dict:
    """KIS 호가 payload를 10단계 typed 관측값으로 순수 변환한다."""
    if not isinstance(payload, dict):
        raise _KISParseError("malformed")
    output1 = payload.get("output1")
    if not isinstance(output1, dict) or not output1:
        raise _KISParseError("malformed")
    output2 = payload.get("output2")
    if output2 is None:
        output2 = {}
    if not isinstance(output2, dict):
        raise _KISParseError("malformed")

    levels: list[dict] = []
    for level in range(1, 11):
        levels.append(
            {
                "level": level,
                "ask_price": _payload_number(output1, f"askp{level}", nonnegative=True),
                "ask_size": _payload_share_count(
                    output1, f"askp_rsqn{level}", nonnegative=True
                ),
                "bid_price": _payload_number(output1, f"bidp{level}", nonnegative=True),
                "bid_size": _payload_share_count(
                    output1, f"bidp_rsqn{level}", nonnegative=True
                ),
            }
        )

    positive_asks = [row["ask_price"] for row in levels if row["ask_price"] > 0]
    positive_bids = [row["bid_price"] for row in levels if row["bid_price"] > 0]
    best_ask = positive_asks[0] if positive_asks else None
    best_bid = positive_bids[0] if positive_bids else None
    spread = best_ask - best_bid if best_ask is not None and best_bid is not None else None
    mid_price = (
        (best_ask + best_bid) / 2
        if best_ask is not None and best_bid is not None
        else None
    )
    spread_pct = (
        round(spread / mid_price * 100, 3)
        if spread is not None and mid_price not in (None, 0)
        else None
    )
    ask_depth = sum(
        row["ask_size"] for row in levels if row["ask_price"] > 0
    )
    bid_depth = sum(
        row["bid_size"] for row in levels if row["bid_price"] > 0
    )
    total_depth = ask_depth + bid_depth
    imbalance = (bid_depth - ask_depth) / total_depth if total_depth else None

    raw_total_keys = (
        "total_askp_rsqn",
        "total_bidp_rsqn",
        "ovtm_total_askp_rsqn",
        "ovtm_total_bidp_rsqn",
        "total_askp_rsqn_icdc",
        "total_bidp_rsqn_icdc",
    )
    raw_totals = {key: output1.get(key) for key in raw_total_keys}
    completed = _aware_utc(fetch_completed_at_utc)

    return {
        "ticker": ticker,
        "symbol": _ticker_to_kis_code(ticker),
        "venue": venue,
        "source_as_of": completed.isoformat(),
        "source_as_of_precision": "fetch_completion",
        "provider_time_hhmmss": output1.get("aspr_acpt_hour"),
        "intraday": True,
        "levels": levels,
        "raw_totals": raw_totals,
        "expected_execution": dict(output2),
        "best_ask": best_ask,
        "best_bid": best_bid,
        "best_ask_price_krw_per_share": best_ask,
        "best_bid_price_krw_per_share": best_bid,
        "spread": spread,
        "spread_krw_per_share": spread,
        "spread_pct": spread_pct,
        "mid_price": mid_price,
        "mid_price_krw_per_share": mid_price,
        "depth_ask_shares": ask_depth,
        "depth_bid_shares": bid_depth,
        "depth_total_shares": total_depth,
        "imbalance": imbalance,
        "imbalance_pct": round(imbalance * 100, 1) if imbalance is not None else None,
        "depth_status": "zero_depth" if total_depth == 0 else "available",
        "units": {
            "levels.ask_price": "KRW/share",
            "levels.bid_price": "KRW/share",
            "levels.ask_size": "shares",
            "levels.bid_size": "shares",
            "spread": "KRW/share",
            "mid_price": "KRW/share",
            "depth_ask_shares": "shares",
            "depth_bid_shares": "shares",
            "depth_total_shares": "shares",
            "imbalance": "ratio",
            "imbalance_pct": "percent",
            **{f"raw_totals.{key}": "provider_raw" for key in raw_total_keys},
        },
        "derived_schema_version": "1",
    }


_INVESTOR_NUMERIC_FIELDS = (
    "stck_clpr",
    "prdy_vrss",
    "prsn_ntby_qty",
    "frgn_ntby_qty",
    "orgn_ntby_qty",
    "prsn_ntby_tr_pbmn",
    "frgn_ntby_tr_pbmn",
    "orgn_ntby_tr_pbmn",
    "prsn_shnu_vol",
    "frgn_shnu_vol",
    "orgn_shnu_vol",
    "prsn_shnu_tr_pbmn",
    "frgn_shnu_tr_pbmn",
    "orgn_shnu_tr_pbmn",
    "prsn_seln_vol",
    "frgn_seln_vol",
    "orgn_seln_vol",
    "prsn_seln_tr_pbmn",
    "frgn_seln_tr_pbmn",
    "orgn_seln_tr_pbmn",
)

_INVESTOR_SEMANTIC_FIELDS = {
    "stck_clpr": "close",
    "prdy_vrss": "previous_day_change",
    "prsn_ntby_qty": "personal_net_qty",
    "frgn_ntby_qty": "foreign_net_qty",
    "orgn_ntby_qty": "institution_net_qty",
    "prsn_ntby_tr_pbmn": "personal_net_trade_amount",
    "frgn_ntby_tr_pbmn": "foreign_net_trade_amount",
    "orgn_ntby_tr_pbmn": "institution_net_trade_amount",
    "prsn_shnu_vol": "personal_buy_volume",
    "frgn_shnu_vol": "foreign_buy_volume",
    "orgn_shnu_vol": "institution_buy_volume",
    "prsn_shnu_tr_pbmn": "personal_buy_trade_amount",
    "frgn_shnu_tr_pbmn": "foreign_buy_trade_amount",
    "orgn_shnu_tr_pbmn": "institution_buy_trade_amount",
    "prsn_seln_vol": "personal_sell_volume",
    "frgn_seln_vol": "foreign_sell_volume",
    "orgn_seln_vol": "institution_sell_volume",
    "prsn_seln_tr_pbmn": "personal_sell_trade_amount",
    "frgn_seln_tr_pbmn": "foreign_sell_trade_amount",
    "orgn_seln_tr_pbmn": "institution_sell_trade_amount",
}


def parse_kis_investor_payload(payload: dict, ticker: str, venue: str) -> list[dict]:
    """KIS 일별 투자자 매매동향의 공식 필드를 typed 행으로 변환한다."""
    if not isinstance(payload, dict) or "output" not in payload:
        raise _KISParseError("malformed")
    output = payload["output"]
    if not isinstance(output, list):
        raise _KISParseError("malformed")

    kst = timezone(timedelta(hours=9))
    rows: list[dict] = []
    for raw_row in output:
        if not isinstance(raw_row, dict):
            raise _KISParseError("malformed")
        raw_date = raw_row.get("stck_bsop_date")
        sign = raw_row.get("prdy_vrss_sign")
        if not isinstance(raw_date, str) or len(raw_date) != 8 or not raw_date.isdigit():
            raise _KISParseError("malformed")
        if sign is None:
            raise _KISParseError("malformed")
        try:
            business_close = datetime.strptime(raw_date, "%Y%m%d").replace(
                hour=15, minute=30, tzinfo=kst
            )
        except ValueError:
            raise _KISParseError("malformed") from None

        official: dict[str, object] = {
            "stck_bsop_date": raw_date,
            "prdy_vrss_sign": str(sign),
        }
        for field in _INVESTOR_NUMERIC_FIELDS:
            parser = (
                _payload_share_count
                if field.endswith(("_qty", "_vol"))
                else _payload_number
            )
            official[field] = parser(raw_row, field)

        semantic = {
            semantic_name: official[official_name]
            for official_name, semantic_name in _INVESTOR_SEMANTIC_FIELDS.items()
        }
        volume_fields = {
            name: "shares"
            for name in semantic
            if name.endswith("_qty") or name.endswith("_volume")
        }
        amount_fields = {
            name: "KRW million"
            for name in semantic
            if name.endswith("_trade_amount")
        }
        rows.append(
            {
                "ticker": ticker,
                "symbol": _ticker_to_kis_code(ticker),
                "venue": venue,
                "date": raw_date,
                **semantic,
                "previous_day_sign": str(sign),
                "source_as_of": business_close.astimezone(timezone.utc).isoformat(),
                "source_as_of_precision": "business_date",
                "availability_as_of": None,
                "intraday": False,
                "official_fields": official,
                "units": {
                    "close": "KRW/share",
                    "previous_day_change": "KRW/share",
                    **volume_fields,
                    **amount_fields,
                },
                "derived_schema_version": "1",
            }
        )
    return rows


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
            source="kis",
            as_of=time.time(),
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
        volume = float(output.get("tvol", 0) or 0)  # 누적 거래량
        turnover = float(output.get("tamt", 0) or 0)  # 누적 거래대금
        previous_volume = float(output.get("pvol", 0) or 0)  # 전일 거래량

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
            source="kis",
            as_of=time.time(),
            volume=volume,
            turnover=turnover,
            previous_volume=previous_volume,
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
_KIS_ORDERBOOK_ENDPOINT = (
    "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
)
_KIS_ORDERBOOK_TR_ID = "FHKST01010200"
_KIS_INVESTOR_ENDPOINT = "/uapi/domestic-stock/v1/quotations/inquire-investor"
_KIS_INVESTOR_TR_ID = "FHKST01010900"


def _clock_now_utc(clock) -> datetime:
    value = clock()
    return _aware_utc(value)


def _system_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _configured_value(configured) -> bool:
    return bool(configured() if callable(configured) else configured)


def _typed_result(
    *,
    status: FetchStatus,
    endpoint: str,
    tr_id: str | None,
    venue: str,
    symbol: str,
    started: datetime,
    completed: datetime,
    error_type: FetchErrorType,
    cache_source: CacheSource,
    value,
    fallback_used: bool = False,
) -> FetchResult:
    return FetchResult(
        status=status,
        provider="KIS",
        endpoint=endpoint,
        tr_id=tr_id,
        venue=venue,
        symbol=symbol,
        started_at_utc=started,
        completed_at_utc=completed,
        error_type=error_type,
        cache_source=cache_source,
        fallback_used=fallback_used,
        value=value,
    )


def fetch_domestic_orderbook_result(
    ticker: str,
    venue: str = "J",
    clock=None,
    http_get=None,
    token_provider=None,
    configured=None,
) -> FetchResult[dict]:
    """공식 KIS 10단계 국내 호가를 안전한 typed 결과로 조회한다."""
    if clock is None:
        clock = _system_utc_now
    if http_get is None:
        http_get = requests.get
    if token_provider is None:
        token_provider = _get_typed_access_token
    if configured is None:
        configured = _is_kis_configured

    started = _clock_now_utc(clock)
    symbol = _ticker_to_kis_code(ticker)
    if not _configured_value(configured):
        return _typed_result(
            status=FetchStatus.SKIPPED,
            endpoint=_KIS_ORDERBOOK_ENDPOINT,
            tr_id=_KIS_ORDERBOOK_TR_ID,
            venue=venue,
            symbol=symbol,
            started=started,
            completed=_clock_now_utc(clock),
            error_type=FetchErrorType.NOT_CONFIGURED,
            cache_source=CacheSource.NONE,
            value=None,
        )

    try:
        token = token_provider()
    except _KISTokenFetchError as exc:
        logger.warning(
            "KIS typed 호가 토큰 실패: error_type=%s exception_class=%s",
            exc.error_type.value,
            type(exc).__name__,
        )
        return _typed_result(
            status=FetchStatus.FAILED,
            endpoint=_KIS_ORDERBOOK_ENDPOINT,
            tr_id=_KIS_ORDERBOOK_TR_ID,
            venue=venue,
            symbol=symbol,
            started=started,
            completed=_clock_now_utc(clock),
            error_type=exc.error_type,
            cache_source=(
                CacheSource.NONE
                if exc.error_type is FetchErrorType.AUTH
                else CacheSource.NETWORK
            ),
            value=None,
        )
    except requests.RequestException as exc:
        logger.warning(
            "KIS typed 호가 토큰 네트워크 실패: "
            "error_type=network exception_class=%s",
            type(exc).__name__,
        )
        return _typed_result(
            status=FetchStatus.FAILED,
            endpoint=_KIS_ORDERBOOK_ENDPOINT,
            tr_id=_KIS_ORDERBOOK_TR_ID,
            venue=venue,
            symbol=symbol,
            started=started,
            completed=_clock_now_utc(clock),
            error_type=FetchErrorType.NETWORK,
            cache_source=CacheSource.NETWORK,
            value=None,
        )
    except Exception as exc:
        logger.warning(
            "KIS typed 호가 인증 실패: error_type=auth exception_class=%s",
            type(exc).__name__,
        )
        return _typed_result(
            status=FetchStatus.FAILED,
            endpoint=_KIS_ORDERBOOK_ENDPOINT,
            tr_id=_KIS_ORDERBOOK_TR_ID,
            venue=venue,
            symbol=symbol,
            started=started,
            completed=_clock_now_utc(clock),
            error_type=FetchErrorType.AUTH,
            cache_source=CacheSource.NONE,
            value=None,
        )
    if not token:
        return _typed_result(
            status=FetchStatus.FAILED,
            endpoint=_KIS_ORDERBOOK_ENDPOINT,
            tr_id=_KIS_ORDERBOOK_TR_ID,
            venue=venue,
            symbol=symbol,
            started=started,
            completed=_clock_now_utc(clock),
            error_type=FetchErrorType.AUTH,
            cache_source=CacheSource.NONE,
            value=None,
        )

    url = f"{KIS_BASE_URL}{_KIS_ORDERBOOK_ENDPOINT}"
    try:
        response = http_get(
            url,
            headers={
                "authorization": f"Bearer {token}",
                "appkey": KIS_APP_KEY,
                "appsecret": KIS_APP_SECRET,
                "tr_id": _KIS_ORDERBOOK_TR_ID,
                "content-type": "application/json; charset=utf-8",
            },
            params={
                "FID_COND_MRKT_DIV_CODE": venue,
                "FID_INPUT_ISCD": symbol,
            },
            timeout=10,
        )
    except Exception as exc:
        logger.warning(
            "KIS typed 호가 네트워크 실패: error_type=network exception_class=%s",
            type(exc).__name__,
        )
        return _typed_result(
            status=FetchStatus.FAILED,
            endpoint=_KIS_ORDERBOOK_ENDPOINT,
            tr_id=_KIS_ORDERBOOK_TR_ID,
            venue=venue,
            symbol=symbol,
            started=started,
            completed=_clock_now_utc(clock),
            error_type=FetchErrorType.NETWORK,
            cache_source=CacheSource.NETWORK,
            value=None,
        )

    try:
        response.raise_for_status()
        if not 200 <= int(getattr(response, "status_code", 200)) < 300:
            raise RuntimeError("http status")
    except Exception as exc:
        logger.warning(
            "KIS typed 호가 HTTP 실패: error_type=http exception_class=%s",
            type(exc).__name__,
        )
        return _typed_result(
            status=FetchStatus.FAILED,
            endpoint=_KIS_ORDERBOOK_ENDPOINT,
            tr_id=_KIS_ORDERBOOK_TR_ID,
            venue=venue,
            symbol=symbol,
            started=started,
            completed=_clock_now_utc(clock),
            error_type=FetchErrorType.HTTP,
            cache_source=CacheSource.NETWORK,
            value=None,
        )

    try:
        payload = response.json()
    except Exception as exc:
        logger.warning(
            "KIS typed 호가 payload 실패: error_type=malformed exception_class=%s",
            type(exc).__name__,
        )
        return _typed_result(
            status=FetchStatus.FAILED,
            endpoint=_KIS_ORDERBOOK_ENDPOINT,
            tr_id=_KIS_ORDERBOOK_TR_ID,
            venue=venue,
            symbol=symbol,
            started=started,
            completed=_clock_now_utc(clock),
            error_type=FetchErrorType.MALFORMED,
            cache_source=CacheSource.NETWORK,
            value=None,
        )

    completed = _clock_now_utc(clock)
    if not isinstance(payload, dict):
        return _typed_result(
            status=FetchStatus.FAILED,
            endpoint=_KIS_ORDERBOOK_ENDPOINT,
            tr_id=_KIS_ORDERBOOK_TR_ID,
            venue=venue,
            symbol=symbol,
            started=started,
            completed=completed,
            error_type=FetchErrorType.MALFORMED,
            cache_source=CacheSource.NETWORK,
            value=None,
        )
    if payload.get("rt_cd") != "0":
        return _typed_result(
            status=FetchStatus.FAILED,
            endpoint=_KIS_ORDERBOOK_ENDPOINT,
            tr_id=_KIS_ORDERBOOK_TR_ID,
            venue=venue,
            symbol=symbol,
            started=started,
            completed=completed,
            error_type=FetchErrorType.PROVIDER,
            cache_source=CacheSource.NETWORK,
            value=None,
        )
    try:
        value = parse_kis_orderbook_payload(payload, ticker, venue, completed)
    except _KISParseError as exc:
        error_type = (
            FetchErrorType.NUMERIC
            if exc.error_type == "numeric"
            else FetchErrorType.MALFORMED
        )
        return _typed_result(
            status=FetchStatus.FAILED,
            endpoint=_KIS_ORDERBOOK_ENDPOINT,
            tr_id=_KIS_ORDERBOOK_TR_ID,
            venue=venue,
            symbol=symbol,
            started=started,
            completed=completed,
            error_type=error_type,
            cache_source=CacheSource.NETWORK,
            value=None,
        )
    if value["depth_status"] == "zero_depth":
        return _typed_result(
            status=FetchStatus.INCOMPLETE,
            endpoint=_KIS_ORDERBOOK_ENDPOINT,
            tr_id=_KIS_ORDERBOOK_TR_ID,
            venue=venue,
            symbol=symbol,
            started=started,
            completed=completed,
            error_type=FetchErrorType.ZERO_DEPTH,
            cache_source=CacheSource.NETWORK,
            value=value,
        )
    return _typed_result(
        status=FetchStatus.SUCCESS,
        endpoint=_KIS_ORDERBOOK_ENDPOINT,
        tr_id=_KIS_ORDERBOOK_TR_ID,
        venue=venue,
        symbol=symbol,
        started=started,
        completed=completed,
        error_type=FetchErrorType.NONE,
        cache_source=CacheSource.NETWORK,
        value=value,
    )


def fetch_domestic_investor_result(
    ticker: str,
    venue: str = "J",
    clock=None,
    http_get=None,
    token_provider=None,
    configured=None,
) -> FetchResult[list[dict]]:
    """장 종료 후 제공되는 KIS 일별 투자자 매매동향을 typed 조회한다."""
    if clock is None:
        clock = _system_utc_now
    if http_get is None:
        http_get = requests.get
    if token_provider is None:
        token_provider = _get_typed_access_token
    if configured is None:
        configured = _is_kis_configured

    started = _clock_now_utc(clock)
    symbol = _ticker_to_kis_code(ticker)
    common = {
        "endpoint": _KIS_INVESTOR_ENDPOINT,
        "tr_id": _KIS_INVESTOR_TR_ID,
        "venue": venue,
        "symbol": symbol,
        "started": started,
    }
    if not _configured_value(configured):
        return _typed_result(
            **common,
            status=FetchStatus.SKIPPED,
            completed=_clock_now_utc(clock),
            error_type=FetchErrorType.NOT_CONFIGURED,
            cache_source=CacheSource.NONE,
            value=None,
        )

    try:
        token = token_provider()
    except _KISTokenFetchError as exc:
        logger.warning(
            "KIS typed 투자자 토큰 실패: error_type=%s exception_class=%s",
            exc.error_type.value,
            type(exc).__name__,
        )
        return _typed_result(
            **common,
            status=FetchStatus.FAILED,
            completed=_clock_now_utc(clock),
            error_type=exc.error_type,
            cache_source=(
                CacheSource.NONE
                if exc.error_type is FetchErrorType.AUTH
                else CacheSource.NETWORK
            ),
            value=None,
        )
    except requests.RequestException as exc:
        logger.warning(
            "KIS typed 투자자 토큰 네트워크 실패: "
            "error_type=network exception_class=%s",
            type(exc).__name__,
        )
        return _typed_result(
            **common,
            status=FetchStatus.FAILED,
            completed=_clock_now_utc(clock),
            error_type=FetchErrorType.NETWORK,
            cache_source=CacheSource.NETWORK,
            value=None,
        )
    except Exception as exc:
        logger.warning(
            "KIS typed 투자자 인증 실패: error_type=auth exception_class=%s",
            type(exc).__name__,
        )
        return _typed_result(
            **common,
            status=FetchStatus.FAILED,
            completed=_clock_now_utc(clock),
            error_type=FetchErrorType.AUTH,
            cache_source=CacheSource.NONE,
            value=None,
        )
    if not token:
        return _typed_result(
            **common,
            status=FetchStatus.FAILED,
            completed=_clock_now_utc(clock),
            error_type=FetchErrorType.AUTH,
            cache_source=CacheSource.NONE,
            value=None,
        )

    try:
        response = http_get(
            f"{KIS_BASE_URL}{_KIS_INVESTOR_ENDPOINT}",
            headers={
                "authorization": f"Bearer {token}",
                "appkey": KIS_APP_KEY,
                "appsecret": KIS_APP_SECRET,
                "tr_id": _KIS_INVESTOR_TR_ID,
                "content-type": "application/json; charset=utf-8",
            },
            params={
                "FID_COND_MRKT_DIV_CODE": venue,
                "FID_INPUT_ISCD": symbol,
            },
            timeout=10,
        )
    except Exception as exc:
        logger.warning(
            "KIS typed 투자자 네트워크 실패: error_type=network exception_class=%s",
            type(exc).__name__,
        )
        return _typed_result(
            **common,
            status=FetchStatus.FAILED,
            completed=_clock_now_utc(clock),
            error_type=FetchErrorType.NETWORK,
            cache_source=CacheSource.NETWORK,
            value=None,
        )

    try:
        response.raise_for_status()
        if not 200 <= int(getattr(response, "status_code", 200)) < 300:
            raise RuntimeError("http status")
    except Exception as exc:
        logger.warning(
            "KIS typed 투자자 HTTP 실패: error_type=http exception_class=%s",
            type(exc).__name__,
        )
        return _typed_result(
            **common,
            status=FetchStatus.FAILED,
            completed=_clock_now_utc(clock),
            error_type=FetchErrorType.HTTP,
            cache_source=CacheSource.NETWORK,
            value=None,
        )

    try:
        payload = response.json()
    except Exception as exc:
        logger.warning(
            "KIS typed 투자자 payload 실패: error_type=malformed exception_class=%s",
            type(exc).__name__,
        )
        return _typed_result(
            **common,
            status=FetchStatus.FAILED,
            completed=_clock_now_utc(clock),
            error_type=FetchErrorType.MALFORMED,
            cache_source=CacheSource.NETWORK,
            value=None,
        )

    completed = _clock_now_utc(clock)
    if not isinstance(payload, dict):
        return _typed_result(
            **common,
            status=FetchStatus.FAILED,
            completed=completed,
            error_type=FetchErrorType.MALFORMED,
            cache_source=CacheSource.NETWORK,
            value=None,
        )
    if payload.get("rt_cd") != "0":
        return _typed_result(
            **common,
            status=FetchStatus.FAILED,
            completed=completed,
            error_type=FetchErrorType.PROVIDER,
            cache_source=CacheSource.NETWORK,
            value=None,
        )
    try:
        rows = parse_kis_investor_payload(payload, ticker, venue)
    except _KISParseError as exc:
        error_type = (
            FetchErrorType.NUMERIC
            if exc.error_type == "numeric"
            else FetchErrorType.MALFORMED
        )
        return _typed_result(
            **common,
            status=FetchStatus.FAILED,
            completed=completed,
            error_type=error_type,
            cache_source=CacheSource.NETWORK,
            value=None,
        )

    for row in rows:
        row["availability_as_of"] = completed.isoformat()
    return _typed_result(
        **common,
        status=FetchStatus.SUCCESS if rows else FetchStatus.EMPTY,
        completed=completed,
        error_type=FetchErrorType.NONE,
        cache_source=CacheSource.NETWORK,
        value=rows,
    )


def _valid_legacy_orderbook_level(row: object, expected_level: int) -> bool:
    if not isinstance(row, dict) or row.get("level") != expected_level:
        return False
    for price_key in ("ask_price", "bid_price"):
        value = row.get(price_key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            return False
    for size_key in ("ask_size", "bid_size"):
        value = row.get(size_key)
        if type(value) is not int or value < 0:
            return False
    return True


def get_domestic_orderbook(ticker: str) -> dict | None:
    """typed 성공 호가만 기존 5단계 dashboard 계약으로 투영한다."""
    result = fetch_domestic_orderbook_result(ticker)
    if result.status is not FetchStatus.SUCCESS or not isinstance(result.value, dict):
        return None

    levels = result.value.get("levels")
    if (
        not isinstance(levels, list)
        or len(levels) != 10
        or not all(
            _valid_legacy_orderbook_level(row, level)
            for level, row in enumerate(levels, start=1)
        )
    ):
        return None
    first_five = levels[:5]
    asks = [
        {"price": round(row["ask_price"]), "size": int(row["ask_size"])}
        for row in first_five
        if row.get("ask_price", 0) > 0
    ]
    bids = [
        {"price": round(row["bid_price"]), "size": int(row["bid_size"])}
        for row in first_five
        if row.get("bid_price", 0) > 0
    ]

    best_ask = asks[0]["price"] if asks else 0
    best_bid = bids[0]["price"] if bids else 0
    spread = best_ask - best_bid if best_ask and best_bid else 0
    mid_price = (best_ask + best_bid) / 2 if best_ask and best_bid else 0
    spread_pct = round(spread / mid_price * 100, 3) if mid_price else 0
    total_bid = sum(row["size"] for row in bids)
    total_ask = sum(row["size"] for row in asks)
    total = total_bid + total_ask
    imbalance_pct = (
        round((total_bid - total_ask) / total * 100, 1) if total else 0
    )

    if spread_pct <= 0.2:
        exec_label = "체결 리스크 낮음"
        liq_label = "유동성 양호"
    elif spread_pct <= 0.7:
        exec_label = "스프레드 주의"
        liq_label = "유동성 보통"
    else:
        exec_label = "유동성 주의"
        liq_label = "호가 얇음"

    kst = timezone(timedelta(hours=9))
    return {
        "ticker": ticker,
        "source": "KIS",
        "updated_at": result.completed_at_utc.astimezone(kst).strftime(
            "%Y-%m-%dT%H:%M:%S"
        ),
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


def get_domestic_short_sale(
    ticker: str, start_date: str, end_date: str
) -> list[dict] | None:
    """KIS API 국내주식 공매도 일별추이 조회 (FHPST04830000). read-only 판단 보조용.

    Args:
        ticker: yfinance 형식 티커 (예: 462870.KS)
        start_date/end_date: YYYYMMDD

    Returns:
        최신순 dict 리스트 [{"date", "close", "short_qty", "short_ratio_pct",
        "cum_short_qty", "cum_short_ratio_pct"}, ...] 또는 실패 시 None.
        비고: KRX 공매도 '잔고'(로그인 게이트)가 아닌 '거래' 기반 지표.
    """
    if not _is_kis_configured():
        return None

    token = _get_access_token()
    if not token:
        return None

    stock_code = _ticker_to_kis_code(ticker)

    try:
        resp = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/daily-short-sale",
            headers={
                "authorization": f"Bearer {token}",
                "appkey": KIS_APP_KEY,
                "appsecret": KIS_APP_SECRET,
                "tr_id": "FHPST04830000",
                "content-type": "application/json; charset=utf-8",
            },
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": stock_code,
                "FID_INPUT_DATE_1": start_date,
                "FID_INPUT_DATE_2": end_date,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("rt_cd") != "0":
            logger.warning(
                "KIS 공매도 조회 실패 [%s]: %s", ticker, data.get("msg1", "unknown")
            )
            return None

        rows = data.get("output2") or data.get("output") or []
        result: list[dict] = []
        for r in rows:
            try:
                result.append({
                    "date": str(r.get("stck_bsop_date", "")),
                    "close": float(r.get("stck_clpr", 0)),
                    "short_qty": int(float(r.get("ssts_cntg_qty", 0))),
                    "short_ratio_pct": float(r.get("ssts_vol_rlim", 0)),
                    "cum_short_qty": int(float(r.get("acml_ssts_cntg_qty", 0))),
                    "cum_short_ratio_pct": float(r.get("acml_ssts_cntg_qty_rlim", 0)),
                })
            except (ValueError, TypeError):
                continue
        return result or None

    except requests.RequestException as e:
        logger.warning("KIS 공매도 조회 실패 [%s]: %s", ticker, e)
        return None
