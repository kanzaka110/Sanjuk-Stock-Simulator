from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import sys

import pandas as pd

from core.trade_outcome_attribution import (
    calculate_trade_outcome_attribution,
    hermes_interpretation_payload,
    normalize_execution_records,
)

KST = timezone(timedelta(hours=9))


def _prediction(**overrides):
    row = {
        "id": 1,
        "created_at": "2026-07-01T09:00:00+09:00",
        "closed_at": "2026-07-08T09:00:00+09:00",
        "ticker": "005930.KS",
        "name": "삼성전자",
        "signal": "매수",
        "entry_price": 100.0,
        "closed_price": 110.0,
        "pnl_pct": 10.0,
        "outcome": "win",
        "status": "closed",
        "action_type": "AI_NEW_BUY",
        "persona": "가치",
        "strategy_type": "중기보유",
        "strategy_tags": "RSI과매도,펀더멘털성장",
        "agreement_count": 0,
        "confidence": 70,
        "briefing_type": "KR_OPEN",
        "account_type": "ISA",
        "benchmark_ticker": "^KS11",
    }
    row.update(overrides)
    return row


def _manual(**overrides):
    row = {
        "id": 7,
        "created_at": "2026-07-01T10:00:00+09:00",
        "ticker": "005930.KS",
        "side": "매수",
        "shares": 2,
        "price": 101.0,
        "account": "ISA",
    }
    row.update(overrides)
    return row


def test_manual_trade_normalizes_as_real_execution():
    rows = normalize_execution_records(manual_trades=[_manual()])
    assert rows == [{
        "execution_id": "manual:7",
        "decision_ref": "",
        "symbol": "005930.KS",
        "side": "buy",
        "state": "filled",
        "filled_price": 101.0,
        "filled_quantity": 2.0,
        "executed_at": "2026-07-01T10:00:00+09:00",
        "account": "ISA",
        "fees": 0.0,
        "taxes": 0.0,
        "cost_basis_price": 0.0,
        "source": "manual_trade_log",
        "is_real_execution": True,
    }]


def test_live_artifact_and_unconfirmed_rows_are_excluded():
    events = [
        {
            "event_id": "artifact", "event_type": "live_sent_artifact",
            "live_order_sent": True, "adapter_status": "enabled", "live_order_allowed": False,
            "symbol": "MU", "side": "buy", "filled_price": 100, "filled_quantity": 1,
            "created_at": "2026-07-01T10:00:00+09:00",
        },
        {
            "event_id": "fake", "event_type": "live_sent",
            "live_order_sent": True, "adapter_status": "disabled", "live_order_allowed": True,
            "symbol": "MU", "side": "buy", "filled_price": 100, "filled_quantity": 1,
            "created_at": "2026-07-01T10:00:00+09:00",
        },
    ]
    assert normalize_execution_records(live_events=events) == []


def test_canonical_live_fill_is_included():
    event = {
        "event_id": "tle_1", "event_type": "live_sent",
        "live_order_sent": True, "adapter_status": "enabled", "live_order_allowed": True,
        "symbol": "MU", "side": "BUY", "filled_price": 501, "filled_quantity": 2,
        "created_at": "2026-07-01T10:00:00+09:00",
    }
    rows = normalize_execution_records(live_events=[event])
    assert rows[0]["execution_id"] == "live:tle_1"
    assert rows[0]["source"] == "toss_live_event"


def test_filled_and_partial_broker_get_rows_are_included():
    base = {
        "symbol": "MU", "side": "BUY", "filled_price": 501, "filled_quantity": 2,
        "filled_at": "2026-07-01T10:00:00+09:00",
        "broker_order_id_masked": "***123",
    }
    rows = normalize_execution_records(broker_orders=[
        {**base, "broker_order_status": "FILLED"},
        {**base, "broker_order_status": "OPEN", "broker_order_id_masked": "***456"},
    ])
    assert len(rows) == 2
    assert {row["state"] for row in rows} == {"filled", "partial"}
    assert all(row["source"] == "toss_broker_orders_get" for row in rows)


