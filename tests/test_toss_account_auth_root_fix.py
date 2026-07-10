# Regression tests for Toss account auth/root-fix behavior.
# Locks the non-band-aid fix for repeated autonomous failures:
# - stale long-running access token must be refreshed on GET 401
# - accountSeq must be cached / env-provided so orders do not call /accounts every time
# - insufficient buying power must not be treated as transient retryable failure
#
# OAuth 401 경쟁 상태 근본 해결 (2026-07-11):
# - OAuth 발급 singleflight: 동시 N회 호출 → 실제 발급 POST 1회
# - 세대 안전 invalidation: 과거 401이 최신 토큰을 지우지 못함
# - 주문 직전 최신 토큰: 계좌/보유 GET 뒤에 토큰 획득
# - POST 401은 재POST 금지 + 원장 reconciliation (auth_ambiguous terminal)

from __future__ import annotations

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def _clear_toss_account_seq_cache():
    import core.toss_live_order_http as oh
    if hasattr(oh, "_clear_account_seq_cache"):
        oh._clear_account_seq_cache()
    yield
    if hasattr(oh, "_clear_account_seq_cache"):
        oh._clear_account_seq_cache()


@pytest.fixture(autouse=True)
def _isolated_token_cache():
    """실토큰 캐시 오염/노출 방지 — 각 테스트 전후 저장·초기화·복원."""
    import core.toss_client as tc
    saved = (tc._mem_token, tc._mem_expires)
    tc._mem_token, tc._mem_expires = "", 0.0
    yield
    tc._mem_token, tc._mem_expires = saved


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


def test_toss_get_retries_once_after_401_and_refreshes_token():
    "A stale cached token in a long-running process must not poison all future GETs."
    import core.toss_client as tc

    get_mock = MagicMock(side_effect=[
        _Resp(401, {"error": {"code": "Unauthorized", "description": "stale token"}}),
        _Resp(200, {"result": [{"accountSeq": "seq-1"}]}),
    ])
    token_mock = MagicMock(side_effect=["old-token", "new-token"])

    with patch("core.toss_client._get_access_token", token_mock), \
         patch("core.toss_client.TOSS_BASE_URL", "https://test.example"), \
         patch("requests.get", get_mock):
        result = tc._get("/api/v1/accounts")

    assert result == {"result": [{"accountSeq": "seq-1"}]}
    assert token_mock.call_count == 2
    assert get_mock.call_count == 2
    first_auth = get_mock.call_args_list[0].kwargs["headers"]["Authorization"]
    second_auth = get_mock.call_args_list[1].kwargs["headers"]["Authorization"]
    assert first_auth.endswith("old-token")
    assert second_auth.endswith("new-token")


def test_toss_get_does_not_retry_forever_on_repeated_401():
    "Retry 401 exactly once; no tight loop / rate-limit amplification."
    import core.toss_client as tc

    get_mock = MagicMock(side_effect=[
        _Resp(401, {"error": {"code": "Unauthorized"}}),
        _Resp(401, {"error": {"code": "Unauthorized"}}),
    ])

    with patch("core.toss_client._get_access_token", MagicMock(side_effect=["old", "new"])), \
         patch("core.toss_client.TOSS_BASE_URL", "https://test.example"), \
         patch("requests.get", get_mock):
        result = tc._get("/api/v1/accounts")

    assert result is None
    assert get_mock.call_count == 2


def test_resolve_account_seq_uses_env_without_accounts_call():
    "Configured accountSeq should avoid calling /accounts during every order."
    import core.toss_live_order_http as oh

    with patch.dict(os.environ, {"TOSS_ACCOUNT_SEQ": "env-seq-777"}, clear=False), \
         patch("core.toss_client.get_accounts", side_effect=AssertionError("/accounts should not be called")):
        assert oh._resolve_account_seq(None) == "env-seq-777"


def test_resolve_account_seq_caches_successful_accounts_lookup():
    "First account lookup may call /accounts; later orders should use cached accountSeq."
    import core.toss_live_order_http as oh

    get_accounts = MagicMock(side_effect=[
        [{"accountSeq": "cached-seq-1"}],
        AssertionError("second /accounts call should not happen"),
    ])

    with patch.dict(os.environ, {}, clear=False), \
         patch("core.toss_client.get_accounts", get_accounts):
        assert oh._resolve_account_seq(None) == "cached-seq-1"
        assert oh._resolve_account_seq(None) == "cached-seq-1"

    assert get_accounts.call_count == 1


