"""
라이브 포트폴리오 합성 — settings.HOLDINGS + 미반영 매매(trades applied=0)

배경: 보유 잔고 원본은 settings.py 수동 관리(삼성증권 공개 API 없음).
텔레그램 "매매 ..." 기록이 trades 테이블(applied=0)에 쌓이는데, 기존
대시보드는 이를 무시해 매매할 때마다 삼성증권 앱과 금액 갭이 커졌다.
이 모듈은 settings HOLDINGS/CASH를 base로 미반영 매매 델타를 합성해
"지금 시점의 유효 보유/예수금"을 계산한다 (read-only, settings 무변경).

불변식: "매매반영"(applied=1) 처리 후에는 델타가 0 → settings 값 그대로.
"""

from __future__ import annotations

import logging
import sqlite3

from config.settings import DB_DIR

log = logging.getLogger(__name__)

_DB_PATH = DB_DIR / "memory.db"

# 텔레그램 매매 기록의 계좌 표기 정규화 (자유 입력 → 대시보드 계좌명)
_ACCOUNT_ALIASES = {
    "": "일반",
    "일반": "일반",
    "general": "일반",
    "isa": "ISA",
    "ria": "RIA",
    "irp": "IRP",
    "연금": "연금저축",
    "연금저축": "연금저축",
    "pension": "연금저축",
}


def _normalize_account(raw: str) -> str:
    key = (raw or "").strip()
    return _ACCOUNT_ALIASES.get(key.lower(), _ACCOUNT_ALIASES.get(key, key or "일반"))


def _is_usd_ticker(ticker: str) -> bool:
    return not str(ticker).endswith((".KS", ".KQ"))


def pending_trades(as_of: str = "") -> tuple[dict[str, list[dict]], list[str]]:
    """미반영 매매(applied=0)를 계좌별로 그룹핑.

    Args:
        as_of: settings.HOLDINGS_AS_OF (YYYY-MM-DD). 이 날짜 **이전**에 기록된
            미반영 매매는 이미 settings에 수동 반영됐을 가능성이 있어
            이중 계산 방지를 위해 델타에서 제외하고 경고만 남긴다.

    Returns:
        ({계좌명: [trade dict, ...]}, [경고 문자열, ...])
    """
    warnings: list[str] = []
    grouped: dict[str, list[dict]] = {}
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trades WHERE applied = 0 ORDER BY created_at, id"
        ).fetchall()
        conn.close()
    except sqlite3.Error as e:
        log.warning("pending_trades 조회 실패: %s", e)
        return {}, [f"매매 기록 조회 실패: {e}"]

    for r in rows:
        created = str(r["created_at"] or "")
        trade = {
            "id": int(r["id"]),
            "created_at": created,
            "ticker": r["ticker"],
            "name": r["name"] or r["ticker"],
            "side": r["side"],
            "shares": int(r["shares"] or 0),
            "price": float(r["price"] or 0),
            "account": _normalize_account(r["account"] or ""),
        }
        if as_of and created[:10] < as_of:
            warnings.append(
                f"{created[:10]} {trade['name']} {trade['side']} {trade['shares']}주 — "
                f"HOLDINGS_AS_OF({as_of}) 이전 기록이라 이중계산 방지를 위해 제외됨. '매매반영' 필요"
            )
            continue
        grouped.setdefault(trade["account"], []).append(trade)
    return grouped, warnings


def apply_trades(
    base_holdings: dict[str, dict],
    base_cash: float,
    trades: list[dict],
    usdkrw: float,
) -> tuple[dict[str, dict], float, list[str]]:
    """base HOLDINGS + 미반영 매매 → 유효 보유/예수금 계산 (불변, 새 dict 반환).

    매수: 수량 증가 + 평단 가중평균 재계산 + 예수금 차감 (USD 종목은 ×환율)
    매도: 수량 차감 + 예수금 증가. 전량 매도 시 종목 제거.
    """
    holdings = {tk: dict(info) for tk, info in base_holdings.items()}
    cash = float(base_cash or 0)
    notes: list[str] = []

    for t in trades:
        ticker = t["ticker"]
        shares = int(t["shares"])
        price = float(t["price"])
        is_usd = _is_usd_ticker(ticker)
        avg_key = "avg_cost_usd" if is_usd else "avg_cost_krw"
        cash_delta_krw = price * shares * (usdkrw if is_usd else 1.0)

        if t["side"] == "매수":
            info = holdings.get(ticker)
            if info is None:
                holdings[ticker] = {"shares": shares, avg_key: price, "name": t["name"]}
            else:
                old_shares = float(info.get("shares", 0))
                old_avg = float(info.get(avg_key, 0) or 0)
                new_shares = old_shares + shares
                new_avg = (
                    (old_avg * old_shares + price * shares) / new_shares
                    if new_shares else price
                )
                info["shares"] = new_shares
                info[avg_key] = round(new_avg, 4)
            cash -= cash_delta_krw
        elif t["side"] == "매도":
            info = holdings.get(ticker)
            if info is None:
                notes.append(f"{t['name']} 매도 기록이 있으나 보유 내역 없음 — 델타 스킵 (수량 확인 필요)")
                continue
            old_shares = float(info.get("shares", 0))
            new_shares = old_shares - shares
            if new_shares < 0:
                notes.append(f"{t['name']} 매도 수량({shares})이 보유({old_shares:.0f}) 초과 — 0으로 클램프")
                new_shares = 0
            if new_shares <= 0:
                holdings.pop(ticker, None)
            else:
                info["shares"] = new_shares
            cash += cash_delta_krw
        else:
            notes.append(f"알 수 없는 매매 유형: {t['side']} ({t['name']})")

    return holdings, cash, notes


def effective_holdings(
    account: str,
    base_holdings: dict[str, dict],
    base_cash: float,
    usdkrw: float,
    pending_by_account: dict[str, list[dict]] | None = None,
    as_of: str = "",
) -> tuple[dict[str, dict], float, dict]:
    """계좌 하나의 유효 보유/예수금 + 메타 반환.

    Args:
        pending_by_account: pending_trades() 결과 재사용 (None이면 내부 조회).
    """
    if pending_by_account is None:
        pending_by_account, _ = pending_trades(as_of=as_of)
    trades = pending_by_account.get(account, [])
    if not trades:
        return dict(base_holdings), float(base_cash or 0), {
            "pending_trade_count": 0,
            "pending_notes": [],
        }
    holdings, cash, notes = apply_trades(base_holdings, base_cash, trades, usdkrw)
    return holdings, cash, {
        "pending_trade_count": len(trades),
        "pending_notes": notes,
        "pending_trades": [
            f"{t['created_at'][:10]} {t['name']} {t['side']} {t['shares']}주 @ {t['price']:,.0f}"
            for t in trades
        ],
    }
