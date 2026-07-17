"""core/toss_position_review.py

Toss 보유 포지션 일일 재평가 → 자동 매도 후보 생성.

[배경]
exit watch(A-2/A-3)는 ledger에 stop_loss/target_price가 있는 live_sent
포지션만 감시한다. 그 밖의 보유종목(수동 매수분, 레벨 미기록 포지션)은
아무도 재평가하지 않아 손실이 방치될 수 있다. 이 모듈은 1일 1회 전
보유종목의 평가손익률을 점검하고 기준 초과 시 자동 매도 경로에 태운다.

[규칙 — env로 조정 가능]
- 손익률 ≤ -8% (TOSS_REVIEW_STOP_LOSS_PCT)  → 전량 매도 후보
- 손익률 ≥ +15% (TOSS_REVIEW_TAKE_PROFIT_PCT) → 분할 익절 후보 (절반)
- ledger에 활성 exit 레벨이 있는 심볼은 제외 (exit watch 담당)

[안전장치]
- autonomous mode ON + kill switch OFF + env sell 허용일 때만 매도 실행
- 실행은 toss_autonomous_pipeline.process_candidate 경로 재사용
  (preview→ledger→검증→자동판정→finalizer). 이 파일은 주문 API 직접 호출 없음
- 해당 시장 정규장 시간에만 매도 시도
- 손익률 계산 불가(원가 0/필드 누락) 종목은 건드리지 않음 (fail-safe)
- 1일 1회 실행 (state 파일 dedup) + 심볼당 1일 1회 매도 시도
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
US_EASTERN = ZoneInfo("America/New_York")

_STATE_FILE = "toss_position_review_state.json"
_DEFAULT_STOP_LOSS_PCT = -8.0    # 이하 → 전량 매도
_DEFAULT_TAKE_PROFIT_PCT = 15.0  # 이상 → 분할 익절
_PARTIAL_SELL_RATIO = 0.5
_INCOME_TAKE_PROFIT_SINGLE_PCT = 1.5
_INCOME_PARTIAL_TAKE_PROFIT_PCT = 1.2
_INCOME_EARLY_STOP_LOSS_PCT = -2.5
_INCOME_HARD_STOP_LOSS_PCT = -4.5
_MAX_EXECUTION_QUANTITY = 1_000_000
_MAX_EXECUTION_PRICE = 1_000_000_000_000.0
_REVIEW_HOUR_KST = 10            # KST 10시 이후 (개장 직후 노이즈 회피)
_REVIEW_INTERVAL_MINUTES = 30     # 장중 보유 리스크는 일 1회가 아니라 주기 재평가

# 리밸런싱 매도(제한형 A) — 보유 과다 정리를 자동 SELL로 연결하되 하드가드로 제한
_SELL_TO_FUND_ACTION = "sell_to_fund"
_SELL_TO_FUND_REASON = "income_rebalance_sell_to_fund"
_DEFAULT_REBALANCE_MIN_HOLDINGS = 20      # 초과일 때만 sell_to_fund 허용
_DEFAULT_REBALANCE_TARGET_HOLDINGS = 12
_DEFAULT_REBALANCE_MAX_SELLS_PER_RUN = 1
_DEFAULT_REBALANCE_MAX_SELLS_PER_DAY = 2
_REBALANCE_MAX_SELLS_PER_RUN_CAP = 3
_REBALANCE_MAX_SELLS_PER_DAY_CAP = 5
_BASE_PROTECTED_SYMBOLS = frozenset({"MU"})


def _stop_loss_pct() -> float:
    try:
        return float(os.environ.get("TOSS_REVIEW_STOP_LOSS_PCT", _DEFAULT_STOP_LOSS_PCT))
    except ValueError:
        return _DEFAULT_STOP_LOSS_PCT


def _take_profit_pct() -> float:
    try:
        return float(os.environ.get("TOSS_REVIEW_TAKE_PROFIT_PCT", _DEFAULT_TAKE_PROFIT_PCT))
    except ValueError:
        return _DEFAULT_TAKE_PROFIT_PCT


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(value, high))


def _rebalance_min_holdings() -> int:
    return _env_int("TOSS_REBALANCE_MIN_HOLDINGS", _DEFAULT_REBALANCE_MIN_HOLDINGS, 0, 100)


def _rebalance_target_holdings() -> int:
    return _env_int("TOSS_REBALANCE_TARGET_HOLDINGS", _DEFAULT_REBALANCE_TARGET_HOLDINGS, 1, 100)


def _rebalance_max_sells_per_run() -> int:
    return _env_int(
        "TOSS_REBALANCE_MAX_SELLS_PER_RUN",
        _DEFAULT_REBALANCE_MAX_SELLS_PER_RUN,
        0, _REBALANCE_MAX_SELLS_PER_RUN_CAP,
    )


def _rebalance_max_sells_per_day() -> int:
    return _env_int(
        "TOSS_REBALANCE_MAX_SELLS_PER_DAY",
        _DEFAULT_REBALANCE_MAX_SELLS_PER_DAY,
        0, _REBALANCE_MAX_SELLS_PER_DAY_CAP,
    )


def _symbol_variants(symbol: str) -> set[str]:
    """`015760` ↔ `015760.KS` 표기 차이로 보호가 뚫리지 않도록 변형을 함께 본다."""
    sym = str(symbol or "").upper().strip()
    if not sym:
        return set()
    out = {sym}
    if sym.endswith((".KS", ".KQ")):
        out.add(sym.split(".", 1)[0])
    elif sym.isdigit() and len(sym) == 6:
        out.add(f"{sym}.KS")
        out.add(f"{sym}.KQ")
    return out


def _rebalance_protected_symbols(policy: dict | None = None) -> set[str]:
    """sell_to_fund 자동매도 금지 심볼 — 기본 + env + policy preferred."""
    protected: set[str] = set()
    for sym in _BASE_PROTECTED_SYMBOLS:
        protected |= _symbol_variants(sym)
    for raw in str(os.environ.get("TOSS_REBALANCE_PROTECTED_SYMBOLS", "")).split(","):
        protected |= _symbol_variants(raw)
    for raw in (policy or {}).get("preferred_symbols") or []:
        protected |= _symbol_variants(raw)
    return protected


def _state_path() -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / "db" / "data" / _STATE_FILE


def _load_state() -> dict:
    p = _state_path()
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
        if type(state) is not dict:
            raise ValueError("state_not_object")
        attempted = state.get("attempted", {})
        if type(attempted) is not dict:
            raise ValueError("attempted_not_object")
        for entry in attempted.values():
            if type(entry) is not dict:
                raise ValueError("attempt_entry_not_object")
            if "live_order_sent" in entry and type(entry["live_order_sent"]) is not bool:
                raise ValueError("attempt_live_order_sent_not_bool")
            at_iso = entry.get("at_iso")
            if at_iso:
                parsed = datetime.fromisoformat(str(at_iso).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("attempt_timestamp_naive")
        last_review_at = state.get("last_review_at")
        if last_review_at:
            parsed = datetime.fromisoformat(str(last_review_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("review_timestamp_naive")
        return state
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("position review state load failed: %s", e)
        return {"_state_load_failed": True}


def _save_state(state: dict) -> bool:
    p = _state_path()
    tmp_name = ""
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent),
        )
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, p)
        tmp_name = ""
        dir_fd = os.open(str(p.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return True
    except Exception as e:
        log.warning("position review state save failed: %s", e)
        return False
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _position_market_day(symbol: str, value: datetime):
    is_kr = symbol.endswith((".KS", ".KQ")) or symbol.isdigit()
    return value.astimezone(KST if is_kr else US_EASTERN).date()


def _attempted_for_market_day(state: dict, now: datetime) -> dict:
    attempted = state.get("attempted")
    if not isinstance(attempted, dict):
        return {}
    current: dict = {}
    legacy_date = str(state.get("attempted_date") or "")
    for symbol, entry in attempted.items():
        if not isinstance(entry, dict):
            continue
        raw = str(entry.get("at_iso") or "")
        attempted_at = None
        if raw:
            try:
                attempted_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if attempted_at.tzinfo is None:
                    attempted_at = attempted_at.replace(tzinfo=KST)
            except ValueError:
                attempted_at = None
        if attempted_at is not None:
            if _position_market_day(symbol, attempted_at) == _position_market_day(symbol, now):
                current[symbol] = entry
        elif legacy_date == now.astimezone(KST).strftime("%Y-%m-%d"):
            current[symbol] = entry
    return current


def _to_float(v, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _normalize_symbol(raw: str) -> str:
    """Toss 심볼 → 주문 경로 심볼 (6자리 코드는 .KS 기본)."""
    sym = str(raw or "").upper().strip()
    if sym.isdigit() and len(sym) == 6:
        return f"{sym}.KS"
    return sym


def _symbols_with_active_exit_levels() -> set[str]:
    """ledger에 활성 exit 레벨(stop/target)이 있는 live_sent 심볼 — exit watch 담당."""
    try:
        from core.toss_live_pilot_ledger import list_live_pilot_records
        records = list_live_pilot_records(limit=100)
    except Exception as e:
        log.warning("position review ledger fetch failed: %s", e)
        return set()
    out: set[str] = set()
    for r in records:
        if r.get("status") != "live_sent":
            continue
        if _to_float(r.get("stop_loss")) > 0 or _to_float(r.get("target_price")) > 0:
            sym = str(r.get("symbol", "")).upper().strip()
            if sym:
                out.add(sym)
                if sym.endswith((".KS", ".KQ")):
                    out.add(sym.split(".")[0])
    return out



def _income_managed_symbols() -> set[str]:
    """자동 income BUY로 만든 보유 후보 심볼.

    주문/취소 부작용 없이 ledger만 읽는다. 이 집합에 들어온 보유분만
    +1~2% 익절 / -2.5% 조기 실패 컷 규칙을 적용한다.
    """
    try:
        from core.toss_live_pilot_ledger import list_live_pilot_records
        records = list_live_pilot_records(limit=200)
    except Exception as e:
        log.warning("income position ledger fetch failed: %s", e)
        return set()
    out: set[str] = set()
    for r in records:
        side = str(r.get("side") or "").lower()
        status = str(r.get("status") or "").lower()
        reason = str(r.get("reason") or "").lower()
        if side != "buy" or status not in {"live_sent", "filled"}:
            continue
        if "auto_pipeline" not in reason and "income" not in reason:
            continue
        sym = str(r.get("symbol") or "").upper().strip()
        if not sym:
            continue
        out.add(sym)
        if sym.endswith((".KS", ".KQ")):
            out.add(sym.split(".", 1)[0])
    return out

def evaluate_holdings(holdings_items: list[dict] | None = None) -> list[dict]:
    """보유종목 평가 → 매도 후보 목록.

    Returns:
        [{symbol, name, pl_pct, action("stop_loss"/"take_profit"),
          quantity(매도 수량), held_quantity, last_price, currency}, ...]
    """
    if holdings_items is None:
        try:
            from core.dashboard_data import toss_account_summary
            holdings_items = (toss_account_summary() or {}).get("holdings_items") or []
        except Exception as e:
            log.warning("position review holdings fetch failed: %s", e)
            return []

    exit_covered = _symbols_with_active_exit_levels()
    income_managed = _income_managed_symbols()
    stop_pct = _stop_loss_pct()
    profit_pct = _take_profit_pct()

    candidates: list[dict] = []
    for item in holdings_items:
        raw_sym = str(item.get("symbol") or "").upper().strip()
        if not raw_sym:
            continue
        symbol = _normalize_symbol(raw_sym)
        exit_covered_hit = raw_sym in exit_covered or symbol in exit_covered
        income_managed_hit = raw_sym in income_managed or symbol in income_managed

        qty = int(_to_float(item.get("quantity")))
        last_price = _to_float(item.get("lastPrice"))
        if qty <= 0 or last_price <= 0:
            continue

        pl = item.get("profitLoss") or {}
        mv = item.get("marketValue") or {}
        pl_amount = _to_float(pl.get("amountAfterCost", pl.get("amount")))
        purchase = _to_float(mv.get("purchaseAmount"))
        if purchase <= 0:
            continue  # 원가 불명 — 판단 불가, 건드리지 않음 (fail-safe)
        pl_pct = pl_amount / purchase * 100

        if income_managed_hit:
            if pl_pct <= _INCOME_HARD_STOP_LOSS_PCT:
                action, sell_qty = "income_hard_stop_loss", qty
            elif pl_pct <= _INCOME_EARLY_STOP_LOSS_PCT:
                action, sell_qty = "income_early_stop_loss", qty
            elif qty <= 1 and pl_pct >= _INCOME_TAKE_PROFIT_SINGLE_PCT:
                action, sell_qty = "income_take_profit", qty
            elif qty > 1 and pl_pct >= _INCOME_PARTIAL_TAKE_PROFIT_PCT:
                action, sell_qty = "income_partial_take_profit", max(1, int(qty * _PARTIAL_SELL_RATIO))
            else:
                continue
        elif pl_pct <= stop_pct:
            action, sell_qty = "stop_loss", qty
        elif pl_pct >= profit_pct:
            action, sell_qty = "take_profit", max(1, int(qty * _PARTIAL_SELL_RATIO))
        else:
            continue

        candidates.append({
            "symbol": symbol,
            "name": str(item.get("name") or raw_sym),
            "pl_pct": round(pl_pct, 2),
            "action": action,
            "quantity": sell_qty,
            "held_quantity": qty,
            "last_price": last_price,
            "currency": str(item.get("currency") or "KRW").upper(),
            "exit_covered": bool(exit_covered_hit),
            "income_managed": bool(income_managed_hit),
            "review_reason": (
                "income_position_review" if income_managed_hit else (
                    "aggregate_position_risk_overrides_exit_watch"
                    if exit_covered_hit else "aggregate_position_review"
                )
            ),
        })
    return candidates


def _sell_to_fund_attempts_today(attempted_map: dict | None) -> int:
    """오늘 이미 시도한 sell_to_fund 건수 (state의 attempted 기록 기준)."""
    count = 0
    for entry in (attempted_map or {}).values():
        if not isinstance(entry, dict):
            continue
        if (entry.get("action") == _SELL_TO_FUND_ACTION
                or entry.get("process_reason") == _SELL_TO_FUND_REASON):
            count += 1
    return count


def _strict_positive_value(value) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _valid_funding_target(plan: dict, currency: str) -> dict | None:
    if plan.get("funding_rebalance_required") is not True:
        return None
    target = plan.get("funding_target")
    if type(target) is not dict or target.get("side") != "buy":
        return None
    target_symbol = target.get("symbol")
    if type(target_symbol) is not str or not target_symbol.strip():
        return None
    if type(target.get("currency")) is not str or target.get("currency") != currency:
        return None
    from core.toss_income_strategy import (
        canonical_trade_identity,
        validate_executable_income_contract,
    )
    identity = canonical_trade_identity(target_symbol)
    if identity is None or identity[2] != currency:
        return None
    ok, _ = validate_executable_income_contract(target)
    if not ok or not str(target.get("symbol") or "").strip():
        return None
    return target


def _valid_funding_row(row: dict, target: dict, currency: str, gap: float) -> bool:
    row_gap = _strict_positive_value(row.get("funding_gap_native"))
    release = _strict_positive_value(row.get("estimated_release_native"))
    cumulative = _strict_positive_value(row.get("cumulative_release_native"))
    quantity = row.get("quantity")
    last_price = _strict_positive_value(row.get("last_price"))
    row_symbol = row.get("symbol")
    if (
        row_gap is None
        or release is None
        or cumulative is None
        or type(quantity) is not int
        or not 0 < quantity <= _MAX_EXECUTION_QUANTITY
        or last_price is None
        or last_price > _MAX_EXECUTION_PRICE
        or type(row_symbol) is not str
        or not row_symbol.strip()
    ):
        return False
    actual_release = quantity * last_price
    if (
        not math.isfinite(actual_release)
        or abs(release - actual_release) > max(0.01, actual_release * 0.000001)
    ):
        return False
    from core.toss_income_strategy import canonical_trade_identity
    identity = canonical_trade_identity(row_symbol)
    if identity is None or identity[2] != currency:
        return False
    return (
        row.get("auto_sell_eligible") is True
        and type(row.get("side")) is str
        and row.get("side") == "sell"
        and row.get("covers_funding_target") is True
        and row.get("funding_mode") == "currency_income_replacement"
        and type(row.get("funding_currency")) is str
        and row.get("funding_currency") == currency
        and type(row.get("currency")) is str
        and row.get("currency") == currency
        and type(row.get("funding_target_symbol")) is str
        and row.get("funding_target_symbol") == target.get("symbol")
        and abs(row_gap - gap) <= 0.0001
        and release >= gap
        and abs(cumulative - release) <= 0.0001
    )


def evaluate_sell_to_fund_candidates(
    account_summary: dict | None = None,
    rebalance_plan: dict | None = None,
    policy: dict | None = None,
    attempted_map: dict | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """리밸런싱/자금조달 매도 후보 (제한형 A).

    발동 경로 두 가지:
    ① 보유 과다(portfolio_rebalance_required + 보유 하한 초과) — 포지션 수 정리
    ② 통화별 income 자금조달(funding_rebalance_required) — 보유 20 이하라도,
       같은 통화 현금+eligible 매도액으로 income 후보를 전액 매수 가능할 때만

    하드가드: 루프당 상한 / 일일 상한 / 보호 심볼
    + AI Berkshire eligibility(fail-closed) + 열린 시장 필터.

    시장 필터를 per_run cap보다 먼저 적용한다 — 닫힌 시장 후보(예: KR장에
    US 종목)가 유일한 slot을 점유한 채 market_closed로 스킵되면 정작 열린
    시장 후보까지 순번이 안 내려가기 때문이다.
    주문 API를 직접 호출하지 않고 후보 목록만 만든다.
    """
    now = now or datetime.now(KST)
    max_per_run = _rebalance_max_sells_per_run()
    if max_per_run <= 0:
        return []
    remaining_today = _rebalance_max_sells_per_day() - _sell_to_fund_attempts_today(attempted_map)
    if remaining_today <= 0:
        return []
    max_per_run = min(max_per_run, remaining_today)  # 루프 상한이 일일 잔여를 넘지 않게

    if account_summary is None:
        try:
            from core.dashboard_data import toss_account_summary
            account_summary = toss_account_summary() or {}
        except Exception as e:
            log.warning("sell_to_fund account fetch failed: %s", e)
            return []

    holdings_items = account_summary.get("holdings_items") or []
    holdings_count = int(_to_float(account_summary.get("holdings_count"), len(holdings_items)))

    if rebalance_plan is None:
        try:
            from core.dashboard_data import toss_buy_candidates_data
            from core.toss_income_strategy import build_rebalance_plan
            buy_data = toss_buy_candidates_data(range_="today", market="ALL", limit=80) or {}
            rebalance_plan = build_rebalance_plan(
                account_summary,
                buy_data.get("items") or [],
                target_holding_count=_rebalance_target_holdings(),
            )
        except Exception as e:
            log.warning("sell_to_fund rebalance plan build failed: %s", e)
            return []

    # 두 발동 경로: ①보유 과다 리밸런싱 ②통화별 income 자금조달 (보유 20 이하 허용)
    portfolio_mode = (
        rebalance_plan.get("portfolio_rebalance_required") is True
        and holdings_count > _rebalance_min_holdings()
    )
    raw_funding_currency = rebalance_plan.get("funding_currency")
    funding_currency = (
        raw_funding_currency
        if type(raw_funding_currency) is str
        and raw_funding_currency in {"KRW", "USD"}
        else ""
    )
    funding_gap = _strict_positive_value(rebalance_plan.get("funding_gap_native"))
    raw_funding_source = rebalance_plan.get("funding_source_symbol")
    funding_source = (
        raw_funding_source
        if type(raw_funding_source) is str and raw_funding_source.strip()
        else ""
    )
    funding_target = (
        _valid_funding_target(rebalance_plan, funding_currency)
        if funding_currency and funding_source and funding_gap is not None
        else None
    )
    funding_mode = funding_target is not None
    if not portfolio_mode and not funding_mode:
        return []


    protected = _rebalance_protected_symbols(policy)
    attempted = attempted_map or {}

    rows = [
        r for r in rebalance_plan.get("sell_to_fund_candidates") or []
        if isinstance(r, dict)
    ]
    # AI Berkshire eligibility가 없는 row는 자동매도 후보에서 제외 (fail-closed)
    rows = [r for r in rows if r.get("auto_sell_eligible") is True]
    if not portfolio_mode:
        # funding 전용: funding target과 같은 통화의 funding rows만
        if not isinstance(funding_target, dict) or funding_gap is None:
            return []
        rows = [
            r for r in rows
            if _valid_funding_row(r, funding_target, funding_currency, funding_gap)
        ]
        if len(rows) != 1:
            return []
        if rows[0].get("symbol") != funding_source:
            return []
    rows.sort(
        key=lambda r: _to_float(r.get("adjusted_sell_priority"),
                                _to_float(r.get("weakness_score"))),
        reverse=True,
    )

    candidates: list[dict] = []
    for row in rows:
        symbol = _normalize_symbol(row.get("symbol") or "")
        if not symbol or _symbol_variants(symbol) & protected:
            continue
        if any(v in attempted for v in _symbol_variants(symbol)):
            continue
        if not _market_open_for_symbol(symbol, now):
            continue  # 닫힌 시장 후보는 slot을 소모하지 않는다

        qty = int(_to_float(row.get("quantity")))
        last_price = _to_float(row.get("last_price"))
        if qty <= 0 or last_price <= 0:
            continue

        candidate = {
            "symbol": symbol,
            "name": str(row.get("name") or symbol),
            "pl_pct": round(_to_float(row.get("pl_pct")), 2),
            "action": _SELL_TO_FUND_ACTION,
            "quantity": qty,           # 기본 전량
            "held_quantity": qty,
            "last_price": last_price,
            "currency": str(row.get("currency") or "KRW").upper(),
            "review_reason": _SELL_TO_FUND_REASON,
            "process_reason": _SELL_TO_FUND_REASON,
            "estimated_release_krw": _to_float(row.get("estimated_release_krw")),
            "weakness_score": _to_float(row.get("weakness_score")),
            "adjusted_sell_priority": _to_float(row.get("adjusted_sell_priority")),
            "ai_berkshire_classification": (row.get("ai_berkshire") or {}).get("classification"),
        }
        if row.get("funding_target_symbol"):
            candidate["funding_mode"] = row.get("funding_mode") or "currency_income_replacement"
            candidate["funding_target_symbol"] = row.get("funding_target_symbol")
            candidate["funding_currency"] = str(row.get("funding_currency") or "").upper()
        candidates.append(candidate)
        if len(candidates) >= max_per_run:
            break
    return candidates


def _market_open_for_symbol(symbol: str, now: datetime) -> bool:
    from core.market_hours import is_kr_market_open, is_us_market_open
    if symbol.endswith((".KS", ".KQ")) or symbol.isdigit():
        return is_kr_market_open(now) is True
    return is_us_market_open(now) is True


def execute_sell_candidates(
    candidates: list[dict],
    policy: dict,
    now: datetime,
    attempted_map: dict,
) -> list[dict]:
    """매도 후보 → 자동 매도 경로 (process_candidate 재사용).

    가드: autonomous/kill switch/env sell 허용/장중 + 심볼당 1일 1회.
    """
    if not isinstance(policy, dict):
        return []
    from core.toss_live_pilot_policy import validate_autonomous_execution_policy
    policy_ok, policy_reason = validate_autonomous_execution_policy(policy, side="sell")
    if not policy_ok:
        return [{"symbol": c["symbol"], "stage": "skipped",
                 "reason": policy_reason} for c in candidates]

    from core.toss_autonomous_pipeline import process_candidate

    results: list[dict] = []
    for c in candidates:
        symbol = c["symbol"]
        if symbol in attempted_map:
            results.append({"symbol": symbol, "stage": "skipped",
                            "reason": "already_attempted_today"})
            continue
        if not _market_open_for_symbol(symbol, now):
            results.append({"symbol": symbol, "stage": "skipped",
                            "reason": "market_closed"})
            continue

        action = str(c.get("action") or "")
        if action == _SELL_TO_FUND_ACTION:
            intent_class = "rebalance"
        elif int(c.get("quantity") or 0) >= int(c.get("held_quantity") or 0):
            intent_class = "full_exit"
        else:
            intent_class = "partial_exit"
        from core.toss_exit_execution_intent import build_exit_decision_ref
        order_candidate = {
            "symbol": symbol,
            "side": "sell",
            "quantity": c["quantity"],
            "limit_price": c["last_price"],
            "currency": c.get("currency"),
            "decision_ref": build_exit_decision_ref(symbol, intent_class, now),
        }
        process_reason = c.get("process_reason") or (
            _SELL_TO_FUND_REASON
            if c.get("action") == _SELL_TO_FUND_ACTION
            else "position_review_sell"
        )
        note_parts = [
            f"review_action={c['action']}",
            f"pl_pct={c['pl_pct']}",
            f"qty={c['quantity']}/{c['held_quantity']}",
            f"exit_covered={c.get('exit_covered', False)}",
            f"review_reason={c.get('review_reason', '')}",
        ]
        if c.get("estimated_release_krw") is not None:
            note_parts.append(f"estimated_release_krw={c['estimated_release_krw']}")
        if c.get("weakness_score") is not None:
            note_parts.append(f"weakness_score={c['weakness_score']}")
        try:
            r = process_candidate(
                order_candidate, policy,
                reason=process_reason,
                note=" ".join(note_parts),
            )
        except Exception as e:
            log.error("position review sell error %s: %s", symbol, e)
            r = {
                "ok": False, "live_order_sent": False,
                "symbol": symbol, "stage": "error", "reason": str(e)[:200],
            }
        if type(r) is not dict:
            r = {
                "ok": False, "live_order_sent": False,
                "symbol": symbol, "stage": "error", "reason": "invalid_pipeline_result",
            }
        r["action"] = c["action"]
        r["pl_pct"] = c["pl_pct"]
        if r.get("ok") is True and r.get("live_order_sent") is True:
            attempted_map[symbol] = {
                "at": now.strftime("%H:%M"),
                "at_iso": now.isoformat(),
                "action": c["action"],
                "process_reason": process_reason,
                "stage": r.get("stage", ""),
                "verdict": r.get("verdict", ""),
                "live_order_sent": True,
            }
        results.append(r)
    return results


def _format_review_message(candidates: list[dict], results: list[dict]) -> str:
    lines = ["📋 [Toss 보유 포지션 일일 재평가]"]
    result_by_symbol = {r.get("symbol"): r for r in results}
    stop_actions = {"stop_loss", "income_hard_stop_loss", "income_early_stop_loss"}
    full_profit_actions = {"income_take_profit"}
    for c in candidates:
        if c["action"] == _SELL_TO_FUND_ACTION:
            label = "🔁 리밸런싱 매도 후보"
        elif c["action"] in stop_actions:
            label = "🔻 손절 기준 도달"
        else:
            label = "🔺 익절 기준 도달"
        lines.append(
            f"- {c['name']}({c['symbol']}) {label}: 손익 {c['pl_pct']:+.1f}%"
        )
        r = result_by_symbol.get(c["symbol"]) or {}
        if r.get("ok") is True and r.get("live_order_sent") is True:
            if c["action"] == _SELL_TO_FUND_ACTION:
                kind = "리밸런싱 매도"
            elif c["action"] in stop_actions or c["action"] in full_profit_actions:
                kind = "전량 매도"
            else:
                kind = "분할 익절"
            lines.append(f"  → 🤖 자동 매도 전송 완료 ({kind} {c['quantity']}주)")
        elif r.get("stage") == "skipped":
            lines.append(f"  → 자동 매도 스킵: {r.get('reason', '')}")
        else:
            lines.append(
                f"  → 자동 매도 미실행 ({r.get('stage', '')}: "
                f"{str(r.get('reason', ''))[:80]})"
            )
    return "\n".join(lines)


def run_toss_position_review(
    now: datetime | None = None,
    force: bool = False,
    send: bool = True,
) -> dict:
    """보유 포지션 일일 재평가 1회 실행 (monitor 루프에서 호출).

    - 주중 KST 10시 이후, 기본 30분 스로틀 재평가
    - 매도 후보 발견 시 자동 매도 경로 + 텔레그램 요약
    """
    now = now or datetime.now(KST)
    from core.market_hours import is_kr_market_open, is_us_market_open

    kr_market_open = is_kr_market_open(now) is True
    us_market_open = is_us_market_open(now) is True
    if not force:
        # KST 토요일 새벽은 미국 금요일 정규장일 수 있다. 캘린더 요일이나
        # KST 10시 기준으로 US 보호 SELL review를 막지 않는다.
        if now.weekday() >= 5 and not us_market_open:
            return {"skipped": "weekend"}
        if now.hour < _REVIEW_HOUR_KST and not us_market_open:
            return {"skipped": "before_review_hour"}
        # 장외 review가 throttle 상태를 소비하면 실제 개장 직후 보호 SELL이
        # 최대 30분 늦어진다. 열린 시장이 있을 때만 상태를 소비한다.
        if not kr_market_open and not us_market_open:
            return {"skipped": "market_closed"}
    elif not kr_market_open and not us_market_open:
        return {"skipped": "market_closed"}

    state = _load_state()
    if type(state) is not dict or state.get("_state_load_failed") is True:
        return {"ok": False, "reason": "state_load_failed", "reviewed": False}
    today = now.strftime("%Y-%m-%d")
    if not force:
        last_review_at = state.get("last_review_at", "")
        if last_review_at:
            try:
                last_dt = datetime.fromisoformat(str(last_review_at))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=KST)
                last_dt = last_dt.astimezone(KST)
                if (now - last_dt) < timedelta(minutes=_REVIEW_INTERVAL_MINUTES):
                    return {"skipped": "throttled"}
            except Exception:
                pass

    attempted_map = _attempted_for_market_day(state, now)

    policy = None
    try:
        from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
        policy = compute_toss_live_pilot_policy()
    except Exception as e:
        log.warning("position review policy load failed: %s", e)

    # 손절/익절 등 리스크 후보가 먼저, 리밸런싱 매도는 남은 slot에만 append
    candidates = evaluate_holdings()
    if policy is not None:
        try:
            seen = {c["symbol"] for c in candidates}
            candidates.extend(
                c for c in evaluate_sell_to_fund_candidates(
                    policy=policy, attempted_map=attempted_map, now=now)
                if c["symbol"] not in seen
            )
        except Exception as e:
            log.warning("sell_to_fund evaluation failed: %s", e)

    state.update({
        "review_date": today,
        "last_review_at": now.isoformat(timespec="seconds"),
        "attempted_date": today,
        "attempted": attempted_map,
        "last_candidates": [
            {k: c[k] for k in ("symbol", "action", "pl_pct", "quantity")}
            for c in candidates
        ],
    })
    if candidates and not _save_state(state):
        return {
            "ok": False,
            "reason": "state_reservation_failed",
            "reviewed": False,
            "candidate_count": len(candidates),
            "candidates": candidates,
            "results": [],
            "sent": False,
        }

    results: list[dict] = []
    if candidates and policy is not None:
        try:
            results = execute_sell_candidates(candidates, policy, now, attempted_map)
        except Exception as e:
            log.warning("position review sell execution failed: %s", e)

    state.update({
        "attempted": attempted_map,
    })
    state_saved = _save_state(state)

    sent = False
    if candidates and send:
        try:
            from core.telegram import send_simple_message
            sent = send_simple_message(_format_review_message(candidates, results))
        except Exception as e:
            log.warning("position review 알림 전송 실패: %s", e)

    if candidates:
        log.info(
            "position review: %d candidates — %s",
            len(candidates),
            "; ".join(f"{c['symbol']}:{c['action']}({c['pl_pct']:+.1f}%)" for c in candidates),
        )

    return {
        "ok": state_saved,
        "reason": "" if state_saved else "state_finalize_failed",
        "reviewed": True,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "results": results,
        "sent": sent,
    }