# ─── OAuth singleflight ───────────────────────────────────────────

_TOKEN_ENV = {
    "target": "core.toss_client",
}


def _patch_token_issuer(post_mock):
    """실발급 경로를 mock으로 격리 (키/URL은 가짜, 네트워크 없음)."""
    import core.toss_client as tc
    return (
        patch.object(tc, "TOSS_APP_KEY", "fake-key"),
        patch.object(tc, "TOSS_APP_SECRET", "fake-secret"),
        patch.object(tc, "TOSS_BASE_URL", "https://test.example"),
        patch("core.toss_client.requests.post", post_mock),
    )


def test_concurrent_token_calls_issue_exactly_one_oauth_request():
    "동시 8회 호출 → OAuth 발급 POST 정확히 1회, 전원 같은 토큰."
    import core.toss_client as tc

    barrier = threading.Barrier(8)

    def _slow_issue(*a, **k):
        time.sleep(0.05)  # 발급 중 race 창 확대
        return _Resp(200, {"access_token": "[REDACTED-single]", "expires_in": 3600})

    post_mock = MagicMock(side_effect=_slow_issue)
    patches = _patch_token_issuer(post_mock)

    def _call():
        barrier.wait(timeout=5)
        return tc._get_access_token()

    with patches[0], patches[1], patches[2], patches[3]:
        with ThreadPoolExecutor(max_workers=8) as ex:
            tokens = list(ex.map(lambda _: _call(), range(8)))

    assert post_mock.call_count == 1
    assert set(tokens) == {"[REDACTED-single]"}


def test_second_thread_waits_then_uses_cache_without_network():
    "double-check: 첫 스레드 발급 중 대기한 스레드는 캐시로 받는다 (발급 1회)."
    import core.toss_client as tc

    first_inside = threading.Event()

    def _blocking_issue(*a, **k):
        first_inside.set()
        time.sleep(0.1)
        return _Resp(200, {"access_token": "[REDACTED-dc]", "expires_in": 3600})

    post_mock = MagicMock(side_effect=_blocking_issue)
    patches = _patch_token_issuer(post_mock)
    results: list[str | None] = []

    with patches[0], patches[1], patches[2], patches[3]:
        t1 = threading.Thread(target=lambda: results.append(tc._get_access_token()))
        t1.start()
        assert first_inside.wait(timeout=5)   # t1이 발급 네트워크 구간에 진입
        t2 = threading.Thread(target=lambda: results.append(tc._get_access_token()))
        t2.start()
        t1.join(timeout=5); t2.join(timeout=5)

    assert post_mock.call_count == 1
    assert results == ["[REDACTED-dc]", "[REDACTED-dc]"]


def test_issue_failure_does_not_deadlock_next_caller():
    "발급 실패(네트워크 예외) 후에도 lock이 풀려 다음 호출이 진행된다."
    import requests as _requests
    import core.toss_client as tc

    post_mock = MagicMock(side_effect=[
        _requests.ConnectionError("boom"),
        _Resp(200, {"access_token": "[REDACTED-retry]", "expires_in": 3600}),
    ])
    patches = _patch_token_issuer(post_mock)
    with patches[0], patches[1], patches[2], patches[3]:
        assert tc._get_access_token() is None
        assert tc._get_access_token() == "[REDACTED-retry]"
    assert post_mock.call_count == 2


# ─── 세대 안전 invalidation ───────────────────────────────────────

def test_stale_401_does_not_invalidate_newer_token():
    "old token 요청의 늦은 401이 최신 토큰을 지우지 못한다."
    import core.toss_client as tc

    tc._mem_token, tc._mem_expires = "new-generation-token", time.time() + 3600
    tc._invalidate_access_token(expected_token="old-generation-token")
    assert tc._mem_token == "new-generation-token"   # 보호됨

    tc._invalidate_access_token(expected_token="new-generation-token")
    assert tc._mem_token == ""                        # 같은 세대만 폐기

    tc._mem_token, tc._mem_expires = "another-token", time.time() + 3600
    tc._invalidate_access_token()                     # 무조건 폐기 (하위호환)
    assert tc._mem_token == ""


