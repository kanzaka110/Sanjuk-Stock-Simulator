"""core/toss_live_pilot_verification.py

Hermes 교차검증 게이트 — Live Pilot 최종 승인 전 2차 검증 ledger.

구조:
  1차: GCP stock-bot → 후보 생성 + verification request (PENDING)
  2차: Hermes → 검증 결과 기록 (PASS / HOLD / BLOCK / ERROR)
  3차: 사용자 → Telegram 최종 승인 버튼
  4차: callback → Hermes PASS + 미만료 + guard 통과 시에만 전송 가능

상태:
  PENDING  : 검증 요청됨, Hermes 판정 대기
  PASS     : Hermes 교차검증 통과 (expires_at까지만 유효)
  HOLD     : Hermes가 보류 판정 → 최종 승인 차단
  BLOCK    : Hermes가 차단 판정 → 최종 승인 차단
  ERROR    : 검증 오류 → 최종 승인 차단
  STALE    : (계산 상태) PASS이지만 expires_at 초과 → 차단

금지:
  - accountNo/token/key/secret 저장 금지
  - verification status로 live_order_allowed를 true로 바꾸지 않음
  - 자동매매 실행 금지
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

_VALID_STATUSES = frozenset(["PENDING", "PASS", "HOLD", "BLOCK", "ERROR"])
_PASS_STATUS = "PASS"

_DEFAULT_TTL_MINUTES = 10


def _db_path() -> Path:
    try:
        from db.store import DB_DIR
        return DB_DIR / "toss_live_pilot_verification.db"
    except Exception:
        return Path("db/data/toss_live_pilot_verification.db")


_db_lock = threading.Lock()
_schema_created = False


def _conn() -> sqlite3.Connection:
    global _schema_created
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    if not _schema_created:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS live_pilot_verification (
                verification_id     TEXT PRIMARY KEY,
                pilot_id            TEXT NOT NULL,
                preview_id          TEXT NOT NULL,
                symbol              TEXT NOT NULL,
                side                TEXT NOT NULL DEFAULT 'buy',
                quantity            INTEGER DEFAULT 0,
                limit_price         REAL DEFAULT 0,
                estimated_amount_krw REAL DEFAULT 0,
                status              TEXT NOT NULL DEFAULT 'PENDING',
                reasons             TEXT DEFAULT '[]',
                checks              TEXT DEFAULT '{}',
                hermes_message      TEXT DEFAULT '',
                requested_at        TEXT NOT NULL,
                verified_at         TEXT,
                expires_at          TEXT,
                source              TEXT DEFAULT 'hermes',
                reviewer            TEXT DEFAULT 'Hermes',
                live_order_allowed  INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        _schema_created = True
    return conn


def _now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")


def _gen_verification_id() -> str:
    ts = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    seq = random.randint(1000, 9999)
    return f"hv_{ts}_{seq}"


# ── 공개 API ───────────────────────────────────────────────────────

def create_verification_request(
    preview_record: dict,
    pilot_id: str = "",
) -> dict:
    """Hermes 검증 요청 생성 (status=PENDING).

    Args:
        preview_record: preview dict (symbol/side/quantity/limit_price/estimated_amount_krw 포함)
        pilot_id: live pilot ledger의 pilot_id

    Returns:
        {"ok": bool, "verification_id": str, "status": "PENDING"}
    """
    verification_id = _gen_verification_id()
    symbol = preview_record.get("symbol", "")
    side = preview_record.get("side", "buy")
    quantity = int(preview_record.get("quantity") or 0)
    limit_price = float(preview_record.get("limit_price") or 0)
    estimated = float(preview_record.get("estimated_amount_krw") or 0)
    preview_id = preview_record.get("preview_id", preview_record.get("pilot_id", ""))

    now = _now_kst()

    with _db_lock:
        conn = _conn()
        try:
            conn.execute(
                """INSERT INTO live_pilot_verification
                   (verification_id, pilot_id, preview_id, symbol, side, quantity,
                    limit_price, estimated_amount_krw, status, reasons, checks,
                    hermes_message, requested_at, verified_at, expires_at,
                    source, reviewer, live_order_allowed)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    verification_id, pilot_id, preview_id, symbol, side, quantity,
                    limit_price, estimated, "PENDING", "[]", "{}",
                    "", now, None, None,
                    "hermes", "Hermes", 0,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    return {
        "ok": True,
        "verification_id": verification_id,
        "pilot_id": pilot_id,
        "status": "PENDING",
        "requested_at": now,
    }


