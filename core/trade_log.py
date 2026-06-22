"""
매매 기록 — 텔레그램 입력 기반 HOLDINGS 갱신 추적

배경: 보유 잔고는 삼성증권(공개 API 없음)이라 자동 동기화 불가.
settings.HOLDINGS는 수동 관리 — 매매 후 갱신을 잊으면 AI가 틀린
포지션으로 판단한다. 텔레그램으로 매매를 기록하면:
  1. trades 테이블에 저장
  2. 다음 브리핑에서 "미반영 매매 있음" 경고를 AI 프롬프트에 주입
  3. settings.py를 갱신하면 기록을 반영 처리 (/매매반영)

입력 형식 (텔레그램):
  매매 삼성전자 매수 10주 290000
  매매 005930 매도 5주 295000 ISA
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime

from config.settings import DB_DIR, KST

log = logging.getLogger(__name__)

_DB_PATH = DB_DIR / "memory.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT,
            side TEXT NOT NULL,
            shares INTEGER NOT NULL,
            price REAL NOT NULL,
            account TEXT DEFAULT '',
            applied INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def _resolve_ticker(word: str) -> tuple[str, str]:
    """종목명 또는 코드 → (ticker, name). PORTFOLIO/WATCHLIST/스캔 유니버스에서 검색."""
    from config.settings import PORTFOLIO, SCAN_UNIVERSE_KR, SCAN_UNIVERSE_US, WATCHLIST

    word = word.strip()
    # 6자리 코드
    if re.fullmatch(r"\d{6}", word):
        for suffix in (".KS", ".KQ"):
            tk = f"{word}{suffix}"
            for src in (PORTFOLIO, WATCHLIST, SCAN_UNIVERSE_KR):
                if tk in src:
                    return tk, src[tk]
        return f"{word}.KS", word
    # US 심볼 (대문자 1~5)
    if re.fullmatch(r"[A-Za-z]{1,5}", word):
        sym = word.upper()
        for src in (PORTFOLIO, WATCHLIST, SCAN_UNIVERSE_US):
            if sym in src:
                return sym, src[sym]
        return sym, sym
    # 한글 이름 매칭
    for src in (PORTFOLIO, WATCHLIST, SCAN_UNIVERSE_KR, SCAN_UNIVERSE_US):
        for tk, nm in src.items():
            if word in nm or nm in word:
                return tk, nm
    # 발굴 기록(discoveries)에서 검색 — 스캐너가 찾아낸 동적 종목
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT ticker, name FROM discoveries WHERE name LIKE ? ORDER BY created_at DESC LIMIT 1",
            (f"%{word}%",),
        ).fetchone()
        if row:
            return row["ticker"], row["name"]
    except Exception:
        pass
    return "", word


def parse_trade_message(text: str) -> dict | None:
    """'매매 삼성전자 매수 10주 290000 [계좌]' 파싱. 실패 시 None."""
    m = re.match(
        r"매매\s+(\S+)\s+(매수|매도)\s+(\d+)\s*주?\s+([\d,\.]+)(?:\s+(\S+))?",
        text.strip(),
    )
    if not m:
        return None
    word, side, shares, price, account = m.groups()
    ticker, name = _resolve_ticker(word)
    if not ticker:
        return None
    return {
        "ticker": ticker,
        "name": name,
        "side": side,
        "shares": int(shares),
        "price": float(price.replace(",", "")),
        "account": account or "",
    }


def record_trade(trade: dict) -> int:
    """매매 기록 저장. Returns trade ID."""
    conn = _get_conn()
    cur = conn.execute(
        """INSERT INTO trades (created_at, ticker, name, side, shares, price, account)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(KST).isoformat(), trade["ticker"], trade["name"],
         trade["side"], trade["shares"], trade["price"], trade["account"]),
    )
    conn.commit()
    return cur.lastrowid or 0


def mark_all_applied() -> int:
    """미반영 매매를 모두 반영 처리. Returns 처리 건수."""
    conn = _get_conn()
    n = conn.execute("UPDATE trades SET applied = 1 WHERE applied = 0").rowcount
    conn.commit()
    return n


