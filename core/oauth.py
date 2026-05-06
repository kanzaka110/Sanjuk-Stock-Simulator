"""
Claude OAuth 클라이언트 — Claude Max 토큰 기반 ($0)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
~/.claude/.credentials.json에서 OAuth 토큰을 읽어
Anthropic 클라이언트를 생성한다. API 키 과금 없음.
"""

import json
import logging
import time
from pathlib import Path

import anthropic

log = logging.getLogger(__name__)

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

_cached_token: str = ""
_token_expires: float = 0


def _refresh_token() -> str:
    """OAuth 토큰을 읽고 캐시한다."""
    global _cached_token, _token_expires

    now = time.time() * 1000
    if _cached_token and _token_expires > now + 60_000:
        return _cached_token

    try:
        data = json.loads(CREDENTIALS_PATH.read_text())
        oauth = data.get("claudeAiOauth", {})
        _cached_token = oauth.get("accessToken", "")
        _token_expires = oauth.get("expiresAt", 0)
        if not _cached_token:
            log.error("OAuth 토큰 비어있음: %s", CREDENTIALS_PATH)
        return _cached_token
    except Exception as e:
        log.error("OAuth 토큰 읽기 실패: %s", e)
        return _cached_token


def get_client() -> anthropic.Anthropic:
    """OAuth 토큰 기반 Anthropic 클라이언트를 반환한다."""
    token = _refresh_token()
    if not token:
        raise RuntimeError(f"OAuth 토큰 없음: {CREDENTIALS_PATH}")
    return anthropic.Anthropic(api_key=token)
