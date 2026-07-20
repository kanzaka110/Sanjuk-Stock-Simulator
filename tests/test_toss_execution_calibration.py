import sqlite3
from pathlib import Path

import pytest

from src import toss_execution_calibration as calibration
from src.toss_execution_calibration import reconstruct_execution_calibration


class _StrSubclass(str):
    pass


def _canonical_pilot_id(value):
    return value if value.startswith("tlive_") else f"tlive_{value}"


def _fill(
    pilot_id,
    side,
    symbol,
    quantity,
    price,
    at,
    *,
    reason,
    estimated_amount_krw,
    status="FILLED",
    order_quantity=None,
    event_type="autonomous_live_sent",
    live_order_sent=1,
    adapter_status="enabled",
    live_order_allowed=1,
    fx_usdkrw=None,
    fx_source=None,
    canonical_pilot=True,
):
    return {
        "pilot_id": _canonical_pilot_id(pilot_id) if canonical_pilot else pilot_id,
        "side": side,
        "symbol": symbol,
        "quantity": quantity if order_quantity is None else order_quantity,
        "filled_quantity": quantity,
        "filled_price": price,
        "created_at": at,
        "broker_order_status": status,
        "strategy_reason": reason,
        "estimated_amount_krw": estimated_amount_krw,
        "event_type": event_type,
        "live_order_sent": live_order_sent,
        "adapter_status": adapter_status,
        "live_order_allowed": live_order_allowed,
        "fx_usdkrw": fx_usdkrw,
        "fx_source": fx_source,
    }


def test_reconstruct_execution_calibration_closes_full_fifo_lifecycle():
    rows = [
        _fill(
            "buy-1", "buy", "005930.KS", 4, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=400_000,
        ),
        _fill(
            "sell-1", "sell", "005930.KS", 2, 102_000,
            "2026-07-15T10:00:00+09:00",
            reason="position_review_sell", estimated_amount_krw=204_000,
        ),
        _fill(
            "sell-2", "sell", "005930.KS", 2, 97_000,
            "2026-07-16T10:00:00+09:00",
            reason="auto_exit_sell", estimated_amount_krw=194_000,
        ),
    ]

    result = reconstruct_execution_calibration(rows, min_samples=2)

    assert result["schema"] == "toss_execution_calibration.v1"
    assert result["mode"] == "observability_only"
    assert result["decision_usable"] is False
    assert result["attribution_model"] == "symbol_fifo_v1"
    assert result["attribution_verified"] is False
    assert result["cost_model"] == "decision_buffer_v1_not_broker_statement"
    assert result["completed_count"] == 1
    assert result["open_lot_count"] == 0
    assert result["sample_sufficient"] is False
    outcome = result["outcomes"][0]
    assert outcome["buy_pilot_id"] == "tlive_buy-1"
    assert outcome["entry_quantity"] == 4
    assert outcome["exit_count"] == 2
    assert outcome["gross_return_pct"] == -0.5
    assert outcome["cost_buffer_krw"] == 1_000.0
    assert outcome["net_return_pct"] == -0.75
    assert outcome["outcome"] == "loss"


def test_reconstruct_execution_calibration_deduplicates_pilot_fills():
    buy = _fill(
        "buy-1", "buy", "005930.KS", 1, 100_000,
        "2026-07-15T09:00:00+09:00",
        reason="auto_pipeline", estimated_amount_krw=100_000,
    )
    rows = [
        buy,
        dict(buy),
        _fill(
            "sell-1", "sell", "005930.KS", 1, 105_000,
            "2026-07-15T10:00:00+09:00",
            reason="position_review_sell", estimated_amount_krw=105_000,
        ),
    ]

    result = reconstruct_execution_calibration(rows)

    assert result["completed_count"] == 1
    assert result["open_lot_count"] == 0
    assert result["ignored_count"] == 1


def test_reconstruct_execution_calibration_quarantines_conflicting_pilot_rows():
    buy = _fill(
        "buy-1", "buy", "005930.KS", 1, 100_000,
        "2026-07-15T09:00:00+09:00",
        reason="auto_pipeline", estimated_amount_krw=100_000,
    )
    conflicting_buy = dict(buy, filled_price=110_000, estimated_amount_krw=110_000)
    later_conflicting_buy = dict(
        buy,
        filled_price=120_000,
        estimated_amount_krw=120_000,
    )
    rows = [
        buy,
        conflicting_buy,
        later_conflicting_buy,
        _fill(
            "sell-1", "sell", "005930.KS", 1, 105_000,
            "2026-07-15T10:00:00+09:00",
            reason="position_review_sell", estimated_amount_krw=105_000,
        ),
    ]

    result = reconstruct_execution_calibration(rows)

    assert result["completed_count"] == 0
    assert result["open_lot_count"] == 0
    assert result["conflict_count"] == 1
    assert result["quarantined_fill_count"] == 3
    assert result["ignored_count"] == 4
    assert result["lineage_status"] == "incomplete"
    assert "pilot_payload_conflict" in result["lineage_reasons"]


