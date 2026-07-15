"""tests/test_toss_role_fallback.py (Task 4.1A)

snapshot 정책 모듈 import가 실패해도 비소유 프로세스가 OAuth/Broker로
fail-open하지 않는다 — except fallback의 role 계약 검증.

실제 Toss URL·토큰·계좌 미사용. import 실패는 builtins.__import__ patch로
실제 재현한다.
"""

from __future__ import annotations

import builtins
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture()
def _policy_import_broken(monkeypatch):
    """core.toss_readonly_snapshot import를 실제로 실패시킨다."""
    real_import = builtins.__import__

    def broken(name, *args, **kwargs):
        if "toss_readonly_snapshot" in name:
            raise ImportError("policy module broken (test)")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "core.toss_readonly_snapshot", raising=False)
    monkeypatch.setattr(builtins, "__import__", broken)
    yield


@pytest.fixture()
def _clean_role_env(monkeypatch):
    monkeypatch.delenv("TOSS_PROCESS_ROLE", raising=False)
    monkeypatch.delenv("TOSS_AUTONOMOUS_MODE", raising=False)
    yield monkeypatch


def _isolated_via_client():
    import core.toss_client as tc
    return tc._broker_access_isolated_for_process()


def _isolated_via_dashboard():
    import core.dashboard_data as dd
    return dd._dashboard_toss_broker_reads_isolated()


# ── 1~5: import 실패 시 argv 기반 role ────────────────────────────

@pytest.mark.parametrize("argv,expect_consumer", [
    (["some_tool.py"], True),            # 1. plain tool
    (["main.py", "briefing"], True),     # 2. briefing
    (["main.py", "dashboard"], True),    # 3. dashboard
    (["main.py", "bot"], False),         # 4. bot → owner
    (["main.py", "monitor"], False),     # 5. monitor → owner
])
def test_fallback_role_by_argv(_policy_import_broken, _clean_role_env,
                               argv, expect_consumer):
    _clean_role_env.setattr(sys, "argv", argv)
    assert _isolated_via_client() is expect_consumer
    assert _isolated_via_dashboard() is expect_consumer


# ── 6~7: 명시 role이 argv보다 우선 ───────────────────────────────

def test_explicit_broker_owner_wins(_policy_import_broken, _clean_role_env):
    _clean_role_env.setenv("TOSS_PROCESS_ROLE", "broker_owner")
    _clean_role_env.setattr(sys, "argv", ["some_tool.py"])
    assert _isolated_via_client() is False
    assert _isolated_via_dashboard() is False


def test_explicit_consumer_wins_over_bot_argv(_policy_import_broken, _clean_role_env):
    _clean_role_env.setenv("TOSS_PROCESS_ROLE", "snapshot_consumer")
    _clean_role_env.setattr(sys, "argv", ["main.py", "bot"])
    assert _isolated_via_client() is True
    assert _isolated_via_dashboard() is True


def test_unknown_role_falls_back_to_argv(_policy_import_broken, _clean_role_env):
    _clean_role_env.setenv("TOSS_PROCESS_ROLE", "weird_value")
    _clean_role_env.setattr(sys, "argv", ["main.py", "bot"])
    assert _isolated_via_client() is False
    _clean_role_env.setattr(sys, "argv", ["main.py", "briefing"])
    assert _isolated_via_client() is True


# ── 8~9: consumer fallback에서 네트워크 도달 0 ───────────────────

def test_consumer_fallback_no_oauth_post(_policy_import_broken, _clean_role_env):
    import core.toss_client as tc
    _clean_role_env.setattr(sys, "argv", ["main.py", "briefing"])
    # 메모리 토큰이 있어도 발급/사용 경로가 열리면 안 된다
    _clean_role_env.setattr(tc, "_mem_token", "stale-token", raising=False)
    _clean_role_env.setattr(tc, "_mem_expires", 9e12, raising=False)
    with patch.object(tc.requests, "post",
                      side_effect=AssertionError("OAuth POST reached")) as post_mock:
        token = tc._get_access_token()
    assert token is None
    assert post_mock.call_count == 0


def test_consumer_fallback_no_broker_get(_policy_import_broken, _clean_role_env):
    import core.toss_client as tc
    _clean_role_env.setattr(sys, "argv", ["main.py", "briefing"])
    with patch.object(tc.requests, "get",
                      side_effect=AssertionError("Broker GET reached")) as get_mock, \
         patch.object(tc.requests, "post",
                      side_effect=AssertionError("OAuth POST reached")):
        try:
            result = tc.get_accounts()
        except Exception as exc:
            pytest.fail(f"fail-closed여야 하는데 예외 발생: {exc}")
    assert get_mock.call_count == 0
    assert result in (None, [], {})   # 빈 결과 fail-closed, 네트워크 미도달


