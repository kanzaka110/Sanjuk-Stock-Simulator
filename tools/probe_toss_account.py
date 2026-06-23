"""
Toss Securities Open API — read-only account probe

인증 → 계좌 기본정보 → 잔고/보유종목 순서로 조회.
read-only GET 호출만 수행. 변경성 API 호출 금지.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

# ─── 프로젝트 루트에서 .env 로드 ─────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# ─── 상수 ────────────────────────────────────────────
TIMEOUT = 10

# read-only 엔드포인트만 허용 (GET only)
# needs_account: True면 X-Tossinvest-Account 헤더 필요
# extra_params: 추가 query params
READ_ONLY_ENDPOINTS: list[tuple[str, str, bool, dict]] = [
    ("계좌 목록", "/api/v1/accounts", False, {}),
    ("보유종목", "/api/v1/holdings", True, {}),
    ("환율", "/api/v1/exchange-rate", False, {
        "baseCurrency": "USD", "quoteCurrency": "KRW",
    }),
    ("한국장 캘린더", "/api/v1/market-calendar/KR", False, {}),
    ("미국장 캘린더", "/api/v1/market-calendar/US", False, {}),
]

# 민감 키 (소문자, 언더스코어 정규화 후 매칭)
_SENSITIVE_KEYS: set[str] = {
    "access_token", "refresh_token", "token", "authorization",
    "accountno", "accountnumber", "account_number", "account", "account_id", "accountid",
    "appkey", "appsecret", "clientsecret", "secret", "key", "password",
}

# 8자리 이상 연속 숫자 패턴 (계좌번호 가능성)
_LONG_NUM_RE = re.compile(r"\b\d{8,}\b")


# ─── 마스킹 유틸 ─────────────────────────────────────
def mask_value(val: str, show_last: int = 0) -> str:
    """민감정보 마스킹. show_last>0이면 뒤 N자리만 표시."""
    if not val:
        return "NOT_SET"
    if show_last > 0 and len(val) > show_last:
        return f"***{val[-show_last:]} (len={len(val)})"
    return f"FOUND (len={len(val)})"


def _normalize_key(k: str) -> str:
    """키를 소문자+언더스코어로 정규화."""
    return k.lower().replace("-", "_")


def _is_sensitive_key(k: str) -> bool:
    """민감 키 여부 판별."""
    return _normalize_key(k) in _SENSITIVE_KEYS


def _mask_long_numbers(s: str) -> str:
    """8자리 이상 연속 숫자를 마스킹."""
    return _LONG_NUM_RE.sub("[NUM_REDACTED]", s)


def _sanitize_value(v: object) -> object:
    """값을 재귀적으로 sanitize."""
    if isinstance(v, dict):
        return _sanitize_dict(v)
    if isinstance(v, list):
        return [_sanitize_value(item) for item in v]
    if isinstance(v, str):
        return _mask_long_numbers(v)
    return v


def _sanitize_dict(data: dict) -> dict:
    """dict를 재귀적으로 sanitize. 민감 키는 [REDACTED]."""
    sanitized = {}
    for k, v in data.items():
        if _is_sensitive_key(k):
            sanitized[k] = "[REDACTED]"
        else:
            sanitized[k] = _sanitize_value(v)
    return sanitized


def sanitize_response(data: object, max_chars: int = 500) -> str:
    """응답을 재귀적으로 sanitize 후 JSON 문자열로 반환."""
    if isinstance(data, dict):
        sanitized = _sanitize_dict(data)
    elif isinstance(data, list):
        sanitized = [_sanitize_value(item) for item in data]
    else:
        sanitized = data
    raw = json.dumps(sanitized, ensure_ascii=False, default=str)
    raw = _mask_long_numbers(raw)
    if len(raw) > max_chars:
        raw = raw[:max_chars] + "...(truncated)"
    return raw


# ─── 환경변수 확인 ───────────────────────────────────
def check_env() -> dict[str, str]:
    """TOSS_ 환경변수 로드 및 존재 확인."""
    keys = {
        "TOSS_APP_KEY": os.environ.get("TOSS_APP_KEY", ""),
        "TOSS_APP_SECRET": os.environ.get("TOSS_APP_SECRET", ""),
        "TOSS_ACCOUNT_NO": os.environ.get("TOSS_ACCOUNT_NO", ""),
        "TOSS_BASE_URL": os.environ.get("TOSS_BASE_URL", ""),
        "TOSS_MODE": os.environ.get("TOSS_MODE", ""),
    }
    return keys


def print_env_status(env: dict[str, str]) -> bool:
    """환경변수 상태 출력. 모두 있으면 True."""
    print("═══ Toss env check ═══")
    all_ok = True
    for name, val in env.items():
        if name == "TOSS_ACCOUNT_NO":
            status = mask_value(val, show_last=4)
        elif name in ("TOSS_BASE_URL", "TOSS_MODE"):
            if name == "TOSS_BASE_URL" and val:
                p = urlparse(val)
                status = f"{p.scheme}://{p.hostname}"
            else:
                status = val or "NOT_SET"
        else:
            status = mask_value(val)
        present = "OK" if val else "MISSING"
        if not val and name != "TOSS_MODE":
            all_ok = False
        print(f"  {name}: {present} — {status}")
    return all_ok


# ─── OAuth2 토큰 발급 ───────────────────────────────
def get_access_token(base_url: str, app_key: str, app_secret: str) -> tuple[str | None, dict]:
    """OAuth2 client_credentials로 토큰 발급. 토큰은 절대 출력하지 않음."""
    cred = base64.b64encode(f"{app_key}:{app_secret}".encode()).decode()
    try:
        resp = requests.post(
            f"{base_url}/oauth2/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {cred}",
            },
            data={"grant_type": "client_credentials"},
            timeout=TIMEOUT,
        )
        body = resp.json()
        info = {
            "status": resp.status_code,
            "keys": list(body.keys()),
        }
        if resp.status_code == 200 and "access_token" in body:
            info["token_type"] = body.get("token_type", "")
            info["expires_in"] = body.get("expires_in", "")
            info["scope"] = body.get("scope", "")
            return body["access_token"], info

        info["error"] = body.get("error", "")
        info["error_desc"] = body.get("error_description", "")
        info["message"] = body.get("error", {}).get("message", "") if isinstance(body.get("error"), dict) else ""
        return None, info

    except requests.RequestException as e:
        return None, {"status": 0, "error": str(e)[:200]}


# ─── read-only 엔드포인트 조회 ──────────────────────
def probe_endpoint(
    base_url: str, path: str, token: str,
    account_no: str = "", needs_account: bool = False,
    extra_params: dict | None = None,
) -> dict:
    """GET-only read 엔드포인트 probe."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if needs_account and account_no:
        headers["X-Tossinvest-Account"] = str(account_no)
    params = dict(extra_params) if extra_params else {}

    try:
        resp = requests.get(
            f"{base_url}{path}",
            headers=headers,
            params=params,
            timeout=TIMEOUT,
        )
        ct = resp.headers.get("content-type", "")
        body = resp.json() if "json" in ct else {}
        result: dict = {
            "status": resp.status_code,
            "keys": list(body.keys()) if isinstance(body, dict) else ["(list)"],
        }
        if resp.status_code == 200:
            result["raw_body"] = body
            result["sanitized"] = sanitize_response(body)
            if isinstance(body, list):
                result["count"] = len(body)
        else:
            if isinstance(body, dict):
                err = body.get("error", body)
                if isinstance(err, dict):
                    result["error_code"] = err.get("code", "")
                    result["error_msg"] = err.get("message", "")[:200]
                else:
                    result["error"] = str(err)[:200]
        return result

    except requests.RequestException as e:
        return {"status": 0, "error": str(e)[:200]}


