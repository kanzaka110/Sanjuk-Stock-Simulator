#!/usr/bin/env python3
"""Read-only portfolio cluster risk CLI for Hermes/manual review."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.portfolio_cluster_risk import (
    calculate_portfolio_cluster_risk,
    fetch_price_correlation_matrix,
    format_cluster_risk_summary,
    hermes_interpretation_payload,
)

KST = timezone(timedelta(hours=9))


def load_portfolio(path: str) -> dict:
    if path == "-":
        data = json.load(sys.stdin)
    else:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("portfolio_root_must_be_object")
    return data


def build_report(portfolio: dict, *, with_correlation: bool = False,
                 period: str = "6mo", min_points: int = 40) -> dict:
    base = calculate_portfolio_cluster_risk(portfolio)
    corr_meta = {
        "status": "not_requested", "requested_symbols": [],
        "available_symbols": [], "missing_symbols": [], "return_points": 0,
    }
    matrix = None
    if with_correlation:
        symbols = [row["symbol"] for row in base["positions"]]
        fetched_matrix, corr_meta = fetch_price_correlation_matrix(
            symbols, period=period, min_points=min_points)
        if corr_meta.get("status") == "ok":
            matrix = fetched_matrix
    report = calculate_portfolio_cluster_risk(
        portfolio, correlation_matrix=matrix, correlation_threshold=0.75)
    report["generated_at"] = datetime.now(KST).isoformat()
    report["source"] = "portfolio_cluster_risk_cli"
    report["scope"] = "samsung_manual_portfolio_only"
    report["data_quality"]["correlation_source"] = corr_meta
    report["interpretation_payload"] = hermes_interpretation_payload(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="portfolio JSON path or - for stdin")
    parser.add_argument("--output", help="optional report JSON path")
    parser.add_argument("--with-correlation", action="store_true")
    parser.add_argument("--period", default="6mo")
    parser.add_argument("--min-points", type=int, default=40)
    args = parser.parse_args()

    portfolio = load_portfolio(args.input)
    report = build_report(
        portfolio,
        with_correlation=args.with_correlation,
        period=args.period,
        min_points=max(5, args.min_points),
    )
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    print(format_cluster_risk_summary(report))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