def test_get_401_passes_expected_token_to_invalidation():
    "_get()의 401 재시도는 자신이 쓴 토큰 세대만 폐기한다."
    import core.toss_client as tc

    get_mock = MagicMock(side_effect=[
        _Resp(401, {"error": {"code": "Unauthorized"}}),
        _Resp(200, {"result": []}),
    ])
    inv = MagicMock()
    with patch("core.toss_client._get_access_token",
               MagicMock(side_effect=["old-token", "new-token"])), \
         patch("core.toss_client._invalidate_access_token", inv), \
         patch("core.toss_client.TOSS_BASE_URL", "https://test.example"), \
         patch("requests.get", get_mock):
        tc._get("/api/v1/accounts")
    inv.assert_called_once_with(expected_token="old-token")


# ─── 주문 직전 최신 토큰 획득 (호출 순서) ─────────────────────────

_ORDER_BODY_BUY = {"symbol": "NEM", "side": "BUY", "quantity": 1,
                   "price": 50.0, "orderType": "LIMIT"}
_ORDER_BODY_SELL = {"symbol": "NEM", "side": "SELL", "quantity": 1,
                    "price": 50.0, "orderType": "LIMIT"}


def test_submit_order_fetches_token_after_account_and_holdings_gets():
    "accountSeq/SELL 보유확인 GET이 전부 끝난 뒤에 토큰을 얻는다."
    import core.toss_client as tc
    import core.toss_live_order_http as oh

    calls: list[str] = []
    with patch.object(tc, "get_accounts",
                      side_effect=lambda *a, **k: (calls.append("accounts"), [{"accountSeq": "seq-1"}])[1]), \
         patch.object(tc, "get_holdings",
                      side_effect=lambda *a, **k: (calls.append("holdings"),
                                                   [{"symbol": "NEM", "sellableQuantity": 5}])[1]), \
         patch.object(tc, "_get_access_token",
                      side_effect=lambda: (calls.append("token"), "fresh-token")[1]), \
         patch.object(tc, "TOSS_BASE_URL", "https://test.example"), \
         patch("core.toss_live_order_http.confirm_order_state",
               return_value={"broker_confirmed": True, "broker_order_status": "PENDING"}), \
         patch("requests.post",
               MagicMock(return_value=_Resp(200, {"result": {"orderId": "ord-1"}}))) as post_mock:
        result = oh.submit_order(dict(_ORDER_BODY_SELL))

    assert result["live_order_sent"] is True
    first_token_at = calls.index("token")
    assert "accounts" in calls[:first_token_at]
    assert "holdings" in calls[:first_token_at]
    auth = post_mock.call_args.kwargs["headers"]["Authorization"]
    assert auth.endswith("fresh-token")


def test_token_refreshed_during_account_resolution_used_for_post():
    "계좌 확인 GET 중 토큰이 old→new 교체되면 POST는 new 토큰을 쓴다."
    import core.toss_client as tc
    import core.toss_live_order_http as oh

    # 시작 시 old 토큰이 캐시에 살아 있음
    tc._mem_token, tc._mem_expires = "old-cached-token", time.time() + 3600

    def _accounts(*a, **k):
        # 계좌 GET 도중 다른 스레드가 토큰을 갱신한 상황 재현
        tc._mem_token, tc._mem_expires = "refreshed-token", time.time() + 3600
        return [{"accountSeq": "seq-1"}]

    with patch.object(tc, "get_accounts", side_effect=_accounts), \
         patch.object(tc, "TOSS_BASE_URL", "https://test.example"), \
         patch("core.toss_live_order_http.confirm_order_state",
               return_value={"broker_confirmed": True, "broker_order_status": "PENDING"}), \
         patch("requests.post",
               MagicMock(return_value=_Resp(200, {"result": {"orderId": "ord-2"}}))) as post_mock:
        result = oh.submit_order(dict(_ORDER_BODY_BUY))

    assert result["live_order_sent"] is True
    auth = post_mock.call_args.kwargs["headers"]["Authorization"]
    assert auth.endswith("refreshed-token")
    assert "old-cached-token" not in auth


# ─── POST 401 — 재POST 금지 + reconciliation ─────────────────────

def _iso_now_kst() -> str:
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=9))).isoformat()


