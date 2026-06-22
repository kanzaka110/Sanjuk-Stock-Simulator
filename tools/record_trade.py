#!/usr/bin/env python3
"""Claude/터미널용 안전 매매 기록 CLI.

실제 주문/자동매매가 아니라 core.trade_log.trades ledger에만 저장한다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.trade_log import _resolve_ticker, parse_trade_message, record_trade


def _trade_from_args(args: argparse.Namespace) -> dict | None:
    if args.text:
        text = " ".join(args.text).strip()
        if not text.startswith("매매 "):
            text = "매매 " + text
        return parse_trade_message(text)

    if not (args.ticker and args.side and args.shares and args.price):
        return None

    ticker_arg = args.ticker.strip()
    if "." in ticker_arg or ticker_arg.isalpha():
        ticker = ticker_arg.upper()
        name = args.name or ticker
    else:
        ticker, name = _resolve_ticker(ticker_arg)
        if args.name:
            name = args.name
    if not ticker:
        return None
    return {
        "ticker": ticker,
        "name": name,
        "side": args.side,
        "shares": int(args.shares),
        "price": float(str(args.price).replace(",", "")),
        "account": args.account or "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="매매 기록을 trades ledger에 저장한다. 실제 주문 아님.")
    parser.add_argument("text", nargs="*", help='예: "매매 삼성전자 매수 10주 68500 일반"')
    parser.add_argument("--ticker")
    parser.add_argument("--name")
    parser.add_argument("--side", choices=["매수", "매도"])
    parser.add_argument("--shares", type=int)
    parser.add_argument("--price")
    parser.add_argument("--account", default="")
    args = parser.parse_args()

    trade = _trade_from_args(args)
    if trade is None:
        parser.print_help()
        print("\n입력 예: python3 tools/record_trade.py '매매 삼성전자 매수 10주 68500 일반'", file=sys.stderr)
        return 2

    tid = record_trade(trade)
    unit = "₩" if trade["ticker"].endswith((".KS", ".KQ")) else "$"
    acct = f" [{trade['account']}]" if trade.get("account") else ""
    total = trade["shares"] * trade["price"]
    print(
        f"기록 완료 #{tid}: {trade['name']}({trade['ticker']}){acct} "
        f"{trade['side']} {trade['shares']}주 @ {unit}{trade['price']:,.0f} "
        f"= {unit}{total:,.0f}"
    )
    print("상태: 미반영 · 앱/대시보드에서 조회 가능 · 실제 주문 아님")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