def test_autonomous_live_sent_with_all_production_invariants_is_fill_truth():
    event = {
        "event_id": "auto_1", "event_type": "autonomous_live_sent",
        "decision_ref": "execution_decision:tlive_1",
        "verification_id": "hv_1", "hermes_decision_verified": True,
        "live_order_sent": True, "adapter_status": "enabled", "live_order_allowed": True,
        "symbol": "MU", "side": "buy", "filled_price": 500, "filled_quantity": 1,
        "created_at": "2026-07-01T10:00:00+09:00",
    }
    rows = normalize_execution_records(live_events=[event])
    assert len(rows) == 1
    assert rows[0]["decision_ref"] == "execution_decision:tlive_1"
    assert rows[0]["verification_id"] == "hv_1"
    assert rows[0]["hermes_decision_verified"] is True


def test_autonomous_live_sent_missing_any_production_invariant_is_excluded():
    base = {
        "event_id": "auto_1", "event_type": "autonomous_live_sent",
        "live_order_sent": True, "adapter_status": "enabled", "live_order_allowed": True,
        "symbol": "MU", "side": "buy", "filled_price": 500, "filled_quantity": 1,
        "created_at": "2026-07-01T10:00:00+09:00",
    }
    for field, invalid in (
        ("live_order_sent", False),
        ("adapter_status", "disabled"),
        ("live_order_allowed", False),
    ):
        assert normalize_execution_records(live_events=[{**base, field: invalid}]) == []


def test_hermes_traceability_is_separate_from_recommendation_linkage():
    execution = normalize_execution_records(live_events=[{
        "event_id": "auto_2", "event_type": "autonomous_live_sent",
        "decision_ref": "execution_decision:tlive_2",
        "verification_id": "hv_2", "hermes_decision_verified": True,
        "live_order_sent": True, "adapter_status": "enabled", "live_order_allowed": True,
        "symbol": "MU", "side": "buy", "filled_price": 500, "filled_quantity": 1,
        "created_at": "2026-07-01T10:00:00+09:00",
    }])
    report = calculate_trade_outcome_attribution([_prediction()], executions=execution)
    assert report["summary"]["observed_real_executions"] == 1
    assert report["summary"]["directly_linked_executions"] == 0
    assert report["summary"]["hermes_verified_executions"] == 1
    assert report["summary"]["hermes_decision_traceability_rate_pct"] == 100.0
    assert report["data_quality"]["direct_prediction_execution_id_available"] is False
    assert report["data_quality"]["direct_hermes_decision_id_available"] is True


def test_hermes_verification_join_requires_both_exact_keys_and_pass():
    from core.dashboard_data import _mark_hermes_verified_live_events

    verification = [{
        "verification_id": "hv_1",
        "decision_ref": "execution_decision:tlive_1",
        "status": "PASS",
        "symbol": "005930.KS",
        "side": "buy",
    }]
    events = [
        {"event_id": "ok", "verification_id": "hv_1", "decision_ref": "execution_decision:tlive_1", "symbol": "005930.KS", "side": "buy"},
        {"event_id": "wrong_ref", "verification_id": "hv_1", "decision_ref": "execution_decision:other", "symbol": "005930.KS", "side": "buy"},
        {"event_id": "wrong_id", "verification_id": "hv_other", "decision_ref": "execution_decision:tlive_1", "symbol": "005930.KS", "side": "buy"},
        {"event_id": "wrong_symbol", "verification_id": "hv_1", "decision_ref": "execution_decision:tlive_1", "symbol": "MU", "side": "buy"},
        {"event_id": "wrong_side", "verification_id": "hv_1", "decision_ref": "execution_decision:tlive_1", "symbol": "005930.KS", "side": "sell"},
        {"event_id": "missing", "verification_id": "", "decision_ref": "", "symbol": "", "side": ""},
    ]
    marked = _mark_hermes_verified_live_events(events, verification)
    assert [row["hermes_decision_verified"] for row in marked] == [
        True, False, False, False, False, False,
    ]

    hold = [{**verification[0], "status": "HOLD"}]
    assert _mark_hermes_verified_live_events([{
        "verification_id": "hv_1",
        "decision_ref": "execution_decision:tlive_1",
        "symbol": "005930.KS",
        "side": "buy",
    }], hold)[0]["hermes_decision_verified"] is False


