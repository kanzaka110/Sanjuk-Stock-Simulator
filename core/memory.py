"""
AI 메모리 시스템 — 과거 추천 기록 + 정확도 추적

FinMem 패턴 참고: 계층적 메모리로 AI가 자신의 과거 판단을 학습.
- 에피소드 메모리: 개별 추천 기록 (날짜, 종목, 판단, 가격)
- 성과 메모리: 추천 정확도 통계 (적중률, 평균 수익률)
"""

from __future__ import annotations

import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime

from config.settings import DB_DIR, KST

log = logging.getLogger(__name__)

DB_PATH = DB_DIR / "memory.db"

_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_tables(_conn)
    return _conn


def _init_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            signal TEXT NOT NULL,
            entry_price REAL NOT NULL,
            target_price REAL,
            stop_loss REAL,
            confidence INTEGER DEFAULT 50,
            reasoning TEXT,
            persona TEXT,
            status TEXT DEFAULT 'open',
            closed_at TEXT,
            closed_price REAL,
            pnl_pct REAL,
            outcome TEXT
        );

        CREATE TABLE IF NOT EXISTS accuracy_stats (
            ticker TEXT PRIMARY KEY,
            total_predictions INTEGER DEFAULT 0,
            correct INTEGER DEFAULT 0,
            wrong INTEGER DEFAULT 0,
            avg_pnl REAL DEFAULT 0,
            win_rate REAL DEFAULT 0,
            last_updated TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_predictions_ticker
            ON predictions(ticker);
        CREATE INDEX IF NOT EXISTS idx_predictions_status
            ON predictions(status);
    """)


# ═══════════════════════════════════════════════════════
# 추천 기록 저장
# ═══════════════════════════════════════════════════════
@dataclass(frozen=True)
class Prediction:
    """AI 추천 기록."""

    id: int = 0
    created_at: str = ""
    ticker: str = ""
    name: str = ""
    signal: str = ""  # 매수/매도/홀딩/관망
    entry_price: float = 0.0
    target_price: float = 0.0
    stop_loss: float = 0.0
    confidence: int = 50
    reasoning: str = ""
    persona: str = ""
    status: str = "open"  # open/closed
    closed_at: str = ""
    closed_price: float = 0.0
    pnl_pct: float = 0.0
    outcome: str = ""  # win/loss/neutral


def save_prediction(
    ticker: str,
    name: str,
    signal: str,
    entry_price: float,
    target_price: float = 0.0,
    stop_loss: float = 0.0,
    confidence: int = 50,
    reasoning: str = "",
    persona: str = "종합",
) -> int:
    """새 추천 기록 저장. Returns prediction ID."""
    conn = _get_conn()
    now = datetime.now(KST).isoformat()
    cursor = conn.execute(
        """INSERT INTO predictions
           (created_at, ticker, name, signal, entry_price, target_price,
            stop_loss, confidence, reasoning, persona)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (now, ticker, name, signal, entry_price, target_price,
         stop_loss, confidence, reasoning, persona),
    )
    conn.commit()
    return cursor.lastrowid or 0


def save_predictions_from_briefing(raw_json: dict) -> int:
    """브리핑 결과에서 추천 기록 자동 저장. Returns 저장된 건수."""
    count = 0

    for row in raw_json.get("strategy_buy", []):
        try:
            entry = _parse_price(row.get("entry_price", "0"))
            target = _parse_price(row.get("target_price", "0"))
            stop = _parse_price(row.get("stop_loss", "0"))

            save_prediction(
                ticker=row.get("ticker", ""),
                name=row.get("name", ""),
                signal="매수",
                entry_price=entry,
                target_price=target,
                stop_loss=stop,
                reasoning=row.get("reason", "")[:200],
            )
            count += 1
        except Exception as e:
            log.debug(f"매수 추천 저장 실패: {e}")

    for row in raw_json.get("strategy_sell", []):
        try:
            entry = _parse_price(row.get("current_price", "0"))
            target = _parse_price(row.get("take_profit", "0"))
            stop = _parse_price(row.get("stop_loss", "0"))

            save_prediction(
                ticker=row.get("ticker", ""),
                name=row.get("name", ""),
                signal="매도",
                entry_price=entry,
                target_price=target,
                stop_loss=stop,
                reasoning=row.get("reason", "")[:200],
            )
            count += 1
        except Exception as e:
            log.debug(f"매도 추천 저장 실패: {e}")

    return count


def _parse_price(val: str | float) -> float:
    """가격 문자열 파싱. '₩201,000' → 201000.0"""
    if isinstance(val, (int, float)):
        return float(val)
    cleaned = str(val).replace("₩", "").replace("$", "").replace(",", "").replace("원", "").strip()
    # 범위 표기 (예: "198,000~202,000") → 중간값
    if "~" in cleaned:
        parts = cleaned.split("~")
        try:
            return (float(parts[0]) + float(parts[1])) / 2
        except (ValueError, IndexError):
            pass
    try:
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        return 0.0


