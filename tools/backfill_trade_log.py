"""
trade_log(trades 테이블) 백필 — 텔레그램 "매매" 명령 없이 settings.py에 직접
반영된 체결을 dashboard 거래내역/오늘 체결 위젯에도 보이게 동기화.

배경: web/index.html의 todayExecTrades()는 /api/trades(= trade_log.trades 테이블)만
읽는다. settings.py HOLDINGS_RIA를 스크린샷 기준으로 직접 갱신하면 그 체결은
trades 테이블에 없어서 "오늘 체결" 위젯에 안 뜬다. 이 스크립트는 그 갭을 메운다.

이미 반영된 체결이라 applied=1로 넣는다(재반영 대상 아님).
동일 (ticker, side, shares, price, account, date) 조합이 이미 있으면 건너뛴다(idempotent).

사용법:
  python3 tools/backfill_trade_log.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.trade_log import _get_conn  # noqa: E402

# (date, ticker, name, side, shares, price, account)
_BACKFILL = [
    ("2026-07-01", "328130.KQ", "루닛", "매수", 87, 11_450.0, "RIA"),
    ("2026-07-01", "041510.KQ", "에스엠", "매수", 13, 72_100.0, "RIA"),
    # 2026-07-02 일반(종합) 계좌 체결 (삼성증권 스샷)
    ("2026-07-02", "005930.KS", "삼성전자", "매수", 5, 292_000.0, "일반"),
    ("2026-07-02", "005930.KS", "삼성전자", "매수", 5, 290_000.0, "일반"),
    ("2026-07-02", "000660.KS", "SK하이닉스", "매수", 1, 2_300_000.0, "일반"),
    ("2026-07-02", "000660.KS", "SK하이닉스", "매수", 1, 2_350_000.0, "일반"),
]


def main() -> None:
    conn = _get_conn()
    inserted = 0
    for date, ticker, name, side, shares, price, account in _BACKFILL:
        exists = conn.execute(
            """SELECT 1 FROM trades
               WHERE ticker=? AND side=? AND shares=? AND price=? AND account=?
                 AND substr(created_at, 1, 10)=?""",
            (ticker, side, shares, price, account, date),
        ).fetchone()
        if exists:
            print(f"skip (이미 존재): {date} {name} {side} {shares}주 @ {price:,.0f}")
            continue
        conn.execute(
            """INSERT INTO trades (created_at, ticker, name, side, shares, price, account, applied)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
            (f"{date}T09:00:00+09:00", ticker, name, side, shares, price, account),
        )
        inserted += 1
        print(f"backfilled: {date} {name} {side} {shares}주 @ {price:,.0f}")
    conn.commit()
    print(f"총 {inserted}건 추가")


if __name__ == "__main__":
    main()