# ── 10: AUTONOMOUS_MODE는 판정과 무관 ────────────────────────────

@pytest.mark.parametrize("mode", [None, "false", "true"])
def test_autonomous_mode_irrelevant(_policy_import_broken, _clean_role_env, mode):
    if mode is not None:
        _clean_role_env.setenv("TOSS_AUTONOMOUS_MODE", mode)
    _clean_role_env.setattr(sys, "argv", ["main.py", "briefing"])
    assert _isolated_via_client() is True
    _clean_role_env.setattr(sys, "argv", ["main.py", "bot"])
    assert _isolated_via_client() is False


# ── Task 4.1A2-A: 정책 반환 비-bool → typed fallback ──────────────

_NON_BOOL_VALUES = [None, 0, 1, "", "false", [], {}]


@pytest.fixture()
def _policy_returns(monkeypatch):
    """정상 import 경로에서 should_consume_snapshot 반환값을 주입."""
    import core.toss_readonly_snapshot as trs

    def set_value(value):
        monkeypatch.setattr(trs, "should_consume_snapshot", lambda: value)
    return set_value


@pytest.mark.parametrize("bad", _NON_BOOL_VALUES)
def test_non_bool_policy_falls_back_briefing_consumer(
        _policy_returns, _clean_role_env, bad):
    _policy_returns(bad)
    _clean_role_env.setattr(sys, "argv", ["main.py", "briefing"])
    assert _isolated_via_client() is True
    assert _isolated_via_dashboard() is True


@pytest.mark.parametrize("bad", _NON_BOOL_VALUES)
def test_non_bool_policy_explicit_consumer(_policy_returns, _clean_role_env, bad):
    _policy_returns(bad)
    _clean_role_env.setenv("TOSS_PROCESS_ROLE", "snapshot_consumer")
    _clean_role_env.setattr(sys, "argv", ["main.py", "bot"])
    assert _isolated_via_client() is True
    assert _isolated_via_dashboard() is True


@pytest.mark.parametrize("bad", _NON_BOOL_VALUES)
def test_non_bool_policy_explicit_owner(_policy_returns, _clean_role_env, bad):
    _policy_returns(bad)
    _clean_role_env.setenv("TOSS_PROCESS_ROLE", "broker_owner")
    _clean_role_env.setattr(sys, "argv", ["some_tool.py"])
    assert _isolated_via_client() is False
    assert _isolated_via_dashboard() is False


@pytest.mark.parametrize("argv", [["main.py", "bot"], ["main.py", "monitor"]])
def test_non_bool_policy_bot_monitor_owner(_policy_returns, _clean_role_env, argv):
    _policy_returns("false")   # 문자열 'false' — truthy 오판 방지의 핵심 케이스
    _clean_role_env.setattr(sys, "argv", argv)
    assert _isolated_via_client() is False
    assert _isolated_via_dashboard() is False


def test_non_bool_consumer_no_oauth_and_no_broker(_policy_returns, _clean_role_env):
    import core.toss_client as tc
    _policy_returns(1)   # truthy 정수 — bool 아님
    _clean_role_env.setattr(sys, "argv", ["main.py", "briefing"])
    _clean_role_env.setattr(tc, "_mem_token", "stale", raising=False)
    _clean_role_env.setattr(tc, "_mem_expires", 9e12, raising=False)
    with patch.object(tc.requests, "post",
                      side_effect=AssertionError("OAuth POST")) as post_mock, \
         patch.object(tc.requests, "get",
                      side_effect=AssertionError("Broker GET")) as get_mock:
        assert tc._get_access_token() is None
        assert tc.get_accounts() in (None, [], {})
    assert post_mock.call_count == 0
    assert get_mock.call_count == 0


@pytest.mark.parametrize("mode", [None, "false", "true"])
def test_non_bool_policy_autonomous_irrelevant(
        _policy_returns, _clean_role_env, mode):
    _policy_returns(None)
    if mode is not None:
        _clean_role_env.setenv("TOSS_AUTONOMOUS_MODE", mode)
    _clean_role_env.setattr(sys, "argv", ["main.py", "briefing"])
    assert _isolated_via_client() is True
    _clean_role_env.setattr(sys, "argv", ["main.py", "bot"])
    assert _isolated_via_client() is False


