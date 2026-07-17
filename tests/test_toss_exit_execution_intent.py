"""Shared durable idempotency boundary for autonomous protective SELLs."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

KST = timezone(timedelta(hours=9))


def _intent_module():
    from core import toss_exit_execution_intent as intent
    return intent


def test_decision_ref_uses_symbol_market_day():
    intent = _intent_module()
    before_midnight = datetime(2026, 7, 2, 23, 50, tzinfo=KST)
    after_midnight = datetime(2026, 7, 3, 0, 21, tzinfo=KST)
    next_us_session = datetime(2026, 7, 3, 22, 30, tzinfo=KST)

    first = intent.build_exit_decision_ref("LRCX", "full_exit", before_midnight)
    assert first == intent.build_exit_decision_ref("LRCX", "full_exit", after_midnight)
    assert first != intent.build_exit_decision_ref("LRCX", "full_exit", next_us_session)

    kr_open = datetime(2026, 7, 2, 9, 30, tzinfo=KST)
    kr_later = datetime(2026, 7, 2, 14, 0, tzinfo=KST)
    assert (
        intent.build_exit_decision_ref("005930", "full_exit", kr_open)
        == intent.build_exit_decision_ref("005930", "full_exit", kr_later)
    )
    assert (
        intent.build_exit_decision_ref("005930", "full_exit", kr_open)
        == intent.build_exit_decision_ref("005930.KS", "full_exit", kr_open)
    )
    for malformed in ("123", "1234567"):
        with pytest.raises(ValueError, match="invalid_exit_symbol"):
            intent.build_exit_decision_ref(malformed, "full_exit", kr_open)


def test_decision_ref_must_bind_symbol_market_day_and_intent_class():
    intent = _intent_module()
    current = datetime(2026, 7, 2, 23, 50, tzinfo=KST)
    same_us_day = datetime(2026, 7, 3, 0, 21, tzinfo=KST)
    next_us_day = datetime(2026, 7, 3, 22, 30, tzinfo=KST)
    ref = intent.build_exit_decision_ref("LRCX", "full_exit", current)

    assert intent.exit_decision_ref_matches(ref, "LRCX", same_us_day) is True
    assert intent.exit_decision_ref_matches(ref, "MU", same_us_day) is False
    assert intent.exit_decision_ref_matches(ref, "LRCX", next_us_day) is False
    assert intent.exit_decision_ref_matches(ref, "LRCX", datetime(2026, 7, 2, 23, 50)) is False

    unknown_intent = ref.rsplit(".", 1)[0] + ".unknown_exit"
    assert intent.is_exit_decision_ref(unknown_intent) is False
    assert intent.exit_decision_ref_matches(unknown_intent, "LRCX", current) is False


def test_claim_sent_and_stale_takeover(monkeypatch, tmp_path):
    intent = _intent_module()
    monkeypatch.setenv("TOSS_EXIT_INTENT_STATE_PATH", str(tmp_path / "intents.json"))
    now = datetime(2026, 7, 2, 22, 30, tzinfo=KST)
    ref = intent.build_exit_decision_ref("LRCX", "full_exit", now)

    first = intent.claim_exit_intent(ref, "pilot-a", now=now)
    assert first["ok"] is True
    same_pilot = intent.claim_exit_intent(
        ref, "pilot-a", now=now + timedelta(seconds=1),
    )
    assert same_pilot == {
        "ok": False,
        "reason": "exit_intent_reserved",
        "prior_pilot_id": "pilot-a",
    }
    blocked = intent.claim_exit_intent(ref, "pilot-b", now=now + timedelta(minutes=1))
    assert blocked == {
        "ok": False,
        "reason": "exit_intent_reserved",
        "prior_pilot_id": "pilot-a",
    }

    stale = intent.claim_exit_intent(ref, "pilot-b", now=now + timedelta(minutes=31))
    assert stale["ok"] is False
    assert stale["reason"] == "exit_intent_reconcile_required"
    assert stale["prior_pilot_id"] == "pilot-a"
    wrong_ts = intent.takeover_exit_intent(
        ref,
        "pilot-a",
        "pilot-b",
        expected_decision_ref=stale["prior_decision_ref"],
        expected_updated_at="2000-01-01T00:00:00+09:00",
        now=now + timedelta(minutes=31),
    )
    assert wrong_ts == {"ok": False, "reason": "exit_intent_takeover_conflict"}
    wrong_ref = intent.takeover_exit_intent(
        ref,
        "pilot-a",
        "pilot-b",
        expected_decision_ref=intent.build_exit_decision_ref(
            "LRCX", "partial_exit", now,
        ),
        expected_updated_at=stale["prior_updated_at"],
        now=now + timedelta(minutes=31),
    )
    assert wrong_ref == {"ok": False, "reason": "exit_intent_takeover_conflict"}
    still_owned = intent.claim_exit_intent(
        ref, "pilot-b", now=now + timedelta(minutes=31),
    )
    assert still_owned["reason"] == "exit_intent_reconcile_required"
    assert still_owned["prior_pilot_id"] == "pilot-a"
    assert still_owned["prior_updated_at"] == stale["prior_updated_at"]
    assert intent.takeover_exit_intent(
        ref,
        "pilot-a",
        "pilot-b",
        expected_decision_ref=stale["prior_decision_ref"],
        expected_updated_at=stale["prior_updated_at"],
        now=now + timedelta(minutes=31),
    )["ok"] is True
    assert intent.mark_exit_intent_sent(ref, "pilot-b", now=now + timedelta(minutes=32))["ok"] is True
    sent = intent.claim_exit_intent(ref, "pilot-c", now=now + timedelta(minutes=33))
    assert sent["ok"] is False
    assert sent["reason"] == "exit_intent_already_sent"


def test_corrupt_nested_state_fails_closed(monkeypatch, tmp_path):
    intent = _intent_module()
    path = tmp_path / "intents.json"
    path.write_text('{"intents":"truthy"}', encoding="utf-8")
    monkeypatch.setenv("TOSS_EXIT_INTENT_STATE_PATH", str(path))
    result = intent.claim_exit_intent(
        "execution_decision:exit.LRCX.20260702.full_exit",
        "pilot-a",
        now=datetime(2026, 7, 2, 22, 30, tzinfo=KST),
    )
    assert result == {"ok": False, "reason": "exit_intent_state_invalid"}


def test_bool_state_version_fails_closed(monkeypatch, tmp_path):
    intent = _intent_module()
    path = tmp_path / "intents.json"
    path.write_text('{"version":true,"intents":{}}', encoding="utf-8")
    monkeypatch.setenv("TOSS_EXIT_INTENT_STATE_PATH", str(path))
    result = intent.claim_exit_intent(
        "execution_decision:exit.LRCX.20260702.full_exit",
        "pilot-a",
    )
    assert result == {"ok": False, "reason": "exit_intent_state_invalid"}


def test_different_exit_dispositions_share_symbol_day_scope(monkeypatch, tmp_path):
    intent = _intent_module()
    monkeypatch.setenv("TOSS_EXIT_INTENT_STATE_PATH", str(tmp_path / "intents.json"))
    now = datetime(2026, 7, 2, 22, 30, tzinfo=KST)
    full = intent.build_exit_decision_ref("LRCX", "full_exit", now)
    partial = intent.build_exit_decision_ref("LRCX", "partial_exit", now)
    rebalance = intent.build_exit_decision_ref("LRCX", "rebalance", now)

    assert intent.claim_exit_intent(full, "pilot-a", now=now)["ok"] is True
    blocked = intent.claim_exit_intent(partial, "pilot-b", now=now + timedelta(seconds=1))
    assert blocked["ok"] is False
    assert blocked["reason"] == "exit_intent_reserved"
    assert intent.mark_exit_intent_sent(full, "pilot-a", now=now + timedelta(seconds=2))["ok"] is True
    sent = intent.claim_exit_intent(rebalance, "pilot-c", now=now + timedelta(seconds=3))
    assert sent["ok"] is False
    assert sent["reason"] == "exit_intent_already_sent"


def test_intent_state_save_failure_blocks_claim(monkeypatch, tmp_path):
    intent = _intent_module()
    monkeypatch.setenv("TOSS_EXIT_INTENT_STATE_PATH", str(tmp_path / "intents.json"))
    ref = intent.build_exit_decision_ref(
        "LRCX", "full_exit", datetime(2026, 7, 2, 22, 30, tzinfo=KST),
    )
    with patch.object(intent, "_write_state", return_value=False):
        result = intent.claim_exit_intent(ref, "pilot-a")
    assert result == {"ok": False, "reason": "exit_intent_state_unavailable"}


def test_stale_claim_reconciles_broker_before_takeover(monkeypatch, tmp_path):
    intent = _intent_module()
    monkeypatch.setenv("TOSS_EXIT_INTENT_STATE_PATH", str(tmp_path / "intents.json"))
    now = datetime.now(KST)
    ref = intent.build_exit_decision_ref("LRCX", "full_exit", now)
    assert intent.claim_exit_intent(
        ref, "pilot-a", now=now - timedelta(minutes=31),
    )["ok"] is True
    current = {"pilot_id": "pilot-b", "decision_ref": ref, "symbol": "LRCX", "side": "sell"}
    prior = {"pilot_id": "pilot-a", "decision_ref": ref, "symbol": "LRCX",
             "side": "sell", "status": "previewed", "live_order_sent": False}

    with patch("core.toss_live_order_http.list_orders", side_effect=[
        {"ok": True, "status": "OPEN", "complete": True,
         "orders": [{"client_order_id": "pilot-a"}]},
        {"ok": True, "status": "CLOSED", "complete": True, "orders": []},
    ]):
        from core.toss_autonomous_finalizer import _claim_exit_intent
        found = _claim_exit_intent(current, "pilot-b", [current, prior])
    assert found["ok"] is False
    assert found["reason"] == "exit_intent_already_sent"


def test_stale_claim_fails_closed_when_broker_unavailable(monkeypatch, tmp_path):
    intent = _intent_module()
    monkeypatch.setenv("TOSS_EXIT_INTENT_STATE_PATH", str(tmp_path / "intents.json"))
    now = datetime.now(KST)
    ref = intent.build_exit_decision_ref("LRCX", "full_exit", now)
    intent.claim_exit_intent(ref, "pilot-a", now=now - timedelta(minutes=31))
    current = {"pilot_id": "pilot-b", "decision_ref": ref, "symbol": "LRCX", "side": "sell"}
    prior = {"pilot_id": "pilot-a", "decision_ref": ref, "symbol": "LRCX",
             "side": "sell", "status": "previewed", "live_order_sent": False}

    with patch("core.toss_live_order_http.list_orders", side_effect=[
        {"ok": True, "status": "OPEN", "complete": True, "orders": []},
        {"ok": False, "status": "CLOSED", "complete": False, "orders": []},
    ]):
        from core.toss_autonomous_finalizer import _claim_exit_intent
        blocked = _claim_exit_intent(current, "pilot-b", [current, prior])
    assert blocked["ok"] is False
    assert blocked["reason"] == "exit_intent_reconcile_unavailable"


def test_stale_claim_takes_over_only_after_exact_broker_absence(monkeypatch, tmp_path):
    intent = _intent_module()
    monkeypatch.setenv("TOSS_EXIT_INTENT_STATE_PATH", str(tmp_path / "intents.json"))
    now = datetime.now(KST)
    ref = intent.build_exit_decision_ref("LRCX", "full_exit", now)
    intent.claim_exit_intent(ref, "pilot-a", now=now - timedelta(minutes=31))
    current = {"pilot_id": "pilot-b", "decision_ref": ref, "symbol": "LRCX", "side": "sell"}
    prior = {"pilot_id": "pilot-a", "decision_ref": ref, "symbol": "LRCX",
             "side": "sell", "status": "previewed", "live_order_sent": False}

    with patch("core.toss_live_order_http.list_orders", side_effect=[
        {"ok": True, "status": "OPEN", "complete": True, "orders": []},
        {"ok": True, "status": "CLOSED", "complete": True, "orders": []},
    ]):
        from core.toss_autonomous_finalizer import _claim_exit_intent
        takeover = _claim_exit_intent(current, "pilot-b", [current, prior])
    assert takeover["ok"] is True
    assert takeover["reason"] == "exit_intent_taken_over"


def test_stale_claim_rejects_wrong_broker_scope(monkeypatch, tmp_path):
    intent = _intent_module()
    monkeypatch.setenv("TOSS_EXIT_INTENT_STATE_PATH", str(tmp_path / "intents.json"))
    now = datetime.now(KST)
    ref = intent.build_exit_decision_ref("LRCX", "full_exit", now)
    intent.claim_exit_intent(ref, "pilot-a", now=now - timedelta(minutes=31))
    current = {"pilot_id": "pilot-b", "decision_ref": ref, "symbol": "LRCX", "side": "sell"}

    with patch("core.toss_live_order_http.list_orders", side_effect=[
        {"ok": True, "status": "CLOSED", "complete": True, "orders": []},
        {"ok": True, "status": "CLOSED", "complete": True, "orders": []},
    ]):
        from core.toss_autonomous_finalizer import _claim_exit_intent
        blocked = _claim_exit_intent(current, "pilot-b", [current])
    assert blocked["ok"] is False
    assert blocked["reason"] == "exit_intent_reconcile_unavailable"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({}, "malformed_orders_body"),
        ({"result": "truthy"}, "malformed_orders_result"),
        ({"result": {"items": [], "hasNext": True}}, "orders_pagination_incomplete"),
        ({"result": [{}, "bad-row"]}, "malformed_order_row"),
    ],
)
def test_list_orders_fails_closed_on_incomplete_or_malformed_body(body, reason):
    from core import toss_live_order_http as order_http

    with patch.object(order_http, "_safe_get", return_value={"ok": True, "body": body}):
        result = order_http.list_orders("OPEN", account_seq="safe-seq")
    assert result["ok"] is False
    assert result["complete"] is False
    assert result["reason"] == reason


def test_list_orders_marks_only_exact_complete_result_authoritative():
    from core import toss_live_order_http as order_http

    body = {"result": {"items": [], "hasNext": False, "nextCursor": None}}
    with patch.object(order_http, "_safe_get", return_value={"ok": True, "body": body}):
        result = order_http.list_orders("OPEN", account_seq="safe-seq")
    assert result == {"ok": True, "status": "OPEN", "orders": [], "complete": True}


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"result": []}, "malformed_orders_result"),
        ({"result": {"orders": []}}, "malformed_orders_pagination"),
        ({"result": {"orders": [], "hasNext": False}}, "malformed_orders_pagination"),
        ({"result": {"orders": [], "nextCursor": None}}, "malformed_orders_pagination"),
        ({"result": {"orders": [], "hasNext": 0, "nextCursor": None}},
         "malformed_orders_pagination"),
    ],
)
def test_list_orders_requires_official_closed_pagination_envelope(body, reason):
    from core import toss_live_order_http as order_http

    with patch.object(order_http, "_safe_get", return_value={"ok": True, "body": body}):
        result = order_http.list_orders("CLOSED", account_seq="safe-seq")

    assert result == {
        "ok": False,
        "reason": reason,
        "status": "CLOSED",
        "orders": [],
        "complete": False,
    }


def test_list_orders_keeps_legacy_list_result_for_open_only():
    from core import toss_live_order_http as order_http

    with patch.object(order_http, "_safe_get", return_value={"ok": True, "body": {"result": []}}):
        result = order_http.list_orders("OPEN", account_seq="safe-seq")

    assert result == {"ok": True, "status": "OPEN", "orders": [], "complete": True}


def test_list_orders_collects_every_closed_cursor_page_before_authoritative_success():
    from core import toss_live_order_http as order_http

    pages = [
        {"ok": True, "body": {"result": {
            "orders": [{"orderId": "order-a", "status": "FILLED"}],
            "nextCursor": "cursor-2", "hasNext": True,
        }}},
        {"ok": True, "body": {"result": {
            "orders": [{"orderId": "order-b", "status": "CANCELED"}],
            "nextCursor": None, "hasNext": False,
        }}},
    ]
    with patch.object(order_http, "_safe_get", side_effect=pages) as fetch:
        result = order_http.list_orders("CLOSED", account_seq="safe-seq")

    assert result["ok"] is True
    assert result["complete"] is True
    assert [row["broker_order_id"] for row in result["orders"]] == ["order-a", "order-b"]
    assert [call.kwargs["params"] for call in fetch.call_args_list] == [
        {"status": "CLOSED", "limit": 100},
        {"status": "CLOSED", "limit": 100, "cursor": "cursor-2"},
    ]


def test_list_orders_discards_partial_closed_pages_when_later_page_fails():
    from core import toss_live_order_http as order_http

    pages = [
        {"ok": True, "body": {"result": {
            "orders": [{"orderId": "order-a", "status": "FILLED"}],
            "nextCursor": "cursor-2", "hasNext": True,
        }}},
        {"ok": False, "reason": "http_429"},
    ]
    with patch.object(order_http, "_safe_get", side_effect=pages):
        result = order_http.list_orders("CLOSED", account_seq="safe-seq")

    assert result["ok"] is False
    assert result["complete"] is False
    assert result["orders"] == []
    assert result["reason"] == "http_429"


def test_list_orders_rejects_repeated_closed_cursor_without_partial_truth():
    from core import toss_live_order_http as order_http

    repeated = {"ok": True, "body": {"result": {
        "orders": [{"orderId": "order-a", "status": "FILLED"}],
        "nextCursor": "same-cursor", "hasNext": True,
    }}}
    with patch.object(order_http, "_safe_get", side_effect=[repeated, repeated]):
        result = order_http.list_orders("CLOSED", account_seq="safe-seq")

    assert result == {
        "ok": False,
        "reason": "orders_pagination_cycle",
        "status": "CLOSED",
        "orders": [],
        "complete": False,
    }


def test_list_orders_rejects_multi_cursor_cycle_without_partial_truth():
    from core import toss_live_order_http as order_http

    def page(order_id, cursor):
        return {"ok": True, "body": {"result": {
            "orders": [{"orderId": order_id, "status": "FILLED"}],
            "nextCursor": cursor, "hasNext": True,
        }}}

    with patch.object(order_http, "_safe_get", side_effect=[
        page("order-a", "cursor-a"),
        page("order-b", "cursor-b"),
        page("order-c", "cursor-a"),
    ]):
        result = order_http.list_orders("CLOSED", account_seq="safe-seq")

    assert result == {
        "ok": False,
        "reason": "orders_pagination_cycle",
        "status": "CLOSED",
        "orders": [],
        "complete": False,
    }


def test_list_orders_discards_partial_pages_at_total_deadline(monkeypatch):
    from core import toss_live_order_http as order_http

    pages = [
        {"ok": True, "body": {"result": {
            "orders": [{"orderId": "order-a", "status": "FILLED"}],
            "nextCursor": "cursor-2", "hasNext": True,
        }}},
        {"ok": True, "body": {"result": {
            "orders": [{"orderId": "order-b", "status": "FILLED"}],
            "nextCursor": None, "hasNext": False,
        }}},
    ]
    monkeypatch.setattr(order_http, "_ORDER_LIST_DEADLINE_SEC", 20.0, raising=False)
    real_monotonic = time.monotonic
    base = real_monotonic()
    worker_times = iter([base + 1.0, base + 10.0, base + 21.0])

    def monotonic():
        if threading.current_thread().name == "toss-order-history-worker":
            return next(worker_times, base + 21.0)
        return real_monotonic()

    monkeypatch.setattr(order_http.time, "monotonic", monotonic)
    with patch.object(order_http, "_safe_get", side_effect=pages) as fetch:
        result = order_http.list_orders("CLOSED", account_seq="safe-seq")

    assert result == {
        "ok": False,
        "reason": "orders_pagination_deadline",
        "status": "CLOSED",
        "orders": [],
        "complete": False,
    }
    assert fetch.call_count == 1
    assert 0 < fetch.call_args.kwargs["timeout"] <= 20.0


def test_list_orders_discards_terminal_page_that_returns_after_total_deadline(monkeypatch):
    from core import toss_live_order_http as order_http

    terminal_page = {"ok": True, "body": {"result": {
        "orders": [], "nextCursor": None, "hasNext": False,
    }}}
    real_monotonic = time.monotonic
    base = real_monotonic()
    worker_times = iter([base, base + 21.0])

    def monotonic():
        if threading.current_thread().name == "toss-order-history-worker":
            return next(worker_times, base + 21.0)
        return real_monotonic()

    monkeypatch.setattr(order_http.time, "monotonic", monotonic)
    with patch.object(order_http, "_safe_get", return_value=terminal_page) as fetch:
        result = order_http.list_orders("CLOSED", account_seq="safe-seq")

    assert result == {
        "ok": False,
        "reason": "orders_pagination_deadline",
        "status": "CLOSED",
        "orders": [],
        "complete": False,
    }
    assert fetch.call_count == 1


def test_safe_get_never_expands_callers_remaining_timeout():
    from core import toss_live_order_http as order_http

    response = Mock(status_code=200)
    response.json.return_value = {"result": []}
    with patch.object(order_http.tc, "_get_access_token", return_value="test-token"), \
            patch("requests.get", return_value=response) as request:
        result = order_http._safe_get(
            "/api/v1/orders",
            account_seq="safe-seq",
            params={"status": "CLOSED"},
            timeout=0.05,
        )

    assert result["ok"] is True
    assert request.call_args.kwargs["timeout"] == 0.05


def test_list_orders_hard_deadline_keeps_one_inflight_get(monkeypatch):
    from core import toss_live_order_http as order_http

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocked_get(*args, **kwargs):
        started.set()
        release.wait(timeout=0.3)
        finished.set()
        return {"ok": True, "body": {"result": {
            "orders": [], "nextCursor": None, "hasNext": False,
        }}}

    monkeypatch.setattr(order_http, "_ORDER_LIST_DEADLINE_SEC", 0.03)
    try:
        with patch.object(order_http, "_safe_get", side_effect=blocked_get) as fetch:
            began = time.monotonic()
            first = order_http.list_orders("CLOSED", account_seq="safe-seq")
            elapsed = time.monotonic() - began
            assert started.wait(timeout=0.1)
            second = order_http.list_orders("CLOSED", account_seq="safe-seq")

        assert elapsed < 0.15
        assert first["reason"] == "orders_pagination_deadline"
        assert first["orders"] == [] and first["complete"] is False
        assert second["reason"] == "orders_request_inflight"
        assert second["orders"] == [] and second["complete"] is False
        assert fetch.call_count == 1
    finally:
        release.set()
        assert finished.wait(timeout=1)
        slot = getattr(order_http, "_ORDER_JOB_INFLIGHT", None)
        if slot:
            assert slot["done"].wait(timeout=1)


def test_list_orders_hard_deadline_includes_account_resolution(monkeypatch):
    from core import toss_live_order_http as order_http

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocked_resolution(_account_seq):
        started.set()
        release.wait(timeout=0.3)
        finished.set()
        return "safe-seq"

    terminal = {"ok": True, "body": {"result": {
        "orders": [], "nextCursor": None, "hasNext": False,
    }}}
    monkeypatch.setattr(order_http, "_ORDER_LIST_DEADLINE_SEC", 0.03)
    try:
        with patch.object(order_http, "_resolve_account_seq", side_effect=blocked_resolution), \
                patch.object(order_http, "_safe_get", return_value=terminal):
            began = time.monotonic()
            first = order_http.list_orders("CLOSED")
            elapsed = time.monotonic() - began
            assert started.wait(timeout=0.1)
            second = order_http.list_orders("CLOSED")

        assert elapsed < 0.15
        assert first["reason"] == "orders_pagination_deadline"
        assert first["orders"] == [] and first["complete"] is False
        assert second["reason"] == "orders_request_inflight"
        assert second["orders"] == [] and second["complete"] is False
    finally:
        release.set()
        assert finished.wait(timeout=1)


def test_order_job_worker_is_prestarted_daemon():
    from core import toss_live_order_http as order_http

    assert order_http._ORDER_JOB_THREAD.daemon is True
    assert order_http._ORDER_JOB_THREAD.is_alive() is True


def test_list_orders_worker_exception_and_wrong_result_fail_closed():
    from core import toss_live_order_http as order_http

    with patch.object(order_http, "_resolve_account_seq", side_effect=RuntimeError("safe")):
        raised = order_http.list_orders("CLOSED")
    with patch.object(order_http, "_list_orders_sync", return_value=None):
        malformed = order_http.list_orders("CLOSED", account_seq="safe-seq")

    assert raised["reason"] == "request_exception"
    assert raised["orders"] == [] and raised["complete"] is False
    assert malformed["reason"] == "malformed_request_result"
    assert malformed["orders"] == [] and malformed["complete"] is False


def test_list_orders_concurrent_caller_reuses_single_inflight_worker():
    from core import toss_live_order_http as order_http

    started = threading.Event()
    release = threading.Event()
    first = {}

    def blocked_get(*args, **kwargs):
        started.set()
        release.wait(timeout=1)
        return {"ok": True, "body": {"result": {
            "orders": [], "nextCursor": None, "hasNext": False,
        }}}

    def call_first():
        first.update(order_http.list_orders("CLOSED", account_seq="safe-seq"))

    with patch.object(order_http, "_safe_get", side_effect=blocked_get) as fetch:
        caller = threading.Thread(target=call_first)
        caller.start()
        try:
            assert started.wait(timeout=1)
            second = order_http.list_orders("CLOSED", account_seq="safe-seq")
            assert second["reason"] == "orders_request_inflight"
            assert second["orders"] == [] and second["complete"] is False
            assert fetch.call_count == 1
        finally:
            release.set()
            caller.join(timeout=1)

    assert caller.is_alive() is False
    assert first["ok"] is True and first["complete"] is True


def test_dispatch_lock_is_process_wide_and_released_on_process_death(monkeypatch, tmp_path):
    from core import toss_exit_execution_intent as intent

    state_path = tmp_path / "intents.json"
    monkeypatch.setenv("TOSS_EXIT_INTENT_STATE_PATH", str(state_path))
    decision_ref = intent.build_exit_decision_ref(
        "LRCX", "full_exit", datetime.now(KST),
    )
    repo_root = Path(__file__).resolve().parent.parent
    child_code = """
