"""
Toss paper 승인 ledger — preview → approved/cancelled/expired/blocked

실제 주문 0건. dry_run=True, live_order_allowed=False 강제.
기존 paper_trades 테이블과 별도 테이블(paper_ledger)에 저장.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

KST = timezone(timedelta(hours=9))
logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parents[1] / "db" / "data" / "toss_paper_trades.db"
_db_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    """paper ledger DB 연결. 테이블 없으면 자동 생성."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_ledger (
            paper_id TEXT PRIMARY KEY,
            preview_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity INTEGER DEFAULT 0,
            limit_price REAL DEFAULT 0,
            estimated_amount_krw REAL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'previewed',
            source TEXT DEFAULT '',
            account_label TEXT DEFAULT 'Toss 실전 AI 자동거래 계좌',
            live_order_allowed INTEGER NOT NULL DEFAULT 0,
            dry_run INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            approved_at TEXT,
            cancelled_at TEXT,
            reason TEXT DEFAULT '',
            confidence REAL DEFAULT 0,
            blocks TEXT DEFAULT '[]',
            warnings TEXT DEFAULT '[]',
            metadata TEXT DEFAULT '{}'
        )
    """)
    conn.commit()
    return conn


def _now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")


_id_counter = 0
_id_lock = threading.Lock()


def _gen_paper_id() -> str:
    global _id_counter
    now = datetime.now(KST)
    with _id_lock:
        _id_counter += 1
        seq = _id_counter
    return f"paper_{now.strftime('%Y%m%d_%H%M%S')}_{seq:04d}"


# ─── 생성 ────────────────────────────────────────────
def create_paper_preview_records(
    preview_id: str,
    candidates: list[dict],
    cross_checks: list[dict],
    toss_context: dict,
) -> list[dict]:
    """preview 후보들을 ledger에 기록. 반환: 생성된 record 목록."""
    records = []
    cash_krw = toss_context.get("cash_krw", 0)
    usdkrw = toss_context.get("usdkrw", 0)
    now = _now_kst()

    with _db_lock:
        conn = _conn()
        try:
            for cand, cc in zip(candidates, cross_checks):
                blocks = cc.get("blocks", [])
                status = "blocked" if blocks else "previewed"
                paper_id = _gen_paper_id()

                meta = {
                    "readiness": cc.get("toss_readiness", "unknown"),
                    "cash_krw_at_creation": cash_krw,
                    "usdkrw_at_creation": usdkrw,
                }

                conn.execute(
                    """INSERT OR IGNORE INTO paper_ledger
                       (paper_id, preview_id, symbol, side, quantity, limit_price,
                        estimated_amount_krw, status, source, live_order_allowed,
                        dry_run, created_at, reason, confidence, blocks, warnings, metadata)
                       VALUES (?,?,?,?,?,?,?,?,?,0,1,?,?,?,?,?,?)""",
                    (paper_id, preview_id, cand.get("symbol", ""),
                     cand.get("side", ""), cand.get("quantity", 0),
                     cand.get("limit_price", 0), cand.get("estimated_amount_krw", 0),
                     status, "telegram_paper_preview",
                     now, cand.get("reason", ""), cand.get("confidence", 0),
                     json.dumps(blocks), json.dumps(cc.get("warnings", [])),
                     json.dumps(meta)),
                )
                records.append({
                    "paper_id": paper_id,
                    "preview_id": preview_id,
                    "symbol": cand.get("symbol", ""),
                    "status": status,
                })
            conn.commit()
        finally:
            conn.close()
    return records


# ─── 승인 ────────────────────────────────────────────
def approve_paper_order(preview_id: str, symbol: str | None = None) -> dict:
    """paper 후보 승인. 실제 주문 없음. dry_run=True 강제."""
    with _db_lock:
        conn = _conn()
        try:
            where = "preview_id = ?"
            params: list = [preview_id]
            if symbol:
                where += " AND symbol = ?"
                params.append(symbol)

            rows = conn.execute(
                f"SELECT * FROM paper_ledger WHERE {where}", params
            ).fetchall()

            if not rows:
                return {"ok": False, "error": "preview not found", "approved": []}

            approved = []
            rejected = []
            now = _now_kst()

            for row in rows:
                r = dict(row)
                if r["status"] == "approved":
                    rejected.append({"paper_id": r["paper_id"], "reason": "already_approved"})
                    continue
                if r["status"] in ("cancelled", "expired"):
                    rejected.append({"paper_id": r["paper_id"], "reason": f"status_{r['status']}"})
                    continue
                if r["status"] == "blocked":
                    rejected.append({"paper_id": r["paper_id"], "reason": "blocked",
                                     "blocks": json.loads(r["blocks"] or "[]")})
                    continue

                # previewed → approved (paper only)
                conn.execute(
                    "UPDATE paper_ledger SET status='approved', approved_at=?, "
                    "source='telegram_paper_approval' WHERE paper_id=?",
                    (now, r["paper_id"]),
                )
                approved.append({
                    "paper_id": r["paper_id"],
                    "symbol": r["symbol"],
                    "side": r["side"],
                    "quantity": r["quantity"],
                    "limit_price": r["limit_price"],
                    "estimated_amount_krw": r["estimated_amount_krw"],
                    "status": "approved",
                    "dry_run": True,
                    "live_order_allowed": False,
                })

            conn.commit()
            return {"ok": True, "approved": approved, "rejected": rejected}
        finally:
            conn.close()


# ─── 취소 ────────────────────────────────────────────
def cancel_paper_order(
    preview_id: str, symbol: str | None = None, reason: str = "user_cancelled",
) -> dict:
    """paper 후보 취소."""
    with _db_lock:
        conn = _conn()
        try:
            where = "preview_id = ? AND status IN ('previewed', 'approved')"
            params: list = [preview_id]
            if symbol:
                where += " AND symbol = ?"
                params.append(symbol)

            now = _now_kst()
            cur = conn.execute(
                f"UPDATE paper_ledger SET status='cancelled', cancelled_at=?, "
                f"reason=reason||' → '||? WHERE {where}",
                (now, reason, *params),
            )
            conn.commit()
            return {"ok": True, "cancelled_count": cur.rowcount}
        finally:
            conn.close()


# ─── 만료 ────────────────────────────────────────────
def expire_paper_preview(preview_id: str) -> dict:
    """preview 만료 처리."""
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "UPDATE paper_ledger SET status='expired' "
                "WHERE preview_id=? AND status='previewed'",
                (preview_id,),
            )
            conn.commit()
            return {"ok": True, "expired_count": cur.rowcount}
        finally:
            conn.close()


# ─── 조회 ────────────────────────────────────────────
def list_paper_orders(status: str | None = None, limit: int = 50) -> list[dict]:
    """paper ledger 조회."""
    with _db_lock:
        conn = _conn()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM paper_ledger WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM paper_ledger ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def paper_ledger_summary() -> dict:
    """상태별 count + 최근 기록."""
    with _db_lock:
        conn = _conn()
        try:
            counts = {}
            for row in conn.execute(
                "SELECT status, COUNT(*) as cnt FROM paper_ledger GROUP BY status"
            ).fetchall():
                counts[row["status"]] = row["cnt"]

            recent = conn.execute(
                "SELECT * FROM paper_ledger ORDER BY created_at DESC LIMIT 10"
            ).fetchall()

            return {
                "counts": counts,
                "total": sum(counts.values()),
                "recent": [dict(r) for r in recent],
            }
        finally:
            conn.close()


# ─── Telegram 응답 텍스트 ────────────────────────────
def format_approval_response(result: dict) -> str:
    """승인 결과를 Telegram 메시지 텍스트로 변환."""
    lines = []
    for a in result.get("approved", []):
        lines.append(
            f"✅ Paper 승인 완료 · 실제 주문 아님\n"
            f"  {a['symbol']}\n"
            f"  paper_id: {a['paper_id']}\n"
            f"  수량: {a['quantity']}주\n"
            f"  지정가: ₩{a['limit_price']:,.0f}\n"
            f"  예상금액: ₩{a['estimated_amount_krw']:,.0f}\n"
            f"  상태: approved\n"
            f"  실주문: 비활성\n"
            f"  기록: paper ledger only"
        )
    for r in result.get("rejected", []):
        reason = r.get("reason", "unknown")
        blocks = r.get("blocks", [])
        lines.append(
            f"⛔ Paper 승인 거절\n"
            f"  paper_id: {r['paper_id']}\n"
            f"  사유: {reason}"
            + (f"\n  차단: {', '.join(blocks)}" if blocks else "")
            + "\n  실주문: 비활성"
        )
    if not lines:
        return "⚠ 처리 대상 없음"
    return "\n\n".join(lines)


def format_cancel_response(result: dict) -> str:
    """취소 결과를 Telegram 메시지 텍스트로 변환."""
    cnt = result.get("cancelled_count", 0)
    return f"🟡 Paper 취소 완료 · 실제 주문 없음\n  취소: {cnt}건\n  상태: cancelled\n  실주문: 비활성"