# ═══════════════════════════════════════════════════════
# 추천 결과 평가 (자동)
# ═══════════════════════════════════════════════════════
def evaluate_open_predictions(current_prices: dict[str, float]) -> int:
    """미결 추천을 현재가로 평가하여 종료 처리.

    Args:
        current_prices: {ticker: current_price}

    Returns:
        종료된 건수
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM predictions WHERE status = 'open'"
    ).fetchall()

    closed_count = 0
    now = datetime.now(KST).isoformat()

    for row in rows:
        ticker = row["ticker"]
        if ticker not in current_prices:
            continue

        current = current_prices[ticker]
        entry = row["entry_price"]
        target = row["target_price"]
        stop = row["stop_loss"]
        signal = row["signal"]

        if entry <= 0:
            continue

        should_close = False
        outcome = "neutral"

        if signal == "매수":
            pnl = (current - entry) / entry * 100
            if target > 0 and current >= target:
                should_close = True
                outcome = "win"
            elif stop > 0 and current <= stop:
                should_close = True
                outcome = "loss"
            # 7일 이상 경과 시 자동 평가
            elif _days_since(row["created_at"]) >= 7:
                should_close = True
                outcome = "win" if pnl > 0 else "loss" if pnl < -3 else "neutral"
        elif signal == "매도":
            pnl = (entry - current) / entry * 100
            if target > 0 and current <= target:
                should_close = True
                outcome = "win"
            elif stop > 0 and current >= stop:
                should_close = True
                outcome = "loss"
            elif _days_since(row["created_at"]) >= 7:
                should_close = True
                outcome = "win" if pnl > 0 else "loss" if pnl < -3 else "neutral"
        else:
            continue

        if should_close:
            pnl_pct = (current - entry) / entry * 100 if signal == "매수" else (entry - current) / entry * 100
            conn.execute(
                """UPDATE predictions
                   SET status='closed', closed_at=?, closed_price=?,
                       pnl_pct=?, outcome=?
                   WHERE id=?""",
                (now, current, round(pnl_pct, 2), outcome, row["id"]),
            )
            closed_count += 1

    if closed_count > 0:
        conn.commit()
        _update_accuracy_stats()

    return closed_count


def _days_since(iso_date: str) -> int:
    try:
        created = datetime.fromisoformat(iso_date)
        now = datetime.now(KST)
        return (now - created).days
    except Exception:
        return 0


def _update_accuracy_stats() -> None:
    """정확도 통계 업데이트."""
    conn = _get_conn()
    now = datetime.now(KST).isoformat()

    rows = conn.execute(
        """SELECT ticker,
                  COUNT(*) as total,
                  SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                  SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                  AVG(pnl_pct) as avg_pnl
           FROM predictions
           WHERE status='closed'
           GROUP BY ticker"""
    ).fetchall()

    for row in rows:
        total = row["total"]
        wins = row["wins"]
        win_rate = (wins / total * 100) if total > 0 else 0

        conn.execute(
            """INSERT OR REPLACE INTO accuracy_stats
               (ticker, total_predictions, correct, wrong, avg_pnl, win_rate, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (row["ticker"], total, wins, row["losses"],
             round(row["avg_pnl"] or 0, 2), round(win_rate, 1), now),
        )
    conn.commit()


# ═══════════════════════════════════════════════════════
# 메모리 조회 (프롬프트용)
# ═══════════════════════════════════════════════════════
def get_recent_predictions(limit: int = 20) -> list[Prediction]:
    """최근 추천 기록 조회."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM predictions
           ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()

    return [
        Prediction(
            id=r["id"],
            created_at=r["created_at"],
            ticker=r["ticker"],
            name=r["name"],
            signal=r["signal"],
            entry_price=r["entry_price"],
            target_price=r["target_price"] or 0,
            stop_loss=r["stop_loss"] or 0,
            confidence=r["confidence"],
            reasoning=r["reasoning"] or "",
            persona=r["persona"] or "",
            status=r["status"],
            closed_at=r["closed_at"] or "",
            closed_price=r["closed_price"] or 0,
            pnl_pct=r["pnl_pct"] or 0,
            outcome=r["outcome"] or "",
        )
        for r in rows
    ]


def get_accuracy_summary() -> dict[str, dict]:
    """종목별 정확도 통계."""
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM accuracy_stats").fetchall()
    return {
        r["ticker"]: {
            "total": r["total_predictions"],
            "wins": r["correct"],
            "losses": r["wrong"],
            "avg_pnl": r["avg_pnl"],
            "win_rate": r["win_rate"],
        }
        for r in rows
    }


def memory_to_text() -> str:
    """메모리를 텍스트로 변환 (프롬프트 삽입용)."""
    predictions = get_recent_predictions(10)
    accuracy = get_accuracy_summary()

    lines = ["【AI 메모리 — 과거 추천 기록】"]

    if accuracy:
        lines.append("\n  [정확도 통계]")
        for ticker, stats in accuracy.items():
            lines.append(
                f"  {ticker}: {stats['total']}건 중 {stats['wins']}적중 "
                f"(승률 {stats['win_rate']:.0f}%, 평균 {stats['avg_pnl']:+.1f}%)"
            )

    if predictions:
        open_preds = [p for p in predictions if p.status == "open"]
        closed_preds = [p for p in predictions if p.status == "closed"]

        if open_preds:
            lines.append("\n  [미결 추천]")
            for p in open_preds[:5]:
                lines.append(
                    f"  {p.created_at[:10]} {p.name} {p.signal} "
                    f"진입 {p.entry_price:,.0f} → 목표 {p.target_price:,.0f}"
                )

        if closed_preds:
            lines.append("\n  [최근 종료]")
            for p in closed_preds[:5]:
                icon = "✅" if p.outcome == "win" else "❌" if p.outcome == "loss" else "➖"
                lines.append(
                    f"  {icon} {p.name} {p.signal}: {p.pnl_pct:+.1f}% [{p.outcome}]"
                )

    if len(lines) == 1:
        lines.append("  (기록 없음 — 첫 브리핑 후 축적됩니다)")

    return "\n".join(lines)
