"""
브리핑 원문 아카이브 — read-only 조회 + append-only 저장.

브리핑 메일/텔레그램 생성 시점의 본문을 날짜별로 저장한다.
대시보드에서 최근 브리핑 원문을 열람할 수 있다.
실패해도 브리핑 전송 자체가 멈추면 안 된다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
log = logging.getLogger(__name__)

_SANITIZE_RE = re.compile(r"<script[^>]*>.*?</script>", re.S | re.I)
_SECRET_PATTERNS = re.compile(
    r"(app_key|app_secret|password|smtp_pass|gmail_app|kis_app|account_no)",
    re.I,
)


def _db_path() -> Path:
    try:
        from config.settings import DB_DIR
        return DB_DIR / "briefing_archive.db"
    except Exception:
        return Path("db/data/briefing_archive.db")


def _conn() -> sqlite3.Connection:
    """읽기/쓰기 연결. 테이블 없으면 자동 생성."""
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS archives (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        briefing_type TEXT NOT NULL,
        title TEXT,
        subject TEXT,
        channel TEXT DEFAULT 'email',
        body_text TEXT,
        body_html TEXT,
        summary TEXT,
        action_count INTEGER DEFAULT 0,
        tickers_json TEXT DEFAULT '[]',
        source TEXT DEFAULT 'system',
        version TEXT DEFAULT 'v1'
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_archives_created
        ON archives(created_at DESC)""")
    conn.commit()
    return conn


def _make_id(created_at: str, briefing_type: str, title: str) -> str:
    """중복 방지용 결정론적 ID."""
    key = f"{created_at[:16]}:{briefing_type}:{title}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _sanitize_html(html: str) -> str:
    """script 태그 제거, secret-like 패턴 마스킹."""
    if not html:
        return ""
    out = _SANITIZE_RE.sub("", html)
    out = _SECRET_PATTERNS.sub("[REDACTED]", out)
    return out


def _extract_tickers(raw_json: dict) -> list[str]:
    """raw_json에서 관련 종목 추출."""
    tickers = set()
    for key in ("buy_recommendations", "sell_recommendations",
                "strategy_buy", "strategy_sell"):
        items = raw_json.get(key) or []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    t = item.get("ticker") or item.get("symbol") or ""
                    if t:
                        tickers.add(t)
    # normalized actions
    norm = raw_json.get("normalized") or {}
    for section in ("executable_actions", "conditional_buy_candidates"):
        for item in (norm.get(section) or []):
            if isinstance(item, dict):
                t = item.get("ticker") or ""
                if t:
                    tickers.add(t)
    return sorted(tickers)[:20]


def save_briefing_archive(
    briefing_type: str,
    title: str,
    subject: str = "",
    body_text: str = "",
    body_html: str = "",
    raw_json: dict | None = None,
    channel: str = "email",
) -> str | None:
    """브리핑 원문을 아카이브에 저장. 실패 시 None 반환 (예외 던지지 않음)."""
    try:
        now = datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S")
        archive_id = _make_id(now, briefing_type, title)

        raw = raw_json or {}
        tickers = _extract_tickers(raw)
        action_count = len(raw.get("normalized", {}).get("executable_actions", []))
        action_count += len(raw.get("normalized", {}).get("conditional_buy_candidates", []))

        # summary: advisor_oneliner or first 200 chars of text
        summary = raw.get("advisor_oneliner") or ""
        if not summary and body_text:
            summary = body_text[:200]

        conn = _conn()
        conn.execute(
            """INSERT OR IGNORE INTO archives
               (id, created_at, briefing_type, title, subject, channel,
                body_text, body_html, summary, action_count, tickers_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (archive_id, now, briefing_type, title, subject, channel,
             body_text, _sanitize_html(body_html), summary,
             action_count, json.dumps(tickers, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()
        log.info("briefing archived: %s %s", archive_id, briefing_type)
        return archive_id
    except Exception as e:
        log.warning("briefing archive save failed: %s", e)
        return None


def list_briefing_archives(
    limit: int = 50, days: int = 90, briefing_type: str = "all"
) -> list[dict]:
    """최근 브리핑 목록 조회. body는 포함하지 않음."""
    try:
        conn = _conn()
        cutoff = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
        where = "created_at >= ?"
        params: list = [cutoff]
        if briefing_type != "all":
            where += " AND briefing_type = ?"
            params.append(briefing_type)
        rows = conn.execute(
            f"""SELECT id, created_at, briefing_type, title, subject,
                       channel, summary, action_count, tickers_json
                FROM archives WHERE {where}
                ORDER BY created_at DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()
        conn.close()
        return [
            {**dict(r), "tickers": json.loads(r["tickers_json"] or "[]")}
            for r in rows
        ]
    except Exception as e:
        log.warning("briefing archive list failed: %s", e)
        return []


def get_briefing_archive(archive_id: str) -> dict | None:
    """단일 브리핑 상세 조회 (body 포함)."""
    if not archive_id or not re.match(r"^[a-f0-9]{1,32}$", archive_id):
        return None
    try:
        conn = _conn()
        row = conn.execute(
            "SELECT * FROM archives WHERE id = ?", (archive_id,)
        ).fetchone()
        conn.close()
        if not row:
            return None
        d = dict(row)
        d["tickers"] = json.loads(d.pop("tickers_json", "[]"))
        return d
    except Exception as e:
        log.warning("briefing archive get failed: %s", e)
        return None