def test_broker_client_order_id_join_requires_exact_pilot_symbol_side():
    from core.dashboard_data import _attach_direct_refs_to_broker_orders

    event = {
        "pilot_id": "tlive_20260712_025100_1234",
        "decision_ref": "prediction:42",
        "verification_id": "hv_42",
        "hermes_decision_verified": True,
        "symbol": "005930.KS",
        "side": "buy",
    }
    orders = [
        {"client_order_id": event["pilot_id"], "symbol": "005930", "side": "BUY"},
        {"client_order_id": "tlive_wrong", "symbol": "005930", "side": "BUY"},
        {"client_order_id": event["pilot_id"], "symbol": "MU", "side": "BUY"},
        {"client_order_id": event["pilot_id"], "symbol": "005930", "side": "SELL"},
        {"client_order_id": "", "symbol": "005930", "side": "BUY"},
    ]
    marked = _attach_direct_refs_to_broker_orders(orders, [event])
    assert marked[0]["decision_ref"] == "prediction:42"
    assert marked[0]["hermes_decision_verified"] is True
    assert [row["decision_ref"] for row in marked[1:]] == ["", "", "", ""]
    assert all(row["hermes_decision_verified"] is False for row in marked[1:])


def test_broker_get_truth_wins_over_live_event_for_same_decision_ref():
    live = {
        "event_id": "live_1", "event_type": "live_sent",
        "source_prediction_id": 1,
        "live_order_sent": True, "adapter_status": "enabled", "live_order_allowed": True,
        "symbol": "005930.KS", "side": "buy", "filled_price": 102,
        "filled_quantity": 1, "broker_order_status": "FILLED",
        "created_at": "2026-07-01T10:00:00+09:00",
    }
    broker = {
        "broker_order_id_masked": "***123", "source_prediction_id": 1,
        "broker_order_status": "FILLED", "symbol": "005930.KS", "side": "buy",
        "filled_price": 103, "filled_quantity": 1,
        "filled_at": "2026-07-01T10:01:00+09:00",
    }
    executions = normalize_execution_records(live_events=[live], broker_orders=[broker])
    assert len(executions) == 1
    assert executions[0]["source"] == "toss_broker_orders_get"
    report = calculate_trade_outcome_attribution([_prediction()], executions=executions)
    assert report["summary"]["observed_real_executions"] == 1
    row = report["rows"][0]
    assert row["execution_source"] == "toss_broker_orders_get"
    assert row["filled_price"] == 103


def test_buy_separates_market_direction_and_stock_selection():
    report = calculate_trade_outcome_attribution(
        [_prediction()], benchmark_returns_by_prediction_id={1: 4.0})
    row = report["rows"][0]
    assert row["market_direction"] == "correct"
    assert row["stock_selection"] == "outperformed"
    assert row["selection_alpha_pct"] == 6.0
    assert report["benchmark_attribution"]["avg_selection_alpha_pct"] == 6.0


def test_strategy_type_and_each_tag_are_attributed_separately():
    report = calculate_trade_outcome_attribution([_prediction()])
    assert report["by_strategy_type"][0]["key"] == "중기보유"
    tags = {row["key"]: row for row in report["by_strategy_tag"]}
    assert set(tags) == {"RSI과매도", "펀더멘털성장"}
    assert tags["RSI과매도"]["evaluated"] == 1
    payload = hermes_interpretation_payload(report)
    assert payload["top_strategy_tags"]


def test_sell_direction_inverts_benchmark_return():
    report = calculate_trade_outcome_attribution([
        _prediction(signal="매도", action_type="AI_SELL_MANAGEMENT",
                    entry_price=100, closed_price=90, pnl_pct=10, outcome="win")
    ], benchmark_returns_by_prediction_id={1: -4.0})
    row = report["rows"][0]
    assert row["direction_adjusted_benchmark_pct"] == 4.0
    assert row["market_direction"] == "correct"
    assert row["selection_alpha_pct"] == 6.0