def record_hermes_verification(
    verification_id: str,
    status: str,
    reasons: list[str],
    checks: dict,
    hermes_message: str = "",
    ttl_minutes: int = _DEFAULT_TTL_MINUTES,
) -> dict:
    """Hermes 검증 결과 기록.

    PASS 시 expires_at = verified_at + ttl_minutes.
    status: PENDING / PASS / HOLD / BLOCK / ERROR

    live_order_allowed는 PASS여도 False 유지 — verification은 gate일 뿐.
    """
    if status not in _VALID_STATUSES:
        return {"ok": False, "reason": f"invalid status: {status!r}"}

    verified_at = _now_kst()
    if status == _PASS_STATUS:
        dt = datetime.now(KST) + timedelta(minutes=ttl_minutes)
        expires_at: str | None = dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    else:
        expires_at = None

    with _db_lock:
        conn = _conn()
        try:
            existing = conn.execute(
                "SELECT verification_id FROM live_pilot_verification WHERE verification_id=?",
                (verification_id,),
            ).fetchone()
            if not existing:
                return {"ok": False, "reason": "verification_id not found"}
            conn.execute(
                """UPDATE live_pilot_verification
                   SET status=?, reasons=?, checks=?, hermes_message=?,
                       verified_at=?, expires_at=?, live_order_allowed=0
                   WHERE verification_id=?""",
                (
                    status,
                    json.dumps(reasons, ensure_ascii=False),
                    json.dumps(checks, ensure_ascii=False),
                    hermes_message,
                    verified_at,
                    expires_at,
                    verification_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    return {
        "ok": True,
        "verification_id": verification_id,
        "status": status,
        "verified_at": verified_at,
        "expires_at": expires_at,
        "live_order_allowed": False,  # 항상 False — gate only
    }


def get_verification_for_pilot(pilot_id: str) -> dict | None:
    """pilot_id에 대한 최신 검증 기록 반환. 없으면 None."""
    try:
        with _db_lock:
            conn = _conn()
            row = conn.execute(
                """SELECT * FROM live_pilot_verification
                   WHERE pilot_id=? ORDER BY requested_at DESC LIMIT 1""",
                (pilot_id,),
            ).fetchone()
            conn.close()
        if not row:
            return None
        d = dict(row)
        d["reasons"] = json.loads(d.get("reasons") or "[]")
        d["checks"] = json.loads(d.get("checks") or "{}")
        return d
    except Exception as e:
        log.warning("get_verification_for_pilot failed: %s", e)
        return None


def is_verification_passed(
    pilot_id: str,
    now: datetime | None = None,
) -> tuple[bool, list[str], dict]:
    """Hermes 검증이 PASS이고 미만료인지 확인.

    Returns:
        (passed: bool, reasons: list[str], verification_record: dict)
    """
    if now is None:
        now = datetime.now(KST)

    rec = get_verification_for_pilot(pilot_id)
    if rec is None:
        return False, ["hermes_verification_not_found"], {}

    status = rec.get("status", "PENDING")

    if status == "PENDING":
        return False, ["hermes_verification_pending"], rec

    if status in ("HOLD", "BLOCK", "ERROR"):
        return False, [f"hermes_verification_{status.lower()}"], rec

    if status == _PASS_STATUS:
        expires_at_str = rec.get("expires_at")
        if not expires_at_str:
            return False, ["hermes_verification_no_expiry"], rec
        try:
            # KST offset 파싱
            dt_str = expires_at_str.replace("+09:00", "")
            expires_dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=KST)
            if now > expires_dt:
                return False, ["hermes_verification_stale"], rec
        except Exception as e:
            log.warning("expires_at 파싱 실패: %s", e)
            return False, ["hermes_verification_expiry_parse_error"], rec
        return True, [], rec

    return False, [f"hermes_verification_unknown_status: {status}"], rec


# ── Hermes 검증 컨텍스트 빌더 ─────────────────────────────────────

def build_hermes_verification_context(
    preview_record: dict,
    policy: dict,
    paper_summary: dict | None = None,
) -> dict:
    """Hermes에 전달할 검증 컨텍스트 dict 생성.

    체크 항목 포함 — Hermes가 판단 가능한 모든 필드.
    민감정보 미포함.
    """
    symbol = preview_record.get("symbol", "")
    estimated = float(preview_record.get("estimated_amount_krw") or 0)
    max_krw = policy.get("max_order_krw", 100_000)
    blocked_symbols = policy.get("blocked_symbols", [])
    paper_count = 0
    paper_status = "unknown"
    if paper_summary:
        paper_count = paper_summary.get("summary", {}).get("evaluated_count", 0)
        paper_status = "insufficient" if paper_count < 5 else "stable"

    checks: dict = {
        "amount_guard": "ok" if estimated <= max_krw else f"FAIL: {estimated:,.0f} > {max_krw:,.0f}",
        "blocked_symbol": "ok" if symbol not in blocked_symbols else f"FAIL: {symbol}",
        "price_nonzero": "ok" if float(preview_record.get("limit_price") or 0) > 0 else "FAIL",
        "quantity_nonzero": "ok" if int(preview_record.get("quantity") or 0) > 0 else "FAIL",
        "live_order_allowed": "false (gate off)",
        "adapter_status": policy.get("adapter_status", "disabled"),
        "transport_status": "not_injected",
        "paper_sample_status": paper_status,
        "paper_evaluated_count": paper_count,
        "user_final_approval_required": "true",
        "duplicate_live_order": "check_required",
        "duplicate_paper_open": "check_required",
        "price_staleness": "check_required",
        "source_disagreement": "check_required",
    }
    if preview_record.get("side") == "sell":
        checks["sell_quantity_guard"] = "check_required"
    if symbol == "MU" and preview_record.get("side") == "sell":
        checks["protected_symbol_sell_block"] = f"FAIL: {symbol} sell blocked"

    return {
        "verification_id": preview_record.get("verification_id", ""),
        "pilot_id": preview_record.get("pilot_id", ""),
        "preview_id": preview_record.get("preview_id", ""),
        "symbol": symbol,
        "side": preview_record.get("side", "buy"),
        "quantity": preview_record.get("quantity", 0),
        "limit_price": preview_record.get("limit_price", 0),
        "estimated_amount_krw": estimated,
        "adapter_status": policy.get("adapter_status", "disabled"),
        "live_order_allowed": False,
        "paper_evaluated_count": paper_count,
        "sample_status": paper_status,
        "blocked_symbols": ",".join(blocked_symbols),
        "checks": checks,
    }


def format_hermes_verification_request(context: dict) -> str:
    """[HERMES_LIVE_PILOT_VERIFY] 블록 문자열 생성.

    Hermes가 읽고 판정할 수 있는 포맷.
    민감정보 미포함.
    """
    checks = context.get("checks", {})
    checks_lines = "\n".join(f"  {k}: {v}" for k, v in checks.items())

    return (
        "[HERMES_LIVE_PILOT_VERIFY]\n"
        f"verification_id: {context.get('verification_id', '')}\n"
        f"pilot_id: {context.get('pilot_id', '')}\n"
        f"preview_id: {context.get('preview_id', '')}\n"
        f"symbol: {context.get('symbol', '')}\n"
        f"side: {context.get('side', '')}\n"
        f"quantity: {context.get('quantity', 0)}\n"
        f"limit_price: {context.get('limit_price', 0)}\n"
        f"estimated_amount_krw: {context.get('estimated_amount_krw', 0)}\n"
        f"adapter_status: {context.get('adapter_status', 'disabled')}\n"
        f"live_order_allowed: false\n"
        f"paper_evaluated_count: {context.get('paper_evaluated_count', 0)}\n"
        f"sample_status: {context.get('sample_status', 'insufficient')}\n"
        f"blocked_symbols: {context.get('blocked_symbols', '')}\n"
        f"checks:\n{checks_lines}\n"
        "[/HERMES_LIVE_PILOT_VERIFY]"
    )


# ── 목록 / 요약 ───────────────────────────────────────────────────

def list_verifications(limit: int = 50) -> list[dict]:
    """최근 검증 기록 조회 (read-only)."""
    try:
        with _db_lock:
            conn = _conn()
            rows = conn.execute(
                "SELECT * FROM live_pilot_verification ORDER BY requested_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
        result = []
        for r in rows:
            d = dict(r)
            d["reasons"] = json.loads(d.get("reasons") or "[]")
            d["checks"] = json.loads(d.get("checks") or "{}")
            result.append(d)
        return result
    except Exception as e:
        log.warning("list_verifications failed: %s", e)
        return []


def verification_summary() -> dict:
    """검증 상태 요약 (read-only)."""
    try:
        with _db_lock:
            conn = _conn()
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM live_pilot_verification GROUP BY status"
            ).fetchall()
            conn.close()
        counts: dict[str, int] = {r["status"]: r["cnt"] for r in rows}
        # STALE 계산 (PASS 중 만료된 것)
        now = datetime.now(KST)
        stale_count = 0
        try:
            with _db_lock:
                conn2 = _conn()
                pass_rows = conn2.execute(
                    "SELECT expires_at FROM live_pilot_verification WHERE status='PASS'"
                ).fetchall()
                conn2.close()
            for pr in pass_rows:
                ea = pr["expires_at"]
                if ea:
                    try:
                        dt_str = ea.replace("+09:00", "")
                        expires_dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=KST)
                        if now > expires_dt:
                            stale_count += 1
                    except Exception:
                        pass
        except Exception:
            pass
        counts["STALE"] = stale_count
        return {"summary": counts, "live_order_allowed": False}
    except Exception as e:
        log.warning("verification_summary failed: %s", e)
        return {"summary": {}, "live_order_allowed": False}
