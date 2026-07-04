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
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

_STATE_FILE = "toss_position_review_state.json"
_DEFAULT_STOP_LOSS_PCT = -8.0    # 이하 → 전량 매도
_DEFAULT_TAKE_PROFIT_PCT = 15.0  # 이상 → 분할 익절
_PARTIAL_SELL_RATIO = 0.5
_REVIEW_HOUR_KST = 10            # KST 10시 이후 (개장 직후 노이즈 회피)


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


def _state_path() -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / "db" / "data" / _STATE_FILE


def _load_state() -> dict:
    p = _state_path()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("position review state load failed: %s", e)
    return {}


def _save_state(state: dict) -> None:
    p = _state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log.warning("position review state save failed: %s", e)


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
    stop_pct = _stop_loss_pct()
    profit_pct = _take_profit_pct()

    candidates: list[dict] = []
    for item in holdings_items:
        raw_sym = str(item.get("symbol") or "").upper().strip()
        if not raw_sym:
            continue
        symbol = _normalize_symbol(raw_sym)
        if raw_sym in exit_covered or symbol in exit_covered:
            continue

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

        if pl_pct <= stop_pct:
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
        })
    return candidates


def _market_open_for_symbol(symbol: str, now: datetime) -> bool:
    from core.market_hours import is_kr_market_open, is_us_market_open
    if symbol.endswith((".KS", ".KQ")) or symbol.isdigit():
        return is_kr_market_open(now)
    return is_us_market_open(now)


def execute_sell_candidates(
    candidates: list[dict],
    policy: dict,
    now: datetime,
    attempted_map: dict,
) -> list[dict]:
    """매도 후보 → 자동 매도 경로 (process_candidate 재사용).

    가드: autonomous/kill switch/env sell 허용/장중 + 심볼당 1일 1회.
    """
    if not policy.get("autonomous_mode"):
        return [{"symbol": c["symbol"], "stage": "skipped",
                 "reason": "autonomous_mode_disabled"} for c in candidates]
    if policy.get("autonomous_kill_switch"):
        return [{"symbol": c["symbol"], "stage": "skipped",
                 "reason": "kill_switch_active"} for c in candidates]
    sides = [str(s).lower() for s in (policy.get("autonomous_allowed_sides") or [])]
    if "sell" not in sides:
        return [{"symbol": c["symbol"], "stage": "skipped",
                 "reason": "sell_not_allowed_by_env"} for c in candidates]

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

        order_candidate = {
            "symbol": symbol,
            "side": "sell",
            "quantity": c["quantity"],
            "limit_price": c["last_price"],
            "currency": c.get("currency"),
        }
        try:
            r = process_candidate(
                order_candidate, policy,
                reason="position_review_sell",
                note=(
                    f"review_action={c['action']} pl_pct={c['pl_pct']} "
                    f"qty={c['quantity']}/{c['held_quantity']}"
                ),
            )
        except Exception as e:
            log.error("position review sell error %s: %s", symbol, e)
            r = {"symbol": symbol, "stage": "error", "reason": str(e)[:200]}
        r["action"] = c["action"]
        r["pl_pct"] = c["pl_pct"]
        attempted_map[symbol] = {
            "at": now.strftime("%H:%M"),
            "action": c["action"],
            "stage": r.get("stage", ""),
            "verdict": r.get("verdict", ""),
        }
        results.append(r)
    return results


def _format_review_message(candidates: list[dict], results: list[dict]) -> str:
    lines = ["📋 [Toss 보유 포지션 일일 재평가]"]
    result_by_symbol = {r.get("symbol"): r for r in results}
    for c in candidates:
        label = "🔻 손절 기준 도달" if c["action"] == "stop_loss" else "🔺 익절 기준 도달"
        lines.append(
            f"- {c['name']}({c['symbol']}) {label}: 손익 {c['pl_pct']:+.1f}%"
        )
        r = result_by_symbol.get(c["symbol"]) or {}
        if r.get("verdict") == "PASS":
            kind = "전량 매도" if c["action"] == "stop_loss" else "분할 익절"
            lines.append(f"  → 🤖 자동 매도 발동 ({kind} {c['quantity']}주, 검증 PASS)")
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

    - 주중 KST 10시 이후, 1일 1회 (state dedup)
    - 매도 후보 발견 시 자동 매도 경로 + 텔레그램 요약
    """
    now = now or datetime.now(KST)

    if not force:
        if now.weekday() >= 5:
            return {"skipped": "weekend"}
        if now.hour < _REVIEW_HOUR_KST:
            return {"skipped": "before_review_hour"}

    state = _load_state()
    today = now.strftime("%Y-%m-%d")
    if not force and state.get("review_date") == today:
        return {"skipped": "already_reviewed_today"}

    candidates = evaluate_holdings()

    attempted_map = state.get("attempted", {})
    if state.get("attempted_date") != today:
        attempted_map = {}

    results: list[dict] = []
    if candidates:
        try:
            from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
            policy = compute_toss_live_pilot_policy()
            results = execute_sell_candidates(candidates, policy, now, attempted_map)
        except Exception as e:
            log.warning("position review sell execution failed: %s", e)

    sent = False
    if candidates and send:
        try:
            from core.telegram import send_simple_message
            sent = send_simple_message(_format_review_message(candidates, results))
        except Exception as e:
            log.warning("position review 알림 전송 실패: %s", e)

    state.update({
        "review_date": today,
        "attempted_date": today,
        "attempted": attempted_map,
        "last_candidates": [
            {k: c[k] for k in ("symbol", "action", "pl_pct", "quantity")}
            for c in candidates
        ],
    })
    _save_state(state)

    if candidates:
        log.info(
            "position review: %d candidates — %s",
            len(candidates),
            "; ".join(f"{c['symbol']}:{c['action']}({c['pl_pct']:+.1f}%)" for c in candidates),
        )

    return {
        "reviewed": True,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "results": results,
        "sent": sent,
    }
