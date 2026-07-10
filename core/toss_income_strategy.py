"""Toss 수입형 자율매매 v1 income gate.

추천 점수만으로 자동 BUY가 실행되지 않도록, 실행 직전 후보를
계좌 수입 기대값(expected_pnl_krw) 기준으로 한 번 더 차단한다.
이 모듈은 read-only 계산만 수행하며 주문/취소/정정 부작용이 없다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

_MIN_EDGE_RATIO = 0.006
_MIN_EXPECTED_KRW = 7_000.0
_STRONG_EDGE_RATIO = 0.012
_STRONG_EXPECTED_KRW = 15_000.0
_MAX_STOP_RISK_PCT = 4.5
_INCOME_PLAN_STOP_RISK_PCT = 4.2
_INCOME_TAKE_PROFIT_SINGLE_PCT = 1.5
_INCOME_PARTIAL_TAKE_PROFIT_PCT = 1.2
_INCOME_EARLY_STOP_LOSS_PCT = -2.5
_INCOME_HARD_STOP_LOSS_PCT = -4.5
_MIN_RISK_REWARD = 1.5
_DEFAULT_FX_USDKRW = 1400.0


def _num(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


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
        "version": "income_v1_exit_plan",
        "entry_price": entry,
        "target_price": target or None,
        "stop_loss": stop or None,
        "original_stop_loss": original_stop or None,
        "original_target_price": original_target or None,
        "stop_risk_pct": stop_risk_pct,
        "risk_reward": rr,
        "single_share_take_profit_pct": _INCOME_TAKE_PROFIT_SINGLE_PCT,
        "multi_share_partial_take_profit_pct": _INCOME_PARTIAL_TAKE_PROFIT_PCT,
        "early_stop_loss_pct": _INCOME_EARLY_STOP_LOSS_PCT,
        "hard_stop_loss_pct": _INCOME_HARD_STOP_LOSS_PCT,
        "note": "income_v1 후보는 기존 6% stop을 그대로 쓰지 않고 4.2% 내외로 당겨 수입 기대값을 재계산",
    }
    return out


def _normalize_trade_symbol(symbol: str) -> str:
    sym = str(symbol or "").upper().strip()
    if sym.isdigit() and len(sym) == 6:
        return f"{sym}.KS"
    return sym


def _holding_to_rebalance_row(item: Mapping, fx_usdkrw: float = 0.0) -> dict | None:
    raw = str(item.get("symbol") or "").upper().strip()
    symbol = _normalize_trade_symbol(raw)
    if not symbol:
        return None
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
    currency = str(item.get("currency") or "KRW").upper()
    # 손실률/당일손실/절대손실을 함께 반영. 낮을수록 약한 보유분.
    weakness_score = (-pl_pct * 2.0) + (-daily_pct * 1.0) + max(-pl_amount, 0.0) / 100_000.0

    if currency == "KRW":
        market_value_krw = round(amount, 2)
        estimated_release_usd = None
        estimated_release_krw = round(amount, 2)
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
    income = item.get("income_strategy") or {}
    if not income.get("income_pass"):
        return None
    symbol = str(item.get("symbol") or item.get("ticker") or "").upper().strip()
    if not symbol:
        return None
    market = str(item.get("market") or ("KR" if symbol.endswith((".KS", ".KQ")) else "US")).upper()
    currency = "USD" if market == "US" else "KRW"
    if currency == "USD":
        native = _num(item.get("estimated_amount_usd"), 0.0)
        if native <= 0 and fx_usdkrw > 0:
            native = round(_num(item.get("estimated_amount_krw"), 0.0) / fx_usdkrw, 4)
    else:
        native = _num(item.get("estimated_amount_krw"), 0.0)
    return {
        "symbol": symbol,
        "name": item.get("name") or symbol,
        "market": market,
        "currency": currency,
        "estimated_amount_krw": round(_num(item.get("estimated_amount_krw"), 0.0), 2),
        "estimated_amount_native": round(native, 4) if native else 0.0,
        "expected_pnl_krw": income.get("expected_pnl_krw"),
        "income_edge_ratio": income.get("income_edge_ratio"),
        "risk_reward": item.get("risk_reward"),
        "execution_status": item.get("execution_status"),
        "block_reason": item.get("block_reason"),
        "stock_agent_ready": bool(item.get("stock_agent_ready")),
    }


_FUNDING_MODE = "currency_income_replacement"


def _row_release_native(row: Mapping) -> float:
    """매도 row의 같은 통화 기준 확보 예상액."""
    if str(row.get("currency") or "KRW").upper() == "USD":
        return _num(row.get("estimated_release_usd"), 0.0)
    return _num(row.get("estimated_release_krw"), 0.0)


def _compute_funding_plan(
    waitlist: list[dict],
    cash: Mapping,
    fx_usdkrw: float,
    merged_rows: list[dict],
) -> dict:
    """통화별 income 자금조달 매도 계획.

    "팔고 나서 못 사는" 흐름을 금지한다: 같은 통화 현금 + AI Berkshire
    eligible 보유분 매도 예상액으로 income_pass 후보를 **전액** 매수할 수
    있을 때만 funding_rebalance_required=true. 선택된 최소 매도 rows에만
    funding 필드를 표시한다 (in-place). Read-only 계산.
    """
    cash_by_ccy = {
        "KRW": _num(cash.get("krw_native", cash.get("krw")), 0.0),
        "USD": _num(cash.get("usd"), 0.0),
    }
    eligible_by_ccy: dict[str, list[dict]] = {"KRW": [], "USD": []}
    release_by_ccy = {"KRW": 0.0, "USD": 0.0}
    for r in merged_rows:
        if r.get("auto_sell_eligible") is not True:
            continue
        native = _row_release_native(r)
        if native <= 0:
            continue
        ccy = str(r.get("currency") or "KRW").upper()
        eligible_by_ccy.setdefault(ccy, []).append(r)
        release_by_ccy[ccy] = release_by_ccy.get(ccy, 0.0) + native

    empty = {"required": False, "currency": None, "target": None,
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
        if avail + release_by_ccy.get(ccy, 0.0) < need:
            continue  # 다 팔아도 전액 매수 불가 — 먼저 팔면 안 됨
        if best is None or _num(w.get("expected_pnl_krw"), 0.0) > _num(best["target"].get("expected_pnl_krw"), 0.0):
            best = {"target": w, "currency": ccy, "gap": gap, "avail": avail}

    if best is None:
        return empty

    ccy = best["currency"]
    gap = round(best["gap"], 4)
    target = best["target"]
    gap_krw = round(gap * fx_usdkrw, 2) if ccy == "USD" and fx_usdkrw > 0 else (
        round(gap, 2) if ccy == "KRW" else None)

    # funding gap 충당에 필요한 최소 rows만 (우선순위 높은 순)
    rows: list[dict] = []
    cumulative = 0.0
    for r in eligible_by_ccy.get(ccy, []):
        if cumulative >= gap:
            break
        native = _row_release_native(r)
        cumulative = round(cumulative + native, 4)
        r["funding_mode"] = _FUNDING_MODE
        r["funding_currency"] = ccy
        r["funding_target_symbol"] = target["symbol"]
        r["funding_gap_native"] = gap
        r["estimated_release_native"] = native
        r["cumulative_release_native"] = cumulative
        r["covers_funding_target"] = cumulative >= gap
        rows.append(r)

    return {
        "required": True,
        "currency": ccy,
        "target": {
            "symbol": target["symbol"],
            "name": target.get("name"),
            "estimated_amount_native": target.get("estimated_amount_native"),
            "expected_pnl_krw": target.get("expected_pnl_krw"),
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
    funding_waitlist.sort(key=lambda r: (_num(r.get("expected_pnl_krw"), 0.0), _num(r.get("income_edge_ratio"), 0.0)), reverse=True)
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

    # 통화별 자금조달 매도: "팔면 전액 매수 가능"할 때만 최소 rows 표시.
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
) -> dict:
    """BUY 후보의 수입 기대값과 실행 가능 여부를 계산한다."""
    symbol = str(candidate.get("symbol") or candidate.get("ticker") or "").upper().strip()
    side = str(candidate.get("side") or "buy").lower()
    qty = max(0, int(_num(candidate.get("quantity"), 0)))
    entry = _num(candidate.get("limit_price") or candidate.get("entry_price") or candidate.get("price"), 0.0)
    target = _num(candidate.get("target_price"), 0.0)
    stop = _num(candidate.get("stop_loss"), 0.0)
    rr = _num(candidate.get("risk_reward"), 0.0)
    asset_type = str(candidate.get("asset_type") or "").upper()
    market = str(candidate.get("market") or "").upper()
    is_us = asset_type == "US_STOCK" or market == "US" or (symbol and not symbol.endswith((".KS", ".KQ")) and not symbol.isdigit())
    fx = _num(candidate.get("fx_usdkrw"), _DEFAULT_FX_USDKRW if is_us else 1.0)
    multiplier = fx if is_us else 1.0

    estimated = _num(candidate.get("estimated_amount_krw"), 0.0)
    if estimated <= 0 and entry > 0 and qty > 0:
        estimated = entry * qty * multiplier

    stop_risk_pct = round(max((entry - stop) / entry * 100.0, 0.0), 4) if entry > 0 and stop > 0 else None
    upside_krw = max(target - entry, 0.0) * qty * multiplier if target > 0 and entry > 0 and qty > 0 else 0.0
    loss_krw = max(entry - stop, 0.0) * qty * multiplier if stop > 0 and entry > 0 and qty > 0 else 0.0
    win_prob = estimate_win_prob(candidate, reliability_stats=reliability_stats)
    fee_slippage = max(1_000.0, estimated * 0.0015) if estimated > 0 else 1_000.0
    expected = win_prob * upside_krw - (1.0 - win_prob) * loss_krw - fee_slippage
    edge_ratio = expected / estimated if estimated > 0 else 0.0

    block_reason = ""
    block_label = ""
    if side != "buy":
        income_pass = True
        grade = "INCOME_PASS"
    elif not symbol or qty <= 0 or entry <= 0 or target <= 0 or stop <= 0:
        income_pass = False
        grade = "BLOCK"
        block_reason = "missing_income_inputs"
        block_label = "수입 기대값 계산 필수값 부족"
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
        if expected >= strong_expected and edge_ratio >= _STRONG_EDGE_RATIO and rr >= 2.0:
            income_pass = True
            grade = "INCOME_PASS"
        elif expected >= min_expected and edge_ratio >= _MIN_EDGE_RATIO:
            income_pass = True
            grade = "SMALL_INCOME_PASS"
        else:
            income_pass = False
            grade = "BLOCK"
            block_reason = "expected_pnl_below_threshold"
            block_label = "수입 기대값이 최소 기준 미달"

    return {
        "version": "income_v1",
        "symbol": symbol,
        "expected_pnl_krw": round(expected, 2),
        "income_edge_ratio": round(edge_ratio, 6),
        "upside_krw": round(upside_krw, 2),
        "loss_krw": round(loss_krw, 2),
        "fee_slippage_buffer_krw": round(fee_slippage, 2),
        "win_prob": win_prob,
        "stop_risk_pct": stop_risk_pct,
        "risk_reward": rr,
        "estimated_amount_krw": round(estimated, 2) if estimated else 0.0,
        "income_pass": bool(income_pass),
        "income_grade": grade,
        "income_block_reason": block_reason,
        "income_block_label": block_label,
        "thresholds": {
            "min_expected_pnl_krw": round(max(_MIN_EXPECTED_KRW, estimated * _MIN_EDGE_RATIO), 2) if estimated else _MIN_EXPECTED_KRW,
            "min_income_edge_ratio": _MIN_EDGE_RATIO,
            "min_risk_reward": _MIN_RISK_REWARD,
            "max_stop_risk_pct": _MAX_STOP_RISK_PCT,
        },
    }