import os
import sys
import time
os.environ["TOSS_EXIT_INTENT_STATE_PATH"] = sys.argv[1]
from core.toss_exit_execution_intent import acquire_exit_dispatch_lock
lock = acquire_exit_dispatch_lock(sys.argv[2])
print("LOCKED" if lock.get("ok") is True else lock.get("reason"), flush=True)
time.sleep(30)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(state_path), decision_ref],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "LOCKED"
        blocked = intent.acquire_exit_dispatch_lock(decision_ref)
        assert blocked["ok"] is False
        assert blocked["reason"] == "exit_intent_inflight"
    finally:
        child.kill()
        child.wait(timeout=3)

    recovered = intent.acquire_exit_dispatch_lock(decision_ref)
    assert recovered["ok"] is True
    intent.release_exit_dispatch_lock(recovered)


def test_different_dispositions_share_process_dispatch_lock(monkeypatch, tmp_path):
    intent = _intent_module()
    monkeypatch.setenv("TOSS_EXIT_INTENT_STATE_PATH", str(tmp_path / "intents.json"))
    now = datetime(2026, 7, 2, 22, 30, tzinfo=KST)
    full = intent.build_exit_decision_ref("LRCX", "full_exit", now)
    partial = intent.build_exit_decision_ref("LRCX", "partial_exit", now)
    first = intent.acquire_exit_dispatch_lock(full)
    assert first["ok"] is True
    try:
        blocked = intent.acquire_exit_dispatch_lock(partial)
        assert blocked == {"ok": False, "reason": "exit_intent_inflight"}
    finally:
        intent.release_exit_dispatch_lock(first)