def test_reconstruct_execution_calibration_rejects_fractional_share_fills():
    rows = [
        _fill(
            "buy-1", "buy", "005930.KS", 1.5, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=150_000,
        ),
        _fill(
            "sell-1", "sell", "005930.KS", 1.5, 105_000,
            "2026-07-15T10:00:00+09:00",
            reason="position_review_sell", estimated_amount_krw=157_500,
        ),
    ]

    result = reconstruct_execution_calibration(rows)

    assert result["completed_count"] == 0
    assert result["open_lot_count"] == 0
    assert result["ignored_count"] == 2


def test_reconstruct_execution_calibration_keeps_partial_lifecycle_open():
    rows = [
        _fill(
            "buy-1", "buy", "005930.KS", 4, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=400_000,
        ),
        _fill(
            "sell-1", "sell", "005930.KS", 2, 102_000,
            "2026-07-15T10:00:00+09:00",
            reason="position_review_sell", estimated_amount_krw=204_000,
        ),
    ]

    result = reconstruct_execution_calibration(rows)

    assert result["completed_count"] == 0
    assert result["open_lot_count"] == 1
    assert result["open_quantity"] == 2
    assert result["outcomes"] == []


def test_reconstruct_execution_calibration_is_fifo_per_symbol():
    rows = [
        _fill(
            "buy-1", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=100_000,
        ),
        _fill(
            "buy-2", "buy", "005930.KS", 1, 110_000,
            "2026-07-15T09:05:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=110_000,
        ),
        _fill(
            "sell-1", "sell", "005930.KS", 1, 105_000,
            "2026-07-15T10:00:00+09:00",
            reason="position_review_sell", estimated_amount_krw=105_000,
        ),
    ]

    result = reconstruct_execution_calibration(rows)

    assert result["completed_count"] == 1
    assert result["outcomes"][0]["buy_pilot_id"] == "tlive_buy-1"
    assert result["open_lot_count"] == 1
    assert result["open_quantity"] == 1


@pytest.mark.parametrize("sell_price", [101_000.0001, 100_999.9999])
def test_reconstruct_execution_calibration_classifies_rounded_zero_as_flat(
    sell_price,
):
    rows = [
        _fill(
            "buy-flat", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=100_000,
        ),
        _fill(
            "sell-flat", "sell", "005930.KS", 1, sell_price,
            "2026-07-15T10:00:00+09:00",
            reason="position_review_sell", estimated_amount_krw=sell_price,
        ),
    ]

    result = reconstruct_execution_calibration(rows)

    assert result["completed_count"] == 1
    assert result["wins"] == 0
    assert result["losses"] == 0
    assert result["flats"] == 1
    assert result["avg_win_pct"] is None
    assert result["avg_loss_pct"] is None
    assert result["mean_net_return_pct"] == 0.0
    assert result["outcomes"][0]["outcome"] == "flat"
    assert result["outcomes"][0]["net_return_pct"] == 0.0


@pytest.mark.parametrize(
    ("buy_id", "sell_id"),
    [("z_buy", "a_sell"), ("a_buy", "z_sell")],
)
def test_reconstruct_execution_calibration_quarantines_ambiguous_fill_order(
    buy_id,
    sell_id,
):
    at = "2026-07-15T09:00:00+09:00"
    rows = [
        _fill(
            buy_id, "buy", "005930.KS", 1, 100_000, at,
            reason="auto_pipeline", estimated_amount_krw=100_000,
        ),
        _fill(
            sell_id, "sell", "005930.KS", 1, 101_000, at,
            reason="position_review_sell", estimated_amount_krw=101_000,
        ),
    ]

    result = reconstruct_execution_calibration(rows)

    assert result["completed_count"] == 0
    assert result["open_lot_count"] == 0
    assert result["ambiguous_fill_count"] == 2
    assert result["invalid_fill_count"] == 2
    assert result["quarantined_fill_count"] == 2
    assert result["lineage_status"] == "incomplete"
    assert "fill_order_ambiguous" in result["lineage_reasons"]


def test_reconstruct_execution_calibration_quarantines_conflicting_krx_suffixes():
    rows = [
        _fill(
            "buy-1", "buy", "058470.KS", 2, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=200_000,
        ),
        _fill(
            "sell-1", "sell", "058470.KQ", 2, 101_000,
            "2026-07-15T10:00:00+09:00",
            reason="position_review_sell", estimated_amount_krw=202_000,
        ),
    ]

    result = reconstruct_execution_calibration(rows)

    assert result["completed_count"] == 0
    assert result["open_lot_count"] == 0
    assert result["symbol_alias_conflict_count"] == 1
    assert result["quarantined_fill_count"] == 2
    assert result["lineage_status"] == "incomplete"
    assert "krx_symbol_alias_conflict" in result["lineage_reasons"]


def test_reconstruct_execution_calibration_quarantines_unbounded_numeric_fills():
    huge = 10**300
    rows = [
        _fill(
            "buy-1", "buy", "AAA", huge, huge,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=huge,
        ),
        _fill(
            "sell-1", "sell", "AAA", huge, huge,
            "2026-07-15T10:00:00+09:00",
            reason="position_review_sell", estimated_amount_krw=huge,
        ),
    ]

    result = reconstruct_execution_calibration(rows)

    assert result["completed_count"] == 0
    assert result["outcomes"] == []
    assert result["quarantined_fill_count"] == 2
    assert result["lineage_status"] == "incomplete"
    assert "fill_contract_invalid" in result["lineage_reasons"]


