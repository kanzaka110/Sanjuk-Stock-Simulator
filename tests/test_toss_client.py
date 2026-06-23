"""
Toss client 단위 테스트 — 실제 API 호출 없음

- read-only 메서드만 존재하는지
- forbidden keyword guard
- 민감정보 sanitizer
- 네트워크 실패 안전
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.toss_client as tc


class TestReadOnlyGuard:
    """toss_client 소스에 주문/변경 관련 코드가 없는지 검증."""

    FORBIDDEN_PATTERNS = [
        r"\bbuy\b", r"\bsell\b",
        r"\bcancel\b",
        r"\bPUT\b", r"\bDELETE\b", r"\bPATCH\b",
    ]

    def _get_source(self) -> str:
        return Path(tc.__file__).resolve().read_text(encoding="utf-8")

    def test_no_forbidden_keywords(self):
        source = self._get_source()
        for pat in self.FORBIDDEN_PATTERNS:
            matches = re.findall(pat, source, re.IGNORECASE)
            assert not matches, f"Forbidden '{pat}' found: {matches}"

    def test_post_only_in_token(self):
        source = self._get_source()
        post_lines = [
            l for l in source.splitlines()
            if re.search(r"\bPOST\b", l, re.IGNORECASE)
        ]
        bad = [l for l in post_lines
               if not any(kw in l.lower() for kw in ("oauth2", "token", "requests.post"))]
        assert not bad, f"POST outside token context: {bad}"

    def test_get_method_only_in_api_caller(self):
        source = inspect.getsource(tc._get)
        assert "requests.get" in source
        assert "requests.post" not in source
        assert "requests.put" not in source
        assert "requests.delete" not in source

    def test_no_file_token_storage(self):
        source = self._get_source()
        assert "write_text" not in source
        assert "open(" not in source.replace("urlopen(", "")
        assert "TOKEN_FILE" not in source


class TestSanitizer:
    def test_sanitize_accountno(self):
        data = {"accountNo": "99900001234", "seq": 1}
        result = tc.sanitize_dict(data)
        assert result["accountNo"] == "[REDACTED]"
        assert result["seq"] == 1

    def test_sanitize_nested(self):
        data = {"result": [{"accountNo": "12345678901", "type": "A"}]}
        result = tc.sanitize_dict(data)
        assert "12345678901" not in str(result)

    def test_sanitize_access_token(self):
        data = {"access_token": "secret123", "expires": 3600}
        result = tc.sanitize_dict(data)
        assert result["access_token"] == "[REDACTED]"

    def test_sanitize_long_numbers(self):
        data = {"note": "ref 12345678 done"}
        result = tc.sanitize_dict(data)
        assert "12345678" not in str(result)

    def test_preserves_safe(self):
        data = {"status": "ok", "count": 5}
        result = tc.sanitize_dict(data)
        assert result == {"status": "ok", "count": 5}


class TestIsConfigured:
    def test_not_configured_without_env(self):
        with patch.object(tc, "TOSS_APP_KEY", ""), \
             patch.object(tc, "TOSS_APP_SECRET", ""), \
             patch.object(tc, "TOSS_BASE_URL", ""):
            assert tc.is_configured() is False

    def test_configured_with_all(self):
        with patch.object(tc, "TOSS_APP_KEY", "k"), \
             patch.object(tc, "TOSS_APP_SECRET", "s"), \
             patch.object(tc, "TOSS_BASE_URL", "https://x"):
            assert tc.is_configured() is True


class TestGetAccounts:
    def test_returns_empty_on_failure(self):
        with patch.object(tc, "_get", return_value=None):
            assert tc.get_accounts() == []

    def test_extracts_result(self):
        with patch.object(tc, "_get", return_value={"result": [{"accountSeq": 1}]}):
            assert tc.get_accounts() == [{"accountSeq": 1}]


class TestGetHoldings:
    def test_returns_empty_on_failure(self):
        with patch.object(tc, "_get", return_value=None):
            assert tc.get_holdings("1") == {}

    def test_extracts_result(self):
        with patch.object(tc, "_get", return_value={"result": {"items": []}}):
            assert tc.get_holdings("1") == {"items": []}


class TestGetExchangeRate:
    def test_returns_empty_on_failure(self):
        with patch.object(tc, "_get", return_value=None):
            assert tc.get_exchange_rate() == {}

    def test_passes_params(self):
        mock_get = MagicMock(return_value={"result": {"rate": "1500"}})
        with patch.object(tc, "_get", mock_get):
            tc.get_exchange_rate("EUR", "KRW")
            mock_get.assert_called_once_with("/api/v1/exchange-rate", params={
                "baseCurrency": "EUR", "quoteCurrency": "KRW",
            })


class TestGetBuyingPower:
    def test_returns_empty_on_failure(self):
        with patch.object(tc, "_get", return_value=None):
            assert tc.get_buying_power("1") == {}

    def test_extracts_result(self):
        with patch.object(tc, "_get", return_value={"result": {"currency": "KRW", "cashBuyingPower": "10000000"}}):
            r = tc.get_buying_power("1", "KRW")
            assert r["cashBuyingPower"] == "10000000"

    def test_passes_params(self):
        mock_get = MagicMock(return_value={"result": {"currency": "USD", "cashBuyingPower": "5.67"}})
        with patch.object(tc, "_get", mock_get):
            tc.get_buying_power("1", "USD")
            mock_get.assert_called_once_with("/api/v1/buying-power", account_seq="1", params={"currency": "USD"})


class TestGetMarketCalendar:
    def test_returns_empty_on_failure(self):
        with patch.object(tc, "_get", return_value=None):
            assert tc.get_market_calendar("KR") == {}

    def test_calls_correct_path(self):
        mock_get = MagicMock(return_value={"result": {}})
        with patch.object(tc, "_get", mock_get):
            tc.get_market_calendar("US")
            mock_get.assert_called_once_with("/api/v1/market-calendar/US")


class TestTokenSafety:
    def test_unconfigured_returns_none(self):
        with patch.object(tc, "is_configured", return_value=False):
            assert tc._get_access_token() is None

    def test_network_error_returns_none(self):
        import requests
        with patch.object(tc, "is_configured", return_value=True), \
             patch.object(tc, "TOSS_APP_KEY", "k"), \
             patch.object(tc, "TOSS_APP_SECRET", "s"), \
             patch.object(tc, "TOSS_BASE_URL", "https://fake"), \
             patch("core.toss_client.requests.post", side_effect=requests.ConnectionError("fail")):
            # Reset cache
            tc._mem_token = ""
            tc._mem_expires = 0.0
            assert tc._get_access_token() is None