def test_process_death_after_sink_acceptance_requires_reconciliation(
    monkeypatch, tmp_path,
):
    """Crash between irreversible sink and sent persist must never resend."""
    intent = _intent_module()
    state_path = tmp_path / "intents.json"
    sink_path = tmp_path / "sink.txt"
    monkeypatch.setenv("TOSS_EXIT_INTENT_STATE_PATH", str(state_path))
    decision_ref = intent.build_exit_decision_ref(
        "LRCX", "full_exit", datetime.now(KST),
    )
    script = r'''
import os, sys
from pathlib import Path
from unittest.mock import patch

state_path, sink_path, decision_ref = sys.argv[1:4]
os.environ.update({
    "TOSS_EXIT_INTENT_STATE_PATH": state_path,
    "TOSS_LIVE_PILOT_ENABLED": "true",
    "TOSS_LIVE_ORDER_ALLOWED": "true",
    "TOSS_LIVE_ADAPTER_ENABLED": "true",
    "TOSS_AUTONOMOUS_MODE": "true",
})
record = {
    "pilot_id": "pilot-crash",
    "decision_ref": decision_ref,
    "symbol": "LRCX", "side": "sell", "quantity": 1,
    "limit_price": 302.0, "estimated_amount_krw": 420000,
    "status": "previewed", "blocks": [], "live_order_sent": False,
}
def accepted(*args, **kwargs):
    Path(sink_path).write_text("accepted\n", encoding="utf-8")
    os._exit(73)
with patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=[record]), \
     patch("core.toss_live_pilot_verification.is_verification_passed",
           return_value=(True, [], {"verification_id": "hv", "status": "PASS"})), \
     patch("core.toss_live_pilot_adapter.can_send_live_pilot_order", return_value=(True, [])), \
     patch("core.toss_live_pilot_telegram.resolve_live_transport_for_confirm", return_value=object()), \
     patch("core.toss_live_pilot_adapter.dispatch_toss_order_live", side_effect=accepted), \
     patch("core.toss_autonomous_finalizer._record_event"):
    from core.toss_autonomous_finalizer import try_autonomous_finalize
    try_autonomous_finalize("pilot-crash")
raise SystemExit(99)
'''
    child = subprocess.run(
        [sys.executable, "-c", script, str(state_path), str(sink_path), decision_ref],
        cwd=str(Path(__file__).resolve().parent.parent),
        timeout=5,
        check=False,
    )
    assert child.returncode == 73
    assert sink_path.read_text(encoding="utf-8") == "accepted\n"

    env = {
        "TOSS_LIVE_PILOT_ENABLED": "true",
        "TOSS_LIVE_ORDER_ALLOWED": "true",
        "TOSS_LIVE_ADAPTER_ENABLED": "true",
        "TOSS_AUTONOMOUS_MODE": "true",
    }
    record = {
        "pilot_id": "pilot-crash",
        "decision_ref": decision_ref,
        "symbol": "LRCX", "side": "sell", "quantity": 1,
        "limit_price": 302.0, "estimated_amount_krw": 420_000,
        "status": "previewed", "blocks": [], "live_order_sent": False,
    }
    with patch.dict(os.environ, env, clear=False), \
         patch.object(intent, "_LEASE", timedelta(0)), \
         patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=[record]), \
         patch("core.toss_live_pilot_verification.is_verification_passed",
               return_value=(True, [], {"verification_id": "hv", "status": "PASS"})), \
         patch("core.toss_live_pilot_adapter.can_send_live_pilot_order", return_value=(True, [])), \
         patch("core.toss_live_pilot_telegram.resolve_live_transport_for_confirm", return_value=object()), \
         patch("core.toss_live_order_http.list_orders", side_effect=[
             {"ok": True, "status": "OPEN", "complete": True,
              "orders": [{"client_order_id": "pilot-crash"}]},
             {"ok": True, "status": "CLOSED", "complete": True, "orders": []},
         ]), \
         patch("core.toss_live_pilot_adapter.dispatch_toss_order_live") as dispatch, \
         patch("core.toss_live_pilot_ledger.record_live_sent",
               side_effect=[OSError("synthetic_io"), {"ok": True}]) as record_reconciled, \
         patch("core.toss_autonomous_finalizer._record_event"):
        from core.toss_autonomous_finalizer import try_autonomous_finalize
        first_retry = try_autonomous_finalize("pilot-crash", allow_retry=True)
        second_retry = try_autonomous_finalize("pilot-crash", allow_retry=True)

    assert first_retry["live_order_sent"] is False
    assert first_retry["reason"] == "exit_intent_already_sent"
    assert second_retry["live_order_sent"] is False
    assert second_retry["reason"] == "exit_intent_already_sent"
    dispatch.assert_not_called()
    assert record_reconciled.call_count == 2
    record_reconciled.assert_called_with("pilot-crash", broker_order_id="")
    assert sink_path.read_text(encoding="utf-8").count("accepted") == 1