@pytest.mark.parametrize("symbol", ["005930.KS", "AAPL"])
def test_reconstruct_execution_calibration_quarantines_canonical_notional_mismatch(
    symbol,
):
    result = reconstruct_execution_calibration([
        _fill(
            "buy-1", "buy", symbol, 2, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=20_000,
        ),
    ])

    assert result["open_lot_count"] == 0
    assert result["quarantined_fill_count"] == 1
    assert result["lineage_status"] == "incomplete"
    assert "fill_contract_invalid" in result["lineage_reasons"]


def test_reconstruct_execution_calibration_rejects_us_implicit_fx_one_to_one():
    rows = [
        _fill(
            "buy-1", "buy", "AAPL", 1, 100,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=100,
        ),
        _fill(
            "sell-1", "sell", "AAPL", 1, 105,
            "2026-07-15T10:00:00+09:00",
            reason="position_review_sell", estimated_amount_krw=105,
        ),
    ]

    result = reconstruct_execution_calibration(rows)

    assert result["completed_count"] == 0
    assert result["quarantined_fill_count"] == 2
    assert result["lineage_status"] == "incomplete"
    assert "fill_contract_invalid" in result["lineage_reasons"]


def test_reconstruct_execution_calibration_accepts_us_provenance_verified_fx():
    rows = [
        _fill(
            "buy-1", "buy", "AAPL", 1, 100,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=150_000,
            fx_usdkrw=1_500, fx_source="broker_order_preview",
        ),
        _fill(
            "sell-1", "sell", "AAPL", 1, 105,
            "2026-07-15T10:00:00+09:00",
            reason="position_review_sell", estimated_amount_krw=157_500,
            fx_usdkrw=1_500, fx_source="broker_order_preview",
        ),
    ]

    result = reconstruct_execution_calibration(rows, min_samples=1)

    assert result["completed_count"] == 1
    assert result["quarantined_fill_count"] == 0
    assert result["outcomes"][0]["gross_return_pct"] == 5.0


def test_reconstruct_execution_calibration_uses_actual_kr_fill_notional():
    rows = [
        _fill(
            "buy-1", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=100_000,
        ),
        _fill(
            "sell-1", "sell", "005930.KS", 1, 200_000,
            "2026-07-15T10:00:00+09:00",
            reason="position_review_sell", estimated_amount_krw=200_000,
        ),
    ]

    result = reconstruct_execution_calibration(rows, min_samples=1)

    assert result["completed_count"] == 1
    assert result["outcomes"][0]["gross_return_pct"] == 100.0
    assert result["outcomes"][0]["net_return_pct"] == 99.0


def test_reconstruct_execution_calibration_rejects_stale_kr_estimate():
    rows = [
        _fill(
            "buy-1", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=100_000,
        ),
        _fill(
            "sell-1", "sell", "005930.KS", 1, 200_000,
            "2026-07-15T10:00:00+09:00",
            reason="position_review_sell", estimated_amount_krw=100_000,
        ),
    ]

    result = reconstruct_execution_calibration(rows)

    assert result["completed_count"] == 0
    assert result["invalid_fill_count"] == 1
    assert "fill_contract_invalid" in result["lineage_reasons"]


def test_reconstruct_execution_calibration_includes_verified_fx_change_in_krw_return():
    rows = [
        _fill(
            "buy-1", "buy", "AAPL", 1, 100,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=100_000,
            fx_usdkrw=1_000, fx_source="broker_order_preview",
        ),
        _fill(
            "sell-1", "sell", "AAPL", 1, 100,
            "2026-07-15T10:00:00+09:00",
            reason="position_review_sell", estimated_amount_krw=150_000,
            fx_usdkrw=1_500, fx_source="broker_order_preview",
        ),
    ]

    result = reconstruct_execution_calibration(rows, min_samples=1)

    assert result["completed_count"] == 1
    assert result["outcomes"][0]["gross_return_pct"] == 50.0
    assert result["outcomes"][0]["net_return_pct"] == 49.0


@pytest.mark.parametrize(
    ("fx_usdkrw", "accepted"),
    [(500, True), (3_000, True), (499.99, False), (3_000.01, False)],
)
def test_reconstruct_execution_calibration_enforces_verified_fx_bounds(
    fx_usdkrw,
    accepted,
):
    result = reconstruct_execution_calibration([
        _fill(
            "buy-1", "buy", "AAPL", 1, 100,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=100 * fx_usdkrw,
            fx_usdkrw=fx_usdkrw, fx_source="broker_order_preview",
        ),
    ])

    assert (result["open_lot_count"] == 1) is accepted
    assert (result["invalid_fill_count"] == 0) is accepted


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("event_type", "live_sent_artifact"),
        ("live_order_sent", 0),
        ("live_order_sent", True),
        ("adapter_status", "disabled"),
        ("live_order_allowed", 0),
        ("live_order_allowed", True),
        ("order_quantity", 2),
    ],
)
def test_reconstruct_execution_calibration_requires_exact_production_fill_invariant(
    override,
    value,
):
    kwargs = {override: value}
    result = reconstruct_execution_calibration([
        _fill(
            "buy-1", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=100_000,
            **kwargs,
        ),
    ])

    assert result["open_lot_count"] == 0
    assert result["quarantined_fill_count"] == 1
    assert result["lineage_status"] == "incomplete"
    assert "fill_contract_invalid" in result["lineage_reasons"]


