"""core/toss_live_pilot_events.py

Live pilot callback 이벤트 로그.

버튼 callback(review/cancel/confirm)의 결과를 DB에 기록해
Hermes가 GET polling으로 확인할 수 있게 한다.

event_type:
  preview_created          | 미리보기 생성
  reviewed                 | 검토 완료
  cancelled                | 취소
  confirm_attempted        | 최종 승인 시도
  confirm_blocked_hermes   | Hermes 검증 미완료/미통과로 차단
  confirm_blocked_policy   | Hermes PASS + live pilot 조건 미충족으로 차단
  confirm_blocked_transport| Hermes PASS + transport 미설정으로 차단
  confirm_blocked_quality  | exact quality row 누락/불일치로 차단
  confirmed_but_not_sent   | Hermes PASS + guard 통과 + 최종 차단 (adapter 등)
  live_send_blocked        | guard 차단
  live_sent                | 진짜 live 전송 성공 (adapter enabled + live_order_allowed + sent)
  live_sent_artifact       | test/mock/리허설 전송 — production live_sent로 카운트 금지
  live_send_failed         | 전송 시도 후 실패
  autonomous_blocked_quality | exact quality row 누락/불일치로 자율주문 차단
  autonomous_send_retryable | 자율주문 전송 실패이나 재시도 가능

[오염 방지 invariant]
  - adapter_status != enabled 또는 live_order_allowed=false인 live_sent는
    record 시점에 live_sent_artifact로 강등 (production summary 오염 방지).
  - live_order_sent_total / live_sent_real은 진짜 게이트 통과 row만 카운트.

금지:
  - row 삭제 없음
  - 민감정보 없음 (accountNo/token/key/secret)
  - broker raw response 저장 금지
  - live_order_allowed 변경 금지
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from core.toss_live_pilot_hermes_bridge import SYMBOL_NAMES, get_symbol_display

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
_DECISION_REF_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_CLIENT_ORDER_ID_RE = re.compile(r"^tlive_[A-Za-z0-9_-]{1,30}$")
_DECISION_REF_PREFIXES = ("prediction:", "execution_decision:")
_REAL_LIVE_EVENT_TYPES = frozenset({"live_sent", "autonomous_live_sent"})
_BEARER_SECRET_RE = re.compile(
    r"(?i)\bbearer\s+(?!\[REDACTED\])[^\s,;]+"
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?ix)\b(?:"
    r"access[_-]?token|refresh[_-]?token|token|secret|password|authorization|"
    r"api[_-]?key|app[_-]?(?:key|secret)|client[_-]?secret|credential(?:s)?|"
    r"account[_-]?(?:no|number|id|seq)|connection[_-]?string"
    r")\b[\"']?\s*[:=]\s*[^\s,;]+"
)
_NAKED_SECRET_RE = re.compile(
    r"(?ix)(?:"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b|"
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bhf_[A-Za-z0-9]{20,}\b|"
    r"\bsk-(?:live-)?[A-Za-z0-9_-]{20,}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"\bAIza[0-9A-Za-z_-]{30,}\b|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r")"
)

_VALID_EVENT_TYPES = frozenset([
    "preview_created",
    "reviewed",
    "cancelled",
    "confirm_attempted",
    "confirm_blocked_hermes",
    "confirm_blocked_policy",
    "confirm_blocked_transport",
    "confirm_blocked_quality",
    "confirmed_but_not_sent",
    "live_send_blocked",
    "live_sent",
    "live_sent_artifact",
    "live_send_failed",
    # autonomous finalizer 이벤트
    "autonomous_blocked_hermes",
    "autonomous_blocked_policy",
    "autonomous_blocked_guard",
    "autonomous_blocked_quality",
    "autonomous_live_sent",
    "autonomous_send_failed",
    "autonomous_send_retryable",
])


def _db_path() -> Path:
    try:
        from db.store import DB_DIR
        return DB_DIR / "toss_live_pilot_events.db"
    except Exception:
        return Path("db/data/toss_live_pilot_events.db")


_db_lock = threading.Lock()
_schema_created = False


def _conn() -> sqlite3.Connection:
    global _schema_created
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5.0)
    conn.row_factory = sqlite3.Row
    if _schema_created:
        return conn

    migration_columns = (
        ("event_id", "TEXT DEFAULT ''"),
        ("pilot_id", "TEXT NOT NULL DEFAULT ''"),
        ("preview_id", "TEXT DEFAULT ''"),
        ("verification_id", "TEXT DEFAULT ''"),
        ("decision_ref", "TEXT DEFAULT ''"),
        ("event_type", "TEXT NOT NULL DEFAULT ''"),
        ("status", "TEXT NOT NULL DEFAULT ''"),
        ("symbol", "TEXT DEFAULT ''"),
        ("symbol_name", "TEXT DEFAULT ''"),
        ("symbol_label", "TEXT DEFAULT ''"),
        ("side", "TEXT DEFAULT 'buy'"),
        ("quantity", "INTEGER DEFAULT 0"),
        ("limit_price", "REAL DEFAULT 0"),
        ("estimated_amount_krw", "REAL DEFAULT 0"),
        ("live_order_sent", "INTEGER DEFAULT 0"),
        ("adapter_status", "TEXT DEFAULT 'disabled'"),
        ("live_order_allowed", "INTEGER DEFAULT 0"),
        ("reason", "TEXT DEFAULT ''"),
        ("message", "TEXT DEFAULT ''"),
        ("broker_order_id", "TEXT DEFAULT ''"),
        ("broker_order_status", "TEXT DEFAULT ''"),
        ("filled_quantity", "REAL DEFAULT 0"),
        ("filled_price", "REAL DEFAULT 0"),
        ("fill_updated_at", "TEXT DEFAULT ''"),
        ("error_body", "TEXT DEFAULT ''"),
        ("order_request_preview", "TEXT DEFAULT ''"),
        ("created_at", "TEXT NOT NULL DEFAULT ''"),
        ("delivered_to_hermes", "INTEGER DEFAULT 0"),
    )
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS live_pilot_events (
                event_id            TEXT PRIMARY KEY,
                pilot_id            TEXT NOT NULL,
                preview_id          TEXT DEFAULT '',
                verification_id     TEXT DEFAULT '',
                decision_ref        TEXT DEFAULT '',
                event_type          TEXT NOT NULL,
                status              TEXT NOT NULL,
                symbol              TEXT DEFAULT '',
                symbol_name         TEXT DEFAULT '',
                symbol_label        TEXT DEFAULT '',
                side                TEXT DEFAULT 'buy',
                quantity            INTEGER DEFAULT 0,
                limit_price         REAL DEFAULT 0,
                estimated_amount_krw REAL DEFAULT 0,
                live_order_sent     INTEGER DEFAULT 0,
                adapter_status      TEXT DEFAULT 'disabled',
                live_order_allowed  INTEGER DEFAULT 0,
                reason              TEXT DEFAULT '',
                message             TEXT DEFAULT '',
                broker_order_id     TEXT DEFAULT '',
                broker_order_status TEXT DEFAULT '',
                filled_quantity     REAL DEFAULT 0,
                filled_price        REAL DEFAULT 0,
                fill_updated_at     TEXT DEFAULT '',
                error_body          TEXT DEFAULT '',
                order_request_preview TEXT DEFAULT '',
                created_at          TEXT NOT NULL,
                delivered_to_hermes INTEGER DEFAULT 0
            )
        """)
        existing = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(live_pilot_events)"
            ).fetchall()
        }
        for col, defn in migration_columns:
            if col in existing:
                continue
            try:
                conn.execute(
                    f"ALTER TABLE live_pilot_events ADD COLUMN {col} {defn}"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_live_events_event_id_exact "
            "ON live_pilot_events(event_id) WHERE event_id <> ''"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_live_events_pilot_id "
            "ON live_pilot_events(pilot_id)"
        )
        conn.commit()
        verified = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(live_pilot_events)"
            ).fetchall()
        }
        missing = {col for col, _ in migration_columns} - verified
        indexes = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA index_list(live_pilot_events)"
            ).fetchall()
        }
        if missing or "idx_live_events_event_id_exact" not in indexes:
            raise RuntimeError(
                "live_pilot_events_schema_incomplete:"
                + ",".join(sorted(missing))
            )
        _schema_created = True
        return conn
    except Exception:
        conn.rollback()
        conn.close()
        _schema_created = False
        raise