def test_finalizer_dispatches_once_for_two_pilots_same_exit_intent(monkeypatch, tmp_path):
    intent = _intent_module()
    monkeypatch.setenv("TOSS_EXIT_INTENT_STATE_PATH", str(tmp_path / "intents.json"))
    env = {
        "TOSS_LIVE_PILOT_ENABLED": "true",
        "TOSS_LIVE_ORDER_ALLOWED": "true",
        "TOSS_LIVE_ADAPTER_ENABLED": "true",
        "TOSS_AUTONOMOUS_MODE": "true",
    }
    decision_ref = intent.build_exit_decision_ref(
        "LRCX", "full_exit", datetime.now(KST),
    )
    records = [
        {
            "pilot_id": pilot_id,
            "decision_ref": decision_ref,
            "symbol": "LRCX",
            "side": "sell",
            "quantity": 10,
            "limit_price": 302.0,
            "estimated_amount_krw": 4_200_000,
            "status": "previewed",
            "blocks": [],
            "live_order_sent": False,
        }
        for pilot_id in ("pilot-a", "pilot-b")
    ]
    transport_result = {
        "ok": True,
        "live_order_sent": True,
        "broker_order_id": "safe-order",
        "broker_confirmed": True,
        "broker_order_status": "OPEN",
    }
    with patch.dict(os.environ, env, clear=False), \
         patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=records), \
         patch("core.toss_live_pilot_verification.is_verification_passed",
               return_value=(True, [], {"verification_id": "hv", "status": "PASS"})), \
         patch("core.toss_live_pilot_adapter.can_send_live_pilot_order", return_value=(True, [])), \
         patch("core.toss_live_pilot_telegram.resolve_live_transport_for_confirm", return_value=object()), \
         patch("core.toss_live_pilot_adapter.dispatch_toss_order_live",
               return_value=transport_result) as dispatch, \
         patch("core.toss_live_pilot_ledger.record_live_sent"), \
         patch("core.toss_live_pilot_telegram.send_autonomous_result_message", return_value=True), \
         patch("core.toss_autonomous_finalizer._record_event"):
        from core.toss_autonomous_finalizer import try_autonomous_finalize
        first = try_autonomous_finalize("pilot-a")
        second = try_autonomous_finalize("pilot-b")

    assert first["live_order_sent"] is True
    assert second["live_order_sent"] is False
    assert second["reason"] == "exit_intent_already_sent"
    dispatch.assert_called_once()