@pytest.mark.parametrize(
    "field",
    ["broker_order_status", "event_type", "side", "strategy_reason", "adapter_status"],
)
def test_reconstruct_execution_calibration_quarantines_string_subclass_fields(field):
    row = _fill(
        "buy-1", "buy", "005930.KS", 1, 100_000,
        "2026-07-15T09:00:00+09:00",
        reason="auto_pipeline", estimated_amount_krw=100_000,
    )
    row[field] = _StrSubclass(row[field])

    result = reconstruct_execution_calibration([row])

    assert result["open_lot_count"] == 0
    assert result["invalid_fill_count"] == 1
    assert result["quarantined_fill_count"] == 1
    assert "fill_contract_invalid" in result["lineage_reasons"]


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("filled_quantity", float("nan")),
        ("filled_quantity", float("inf")),
        ("filled_price", float("nan")),
        ("filled_price", float("inf")),
        ("estimated_amount_krw", float("nan")),
        ("estimated_amount_krw", float("inf")),
    ],
)
def test_reconstruct_execution_calibration_quarantines_nonfinite_fill_values(
    field,
    bad_value,
):
    row = _fill(
        "buy-1", "buy", "005930.KS", 1, 100_000,
        "2026-07-15T09:00:00+09:00",
        reason="auto_pipeline", estimated_amount_krw=100_000,
    )
    row[field] = bad_value

    result = reconstruct_execution_calibration([row])

    assert result["invalid_fill_count"] == 1
    assert result["quarantined_fill_count"] == 1
    assert "fill_contract_invalid" in result["lineage_reasons"]


@pytest.mark.parametrize(
    "bad_pilot_id",
    ["", " ", "buy-1", "tlive_", f"tlive_{'x' * 31}", "tlive_bad!"],
)
def test_reconstruct_execution_calibration_quarantines_noncanonical_pilot_id(
    bad_pilot_id,
):
    result = reconstruct_execution_calibration([
        _fill(
            bad_pilot_id, "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=100_000,
            canonical_pilot=False,
        ),
    ])

    assert result["open_lot_count"] == 0
    assert result["invalid_fill_count"] == 1
    assert result["lineage_status"] == "incomplete"


@pytest.mark.parametrize("suffix", ["KS", "KQ"])
def test_reconstruct_execution_calibration_accepts_normal_krx_suffixes(suffix):
    rows = [
        _fill(
            "buy-1", "buy", f"058470.{suffix}", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=100_000,
        ),
        _fill(
            "sell-1", "sell", f"058470.{suffix}", 1, 101_000,
            "2026-07-15T10:00:00+09:00",
            reason="position_review_sell", estimated_amount_krw=101_000,
        ),
    ]

    result = reconstruct_execution_calibration(rows)

    assert result["completed_count"] == 1
    assert result["symbol_alias_conflict_count"] == 0


def test_reconstruct_execution_calibration_allows_bare_to_explicit_krx_fill_alias():
    rows = [
        _fill(
            "buy-1", "buy", "058470", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=100_000,
        ),
        _fill(
            "sell-1", "sell", "058470.KQ", 1, 101_000,
            "2026-07-15T10:00:00+09:00",
            reason="position_review_sell", estimated_amount_krw=101_000,
        ),
    ]

    result = reconstruct_execution_calibration(rows)

    assert result["completed_count"] == 1
    assert result["symbol_alias_conflict_count"] == 0


def test_reconstruct_execution_calibration_quarantines_float_conversion_overflow():
    result = reconstruct_execution_calibration([
        _fill(
            "buy-1", "buy", "005930.KS", 1, 10**400,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=10**400,
        ),
    ])

    assert result["open_lot_count"] == 0
    assert result["quarantined_fill_count"] == 1
    assert "fill_contract_invalid" in result["lineage_reasons"]


def test_reconstruct_execution_calibration_ignores_non_auto_or_unfilled_rows():
    rows = [
        _fill(
            "manual-buy", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="manual", estimated_amount_krw=100_000,
        ),
        _fill(
            "pending-buy", "buy", "000660.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=100_000,
            status="PENDING",
        ),
        _fill(
            "unmatched-sell", "sell", "035420.KS", 1, 100_000,
            "2026-07-15T10:00:00+09:00",
            reason="position_review_sell", estimated_amount_krw=100_000,
        ),
    ]

    result = reconstruct_execution_calibration(rows)

    assert result["completed_count"] == 0
    assert result["open_lot_count"] == 0
    assert result["ignored_count"] == 3


def test_reconstruct_execution_calibration_marks_unmatched_sell_lineage_incomplete():
    rows = [
        _fill(
            "sell-1", "sell", "005930.KS", 3, 100_000,
            "2026-07-15T10:00:00+09:00",
            reason="position_review_sell", estimated_amount_krw=300_000,
        ),
    ]

    result = reconstruct_execution_calibration(rows)

    assert result["unmatched_sell_fill_count"] == 1
    assert result["unmatched_sell_quantity"] == 3
    assert result["status"] == "partial"
    assert result["lineage_status"] == "incomplete"
    assert "unmatched_sell_fill" in result["lineage_reasons"]
    assert result["evidence_sufficient"] is False


