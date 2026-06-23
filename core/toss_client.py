"""
Toss Securities Open API — read-only client

OAuth2 인증 + 계좌/잔고/환율/캘린더 조회만 수행.
read-only GET 호출만 허용. 변경성 API 호출 금지.
토큰은 메모리 캐시만 사용 (파일 저장 금지).
"""

from __future__ import annotations

import base64
import logging
import os
import re
import threading
import time

import requests

logger = logging.getLogger(__name__)

# ─── 환경변수 ────────────────────────────────────────
TOSS_APP_KEY: str = os.environ.get("TOSS_APP_KEY", "")
TOSS_APP_SECRET: str = os.environ.get("TOSS_APP_SECRET", "")
TOSS_ACCOUNT_NO: str = os.environ.get("TOSS_ACCOUNT_NO", "")
TOSS_BASE_URL: str = os.environ.get("TOSS_BASE_URL", "").rstrip("/")

TIMEOUT = 10

# ─── 토큰 메모리 캐시 (파일 저장 금지) ──────────────
_token_lock = threading.Lock()
_mem_token: str = ""
_mem_expires: float = 0.0


def is_configured() -> bool:
    """Toss API 키가 설정되어 있는지 확인."""
    return bool(TOSS_APP_KEY and TOSS_APP_SECRET and TOSS_BASE_URL)


# ─── 민감정보 마스킹 ────────────────────────────────
_SENSITIVE_KEYS: set[str] = {
    "access_token", "refresh_token", "token", "authorization",
    "accountno", "accountnumber", "account_number", "account",
    "account_id", "accountid",
    "appkey", "appsecret", "clientsecret", "secret", "key", "password",
}
_LONG_NUM_RE = re.compile(r"\b\d{8,}\b")


def _is_sensitive_key(k: str) -> bool:
    return k.lower().replace("-", "_") in _SENSITIVE_KEYS


def sanitize_dict(data: object) -> object:
    """재귀적 민감정보 마스킹."""
    if isinstance(data, dict):
        return {
            k: "[REDACTED]" if _is_sensitive_key(k) else sanitize_dict(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [sanitize_dict(item) for item in data]
    if isinstance(data, str):
        return _LONG_NUM_RE.sub("[NUM_REDACTED]", data)
    return data


# ─── OAuth2 토큰 발급 ───────────────────────────────
def _get_access_token() -> str | None:
    """OAuth2 client_credentials 토큰. 메모리 캐시, 만료 5분 전 갱신."""
    global _mem_token, _mem_expires

    now = time.time()
    with _token_lock:
        if _mem_token and now < _mem_expires:
            return _mem_token

    if not is_configured():
        return None

    cred = base64.b64encode(
        f"{TOSS_APP_KEY}:{TOSS_APP_SECRET}".encode()
    ).decode()
    try:
        resp = requests.post(
            f"{TOSS_BASE_URL}/oauth2/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {cred}",
            },
            data={"grant_type": "client_credentials"},
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("Toss token failed: status=%d", resp.status_code)
            return None
        body = resp.json()
        token = body.get("access_token", "")
        expires_in = int(body.get("expires_in", 3600))
        if not token:
            return None

        with _token_lock:
            _mem_token = token
            _mem_expires = now + expires_in - 300  # 5분 전 갱신
        return token

    except requests.RequestException as e:
        logger.warning("Toss token error: %s", str(e)[:100])
        return None


# ─── GET-only API 호출 ──────────────────────────────
def _get(path: str, account_seq: str = "", params: dict | None = None) -> dict | list | None:
    """GET-only API 호출. 실패 시 None 반환."""
    token = _get_access_token()
    if not token:
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if account_seq:
        headers["X-Tossinvest-Account"] = str(account_seq)

    try:
        resp = requests.get(
            f"{TOSS_BASE_URL}{path}",
            headers=headers,
            params=params or {},
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("Toss GET %s failed: %d", path, resp.status_code)
            return None
        return resp.json()
    except requests.RequestException as e:
        logger.warning("Toss GET %s error: %s", path, str(e)[:100])
        return None


# ─── 공개 read-only 메서드 ──────────────────────────
def get_accounts() -> list[dict]:
    """계좌 목록 조회. [{accountNo, accountSeq, accountType}, ...]"""
    data = _get("/api/v1/accounts")
    if not data:
        return []
    result = data.get("result", []) if isinstance(data, dict) else []
    return result if isinstance(result, list) else []


def get_holdings(account_seq: str) -> dict:
    """보유종목 조회. {totalPurchaseAmount, marketValue, profitLoss, items, ...}"""
    data = _get("/api/v1/holdings", account_seq=account_seq)
    if not data:
        return {}
    result = data.get("result", {}) if isinstance(data, dict) else {}
    return result if isinstance(result, dict) else {}


def get_exchange_rate(base_currency: str = "USD", quote_currency: str = "KRW") -> dict:
    """환율 조회. {baseCurrency, quoteCurrency, rate, ...}"""
    data = _get("/api/v1/exchange-rate", params={
        "baseCurrency": base_currency,
        "quoteCurrency": quote_currency,
    })
    if not data:
        return {}
    result = data.get("result", {}) if isinstance(data, dict) else {}
    return result if isinstance(result, dict) else {}


def get_market_calendar(market: str = "KR") -> dict:
    """장 캘린더 조회. market: KR 또는 US"""
    data = _get(f"/api/v1/market-calendar/{market}")
    if not data:
        return {}
    result = data.get("result", {}) if isinstance(data, dict) else {}
    return result if isinstance(result, dict) else {}