def _now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")


def _gen_event_id() -> str:
    ts = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    seq = random.randint(1000, 9999)
    return f"tle_{ts}_{seq}"


# ─── 이벤트 기록 ─────────────────────────────────────────

def _stringify_event_diagnostic(value, limit: int = 600) -> str:
    """Store only short, sanitized diagnostic strings in event rows."""
    if value in (None, ""):
        return ""
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    return text[:limit]


def _contains_sensitive_event_value(value: object) -> bool:
    if value in (None, ""):
        return False
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    return bool(
        _BEARER_SECRET_RE.search(text)
        or _SENSITIVE_ASSIGNMENT_RE.search(text)
        or _NAKED_SECRET_RE.search(text)
    )


def _nonnegative_finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def record_event(
    pilot_id: str,
    event_type: str,
    status: str,
    *,
    preview_id: str = "",
    verification_id: str = "",
    decision_ref: str = "",
    symbol: str = "",
    side: str = "buy",
    quantity: int = 0,
    limit_price: float = 0.0,
    estimated_amount_krw: float = 0.0,
    live_order_sent: bool = False,
    adapter_status: str = "disabled",
    live_order_allowed: bool = False,
    reason: str = "",
    message: str = "",
    broker_order_id: str = "",
    broker_order_status: str = "",
    filled_quantity: float = 0.0,
    filled_price: float = 0.0,
    error_body: str = "",
    order_request_preview="",
) -> dict:
    """Live pilot 이벤트 기록.

    민감정보(accountNo/token/key/secret/broker raw) 포함 금지.

    안전 invariant:
        live_sent는 (adapter_status='enabled' + live_order_allowed=true +
        live_order_sent=true)일 때만 진짜 live_sent로 기록한다. 그 외의
        live_sent는 test/mock/리허설 artifact이므로 live_sent_artifact로
        강등하여 production summary/ledger 오염을 막는다.
        production callback 경로(transport=None)는 애초에 live_sent를 만들지
        못하므로, 이 강등은 fake transport 테스트 등에 대한 2차 방어막이다.

    Returns:
        {"ok": bool, "event_id": str, "event_type": str}
    """
    if event_type not in _VALID_EVENT_TYPES:
        log.warning("invalid event_type")
        return {"ok": False, "reason": "invalid_event_type"}

    if type(live_order_sent) is not bool or type(live_order_allowed) is not bool:
        return {"ok": False, "reason": "live_boolean_contract_invalid"}

    numeric_values = tuple(
        _nonnegative_finite(value)
        for value in (
            quantity,
            limit_price,
            estimated_amount_krw,
            filled_quantity,
            filled_price,
        )
    )
    if (
        any(value is None for value in numeric_values)
        or numeric_values[0] != int(numeric_values[0] or 0)
        or (
            (numeric_values[0] or 0) > 0
            and (numeric_values[3] or 0) > (numeric_values[0] or 0)
        )
    ):
        return {"ok": False, "reason": "event_numeric_contract_invalid"}
    quantity = int(numeric_values[0] or 0)
    limit_price = float(numeric_values[1] or 0)
    estimated_amount_krw = float(numeric_values[2] or 0)
    filled_quantity = float(numeric_values[3] or 0)
    filled_price = float(numeric_values[4] or 0)

    decision_ref = str(decision_ref or "")
    if decision_ref and (
        len(decision_ref) > 160
        or not decision_ref.startswith(_DECISION_REF_PREFIXES)
        or not _DECISION_REF_RE.fullmatch(decision_ref)
    ):
        return {"ok": False, "reason": "invalid_decision_ref"}

    error_body_text = _stringify_event_diagnostic(error_body, limit=600)
    order_request_preview_text = _stringify_event_diagnostic(order_request_preview, limit=600)

    # 민감정보 가드 — 키 이름·대소문자·중첩 JSON 위치와 무관하게 fail-closed.
    sensitive_fields = (
        pilot_id,
        event_type,
        status,
        preview_id,
        verification_id,
        decision_ref,
        symbol,
        side,
        adapter_status,
        reason,
        message,
        broker_order_id,
        broker_order_status,
        error_body_text,
        order_request_preview_text,
    )
    if any(_contains_sensitive_event_value(field) for field in sensitive_fields):
        log.warning("민감정보 감지 in event fields")
        return {"ok": False, "reason": "sensitive_field"}

    # 안전 invariant: 진짜 live_sent 판별 + artifact 강등
    is_real_live_sent = (
        event_type in _REAL_LIVE_EVENT_TYPES
        and bool(live_order_sent)
        and adapter_status == "enabled"
        and bool(live_order_allowed)
    )
    if event_type in _REAL_LIVE_EVENT_TYPES and not is_real_live_sent:
        event_type = "live_sent_artifact"
        log.info(
            "live_sent 강등 → live_sent_artifact: adapter=%s allowed=%s "
            "(production live_sent 아님)",
            adapter_status, bool(live_order_allowed),
        )
    stored_allowed = 1 if is_real_live_sent else 0

    event_id = _gen_event_id()
    symbol_name = SYMBOL_NAMES.get(symbol, "")
    symbol_label = get_symbol_display(symbol)
    now = _now_kst()
    fill_updated_at = (
        now
        if _positive_finite(filled_quantity) and _positive_finite(filled_price)
        else ""
    )

    try:
        with _db_lock:
            conn = _conn()
            conn.execute(
                """INSERT INTO live_pilot_events
                   (event_id, pilot_id, preview_id, verification_id, decision_ref,
                    event_type, status, symbol, symbol_name, symbol_label,
                    side, quantity, limit_price, estimated_amount_krw,
                    live_order_sent, adapter_status, live_order_allowed,
                    reason, message, broker_order_id, broker_order_status,
                    filled_quantity, filled_price, fill_updated_at,
                    error_body, order_request_preview,
                     created_at, delivered_to_hermes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id, pilot_id, preview_id, verification_id, decision_ref,
                    event_type, status, symbol, symbol_name, symbol_label,
                    side, quantity, limit_price, estimated_amount_krw,
                    1 if live_order_sent else 0, adapter_status, stored_allowed,
                    reason, message, broker_order_id, broker_order_status,
                    float(filled_quantity or 0), float(filled_price or 0),
                    fill_updated_at, error_body_text, order_request_preview_text, now, 0,
                ),
            )
            conn.commit()
            conn.close()
    except Exception as exc:
        log.warning("record_event failed: %s", type(exc).__name__)
        return {"ok": False, "reason": "record_event_failed"}

    return {
        "ok": True,
        "event_id": event_id,
        "event_type": event_type,
        "status": status,
        "pilot_id": pilot_id,
        "decision_ref": decision_ref,
        "symbol_label": symbol_label,
        "live_order_sent": live_order_sent,
        "live_order_allowed": bool(is_real_live_sent),
        "is_real_live_sent": bool(is_real_live_sent),
        "broker_order_id": broker_order_id,
        "broker_order_status": broker_order_status,
        "filled_quantity": float(filled_quantity or 0),
        "filled_price": float(filled_price or 0),
        "fill_updated_at": fill_updated_at,
        "error_body": error_body_text,
        "order_request_preview": order_request_preview_text,
        "created_at": now,
    }


# ─── exact broker fill 동기화 ─────────────────────────────

def _symbol_base(value: object) -> str:
    symbol = str(value or "").upper().strip()
    for suffix in (".KS", ".KQ"):
        if symbol.endswith(suffix):
            return symbol[:-3]
    return symbol


def _positive_finite(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _broker_timestamp(row: dict) -> datetime | None:
    raw = str(
        row.get("filled_at")
        or row.get("ordered_at")
        or row.get("created_at")
        or ""
    ).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


_STATUS_STAGES = {
    "NEW": 0,
    "PENDING": 0,
    "OPEN": 0,
    "RECEIVED": 0,
    "ACCEPTED": 0,
    "PARTIAL": 1,
    "PARTIALLY_FILLED": 1,
    "FILLED": 2,
    "COMPLETED": 2,
    "EXECUTED": 2,
    "DONE": 2,
    "CLOSED": 2,
    "CANCELED": 2,
    "CANCELLED": 2,
    "REJECTED": 2,
    "FAILED": 2,
    "EXPIRED": 2,
}


def _status_transition_ok(previous: object, current: object) -> bool:
    old = str(previous or "").upper().strip()
    new = str(current or "").upper().strip()
    if old == new:
        return True
    if not old:
        return True
    old_stage = _STATUS_STAGES.get(old)
    new_stage = _STATUS_STAGES.get(new)
    if old_stage is None or new_stage is None or old_stage == 2:
        return False
    return new_stage >= old_stage


def sync_live_event_fills_from_broker_orders(broker_orders: object) -> dict:
    """Exact broker client ID로 기존 production event의 fill만 monotonic 보강."""
    result = {"updated": 0, "ambiguous": 0, "rejected": 0}
    if not isinstance(broker_orders, list):
        return result

    grouped: dict[str, list[dict]] = {}
    for broker in broker_orders:
        if not isinstance(broker, dict):
            result["rejected"] += 1
            continue
        pilot_id = str(broker.get("client_order_id") or "")
        symbol = _symbol_base(broker.get("symbol") or broker.get("ticker"))
        side = str(broker.get("side") or "").lower().strip()
        if (
            not _CLIENT_ORDER_ID_RE.fullmatch(pilot_id)
            or not symbol
            or side not in {"buy", "sell"}
        ):
            result["rejected"] += 1
            continue
        filled_quantity = _positive_finite(broker.get("filled_quantity"))
        filled_price = _positive_finite(broker.get("filled_price"))
        order_quantity = _positive_finite(broker.get("quantity"))
        if not filled_quantity or not filled_price:
            continue
        if not order_quantity:
            result["rejected"] += 1
            continue
        row = dict(broker)
        row["_symbol"] = symbol
        row["_side"] = side
        row["_quantity"] = order_quantity
        row["_filled_quantity"] = filled_quantity
        row["_filled_price"] = filled_price
        grouped.setdefault(pilot_id, []).append(row)

    collapsed: list[tuple[str, dict]] = []
    for pilot_id, candidates in grouped.items():
        if len(candidates) == 1:
            collapsed.append((pilot_id, candidates[0]))
            continue
        signatures = {
            (row["_symbol"], row["_side"], row["_quantity"])
            for row in candidates
        }
        if len(signatures) != 1:
            result["ambiguous"] += 1
            continue
        payloads = {
            (
                row["_filled_quantity"],
                row["_filled_price"],
                str(row.get("broker_order_status") or "").upper(),
            )
            for row in candidates
        }
        if len(payloads) == 1:
            collapsed.append((pilot_id, candidates[0]))
            continue

        timeline = [(_broker_timestamp(row), row) for row in candidates]
        parsed_times = [stamp for stamp, _ in timeline]
        if (
            any(stamp is None for stamp in parsed_times)
            or len(set(parsed_times)) != len(parsed_times)
        ):
            result["ambiguous"] += 1
            continue
        timeline.sort(key=lambda item: item[0])
        valid_timeline = True
        for (_, previous), (_, current) in zip(timeline, timeline[1:]):
            if (
                current["_filled_quantity"] < previous["_filled_quantity"]
                or not _status_transition_ok(
                    previous.get("broker_order_status"),
                    current.get("broker_order_status"),
                )
            ):
                valid_timeline = False
                break
        if not valid_timeline:
            result["ambiguous"] += 1
            continue
        collapsed.append((pilot_id, timeline[-1][1]))

    with _db_lock:
        conn = _conn()
        try:
            for pilot_id, broker in collapsed:
                filled_quantity = float(broker["_filled_quantity"])
                filled_price = float(broker["_filled_price"])
                broker_quantity = float(broker["_quantity"])
                symbol = str(broker["_symbol"])
                side = str(broker["_side"])
                status = str(broker.get("broker_order_status") or "")[:80]
                resolved = False

                for _attempt in range(3):
                    rows = conn.execute(
                        "SELECT * FROM live_pilot_events WHERE pilot_id=? "
                        "AND event_type IN ('live_sent','autonomous_live_sent') "
                        "AND live_order_sent=1 AND adapter_status='enabled' "
                        "AND live_order_allowed=1",
                        (pilot_id,),
                    ).fetchall()
                    if len(rows) != 1:
                        if rows:
                            result["ambiguous"] += 1
                        else:
                            result["rejected"] += 1
                        resolved = True
                        break

                    event = dict(rows[0])
                    event_side = str(event.get("side") or "").lower().strip()
                    event_quantity = _positive_finite(event.get("quantity"))
                    old_filled = max(0.0, float(event.get("filled_quantity") or 0))
                    old_price = max(0.0, float(event.get("filled_price") or 0))
                    old_status = str(event.get("broker_order_status") or "")
                    if (
                        _symbol_base(event.get("symbol")) != symbol
                        or event_side != side
                        or not event_quantity
                        or broker_quantity != event_quantity
                        or filled_quantity > event_quantity
                        or filled_quantity < old_filled
                        or not _status_transition_ok(old_status, status)
                    ):
                        result["rejected"] += 1
                        resolved = True
                        break
                    if (
                        filled_quantity == old_filled
                        and filled_price == old_price
                        and status == old_status
                    ):
                        resolved = True
                        break

                    cursor = conn.execute(
                        "UPDATE live_pilot_events SET filled_quantity=?, filled_price=?, "
                        "broker_order_status=?, fill_updated_at=? "
                        "WHERE event_id=? "
                        "AND COALESCE(filled_quantity, 0)=? "
                        "AND COALESCE(filled_price, 0)=? "
                        "AND COALESCE(broker_order_status, '')=?",
                        (
                            filled_quantity,
                            filled_price,
                            status,
                            _now_kst(),
                            event["event_id"],
                            old_filled,
                            old_price,
                            old_status,
                        ),
                    )
                    if cursor.rowcount == 1:
                        conn.commit()
                        result["updated"] += 1
                        resolved = True
                        break
                    conn.rollback()

                if not resolved:
                    result["ambiguous"] += 1
            conn.commit()
        finally:
            conn.close()
    return result


# ─── 조회 ────────────────────────────────────────────────

def list_events(limit: int = 50) -> list[dict]:
    """최근 이벤트 조회 (최신순, read-only)."""
    try:
        with _db_lock:
            conn = _conn()
            rows = conn.execute(
                "SELECT * FROM live_pilot_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
        result = []
        for r in rows:
            d = dict(r)
            sent = bool(d.get("live_order_sent"))
            d["live_order_sent"] = sent
            et = d.get("event_type")
            real = (
                et in _REAL_LIVE_EVENT_TYPES
                and sent
                and d.get("adapter_status") == "enabled"
                and bool(d.get("live_order_allowed"))
            )
            # Hermes 필터 기준과 일치: real일 때만 true
            d["live_order_allowed"] = real
            if et in (*_REAL_LIVE_EVENT_TYPES, "live_sent_artifact") and sent:
                d["live_sent_classification"] = "real" if real else "mock_or_artifact"
            else:
                d["live_sent_classification"] = "n/a"
            result.append(d)
        return result
    except Exception as exc:
        log.warning("list_events failed: error_type=%s", type(exc).__name__)
        return []


def latest_fill_for_pilot(pilot_id: str) -> dict:
    """pilot_id의 최신 체결 정보 조회 (read-only).

    반환: {"filled_price": float, "filled_quantity": float, "broker_order_status": str}
    체결 정보 없으면 빈 dict.
    """
    pid = str(pilot_id or "").strip()
    if not pid:
        return {}
    try:
        with _db_lock:
            conn = _conn()
            row = conn.execute(
                "SELECT filled_price, filled_quantity, broker_order_status "
                "FROM live_pilot_events "
                "WHERE pilot_id=? AND event_type IN ('live_sent','autonomous_live_sent') "
                "AND filled_price > 0 AND filled_quantity > 0 "
                "ORDER BY created_at DESC LIMIT 1",
                (pid,),
            ).fetchone()
            conn.close()
        if not row:
            return {}
        return {
            "filled_price": float(row["filled_price"] or 0),
            "filled_quantity": float(row["filled_quantity"] or 0),
            "broker_order_status": str(row["broker_order_status"] or ""),
        }
    except Exception as exc:
        log.warning(
            "latest_fill_for_pilot failed: error_type=%s",
            type(exc).__name__,
        )
        return {}


def event_summary() -> dict:
    """이벤트 타입별 건수 요약 (read-only).

    production live_sent 오염 방지를 위해 real / mock_or_artifact를 분리한다.
    - live_sent_real: adapter_status='enabled' + live_order_allowed=1 + sent
    - live_sent_mock_or_artifact: 그 외의 live_order_sent=1 row 전부
    - live_order_sent_total: real만 카운트 (기존 의미 변경)
    """
    try:
        with _db_lock:
            conn = _conn()
            rows = conn.execute(
                "SELECT event_type, COUNT(*) as cnt FROM live_pilot_events GROUP BY event_type"
            ).fetchall()
            real_count = conn.execute(
                "SELECT COUNT(*) FROM live_pilot_events "
                "WHERE event_type IN ('live_sent','autonomous_live_sent') AND live_order_sent=1 "
                "AND adapter_status='enabled' AND live_order_allowed=1"
            ).fetchone()[0]
            artifact_count = conn.execute(
                "SELECT COUNT(*) FROM live_pilot_events "
                "WHERE live_order_sent=1 AND NOT ("
                "event_type IN ('live_sent','autonomous_live_sent') AND adapter_status='enabled' "
                "AND live_order_allowed=1)"
            ).fetchone()[0]
            conn.close()
        counts = {r["event_type"]: r["cnt"] for r in rows}
        blocked_policy = (
            counts.get("confirm_blocked_policy", 0)
            + counts.get("confirm_blocked_hermes", 0)
        )
        blocked_transport = counts.get("confirm_blocked_transport", 0)
        blocked_guard = counts.get("live_send_blocked", 0)
        return {
            "summary": counts,
            "live_sent_real": int(real_count),
            "live_sent_mock_or_artifact": int(artifact_count),
            "blocked_policy": blocked_policy,
            "blocked_transport": blocked_transport,
            "blocked_guard": blocked_guard,
            "live_order_sent_total": int(real_count),   # real만 카운트
            "live_order_allowed": False,
        }
    except Exception as exc:
        log.warning("event_summary failed: error_type=%s", type(exc).__name__)
        return {
            "summary": {},
            "live_sent_real": 0,
            "live_sent_mock_or_artifact": 0,
            "blocked_policy": 0,
            "blocked_transport": 0,
            "blocked_guard": 0,
            "live_order_sent_total": 0,
            "live_order_allowed": False,
        }