def test_invalid_data_error_and_expired_are_not_in_win_loss_denominator():
    predictions = [
        _prediction(id=1, outcome="invalid"),
        _prediction(id=2, outcome="data_error"),
        _prediction(id=3, outcome="expired"),
        _prediction(id=4, outcome="win"),
    ]
    report = calculate_trade_outcome_attribution(predictions)
    assert report["summary"]["resolved_predictions"] == 1
    assert report["summary"]["evaluated_predictions"] == 1
    assert report["summary"]["wins"] == 1
    assert report["summary"]["win_rate_pct"] == 100.0
    assert report["data_quality"]["evaluated_rate_pct"] == 25.0


def test_neutral_is_resolved_but_not_in_decisive_evaluation_denominator():
    report = calculate_trade_outcome_attribution([
        _prediction(id=1, outcome="win"),
        _prediction(id=2, outcome="loss"),
        _prediction(id=3, outcome="neutral"),
    ])
    assert report["summary"]["resolved_predictions"] == 3
    assert report["summary"]["evaluated_predictions"] == 2
    assert report["summary"]["neutral"] == 1
    assert report["summary"]["win_rate_pct"] == 50.0
    ticker = report["by_ticker"][0]
    assert ticker["resolved"] == 3
    assert ticker["evaluated"] == 2
    assert ticker["neutral"] == 1


def test_non_actionable_watch_is_excluded():
    report = calculate_trade_outcome_attribution([
        _prediction(signal="관망", action_type="WATCH_ONLY", outcome="neutral")])
    assert report["rows"][0]["quality_status"] == "excluded_non_actionable"
    assert report["summary"]["evaluated_predictions"] == 0


def test_unlinked_execution_is_independent_cohort_not_loss():
    execution = normalize_execution_records(manual_trades=[_manual()])
    report = calculate_trade_outcome_attribution([_prediction()], executions=execution)
    row = report["rows"][0]
    assert row["execution_status"] == "not_linked"
    assert row["linkage_status"] == "unavailable"
    assert "독립 집계" in row["execution_note"]
    assert report["summary"]["losses"] == 0
    assert report["summary"]["observed_real_executions"] == 1
    assert report["summary"]["directly_linked_executions"] == 0
    assert report["execution_cohort"]["unlinked_executions"] == 1


def test_same_ticker_side_and_time_never_links_without_direct_ref():
    predictions = [
        _prediction(id=1, created_at="2026-07-01T08:00:00+09:00"),
        _prediction(id=2, created_at="2026-07-01T09:00:00+09:00"),
    ]
    execution = normalize_execution_records(manual_trades=[_manual()])
    report = calculate_trade_outcome_attribution(predictions, executions=execution)
    assert all(row["execution_status"] == "not_linked" for row in report["rows"])


def test_direct_prediction_ref_links_exactly_one_prediction():
    predictions = [_prediction(id=1), _prediction(id=2)]
    execution = normalize_execution_records(manual_trades=[
        _manual(source_prediction_id=2),
    ])
    by_id = {
        row["prediction_id"]: row
        for row in calculate_trade_outcome_attribution(predictions, executions=execution)["rows"]
    }
    assert by_id[2]["execution_status"] == "linked"
    assert by_id[2]["linkage_status"] == "direct"
    assert by_id[1]["execution_status"] == "not_linked"


def test_direct_ref_with_ticker_mismatch_is_not_linked():
    execution = normalize_execution_records(manual_trades=[
        _manual(source_prediction_id=1, ticker="MU"),
    ])
    row = calculate_trade_outcome_attribution([_prediction()], executions=execution)["rows"][0]
    assert row["execution_status"] == "not_linked"


def test_buy_slippage_and_directional_return_require_direct_ref():
    execution = normalize_execution_records(manual_trades=[
        _manual(price=102.0, source_prediction_id=1),
    ])
    row = calculate_trade_outcome_attribution([_prediction()], executions=execution)["rows"][0]
    assert row["slippage_pct"] == 2.0
    assert row["actual_execution_directional_return_pct"] == 7.84
    assert row["execution_effect_pct"] == -2.16
    assert row["actual_execution_net_return_pct"] is None


