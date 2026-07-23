"""core/toss_order_watch.py

Toss 미체결 주문 + exit 레벨 감시.

A-1: 브로커 OPEN 주문 중 장시간 미체결 건 → 텔레그램 알림
A-2: live_sent 매수 포지션의 stop_loss/target_price 도달 → 텔레그램 알림
A-3: exit 도달 시 자동 매도 승격 — autonomous mode + sell 허용(env)일 때만
     preview→검증→자동판정 경로(toss_autonomous_pipeline.process_candidate)로
     연결. 이 모듈 자체는 주문 API를 직접 호출하지 않는다 (finalizer가 실행).

원칙:
- 이 파일에는 주문/취소/정정 API 호출 없음 — 실행은 기존 finalizer 경로 재사용
- 자동 매도 가드: TOSS_AUTONOMOUS_MODE + kill switch + TOSS_AUTONOMOUS_ALLOWED_SIDES
  + 정규장 시간 + Toss 실보유 수량 확인 (조회 실패 시 매도 안 함)
- 알림 중복 방지: db/data/toss_order_watch_state.json (키별 1일 1회)
- 민감정보(계좌/토큰) 저장·출력 금지
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

KST = timezone(timedelta(hours=9))
US_EASTERN = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)

# 미체결 판정 기준 (분)
STALE_OPEN_ORDER_MINUTES = 60
# 감시 주기 (분) — monitor 루프에서 매 사이클 호출돼도 이 간격으로 스로틀
WATCH_INTERVAL_MINUTES = 30
EXIT_RESERVATION_LEASE_MINUTES = 5
# exit 감시 대상: 최근 N일 내 live_sent 기록만
EXIT_WATCH_LOOKBACK_DAYS = 14


def _state_path() -> Path:
    try:
        from db.store import DB_DIR
        return DB_DIR / "toss_order_watch_state.json"
    except Exception:
        return Path("db/data/toss_order_watch_state.json")


def _load_state() -> dict:
    p = _state_path()
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
        if type(state) is not dict:
            raise ValueError("state_not_object")
        for bucket in ("alerted", "reservations"):
            rows = state.get(bucket, {})
            if type(rows) is not dict:
                raise ValueError(f"{bucket}_not_object")
            for key, value in rows.items():
                if type(key) is not str or _parse_ts(value) is None:
                    raise ValueError(f"{bucket}_entry_invalid")
        if state.get("last_run") and _parse_ts(state["last_run"]) is None:
            raise ValueError("last_run_invalid")
        if "last_market_open" in state and type(state["last_market_open"]) is not bool:
            raise ValueError("last_market_open_invalid")
        return state
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.error("toss_order_watch state load failed: %s", e)
        return {"_state_load_failed": True}


def _save_state(state: dict) -> bool:
    """상태를 원자적으로 저장. 실패하면 주문 경로가 fail-closed할 수 있게 False 반환."""
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
        log.warning("toss_order_watch state save failed: %s", e)
        return False
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _now_kst() -> datetime:
    return datetime.now(KST)


def _parse_ts(value: str) -> datetime | None:
    """ISO 계열 timestamp 문자열 파싱 (naive는 KST 가정)."""
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def _already_alerted(state: dict, key: str, now: datetime) -> bool:
    """같은 키는 하루 1회만 알림."""
    ts = _parse_ts(state.get("alerted", {}).get(key, ""))
    return bool(ts and ts.date() == now.date())


def _mark_alerted(state: dict, key: str, now: datetime) -> None:
    state.setdefault("alerted", {})[key] = now.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    # 오래된 키 정리 (7일+)
    cutoff = now - timedelta(days=7)
    state["alerted"] = {
        k: v for k, v in state["alerted"].items()
        if (_parse_ts(v) or now) >= cutoff
    }


def _exit_completion_key(alert: dict) -> str:
    """구버전의 감지-only `exit:` 키와 분리한 실제 전송 완료 키."""
    return f"exit_sent:{alert['symbol']}:{alert['type']}"


def _exit_reservation_key(alert: dict) -> str:
    return f"exit_reserved:{alert['symbol']}:{alert['type']}"


def _market_day(symbol: str, value: datetime):
    tz = KST if symbol.endswith((".KS", ".KQ")) or symbol.isdigit() else US_EASTERN
    return value.astimezone(tz).date()


def _exit_completed(state: dict, key: str, symbol: str, now: datetime) -> bool:
    ts = _parse_ts(state.get("alerted", {}).get(key, ""))
    return bool(ts and _market_day(symbol, ts) == _market_day(symbol, now))


def _exit_reserved(state: dict, key: str, symbol: str, now: datetime) -> bool:
    reservations = state.get("reservations")
    if not isinstance(reservations, dict):
        return False
    ts = _parse_ts(reservations.get(key, ""))
    if ts is None or _market_day(symbol, ts) != _market_day(symbol, now):
        return False
    age = now.astimezone(KST) - ts
    return age < timedelta(minutes=EXIT_RESERVATION_LEASE_MINUTES)


def _reserve_exit(state: dict, key: str, now: datetime) -> None:
    reservations = state.setdefault("reservations", {})
    reservations[key] = now.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    cutoff = now - timedelta(days=7)
    state["reservations"] = {
        k: v for k, v in reservations.items()
        if (_parse_ts(v) or now) >= cutoff
    }


def _release_exit_reservation(state: dict, key: str) -> None:
    reservations = state.get("reservations")
    if isinstance(reservations, dict):
        reservations.pop(key, None)


def _promotion_sent(result: dict | None) -> bool:
    return bool(
        type(result) is dict
        and result.get("ok") is True
        and result.get("live_order_sent") is True
    )


# ── A-1: 미체결 주문 감시 ─────────────────────────────────────────

def check_stale_open_orders(
    now: datetime | None = None,
    list_orders_fn=None,
    stale_minutes: int = STALE_OPEN_ORDER_MINUTES,
) -> list[dict]:
    """브로커 OPEN 주문 중 stale_minutes 초과 미체결 건 반환 (read-only)."""
    now = now or _now_kst()
    if list_orders_fn is None:
        from core.toss_live_order_http import list_orders
        list_orders_fn = list_orders

    res = list_orders_fn("OPEN")
    if not res.get("ok"):
        log.debug("open orders 조회 실패: %s", res.get("reason"))
        return []

    alerts: list[dict] = []
    for row in res.get("orders") or []:
        ordered_at = _parse_ts(row.get("ordered_at", ""))
        if ordered_at is None:
            # 주문시각을 알 수 없으면 보수적으로 알림 대상에 포함하지 않음
            continue
        age_min = (now - ordered_at).total_seconds() / 60
        if age_min < stale_minutes:
            continue
        alerts.append({
            "type": "stale_open_order",
            "broker_order_id": str(row.get("broker_order_id", "")),
            "symbol": str(row.get("symbol", "")),
            "side": str(row.get("side", "")),
            "quantity": row.get("quantity", 0),
            "age_minutes": round(age_min),
            "ordered_at": ordered_at.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        })
    return alerts


# ── A-2: exit 레벨 감시 ──────────────────────────────────────────

def _default_price_fn(symbol: str) -> float:
    try:
        from core.market import _get_quote_realtime
        q = _get_quote_realtime(symbol)
        return float(q.price) if q else 0.0
    except Exception:
        return 0.0


def _as_float(value) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _exit_check_all_holdings() -> bool:
    """env TOSS_EXIT_CHECK_ALL_HOLDINGS: 실보유 전 종목의 손절 커버리지 보장.
    기본 OFF. 켜면 최근 ledger 창(100건)을 벗어난 보유 포지션도 손절 체크."""
    return str(os.getenv("TOSS_EXIT_CHECK_ALL_HOLDINGS", "")).strip().lower() in ("1", "true", "yes", "on")


def _held_symbols_for_exit() -> dict[str, float]:
    """실보유 심볼 → 수량 (홀딩맵). 조회 실패 시 {} (fail-safe)."""
    try:
        from core.dashboard_data import _toss_holding_price_map
        out = {}
        for s, v in _toss_holding_price_map().items():
            q = _as_float((v or {}).get("quantity"))
            if q > 0:
                out[str(s).upper().strip()] = q
        return out
    except Exception as e:
        log.debug("held symbols lookup failed: %s", e)
        return {}


def _recent_live_buy_record(symbol: str, records: list[dict]) -> dict | None:
    """해당 심볼의 최신 live_sent 매수 레코드 (없으면 넓은 창에서 조회)."""
    sym = str(symbol).upper().strip()
    for r in records:
        if (str(r.get("symbol", "")).upper().strip() == sym
                and r.get("status") == "live_sent"
                and str(r.get("side", "buy")).lower() == "buy"):
            return r
    try:
        from core.toss_live_pilot_ledger import list_live_pilot_records
        for r in list_live_pilot_records(limit=400):
            if (str(r.get("symbol", "")).upper().strip() == sym
                    and r.get("status") == "live_sent"
                    and str(r.get("side", "buy")).lower() == "buy"):
                return r
    except Exception:
        pass
    return None


def _record_to_exit_alert(r: dict, price_fn) -> dict | None:
    """단일 매수 레코드 → 손절/익절 도달 시 alert (없으면 None)."""
    entry = _as_float(r.get("limit_price"))
    reason = str(r.get("reason") or "").lower()
    income_managed = "auto_pipeline" in reason or "income" in reason
    stop = _as_float(r.get("stop_loss"))
    target = _as_float(r.get("target_price"))
    if income_managed and entry > 0:
        stop = round(entry * (1.0 - 0.025), 6)
        target = round(entry * (1.0 + 0.015), 6)
    if stop <= 0 and target <= 0:
        return None
    symbol = str(r.get("symbol", ""))
    price = price_fn(symbol)
    if price <= 0:
        return None
    base = {
        "pilot_id": str(r.get("pilot_id", "")),
        "symbol": symbol,
        "current_price": price,
        "entry_price": entry,
        "stop_loss": stop,
        "target_price": target,
        "quantity": int(_as_float(r.get("quantity"))),
        "income_managed": income_managed,
    }
    if stop > 0 and price <= stop:
        return {**base, "type": "stop_loss_hit"}
    if target > 0 and price >= target:
        return {**base, "type": "target_hit"}
    return None


def check_exit_levels(
    now: datetime | None = None,
    records: list[dict] | None = None,
    price_fn=None,
) -> list[dict]:
    """live_sent 매수 포지션의 손절/익절 레벨 도달 확인 (알림용, 자동매도 아님)."""
    now = now or _now_kst()
    price_fn = price_fn or _default_price_fn
    if records is None:
        from core.toss_live_pilot_ledger import list_live_pilot_records
        records = list_live_pilot_records(limit=100)

    cutoff = now - timedelta(days=EXIT_WATCH_LOOKBACK_DAYS)
    alerts: list[dict] = []
    seen_symbols: set[str] = set()

    for r in records:
        if r.get("status") != "live_sent" or str(r.get("side", "buy")).lower() != "buy":
            continue
        sent_at = _parse_ts(r.get("sent_at") or r.get("created_at") or "")
        if sent_at is None or sent_at < cutoff:
            continue
        symbol = str(r.get("symbol", ""))
        if not symbol or symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)

        entry = _as_float(r.get("limit_price"))
        reason = str(r.get("reason") or "").lower()
        income_managed = "auto_pipeline" in reason or "income" in reason
        stop = _as_float(r.get("stop_loss"))
        target = _as_float(r.get("target_price"))
        if income_managed and entry > 0:
            stop = round(entry * (1.0 - 0.025), 6)
            target = round(entry * (1.0 + 0.015), 6)
        if stop <= 0 and target <= 0:
            continue

        price = price_fn(symbol)
        if price <= 0:
            continue

        base = {
            "pilot_id": str(r.get("pilot_id", "")),
            "symbol": symbol,
            "current_price": price,
            "entry_price": entry,
            "stop_loss": stop,
            "target_price": target,
            "quantity": int(_as_float(r.get("quantity"))),
            "income_managed": income_managed,
        }
        if stop > 0 and price <= stop:
            alerts.append({**base, "type": "stop_loss_hit"})
        elif target > 0 and price >= target:
            alerts.append({**base, "type": "target_hit"})

    # #3 커버리지 (flag, 기본 OFF): 최근 ledger 창(100건)을 벗어난 실보유 포지션도
    # 손절 체크 → 손절 감지 누락 방지 (감지 안 되면 #1 체결개선도 못 터짐).
    if _exit_check_all_holdings():
        checked = {str(s).upper().strip() for s in seen_symbols}
        for sym in _held_symbols_for_exit():
            if sym in checked:
                continue
            rec = _recent_live_buy_record(sym, records)
            if rec is None:
                continue
            alert = _record_to_exit_alert(rec, price_fn)
            if alert is not None:
                alerts.append(alert)
    return alerts


# ── A-3: exit 자동 매도 승격 (autonomous mode 전용) ──────────────

TARGET_PARTIAL_SELL_RATIO = 0.5  # 목표가 도달 시 분할 익절 비율


def _held_quantity(symbol: str) -> float:
    """Toss 실보유 수량 확인. 조회 실패/미보유 → 0 (fail-safe: 매도 안 함)."""
    try:
        from core.dashboard_data import _toss_holding_price_map
        row = _toss_holding_price_map().get(str(symbol).upper().strip())
        if not row:
            return 0.0
        return float(row.get("quantity") or 0)
    except Exception as e:
        log.debug("held quantity lookup failed %s: %s", symbol, e)
        return 0.0


def compute_exit_sell_quantity(alert: dict, held_qty: float) -> int:
    """자동 매도 수량: 손절=전량, 목표=분할 익절 (실보유 초과 금지)."""
    bought = int(_as_float(alert.get("quantity")))
    held = int(held_qty)
    base_qty = min(bought, held) if bought > 0 else held
    if base_qty <= 0:
        return 0
    if alert.get("type") == "target_hit":
        if alert.get("income_managed") is True:
            return base_qty
        return max(1, int(base_qty * TARGET_PARTIAL_SELL_RATIO))
    return base_qty


def _stop_exit_aggressive_pct() -> float:
    """env TOSS_STOP_EXIT_AGGRESSIVE_PCT: 손절 청산 시 현재가보다 이 % 낮은 지정가로
    발주해 급락장에서도 체결되게 (marketable limit). 기본 0.0=OFF (현재가 지정가 유지).

    근거: 현재가 지정가 손절은 급락장에서 미체결 → 포지션이 -29%까지 흘러내림
    (실현 P&L -543k의 핵심 동인). 설정 시 손절만 공격적, 익절은 그대로.
    """
    return max(0.0, _as_float(os.getenv("TOSS_STOP_EXIT_AGGRESSIVE_PCT")))


def _exit_limit_price(alert: dict) -> float:
    """청산 지정가. stop_loss_hit는 flag 설정 시 현재가보다 공격적(체결 보장),
    그 외(target_hit 등)는 현재가 유지 (기본 동작 불변)."""
    current = _as_float(alert.get("current_price"))
    if current <= 0:
        return 0.0
    if str(alert.get("type")) == "stop_loss_hit":
        agg = _stop_exit_aggressive_pct()
        if agg > 0:
            return round(current * (1.0 - agg / 100.0), 6)
    return current


def _market_open_for_symbol(symbol: str, now: datetime) -> bool:
    from core.market_hours import is_kr_market_open, is_us_market_open
    if symbol.endswith((".KS", ".KQ")) or symbol.isdigit():
        return is_kr_market_open(now) is True
    return is_us_market_open(now) is True


def promote_exit_to_sell(alert: dict, policy: dict, now: datetime | None = None) -> dict:
    """exit 레벨 도달 → 자동 SELL 주문 경로 승격.

    preview → ledger → 검증 → 자동 판정 (PASS 시 finalizer 자동 발동)
    — buy 자율 파이프라인과 동일한 process_candidate 경로 재사용.

    가드:
    - autonomous_mode ON + kill switch OFF
    - autonomous_allowed_sides에 sell 포함 (env TOSS_AUTONOMOUS_ALLOWED_SIDES)
    - 해당 시장 정규장 시간
    - Toss 실보유 수량 확인 필수 (조회 실패 시 매도 안 함)
    """
    now = now or _now_kst()
    symbol = str(alert.get("symbol", ""))

    if not policy.get("autonomous_mode"):
        return {"symbol": symbol, "stage": "skipped", "reason": "autonomous_mode_disabled"}
    if policy.get("autonomous_kill_switch"):
        return {"symbol": symbol, "stage": "skipped", "reason": "kill_switch_active"}
    sides = [str(s).lower() for s in (policy.get("autonomous_allowed_sides") or [])]
    if "sell" not in sides:
        return {"symbol": symbol, "stage": "skipped", "reason": "sell_not_allowed_by_env"}
    if not _market_open_for_symbol(symbol, now):
        return {"symbol": symbol, "stage": "skipped", "reason": "market_closed"}

    held = _held_quantity(symbol)
    qty = compute_exit_sell_quantity(alert, held)
    if qty <= 0:
        return {
            "symbol": symbol, "stage": "skipped",
            "reason": f"no_confirmed_holding (held={held:g})",
        }

    exit_type = str(alert.get("type", ""))
    from core.toss_exit_execution_intent import build_exit_decision_ref
    intent_class = "full_exit" if qty >= int(held) else "partial_exit"
    candidate = {
        "symbol": symbol,
        "side": "sell",
        "quantity": qty,
        "limit_price": _exit_limit_price(alert),
        "stop_loss": alert.get("stop_loss"),
        "target_price": alert.get("target_price"),
        "decision_ref": build_exit_decision_ref(symbol, intent_class, now),
    }
    from core.toss_autonomous_pipeline import process_candidate
    result = process_candidate(
        candidate, policy,
        reason="auto_exit_sell",
        note=(
            f"exit_type={exit_type} entry={alert.get('entry_price')} "
            f"current={alert.get('current_price')} qty={qty}/{held:g}"
        ),
    )
    result["exit_type"] = exit_type
    result["sell_quantity"] = qty
    return result


# ── 메시지 조립 + 전송 ───────────────────────────────────────────

def _fmt_price(symbol: str, price: float) -> str:
    if symbol.endswith((".KS", ".KQ")):
        return f"{price:,.0f}원"
    return f"${price:,.2f}"


def format_watch_message(
    stale: list[dict],
    exits: list[dict],
    promotions: dict[str, dict] | None = None,
) -> str:
    """알림 메시지 구성. 알림 없으면 빈 문자열.

    promotions: {f"{symbol}:{type}": promote_exit_to_sell 결과} — 자동 매도 승격 결과 표시.
    """
    promotions = promotions or {}
    lines: list[str] = []
    if stale:
        lines.append("⏳ [Toss 미체결 주문 감시]")
        for a in stale:
            lines.append(
                f"- {a['symbol']} {a['side'].upper()} {a['quantity']:g}주 — "
                f"{a['age_minutes']}분째 미체결 (주문 {a['ordered_at'][11:16]})"
            )
        lines.append("→ 취소/정정 여부 직접 판단 필요 (자동 취소 안 함)")
    if exits:
        if lines:
            lines.append("")
        lines.append("🎯 [Toss exit 레벨 도달]")
        manual_needed = False
        for a in exits:
            label = "🔻 손절가 도달" if a["type"] == "stop_loss_hit" else "🔺 목표가 도달"
            level = a["stop_loss"] if a["type"] == "stop_loss_hit" else a["target_price"]
            lines.append(
                f"- {a['symbol']} {label}: 현재 {_fmt_price(a['symbol'], a['current_price'])} "
                f"(레벨 {_fmt_price(a['symbol'], level)})"
            )
            promo = promotions.get(f"{a['symbol']}:{a['type']}")
            if promo:
                if _promotion_sent(promo):
                    kind = "전량 손절" if a["type"] == "stop_loss_hit" else "분할 익절"
                    lines.append(
                        f"  → 🤖 자동 매도 전송 완료 ({kind} {promo.get('sell_quantity', 0)}주)"
                    )
                elif promo.get("stage") == "skipped":
                    lines.append(f"  → 자동 매도 스킵: {promo.get('reason', '')}")
                    manual_needed = True
                else:
                    lines.append(
                        f"  → 자동 매도 미실행 ({promo.get('stage', '')}: "
                        f"{str(promo.get('reason', ''))[:80]})"
                    )
                    manual_needed = True
            else:
                manual_needed = True
        if manual_needed:
            lines.append("→ 매도 여부 직접 판단 필요")
    return "\n".join(lines)


def run_toss_order_watch(
    now: datetime | None = None,
    send: bool = True,
    force: bool = False,
) -> dict:
    """감시 사이클 실행. exit는 durable reservation 후에만 finalizer로 보낸다."""
    now = now or _now_kst()
    state = _load_state()
    if type(state) is not dict or state.get("_state_load_failed") is True:
        return {
            "ok": False,
            "reason": "state_load_failed",
            "stale_count": 0,
            "exit_count": 0,
            "deferred_exit_count": 0,
            "promotions": {},
            "message": "",
            "sent": False,
        }

    try:
        from core.market_hours import is_any_market_open
        market_open_now = is_any_market_open(now) is True
    except Exception as e:
        log.warning("watch market transition gate 실패: %s", e)
        market_open_now = False

    last_run = _parse_ts(state.get("last_run", ""))
    opening_transition = (
        market_open_now
        and state.get("last_market_open") is not True
    )
    if (
        not force
        and not opening_transition
        and last_run
        and (now - last_run) < timedelta(minutes=WATCH_INTERVAL_MINUTES)
    ):
        return {"ok": True, "skipped": "throttled"}

    state["last_run"] = now.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    state["last_market_open"] = market_open_now

    try:
        stale = check_stale_open_orders(now=now)
    except Exception as e:
        log.warning("stale order check 실패: %s", e)
        stale = []
    try:
        exits = check_exit_levels(now=now)
    except Exception as e:
        log.warning("exit level check 실패: %s", e)
        exits = []

    # 장외 stop/target은 감지만 남기고 reservation/completion dedup을 소비하지 않는다.
    deferred_exits = []
    market_open_exits = []
    for alert in exits:
        try:
            if _market_open_for_symbol(str(alert.get("symbol") or ""), now):
                market_open_exits.append(alert)
            else:
                deferred_exits.append(alert)
        except Exception as e:
            log.warning("exit market gate 실패 %s: %s", alert.get("symbol"), e)
            deferred_exits.append(alert)

    new_stale = []
    for alert in stale:
        key = f"stale:{alert['broker_order_id'] or alert['symbol']}"
        if not _already_alerted(state, key, now):
            _mark_alerted(state, key, now)
            new_stale.append(alert)

    new_exits = []
    for alert in market_open_exits:
        completion_key = _exit_completion_key(alert)
        reservation_key = _exit_reservation_key(alert)
        if _exit_completed(state, completion_key, alert["symbol"], now):
            continue
        if _exit_reserved(state, reservation_key, alert["symbol"], now):
            continue
        _reserve_exit(state, reservation_key, now)
        new_exits.append(alert)

    # 주문 경로 진입 전 reservation이 durable해야 한다. 실패 시 submit 0.
    if not _save_state(state):
        return {
            "ok": False,
            "reason": "state_reservation_failed" if new_exits else "state_save_failed",
            "stale_count": len(new_stale),
            "exit_count": 0,
            "deferred_exit_count": len(deferred_exits),
            "promotions": {},
            "message": "",
            "sent": False,
        }

    promotions: dict[str, dict] = {}
    if new_exits:
        try:
            from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
            policy = compute_toss_live_pilot_policy()
        except Exception as e:
            log.warning("exit 자동 매도 policy 조회 실패: %s", e)
            policy = None
            for alert in new_exits:
                key = f"{alert['symbol']}:{alert['type']}"
                promotions[key] = {
                    "ok": False,
                    "live_order_sent": False,
                    "symbol": alert["symbol"],
                    "stage": "error",
                    "reason": f"policy_unavailable:{type(e).__name__}",
                }

        if policy is not None:
            for alert in new_exits:
                key = f"{alert['symbol']}:{alert['type']}"
                try:
                    promotions[key] = promote_exit_to_sell(alert, policy, now=now)
                except Exception as e:
                    log.warning("exit 자동 매도 승격 실패 %s: %s", key, e)
                    promotions[key] = {
                        "ok": False,
                        "live_order_sent": False,
                        "symbol": alert["symbol"],
                        "stage": "error",
                        "reason": str(e)[:200],
                    }

    # 실제 live send만 completion dedup으로 승격한다. 실패는 reservation을 해제해 재시도한다.
    for alert in new_exits:
        key = f"{alert['symbol']}:{alert['type']}"
        result = promotions.get(key)
        if _promotion_sent(result):
            _mark_alerted(state, _exit_completion_key(alert), now)
        _release_exit_reservation(state, _exit_reservation_key(alert))

    state_saved = _save_state(state)
    if not state_saved:
        # 직전 durable reservation이 디스크에 남으므로 다음 사이클 중복 submit은 차단된다.
        log.error("exit 결과 state finalize 실패 — durable reservation 유지")

    message = format_watch_message(new_stale, new_exits, promotions)
    sent = False
    if message and send:
        try:
            from core.telegram import send_simple_message
            sent = send_simple_message(message)
        except Exception as e:
            log.warning("toss watch 알림 전송 실패: %s", e)

    return {
        "ok": state_saved,
        "reason": "" if state_saved else "state_finalize_failed",
        "stale_count": len(new_stale),
        "exit_count": len(new_exits),
        "deferred_exit_count": len(deferred_exits),
        "promotions": promotions,
        "message": message,
        "sent": sent,
    }
