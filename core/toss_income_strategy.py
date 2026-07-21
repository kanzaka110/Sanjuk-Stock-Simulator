"""Toss 수입형 자율매매 v2 dual-EV income gate.

추천 점수만으로 자동 BUY가 실행되지 않도록, 다음 청산 관측 EV와
검증된 실행 decision EV를 분리해 실행 직전 후보를 한 번 더 차단한다.
이 모듈은 read-only 계산만 수행하며 주문/취소/정정 부작용이 없다.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

_MIN_EDGE_RATIO = 0.006
_MIN_EXPECTED_KRW = 7_000.0
_STRONG_EDGE_RATIO = 0.012
_STRONG_EXPECTED_KRW = 15_000.0
_MAX_STOP_RISK_PCT = 4.5
_INCOME_PLAN_STOP_RISK_PCT = 4.2
_INCOME_TAKE_PROFIT_SINGLE_PCT = 1.5
_INCOME_EARLY_STOP_LOSS_PCT = -2.5
_INCOME_HARD_STOP_LOSS_PCT = -4.5
_MIN_RISK_REWARD = 1.5
_DEFAULT_FX_USDKRW = 1400.0
_MAX_EXECUTION_QUANTITY = 1_000_000
_MAX_EXECUTION_PRICE = 1_000_000_000_000.0
_MAX_PRICE_ALIAS_RATIO = 10.0
_MAX_TARGET_PRICE_RATIO = 10.0
_MAX_RISK_REWARD = 100.0
_MAX_REPORTED_NOTIONAL = 1_000_000_000_000_000_000.0
_MAX_FX_USDKRW = 1_000_000.0
_EXECUTION_VALUE_FIELDS = (
    "quantity", "limit_price", "entry_price", "price", "current_price",
    "target_price", "stop_loss", "risk_reward", "score",
    "estimated_amount_krw", "estimated_amount_usd", "fx_usdkrw",
)
_EXECUTION_INPUT_ERRORS = frozenset({
    "quantity_invalid", "entry_price_invalid", "target_price_invalid",
    "stop_loss_invalid", "risk_reward_invalid", "score_invalid",
    "estimated_notional_invalid", "fx_usdkrw_invalid", "side_invalid",
    "market_invalid", "symbol_market_mismatch", "currency_invalid",
    "canonical_notional_invalid", "quality_data_starvation",
    "canonical_quality_score_invalid",
})
_EXECUTABLE_DECISION_CONTRACTS = frozenset({
    ("income_exit_lifecycle_v1", "full_position_threshold_exit"),
    ("income_exit_lifecycle_v2", "full_position_threshold_exit"),
})


def _num(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "" or isinstance(value, bool):
            return default
        number = float(str(value).replace(",", ""))
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _strict_finite_number(value, *, positive: bool = False) -> float | None:
    """실행 경계용 숫자 검증. bool·문자열·NaN/Inf를 숫자로 승격하지 않는다."""
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return number


def _strict_bounded_number(value, maximum: float) -> float | None:
    number = _strict_finite_number(value, positive=True)
    return number if number is not None and number <= maximum else None


def detect_explicit_toss_input_error(candidate: Mapping) -> str:
    """Normalization 전에 명시된 오염 입력을 탐지한다. 누락값은 sizing 대상이다."""
    upstream_error = candidate.get("upstream_input_validation_error")
    if type(upstream_error) is str and upstream_error in _EXECUTION_INPUT_ERRORS:
        return upstream_error
    if "side" in candidate and (
        type(candidate.get("side")) is not str
        or str(candidate.get("side")).strip().lower() != "buy"
    ):
        return "side_invalid"
    if "market" in candidate and (
        type(candidate.get("market")) is not str
        or str(candidate.get("market")).strip().upper() not in {"KR", "US"}
    ):
        return "market_invalid"
    raw_symbol = (
        candidate.get("symbol")
        if "symbol" in candidate
        else candidate.get("ticker")
    )
    symbol = raw_symbol.upper().strip() if type(raw_symbol) is str else ""
    symbol_market = (
        "KR" if symbol.endswith((".KS", ".KQ")) or symbol.isdigit() else "US"
    ) if symbol else ""
    raw_market = candidate.get("market")
    market = raw_market.strip().upper() if type(raw_market) is str else symbol_market
    if symbol_market and market and symbol_market != market:
        return "symbol_market_mismatch"
    if "currency" in candidate:
        currency = candidate.get("currency")
        expected_currency = "KRW" if market == "KR" else "USD" if market == "US" else ""
        if (
            type(currency) is not str
            or currency not in {"KRW", "USD"}
            or not expected_currency
            or currency != expected_currency
        ):
            return "currency_invalid"
    if "quantity" in candidate:
        quantity = candidate.get("quantity")
        if type(quantity) is not int or not 0 < quantity <= _MAX_EXECUTION_QUANTITY:
            return "quantity_invalid"
    for key in ("limit_price", "entry_price", "price", "current_price"):
        if key in candidate and _strict_bounded_number(
            candidate.get(key), _MAX_EXECUTION_PRICE
        ) is None:
            return "entry_price_invalid"
    if "target_price" in candidate and _strict_bounded_number(
        candidate.get("target_price"), _MAX_EXECUTION_PRICE
    ) is None:
        return "target_price_invalid"
    if "stop_loss" in candidate and _strict_bounded_number(
        candidate.get("stop_loss"), _MAX_EXECUTION_PRICE
    ) is None:
        return "stop_loss_invalid"
    if "risk_reward" in candidate and _strict_bounded_number(
        candidate.get("risk_reward"), _MAX_RISK_REWARD
    ) is None:
        return "risk_reward_invalid"
    if "score" in candidate:
        score = _strict_finite_number(candidate.get("score"), positive=True)
        if score is None or score > 100.0:
            return "score_invalid"
    if "estimated_amount_krw" in candidate and _strict_bounded_number(
        candidate.get("estimated_amount_krw"), _MAX_REPORTED_NOTIONAL
    ) is None:
        return "estimated_notional_invalid"
    if (
        candidate.get("estimated_amount_usd") is not None
        and _strict_bounded_number(
            candidate.get("estimated_amount_usd"), _MAX_REPORTED_NOTIONAL
        ) is None
    ):
        return "estimated_notional_invalid"
    entry_key = next(
        (key for key in ("limit_price", "entry_price", "price") if key in candidate),
        None,
    )
    entry = (
        _strict_bounded_number(candidate.get(entry_key), _MAX_EXECUTION_PRICE)
        if entry_key else None
    )
    aliases: list[float] = []
    for key in ("limit_price", "entry_price", "price", "current_price"):
        if key in candidate:
            alias = _strict_bounded_number(candidate.get(key), _MAX_EXECUTION_PRICE)
            if alias is not None:
                aliases.append(alias)
    if (
        entry is not None
        and aliases
        and max(aliases) / min(aliases) > _MAX_PRICE_ALIAS_RATIO
    ):
        return "entry_price_invalid"
    target = (
        _strict_bounded_number(candidate.get("target_price"), _MAX_EXECUTION_PRICE)
        if "target_price" in candidate else None
    )
    stop = (
        _strict_bounded_number(candidate.get("stop_loss"), _MAX_EXECUTION_PRICE)
        if "stop_loss" in candidate else None
    )
    if entry is not None and target is not None and target <= entry:
        return "target_price_invalid"
    if (
        entry is not None
        and target is not None
        and target > entry * _MAX_TARGET_PRICE_RATIO
    ):
        return "target_price_invalid"
    if entry is not None and stop is not None and stop >= entry:
        return "stop_loss_invalid"
    is_us = market == "US" or (
        symbol and not symbol.endswith((".KS", ".KQ")) and not symbol.isdigit()
    )
    if is_us and "fx_usdkrw" in candidate and _strict_bounded_number(
        candidate.get("fx_usdkrw"), _MAX_FX_USDKRW
    ) is None:
        return "fx_usdkrw_invalid"
    return ""


def quarantine_explicit_toss_input(candidate: dict, reason: str) -> None:
    """오염 후보를 items에 보존하되 후속 normalization 입력은 제거한다."""
    candidate["upstream_input_validation_error"] = reason
    for field in _EXECUTION_VALUE_FIELDS:
        candidate[field] = None
    if reason == "side_invalid":
        candidate["side"] = None
    if reason == "market_invalid":
        symbol = str(candidate.get("symbol") or candidate.get("ticker") or "").upper()
        candidate["market"] = "KR" if symbol.endswith((".KS", ".KQ")) or symbol.isdigit() else "US"
    candidate["stock_agent_ready"] = False
    candidate["executable_now"] = False
    candidate["execution_status"] = "input_validation_blocked"
    candidate["block_reason"] = reason


def _without_nonfinite(value):
    if isinstance(value, dict):
        return {key: _without_nonfinite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_nonfinite(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_without_nonfinite(item) for item in value)
    if type(value) in (int, float):
        try:
            return value if math.isfinite(float(value)) else None
        except (TypeError, ValueError, OverflowError):
            return None
    return value


def validate_executable_income_contract(income: Mapping | None) -> tuple[bool, str]:
    """BUY 실행/funding이 신뢰할 수 있는 v2 decision 계약인지 검증한다."""
    if not isinstance(income, Mapping):
        return False, "income_contract_missing"
    if income.get("income_pass") is not True:
        return False, "income_contract_not_passed"
    contract = (
        income.get("decision_expected_pnl_model"),
        income.get("decision_expected_pnl_scope"),
    )
    if contract not in _EXECUTABLE_DECISION_CONTRACTS:
        return False, "income_contract_model_scope_invalid"
    expected = _strict_finite_number(
        income.get("decision_expected_pnl_krw"),
        positive=True,
    )
    edge = _strict_finite_number(
        income.get("decision_income_edge_ratio"),
        positive=True,
    )
    if expected is None or edge is None:
        return False, "income_contract_metrics_invalid"
    return True, ""


def _symbol_keys(symbol: str) -> set[str]:
    sym = str(symbol or "").upper().strip()
    keys = {sym} if sym else set()
    if sym.endswith((".KS", ".KQ")):
        keys.add(sym.split(".", 1)[0])
    elif sym.isdigit() and len(sym) == 6:
        keys.add(f"{sym}.KS")
        keys.add(f"{sym}.KQ")
    return {k for k in keys if k}


def _lookup_symbol_map(data, symbol: str):
    if not data:
        return None
    keys = _symbol_keys(symbol)
    if isinstance(data, Mapping):
        for k in keys:
            if k in data:
                return data[k]
        return None
    if isinstance(data, (set, list, tuple)):
        for row in data:
            if isinstance(row, str):
                if row.upper().strip() in keys:
                    return row
            elif isinstance(row, Mapping):
                rkeys = _symbol_keys(row.get("symbol") or row.get("ticker") or "")
                if keys & rkeys:
                    return row
    return None


def _has_same_symbol_pending(pending_orders, symbol: str) -> bool:
    hit = _lookup_symbol_map(pending_orders, symbol)
    if hit is None:
        return False
    if not isinstance(hit, Mapping):
        return True
    side = str(hit.get("side") or "buy").lower()
    status = str(hit.get("status") or hit.get("broker_status") or "pending").lower()
    terminal = {"filled", "cancelled", "canceled", "rejected", "failed", "live_send_failed", "blocked"}
    return side == "buy" and status not in terminal


def estimate_win_prob(candidate: Mapping, reliability_stats=None) -> float:
    """후보의 보수적 성공확률 추정.

    표본 3건 미만 신뢰도는 0%로 감점하지 않고 score 기반 기본값을 사용한다.
    """
    symbol = str(candidate.get("symbol") or candidate.get("ticker") or "").upper().strip()
    try:
        from core.toss_quality_gate import has_canonical_quality_authority

        has_quality_authority = has_canonical_quality_authority(candidate)
    except Exception:
        has_quality_authority = "quality_score_authority" in candidate
    if has_quality_authority:
        try:
            from core.toss_quality_gate import canonical_quality_score

            score, _ = canonical_quality_score(candidate)
        except Exception:
            score = None
        if score is None:
            return 0.0
    else:
        score = _num(candidate.get("score"), 65.0)
    # score 60=0.52, 80=0.62 근처. 과신 방지로 상한 제한.
    prob = 0.52 + max(min(score - 60.0, 30.0), -20.0) * 0.005

    bucket = str(candidate.get("decision_bucket") or "").upper()
    if bucket == "PASS_EXECUTE":
        prob += 0.025
    elif bucket == "SMALL_PASS":
        prob += 0.010

    stats = _lookup_symbol_map(reliability_stats, symbol)
    if isinstance(stats, Mapping):
        count = int(_num(stats.get("count") or stats.get("n") or stats.get("samples"), 0))
        raw = None
        for key in ("win_rate", "hit_rate", "success_rate", "accuracy"):
            if key in stats:
                raw = _num(stats.get(key), 0.0)
                break
        if raw is not None and count >= 3:
            if raw > 1.0:
                raw = raw / 100.0
            # 관측값은 반영하되 과적합 방지: 40~72% 범위만 사용
            prob = 0.45 * prob + 0.55 * max(0.40, min(raw, 0.72))

    return round(max(0.42, min(prob, 0.72)), 4)



def prepare_income_buy_plan(candidate: Mapping) -> dict:
    """BUY 후보에 income 전용 exit plan을 부여한다.

    기존 발굴 후보는 보통 -6% stop을 갖고 있어 income gate(최대 4.5%)에서
    전부 막힌다. 원본 stop은 보존하고, 자동 수입형 BUY에 실제로 적용할
    stop_loss를 4.2% 이내로 당겨 risk_reward를 재계산한다.
    """
    out = dict(candidate or {})
    side = str(out.get("side") or "buy").lower()
    if side != "buy":
        return out

    entry = _num(out.get("limit_price") or out.get("entry_price") or out.get("price"), 0.0)
    target = _num(out.get("target_price"), 0.0)
    stop = _num(out.get("stop_loss"), 0.0)
    if entry <= 0:
        return out

    original_stop = stop
    original_target = target
    stop_risk_pct = round(max((entry - stop) / entry * 100.0, 0.0), 4) if stop > 0 else None

    if stop <= 0 or stop_risk_pct is None or stop_risk_pct > _MAX_STOP_RISK_PCT:
        stop = round(entry * (1.0 - _INCOME_PLAN_STOP_RISK_PCT / 100.0), 4)
        out["original_stop_loss"] = original_stop if original_stop else None
        out["stop_loss"] = stop
        stop_risk_pct = round(max((entry - stop) / entry * 100.0, 0.0), 4)

    if target > entry and stop < entry:
        rr = round((target - entry) / (entry - stop), 4)
        out["risk_reward"] = rr
    else:
        rr = _num(out.get("risk_reward"), 0.0)

    if original_target and "original_target_price" not in out:
        out.setdefault("original_target_price", original_target)

    out["income_exit_plan"] = {
        "version": "income_v2_full_exit_plan",
        "entry_price": entry,
        "target_price": target or None,
        "stop_loss": stop or None,
        "original_stop_loss": original_stop or None,
        "original_target_price": original_target or None,
        "stop_risk_pct": stop_risk_pct,
        "risk_reward": rr,
        "take_profit_pct": _INCOME_TAKE_PROFIT_SINGLE_PCT,
        "profit_exit_mode": "full_position",
        "early_stop_loss_pct": _INCOME_EARLY_STOP_LOSS_PCT,
        "hard_stop_loss_pct": _INCOME_HARD_STOP_LOSS_PCT,
        "note": "income v2는 수량과 무관하게 +1.5% 전량익절/-2.5% 전량손절 lifecycle EV를 사용",
    }
    return out


def _normalize_trade_symbol(symbol: str) -> str:
    sym = str(symbol or "").upper().strip()
    if sym.isdigit() and len(sym) == 6:
        return f"{sym}.KS"
    return sym


def canonical_trade_identity(symbol: object) -> tuple[str, str, str] | None:
    """Funding 권한용 canonical (symbol, market, currency) identity."""
    if type(symbol) is not str:
        return None
    normalized = _normalize_trade_symbol(symbol)
    if not normalized:
        return None
    is_kr = normalized.endswith((".KS", ".KQ"))
    return normalized, "KR" if is_kr else "US", "KRW" if is_kr else "USD"


def _holding_to_rebalance_row(item: Mapping, fx_usdkrw: float = 0.0) -> dict | None:
    identity = canonical_trade_identity(item.get("symbol"))
    if identity is None:
        return None
    symbol, _, expected_currency = identity
    raw = item["symbol"].upper().strip()
    qty = int(_num(item.get("quantity"), 0))
    last = _num(item.get("lastPrice") or item.get("last_price"), 0.0)
    if qty <= 0 or last <= 0:
        return None
    mv = item.get("marketValue") or {}
    pl = item.get("profitLoss") or {}
    daily = item.get("dailyProfitLoss") or {}
    purchase = _num(mv.get("purchaseAmount"), 0.0)
    amount = _num(mv.get("amount"), 0.0) or qty * last
    pl_amount = _num(pl.get("amountAfterCost", pl.get("amount")), 0.0)
    daily_amount = _num(daily.get("amount"), 0.0)
    pl_pct = (pl_amount / purchase * 100.0) if purchase > 0 else 0.0
    daily_pct = (daily_amount / purchase * 100.0) if purchase > 0 else 0.0
    raw_currency = item.get("currency")
    if type(raw_currency) is not str or raw_currency != expected_currency:
        return None
    currency = raw_currency
    # 손실률/당일손실/절대손실을 함께 반영. 낮을수록 약한 보유분.
    weakness_score = (-pl_pct * 2.0) + (-daily_pct * 1.0) + max(-pl_amount, 0.0) / 100_000.0

    if currency == "KRW":
        market_value_krw = round(amount, 2)
        estimated_release_usd = None
        estimated_release_krw = round(qty * last, 2)
        release_currency = "KRW"
        fx_rate_used = None
        valuation_warning = None
    else:
        # USD 보유분 — 환율 없이 0원으로 조용히 처리하지 않는다 (missing_usdkrw)
        estimated_release_usd = round(qty * last, 4)
        release_currency = "USD"
        if fx_usdkrw > 0:
            market_value_krw = round(amount * fx_usdkrw, 2)
            estimated_release_krw = round(estimated_release_usd * fx_usdkrw, 2)
            fx_rate_used = round(fx_usdkrw, 4)
            valuation_warning = None
        else:
            market_value_krw = None
            estimated_release_krw = None
            fx_rate_used = None
            valuation_warning = "missing_usdkrw"

    return {
        "symbol": symbol,
        "raw_symbol": raw,
        "side": "sell",
        "name": str(item.get("name") or symbol),
        "currency": currency,
        "quantity": qty,
        "last_price": round(last, 4),
        "market_value_krw": market_value_krw,
        "estimated_release_usd": estimated_release_usd,
        "estimated_release_krw": estimated_release_krw,
        "release_currency": release_currency,
        "fx_rate_used": fx_rate_used,
        "valuation_warning": valuation_warning,
        "purchase_amount_krw": round(purchase, 2) if currency == "KRW" else None,
        "pl_amount_krw": round(pl_amount, 2) if currency == "KRW" else None,
        "pl_pct": round(pl_pct, 4),
        "daily_pl_amount_krw": round(daily_amount, 2) if currency == "KRW" else None,
        "daily_pl_pct": round(daily_pct, 4),
        "weakness_score": round(weakness_score, 4),
        "action": "sell_to_fund_candidate",
        "read_only": True,
        "reason": "보유 과다/현금 부족 시 income 후보 매수 전 정리 검토 대상",
    }


def _income_waitlist_row(item: Mapping, fx_usdkrw: float = 0.0) -> dict | None:
    income = item.get("income_strategy")
    raw_side = item.get("side")
    if raw_side != "buy":
        return None
    raw_status = item.get("status", item.get("broker_status"))
    if raw_status is not None:
        if type(raw_status) is not str:
            return None
        if raw_status.lower().strip() in _NONTERMINAL_BUY_STATUSES:
            return None
    raw_symbol = item.get("symbol") if "symbol" in item else item.get("ticker")
    identity = canonical_trade_identity(raw_symbol)
    if identity is None:
        return None
    symbol, symbol_market, expected_currency = identity
    if not isinstance(income, Mapping):
        return None
    contract_ok, _ = validate_executable_income_contract(income)
    if not contract_ok:
        return None
    decision_expected_value = float(income["decision_expected_pnl_krw"])
    decision_edge_value = float(income["decision_income_edge_ratio"])
    decision_model = str(income["decision_expected_pnl_model"])
    decision_scope = str(income["decision_expected_pnl_scope"])
    raw_market = item.get("market")
    if raw_market is None:
        market = symbol_market
    elif type(raw_market) is str and raw_market in {"KR", "US"}:
        market = raw_market
    else:
        return None
    if market != symbol_market:
        return None
    if "currency" in item:
        raw_currency = item.get("currency")
        if type(raw_currency) is not str or raw_currency != expected_currency:
            return None
    currency = expected_currency
    if currency == "USD":
        native = _num(item.get("estimated_amount_usd"), 0.0)
        if native <= 0 and fx_usdkrw > 0:
            native = round(_num(item.get("estimated_amount_krw"), 0.0) / fx_usdkrw, 4)
    else:
        native = _num(item.get("estimated_amount_krw"), 0.0)
    return {
        "symbol": symbol,
        "side": "buy",
        "name": item.get("name") or symbol,
        "market": market,
        "currency": currency,
        "estimated_amount_krw": round(_num(item.get("estimated_amount_krw"), 0.0), 2),
        "estimated_amount_native": round(native, 4) if native else 0.0,
        "expected_pnl_krw": income.get("expected_pnl_krw"),
        "income_edge_ratio": income.get("income_edge_ratio"),
        "decision_expected_pnl_krw": decision_expected_value,
        "decision_income_edge_ratio": decision_edge_value,
        "decision_expected_pnl_model": decision_model,
        "decision_expected_pnl_scope": decision_scope,
        "income_pass": True,
        "risk_reward": item.get("risk_reward"),
        "execution_status": item.get("execution_status"),
        "block_reason": item.get("block_reason"),
        "stock_agent_ready": item.get("stock_agent_ready") is True,
    }


_FUNDING_MODE = "currency_income_replacement"
_NONTERMINAL_BUY_STATUSES = frozenset({
    "pending", "new", "open", "working", "submitted", "accepted",
    "partially_filled", "previewed", "approved", "live_sent",
    "live_send_retryable",
})


def _row_release_native(row: Mapping) -> float:
    """매도 row의 실제 주문 수량×가격 기준 gross 확보 예상액."""
    quantity = row.get("quantity")
    last_price = _strict_bounded_number(row.get("last_price"), _MAX_EXECUTION_PRICE)
    if (
        type(quantity) is not int
        or not 0 < quantity <= _MAX_EXECUTION_QUANTITY
        or last_price is None
    ):
        return 0.0
    release = quantity * last_price
    return round(release, 4) if math.isfinite(release) else 0.0


def _compute_funding_plan(
    waitlist: list[dict],
    cash: Mapping,
    fx_usdkrw: float,
    merged_rows: list[dict],
) -> dict:
    """통화별 income 자금조달 매도 계획.

    "팔고 나서 못 사는" 흐름을 금지한다: 같은 통화 현금 + AI Berkshire
    eligible 보유분 매도 예상액으로 income_pass 후보를 **전액** 매수할 수
    있을 때만 funding_rebalance_required=true. 현재 gap을 단독 충당하는
    매도 row 하나에만 funding 필드를 표시한다 (in-place). Read-only 계산.
    """
    cash_by_ccy = {
        "KRW": _num(cash.get("krw_native", cash.get("krw")), 0.0),
        "USD": _num(cash.get("usd"), 0.0),
    }
    eligible_by_ccy: dict[str, list[dict]] = {"KRW": [], "USD": []}
    for r in merged_rows:
        if r.get("auto_sell_eligible") is not True:
            continue
        native = _row_release_native(r)
        if native <= 0:
            continue
        ccy = str(r.get("currency") or "KRW").upper()
        eligible_by_ccy.setdefault(ccy, []).append(r)

    empty = {"required": False, "currency": None, "target": None,
             "source_symbol": None,
             "available_cash_native": None, "gap_native": None,
             "gap_krw": None, "rows": []}

    best = None
    for w in waitlist:
        ccy = str(w.get("currency") or "KRW").upper()
        need = _num(w.get("estimated_amount_native"), 0.0)
        if need <= 0:
            continue
        avail = cash_by_ccy.get(ccy, 0.0)
        gap = need - avail
        if gap <= 0:
            continue  # 현금으로 전액 매수 가능 — funding 매도 불필요
        covering_rows = [
            row for row in eligible_by_ccy.get(ccy, [])
            if _row_release_native(row) >= gap
        ]
        if not covering_rows:
            continue  # 미체결 다중 매도의 누적액은 funding 권한으로 쓰지 않는다.
        covering_row = min(
            covering_rows,
            key=lambda row: (
                -_num(row.get("adjusted_sell_priority"), 0.0),
                str(row.get("symbol") or ""),
            ),
        )
        if best is None or _num(w.get("decision_expected_pnl_krw"), 0.0) > _num(
            best["target"].get("decision_expected_pnl_krw"), 0.0
        ):
            best = {
                "target": w,
                "currency": ccy,
                "gap": gap,
                "avail": avail,
                "row": covering_row,
            }

    if best is None:
        return empty

    ccy = best["currency"]
    gap = round(best["gap"], 4)
    target = best["target"]
    gap_krw = round(gap * fx_usdkrw, 2) if ccy == "USD" and fx_usdkrw > 0 else (
        round(gap, 2) if ccy == "KRW" else None)

    # 현재 gap을 단독으로 충당할 수 있는 한 row만 자동 SELL 권한을 가진다.
    row = best["row"]
    native = _row_release_native(row)
    row["funding_mode"] = _FUNDING_MODE
    row["funding_currency"] = ccy
    row["funding_target_symbol"] = target["symbol"]
    row["funding_gap_native"] = gap
    row["estimated_release_native"] = native
    row["cumulative_release_native"] = native
    row["covers_funding_target"] = True
    rows = [row]

    return {
        "required": True,
        "currency": ccy,
        "source_symbol": row["symbol"],
        "target": {
            "symbol": target["symbol"],
            "side": target.get("side"),
            "currency": target.get("currency"),
            "name": target.get("name"),
            "estimated_amount_native": target.get("estimated_amount_native"),
            "expected_pnl_krw": target.get("expected_pnl_krw"),
            "decision_expected_pnl_krw": target.get("decision_expected_pnl_krw"),
            "decision_income_edge_ratio": target.get("decision_income_edge_ratio"),
            "decision_expected_pnl_model": target.get("decision_expected_pnl_model"),
            "decision_expected_pnl_scope": target.get("decision_expected_pnl_scope"),
            "income_pass": target.get("income_pass"),
        },
        "available_cash_native": round(best["avail"], 4),
        "gap_native": gap,
        "gap_krw": gap_krw,
        "rows": rows,
    }


def build_rebalance_plan(
    account_summary: Mapping | None,
    income_candidates: Iterable[Mapping] | None,
    *,
    target_holding_count: int = 12,
    max_sell_candidates: int = 8,
    max_income_waitlist: int = 8,
    berkshire_scores: Mapping | None = None,
) -> dict:
    """보유 과다 상태에서 sell_to_fund 후보와 income BUY 대기열을 만든다.

    Read-only 계획만 반환한다. 주문/취소/정정 호출은 하지 않는다.
    """
    account_summary = account_summary or {}
    holdings = list(account_summary.get("holdings_items") or [])
    fx_usdkrw = _num((account_summary.get("exchange_rate") or {}).get("rate"), 0.0)
    holdings_count = int(_num(account_summary.get("holdings_count"), len(holdings)))
    target_holding_count = max(1, int(target_holding_count))
    rebalance_required = holdings_count > 20
    reduce_by = max(0, holdings_count - target_holding_count) if rebalance_required else 0

    cash = account_summary.get("cash") or {}
    available_krw = _num(cash.get("krw_native", cash.get("krw")), 0.0)

    # funding 계산용 전체 목록과 표시용 상위 N개를 분리한다.
    # 상위 8개로 먼저 자르면 "기대손익은 낮지만 매도액으로 전액 매수 가능한"
    # 후순위 후보가 funding target 계산에서 사라져 불필요하게 funding=false가 된다.
    funding_waitlist = []
    for item in list(income_candidates or []):
        row = _income_waitlist_row(item, fx_usdkrw=fx_usdkrw)
        if row:
            funding_waitlist.append(row)
    funding_waitlist.sort(
        key=lambda r: (
            _num(r.get("decision_expected_pnl_krw"), 0.0),
            _num(r.get("decision_income_edge_ratio"), 0.0),
        ),
        reverse=True,
    )
    waitlist = funding_waitlist[:max_income_waitlist]

    required_for_first = _num(waitlist[0].get("estimated_amount_krw"), 0.0) if waitlist else 0.0
    funding_gap = max(required_for_first - available_krw, 0.0)

    all_rows = []
    for h in holdings:
        row = _holding_to_rebalance_row(h, fx_usdkrw=fx_usdkrw)
        if row:
            all_rows.append(row)
    # AI Berkshire 판정 병합 + adjusted_sell_priority 내림차순 정렬 (fail-closed)
    from core.ai_berkshire_toss import apply_berkshire_to_sell_to_fund
    all_rows = apply_berkshire_to_sell_to_fund(all_rows, scores=berkshire_scores)

    # 통화별 자금조달 매도: "한 번 팔면 전액 매수 가능"한 row 하나만 표시.
    # 반드시 표시용 절단 전의 전체 income_pass 목록으로 계산한다.
    funding = _compute_funding_plan(funding_waitlist, cash, fx_usdkrw, all_rows)

    if rebalance_required:
        sell_rows = all_rows[:max_sell_candidates]
        seen = {r.get("symbol") for r in sell_rows}
        sell_rows += [r for r in funding["rows"] if r.get("symbol") not in seen]
    elif funding["required"]:
        sell_rows = list(funding["rows"])
    elif funding_gap > 0:
        sell_rows = all_rows[:max_sell_candidates]
    else:
        sell_rows = []

    cumulative = 0.0
    for row in sell_rows:
        cumulative += _num(row.get("estimated_release_krw"), 0.0)
        row["cumulative_release_krw"] = round(cumulative, 2)
        row["covers_first_income_candidate"] = bool(funding_gap and cumulative >= funding_gap)

    return {
        "version": "income_rebalance_v1",
        "read_only": True,
        "portfolio_rebalance_required": bool(rebalance_required),
        "holdings_count": holdings_count,
        "target_holding_count": target_holding_count,
        "reduce_positions_by": reduce_by,
        "available_cash_krw": round(available_krw, 2),
        "income_buy_waitlist": waitlist,
        "sell_to_fund_candidates": sell_rows,
        # funding 미발동 시에는 기존 의미(1순위 후보 KRW 부족분) 유지
        "funding_gap_krw": funding["gap_krw"] if funding["required"] else round(funding_gap, 2),
        "funding_rebalance_required": funding["required"],
        "funding_currency": funding["currency"],
        "funding_target": funding["target"],
        "funding_source_symbol": funding["source_symbol"],
        "available_cash_native": funding["available_cash_native"],
        "funding_gap_native": funding["gap_native"],
        "note": "GET-only 리밸런싱 계획. 실제 매도/매수 주문은 생성하지 않음",
    }

def compute_income_edge(
    candidate: Mapping,
    *,
    account: Mapping | None = None,
    pending_orders: Iterable | Mapping | None = None,
    recent_risk_sells: Iterable | Mapping | None = None,
    reliability_stats: Mapping | None = None,
    exit_model: str | None = None,
) -> dict:
    """BUY 후보의 수입 기대값과 실행 가능 여부를 계산한다."""
    symbol = str(candidate.get("symbol") or candidate.get("ticker") or "").upper().strip()
    side = str(candidate.get("side") or "buy").lower()

    if exit_model is not None:
        raw_requested_model = exit_model
    elif "income_exit_model" in candidate:
        raw_requested_model = candidate.get("income_exit_model")
    else:
        raw_requested_model = "research_target_v1"
    if raw_requested_model is None:
        raw_requested_model = "research_target_v1"
    requested_model = str(raw_requested_model).strip().lower()
    valid_models = {"research_target_v1", "toss_position_review_v2"}
    invalid_exit_model = requested_model not in valid_models
    use_toss_exit_cashflow = requested_model == "toss_position_review_v2" or invalid_exit_model

    asset_type = str(candidate.get("asset_type") or "").upper()
    market = str(candidate.get("market") or "").upper()
    is_us = asset_type == "US_STOCK" or market == "US" or (
        symbol
        and not symbol.endswith((".KS", ".KQ"))
        and not symbol.isdigit()
    )
    raw_entry = (
        candidate.get("limit_price")
        if "limit_price" in candidate
        else candidate.get("entry_price")
        if "entry_price" in candidate
        else candidate.get("price")
    )
    strict_toss_contract = requested_model == "toss_position_review_v2"
    toss_input_error = ""

    if strict_toss_contract:
        raw_qty = candidate.get("quantity")
        upstream_error = str(candidate.get("upstream_input_validation_error") or "")
        has_quality_authority = False
        if upstream_error in _EXECUTION_INPUT_ERRORS:
            toss_input_error = upstream_error
        else:
            try:
                from core.toss_quality_gate import has_canonical_quality_authority

                has_quality_authority = has_canonical_quality_authority(candidate)
            except Exception:
                has_quality_authority = "quality_score_authority" in candidate
        if not toss_input_error and has_quality_authority:
            try:
                from core.toss_quality_gate import canonical_quality_score

                canonical_score, _ = canonical_quality_score(candidate)
            except Exception:
                canonical_score = None
            if canonical_score is None:
                toss_input_error = "canonical_quality_score_invalid"
        elif not toss_input_error and (explicit_error := detect_explicit_toss_input_error(candidate)):
            toss_input_error = explicit_error
        if type(raw_qty) is int and 0 < raw_qty <= _MAX_EXECUTION_QUANTITY:
            qty = raw_qty
        else:
            qty = 0
            if not toss_input_error:
                toss_input_error = "quantity_invalid"

        strict_entry = _strict_bounded_number(raw_entry, _MAX_EXECUTION_PRICE)
        strict_target = _strict_bounded_number(
            candidate.get("target_price"), _MAX_EXECUTION_PRICE
        )
        strict_stop = _strict_bounded_number(
            candidate.get("stop_loss"), _MAX_EXECUTION_PRICE
        )
        strict_rr = _strict_bounded_number(
            candidate.get("risk_reward"), _MAX_RISK_REWARD
        )
        strict_score = _strict_finite_number(candidate.get("score"))
        strict_reported = _strict_bounded_number(
            candidate.get("estimated_amount_krw"),
            _MAX_REPORTED_NOTIONAL,
        )
        entry = strict_entry or 0.0
        target = strict_target or 0.0
        stop = strict_stop or 0.0
        rr = strict_rr or 0.0
        reported_estimated = strict_reported or 0.0
        if not toss_input_error and strict_entry is None:
            toss_input_error = "entry_price_invalid"
        elif not toss_input_error and strict_target is None:
            toss_input_error = "target_price_invalid"
        elif (
            not toss_input_error
            and strict_target is not None
            and strict_entry is not None
            and strict_target <= strict_entry
        ):
            toss_input_error = "target_price_invalid"
        elif (
            not toss_input_error
            and strict_target is not None
            and strict_entry is not None
            and strict_target > strict_entry * _MAX_TARGET_PRICE_RATIO
        ):
            toss_input_error = "target_price_invalid"
        elif not toss_input_error and strict_stop is None:
            toss_input_error = "stop_loss_invalid"
        elif (
            not toss_input_error
            and strict_stop is not None
            and strict_entry is not None
            and strict_stop >= strict_entry
        ):
            toss_input_error = "stop_loss_invalid"
        elif not toss_input_error and strict_rr is None:
            toss_input_error = "risk_reward_invalid"
        elif not toss_input_error and (
            strict_score is None or not 0.0 < strict_score <= 100.0
        ):
            toss_input_error = "score_invalid"
        elif not toss_input_error and strict_reported is None:
            toss_input_error = "estimated_notional_invalid"

        if is_us:
            if "fx_usdkrw" in candidate:
                strict_fx = _strict_bounded_number(
                    candidate.get("fx_usdkrw"), _MAX_FX_USDKRW
                )
            else:
                strict_fx = _DEFAULT_FX_USDKRW
            fx = strict_fx or 0.0
            if not toss_input_error and strict_fx is None:
                toss_input_error = "fx_usdkrw_invalid"
        else:
            fx = 1.0
        multiplier = fx if is_us else 1.0
        canonical_notional = entry * qty * multiplier if entry > 0 and qty > 0 and multiplier > 0 else 0.0
        if canonical_notional and not math.isfinite(canonical_notional):
            canonical_notional = 0.0
            if not toss_input_error:
                toss_input_error = "canonical_notional_invalid"
        estimated = canonical_notional
    else:
        qty = max(0, int(_num(candidate.get("quantity"), 0)))
        entry = _num(raw_entry, 0.0)
        target = _num(candidate.get("target_price"), 0.0)
        stop = _num(candidate.get("stop_loss"), 0.0)
        rr = _num(candidate.get("risk_reward"), 0.0)
        fx = _num(candidate.get("fx_usdkrw"), _DEFAULT_FX_USDKRW if is_us else 1.0)
        multiplier = fx if is_us else 1.0
        reported_estimated = _num(candidate.get("estimated_amount_krw"), 0.0)
        canonical_notional = entry * qty * multiplier if entry > 0 and qty > 0 else 0.0
        estimated = canonical_notional if canonical_notional > 0 else reported_estimated

    notional_tolerance = max(1.0, canonical_notional * 0.005) if canonical_notional > 0 else 0.0
    notional_mismatch = bool(
        not toss_input_error
        and reported_estimated > 0
        and canonical_notional > 0
        and abs(reported_estimated - canonical_notional) > notional_tolerance
    )

    stop_risk_pct = round(max((entry - stop) / entry * 100.0, 0.0), 4) if entry > 0 and stop > 0 else None
    research_target_upside_krw = (
        max(target - entry, 0.0) * qty * multiplier
        if target > 0 and entry > 0 and qty > 0 else 0.0
    )
    research_stop_loss_krw = (
        max(entry - stop, 0.0) * qty * multiplier
        if stop > 0 and entry > 0 and qty > 0 else 0.0
    )
    if use_toss_exit_cashflow:
        # Toss position review의 다음 1회 실현 이벤트 현금흐름은 관측값으로
        # 보존한다. 실행 판정은 반복 분할 청산을 반영한 별도 lifecycle EV를
        # 사용해 "다음 절반 이익 vs 전량 손실"의 구조적 영구 차단을 피한다.
        version = "income_v2"
        expected_pnl_model = (
            "invalid_exit_model_fail_closed" if invalid_exit_model
            else "income_exit_cashflow_v2"
        )
        profit_exit_quantity = qty
        loss_exit_quantity = qty
        expected_pnl_scope = "next_realized_exit_only"
        residual_quantity_after_profit = 0
        residual_mark_to_market_included = False
        profit_exit_pct = _INCOME_TAKE_PROFIT_SINGLE_PCT
        loss_exit_pct = abs(_INCOME_EARLY_STOP_LOSS_PCT)
        upside_krw = entry * profit_exit_quantity * (profit_exit_pct / 100.0) * multiplier
        loss_krw = entry * loss_exit_quantity * (loss_exit_pct / 100.0) * multiplier

        multi_share_lifecycle_unmodeled = False
        if invalid_exit_model:
            decision_expected_pnl_model = "invalid_exit_model_fail_closed"
            decision_expected_pnl_scope = "invalid_exit_model"
            decision_profit_exit_quantity = None
            decision_loss_exit_quantity = None
            decision_residual_quantity_after_profit = None
            decision_residual_mark_to_market_included = False
            decision_upside_krw = None
            decision_loss_krw = None
        else:
            decision_expected_pnl_model = (
                "income_exit_lifecycle_v2" if qty > 1 else "income_exit_lifecycle_v1"
            )
            decision_expected_pnl_scope = "full_position_threshold_exit"
            decision_profit_exit_quantity = qty
            decision_loss_exit_quantity = qty
            decision_residual_quantity_after_profit = 0
            decision_residual_mark_to_market_included = False
            decision_upside_krw = entry * qty * (profit_exit_pct / 100.0) * multiplier
            decision_loss_krw = loss_krw
    else:
        # 비-Toss 수동 티켓은 기존 연구 목표/손절 계약을 유지한다.
        version = "income_v1"
        expected_pnl_model = "research_target_v1"
        profit_exit_quantity = qty
        loss_exit_quantity = qty
        expected_pnl_scope = "research_target_full_position"
        residual_quantity_after_profit = 0
        residual_mark_to_market_included = True
        profit_exit_pct = max((target - entry) / entry * 100.0, 0.0) if entry > 0 else 0.0
        loss_exit_pct = stop_risk_pct or 0.0
        upside_krw = research_target_upside_krw
        loss_krw = research_stop_loss_krw

        decision_expected_pnl_model = expected_pnl_model
        decision_expected_pnl_scope = expected_pnl_scope
        decision_profit_exit_quantity = profit_exit_quantity
        decision_loss_exit_quantity = loss_exit_quantity
        decision_residual_quantity_after_profit = residual_quantity_after_profit
        decision_residual_mark_to_market_included = residual_mark_to_market_included
        decision_upside_krw = upside_krw
        decision_loss_krw = loss_krw
        multi_share_lifecycle_unmodeled = False

    win_prob = estimate_win_prob(candidate, reliability_stats=reliability_stats)
    fee_slippage = max(1_000.0, estimated * 0.0015) if estimated > 0 else 1_000.0
    if strict_toss_contract and toss_input_error:
        expected_pnl_model = "invalid_income_inputs_fail_closed"
        expected_pnl_scope = "invalid_income_inputs"
        expected = None
        edge_ratio = None
        breakeven_win_rate = None
        breakeven_reachable = False
        decision_expected_pnl_model = "invalid_income_inputs_fail_closed"
        decision_expected_pnl_scope = "invalid_income_inputs"
        decision_upside_krw = None
        decision_loss_krw = None
        decision_expected = None
        decision_edge_ratio = None
        decision_breakeven_win_rate = None
        decision_breakeven_reachable = False
    else:
        expected = win_prob * upside_krw - (1.0 - win_prob) * loss_krw - fee_slippage
        edge_ratio = expected / estimated if estimated > 0 else 0.0
        breakeven_denominator = upside_krw + loss_krw
        if breakeven_denominator > 0:
            breakeven_win_rate = (loss_krw + fee_slippage) / breakeven_denominator
            breakeven_reachable = breakeven_win_rate <= 1.0
        else:
            breakeven_win_rate = None
            breakeven_reachable = False

        if decision_upside_krw is None or decision_loss_krw is None:
            decision_expected = None
            decision_edge_ratio = None
            decision_breakeven_win_rate = None
            decision_breakeven_reachable = False
        else:
            decision_expected = (
                win_prob * decision_upside_krw
                - (1.0 - win_prob) * decision_loss_krw
                - fee_slippage
            )
            decision_edge_ratio = decision_expected / estimated if estimated > 0 else 0.0
            decision_breakeven_denominator = decision_upside_krw + decision_loss_krw
            if decision_breakeven_denominator > 0:
                decision_breakeven_win_rate = (
                    decision_loss_krw + fee_slippage
                ) / decision_breakeven_denominator
                decision_breakeven_reachable = decision_breakeven_win_rate <= 1.0
            else:
                decision_breakeven_win_rate = None
                decision_breakeven_reachable = False

    block_reason = ""
    block_label = ""
    if invalid_exit_model:
        income_pass = False
        grade = "BLOCK"
        block_reason = "invalid_income_exit_model"
        block_label = "지원하지 않는 수입 청산 모델 — fail-closed"
    elif toss_input_error:
        income_pass = False
        grade = "BLOCK"
        block_reason = toss_input_error
        block_label = f"수입 실행 입력 검증 실패: {toss_input_error}"
    elif side != "buy":
        income_pass = True
        grade = "INCOME_PASS"
    elif not symbol or qty <= 0 or entry <= 0 or target <= 0 or stop <= 0:
        income_pass = False
        grade = "BLOCK"
        block_reason = "missing_income_inputs"
        block_label = "수입 기대값 계산 필수값 부족"
    elif notional_mismatch:
        income_pass = False
        grade = "BLOCK"
        block_reason = "estimated_notional_mismatch"
        block_label = "보고 주문금액과 가격×수량 기준금액 불일치"
    elif pending_orders is None:
        # pending 상태 조회 실패 = '모름' — 중복 주문 방지 위해 차단 (fail-closed)
        income_pass = False
        grade = "BLOCK"
        block_reason = "pending_state_unavailable"
        block_label = "PENDING 주문 상태 확인 불가 — 중복 방지 차단"
    elif _has_same_symbol_pending(pending_orders, symbol):
        income_pass = False
        grade = "BLOCK"
        block_reason = "same_symbol_pending"
        block_label = "같은 종목 PENDING 주문 존재"
    elif _lookup_symbol_map(recent_risk_sells, symbol) is not None:
        income_pass = False
        grade = "BLOCK"
        block_reason = "recent_risk_sell_cooldown"
        block_label = "최근 리스크 매도 종목 재진입 cooldown"
    elif rr < _MIN_RISK_REWARD:
        income_pass = False
        grade = "BLOCK"
        block_reason = "risk_reward_below_1.5"
        block_label = "손익비 1.5 미만"
    elif stop_risk_pct is None or stop_risk_pct > _MAX_STOP_RISK_PCT:
        income_pass = False
        grade = "BLOCK"
        block_reason = "stop_risk_pct_above_4.5"
        block_label = "손절폭 4.5% 초과"
    else:
        min_expected = max(_MIN_EXPECTED_KRW, estimated * _MIN_EDGE_RATIO)
        strong_expected = max(_STRONG_EXPECTED_KRW, estimated * _STRONG_EDGE_RATIO)
        if use_toss_exit_cashflow:
            # Toss lifecycle 모델은 비용을 이미 차감했다. 양수 EV만 실행 가능하게
            # 하되, legacy 연구목표용 +0.6% 임계값을 재사용해 영구 차단하지 않는다.
            if (
                decision_expected is not None
                and decision_edge_ratio is not None
                and decision_expected >= strong_expected
                and decision_edge_ratio >= _STRONG_EDGE_RATIO
                and rr >= 2.0
            ):
                income_pass = True
                grade = "INCOME_PASS"
            elif (
                decision_expected is not None
                and decision_edge_ratio is not None
                and decision_expected > 0.0
                and decision_edge_ratio > 0.0
            ):
                income_pass = True
                grade = "SMALL_INCOME_PASS"
            else:
                income_pass = False
                grade = "BLOCK"
                block_reason = "expected_pnl_below_threshold"
                block_label = "실행 lifecycle 기대값이 비용 후 0 이하"
        elif (
            expected is not None
            and edge_ratio is not None
            and expected >= strong_expected
            and edge_ratio >= _STRONG_EDGE_RATIO
            and rr >= 2.0
        ):
            income_pass = True
            grade = "INCOME_PASS"
        elif (
            expected is not None
            and edge_ratio is not None
            and expected >= min_expected
            and edge_ratio >= _MIN_EDGE_RATIO
        ):
            income_pass = True
            grade = "SMALL_INCOME_PASS"
        else:
            income_pass = False
            grade = "BLOCK"
            block_reason = "expected_pnl_below_threshold"
            block_label = "수입 기대값이 최소 기준 미달"

    decision_min_expected = (
        0.0
        if use_toss_exit_cashflow
        else (max(_MIN_EXPECTED_KRW, estimated * _MIN_EDGE_RATIO) if estimated else _MIN_EXPECTED_KRW)
    )
    decision_min_edge_ratio = 0.0 if use_toss_exit_cashflow else _MIN_EDGE_RATIO

    result = {
        "version": version,
        "symbol": symbol,
        "requested_exit_model": requested_model,
        "expected_pnl_model": expected_pnl_model,
        "expected_pnl_scope": expected_pnl_scope,
        "residual_quantity_after_profit": residual_quantity_after_profit,
        "residual_mark_to_market_included": residual_mark_to_market_included,
        "expected_pnl_krw": round(expected, 2) if expected is not None else None,
        "income_edge_ratio": round(edge_ratio, 6) if edge_ratio is not None else None,
        "upside_krw": round(upside_krw, 2),
        "loss_krw": round(loss_krw, 2),
        "decision_expected_pnl_model": decision_expected_pnl_model,
        "decision_expected_pnl_scope": decision_expected_pnl_scope,
        "decision_expected_pnl_krw": (
            round(decision_expected, 2) if decision_expected is not None else None
        ),
        "decision_income_edge_ratio": (
            round(decision_edge_ratio, 6) if decision_edge_ratio is not None else None
        ),
        "decision_upside_krw": (
            round(decision_upside_krw, 2) if decision_upside_krw is not None else None
        ),
        "decision_loss_krw": (
            round(decision_loss_krw, 2) if decision_loss_krw is not None else None
        ),
        "decision_profit_exit_pct": profit_exit_pct,
        "decision_profit_exit_quantity": decision_profit_exit_quantity,
        "decision_loss_exit_pct": loss_exit_pct,
        "decision_loss_exit_quantity": decision_loss_exit_quantity,
        "decision_residual_quantity_after_profit": decision_residual_quantity_after_profit,
        "decision_residual_mark_to_market_included": decision_residual_mark_to_market_included,
        "decision_breakeven_win_rate": (
            round(decision_breakeven_win_rate, 6)
            if decision_breakeven_win_rate is not None else None
        ),
        "decision_breakeven_reachable": decision_breakeven_reachable,
        "research_target_upside_krw": round(research_target_upside_krw, 2),
        "research_stop_loss_krw": round(research_stop_loss_krw, 2),
        "profit_exit_pct": profit_exit_pct,
        "profit_exit_quantity": profit_exit_quantity,
        "loss_exit_pct": loss_exit_pct,
        "loss_exit_quantity": loss_exit_quantity,
        "breakeven_win_rate": (
            round(breakeven_win_rate, 6) if breakeven_win_rate is not None else None
        ),
        "breakeven_reachable": breakeven_reachable,
        "fee_slippage_buffer_krw": round(fee_slippage, 2),
        "win_prob": win_prob,
        "stop_risk_pct": stop_risk_pct,
        "risk_reward": rr,
        "estimated_amount_krw": round(estimated, 2) if estimated else 0.0,
        "canonical_notional_krw": round(canonical_notional, 2) if canonical_notional else 0.0,
        "reported_estimated_amount_krw": (
            round(reported_estimated, 2) if reported_estimated else 0.0
        ),
        "estimated_notional_tolerance_krw": round(notional_tolerance, 2),
        "estimated_notional_mismatch": notional_mismatch,
        "input_validation_error": toss_input_error or None,
        "income_pass": bool(income_pass),
        "income_grade": grade,
        "income_block_reason": block_reason,
        "income_block_label": block_label,
        "thresholds": {
            "decision_rule": (
                "invalid_exit_model_fail_closed"
                if invalid_exit_model
                else (
                    "positive_lifecycle_ev_after_cost"
                    if use_toss_exit_cashflow
                    else "legacy_research_minimum_edge"
                )
            ),
            "min_expected_pnl_krw": round(decision_min_expected, 2),
            "min_income_edge_ratio": decision_min_edge_ratio,
            "legacy_research_min_expected_pnl_krw": (
                round(max(_MIN_EXPECTED_KRW, estimated * _MIN_EDGE_RATIO), 2)
                if estimated else _MIN_EXPECTED_KRW
            ),
            "legacy_research_min_income_edge_ratio": _MIN_EDGE_RATIO,
            "min_risk_reward": _MIN_RISK_REWARD,
            "max_stop_risk_pct": _MAX_STOP_RISK_PCT,
        },
    }
    sanitized = _without_nonfinite(result)
    return sanitized if isinstance(sanitized, dict) else {}