def _submit_with_post_401(list_orders_result):
    import core.toss_client as tc
    import core.toss_live_order_http as oh

    tc._mem_token, tc._mem_expires = "posting-token", time.time() + 3600
    post_mock = MagicMock(return_value=_Resp(401, {"error": {"code": "Unauthorized"}}))
    list_mock = MagicMock(side_effect=list_orders_result)
    with patch.object(tc, "get_accounts", return_value=[{"accountSeq": "seq-1"}]), \
         patch.object(tc, "TOSS_BASE_URL", "https://test.example"), \
         patch("core.toss_live_order_http.list_orders", list_mock), \
         patch("requests.post", post_mock):
        result = oh.submit_order(dict(_ORDER_BODY_BUY))
    return result, post_mock, list_mock


def test_post_401_matching_order_found_no_repost():
    "401 후 원장에서 동일 주문 발견 → 재POST 없이 broker-confirmed 처리."
    matched_row = {
        "symbol": "NEM", "side": "BUY", "quantity": 1.0,
        "ordered_at": _iso_now_kst(),
        "broker_order_id": "[masked]", "broker_order_status": "PENDING",
        "filled_quantity": 0.0, "filled_price": 0.0,
    }
    result, post_mock, list_mock = _submit_with_post_401([
        {"ok": True, "status": "OPEN", "orders": [matched_row]},
    ])
    assert post_mock.call_count == 1                # 재POST 없음
    assert list_mock.call_count >= 1                # 원장 대조 수행
    assert result["live_order_sent"] is True
    assert result["reason"] == "live_sent_confirmed_after_401"
    assert result["broker_confirmed"] is True
    assert result["auth_race_recovered"] is True


def test_post_401_no_match_is_terminal_auth_ambiguous():
    "401 후 동일 주문 미확인 → 재POST 없이 auth_ambiguous terminal."
    result, post_mock, list_mock = _submit_with_post_401([
        {"ok": True, "status": "OPEN", "orders": []},
        {"ok": True, "status": "CLOSED", "orders": []},
    ])
    assert post_mock.call_count == 1                # 재POST 없음
    assert list_mock.call_count == 2                # OPEN+CLOSED 각 1회
    assert result["live_order_sent"] is False
    assert result["reason"] == "auth_ambiguous"
    assert result.get("failed") is True


def test_post_401_stale_order_outside_window_not_claimed():
    "시각 범위 밖 동일 sym/side/qty 주문은 우리 주문으로 단정하지 않는다."
    from datetime import datetime, timezone, timedelta
    old_at = (datetime.now(timezone(timedelta(hours=9)))
              - timedelta(hours=3)).isoformat()
    stale_row = {"symbol": "NEM", "side": "BUY", "quantity": 1.0,
                 "ordered_at": old_at}
    result, post_mock, _ = _submit_with_post_401([
        {"ok": True, "status": "OPEN", "orders": [stale_row]},
        {"ok": True, "status": "CLOSED", "orders": []},
    ])
    assert post_mock.call_count == 1
    assert result["reason"] == "auth_ambiguous"


def test_post_401_invalidates_only_own_token_generation():
    "401 처리 시 자신이 POST한 토큰 세대만 invalidate 한다."
    import core.toss_client as tc
    import core.toss_live_order_http as oh

    tc._mem_token, tc._mem_expires = "posting-token", time.time() + 3600
    inv = MagicMock()
    with patch.object(tc, "get_accounts", return_value=[{"accountSeq": "seq-1"}]), \
         patch.object(tc, "_invalidate_access_token", inv), \
         patch.object(tc, "TOSS_BASE_URL", "https://test.example"), \
         patch("core.toss_live_order_http.list_orders",
               return_value={"ok": True, "orders": []}), \
         patch("requests.post",
               MagicMock(return_value=_Resp(401, {"error": {}}))):
        oh.submit_order(dict(_ORDER_BODY_BUY))
    inv.assert_called_once_with(expected_token="posting-token")


# ─── finalizer 재시도 정책 ────────────────────────────────────────

def test_http_401_and_auth_ambiguous_are_not_blind_retryable():
    "POST 도달 불확실 실패는 blind retry queue에 들어가지 않는다."
    from core.toss_autonomous_finalizer import _is_retryable_dispatch_failure

    assert _is_retryable_dispatch_failure("http_401", "") is False
    assert _is_retryable_dispatch_failure("auth_ambiguous", "") is False
    assert _is_retryable_dispatch_failure(
        "dispatch_failed", '{"reason":"auth_ambiguous"}') is False


