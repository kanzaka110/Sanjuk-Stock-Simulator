"""
주간 성과 리포트 — AI 협업 성과 측정

목적: "협업으로 수익률이 좋아지면 원금 증액" 판단의 데이터 근거 제공.
매주 1회 (cron) 실행:
  1. 전 계좌 평가액 스냅샷 저장 + 전주 대비 수익률
  2. 벤치마크(KOSPI/S&P500) 주간 수익률과 비교 → 알파 측정
  3. 최근 7일 AI 추천 성과 (종료 건 승/패/수익률)
  4. 발굴 종목 사후 추적 — "그때 알려준 종목이 이후 어떻게 됐나"
  5. 텔레그램 전송

전부 yfinance + SQLite — API 비용 $0.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

from config.settings import DB_DIR, KST

log = logging.getLogger(__name__)

_DB_PATH = DB_DIR / "memory.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_tables(conn)
    return conn


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            total_value_krw REAL,
            total_cost_krw REAL,
            pnl_pct REAL,
            detail_json TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS discoveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT,
            price REAL,
            change_pct REAL,
            source TEXT,
            market TEXT
        )
    """)
    conn.commit()


# ═══════════════════════════════════════════════════════
# 발굴 기록 (scanner에서 호출)
# ═══════════════════════════════════════════════════════
def record_discoveries(hits, market: str) -> None:
    """전시장 발굴 결과를 기록 — 주간 사후 추적용. 같은 날 중복 스킵."""
    try:
        conn = _get_conn()
        today = datetime.now(KST).strftime("%Y-%m-%d")
        for h in hits:
            dup = conn.execute(
                "SELECT 1 FROM discoveries WHERE ticker=? AND created_at LIKE ?",
                (h.ticker, f"{today}%"),
            ).fetchone()
            if dup:
                continue
            conn.execute(
                """INSERT INTO discoveries (created_at, ticker, name, price, change_pct, source, market)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (datetime.now(KST).isoformat(), h.ticker, h.name, h.price,
                 h.change_pct, h.source, market),
            )
        conn.commit()
    except Exception as e:
        log.debug("발굴 기록 실패: %s", e)


# ═══════════════════════════════════════════════════════
# 포트폴리오 평가액 계산 + 스냅샷
# ═══════════════════════════════════════════════════════
def _compute_portfolio_value() -> tuple[float, float, float, dict]:
    """전 계좌 평가액(원화) 계산. (평가액, 원금, 손익%, 계좌별 상세)."""
    from config.settings import (
        DEFAULT_CASH, HOLDINGS_GENERAL, HOLDINGS_IRP, HOLDINGS_ISA,
        HOLDINGS_PENSION, HOLDINGS_RIA, IRP_CASH, IRP_DEFAULT_OPTION,
        ISA_CASH, PENSION_MMF, PORTFOLIO, RIA_CASH,
    )
    from core.market import _batch_quotes, _get_quote_realtime

    prices = {tk: q.price for tk, q in _batch_quotes(PORTFOLIO).items() if q}
    fx_q = _get_quote_realtime("USDKRW=X")
    fx = fx_q.price if fx_q else 1450.0

    accounts = {
        "일반": (HOLDINGS_GENERAL, DEFAULT_CASH),
        "RIA": (HOLDINGS_RIA, RIA_CASH),
        "ISA": (HOLDINGS_ISA, ISA_CASH),
        "IRP": (HOLDINGS_IRP, IRP_CASH + IRP_DEFAULT_OPTION),
        "연금": (HOLDINGS_PENSION, PENSION_MMF),
    }

    total_val = total_cost = 0.0
    detail: dict = {}
    for acct, (holdings, cash) in accounts.items():
        a_val = a_cost = 0.0
        for tk, info in holdings.items():
            price = prices.get(tk, 0.0)
            if price <= 0:
                continue
            shares = info.get("shares", 0)
            if "avg_cost_usd" in info:
                a_cost += info["avg_cost_usd"] * shares * fx
                a_val += price * shares * fx
            else:
                a_cost += info.get("avg_cost_krw", 0) * shares
                a_val += price * shares
        detail[acct] = {"value": a_val + cash, "cost": a_cost, "cash": cash}
        total_val += a_val + cash
        total_cost += a_cost

    pnl = (total_val - total_cost - sum(d["cash"] for d in detail.values())) / total_cost * 100 if total_cost else 0
    return total_val, total_cost, pnl, detail


def _save_snapshot(total_val: float, total_cost: float, pnl: float, detail: dict) -> None:
    import json

    conn = _get_conn()
    conn.execute(
        """INSERT INTO portfolio_snapshots (created_at, total_value_krw, total_cost_krw, pnl_pct, detail_json)
           VALUES (?, ?, ?, ?, ?)""",
        (datetime.now(KST).isoformat(), total_val, total_cost, round(pnl, 2),
         json.dumps(detail, ensure_ascii=False)),
    )
    conn.commit()


def _prev_snapshot(days: int = 6) -> sqlite3.Row | None:
    """N일 이전의 가장 최근 스냅샷."""
    conn = _get_conn()
    cutoff = (datetime.now(KST) - timedelta(days=days)).isoformat()
    return conn.execute(
        """SELECT * FROM portfolio_snapshots WHERE created_at < ?
           ORDER BY created_at DESC LIMIT 1""",
        (cutoff,),
    ).fetchone()


# ═══════════════════════════════════════════════════════
# 벤치마크 / 추천 성과 / 발굴 추적
# ═══════════════════════════════════════════════════════
def _benchmark_weekly() -> str:
    import yfinance as yf

    lines = []
    for tk, nm in [("^KS11", "KOSPI"), ("^GSPC", "S&P500"), ("^IXIC", "NASDAQ")]:
        try:
            h = yf.Ticker(tk).history(period="10d")["Close"]
            if len(h) >= 6:
                wk = (float(h.iloc[-1]) / float(h.iloc[-6]) - 1) * 100
                lines.append(f"  {nm}: {wk:+.2f}%")
        except Exception:
            continue
    return "\n".join(lines) if lines else "  (벤치마크 조회 실패)"


def _recommendations_weekly() -> str:
    conn = _get_conn()
    cutoff = (datetime.now(KST) - timedelta(days=7)).isoformat()
    rows = conn.execute(
        """SELECT signal, outcome, pnl_pct, name FROM predictions
           WHERE closed_at >= ? AND outcome IN ('win','loss','neutral')""",
        (cutoff,),
    ).fetchall()
    if not rows:
        return "  최근 7일 종료된 추천 없음"

    wins = [r for r in rows if r["outcome"] == "win"]
    losses = [r for r in rows if r["outcome"] == "loss"]
    avg_pnl = sum(r["pnl_pct"] or 0 for r in rows) / len(rows)
    lines = [
        f"  종료 {len(rows)}건: ✅{len(wins)}승 ❌{len(losses)}패 (평균 {avg_pnl:+.1f}%)"
    ]
    for r in sorted(rows, key=lambda x: x["pnl_pct"] or 0, reverse=True)[:3]:
        icon = "✅" if r["outcome"] == "win" else "❌" if r["outcome"] == "loss" else "➖"
        lines.append(f"  {icon} {r['name'][:14]} {r['signal']}: {r['pnl_pct']:+.1f}%")
    return "\n".join(lines)


def _calibration_curve() -> str:
    """확신도 구간별 실제 승률 — AI 캘리브레이션 점검 (최근 90일)."""
    conn = _get_conn()
    cutoff = (datetime.now(KST) - timedelta(days=90)).isoformat()
    rows = conn.execute(
        """SELECT confidence, outcome FROM predictions
           WHERE status='closed' AND outcome IN ('win','loss') AND closed_at >= ?""",
        (cutoff,),
    ).fetchall()
    if len(rows) < 10:
        return "  표본 부족 (10건 미만)"

    buckets: dict[int, list[str]] = {}
    for r in rows:
        b = (r["confidence"] // 20) * 20  # 0/20/40/60/80 구간
        buckets.setdefault(b, []).append(r["outcome"])

    lines = []
    for b in sorted(buckets):
        outs = buckets[b]
        wr = sum(1 for o in outs if o == "win") / len(outs) * 100
        # 잘 캘리브레이션됐으면 확신도 ≈ 승률
        gap = wr - (b + 10)
        flag = "✅" if abs(gap) < 15 else "⚠️"
        lines.append(f"  {flag} 확신도 {b}~{b+19}: 실제 승률 {wr:.0f}% (n={len(outs)})")
    return "\n".join(lines)


def _quality_kpi() -> str:
    """추천 품질 KPI — neutral율(엣지 부족)·data_error율 (최근 30일)."""
    conn = _get_conn()
    cutoff = (datetime.now(KST) - timedelta(days=30)).isoformat()
    rows = conn.execute(
        """SELECT outcome, COUNT(*) c FROM predictions
           WHERE status='closed' AND closed_at >= ? GROUP BY outcome""",
        (cutoff,),
    ).fetchall()
    counts = {r["outcome"]: r["c"] for r in rows}
    total = sum(counts.values())
    if total == 0:
        return "  최근 30일 종료 기록 없음"
    decided = counts.get("win", 0) + counts.get("loss", 0)
    neutral = counts.get("neutral", 0)
    bad = counts.get("data_error", 0) + counts.get("invalid", 0) + counts.get("expired", 0)
    neutral_rate = neutral / total * 100
    flag = "✅" if neutral_rate < 35 else "⚠️ 추천 엣지 부족 — 목표가가 너무 멀거나 미지근한 추천"
    return (
        f"  승부 결정 {decided}건 | 무승부 {neutral}건 ({neutral_rate:.0f}%) {flag}\n"
        f"  데이터 손실(error/invalid/expired): {bad}건 ({bad/total*100:.0f}%)"
    )


def _adoption_tracking() -> str:
    """추천 채택 추적 — 매매 기록(trades)과 추천(predictions) 조인.

    "AI 추천을 따른 매매 vs 독자 매매"를 구분 — 협업 알파의 직접 증거.
    매매 시점 ±3일 내 같은 종목·같은 방향 추천이 있으면 '채택'으로 간주.
    """
    conn = _get_conn()
    cutoff = (datetime.now(KST) - timedelta(days=30)).isoformat()
    try:
        trades = conn.execute(
            "SELECT * FROM trades WHERE created_at >= ? ORDER BY created_at",
            (cutoff,),
        ).fetchall()
    except sqlite3.OperationalError:
        return "  매매 기록 없음 (텔레그램 '매매' 명령으로 기록 시작)"
    if not trades:
        return "  최근 30일 매매 기록 없음 (텔레그램 '매매' 명령으로 기록하면 추적 시작)"

    adopted = independent = 0
    lines = []
    for t in trades:
        t_time = datetime.fromisoformat(t["created_at"])
        lo = (t_time - timedelta(days=3)).isoformat()
        hi = t_time.isoformat()
        match = conn.execute(
            """SELECT id FROM predictions
               WHERE ticker = ? AND signal = ? AND created_at BETWEEN ? AND ?
               LIMIT 1""",
            (t["ticker"], t["side"], lo, hi),
        ).fetchone()
        tag = "🤝채택" if match else "🙋독자"
        if match:
            adopted += 1
        else:
            independent += 1
        unit = "₩" if t["ticker"].endswith((".KS", ".KQ")) else "$"
        lines.append(
            f"  {tag} {t['name'][:12]} {t['side']} {t['shares']}주 @ {unit}{t['price']:,.0f}"
        )
    summary = f"  매매 {len(trades)}건 — AI 추천 채택 {adopted} / 독자 판단 {independent}"
    return summary + "\n" + "\n".join(lines[:8])


def _discoveries_followup() -> str:
    """1~4주 전 발굴 종목의 이후 수익률 추적."""
    import yfinance as yf

    conn = _get_conn()
    start = (datetime.now(KST) - timedelta(days=28)).isoformat()
    end = (datetime.now(KST) - timedelta(days=5)).isoformat()
    rows = conn.execute(
        """SELECT ticker, name, price, created_at FROM discoveries
           WHERE created_at BETWEEN ? AND ?
           GROUP BY ticker HAVING MIN(created_at)
           ORDER BY created_at LIMIT 12""",
        (start, end),
    ).fetchall()
    if not rows:
        return "  추적할 과거 발굴 종목 없음 (4주 후 데이터 축적)"

    lines = []
    for r in rows:
        try:
            h = yf.Ticker(r["ticker"]).history(period="2mo")["Close"]
            if h.empty or not r["price"]:
                continue
            ret = (float(h.iloc[-1]) / r["price"] - 1) * 100
            days = (datetime.now(KST) - datetime.fromisoformat(r["created_at"])).days
            icon = "🟢" if ret > 5 else "🔴" if ret < -5 else "⚪"
            lines.append(f"  {icon} {r['name'][:14]}({r['ticker']}): 발굴 후 {days}일, {ret:+.1f}%")
        except Exception:
            continue
    if not lines:
        return "  추적할 과거 발굴 종목 없음"
    return "\n".join(lines[:8])


# ═══════════════════════════════════════════════════════
# 리포트 생성 + 전송
# ═══════════════════════════════════════════════════════
def generate_weekly_report() -> str:
    """주간 성과 리포트 텍스트 생성 + 스냅샷 저장."""
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")

    total_val, total_cost, pnl, detail = _compute_portfolio_value()
    prev = _prev_snapshot()

    wk_change = ""
    if prev and prev["total_value_krw"]:
        diff = total_val - prev["total_value_krw"]
        diff_pct = diff / prev["total_value_krw"] * 100
        wk_change = f"전주 대비: {diff/10000:+,.0f}만원 ({diff_pct:+.2f}%)"
    else:
        wk_change = "전주 스냅샷 없음 (이번 주부터 축적)"

    _save_snapshot(total_val, total_cost, pnl, detail)

    acct_lines = "\n".join(
        f"  [{a}] {d['value']/10000:,.0f}만"
        for a, d in detail.items()
    )

    report = f"""━━━━━━━━━━━━━━━━━━━━━━━━
📊 주간 성과 리포트
{now} KST
━━━━━━━━━━━━━━━━━━━━━━━━

💼 포트폴리오
전체 평가: {total_val/10000:,.0f}만원 (평단 대비 {pnl:+.1f}%)
{wk_change}
{acct_lines}

📈 벤치마크 주간 수익률
{_benchmark_weekly()}

🤖 AI 추천 성과 (최근 7일)
{_recommendations_weekly()}

🎯 확신도 캘리브레이션 (90일 — 확신도≈승률이 목표)
{_calibration_curve()}

📐 추천 품질 KPI (30일)
{_quality_kpi()}

🤝 추천 채택 추적 (30일)
{_adoption_tracking()}

🔭 발굴 종목 사후 추적 (1~4주 전 발굴분)
{_discoveries_followup()}

━━━━━━━━━━━━━━━━━━━━━━━━"""
    return report


def send_weekly_report() -> bool:
    """리포트 생성 + 텔레그램 전송. cron 엔트리포인트."""
    try:
        report = generate_weekly_report()
    except Exception as e:
        log.error("주간 리포트 생성 실패: %s", e)
        return False

    try:
        from core.telegram import send_simple_message
        return send_simple_message(report)
    except Exception as e:
        log.error("주간 리포트 전송 실패: %s", e)
        print(report)  # 전송 실패 시 stdout
        return False


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    ok = send_weekly_report()
    sys.exit(0 if ok else 1)