def test_stale_takeover_cannot_overtake_inflight_transport(monkeypatch, tmp_path):
    """stale reconciliation과 기존 transport가 겹쳐도 실제 sink는 한 번만 진입."""
    from core import toss_exit_execution_intent as intent

    monkeypatch.setenv("TOSS_EXIT_INTENT_STATE_PATH", str(tmp_path / "intents.json"))
    env = {
        "TOSS_LIVE_PILOT_ENABLED": "true",
        "TOSS_LIVE_ORDER_ALLOWED": "true",
        "TOSS_LIVE_ADAPTER_ENABLED": "true",
        "TOSS_AUTONOMOUS_MODE": "true",
    }
    decision_ref = intent.build_exit_decision_ref(
        "LRCX", "full_exit", datetime.now(KST),
    )
    records = [
        {
            "pilot_id": pilot_id,
            "decision_ref": decision_ref,
            "symbol": "LRCX",
            "side": "sell",
            "quantity": 1,
            "limit_price": 302.0,
            "estimated_amount_krw": 420_000,
            "status": "previewed",
            "blocks": [],
            "live_order_sent": False,
        }
        for pilot_id in ("pilot-a", "pilot-b")
    ]
    a_entered = threading.Event()
    allow_a = threading.Event()
    b_started = threading.Event()
    b_entered = threading.Event()
    calls: list[str] = []
    results: dict[str, dict] = {}

    def dispatch(payload, policy, transport=None):
        pilot_id = payload["pilot_id"]
        calls.append(pilot_id)
        if pilot_id == "pilot-a":
            a_entered.set()
            assert allow_a.wait(timeout=10)
        else:
            b_entered.set()
        return {
            "ok": True,
            "live_order_sent": True,
            "broker_order_id": f"safe-{pilot_id}",
            "broker_order_status": "OPEN",
        }

    def finalize(pilot_id):
        if pilot_id == "pilot-b":
            b_started.set()
        from core.toss_autonomous_finalizer import try_autonomous_finalize
        results[pilot_id] = try_autonomous_finalize(pilot_id)

    with patch.dict(os.environ, env, clear=False), \
         patch.object(intent, "_LEASE", timedelta(0)), \
         patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
               return_value={
                   "autonomous_mode": True,
                   "autonomous_kill_switch": False,
                   "live_order_allowed": True,
                   "adapter_status": "enabled",
               }), \
         patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=records), \
         patch("core.toss_live_pilot_verification.is_verification_passed",
               return_value=(True, [], {"verification_id": "hv", "status": "PASS"})), \
         patch("core.toss_live_pilot_adapter.can_send_live_pilot_order", return_value=(True, [])), \
         patch("core.toss_live_pilot_telegram.resolve_live_transport_for_confirm", return_value=object()), \
         patch("core.toss_live_order_http.list_orders",
               side_effect=[
                   {"ok": True, "status": "OPEN", "complete": True, "orders": []},
                   {"ok": True, "status": "CLOSED", "complete": True, "orders": []},
               ]), \
         patch("core.toss_live_pilot_adapter.dispatch_toss_order_live", side_effect=dispatch), \
         patch("core.toss_live_pilot_ledger.record_live_sent"), \
         patch("core.toss_live_pilot_telegram.send_autonomous_result_message", return_value=True), \
         patch("core.toss_autonomous_finalizer._record_event"):
        first = threading.Thread(target=finalize, args=("pilot-a",), daemon=True)
        second = threading.Thread(target=finalize, args=("pilot-b",), daemon=True)
        first.start()
        overtook = False
        try:
            assert a_entered.wait(timeout=10), (
                f"pilot-a did not reach transport: result={results.get('pilot-a')} "
                f"alive={first.is_alive()}"
            )
            second.start()
            assert b_started.wait(timeout=10)
            overtook = b_entered.wait(timeout=0.5)
        finally:
            allow_a.set()
            first.join(timeout=10)
            if second.ident is not None:
                second.join(timeout=10)
        from core.toss_autonomous_finalizer import try_autonomous_finalize
        retried = try_autonomous_finalize("pilot-b")

    assert not first.is_alive()
    assert not second.is_alive()
    assert overtook is False
    assert calls == ["pilot-a"]
    assert results["pilot-a"]["live_order_sent"] is True
    assert results["pilot-b"]["live_order_sent"] is False
    assert results["pilot-b"]["reason"] == "exit_intent_inflight"
    assert retried["live_order_sent"] is False
    assert retried["reason"] == "exit_intent_already_sent"


