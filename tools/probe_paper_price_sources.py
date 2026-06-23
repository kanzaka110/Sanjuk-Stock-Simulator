"""
tools/probe_paper_price_sources.py

Paper 가격 소스 진단 도구.
각 소스(KIS, yfinance_live, yfinance_daily)를 개별 호출해
어떤 소스가 이상 가격을 반환하는지 source_chain으로 확인한다.

사용법:
    python tools/probe_paper_price_sources.py 005930.KS --entry 72000
    python tools/probe_paper_price_sources.py NVDA --entry 130
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 프로젝트 루트 경로 추가
sys.path.insert(0, str(Path(__file__).parent.parent))


def _probe_single_source(source_name: str, fn, symbol: str, entry_price: float | None) -> dict:
    entry: dict = {"source": source_name, "price": None, "accepted": False, "reason": ""}
    try:
        q = fn(symbol)
        if q and q.price and q.price > 0:
            raw_price = float(q.price)
            entry["price"] = raw_price

            if entry_price and entry_price > 0:
                ratio = raw_price / entry_price
                entry["ratio_to_entry"] = round(ratio, 4)
                if ratio > 1.5 or ratio < 0.5:
                    entry["accepted"] = False
                    entry["reason"] = f"이상치(ratio={ratio:.4f})"
                else:
                    entry["accepted"] = True
                    entry["reason"] = "정상"
            else:
                entry["accepted"] = True
                entry["reason"] = "entry 미제공(이상치 미검사)"
        else:
            entry["reason"] = "가격 없음"
    except Exception as exc:
        entry["reason"] = f"오류: {exc}"
    return entry


def probe(symbol: str, entry_price: float | None = None) -> dict:
    """각 가격 소스를 개별 호출해 source_chain 반환."""
    from core.market import _get_quote_kis, _get_quote_yf_live, _get_quote_daily

    steps = [
        ("KIS", _get_quote_kis),
        ("yfinance_live", _get_quote_yf_live),
        ("yfinance_daily", _get_quote_daily),
    ]

    source_chain = [_probe_single_source(name, fn, symbol, entry_price) for name, fn in steps]

    # _get_quote_for_paper와 동일한 로직으로 accepted 소스 확인
    from core.toss_paper_performance import _get_quote_for_paper
    unified = _get_quote_for_paper(symbol, entry_price)

    return {
        "symbol": symbol,
        "entry_price": entry_price,
        "per_source": source_chain,
        "unified_result": {
            "price": unified["price"],
            "accepted_price_source": unified["accepted_price_source"],
            "source_chain": unified["source_chain"],
        },
    }


def _print_report(result: dict) -> None:
    symbol = result["symbol"]
    entry = result["entry_price"]
    print(f"\n=== Price Source Probe: {symbol} (entry={entry:,.0f} if entry else '미제공') ===\n")

    print("[ 소스별 개별 결과 ]")
    for s in result["per_source"]:
        price_str = f"{s['price']:,.2f}" if s["price"] else "None"
        ratio_str = f"  ratio={s.get('ratio_to_entry', 'N/A')}" if "ratio_to_entry" in s else ""
        accepted_str = "✅" if s["accepted"] else "❌"
        print(f"  {accepted_str} [{s['source']:15s}] price={price_str:>12s}{ratio_str}  reason={s['reason']}")

    print("\n[ _get_quote_for_paper() 통합 결과 ]")
    u = result["unified_result"]
    print(f"  accepted_price_source : {u['accepted_price_source']}")
    print(f"  final price           : {u['price']}")
    print("\n  source_chain:")
    for s in u["source_chain"]:
        price_str = f"{s['price']:,.2f}" if s["price"] else "None"
        accepted_str = "✅" if s["accepted"] else "❌"
        print(f"    {accepted_str} [{s['source']:15s}] price={price_str:>12s}  reason={s['reason']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper 가격 소스 진단")
    parser.add_argument("symbol", help="티커 심볼 (예: 005930.KS, NVDA)")
    parser.add_argument("--entry", type=float, default=None, help="진입 가격 (이상치 체크용)")
    parser.add_argument("--json", action="store_true", help="JSON 출력")
    args = parser.parse_args()

    result = probe(args.symbol, args.entry)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        entry = args.entry
        symbol = result["symbol"]
        print(f"\n=== Price Source Probe: {symbol} (entry={f'{entry:,.0f}' if entry else '미제공'}) ===\n")

        print("[ 소스별 개별 결과 ]")
        for s in result["per_source"]:
            price_str = f"{s['price']:,.2f}" if s["price"] else "None"
            ratio_str = f"  ratio={s.get('ratio_to_entry', 'N/A')}" if "ratio_to_entry" in s else ""
            accepted_str = "✅" if s["accepted"] else "❌"
            print(f"  {accepted_str} [{s['source']:15s}] price={price_str:>12s}{ratio_str}  reason={s['reason']}")

        print("\n[ _get_quote_for_paper() 통합 결과 ]")
        u = result["unified_result"]
        print(f"  accepted_price_source : {u['accepted_price_source']}")
        print(f"  final price           : {u['price']}")
        print("\n  source_chain:")
        for s in u["source_chain"]:
            price_str = f"{s['price']:,.2f}" if s["price"] else "None"
            accepted_str = "✅" if s["accepted"] else "❌"
            print(f"    {accepted_str} [{s['source']:15s}] price={price_str:>12s}  reason={s['reason']}")

        print()


if __name__ == "__main__":
    main()