def test_sell_uses_avoided_move_and_requires_cost_basis_for_realized_pnl():
    prediction = _prediction(signal="매도", action_type="AI_SELL_MANAGEMENT",
                             entry_price=100, closed_price=90, pnl_pct=10)
    execution = normalize_execution_records(manual_trades=[
        _manual(side="매도", price=98.0, source_prediction_id=1),
    ])
    row = calculate_trade_outcome_attribution([prediction], executions=execution)["rows"][0]
    assert row["slippage_pct"] == 2.0
    assert row["avoided_move_return_pct"] == 8.16
    assert row["realized_pnl_pct"] is None


def test_sell_realized_pnl_uses_explicit_cost_basis_only():
    prediction = _prediction(signal="매도", action_type="AI_SELL_MANAGEMENT",
                             entry_price=100, closed_price=90, pnl_pct=10)
    execution = normalize_execution_records(manual_trades=[
        _manual(side="매도", price=98.0, source_prediction_id=1, cost_basis_price=80),
    ])
    row = calculate_trade_outcome_attribution([prediction], executions=execution)["rows"][0]
    assert row["realized_pnl_pct"] == 22.5


def test_execution_net_return_requires_explicit_costs():
    execution = normalize_execution_records(manual_trades=[
        _manual(price=100, shares=10, source_prediction_id=1, fees=10),
    ])
    row = calculate_trade_outcome_attribution([_prediction()], executions=execution)["rows"][0]
    assert row["actual_execution_directional_return_pct"] == 10.0
    assert row["actual_execution_net_return_pct"] == 9.0


def test_conditional_recommendation_requires_activation_evidence():
    conditional = _prediction(action_type="CONDITIONAL_NEW_BUY")
    report = calculate_trade_outcome_attribution([conditional])
    assert report["rows"][0]["quality_status"] == "excluded_not_activated"
    linked = normalize_execution_records(manual_trades=[
        _manual(source_prediction_id=1),
    ])
    activated = calculate_trade_outcome_attribution([conditional], executions=linked)
    assert activated["rows"][0]["quality_status"] == "evaluated"


def test_blocked_action_grade_is_non_actionable():
    report = calculate_trade_outcome_attribution([
        _prediction(action_grade="BLOCKED"),
    ])
    assert report["rows"][0]["quality_status"] == "excluded_non_actionable"


def test_execution_input_order_does_not_change_broker_preference():
    live = normalize_execution_records(live_events=[{
        "event_id": "live_1", "event_type": "live_sent", "source_prediction_id": 1,
        "live_order_sent": True, "adapter_status": "enabled", "live_order_allowed": True,
        "symbol": "005930.KS", "side": "buy", "filled_price": 102,
        "filled_quantity": 1, "broker_order_status": "FILLED",
        "created_at": "2026-07-01T10:00:00+09:00",
    }])[0]
    broker = normalize_execution_records(broker_orders=[{
        "broker_order_id_masked": "***123", "source_prediction_id": 1,
        "broker_order_status": "FILLED", "symbol": "005930.KS", "side": "buy",
        "filled_price": 103, "filled_quantity": 1,
        "filled_at": "2026-07-01T10:01:00+09:00",
    }])[0]
    first = calculate_trade_outcome_attribution([_prediction()], executions=[live, broker])
    second = calculate_trade_outcome_attribution([_prediction()], executions=[broker, live])
    assert first == second


def test_inputs_are_not_mutated():
    predictions = [_prediction()]
    executions = normalize_execution_records(manual_trades=[_manual()])
    before_predictions = deepcopy(predictions)
    before_executions = deepcopy(executions)
    calculate_trade_outcome_attribution(
        predictions, executions=executions, benchmark_returns_by_prediction_id={1: 3.0})
    assert predictions == before_predictions
    assert executions == before_executions


