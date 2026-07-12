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
import sys
import threading
import time

import requests

logger = logging.getLogger(__name__)

# ─── .env 안전 로드 (기존 환경변수 override 안 함) ───
from pathlib import Path as _Path
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_Path(__file__).resolve().parents[1] / ".env", override=False)
except Exception:
    pass

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


def _invalidate_access_token(expected_token: str | None = None) -> None:
    """Drop cached OAuth token so the next call fetches a fresh one.

    Toss can invalidate a token before our local expiry clock. Long-running
    processes must recover from GET 401 without hammering /accounts forever.

    expected_token(세대 안전장치): 401을 받은 요청이 실제로 사용한 토큰을
    넘기면, 캐시가 이미 다른 스레드의 새 토큰으로 교체된 경우 지우지 않는다.
    Toss는 client당 유효 토큰이 1개라 늦게 도착한 과거 401이 최신 토큰을
    지우면 발급 폭주(각 발급이 직전 토큰을 무효화)로 번진다.
    """
    global _mem_token, _mem_expires
    with _token_lock:
        if expected_token is not None and _mem_token != expected_token:
            return  # 이미 새 세대 토큰 — 과거 401이 최신 토큰을 지우지 못함
        _mem_token = ""
        _mem_expires = 0.0


def is_configured() -> bool:
    """Toss API 키가 설정되어 있는지 확인."""
    return bool(TOSS_APP_KEY and TOSS_APP_SECRET and TOSS_BASE_URL)


def _broker_access_isolated_for_process() -> bool:
    """자율운영 중 비소유 프로세스의 Toss OAuth/Broker 접근을 전역 차단한다.

    Toss client_credentials는 새 토큰 발급이 이전 토큰을 무효화할 수 있다.
    따라서 자율운영 중에는 장기 실행되는 ``main.py bot``/``monitor``만 GET
    소유자로 두고 dashboard·briefing·도구 프로세스는 sanitized snapshot을
    소비한다. endpoint별 격리에 의존하지 않고 toss_client 경계에서
    fail-closed한다. 주문 전송·재전송 정책은 이 함수가 변경하지 않는다.
    """
    try:
        from core.toss_readonly_snapshot import should_consume_snapshot
        return should_consume_snapshot()
    except Exception:
        # Snapshot 정책 import가 깨지면 기존 dashboard 격리는 최소한 유지한다.
        args = {str(arg).strip().lower() for arg in sys.argv[1:]}
        autonomous = str(os.environ.get("TOSS_AUTONOMOUS_MODE", "")).strip().lower() in {
            "1", "true", "yes", "on", "y",
        }
        return "dashboard" in args and autonomous


# ─── 민감정보 마스킹 ────────────────────────────────
_SENSITIVE_KEYS: set[str] = {
    "access_token", "refresh_token", "token", "authorization",
    "accountno", "accountnumber", "account_number", "account",
    "account_id", "accountid",
    "appkey", "appsecret", "clientsecret", "secret", "key", "password",
}
_LONG_NUM_RE = re.compile(r"\b\d{8,}\b")
_SAFE_CLIENT_ORDER_ID_RE = re.compile(r"^tlive_[A-Za-z0-9_-]{1,30}$")
_CLIENT_ORDER_ID_KEYS = {"clientorderid", "client_order_id"}


def _is_sensitive_key(k: str) -> bool:
    return k.lower().replace("-", "_") in _SENSITIVE_KEYS


def sanitize_dict(data: object) -> object:
    """재귀적 민감정보 마스킹. 안전한 local clientOrderId만 보존."""
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            normalized = str(key).lower().replace("-", "_")
            if _is_sensitive_key(str(key)):
                out[key] = "[REDACTED]"
            elif normalized in _CLIENT_ORDER_ID_KEYS:
                text = str(value or "")
                out[key] = text if _SAFE_CLIENT_ORDER_ID_RE.fullmatch(text) else "[REDACTED]"
            else:
                out[key] = sanitize_dict(value)
        return out
    if isinstance(data, list):
        return [sanitize_dict(item) for item in data]
    if isinstance(data, str):
        return _LONG_NUM_RE.sub("[NUM_REDACTED]", data)
    return data


# ─── OAuth2 토큰 발급 ───────────────────────────────
def _get_access_token() -> str | None:
    """OAuth2 client_credentials 토큰. 메모리 캐시, 만료 5분 전 갱신.

    [singleflight] Toss는 client당 유효 access token이 1개다 — 새 발급이
    직전 토큰을 즉시 무효화하므로, 여러 스레드가 동시에 만료를 감지해도
    실제 발급 POST는 1회만 나가야 한다. 발급 네트워크 호출까지 _token_lock
    안에서 직렬화하고, lock 획득 직후 캐시를 재확인(double-check)한다.
    대기 스레드는 첫 스레드의 발급이 끝나면 같은 토큰을 캐시에서 받는다.
    네트워크 호출은 TIMEOUT으로 bounded — 예외 시 with 블록이 lock을 해제한다.
    """
    global _mem_token, _mem_expires

    # dashboard 격리: 캐시 확인보다 먼저 차단 — 남아 있는 유효 _mem_token도
    # 반환하지 않고, token 발급 네트워크에도 절대 도달하지 않는다 (fail-closed)
    if _broker_access_isolated_for_process():
        return None

    with _token_lock:
        # double-check: 발급 대기 중 다른 스레드가 이미 갱신했으면 그 토큰 사용
        if _mem_token and time.time() < _mem_expires:
            return _mem_token

        if not is_configured():
            return None

        now = time.time()
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

            _mem_token = token
            _mem_expires = now + expires_in - 300  # 5분 전 갱신
            return token

        except requests.RequestException as e:
            logger.warning("Toss token error: %s", str(e)[:100])
            return None


# ─── GET-only API 호출 ──────────────────────────────
def _get(path: str, account_seq: str = "", params: dict | None = None) -> dict | list | None:
    """GET-only API 호출. 실패 시 None 반환.

    401은 장기 프로세스의 stale token에서 자주 발생한다. 이때 캐시를
    폐기하고 새 토큰으로 한 번만 재시도한다. 429/기타 오류는 즉시
    반환해 tight retry loop/rate-limit 증폭을 막는다.
    """

    def _request_once(token: str):
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if account_seq:
            headers["X-Tossinvest-Account"] = str(account_seq)
        return requests.get(
            f"{TOSS_BASE_URL}{path}",
            headers=headers,
            params=params or {},
            timeout=TIMEOUT,
        )

    token = _get_access_token()
    if not token:
        return None

    try:
        resp = _request_once(token)
        if resp.status_code == 401:
            logger.warning("Toss GET %s failed: 401 — refreshing token once", path)
            # 세대 안전: 이 요청이 쓴 토큰일 때만 캐시 폐기 — 다른 스레드가
            # 이미 갱신한 최신 토큰을 과거 401 응답이 지우지 못하게 한다
            _invalidate_access_token(expected_token=token)
            token = _get_access_token()
            if not token:
                return None
            resp = _request_once(token)
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


def get_buying_power(account_seq: str, currency: str = "KRW") -> dict:
    """현금/예수금 조회. {currency, cashBuyingPower}"""
    data = _get("/api/v1/buying-power", account_seq=account_seq, params={
        "currency": currency,
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