def test_token_unavailable_stays_bounded_retryable():
    "POST 자체가 안 나간 차단(token/account 없음)은 retry 후보 유지."
    from core.toss_autonomous_finalizer import _is_retryable_dispatch_failure

    assert _is_retryable_dispatch_failure("token_unavailable", "") is True
    assert _is_retryable_dispatch_failure("account_unavailable", "") is True


# ─── 민감정보 비노출 ─────────────────────────────────────────────

def test_auth_flow_results_contain_no_secrets():
    "반환값/메시지에 token·Authorization·secret·accountSeq 원문 없음."
    result, _, _ = _submit_with_post_401([
        {"ok": True, "status": "OPEN", "orders": []},
        {"ok": True, "status": "CLOSED", "orders": []},
    ])
    blob = json.dumps(result, ensure_ascii=False, default=str)
    for secret in ("posting-token", "fake-secret", "fake-key",
                   "Authorization", "Bearer ", "seq-1"):
        assert secret not in blob, secret


def test_insufficient_buying_power_is_not_retryable_dispatch_failure():
    "Cash shortage needs sizing/rebalance, not blind retry."
    from core.toss_autonomous_finalizer import _is_retryable_dispatch_failure

    body = '{"error":{"code":"insufficient-buying-power","message":"매수가능금액이 부족합니다."}}'
    assert _is_retryable_dispatch_failure("http_422", body) is False


def test_account_unavailable_remains_retryable_with_backoff_candidate_preservation():
    "Account outage is transient, but should be held/backed off rather than terminal cash failure."
    from core.toss_autonomous_finalizer import _is_retryable_dispatch_failure

    assert _is_retryable_dispatch_failure("account_unavailable", "") is True



def test_insufficient_buying_power_marks_rebalance_needed_in_finalizer():
    "A high-quality buy that lacks cash should become rebalance-needed, not blind retry."
    from datetime import timezone, timedelta
    import os

    from core.toss_autonomous_finalizer import try_autonomous_finalize

    rec = {
        "pilot_id": "cash_short_pilot",
        "symbol": "009540.KS",
        "side": "buy",
        "quantity": 1,
        "limit_price": 350500.0,
        "estimated_amount_krw": 350500,
        "status": "previewed",
        "blocks": [],
        "live_order_sent": False,
        "stop_loss": 329470.0,
        "invalidation": "below stop",
    }

    def transport(_payload, _policy):
        return {
            "ok": False,
            "live_order_sent": False,
            "reason": "http_422",
            "error_body": '{"error":{"code":"insufficient-buying-power","message":"매수가능금액이 부족합니다."}}',
            "order_request_preview": {"symbol": "009540", "side": "BUY"},
        }

    env = {
        "TOSS_LIVE_PILOT_ENABLED": "true",
        "TOSS_LIVE_ORDER_ALLOWED": "true",
        "TOSS_LIVE_ADAPTER_ENABLED": "true",
        "TOSS_AUTONOMOUS_MODE": "true",
        "TOSS_AUTONOMOUS_ALLOWED_SIDES": "buy,sell",
        "TOSS_AUTONOMOUS_ALLOWED_ASSET_TYPES": "US_STOCK,KR_STOCK",
    }

    with patch.dict(os.environ, env, clear=False), \
         patch("core.toss_live_pilot_verification.is_verification_passed", return_value=(True, [], {"verification_id": "hv_cash"})), \
         patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=[rec]), \
         patch("core.toss_live_pilot_ledger.record_live_send_failed") as failed, \
         patch("core.toss_live_pilot_ledger.record_live_send_retryable") as retryable, \
         patch("core.toss_live_pilot_events.record_event", return_value={"ok": True}), \
         patch("core.toss_live_pilot_telegram.resolve_live_transport_for_confirm", return_value=transport), \
         patch("core.toss_live_pilot_telegram.send_autonomous_result_message", return_value=True):
        result = try_autonomous_finalize("cash_short_pilot")

    assert result["live_order_sent"] is False
    assert result["rebalance_needed"] is True
    assert result["cash_blocked"] is True
    assert result["failure_class"] == "cash_blocked_rebalance_needed"
    failed.assert_called_once()
    retryable.assert_not_called()
    assert "cash_blocked_rebalance_needed" in failed.call_args.kwargs["failure_reason"]
