"""트랙레코드 리포트 — 실행(주문 lifecycle) + 실현 대리성과 (read-only, 오프라인).

두 축:
1. 실행 트랙레코드: live_pilot_ledger의 주문 상태/전송성공률 등 (실제로 주문이
   나갔는가). 실현 P&L이 아님.
2. 실현 대리성과: quality_gate_decisions.return_5d 기반 기대값/PF/승률.

⚠️ 주의: 시스템은 주문 intent/lifecycle만 저장하고 **체결 왕복 실현 P&L을
저장하지 않는다** (paper_trades 0행, 원장에 exit/pnl 컬럼 없음). 진짜 P&L
트랙레코드를 위해선 포지션 청산 시 왕복 실현손익을 별도 저장해야 한다.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path
from statistics import mean, median


def _db(name: str) -> Path:
    return Path(__file__).resolve().parent.parent / "db" / "data" / name


# ── 실행 트랙레코드 ────────────────────────────────────────
def load_execution_rows(db_path: str | Path | None = None) -> list[dict]:
    path = str(db_path or _db("toss_live_pilot.db"))
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in con.execute(
                "SELECT status, side, live_order_sent, broker_order_id, created_at, "
                "failure_reason FROM live_pilot_ledger"
            )
        ]
    finally:
        con.close()


# 양성(현금 소진) = 브로커가 초과 자본 발주를 안전하게 거부 — 실장애 아님.
_BENIGN_FAILURE = {"insufficient_buying_power"}


def classify_send_failure(reason: str | None) -> str:
    """live_send_failed의 failure_reason을 원인 클래스로 분류."""
    r = str(reason or "").lower()
    if not r.strip():
        return "legacy_unrecorded"  # 07-04 이전 미기록 레거시
    if "insufficient" in r or "cash_blocked" in r:
        return "insufficient_buying_power"  # 양성: 현금 소진
    if "hours" in r:
        return "order_hours_closed"
    if "401" in r or "auth" in r:
        return "auth"
    if "sellable_position_not_ready" in r or "position_not_ready" in r:
        return "position_timing"
    if "reconcile" in r:
        return "reconcile"
    return "other"


def summarize_execution(rows: list[dict]) -> dict:
    """주문 lifecycle 통계 (실현 P&L 아님)."""
    n = len(rows)
    if n == 0:
        return {"n": 0}
    by_status = dict(Counter(str(r.get("status") or "") for r in rows))
    sent = by_status.get("live_sent", 0)
    failed = by_status.get("live_send_failed", 0)
    attempts = sent + failed

    # 실패 원인 분류 → 양성(현금소진)/레거시 제외한 '진짜 오류율'
    fail_rows = [r for r in rows if str(r.get("status") or "") == "live_send_failed"]
    fail_classes = dict(Counter(classify_send_failure(r.get("failure_reason")) for r in fail_rows))
    benign = sum(v for k, v in fail_classes.items() if k in _BENIGN_FAILURE)
    legacy = fail_classes.get("legacy_unrecorded", 0)
    real_failed = failed - benign - legacy
    real_attempts = sent + real_failed

    dates = [str(r.get("created_at") or "") for r in rows if r.get("created_at")]
    return {
        "n": n,
        "by_status": by_status,
        "buy": sum(1 for r in rows if str(r.get("side") or "").lower() == "buy"),
        "sell": sum(1 for r in rows if str(r.get("side") or "").lower() == "sell"),
        "sent": sent,
        "failed": failed,
        "send_success_rate": (sent / attempts if attempts else None),
        "fail_classes": fail_classes,
        "benign_failed": benign,
        "legacy_failed": legacy,
        "real_failed": real_failed,
        # 양성(현금소진)·레거시 제외한 진짜 오류율
        "real_error_rate": (real_failed / real_attempts if real_attempts else None),
        "first": min(dates) if dates else None,
        "last": max(dates) if dates else None,
    }


# ── 실현 대리성과 ──────────────────────────────────────────
def proxy_performance(rows: list[dict]) -> dict:
    """return_5d를 실현 대리지표로 본 성과 (기대값/PF/승률). 동일가중."""
    rets = [float(r["return_5d"]) for r in rows if r.get("return_5d") is not None]
    n = len(rets)
    if n == 0:
        return {"n": 0}
    wins = [v for v in rets if v > 0]
    losses = [v for v in rets if v <= 0]
    loss_sum = abs(sum(losses))
    if loss_sum > 0:
        profit_factor = sum(wins) / loss_sum
    else:
        profit_factor = float("inf") if wins else 0.0
    return {
        "n": n,
        "win_rate": len(wins) / n,
        "expectancy": mean(rets),  # 결정당 평균 5d 수익(%)
        "median_return": median(rets),
        "avg_win": (mean(wins) if wins else 0.0),
        "avg_loss": (mean(losses) if losses else 0.0),
        "profit_factor": profit_factor,
        "cum_return": sum(rets),  # 동일가중 누적(복리 아님)
    }


def weekly_breakdown(rows: list[dict]) -> list[dict]:
    """ISO 주별 결정 수/평균 5d수익/승률."""
    buckets: dict[str, list[float]] = {}
    for r in rows:
        if r.get("return_5d") is None or not r.get("decided_at"):
            continue
        try:
            y, w, _ = date.fromisoformat(str(r["decided_at"])[:10]).isocalendar()
        except ValueError:
            continue
        buckets.setdefault(f"{y}-W{w:02d}", []).append(float(r["return_5d"]))
    out = []
    for wk in sorted(buckets):
        vals = buckets[wk]
        out.append(
            {
                "week": wk,
                "n": len(vals),
                "mean_return": mean(vals),
                "win_rate": sum(1 for v in vals if v > 0) / len(vals),
            }
        )
    return out


# ── 텍스트 ─────────────────────────────────────────────────
def track_record_text(execution: dict, perf: dict, weekly: list[dict]) -> str:
    lines = ["【트랙레코드】"]
    if execution.get("n", 0):
        sr = execution["send_success_rate"]
        srt = f"{sr * 100:.1f}%" if sr is not None else "-"
        rer = execution.get("real_error_rate")
        rert = f"{rer * 100:.1f}%" if rer is not None else "-"
        lines += [
            f"  실행: 주문 {execution['n']}건 (buy {execution['buy']}/sell {execution['sell']}), "
            f"{execution['first'][:10]}~{execution['last'][:10]}",
            f"    전송성공 {execution['sent']} / 실패 {execution['failed']} → 성공률 {srt}",
            f"    실패분류: {execution.get('fail_classes', {})}",
            f"    ↳ 양성(현금소진) {execution.get('benign_failed', 0)} / 레거시 {execution.get('legacy_failed', 0)} "
            f"제외 → 진짜 오류 {execution.get('real_failed', 0)}건, 진짜 오류율 {rert}",
            f"    상태: {execution['by_status']}",
        ]
    else:
        lines.append("  실행: (주문 원장 없음)")

    if perf.get("n", 0):
        pf = perf["profit_factor"]
        pft = "∞" if pf == float("inf") else f"{pf:.2f}"
        lines += [
            f"  실현 대리성과(return_5d, n={perf['n']}): "
            f"승률 {perf['win_rate'] * 100:.1f}% | 기대값 {perf['expectancy']:+.2f}%/건 | PF {pft}",
            f"    평균승 {perf['avg_win']:+.2f}% / 평균패 {perf['avg_loss']:+.2f}% | "
            f"누적(동일가중) {perf['cum_return']:+.1f}%",
        ]
    else:
        lines.append("  실현 대리성과: (return_5d 데이터 없음)")

    if weekly:
        lines.append("  주별:")
        for w in weekly:
            lines.append(
                f"    {w['week']} n={w['n']:3d} 평균 {w['mean_return']:+.2f}% 승률 {w['win_rate'] * 100:.0f}%"
            )
    lines.append("  ⚠️ 체결 왕복 실현 P&L 미저장 — 위 실현치는 return_5d 대리지표 (진짜 P&L 원장 필요)")
    return "\n".join(lines)