def test_report_contract_has_no_order_authority():
    report = calculate_trade_outcome_attribution([_prediction()])
    assert report["read_only"] is True
    assert report["order_side_effects"] is False
    assert "order" not in report
    assert report["matching_rule"]["method"] == "direct_decision_ref_only"
    assert report["matching_rule"]["unmatched_meaning"] == "independent_execution_cohort_not_loss"


def test_hermes_payload_is_fact_only_and_warns_about_direct_linkage():
    report = calculate_trade_outcome_attribution([_prediction()])
    payload = hermes_interpretation_payload(report)
    assert payload["read_only"] is True
    assert any("decision_ref" in rule for rule in payload["interpretation_rules"])
    assert any("자동매도" in rule for rule in payload["interpretation_rules"])


def test_empty_report_is_valid():
    report = calculate_trade_outcome_attribution([])
    assert report["summary"]["total_predictions"] == 0
    assert report["summary"]["execution_linkage_rate_pct"] == 0.0
    assert report["execution_cohort"]["observed_real_executions"] == 0
    assert report["benchmark_attribution"]["status"] == "not_requested"


def test_dashboard_contract_is_cached_and_read_only(monkeypatch):
    from core import dashboard_data as dd

    monkeypatch.setattr(
        dd,
        "_read_trade_outcome_inputs",
        lambda days: ([_prediction()], [_manual()], [], []),
    )
    monkeypatch.setattr(dd, "_cache", {}, raising=False)
    first = dd.trade_outcome_attribution_data(90)
    second = dd.trade_outcome_attribution_data(90)
    assert first == second
    assert first["read_only"] is True
    assert first["order_side_effects"] is False
    assert first["benchmark_attribution"]["status"] == "not_requested"
    assert first["source"] == "memory_db_and_local_execution_logs_read_only"
    assert first["window"]["mode"] == "rolling_days"
    assert first["window"]["days"] == 90
    assert first["window"]["filter_applied"] is True
    assert first["interpretation_payload"]["read_only"] is True
    assert first["interpretation_payload"]["window"]["days"] == 90


def test_dashboard_route_is_get_only():
    from web import app as webapp

    routes = [
        route for route in webapp.app.routes
        if getattr(route, "path", "") == "/api/trade-outcome-attribution"
    ]
    assert len(routes) == 1
    assert set(getattr(routes[0], "methods", set())) <= {"GET", "HEAD"}


def test_cli_build_report_injects_benchmark_and_execution(monkeypatch):
    from tools import trade_outcome_attribution_cli as cli

    monkeypatch.setattr(
        cli,
        "fetch_benchmark_returns",
        lambda rows: ({1: 4.0}, {
            "status": "ok", "available_predictions": 1,
            "requested_predictions": 1, "requested_benchmarks": ["^KS11"],
            "missing_prediction_ids": [], "source": "test",
        }),
    )
    report = cli.build_report({
        "predictions": [_prediction()],
        "manual_trades": [_manual(source_prediction_id=1)],
        "scope": "test_snapshot",
    }, with_benchmark=True)
    assert report["benchmark_attribution"]["status"] == "available"
    assert report["benchmark_attribution"]["source_metadata"]["status"] == "ok"
    assert report["summary"]["observed_real_executions"] == 1
    assert report["summary"]["directly_linked_executions"] == 1
    assert report["scope"] == "test_snapshot"


def test_cli_benchmark_batch_uses_prediction_window(monkeypatch):
    from tools import trade_outcome_attribution_cli as cli

    dates = pd.date_range("2026-07-01", periods=8, freq="D")
    columns = pd.MultiIndex.from_tuples([("Close", "^KS11")])
    data = pd.DataFrame([[100 + index] for index in range(8)], index=dates, columns=columns)
    monkeypatch.setitem(
        sys.modules,
        "yfinance",
        SimpleNamespace(download=lambda *args, **kwargs: data),
    )
    prediction = _prediction(
        created_at="2026-07-01T09:00:00+09:00",
        closed_at="2026-07-08T09:00:00+09:00",
    )
    returns, meta = cli.fetch_benchmark_returns([prediction])
    assert meta["status"] == "ok"
    assert meta["available_predictions"] == 1
    assert returns[1] == 7.0


