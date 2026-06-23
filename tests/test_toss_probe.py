"""
Toss probe 단위 테스트 — 실제 API 호출 없음

- env missing 시 안전 실패
- 마스킹 함수 검증 (재귀, 숫자, 민감 키)
- 응답 sanitizer가 민감정보 제거하는지
- 금지 키워드 guard (주문 관련 문자열 차단)
- 환율 파라미터 검증
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# probe 모듈 임포트
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import tools.probe_toss_account as probe


# ═══ 마스킹 함수 ═══

class TestMaskValue:
    def test_empty_returns_not_set(self):
        assert probe.mask_value("") == "NOT_SET"

    def test_none_returns_not_set(self):
        assert probe.mask_value("") == "NOT_SET"

    def test_show_last_4(self):
        result = probe.mask_value("1234567890", show_last=4)
        assert "7890" in result
        assert "1234" not in result
        assert "len=10" in result

    def test_no_show_last(self):
        result = probe.mask_value("secretvalue123")
        assert "secretvalue123" not in result
        assert "FOUND" in result
        assert "len=14" in result

    def test_short_value_with_show_last(self):
        result = probe.mask_value("ab", show_last=4)
        assert "FOUND" in result


# ═══ Recursive Sanitizer ═══

class TestSanitizeResponse:
    """sanitize_response가 재귀적으로 민감정보를 제거하는지 검증."""

    def test_masks_access_token(self):
        data = {"access_token": "real-secret-token-xyz", "expires_in": 3600}
        result = probe.sanitize_response(data)
        assert "real-secret-token-xyz" not in result
        assert "REDACTED" in result
        assert "3600" in result

    def test_masks_refresh_token(self):
        data = {"refresh_token": "refresh-abc", "scope": "read"}
        result = probe.sanitize_response(data)
        assert "refresh-abc" not in result

    def test_masks_accountno_top_level(self):
        data = {"accountNo": "17401007263", "accountSeq": 1}
        result = probe.sanitize_response(data)
        assert "17401007263" not in result
        assert "REDACTED" in result

    def test_masks_accountnumber(self):
        data = {"accountNumber": "12345678-90", "name": "test"}
        result = probe.sanitize_response(data)
        assert "12345678-90" not in result

    def test_masks_password(self):
        data = {"password": "p@ssw0rd!", "status": "ok"}
        result = probe.sanitize_response(data)
        assert "p@ssw0rd!" not in result

    def test_masks_secret(self):
        data = {"secret": "my-secret-value", "type": "app"}
        result = probe.sanitize_response(data)
        assert "my-secret-value" not in result

    def test_masks_appsecret(self):
        data = {"appSecret": "supersecret123", "appKey": "mykey456"}
        result = probe.sanitize_response(data)
        assert "supersecret123" not in result
        assert "mykey456" not in result

    def test_masks_nested_accountno_in_list(self):
        """nested list 안의 계좌번호도 마스킹."""
        data = {
            "result": [
                {"accountNo": "17401007263", "accountSeq": 1, "accountType": "BROKERAGE"}
            ]
        }
        result = probe.sanitize_response(data)
        assert "17401007263" not in result

    def test_masks_deeply_nested_dict(self):
        """깊은 중첩 dict 안의 민감 키도 마스킹."""
        data = {
            "outer": {
                "inner": {
                    "account_id": "99887766554",
                    "token": "secret-token-deep",
                    "safe": "visible",
                }
            }
        }
        result = probe.sanitize_response(data)
        assert "99887766554" not in result
        assert "secret-token-deep" not in result
        assert "visible" in result

    def test_masks_long_number_strings(self):
        """8자리 이상 숫자 문자열이 마스킹됨."""
        data = {"note": "ref 12345678 done", "count": 5}
        result = probe.sanitize_response(data)
        assert "12345678" not in result
        assert "NUM_REDACTED" in result

    def test_masks_long_number_in_nested_value(self):
        data = {"items": [{"desc": "account 9876543210 active"}]}
        result = probe.sanitize_response(data)
        assert "9876543210" not in result

    def test_truncates_long_response(self):
        data = {f"key_{i}": f"value_{i}" * 50 for i in range(20)}
        result = probe.sanitize_response(data, max_chars=100)
        assert len(result) <= 120
        assert "truncated" in result

    def test_preserves_safe_fields(self):
        data = {"status": "ok", "count": 5, "items": []}
        result = probe.sanitize_response(data)
        assert "ok" in result
        assert "5" in result

    def test_sanitized_preview_no_raw_account(self):
        """sanitize 결과에 실제 계좌번호 패턴이 남으면 실패."""
        acct = "17401007263"
        data = {"result": [{"accountNo": acct, "seq": 1}]}
        result = probe.sanitize_response(data)
        assert acct not in result

    def test_handles_list_input(self):
        data = [{"accountNo": "99887766"}, {"token": "abc123"}]
        result = probe.sanitize_response(data)
        assert "99887766" not in result
        assert "abc123" not in result


# ═══ 내부 함수 ═══

class TestInternalSanitize:
    def test_is_sensitive_key(self):
        assert probe._is_sensitive_key("accountNo")
        assert probe._is_sensitive_key("ACCOUNTNO")
        assert probe._is_sensitive_key("access_token")
        assert probe._is_sensitive_key("appSecret")
        assert probe._is_sensitive_key("clientSecret")
        assert not probe._is_sensitive_key("status")
        assert not probe._is_sensitive_key("expires_in")

    def test_mask_long_numbers(self):
        assert "12345678" not in probe._mask_long_numbers("ref 12345678 ok")
        assert "NUM_REDACTED" in probe._mask_long_numbers("ref 12345678 ok")
        assert "1234" in probe._mask_long_numbers("short 1234 ok")  # 8미만은 유지


# ═══ 환경변수 누락 시 안전 실패 ═══

class TestEnvCheck:
    def test_missing_env_returns_empty(self):
        with patch.dict("os.environ", {}, clear=True):
            env = probe.check_env()
            assert env["TOSS_APP_KEY"] == ""
            assert env["TOSS_APP_SECRET"] == ""

    def test_partial_env(self):
        with patch.dict("os.environ", {"TOSS_APP_KEY": "test_key"}, clear=True):
            env = probe.check_env()
            assert env["TOSS_APP_KEY"] == "test_key"
            assert env["TOSS_APP_SECRET"] == ""

    def test_print_env_status_fails_on_missing(self, capsys):
        with patch.dict("os.environ", {}, clear=True):
            env = probe.check_env()
            result = probe.print_env_status(env)
            assert result is False


# ═══ 환율 endpoint 파라미터 ═══

class TestExchangeRateParams:
    def test_exchange_rate_has_currency_params(self):
        """환율 endpoint에 baseCurrency, quoteCurrency가 포함됨."""
        for label, path, _needs, extra in probe.READ_ONLY_ENDPOINTS:
            if "exchange-rate" in path:
                assert "baseCurrency" in extra, "baseCurrency 파라미터 누락"
                assert "quoteCurrency" in extra, "quoteCurrency 파라미터 누락"
                break
        else:
            pytest.fail("exchange-rate endpoint가 READ_ONLY_ENDPOINTS에 없음")


# ═══ 금지 키워드 guard ═══

class TestForbiddenKeywords:
    """probe 소스코드에 주문 관련 키워드가 없는지 검증."""

    FORBIDDEN_PATTERNS = [
        r"\bbuy\b", r"\bsell\b",
        r"\bcancel\b",
        r"\bPUT\b", r"\bDELETE\b", r"\bPATCH\b",
    ]

    def _get_source(self) -> str:
        source_path = Path(probe.__file__).resolve()
        return source_path.read_text(encoding="utf-8")

    def test_no_forbidden_keywords_in_source(self):
        source = self._get_source()
        for pattern in self.FORBIDDEN_PATTERNS:
            matches = re.findall(pattern, source, re.IGNORECASE)
            assert not matches, (
                f"Forbidden pattern '{pattern}' found in probe source: {matches}"
            )

    def test_post_only_in_token_context(self):
        """POST는 oauth2/token/requests.post 컨텍스트에서만."""
        source = self._get_source()
        post_lines = [
            line for line in source.splitlines()
            if re.search(r"\bPOST\b", line, re.IGNORECASE)
        ]
        bad_posts = [
            line for line in post_lines
            if not any(kw in line.lower() for kw in ("oauth2", "token", "requests.post"))
        ]
        assert not bad_posts, f"POST in non-token context: {bad_posts}"

    def test_only_get_method_in_probe_endpoint(self):
        """probe_endpoint 함수가 GET만 사용하는지 확인."""
        source = inspect.getsource(probe.probe_endpoint)
        assert "requests.get" in source
        assert "requests.post" not in source
        assert "requests.put" not in source
        assert "requests.delete" not in source
        assert "requests.patch" not in source

    def test_read_only_endpoints_are_get_paths(self):
        """READ_ONLY_ENDPOINTS가 모두 조회용 경로인지 확인."""
        for label, path, _needs_acct, _extra in probe.READ_ONLY_ENDPOINTS:
            assert "/submit" not in path.lower()
            assert "/execute" not in path.lower()


# ═══ token 발급 함수 네트워크 실패 안전 ═══

class TestAuthSafety:
    def test_network_error_returns_none(self):
        import requests as req_lib
        with patch("tools.probe_toss_account.requests.post", side_effect=req_lib.ConnectionError("timeout")):
            token, info = probe.get_access_token("https://fake", "k", "s")
            assert token is None
            assert info["status"] == 0
            assert "timeout" in info["error"]

    def test_401_returns_none_with_info(self):
        mock_resp = type("R", (), {
            "status_code": 401,
            "json": lambda self: {"error": "unauthorized", "error_description": "bad creds"},
        })()
        with patch("tools.probe_toss_account.requests.post", return_value=mock_resp):
            token, info = probe.get_access_token("https://fake", "k", "s")
            assert token is None
            assert info["status"] == 401

    def test_success_returns_token(self):
        mock_resp = type("R", (), {
            "status_code": 200,
            "json": lambda self: {
                "access_token": "test_token_abc",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "read",
            },
        })()
        with patch("tools.probe_toss_account.requests.post", return_value=mock_resp):
            token, info = probe.get_access_token("https://fake", "k", "s")
            assert token == "test_token_abc"
            assert info["status"] == 200
            assert info["token_type"] == "Bearer"
