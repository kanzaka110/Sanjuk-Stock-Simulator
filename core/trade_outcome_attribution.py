"""결정론 매매 결과 귀속·사후검증 엔진.

기존 추천(predictions)과 관측된 실제 체결을 읽어 추천 품질, 시장 방향,
종목 선택, 체결 성과를 분리한다. DB 쓰기·가격 조회·주문 권한은 없다.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

VERSION = "trade_outcome_attribution_v1"
KST = timezone(timedelta(hours=9))
_EVALUATED_OUTCOMES = {"win", "loss", "neutral"}
_EXCLUDED_ACTION_TYPES = {"CANCEL_SELL", "HOLD_REVIEW", "WATCH_ONLY", "BLOCKED_BUY"}
_REAL_LIVE_EVENT_TYPES = {"live_sent"}
_FULLY_FILLED_STATUSES = {"FILLED", "COMPLETED", "EXECUTED", "체결"}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if symbol.isdigit() and len(symbol) == 6:
        return f"{symbol}.KS"
    return symbol


def _side(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"buy", "b", "매수"}:
        return "buy"
    if text in {"sell", "s", "매도"}:
        return "sell"
    return "neutral"


def _decision_ref(row: Mapping[str, Any]) -> str:
    direct = str(row.get("decision_ref") or "").strip()
    if direct:
        return direct
    prediction_id = row.get("source_prediction_id")
    if prediction_id in (None, ""):
        prediction_id = row.get("prediction_id")
    if prediction_id not in (None, ""):
        return f"prediction:{prediction_id}"
    return ""


def _prediction_side(row: Mapping[str, Any]) -> str:
    side = _side(row.get("signal") or row.get("original_signal"))
    if side != "neutral":
        return side
    action = str(row.get("action_type") or "").upper()
    if "BUY" in action or "매수" in action:
        return "buy"
    if "SELL" in action or "매도" in action:
        return "sell"
    return "neutral"


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=KST) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def _realistic_pnl(symbol: str, value: float) -> bool:
    threshold = 100.0 if symbol.endswith((".KS", ".KQ")) else 300.0
    return abs(value) <= threshold


def _benchmark_value(mapping: Mapping[Any, Any], prediction_id: Any) -> float | None:
    for key in (prediction_id, str(prediction_id)):
        if key in mapping:
            try:
                return float(mapping[key])
            except (TypeError, ValueError):
                return None
    return None


def normalize_execution_records(
    manual_trades: Iterable[Mapping[str, Any]] | None = None,
    live_events: Iterable[Mapping[str, Any]] | None = None,
    broker_orders: Iterable[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """서로 다른 체결 원본을 공통 read-only 계약으로 정규화한다.

    live event는 production invariant를 모두 만족한 `live_sent`만 인정한다.
    broker order는 GET 원본의 체결 상태·수량·가격이 모두 확인된 행만 인정한다.
    """
    records: list[dict[str, Any]] = []

    for row in manual_trades or []:
        symbol = _symbol(row.get("ticker") or row.get("symbol"))
        side = _side(row.get("side") or row.get("action"))
        price = _num(row.get("price") or row.get("filled_price"))
        quantity = _num(row.get("shares") or row.get("quantity") or row.get("filled_quantity"))
        created_at = str(row.get("created_at") or row.get("filled_at") or "")
        if symbol and side != "neutral" and price > 0 and quantity > 0 and _parse_time(created_at):
            records.append({
                "execution_id": f"manual:{row.get('id', created_at)}",
                "decision_ref": _decision_ref(row),
                "symbol": symbol,
                "side": side,
                "state": "filled",
                "filled_price": price,
                "filled_quantity": quantity,
                "executed_at": created_at,
                "account": str(row.get("account") or ""),
                "fees": _num(row.get("fees")),
                "taxes": _num(row.get("taxes")),
                "cost_basis_price": _num(row.get("cost_basis_price") or row.get("avg_cost")),
                "source": "manual_trade_log",
                "is_real_execution": True,
            })

    for row in live_events or []:
        event_type = str(row.get("event_type") or "")
        is_real = (
            event_type in _REAL_LIVE_EVENT_TYPES
            and bool(row.get("live_order_sent"))
            and str(row.get("adapter_status") or "") == "enabled"
            and bool(row.get("live_order_allowed"))
        )
        symbol = _symbol(row.get("symbol") or row.get("ticker"))
        side = _side(row.get("side"))
        price = _num(row.get("filled_price"))
        quantity = _num(row.get("filled_quantity"))
        created_at = str(row.get("created_at") or row.get("filled_at") or "")
        if is_real and symbol and side != "neutral" and price > 0 and quantity > 0 and _parse_time(created_at):
            broker_status = str(row.get("broker_order_status") or "").upper()
            records.append({
                "execution_id": f"live:{row.get('event_id', created_at)}",
                "decision_ref": _decision_ref(row),
                "symbol": symbol,
                "side": side,
                "state": "filled" if broker_status in _FULLY_FILLED_STATUSES else "partial",
                "filled_price": price,
                "filled_quantity": quantity,
                "executed_at": created_at,
                "account": "",
                "fees": _num(row.get("fees")),
                "taxes": _num(row.get("taxes")),
                "cost_basis_price": _num(row.get("cost_basis_price") or row.get("avg_cost")),
                "source": "toss_live_event",
                "is_real_execution": True,
            })

    for row in broker_orders or []:
        status = str(row.get("broker_order_status") or row.get("status") or "").upper()
        symbol = _symbol(row.get("symbol") or row.get("ticker"))
        side = _side(row.get("side"))
        price = _num(row.get("filled_price") or row.get("average_filled_price"))
        quantity = _num(row.get("filled_quantity") or row.get("quantity"))
        created_at = str(row.get("filled_at") or row.get("ordered_at") or row.get("created_at") or "")
        if symbol and side != "neutral" and price > 0 and quantity > 0 and _parse_time(created_at):
            records.append({
                "execution_id": f"broker:{row.get('broker_order_id_masked') or row.get('id') or created_at}",
                "decision_ref": _decision_ref(row),
                "symbol": symbol,
                "side": side,
                "state": "filled" if status in _FULLY_FILLED_STATUSES else "partial",
                "filled_price": price,
                "filled_quantity": quantity,
                "executed_at": created_at,
                "account": "",
                "fees": _num(row.get("fees")),
                "taxes": _num(row.get("taxes")),
                "cost_basis_price": _num(row.get("cost_basis_price") or row.get("avg_cost")),
                "source": "toss_broker_orders_get",
                "is_real_execution": True,
            })

    unique: dict[str, dict[str, Any]] = {}
    for row in records:
        unique.setdefault(row["execution_id"], row)
    return sorted(unique.values(), key=lambda row: row["executed_at"])


def _quality_status(
    row: Mapping[str, Any], symbol: str, side: str, *, activated: bool = False,
) -> str:
    action_type = str(row.get("action_type") or "").upper()
    action_grade = str(row.get("action_grade") or "").upper()
    status = str(row.get("status") or "").lower()
    outcome = str(row.get("outcome") or "").lower()
    pnl = _num(row.get("pnl_pct"))
    if (
        action_type in _EXCLUDED_ACTION_TYPES
        or action_grade in {"BLOCKED", "WATCH"}
        or side == "neutral"
    ):
        return "excluded_non_actionable"
    if action_type.startswith("CONDITIONAL_") and not (
        activated
        or _parse_time(row.get("activated_at")) is not None
        or _num(row.get("activated_price")) > 0
    ):
        return "excluded_not_activated"
    if status != "closed":
        return "open"
    if outcome in {"invalid", "data_error", "expired"}:
        return f"excluded_{outcome}"
    if outcome not in _EVALUATED_OUTCOMES:
        return "excluded_unknown_outcome"
    if _num(row.get("entry_price")) <= 0 or not _realistic_pnl(symbol, pnl):
        return "excluded_data_quality"
    return "evaluated"


def _match_executions(
    predictions: list[dict[str, Any]],
    executions: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """직접 decision_ref가 일치하는 체결만 추천에 귀속한다.

    브로커 GET 원장을 event/manual보다 우선하며, 같은 ref의 다중 행은 수량을
    합산하지 않고 우선순위가 높은 최신 누적 행 하나를 사용한다.
    """
    source_priority = {
        "toss_broker_orders_get": 3,
        "toss_live_event": 2,
        "manual_trade_log": 1,
    }
    matches: dict[int, dict[str, Any]] = {}
    for index, prediction in enumerate(predictions):
        prediction_id = prediction.get("id")
        prediction_ref = _decision_ref(prediction)
        if not prediction_ref and prediction_id not in (None, ""):
            prediction_ref = f"prediction:{prediction_id}"
        if not prediction_ref:
            continue
        symbol = _symbol(prediction.get("ticker") or prediction.get("symbol"))
        side = _prediction_side(prediction)
        eligible = [
            execution for execution in executions
            if str(execution.get("decision_ref") or "") == prediction_ref
            and execution.get("symbol") == symbol
            and execution.get("side") == side
        ]
        if not eligible:
            continue
        matches[index] = max(
            eligible,
            key=lambda execution: (
                source_priority.get(str(execution.get("source") or ""), 0),
                str(execution.get("executed_at") or ""),
                _num(execution.get("filled_quantity")),
            ),
        )
    return matches


def _row_result(
    row: Mapping[str, Any],
    execution: Mapping[str, Any] | None,
    benchmark_return_pct: float | None,
) -> dict[str, Any]:
    symbol = _symbol(row.get("ticker") or row.get("symbol"))
    side = _prediction_side(row)
    quality = _quality_status(row, symbol, side, activated=execution is not None)
    prediction_pnl = _num(row.get("pnl_pct")) if quality == "evaluated" else None
    direction_benchmark = None
    alpha = None
    market_direction = "not_available"
    stock_selection = "not_available"
    if quality == "evaluated" and benchmark_return_pct is not None:
        prediction_pnl_value = float(prediction_pnl or 0.0)
        direction_benchmark = benchmark_return_pct if side == "buy" else -benchmark_return_pct
        alpha = prediction_pnl_value - direction_benchmark
        market_direction = (
            "correct" if direction_benchmark > 0.5
            else "wrong" if direction_benchmark < -0.5
            else "flat"
        )
        stock_selection = (
            "outperformed" if alpha > 0.5
            else "underperformed" if alpha < -0.5
            else "flat"
        )

    execution_status = "linked" if execution else "not_linked"
    slippage_pct = None
    actual_directional_return_pct = None
    actual_net_return_pct = None
    execution_effect_pct = None
    avoided_move_return_pct = None
    realized_pnl_pct = None
    if execution:
        entry = _num(row.get("entry_price"))
        fill = _num(execution.get("filled_price"))
        closed = _num(row.get("closed_price"))
        quantity = _num(execution.get("filled_quantity"))
        if entry > 0 and fill > 0:
            slippage_pct = (
                (fill - entry) / entry * 100
                if side == "buy"
                else (entry - fill) / entry * 100
            )
        if quality == "evaluated" and fill > 0 and closed > 0:
            actual_directional_return_pct = (
                (closed - fill) / fill * 100
                if side == "buy"
                else (fill - closed) / fill * 100
            )
            execution_effect_pct = actual_directional_return_pct - float(prediction_pnl or 0.0)
            if side == "sell":
                avoided_move_return_pct = actual_directional_return_pct
            costs = _num(execution.get("fees")) + _num(execution.get("taxes"))
            if costs > 0 and fill * quantity > 0:
                actual_net_return_pct = actual_directional_return_pct - costs / (fill * quantity) * 100
        cost_basis = _num(execution.get("cost_basis_price"))
        if side == "sell" and cost_basis > 0 and fill > 0:
            realized_pnl_pct = (fill - cost_basis) / cost_basis * 100

    return {
        "prediction_id": row.get("id"),
        "decision_ref": _decision_ref(row) or (
            f"prediction:{row.get('id')}" if row.get("id") not in (None, "") else ""
        ),
        "created_at": str(row.get("created_at") or ""),
        "closed_at": str(row.get("closed_at") or ""),
        "ticker": symbol,
        "name": str(row.get("name") or symbol),
        "side": side,
        "signal": str(row.get("signal") or ""),
        "action_type": str(row.get("action_type") or ""),
        "persona": str(row.get("persona") or "unknown"),
        "strategy_type": str(row.get("strategy_type") or "unknown"),
        "strategy_tags": [
            tag.strip() for tag in str(row.get("strategy_tags") or "").split(",")
            if tag.strip()
        ],
        "agreement_count": int(_num(row.get("agreement_count"))),
        "confidence": int(_num(row.get("confidence"))),
        "briefing_type": str(row.get("briefing_type") or "unknown"),
        "account_type": str(row.get("account_type") or ""),
        "quality_status": quality,
        "recommendation_outcome": str(row.get("outcome") or ""),
        "recommendation_pnl_pct": round(prediction_pnl, 2) if prediction_pnl is not None else None,
        "benchmark_ticker": str(row.get("benchmark_ticker") or ""),
        "benchmark_return_pct": round(benchmark_return_pct, 2) if benchmark_return_pct is not None else None,
        "direction_adjusted_benchmark_pct": round(direction_benchmark, 2) if direction_benchmark is not None else None,
        "selection_alpha_pct": round(alpha, 2) if alpha is not None else None,
        "market_direction": market_direction,
        "stock_selection": stock_selection,
        "execution_status": execution_status,
        "linkage_status": "direct" if execution else "unavailable",
        "execution_state": str(execution.get("state") or "") if execution else "",
        "execution_source": str(execution.get("source") or "") if execution else "",
        "execution_ref": str(execution.get("execution_id") or "") if execution else "",
        "executed_at": str(execution.get("executed_at") or "") if execution else "",
        "filled_price": _num(execution.get("filled_price")) if execution else None,
        "filled_quantity": _num(execution.get("filled_quantity")) if execution else None,
        "slippage_pct": round(slippage_pct, 2) if slippage_pct is not None else None,
        "actual_execution_directional_return_pct": (
            round(actual_directional_return_pct, 2)
            if actual_directional_return_pct is not None else None
        ),
        "actual_execution_net_return_pct": (
            round(actual_net_return_pct, 2) if actual_net_return_pct is not None else None
        ),
        "execution_effect_pct": (
            round(execution_effect_pct, 2) if execution_effect_pct is not None else None
        ),
        "avoided_move_return_pct": (
            round(avoided_move_return_pct, 2) if avoided_move_return_pct is not None else None
        ),
        "realized_pnl_pct": (
            round(realized_pnl_pct, 2) if realized_pnl_pct is not None else None
        ),
        "execution_note": (
            "직접 decision_ref가 일치한 실제 체결과 연결됨"
            if execution
            else "직접 연결 키 없음 — 추천 cohort와 체결 cohort를 독립 집계함"
        ),
    }


def _group_summary(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key) or "unknown")].append(row)
    output = []
    for name, group in groups.items():
        resolved = [row for row in group if row["quality_status"] == "evaluated"]
        wins = sum(row["recommendation_outcome"] == "win" for row in resolved)
        losses = sum(row["recommendation_outcome"] == "loss" for row in resolved)
        neutral = sum(row["recommendation_outcome"] == "neutral" for row in resolved)
        decisive = wins + losses
        pnls = [row["recommendation_pnl_pct"] for row in resolved if row["recommendation_pnl_pct"] is not None]
        alphas = [row["selection_alpha_pct"] for row in resolved if row["selection_alpha_pct"] is not None]
        output.append({
            "key": name,
            "total": len(group),
            "resolved": len(resolved),
            "evaluated": decisive,
            "neutral": neutral,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wins / decisive * 100, 1) if decisive else None,
            "avg_recommendation_pnl_pct": round(sum(pnls) / len(pnls), 2) if pnls else None,
            "avg_selection_alpha_pct": round(sum(alphas) / len(alphas), 2) if alphas else None,
            "linked_executions": sum(row["execution_status"] == "linked" for row in group),
        })
    return sorted(output, key=lambda row: (-row["evaluated"], -row["resolved"], row["key"]))


def _strategy_tag_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded = []
    for row in rows:
        for tag in row.get("strategy_tags") or []:
            tagged = dict(row)
            tagged["strategy_tag"] = tag
            expanded.append(tagged)
    return _group_summary(expanded, "strategy_tag")


def calculate_trade_outcome_attribution(
    predictions: Iterable[Mapping[str, Any]] | None,
    *,
    executions: Iterable[Mapping[str, Any]] | None = None,
    benchmark_returns_by_prediction_id: Mapping[Any, Any] | None = None,
) -> dict[str, Any]:
    """추천·체결·벤치마크 snapshot의 read-only 귀속 보고서를 만든다."""
    prediction_rows = deepcopy([dict(row) for row in (predictions or [])])
    execution_rows = deepcopy([dict(row) for row in (executions or [])])
    benchmark_map = dict(benchmark_returns_by_prediction_id or {})
    matches = _match_executions(prediction_rows, execution_rows)
    rows = []
    for index, prediction in enumerate(prediction_rows):
        benchmark = _benchmark_value(benchmark_map, prediction.get("id"))
        rows.append(_row_result(prediction, matches.get(index), benchmark))

    quality_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        quality_counts[row["quality_status"]] += 1
    resolved = [row for row in rows if row["quality_status"] == "evaluated"]
    actionable = [row for row in rows if row["quality_status"] != "excluded_non_actionable"]
    activated_actionable = [
        row for row in actionable if row["quality_status"] != "excluded_not_activated"
    ]
    wins = sum(row["recommendation_outcome"] == "win" for row in resolved)
    losses = sum(row["recommendation_outcome"] == "loss" for row in resolved)
    neutral = sum(row["recommendation_outcome"] == "neutral" for row in resolved)
    decisive = wins + losses
    pnls = [row["recommendation_pnl_pct"] for row in resolved if row["recommendation_pnl_pct"] is not None]
    alpha_rows = [row for row in resolved if row["selection_alpha_pct"] is not None]
    linked = [row for row in rows if row["execution_status"] == "linked"]
    linked_execution_ids = {row["execution_ref"] for row in linked if row["execution_ref"]}
    unlinked_executions = [
        execution for execution in execution_rows
        if str(execution.get("execution_id") or "") not in linked_execution_ids
    ]
    slippages = [row["slippage_pct"] for row in linked if row["slippage_pct"] is not None]
    directional_returns = [
        row["actual_execution_directional_return_pct"] for row in linked
        if row["actual_execution_directional_return_pct"] is not None
    ]
    source_counts: dict[str, int] = defaultdict(int)
    state_counts: dict[str, int] = defaultdict(int)
    for execution in execution_rows:
        source_counts[str(execution.get("source") or "unknown")] += 1
        state_counts[str(execution.get("state") or "unknown")] += 1

    return {
        "version": VERSION,
        "read_only": True,
        "order_side_effects": False,
        "matching_rule": {
            "method": "direct_decision_ref_only",
            "ticker_and_side_must_also_match": True,
            "broker_get_preferred": True,
            "multiple_fill_rows_are_not_summed": True,
            "unmatched_meaning": "independent_execution_cohort_not_loss",
        },
        "summary": {
            "total_predictions": len(rows),
            "actionable_predictions": len(actionable),
            "activated_actionable_predictions": len(activated_actionable),
            "resolved_predictions": len(resolved),
            "evaluated_predictions": decisive,
            "wins": wins,
            "losses": losses,
            "neutral": neutral,
            "win_rate_pct": round(wins / decisive * 100, 1) if decisive else None,
            "avg_recommendation_pnl_pct": round(sum(pnls) / len(pnls), 2) if pnls else None,
            "observed_real_executions": len(execution_rows),
            "directly_linked_executions": len(linked),
            "unlinked_executions": len(unlinked_executions),
            "execution_linkage_rate_pct": round(
                len(linked) / len(execution_rows) * 100, 1
            ) if execution_rows else 0.0,
            "avg_linked_slippage_pct": round(
                sum(slippages) / len(slippages), 2
            ) if slippages else None,
            "avg_linked_directional_return_pct": round(
                sum(directional_returns) / len(directional_returns), 2
            ) if directional_returns else None,
        },
        "execution_cohort": {
            "independent_from_recommendations": True,
            "observed_real_executions": len(execution_rows),
            "directly_linked_executions": len(linked),
            "unlinked_executions": len(unlinked_executions),
            "source_counts": dict(sorted(source_counts.items())),
            "state_counts": dict(sorted(state_counts.items())),
            "unlinked_rows": unlinked_executions,
        },
        "benchmark_attribution": {
            "status": "available" if alpha_rows else "not_requested",
            "available_count": len(alpha_rows),
            "market_direction_correct": sum(row["market_direction"] == "correct" for row in alpha_rows),
            "market_direction_wrong": sum(row["market_direction"] == "wrong" for row in alpha_rows),
            "market_direction_flat": sum(row["market_direction"] == "flat" for row in alpha_rows),
            "stock_selection_outperformed": sum(row["stock_selection"] == "outperformed" for row in alpha_rows),
            "stock_selection_underperformed": sum(row["stock_selection"] == "underperformed" for row in alpha_rows),
            "stock_selection_flat": sum(row["stock_selection"] == "flat" for row in alpha_rows),
            "avg_selection_alpha_pct": round(
                sum(row["selection_alpha_pct"] for row in alpha_rows) / len(alpha_rows), 2
            ) if alpha_rows else None,
        },
        "data_quality": {
            "quality_counts": dict(sorted(quality_counts.items())),
            "resolved_rate_pct": round(len(resolved) / len(rows) * 100, 1) if rows else 0.0,
            "evaluated_rate_pct": round(decisive / len(rows) * 100, 1) if rows else 0.0,
            "benchmark_coverage_pct": round(len(alpha_rows) / len(resolved) * 100, 1) if resolved else 0.0,
            "execution_link_is_inferred": False,
            "direct_prediction_execution_id_available": bool(linked),
            "unlinked_execution_count": len(unlinked_executions),
        },
        "by_ticker": _group_summary(rows, "ticker"),
        "by_persona": _group_summary(rows, "persona"),
        "by_strategy_type": _group_summary(rows, "strategy_type"),
        "by_strategy_tag": _strategy_tag_summary(rows),
        "by_action_type": _group_summary(rows, "action_type"),
        "by_briefing_type": _group_summary(rows, "briefing_type"),
        "rows": rows,
    }


def hermes_interpretation_payload(report: Mapping[str, Any] | None) -> dict[str, Any]:
    """Hermes가 재계산 없이 사후검증을 설명할 최소 사실 묶음."""
    source = report if isinstance(report, Mapping) else {}
    return {
        "version": "trade_outcome_attribution_interpretation_v1",
        "read_only": True,
        "summary": dict(source.get("summary") or {}),
        "execution_cohort": dict(source.get("execution_cohort") or {}),
        "benchmark_attribution": dict(source.get("benchmark_attribution") or {}),
        "data_quality": dict(source.get("data_quality") or {}),
        "top_tickers": list(source.get("by_ticker") or [])[:10],
        "top_strategy_types": list(source.get("by_strategy_type") or [])[:10],
        "top_strategy_tags": list(source.get("by_strategy_tag") or [])[:10],
        "interpretation_rules": [
            "추천 성과와 실제 체결 성과를 섞지 않는다",
            "직접 decision_ref 없는 체결은 추천에 임의 연결하지 않는다",
            "미연결 체결은 독립 cohort이며 미체결·실패·손실로 단정하지 않는다",
            "조건부 추천은 활성화 증거가 없으면 승패 분모에서 제외한다",
            "invalid/data_error/expired를 승패 분모에서 제외한다",
            "매도 방향수익을 실제 실현손익으로 표현하지 않는다",
            "시장 방향과 종목 선택 알파를 별도로 설명한다",
            "보고서는 자동매도·주문 권한이 없다",
        ],
    }