def daily_review_text(hours: int = 26) -> str:
    """최근 N시간 매매 리뷰 — 데일리 리뷰(US_CLOSE) 브리핑용.

    사용자가 취한 액션 + 매도는 평단 대비 실현손익 추정 포함.
    applied 여부 무관 (반영됐어도 어제 액션은 리뷰 대상).
    """
    from datetime import timedelta

    conn = _get_conn()
    cutoff = (datetime.now(KST) - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT * FROM trades WHERE created_at >= ? ORDER BY created_at",
        (cutoff,),
    ).fetchall()
    if not rows:
        return ""

    from config.settings import (
        HOLDINGS_GENERAL, HOLDINGS_IRP, HOLDINGS_ISA,
        HOLDINGS_PENSION, HOLDINGS_RIA,
    )
    avg_costs: dict[str, float] = {}
    for h in (HOLDINGS_GENERAL, HOLDINGS_ISA, HOLDINGS_RIA, HOLDINGS_IRP, HOLDINGS_PENSION):
        for tk, info in h.items():
            avg_costs[tk] = info.get("avg_cost_usd") or info.get("avg_cost_krw") or 0.0

    lines = [f"사용자가 최근 {hours}시간 내 실행한 매매 ({len(rows)}건):"]
    for r in rows:
        unit = "₩" if r["ticker"].endswith((".KS", ".KQ")) else "$"
        acct = f" [{r['account']}]" if r["account"] else ""
        base = (
            f"  {r['created_at'][5:16]} {r['name']}({r['ticker']}){acct} "
            f"{r['side']} {r['shares']}주 @ {unit}{r['price']:,.0f}"
        )
        if r["side"] == "매도":
            avg = avg_costs.get(r["ticker"], 0.0)
            if avg > 0:
                pnl_each = r["price"] - avg
                pnl_pct = pnl_each / avg * 100
                base += (
                    f" → 실현손익 {unit}{pnl_each * r['shares']:+,.0f} "
                    f"({pnl_pct:+.1f}%, 평단 {unit}{avg:,.0f})"
                )
        lines.append(base)
    lines.append("→ 위 매매의 적절성을 평가하고, 잔여 포지션의 다음 조치를 제시하라.")
    return "\n".join(lines)


def pending_trades_text() -> str:
    """미반영 매매 목록 (브리핑 프롬프트 주입 + 텔레그램 응답용)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM trades WHERE applied = 0 ORDER BY created_at"
    ).fetchall()
    if not rows:
        return ""
    lines = ["⚠️ settings.py 미반영 매매 기록 — 아래 매매가 HOLDINGS에 아직 반영 안 됨:"]
    for r in rows:
        acct = f" [{r['account']}]" if r["account"] else ""
        unit = "₩" if r["ticker"].endswith((".KS", ".KQ")) else "$"
        lines.append(
            f"  {r['created_at'][:10]} {r['name']}({r['ticker']}){acct} "
            f"{r['side']} {r['shares']}주 @ {unit}{r['price']:,.0f}"
        )
    lines.append("→ 보유 수량/예수금 판단 시 위 매매를 반영해서 계산하라.")
    return "\n".join(lines)



def _trade_row_to_dict(row: sqlite3.Row) -> dict:
    """trades row → read-only dashboard/mobile API item."""
    ticker = row["ticker"]
    price = float(row["price"] or 0)
    shares = int(row["shares"] or 0)
    return {
        "id": int(row["id"]),
        "created_at": row["created_at"],
        "ticker": ticker,
        "name": row["name"] or ticker,
        "side": row["side"],
        "shares": shares,
        "price": price,
        "account": row["account"] or "",
        "applied": bool(row["applied"]),
        "total_value": price * shares,
        "currency": "KRW" if str(ticker).endswith((".KS", ".KQ")) else "USD",
    }


def pending_trade_count() -> int:
    """미반영 매매 수."""
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) AS n FROM trades WHERE applied = 0").fetchone()
    return int(row["n"] if row else 0)


def list_trades(limit: int = 20, pending_only: bool = False) -> dict:
    """최근 매매 기록을 read-only API 형태로 반환."""
    conn = _get_conn()
    limit = max(1, min(int(limit or 20), 100))
    where = "WHERE applied = 0" if pending_only else ""
    rows = conn.execute(
        f"SELECT * FROM trades {where} ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    total_row = conn.execute(f"SELECT COUNT(*) AS n FROM trades {where}").fetchone()
    items = [_trade_row_to_dict(r) for r in rows]
    return {
        "items": items,
        "count": int(total_row["n"] if total_row else len(items)),
        "pending_count": pending_trade_count(),
    }