# ── Task 4.1A3-1: 정책 '호출 예외' parity matrix ──────────────────

@pytest.fixture()
def _policy_call_raises(monkeypatch):
    """import는 성공하지만 호출 시 예외가 나는 정책 함수."""
    import core.toss_readonly_snapshot as trs

    def broken():
        raise RuntimeError("policy call failed")
    monkeypatch.setattr(trs, "should_consume_snapshot", broken)
    yield


_CALL_EXC_MATRIX = [
    # (role, argv, expect_consumer)
    (None, ["main.py", "briefing"], True),
    ("snapshot_consumer", ["main.py", "bot"], True),
    ("broker_owner", ["some_tool.py"], False),
    (None, ["main.py", "bot"], False),
    (None, ["main.py", "monitor"], False),
    ("weird_role", ["main.py", "bot"], False),      # unknown → argv fallback
    ("weird_role", ["main.py", "briefing"], True),
]


@pytest.mark.parametrize("role,argv,expect_consumer", _CALL_EXC_MATRIX)
def test_policy_call_exception_parity(_policy_call_raises, _clean_role_env,
                                      role, argv, expect_consumer):
    if role is not None:
        _clean_role_env.setenv("TOSS_PROCESS_ROLE", role)
    _clean_role_env.setattr(sys, "argv", argv)
    assert _isolated_via_client() is expect_consumer
    assert _isolated_via_dashboard() is expect_consumer


@pytest.mark.parametrize("mode", ["true", "false"])
def test_policy_call_exception_autonomous_irrelevant(
        _policy_call_raises, _clean_role_env, mode):
    _clean_role_env.setenv("TOSS_AUTONOMOUS_MODE", mode)
    _clean_role_env.setattr(sys, "argv", ["main.py", "briefing"])
    assert _isolated_via_client() is True
    assert _isolated_via_dashboard() is True
    _clean_role_env.setattr(sys, "argv", ["main.py", "bot"])
    assert _isolated_via_client() is False
    assert _isolated_via_dashboard() is False


# ── Task 4.1A3-2: consumer 실주문 submit sink POST=0 ─────────────

_FAKE_BUY_BODY = {
    "symbol": "AAPL", "side": "BUY", "quantity": 1,
    "order_type": "limit", "limit_price": 100.0,
}


def _submit_with_sinks(monkeypatch, argv):
    """submit_order를 consumer 상태에서 호출 — 전 네트워크는 AssertionError sink."""
    import requests as requests_lib
    import core.toss_client as tc
    import core.toss_live_order_http as http_mod

    monkeypatch.setattr(sys, "argv", argv)
    # stale memory token 주입 — 사용되면 안 됨
    monkeypatch.setattr(tc, "_mem_token", "stale-token-must-not-be-used",
                        raising=False)
    monkeypatch.setattr(tc, "_mem_expires", 9e12, raising=False)
    # account seq는 fake로 통과시켜 token gate까지 확실히 도달
    monkeypatch.setattr(http_mod, "_resolve_account_seq", lambda seq=None: "1")

    with patch.object(requests_lib, "post",
                      side_effect=AssertionError("order/OAuth POST")) as post_sink, \
         patch.object(requests_lib, "get",
                      side_effect=AssertionError("broker/reconciliation GET")) as get_sink:
        result = http_mod.submit_order(dict(_FAKE_BUY_BODY))
    return result, post_sink, get_sink


def _assert_submit_blocked(result, post_sink, get_sink):
    assert result.get("blocked") is True
    assert result.get("live_order_sent") is False
    assert result.get("reason") == "token_unavailable"
    assert post_sink.call_count == 0
    assert get_sink.call_count == 0
    assert "stale-token-must-not-be-used" not in str(result)


@pytest.mark.parametrize("bad", _NON_BOOL_VALUES)
@pytest.mark.parametrize("argv", [["main.py", "briefing"], ["plain_tool.py"]])
def test_submit_order_sink_non_bool_policy(
        _policy_returns, _clean_role_env, monkeypatch, bad, argv):
    _policy_returns(bad)
    result, post_sink, get_sink = _submit_with_sinks(monkeypatch, argv)
    _assert_submit_blocked(result, post_sink, get_sink)


@pytest.mark.parametrize("argv", [["main.py", "briefing"], ["plain_tool.py"]])
def test_submit_order_sink_policy_call_exception(
        _policy_call_raises, _clean_role_env, monkeypatch, argv):
    result, post_sink, get_sink = _submit_with_sinks(monkeypatch, argv)
    _assert_submit_blocked(result, post_sink, get_sink)