def test_reconcile_calibration_marks_open_lots_exceeding_holdings_incomplete():
    reconstructed = reconstruct_execution_calibration([
        _fill(
            "buy-1", "buy", "005930.KS", 4, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=400_000,
        ),
    ])

    result = calibration.reconcile_calibration_with_holdings(
        reconstructed,
        [{"symbol": "005930.KS", "quantity": 2}],
    )

    assert result["holdings_reconciliation_status"] == "incomplete"
    assert result["open_quantity_exceeds_holdings"] == 2
    assert result["lineage_status"] == "incomplete"
    assert "open_lots_exceed_holdings" in result["lineage_reasons"]
    assert result["evidence_sufficient"] is False


def test_reconcile_calibration_rejects_unbounded_holding_quantity():
    reconstructed = reconstruct_execution_calibration([
        _fill(
            "buy-1", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=100_000,
        ),
    ])

    result = calibration.reconcile_calibration_with_holdings(
        reconstructed,
        [{"symbol": "005930.KS", "quantity": 10**400}],
    )

    assert result["holdings_reconciliation_status"] == "unavailable"
    assert result["lineage_status"] == "incomplete"
    assert result["lineage_reasons"] == ["holdings_reconciliation_unavailable"]
    assert result["evidence_sufficient"] is False


@pytest.mark.parametrize("bad_quantity", [0, -1, 1.5, float("nan"), float("inf")])
def test_reconcile_calibration_rejects_non_positive_or_fractional_holding_quantity(
    bad_quantity,
):
    reconstructed = reconstruct_execution_calibration([
        _fill(
            "buy-1", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=100_000,
        ),
    ])

    result = calibration.reconcile_calibration_with_holdings(
        reconstructed,
        [{"symbol": "005930.KS", "quantity": bad_quantity}],
    )

    assert result["holdings_reconciliation_status"] == "unavailable"
    assert "holdings_reconciliation_unavailable" in result["lineage_reasons"]


def test_reconcile_calibration_quarantines_explicit_krx_suffix_mismatch():
    reconstructed = reconstruct_execution_calibration([
        _fill(
            "buy-1", "buy", "058470.KS", 2, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=200_000,
        ),
    ])

    result = calibration.reconcile_calibration_with_holdings(
        reconstructed,
        [{"symbol": "058470.KQ", "quantity": 2}],
    )

    assert result["holdings_reconciliation_status"] == "incomplete"
    assert result["open_quantity_exceeds_holdings"] == 2
    assert "holdings_symbol_alias_conflict" in result["lineage_reasons"]
    assert result["evidence_sufficient"] is False


def test_reconcile_calibration_allows_bare_krx_alias_without_conflicting_suffix():
    reconstructed = reconstruct_execution_calibration([
        _fill(
            "buy-1", "buy", "058470", 2, 100_000,
            "2026-07-15T09:00:00+09:00",
            reason="auto_pipeline", estimated_amount_krw=200_000,
        ),
    ])

    result = calibration.reconcile_calibration_with_holdings(
        reconstructed,
        [{"symbol": "058470.KS", "quantity": 2}],
    )

    assert result["holdings_reconciliation_status"] == "complete"
    assert result["open_quantity_exceeds_holdings"] == 0
    assert "holdings_symbol_alias_conflict" not in result["lineage_reasons"]


def _event_row(
    event_id,
    pilot_id,
    side,
    symbol,
    quantity,
    estimated_amount_krw,
    created_at,
    *,
    broker_order_status="FILLED",
    filled_quantity=None,
    filled_price=100_000,
    event_type="autonomous_live_sent",
    live_order_sent=1,
    adapter_status="enabled",
    live_order_allowed=1,
    canonical_pilot=True,
):
    return (
        event_id,
        _canonical_pilot_id(pilot_id) if canonical_pilot else pilot_id,
        event_type,
        side,
        symbol,
        quantity,
        estimated_amount_krw,
        created_at,
        broker_order_status,
        quantity if filled_quantity is None else filled_quantity,
        filled_price,
        live_order_sent,
        adapter_status,
        live_order_allowed,
    )


