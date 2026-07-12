from __future__ import annotations

import sys
from unittest.mock import patch


def test_dashboard_autonomous_mode_isolates_account_broker_gets(monkeypatch, tmp_path):
    import core.dashboard_data as dd

    monkeypatch.setenv("TOSS_AUTONOMOUS_MODE", "true")
    monkeypatch.setenv("TOSS_READONLY_SNAPSHOT_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(sys, "argv", ["main.py", "dashboard"])

    with patch(
        "core.toss_client.get_accounts",
        side_effect=AssertionError("dashboard must not call Toss accounts"),
    ):
        result = dd._fetch_toss_account_summary_raw()

    assert result["error"] == "stock_bot_snapshot_unavailable"
    assert result["cache_status"] == "cooldown"
    assert "직접 조회하지 않음" in result["read_only_notice"]


def test_dashboard_autonomous_mode_isolates_order_broker_gets(monkeypatch, tmp_path):
    import core.dashboard_data as dd

    monkeypatch.setenv("TOSS_AUTONOMOUS_MODE", "true")
    monkeypatch.setenv("TOSS_READONLY_SNAPSHOT_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(sys, "argv", ["main.py", "dashboard"])

    with patch(
        "core.toss_live_order_http.list_orders",
        side_effect=AssertionError("dashboard must not call Toss order lists"),
    ):
        result = dd._recent_toss_broker_orders(limit=20)

    assert result["ok"] is False
    assert result["error"] == "snapshot_missing"
    assert result["source"] == "stock_bot_snapshot"
    assert result["usable_for_orders"] is False
    assert result["cache_status"] == "unavailable"
    assert result["orders"] == []


def test_dashboard_reads_broker_orders_only_from_snapshot(monkeypatch):
    import core.dashboard_data as dd

    monkeypatch.setenv("TOSS_AUTONOMOUS_MODE", "true")
    monkeypatch.setattr(sys, "argv", ["main.py", "dashboard"])
    snapshot = {
        "ok": True,
        "orders": [{
            "client_order_id": "tlive_20260712_025100_1234",
            "symbol": "005930",
            "side": "BUY",
            "broker_order_status": "FILLED",
            "filled_quantity": 1,
            "filled_price": 61000,
            "filled_at": "2026-07-12T02:51:01+09:00",
        }],
        "snapshot_status": "fresh",
        "snapshot_age_sec": 10,
        "source": "stock_bot_snapshot",
        "usable_for_orders": False,
    }
    with patch("core.toss_readonly_snapshot.broker_orders_for_consumer", return_value=snapshot), \
         patch("core.toss_live_order_http.list_orders", side_effect=AssertionError("no broker GET")):
        result = dd._recent_toss_broker_orders(limit=20)
    assert result["ok"] is True
    assert result["source"] == "stock_bot_snapshot"
    assert result["usable_for_orders"] is False
    assert result["orders"][0]["symbol"] == "005930.KS"
    assert result["orders"][0]["client_order_id"] == "tlive_20260712_025100_1234"


def test_bot_process_keeps_broker_read_ownership(monkeypatch):
    import core.dashboard_data as dd

    monkeypatch.setenv("TOSS_AUTONOMOUS_MODE", "true")
    monkeypatch.setattr(sys, "argv", ["main.py", "bot"])

    assert dd._dashboard_toss_broker_reads_isolated() is False


# ─── toss_client 경계 전역 격리 (endpoint별 격리에 의존하지 않음) ──
#
# 누락 경로 실증 (2026-07-10 16:06 UTC 401의 원인):
#   Hermes cron → GET /api/toss/buy-candidates
#   → toss_buy_candidates_data() → toss_eligible_new_candidates()
#   → _usdkrw_rate() → toss_client.get_exchange_rate()
#   → dashboard 프로세스가 OAuth 발급 → stock-bot 토큰 무효화 → bot 401
#
# account-summary/order 조회만 막아서는 이런 우회로가 남는다. 아래 계약은
# toss_client 경계에서 dashboard 프로세스의 모든 Broker 네트워크를 차단한다.

import pytest
from unittest.mock import MagicMock


@pytest.fixture
def _restore_token_state():
    """토큰 캐시/argv를 테스트 전후 복원 — 다른 테스트로 상태 누수 금지."""
    import core.toss_client as tc
    saved_token, saved_expires = tc._mem_token, tc._mem_expires
    saved_argv = list(sys.argv)
    yield tc
    tc._mem_token, tc._mem_expires = saved_token, saved_expires
    sys.argv[:] = saved_argv


def _dashboard_mode(monkeypatch):
    monkeypatch.setenv("TOSS_AUTONOMOUS_MODE", "true")
    monkeypatch.setattr(sys, "argv", ["main.py", "dashboard"])


def test_dashboard_autonomous_token_is_none(monkeypatch, _restore_token_state):
    tc = _restore_token_state
    _dashboard_mode(monkeypatch)
    tc._mem_token, tc._mem_expires = "", 0.0
    assert tc._broker_access_isolated_for_process() is True
    assert tc._get_access_token() is None


def test_dashboard_ignores_cached_valid_token(monkeypatch, _restore_token_state):
    "dashboard 메모리에 미래 만료의 유효 토큰이 남아 있어도 반환 금지."
    import time as _time
    tc = _restore_token_state
    _dashboard_mode(monkeypatch)
    tc._mem_token, tc._mem_expires = "leftover-cached-token", _time.time() + 3600
    assert tc._get_access_token() is None
    # 격리는 캐시를 지우지 않는다 (발급도 안 함 — 단순 fail-closed None)
    assert tc._mem_token == "leftover-cached-token"


def test_dashboard_broker_reads_return_empty_without_network(monkeypatch, _restore_token_state):
    "get_accounts()=[] / get_exchange_rate()={} + requests.post/get 호출 0회."
    tc = _restore_token_state
    _dashboard_mode(monkeypatch)
    tc._mem_token, tc._mem_expires = "", 0.0
    post_mock = MagicMock()
    get_mock = MagicMock()
    with patch("core.toss_client.requests.post", post_mock), \
         patch("core.toss_client.requests.get", get_mock):
        assert tc.get_accounts() == []
        assert tc.get_exchange_rate("USD", "KRW") == {}
        assert tc._get_access_token() is None
    assert post_mock.call_count == 0   # OAuth 발급 네트워크 0회
    assert get_mock.call_count == 0    # Broker GET 네트워크 0회


def test_dashboard_isolated_even_with_valid_cache_no_network(monkeypatch, _restore_token_state):
    "유효 캐시 + 격리 상태에서도 네트워크 0회 (buy-candidates 우회로 봉쇄)."
    import time as _time
    tc = _restore_token_state
    _dashboard_mode(monkeypatch)
    tc._mem_token, tc._mem_expires = "leftover-cached-token", _time.time() + 3600
    post_mock = MagicMock()
    get_mock = MagicMock()
    with patch("core.toss_client.requests.post", post_mock), \
         patch("core.toss_client.requests.get", get_mock):
        assert tc.get_exchange_rate() == {}
    assert post_mock.call_count == 0
    assert get_mock.call_count == 0


def test_bot_process_token_path_not_blocked(monkeypatch, _restore_token_state):
    "sys.argv=['main.py','bot']에서는 격리 false — 기존 발급 경로 동작."
    tc = _restore_token_state
    monkeypatch.setenv("TOSS_AUTONOMOUS_MODE", "true")
    monkeypatch.setattr(sys, "argv", ["main.py", "bot"])
    tc._mem_token, tc._mem_expires = "", 0.0

    assert tc._broker_access_isolated_for_process() is False

    class _Resp:
        status_code = 200
        @staticmethod
        def json():
            return {"access_token": "[REDACTED-bot]", "expires_in": 3600}

    with patch.object(tc, "TOSS_APP_KEY", "fake-key"), \
         patch.object(tc, "TOSS_APP_SECRET", "fake-secret"), \
         patch.object(tc, "TOSS_BASE_URL", "https://test.example"), \
         patch("core.toss_client.requests.post", MagicMock(return_value=_Resp())) as post_mock:
        assert tc._get_access_token() == "[REDACTED-bot]"
    assert post_mock.call_count == 1


def test_dashboard_without_autonomous_mode_not_isolated(monkeypatch, _restore_token_state):
    "자율모드 꺼진 dashboard는 격리 대상 아님 (조건 AND)."
    tc = _restore_token_state
    monkeypatch.setenv("TOSS_AUTONOMOUS_MODE", "false")
    monkeypatch.setattr(sys, "argv", ["main.py", "dashboard"])
    assert tc._broker_access_isolated_for_process() is False