@pytest.mark.parametrize(
    ("transport_result", "claim_retained"),
    [
        ({"ok": False, "live_order_sent": True, "reason": "contradictory_result"}, True),
        ({"ok": True, "live_order_sent": 1, "reason": "non_bool_result"}, True),
        ({"ok": False, "live_order_sent": False, "blocked": False, "reason": "network_error"}, True),
        (TimeoutError("synthetic_timeout"), True),
        ({"ok": False, "live_order_sent": False, "blocked": True, "reason": "account_unavailable"}, False),
    ],
)
def test_ambiguous_transport_result_never_marks_or_releases_exit_intent(
    monkeypatch, tmp_path, transport_result, claim_retained,
):
    """모순/비bool 전송 결과는 성공도 확정 미전송도 아니므로 reconciliation까지 보류."""
    from core import toss_exit_execution_intent as intent

    monkeypatch.setenv("TOSS_EXIT_INTENT_STATE_PATH", str(tmp_path / "intents.json"))
    env = {
        "TOSS_LIVE_PILOT_ENABLED": "true",
        "TOSS_LIVE_ORDER_ALLOWED": "true",
        "TOSS_LIVE_ADAPTER_ENABLED": "true",
        "TOSS_AUTONOMOUS_MODE": "true",
    }
    decision_ref = intent.build_exit_decision_ref(
        "LRCX", "full_exit", datetime.now(KST),
    )
    record = {
        "pilot_id": "pilot-a",
        "decision_ref": decision_ref,
        "symbol": "LRCX",
        "side": "sell",
        "quantity": 1,
        "limit_price": 302.0,
        "estimated_amount_krw": 420_000,
        "status": "previewed",
        "blocks": [],
        "live_order_sent": False,
    }
    def fake_dispatch(*args, **kwargs):
        if isinstance(transport_result, BaseException):
            raise transport_result
        return transport_result

    with patch.dict(os.environ, env, clear=False), \
         patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy",
               return_value={
                   "autonomous_mode": True,
                   "autonomous_kill_switch": False,
                   "live_order_allowed": True,
                   "adapter_status": "enabled",
               }), \
         patch("core.toss_live_pilot_ledger.list_live_pilot_records", return_value=[record]), \
         patch("core.toss_live_pilot_verification.is_verification_passed",
               return_value=(True, [], {"verification_id": "hv", "status": "PASS"})), \
         patch("core.toss_live_pilot_adapter.can_send_live_pilot_order", return_value=(True, [])), \
         patch("core.toss_live_pilot_telegram.resolve_live_transport_for_confirm", return_value=object()), \
         patch("core.toss_live_pilot_adapter.dispatch_toss_order_live",
               side_effect=fake_dispatch) as dispatch, \
         patch("core.toss_live_pilot_ledger.record_live_sent") as record_sent, \
         patch("core.toss_live_pilot_ledger.record_live_send_failed") as record_failed, \
         patch("core.toss_live_pilot_ledger.record_live_send_retryable") as record_retryable, \
         patch("core.toss_live_pilot_telegram.send_autonomous_result_message", return_value=True), \
         patch("core.toss_autonomous_finalizer._record_event"):
        from core.toss_autonomous_finalizer import try_autonomous_finalize
        result = try_autonomous_finalize("pilot-a")
        retried = (
            try_autonomous_finalize("pilot-a", allow_retry=True)
            if claim_retained else None
        )

    assert result["live_order_sent"] is False
    record_sent.assert_not_called()
    if claim_retained:
        assert retried is not None
        assert retried["live_order_sent"] is False
        assert retried["reason"] == "exit_intent_reserved"
        dispatch.assert_called_once()
        record_failed.assert_not_called()
        record_retryable.assert_called_once()
        assert record_retryable.call_args.kwargs["failure_reason"].startswith(
            "reconcile_required:"
        )
    second = intent.claim_exit_intent(decision_ref, "pilot-b")
    assert second["ok"] is (not claim_retained)
    assert second["reason"] == (
        "exit_intent_reserved" if claim_retained else "exit_intent_claimed"
    )
