"""core/toss_order_watch.py

Toss 미체결 주문 + exit 레벨 감시 (read-only, 알림 전용).

A-1: 브로커 OPEN 주문 중 장시간 미체결 건 → 텔레그램 알림
A-2: live_sent 매수 포지션의 stop_loss/target_price 도달 → 텔레그램 알림

원칙:
- 주문/취소/정정 실행 없음 — GET 조회 + 알림만. (자동 매도/취소는 검증 후 별도 승인)
- 알림 중복 방지: db/data/toss_order_watch_state.json (키별 1일 1회)
- 민감정보(계좌/토큰) 저장·출력 금지
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
log = logging.getLogger(__name__)

# 미체결 판정 기준 (분)
STALE_OPEN_ORDER_MINUTES = 60
# 감시 주기 (분) — monitor 루프에서 매 사이클 호출돼도 이 간격으로 스로틀
WATCH_INTERVAL_MINUTES = 30
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
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    p = _state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log.warning("toss_order_watch state save failed: %s", e)


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

        stop = _as_float(r.get("stop_loss"))
        target = _as_float(r.get("target_price"))
        if stop <= 0 and target <= 0:
            continue

        price = price_fn(symbol)
        if price <= 0:
            continue

        entry = _as_float(r.get("limit_price"))
        base = {
            "pilot_id": str(r.get("pilot_id", "")),
            "symbol": symbol,
            "current_price": price,
            "entry_price": entry,
            "stop_loss": stop,
            "target_price": target,
        }
        if stop > 0 and price <= stop:
            alerts.append({**base, "type": "stop_loss_hit"})
        elif target > 0 and price >= target:
            alerts.append({**base, "type": "target_hit"})
    return alerts


# ── 메시지 조립 + 전송 ───────────────────────────────────────────

def _fmt_price(symbol: str, price: float) -> str:
    if symbol.endswith((".KS", ".KQ")):
        return f"{price:,.0f}원"
    return f"${price:,.2f}"


def format_watch_message(stale: list[dict], exits: list[dict]) -> str:
    """알림 메시지 구성. 알림 없으면 빈 문자열."""
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
        for a in exits:
            label = "🔻 손절가 도달" if a["type"] == "stop_loss_hit" else "🔺 목표가 도달"
            level = a["stop_loss"] if a["type"] == "stop_loss_hit" else a["target_price"]
            lines.append(
                f"- {a['symbol']} {label}: 현재 {_fmt_price(a['symbol'], a['current_price'])} "
                f"(레벨 {_fmt_price(a['symbol'], level)})"
            )
        lines.append("→ 매도 여부 직접 판단 필요 (자동 매도 안 함)")
    return "\n".join(lines)


def run_toss_order_watch(
    now: datetime | None = None,
    send: bool = True,
    force: bool = False,
) -> dict:
    """감시 사이클 실행. monitor 루프에서 호출 — 내부 스로틀 적용."""
    now = now or _now_kst()
    state = _load_state()

    last_run = _parse_ts(state.get("last_run", ""))
    if not force and last_run and (now - last_run) < timedelta(minutes=WATCH_INTERVAL_MINUTES):
        return {"ok": True, "skipped": "throttled"}

    state["last_run"] = now.strftime("%Y-%m-%dT%H:%M:%S+09:00")

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

    # 중복 알림 제거 (키별 1일 1회)
    new_stale = []
    for a in stale:
        key = f"stale:{a['broker_order_id'] or a['symbol']}"
        if not _already_alerted(state, key, now):
            _mark_alerted(state, key, now)
            new_stale.append(a)
    new_exits = []
    for a in exits:
        key = f"exit:{a['symbol']}:{a['type']}"
        if not _already_alerted(state, key, now):
            _mark_alerted(state, key, now)
            new_exits.append(a)

    _save_state(state)

    message = format_watch_message(new_stale, new_exits)
    sent = False
    if message and send:
        try:
            from core.telegram import send_simple_message
            sent = send_simple_message(message)
        except Exception as e:
            log.warning("toss watch 알림 전송 실패: %s", e)

    return {
        "ok": True,
        "stale_count": len(new_stale),
        "exit_count": len(new_exits),
        "message": message,
        "sent": sent,
    }
