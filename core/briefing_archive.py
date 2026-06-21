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


# ─── 현재 결과 추적 (read-only) ────────────────────
_ACTION_LABELS = {
    "AI_NEW_BUY": "신규 매수", "AI_ADD_BUY": "추가 매수",
    "CONDITIONAL_NEW_BUY": "조건부 매수",
    "AI_SELL_MANAGEMENT": "보유 관리",
    "CANCEL_SELL": "매도 취소", "HOLD_REVIEW": "보유 점검",
    "WATCH_ONLY": "관망",
}


def build_archive_tracking(archive: dict) -> dict:
    """아카이브 브리핑의 추천들에 대한 현재 결과 추적. read-only."""
    result = {"summary": {"total": 0, "open": 0, "closed": 0,
                          "reached": 0, "waiting": 0, "avg_pnl_pct": 0.0},
              "items": [], "error": ""}

    day = (archive.get("created_at") or "")[:10]
    bt = archive.get("briefing_type", "")
    if not day:
        result["error"] = "날짜 정보 없음"
        return result

    # predictions DB에서 같은 날짜+type 조회
    try:
        from core.dashboard_data import _conn as _dd_conn, _rows
        conn = _dd_conn()
        if conn is None:
            result["error"] = "DB 없음"
            return result
        where = "created_at LIKE ?"
        params: list = [f"{day}%"]
        if bt:
            where += " AND briefing_type = ?"
            params.append(bt)
        preds = _rows(conn, f"""
            SELECT ticker, name, action_type, signal, status, outcome,
                   pnl_pct, entry_price, target_price, stop_loss,
                   created_at, account_type
            FROM predictions WHERE {where}
            ORDER BY created_at DESC LIMIT 50
        """, tuple(params))
        conn.close()
    except Exception as e:
        result["error"] = f"predictions 조회 실패: {e}"
        return result

    if not preds:
        result["error"] = "관련 추천 없음"
        return result

    # 현재가 조회 (최대 20종목)
    tickers = list({p["ticker"] for p in preds if p.get("ticker")})[:20]
    cur_prices: dict[str, float] = {}
    try:
        from core.market import _get_quote_realtime
        for tk in tickers:
            q = _get_quote_realtime(tk)
            if q and q.price:
                cur_prices[tk] = q.price
    except Exception:
        pass

    # 각 prediction에 대해 tracking 계산
    from core.dashboard_data import calc_price_context, ticker_orderbook, summarize_execution_risk

    # 국내 종목 호가 리스크 (최대 10종목)
    ob_risks: dict[str, dict] = {}
    kr_tks = [t for t in tickers if t.endswith(".KS") or t.endswith(".KQ")][:10]
    for tk in kr_tks:
        try:
            ob_risks[tk] = summarize_execution_risk(ticker_orderbook(tk))
        except Exception:
            pass

    items = []
    total_pnl = 0.0
    pnl_count = 0
    reached = 0
    waiting = 0
    open_count = 0
    closed_count = 0

    for p in preds:
        tk = p.get("ticker", "")
        at = p.get("action_type", "")
        status = p.get("status", "")
        outcome = p.get("outcome", "")
        pnl_pct = p.get("pnl_pct")
        cur_price = cur_prices.get(tk, 0.0)

        label = _ACTION_LABELS.get(at, at or "기타")
        pctx = calc_price_context(cur_price, p.get("entry_price"),
                                   p.get("target_price"), p.get("stop_loss"), at)

        # tracking label/tone
        if status == "closed":
            closed_count += 1
            if outcome == "win":
                tracking_label = f"승 ({pnl_pct:+.1f}%)" if pnl_pct else "승"
                tone = "good"
            elif outcome == "loss":
                tracking_label = f"패 ({pnl_pct:+.1f}%)" if pnl_pct else "패"
                tone = "bad"
            else:
                tracking_label = "무"
                tone = "neutral"
            if pnl_pct is not None:
                total_pnl += pnl_pct
                pnl_count += 1
        else:
            open_count += 1
            if not cur_price:
                tracking_label = "현재가 대기"
                tone = "neutral"
            elif at == "AI_SELL_MANAGEMENT":
                tracking_label = "보유 유지"
                tone = "neutral"
            elif at == "CONDITIONAL_NEW_BUY":
                tracking_label = pctx["condition_label"]
                if pctx["condition_status"] == "reached":
                    tone = "good"
                    reached += 1
                elif pctx["condition_status"] == "near":
                    tone = "warn"
                else:
                    tone = "wait"
                    waiting += 1
            elif at in ("WATCH_ONLY", "HOLD_REVIEW", "CANCEL_SELL"):
                tracking_label = "관망 유지"
                tone = "neutral"
            else:
                tracking_label = "진행중"
                tone = "neutral"

        items.append({
            "ticker": tk,
            "name": p.get("name", tk),
            "action_type": at,
            "label": label,
            "status": status,
            "outcome": outcome,
            "pnl_pct": pnl_pct,
            "current_price": cur_price,
            "price_context": pctx,
            "condition_label": pctx["condition_label"],
            "distance_summary": pctx["summary"],
            "tracking_label": tracking_label,
            "tracking_tone": tone,
            "execution_risk": ob_risks.get(tk, {"has_warning": False, "label": "", "summary": "", "tone": "unknown"}),
        })

    result["summary"] = {
        "total": len(preds),
        "open": open_count,
        "closed": closed_count,
        "reached": reached,
        "waiting": waiting,
        "avg_pnl_pct": round(total_pnl / pnl_count, 2) if pnl_count else 0.0,
    }
    result["items"] = items
    return result