def _create_events_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE live_pilot_events (
            event_id TEXT PRIMARY KEY,
            pilot_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            side TEXT,
            symbol TEXT,
            quantity INTEGER,
            estimated_amount_krw REAL,
            created_at TEXT,
            broker_order_status TEXT,
            filled_quantity REAL,
            filled_price REAL,
            live_order_sent INTEGER,
            adapter_status TEXT,
            live_order_allowed INTEGER
        )"""
    )
    conn.executemany(
        "INSERT INTO live_pilot_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def _create_ledger_db(path, rows, *, primary_key=True, normalize_ids=True):
    conn = sqlite3.connect(path)
    pk = " PRIMARY KEY" if primary_key else ""
    conn.execute(
        f"CREATE TABLE live_pilot_ledger (pilot_id TEXT{pk}, reason TEXT)"
    )
    normalized_rows = [
        (_canonical_pilot_id(pilot_id), reason) if normalize_ids else (pilot_id, reason)
        for pilot_id, reason in rows
    ]
    conn.executemany("INSERT INTO live_pilot_ledger VALUES (?,?)", normalized_rows)
    conn.commit()
    conn.close()


def test_load_execution_calibration_joins_read_only_event_and_ledger_rows(tmp_path):
    events_path = tmp_path / "events.db"
    ledger_path = tmp_path / "ledger.db"
    _create_events_db(events_path, [
        _event_row(
            "e1", "buy-1", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00", filled_price=100_000,
        ),
        _event_row(
            "e2", "sell-1", "sell", "005930.KS", 1, 105_000,
            "2026-07-15T10:00:00+09:00", filled_price=105_000,
        ),
    ])
    _create_ledger_db(ledger_path, [
        ("buy-1", "auto_pipeline"),
        ("sell-1", "position_review_sell"),
    ])

    result = calibration.load_execution_calibration(
        events_path=events_path,
        ledger_path=ledger_path,
        min_samples=1,
    )

    assert result["status"] == "ok"
    assert result["completed_count"] == 1
    assert result["sample_sufficient"] is True
    assert result["win_rate"] == 1.0
    assert result["mean_net_return_pct"] == 4.0
    assert result["ledger_reason_missing_count"] == 0


@pytest.mark.parametrize("ledger_rows", [[], [("buy-1", "")]])
def test_load_execution_calibration_marks_missing_ledger_reason_incomplete(
    tmp_path,
    ledger_rows,
):
    events_path = tmp_path / "events.db"
    ledger_path = tmp_path / "ledger.db"
    _create_events_db(events_path, [
        _event_row(
            "e1", "buy-1", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
        ),
    ])
    _create_ledger_db(ledger_path, ledger_rows)

    result = calibration.load_execution_calibration(
        events_path=events_path,
        ledger_path=ledger_path,
    )

    assert result["status"] == "partial"
    assert result["lineage_status"] == "incomplete"
    assert result["ledger_reason_missing_count"] == 1
    assert "ledger_reason_missing" in result["lineage_reasons"]
    assert result["evidence_sufficient"] is False


def test_load_execution_calibration_quarantines_blank_pilot_id(tmp_path):
    events_path = tmp_path / "events.db"
    ledger_path = tmp_path / "ledger.db"
    _create_events_db(events_path, [
        _event_row(
            "e1", "", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00", canonical_pilot=False,
        ),
    ])
    _create_ledger_db(ledger_path, [])

    result = calibration.load_execution_calibration(
        events_path=events_path,
        ledger_path=ledger_path,
    )

    assert result["status"] == "partial"
    assert result["ledger_reason_missing_count"] == 1
    assert result["invalid_fill_count"] == 1
    assert result["lineage_status"] == "incomplete"


@pytest.mark.parametrize(
    "duplicate_reasons",
    [("auto_pipeline", "manual"), ("auto_pipeline", "auto_pipeline")],
)
def test_load_execution_calibration_rejects_ledger_without_unique_pilot_id(
    tmp_path,
    duplicate_reasons,
):
    events_path = tmp_path / "events.db"
    ledger_path = tmp_path / "ledger.db"
    _create_events_db(events_path, [
        _event_row(
            "e1", "buy-1", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
        ),
    ])
    _create_ledger_db(
        ledger_path,
        [
            ("buy-1", duplicate_reasons[0]),
            ("buy-1", duplicate_reasons[1]),
        ],
        primary_key=False,
    )

    result = calibration.load_execution_calibration(
        events_path=events_path,
        ledger_path=ledger_path,
    )

    assert result["status"] == "unavailable"
    assert result["reason"] == "execution_calibration_source_unavailable"
    assert result["evidence_sufficient"] is False


def test_load_execution_calibration_rejects_composite_ledger_primary_key(tmp_path):
    events_path = tmp_path / "events.db"
    ledger_path = tmp_path / "ledger.db"
    _create_events_db(events_path, [
        _event_row(
            "e1", "buy-1", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
        ),
    ])
    conn = sqlite3.connect(ledger_path)
    conn.execute(
        """CREATE TABLE live_pilot_ledger (
            pilot_id TEXT,
            seq INTEGER,
            reason TEXT,
            PRIMARY KEY (pilot_id, seq)
        )"""
    )
    conn.execute(
        "INSERT INTO live_pilot_ledger VALUES (?,?,?)",
        ("tlive_buy-1", 1, "auto_pipeline"),
    )
    conn.commit()
    conn.close()

    result = calibration.load_execution_calibration(
        events_path=events_path,
        ledger_path=ledger_path,
    )

    assert result["status"] == "unavailable"
    assert result["evidence_sufficient"] is False


def test_load_execution_calibration_accepts_exact_unique_ledger_index(tmp_path):
    events_path = tmp_path / "events.db"
    ledger_path = tmp_path / "ledger.db"
    _create_events_db(events_path, [
        _event_row(
            "e1", "buy-1", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
        ),
    ])
    conn = sqlite3.connect(ledger_path)
    conn.execute(
        "CREATE TABLE live_pilot_ledger (pilot_id TEXT UNIQUE, reason TEXT)"
    )
    conn.execute(
        "INSERT INTO live_pilot_ledger VALUES (?,?)",
        ("tlive_buy-1", "auto_pipeline"),
    )
    conn.commit()
    conn.close()

    result = calibration.load_execution_calibration(
        events_path=events_path,
        ledger_path=ledger_path,
    )

    assert result["status"] == "ok"
    assert result["open_lot_count"] == 1


def test_load_execution_calibration_joins_ledger_with_binary_pilot_identity(tmp_path):
    events_path = tmp_path / "events.db"
    ledger_path = tmp_path / "ledger.db"
    _create_events_db(events_path, [
        _event_row(
            "e1", "tlive_Case", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
        ),
    ])
    conn = sqlite3.connect(ledger_path)
    conn.execute(
        "CREATE TABLE live_pilot_ledger ("
        "pilot_id TEXT COLLATE NOCASE, reason TEXT)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX ledger_pilot_binary "
        "ON live_pilot_ledger(pilot_id COLLATE BINARY)"
    )
    conn.executemany(
        "INSERT INTO live_pilot_ledger VALUES (?, ?)",
        [
            ("tlive_Case", "auto_pipeline"),
            ("tlive_case", "manual"),
        ],
    )
    conn.commit()
    conn.close()

    result = calibration.load_execution_calibration(
        events_path=events_path,
        ledger_path=ledger_path,
    )

    assert result["status"] == "ok"
    assert result["open_lot_count"] == 1
    assert result["ledger_reason_conflict_count"] == 0
    assert result["ledger_reason_missing_count"] == 0


def test_load_execution_calibration_quarantines_malformed_production_side(
    tmp_path,
):
    events_path = tmp_path / "events.db"
    ledger_path = tmp_path / "ledger.db"
    _create_events_db(events_path, [
        _event_row(
            "e1", "tlive_badside", "BUY", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
        ),
    ])
    _create_ledger_db(
        ledger_path,
        [("tlive_badside", "auto_pipeline")],
    )

    result = calibration.load_execution_calibration(
        events_path=events_path,
        ledger_path=ledger_path,
    )

    assert result["source_rows_loaded"] == 1
    assert result["invalid_fill_count"] == 1
    assert result["quarantined_fill_count"] == 1
    assert result["status"] == "partial"
    assert result["lineage_status"] == "incomplete"
    assert "fill_contract_invalid" in result["lineage_reasons"]


def test_load_execution_calibration_quarantines_side_reason_mismatch(tmp_path):
    events_path = tmp_path / "events.db"
    ledger_path = tmp_path / "ledger.db"
    _create_events_db(events_path, [
        _event_row(
            "e1", "tlive_reason_mismatch", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
        ),
    ])
    _create_ledger_db(
        ledger_path,
        [("tlive_reason_mismatch", "position_review_sell")],
    )

    result = calibration.load_execution_calibration(
        events_path=events_path,
        ledger_path=ledger_path,
    )

    assert result["source_rows_loaded"] == 1
    assert result["invalid_fill_count"] == 1
    assert result["quarantined_fill_count"] == 1
    assert result["status"] == "partial"
    assert result["lineage_status"] == "incomplete"
    assert "fill_contract_invalid" in result["lineage_reasons"]


def test_load_execution_calibration_quarantines_unknown_ledger_reason(tmp_path):
    events_path = tmp_path / "events.db"
    ledger_path = tmp_path / "ledger.db"
    _create_events_db(events_path, [
        _event_row(
            "e1", "tlive_unknown_reason", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
        ),
    ])
    _create_ledger_db(
        ledger_path,
        [("tlive_unknown_reason", "unexpected_strategy")],
    )

    result = calibration.load_execution_calibration(
        events_path=events_path,
        ledger_path=ledger_path,
    )

    assert result["source_rows_loaded"] == 1
    assert result["invalid_fill_count"] == 1
    assert result["quarantined_fill_count"] == 1
    assert result["status"] == "partial"
    assert result["lineage_status"] == "incomplete"
    assert "fill_contract_invalid" in result["lineage_reasons"]


def test_load_execution_calibration_quarantines_blob_ledger_reason(tmp_path):
    events_path = tmp_path / "events.db"
    ledger_path = tmp_path / "ledger.db"
    _create_events_db(events_path, [
        _event_row(
            "e1", "tlive_blob_reason", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
        ),
    ])
    _create_ledger_db(
        ledger_path,
        [("tlive_blob_reason", sqlite3.Binary(b"auto_pipeline"))],
    )

    result = calibration.load_execution_calibration(
        events_path=events_path,
        ledger_path=ledger_path,
    )

    assert result["source_rows_loaded"] == 1
    assert result["ledger_reason_invalid_count"] == 1
    assert result["invalid_fill_count"] == 1
    assert result["quarantined_fill_count"] == 1
    assert result["status"] == "partial"
    assert result["lineage_status"] == "incomplete"
    assert "ledger_reason_invalid" in result["lineage_reasons"]


@pytest.mark.parametrize(
    ("table_ddl", "index_ddl"),
    [
        (
            "CREATE TABLE live_pilot_ledger ("
            "pilot_id TEXT COLLATE NOCASE PRIMARY KEY, reason TEXT)",
            None,
        ),
        (
            "CREATE TABLE live_pilot_ledger (pilot_id TEXT, reason TEXT)",
            "CREATE UNIQUE INDEX ledger_pilot_nocase "
            "ON live_pilot_ledger(pilot_id COLLATE NOCASE)",
        ),
    ],
)
def test_unique_ledger_schema_rejects_case_insensitive_pilot_identity(
    table_ddl,
    index_ddl,
):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(table_ddl)
        if index_ddl is not None:
            conn.execute(index_ddl)

        with pytest.raises(sqlite3.DatabaseError):
            calibration._require_unique_ledger_schema(conn)
    finally:
        conn.close()


def test_load_execution_calibration_bounds_source_window_and_marks_truncation(tmp_path):
    events_path = tmp_path / "events.db"
    ledger_path = tmp_path / "ledger.db"
    rows = [
        _event_row(
            "e1", "buy-1", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
        ),
        _event_row(
            "e2", "sell-1", "sell", "005930.KS", 1, 101_000,
            "2026-07-15T10:00:00+09:00", filled_price=101_000,
        ),
        _event_row(
            "e3", "buy-2", "buy", "000660.KS", 1, 100_000,
            "2026-07-15T11:00:00+09:00",
        ),
    ]
    _create_events_db(events_path, rows)
    _create_ledger_db(ledger_path, [
        ("buy-1", "auto_pipeline"),
        ("sell-1", "position_review_sell"),
        ("buy-2", "auto_pipeline"),
    ])

    result = calibration.load_execution_calibration(
        events_path=events_path,
        ledger_path=ledger_path,
        max_source_rows=2,
    )

    assert result["source_window_truncated"] is True
    assert result["source_row_limit"] == 2
    assert result["source_rows_loaded"] == 2
    assert result["status"] == "partial"
    assert result["lineage_status"] == "incomplete"
    assert "source_window_truncated" in result["lineage_reasons"]
    assert result["evidence_sufficient"] is False


@pytest.mark.parametrize("row_count", [0, 1])
def test_load_execution_calibration_handles_zero_and_one_source_row(tmp_path, row_count):
    events_path = tmp_path / "events.db"
    ledger_path = tmp_path / "ledger.db"
    rows = []
    reasons = []
    if row_count:
        rows.append(_event_row(
            "e1", "buy-1", "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
        ))
        reasons.append(("buy-1", "auto_pipeline"))
    _create_events_db(events_path, rows)
    _create_ledger_db(ledger_path, reasons)

    result = calibration.load_execution_calibration(
        events_path=events_path,
        ledger_path=ledger_path,
    )

    assert result["source_rows_loaded"] == row_count
    assert result["source_window_truncated"] is False


def test_load_execution_calibration_truncates_5001_rows_at_ceiling(tmp_path):
    events_path = tmp_path / "events.db"
    ledger_path = tmp_path / "ledger.db"
    rows = []
    reasons = []
    for index in range(5_001):
        pilot_id = f"buy-{index}"
        rows.append(_event_row(
            f"e-{index}", pilot_id, "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
        ))
        reasons.append((pilot_id, "auto_pipeline"))
    _create_events_db(events_path, rows)
    _create_ledger_db(ledger_path, reasons)

    result = calibration.load_execution_calibration(
        events_path=events_path,
        ledger_path=ledger_path,
        max_source_rows=5_000,
    )

    assert result["source_rows_loaded"] == 5_000
    assert result["source_window_truncated"] is True
    assert "source_window_truncated" in result["lineage_reasons"]


def test_load_execution_calibration_does_not_truncate_exactly_5000_rows(tmp_path):
    events_path = tmp_path / "events.db"
    ledger_path = tmp_path / "ledger.db"
    rows = []
    reasons = []
    for index in range(5_000):
        pilot_id = f"buy-{index}"
        rows.append(_event_row(
            f"e-{index}", pilot_id, "buy", "005930.KS", 1, 100_000,
            "2026-07-15T09:00:00+09:00",
        ))
        reasons.append((pilot_id, "auto_pipeline"))
    _create_events_db(events_path, rows)
    _create_ledger_db(ledger_path, reasons)

    result = calibration.load_execution_calibration(
        events_path=events_path,
        ledger_path=ledger_path,
        max_source_rows=5_000,
    )

    assert result["source_rows_loaded"] == 5_000
    assert result["source_window_truncated"] is False
    assert "source_window_truncated" not in result["lineage_reasons"]


def test_readonly_connection_uses_sqlite_mode_ro_uri(tmp_path, monkeypatch):
    events_path = tmp_path / "events.db"
    _create_events_db(events_path, [])
    real_connect = calibration.sqlite3.connect
    observed = {}

    def capture_connect(database, *args, **kwargs):
        observed["database"] = database
        observed["uri"] = kwargs.get("uri")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(calibration.sqlite3, "connect", capture_connect)
    conn = calibration._readonly_connection(events_path)
    conn.close()

    assert observed["uri"] is True
    assert str(observed["database"]).endswith("?mode=ro")


def test_readonly_connection_enforces_sqlite_query_only(tmp_path):
    events_path = tmp_path / "events.db"
    _create_events_db(events_path, [])

    conn = calibration._readonly_connection(events_path)
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO live_pilot_events (event_id) VALUES ('x')")
    finally:
        conn.close()


@pytest.mark.parametrize(
    "relative_path",
    [
        "core/toss_autonomous_pipeline.py",
        "core/toss_autonomous_finalizer.py",
        "core/toss_live_pilot_adapter.py",
        "core/toss_live_pilot_policy.py",
        "core/toss_order_preview.py",
    ],
)
def test_execution_calibration_is_not_consumed_by_order_or_sell_paths(
    relative_path,
):
    source = (Path(__file__).parent.parent / relative_path).read_text(
        encoding="utf-8"
    )

    assert "toss_execution_calibration" not in source
    assert "execution_calibration" not in source
