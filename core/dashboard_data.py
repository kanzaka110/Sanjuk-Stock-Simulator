"""
대시보드 데이터 수집 — 조회 전용 (읽기 전용)

웹 대시보드(web/app.py)와 헬스체크용. 주문 실행/DB 수정 일절 없음.
DB가 없거나 비어 있어도 절대 예외를 던지지 않고 빈 구조를 반환한다.
"""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


def _db_path():
    try:
        from config.settings import DB_DIR
        return DB_DIR / "memory.db"
    except Exception:
        from pathlib import Path
        return Path("db/data/memory.db")


def _conn() -> sqlite3.Connection | None:
    """읽기 전용 연결. DB 없으면 None (예외 없음)."""
    p = _db_path()
    try:
        if not p.exists():
            return None
        # 읽기 전용 URI — 실수로도 쓰기 불가
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _rows(conn, sql, params=()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


def _scalar(conn, sql, params=(), default=0):
    try:
        r = conn.execute(sql, params).fetchone()
        return r[0] if r and r[0] is not None else default
    except Exception:
        return default


# ─── 추천(predictions) ─────────────────────────────────
def recent_predictions(limit: int = 20) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    rows = _rows(
        conn,
        """SELECT created_at, ticker, name, signal, original_signal, action_type,
                  action_grade, account_type, briefing_type, entry_price, target_price,
                  stop_loss, confidence, status, outcome, pnl_pct, normalizer_version
           FROM predictions ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    )
    conn.close()
    return rows


def open_predictions(limit: int = 50) -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    rows = _rows(
        conn,
        """SELECT created_at, ticker, name, signal, action_type, account_type,
                  entry_price, target_price, stop_loss, confidence, briefing_type
           FROM predictions WHERE status='open' ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    )
    conn.close()
    return rows


def closed_summary(days: int = 30) -> dict:
    conn = _conn()
    if conn is None:
        return {"total": 0, "win": 0, "loss": 0, "neutral": 0, "avg_pnl": 0.0, "recent": []}
    cutoff = (datetime.now(KST) - timedelta(days=days)).isoformat()
    base = "WHERE status='closed' AND closed_at >= ?"
    total = _scalar(conn, f"SELECT COUNT(*) FROM predictions {base}", (cutoff,))
    win = _scalar(conn, f"SELECT COUNT(*) FROM predictions {base} AND outcome='win'", (cutoff,))
    loss = _scalar(conn, f"SELECT COUNT(*) FROM predictions {base} AND outcome='loss'", (cutoff,))
    neutral = _scalar(conn, f"SELECT COUNT(*) FROM predictions {base} AND outcome='neutral'", (cutoff,))
    avg_pnl = _scalar(
        conn,
        f"SELECT AVG(pnl_pct) FROM predictions {base} AND outcome IN ('win','loss','neutral')",
        (cutoff,), default=0.0,
    )
    recent = _rows(
        conn,
        """SELECT closed_at, name, ticker, signal, outcome, pnl_pct
           FROM predictions WHERE status='closed' AND outcome IN ('win','loss','neutral')
           ORDER BY closed_at DESC LIMIT 10""",
    )
    conn.close()
    return {
        "total": total, "win": win, "loss": loss, "neutral": neutral,
        "avg_pnl": round(float(avg_pnl or 0), 2), "recent": recent,
    }


def latest_briefing_actions() -> dict:
    """가장 최근 브리핑(같은 날)의 분류별 카운트 + 행."""
    conn = _conn()
    if conn is None:
        return {"day": "", "by_type": {}, "rows": []}
    latest = _scalar(conn, "SELECT MAX(created_at) FROM predictions", default="")
    if not latest:
        conn.close()
        return {"day": "", "by_type": {}, "rows": []}
    day = str(latest)[:10]
    rows = _rows(
        conn,
        """SELECT created_at, name, ticker, signal, action_type, account_type,
                  entry_price, target_price, briefing_type, normalizer_version
           FROM predictions WHERE created_at LIKE ? ORDER BY created_at DESC""",
        (f"{day}%",),
    )
    conn.close()
    by_type: dict[str, int] = {}
    for r in rows:
        k = r.get("action_type") or "(미분류)"
        by_type[k] = by_type.get(k, 0) + 1
    return {"day": day, "by_type": by_type, "rows": rows}


# ─── 적중률(accuracy_stats) ────────────────────────────
def accuracy_by_ticker() -> list[dict]:
    conn = _conn()
    if conn is None:
        return []
    rows = _rows(
        conn,
        """SELECT ticker, total_predictions, evaluated_count, wins, losses,
                  win_rate, avg_pnl, profit_factor, expectancy
           FROM accuracy_stats WHERE evaluated_count >= 1
           ORDER BY evaluated_count DESC, win_rate DESC""",
    )
    conn.close()
    return rows


# ─── 시스템 상태 ───────────────────────────────────────
def db_stats() -> dict:
    conn = _conn()
    if conn is None:
        return {"db_exists": False, "predictions": 0, "open": 0, "closed": 0,
                "v1": 0, "last_created": "", "last_closed": ""}
    out = {
        "db_exists": True,
        "predictions": _scalar(conn, "SELECT COUNT(*) FROM predictions"),
        "open": _scalar(conn, "SELECT COUNT(*) FROM predictions WHERE status='open'"),
        "closed": _scalar(conn, "SELECT COUNT(*) FROM predictions WHERE status='closed'"),
        "v1": _scalar(conn, "SELECT COUNT(*) FROM predictions WHERE normalizer_version='v1'"),
        "last_created": _scalar(conn, "SELECT MAX(created_at) FROM predictions", default=""),
        "last_closed": _scalar(conn, "SELECT MAX(closed_at) FROM predictions WHERE closed_at != ''", default=""),
    }
    conn.close()
    return out


def service_status(service: str = "stock-bot") -> dict:
    """systemctl show 기반 읽기 전용 서비스 상태. 실패 시 unknown."""
    out = {"service": service, "active": "unknown", "sub": "", "since": ""}
    try:
        r = subprocess.run(
            ["systemctl", "show", service,
             "--property=ActiveState,SubState,ActiveEnterTimestamp", "--no-pager"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.startswith("ActiveState="):
                    out["active"] = line.split("=", 1)[1]
                elif line.startswith("SubState="):
                    out["sub"] = line.split("=", 1)[1]
                elif line.startswith("ActiveEnterTimestamp="):
                    out["since"] = line.split("=", 1)[1]
    except Exception:
        pass
    return out


def system_status() -> dict:
    """대시보드 상단 종합 상태."""
    return {
        "now": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "db": db_stats(),
        "service": service_status(),
        "latest_briefing": latest_briefing_actions(),
    }


def health() -> dict:
    """DB 유무와 무관하게 항상 정상 응답."""
    db = _conn()
    db_ok = db is not None
    if db:
        db.close()
    return {
        "status": "ok",
        "db_available": db_ok,
        "now": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
    }
