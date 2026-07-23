"""실현 P&L 원장 재구성 (Tier 0.1) — read-only, 오프라인.

live_pilot_ledger의 실전송(live_sent) 주문을 심볼별 FIFO로 매칭해 왕복 실현손익을
산출한다. 지금까지 이 시스템은 주문 intent만 저장하고 실현손익을 측정하지 못했다.

⚠️ 데이터 한계 (정직하게):
- live_pilot_ledger는 '주문 intent(limit_price)'만 저장하고 **실제 체결가를 저장하지
  않는다.** 따라서 산출값은 limit_price 근사다.
- live_sent = '전송됨'이지 '체결 확정'이 아니다 (부분체결/미체결 구분 불가).
- 진짜 실현 P&L은 브로커 체결가 캡처(broker_order_id로 execution 조회)가 선행돼야
  한다 — 이는 Tier 0 후속 작업. 이 모듈은 '측정의 시작점'이다.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from pathlib import Path
from statistics import mean


def default_db_path() -> Path:
    return Path(__file__).resolve().parent.parent / "db" / "data" / "toss_live_pilot.db"


def _is_kr(symbol: str) -> bool:
    return str(symbol).upper().endswith((".KS", ".KQ"))


def _round_trip_cost_pct(symbol: str) -> float:
    """왕복 수수료+세금 근사 (%). backtest 모델과 동일 기준."""
    return 0.23 if _is_kr(symbol) else 0.10


def load_sent_orders(db_path: str | Path | None = None) -> list[dict]:
    """실전송(live_sent) 주문을 시간순 로드 (limit_price를 체결가 근사로 사용)."""
    path = str(db_path or default_db_path())
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT symbol, side, quantity, limit_price, created_at "
            "FROM live_pilot_ledger WHERE status='live_sent' "
            "ORDER BY created_at ASC"
        ).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        sym = str(r["symbol"] or "")
        side = str(r["side"] or "").lower()
        qty = float(r["quantity"] or 0)
        price = float(r["limit_price"] or 0)
        if not sym or side not in ("buy", "sell") or qty <= 0 or price <= 0:
            continue
        out.append({"symbol": sym, "side": side, "qty": qty,
                    "price": price, "ts": str(r["created_at"] or "")})
    return out


def reconstruct_round_trips(orders: list[dict]) -> dict:
    """심볼별 FIFO 매칭으로 왕복 실현손익 재구성.

    반환: {"round_trips": [...], "open_positions": [...], "unmatched_sells": [...]}
    각 round_trip: entry/exit 가격·수량·시각, gross/fee/net_pnl(%,금액 근사).
    """
    lots: dict[str, deque] = {}   # symbol → deque of open buy lots {qty, price, ts}
    round_trips: list[dict] = []
    unmatched_sells: list[dict] = []

    for o in orders:
        sym = o["symbol"]
        if o["side"] == "buy":
            lots.setdefault(sym, deque()).append(
                {"qty": o["qty"], "price": o["price"], "ts": o["ts"]}
            )
            continue
        # sell → FIFO 매칭
        remaining = o["qty"]
        q = lots.get(sym)
        while remaining > 0 and q:
            lot = q[0]
            matched = min(remaining, lot["qty"])
            entry_p, exit_p = lot["price"], o["price"]
            cost_pct = _round_trip_cost_pct(sym)
            entry_notional = entry_p * matched
            gross = (exit_p - entry_p) * matched
            fee = entry_notional * cost_pct / 100.0
            net = gross - fee
            round_trips.append({
                "symbol": sym, "qty": matched,
                "entry_price": entry_p, "exit_price": exit_p,
                "entry_ts": lot["ts"], "exit_ts": o["ts"],
                "gross_pnl": gross, "fee": fee, "net_pnl": net,
                "net_pnl_pct": (net / entry_notional * 100.0) if entry_notional else 0.0,
            })
            lot["qty"] -= matched
            remaining -= matched
            if lot["qty"] <= 1e-9:
                q.popleft()
        if remaining > 1e-9:
            unmatched_sells.append({"symbol": sym, "qty": remaining,
                                    "price": o["price"], "ts": o["ts"]})

    open_positions = [
        {"symbol": s, "qty": lot["qty"], "price": lot["price"], "ts": lot["ts"]}
        for s, q in lots.items() for lot in q if lot["qty"] > 1e-9
    ]
    return {"round_trips": round_trips, "open_positions": open_positions,
            "unmatched_sells": unmatched_sells}


def summarize(round_trips: list[dict]) -> dict:
    """왕복 실현손익 집계 (근사)."""
    n = len(round_trips)
    if n == 0:
        return {"n": 0}
    nets = [rt["net_pnl"] for rt in round_trips]
    pcts = [rt["net_pnl_pct"] for rt in round_trips]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    loss_sum = abs(sum(losses))
    return {
        "n": n,
        "total_net_pnl": sum(nets),
        "win_rate": len(wins) / n,
        "avg_net_pnl_pct": mean(pcts),
        "avg_win": (mean(wins) if wins else 0.0),
        "avg_loss": (mean(losses) if losses else 0.0),
        "profit_factor": (sum(wins) / loss_sum) if loss_sum > 0 else (float("inf") if wins else 0.0),
        "total_fees": sum(rt["fee"] for rt in round_trips),
    }


def realized_pnl_text(recon: dict) -> str:
    rt = recon.get("round_trips", [])
    s = summarize(rt)
    lines = ["【실현 P&L (근사 · Tier0.1)】"]
    if s.get("n", 0) == 0:
        lines.append("  완결된 왕복 트레이드 없음 (buy↔sell FIFO 매칭 0)")
    else:
        pf = s["profit_factor"]
        pft = "∞" if pf == float("inf") else f"{pf:.2f}"
        lines += [
            f"  완결 왕복 {s['n']}건 | 승률 {s['win_rate'] * 100:.1f}% | "
            f"평균 {s['avg_net_pnl_pct']:+.2f}% | PF {pft}",
            f"  총 실현손익 {s['total_net_pnl']:+,.0f} (수수료 {s['total_fees']:,.0f} 차감 후) | "
            f"평균승 {s['avg_win']:+,.0f} / 평균패 {s['avg_loss']:+,.0f}",
        ]
    lines += [
        f"  미청산 포지션 {len(recon.get('open_positions', []))}건 / "
        f"미매칭 매도 {len(recon.get('unmatched_sells', []))}건",
        "  ⚠️ limit_price 근사 (실체결가 미저장). live_sent=전송이지 체결확정 아님 — "
        "진짜 값은 브로커 체결가 캡처 후.",
    ]
    return "\n".join(lines)