def _print_error(result: dict) -> None:
    """에러 정보 출력 헬퍼."""
    if "error_code" in result:
        print(f"    error_code: {result['error_code']}")
    if "error_msg" in result:
        print(f"    error_msg: {result['error_msg']}")
    if "error" in result:
        print(f"    error: {result['error']}")


# ─── 메인 ───────────────────────────────────────────
def main() -> None:
    env = check_env()
    env_ok = print_env_status(env)
    print()

    if not env_ok:
        print("FAIL: 필수 환경변수 누락. .env를 확인하세요.")
        sys.exit(1)

    base_url = env["TOSS_BASE_URL"].rstrip("/")
    app_key = env["TOSS_APP_KEY"]
    app_secret = env["TOSS_APP_SECRET"]

    # ── 토큰 발급 ──
    print("═══ Auth (OAuth2 client_credentials) ═══")
    token, auth_info = get_access_token(base_url, app_key, app_secret)
    print(f"  status: {auth_info['status']}")
    print(f"  response_keys: {auth_info.get('keys', [])}")

    if token:
        print(f"  result: OK")
        print(f"  token_type: {auth_info.get('token_type', '')}")
        print(f"  expires_in: {auth_info.get('expires_in', '')}")
        print(f"  scope: {auth_info.get('scope', '')}")
    else:
        err = auth_info.get("error", "")
        desc = auth_info.get("error_desc", "") or auth_info.get("message", "")
        print(f"  result: FAIL")
        print(f"  error: {err}")
        print(f"  description: {desc}")
        if "IP" in str(desc) or "IP" in str(err):
            print(f"\n  → IP 허용 목록에 이 서버 IP를 등록해야 합니다.")
            try:
                ip = requests.get("https://api.ipify.org", timeout=5).text
                print(f"  → 이 서버 외부 IP: {ip}")
            except Exception:
                print("  → 외부 IP 확인 실패")
        print("\n다음 단계: 토스증권 개발자 콘솔에서 IP 등록 또는 앱 키 권한 확인")
        sys.exit(2)

    # ── 계좌 목록 조회 → accountSeq 획득 ──
    print()
    print("═══ Read-only endpoint probe ═══")
    results_summary = {}

    acct_result = probe_endpoint(base_url, "/api/v1/accounts", token)
    acct_status = acct_result["status"]
    results_summary["계좌 목록"] = "OK" if acct_status == 200 else "FAIL"
    print(f"\n  [계좌 목록] /api/v1/accounts")
    print(f"    status: {acct_status}")

    account_seq = ""
    accounts_count = 0
    if acct_status == 200 and "raw_body" in acct_result:
        raw = acct_result["raw_body"]
        items = raw.get("result", []) if isinstance(raw, dict) else raw
        accounts_count = len(items) if isinstance(items, list) else 0
        print(f"    accounts_count: {accounts_count}")
        if isinstance(items, list) and items:
            account_seq = str(items[0].get("accountSeq", ""))
            acct_type = items[0].get("accountType", "")
            print(f"    first_account_type: {acct_type}")
            print(f"    accountSeq: {account_seq}")
    if "sanitized" in acct_result:
        print(f"    data: {acct_result['sanitized']}")
    if not account_seq:
        _print_error(acct_result)

    # ── 나머지 엔드포인트 ──
    for label, path, needs_acct, extra in READ_ONLY_ENDPOINTS:
        if path == "/api/v1/accounts":
            continue
        acct_val = account_seq if needs_acct else ""
        result = probe_endpoint(base_url, path, token, acct_val, needs_acct, extra)
        status = result["status"]
        ok = "OK" if status == 200 else "FAIL"
        results_summary[label] = ok

        print(f"\n  [{label}] {path}")
        print(f"    status: {status} ({ok})")
        print(f"    keys: {result.get('keys', [])}")

        if status == 200:
            if "count" in result:
                print(f"    count: {result['count']}")
            if "sanitized" in result:
                print(f"    data: {result['sanitized']}")
        else:
            _print_error(result)

    # ── 요약 ──
    print()
    print("═══ Summary ═══")
    print(f"  env: OK")
    print(f"  auth: OK (expires_in={auth_info.get('expires_in', '')})")
    print(f"  accounts_count: {accounts_count}")
    for label, status in results_summary.items():
        print(f"  {label}: {status}")
    print(f"  민감정보 로그 노출: 없음")


if __name__ == "__main__":
    main()