def test_cli_recent_scope_enforces_created_at_cohort_window():
    from tools import trade_outcome_attribution_cli as cli

    as_of = datetime(2026, 7, 11, 12, 0, tzinfo=KST)
    old = _prediction(
        id=10,
        created_at="2026-03-01T09:00:00+09:00",
        closed_at="2026-03-10T09:00:00+09:00",
    )
    closed_recently = _prediction(
        id=11,
        created_at="2026-03-01T09:00:00+09:00",
        closed_at="2026-07-01T09:00:00+09:00",
    )
    recent = _prediction(
        id=12,
        created_at="2026-07-02T09:00:00+09:00",
        closed_at="2026-07-08T09:00:00+09:00",
    )
    report = cli.build_report({
        "scope": "recent_90_days",
        "predictions": [old, closed_recently, recent],
        "manual_trades": [
            _manual(id=1, created_at="2026-03-01T10:00:00+09:00"),
            _manual(id=2, created_at="2026-07-01T10:00:00+09:00"),
        ],
        "live_events": [
            {
                "event_id": "old", "event_type": "live_sent",
                "live_order_sent": True, "adapter_status": "enabled",
                "live_order_allowed": True, "symbol": "MU", "side": "buy",
                "filled_price": 100, "filled_quantity": 1,
                "created_at": "2026-03-01T10:00:00+09:00",
            },
            {
                "event_id": "recent", "event_type": "live_sent",
                "live_order_sent": True, "adapter_status": "enabled",
                "live_order_allowed": True, "symbol": "MU", "side": "buy",
                "filled_price": 100, "filled_quantity": 1,
                "created_at": "2026-07-01T10:00:00+09:00",
            },
        ],
    }, as_of=as_of)

    assert report["scope"] == "recent_90_days"
    assert report["window"]["mode"] == "rolling_days"
    assert report["window"]["days"] == 90
    assert report["window"]["cutoff"].startswith("2026-04-12T12:00:00")
    assert report["window"]["input_counts"]["predictions"] == 3
    assert report["window"]["output_counts"]["predictions"] == 1
    assert report["summary"]["total_predictions"] == 1
    assert report["summary"]["observed_real_executions"] == 2
    assert report["interpretation_payload"]["window"]["days"] == 90


def test_cli_window_is_optional_and_explicit_days_override_scope():
    from tools import trade_outcome_attribution_cli as cli

    as_of = datetime(2026, 7, 11, 12, 0, tzinfo=KST)
    payload = {
        "scope": "test_snapshot",
        "predictions": [
            _prediction(id=10, created_at="2026-07-01T09:00:00+09:00"),
            _prediction(
                id=11,
                created_at="2026-05-01T09:00:00+09:00",
                closed_at="2026-05-08T09:00:00+09:00",
            ),
        ],
    }
    unfiltered = cli.build_report(payload, as_of=as_of)
    filtered = cli.build_report(payload, days=30, as_of=as_of)
    assert unfiltered["summary"]["total_predictions"] == 2
    assert unfiltered["window"]["mode"] == "provided_snapshot"
    assert filtered["scope"] == "recent_30_days"
    assert filtered["summary"]["total_predictions"] == 1
    assert filtered["window"]["days"] == 30


def test_cli_window_rejects_invalid_days():
    from tools import trade_outcome_attribution_cli as cli

    for invalid in (0, -1, 3651, "bad"):
        try:
            cli.build_report({"predictions": []}, days=invalid)  # type: ignore[arg-type]
        except ValueError as exc:
            assert str(exc) in {"window_days_must_be_integer", "window_days_out_of_range"}
        else:
            raise AssertionError(f"invalid days accepted: {invalid!r}")


def test_cli_summary_repeats_no_order_contract():
    from tools import trade_outcome_attribution_cli as cli

    report = cli.build_report({"predictions": [_prediction()]})
    text = cli.format_summary(report)
    assert "매매 결과 귀속" in text
    assert "자동매도/주문 권한 없음" in text
