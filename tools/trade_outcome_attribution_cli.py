#!/usr/bin/env python3
"""Read-only trade outcome attribution CLI with optional benchmark history."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.trade_outcome_attribution import (
    calculate_trade_outcome_attribution,
    hermes_interpretation_payload,
    normalize_execution_records,
)

KST = timezone(timedelta(hours=9))


def load_payload(path: str) -> dict[str, Any]:
    if path == "-":
        value = json.load(sys.stdin)
    else:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("payload_root_must_be_object")
    return value


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=KST) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def _default_benchmark(symbol: str) -> str:
    return "^KS11" if str(symbol or "").upper().endswith((".KS", ".KQ")) else "^GSPC"


def fetch_benchmark_returns(
    predictions: list[dict[str, Any]],
) -> tuple[dict[Any, float], dict[str, Any]]:
    """추천 생성일~종료일의 벤치마크 수익률을 배치 GET으로 계산한다."""
    import pandas as pd
    import yfinance as yf

    eligible = []
    for row in predictions:
        prediction_id = row.get("id")
        created = _parse_time(row.get("created_at"))
        closed = _parse_time(row.get("closed_at"))
        if prediction_id is None or not created or not closed or closed < created:
            continue
        benchmark = str(row.get("benchmark_ticker") or "").strip()
        if not benchmark:
            benchmark = _default_benchmark(str(row.get("ticker") or row.get("symbol") or ""))
        eligible.append((prediction_id, benchmark, created.date(), closed.date()))

    tickers = sorted({item[1] for item in eligible})
    metadata: dict[str, Any] = {
        "source": "yfinance_batch_history",
        "requested_predictions": len(eligible),
        "requested_benchmarks": tickers,
        "available_predictions": 0,
        "missing_prediction_ids": [],
        "status": "not_requested" if not eligible else "unavailable",
    }
    if not eligible:
        return {}, metadata

    start = min(item[2] for item in eligible) - timedelta(days=5)
    end = max(item[3] for item in eligible) + timedelta(days=5)
    try:
        data = yf.download(
            tickers,
            start=start.isoformat(),
            end=end.isoformat(),
            auto_adjust=True,
            progress=False,
            threads=True,
            group_by="column",
        )
        if data is None or getattr(data, "empty", True):
            metadata["missing_prediction_ids"] = [item[0] for item in eligible]
            return {}, metadata
        close = data["Close"] if "Close" in data else pd.DataFrame()
        if isinstance(close, pd.Series):
            close = close.to_frame(name=tickers[0])
        if not isinstance(close, pd.DataFrame):
            metadata["missing_prediction_ids"] = [item[0] for item in eligible]
            return {}, metadata
        close.columns = [str(column) for column in close.columns]

        returns: dict[Any, float] = {}
        missing = []
        for prediction_id, benchmark, created, closed_date in eligible:
            if benchmark not in close.columns:
                missing.append(prediction_id)
                continue
            series: pd.Series = pd.Series(close[benchmark]).dropna()
            selected_values = [
                float(value)
                for index, value in series.items()
                if created <= pd.Timestamp(str(index)).date() <= closed_date
            ]
            if len(selected_values) < 2 or selected_values[0] <= 0:
                missing.append(prediction_id)
                continue
            returns[prediction_id] = round(
                (selected_values[-1] / selected_values[0] - 1) * 100, 4)
        metadata.update({
            "available_predictions": len(returns),
            "missing_prediction_ids": missing,
            "status": "ok" if returns else "insufficient_history",
        })
        return returns, metadata
    except Exception as exc:
        metadata["status"] = f"error:{type(exc).__name__}"
        metadata["missing_prediction_ids"] = [item[0] for item in eligible]
        return {}, metadata


def build_report(payload: dict[str, Any], *, with_benchmark: bool = False) -> dict[str, Any]:
    predictions = [dict(row) for row in (payload.get("predictions") or []) if isinstance(row, dict)]
    executions = normalize_execution_records(
        manual_trades=payload.get("manual_trades") or [],
        live_events=payload.get("live_events") or [],
        broker_orders=payload.get("broker_orders") or [],
    )
    benchmark_returns: dict[Any, float] = {}
    benchmark_meta = {
        "source": "none",
        "requested_predictions": 0,
        "requested_benchmarks": [],
        "available_predictions": 0,
        "missing_prediction_ids": [],
        "status": "not_requested",
    }
    if with_benchmark:
        benchmark_returns, benchmark_meta = fetch_benchmark_returns(predictions)
    report = calculate_trade_outcome_attribution(
        predictions,
        executions=executions,
        benchmark_returns_by_prediction_id=benchmark_returns,
    )
    report["generated_at"] = datetime.now(KST).isoformat()
    report["source"] = "trade_outcome_attribution_cli"
    report["scope"] = str(payload.get("scope") or "provided_read_only_snapshot")
    report["benchmark_attribution"]["source_metadata"] = benchmark_meta
    report["interpretation_payload"] = hermes_interpretation_payload(report)
    return report


def format_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    benchmark = report.get("benchmark_attribution") or {}
    quality = report.get("data_quality") or {}
    return "\n".join([
        (
            f"매매 결과 귀속: 추천 {summary.get('total_predictions', 0)}건 · "
            f"판정 {summary.get('resolved_predictions', 0)}건 · "
            f"승패 평가 {summary.get('evaluated_predictions', 0)}건"
        ),
        (
            f"추천 승률 {summary.get('win_rate_pct')}% · 평균 방향수익 "
            f"{summary.get('avg_recommendation_pnl_pct')}%"
        ),
        (
            f"실제 체결 cohort {summary.get('observed_real_executions', 0)}건 · "
            f"직접 연결 {summary.get('directly_linked_executions', 0)}건 · "
            f"연결률 {summary.get('execution_linkage_rate_pct', 0)}%"
        ),
        (
            f"벤치마크 {benchmark.get('status', 'not_requested')} "
            f"{benchmark.get('available_count', 0)}건 · 평균 선택 알파 "
            f"{benchmark.get('avg_selection_alpha_pct')}%"
        ),
        (
            f"데이터 품질: 평가율 {quality.get('evaluated_rate_pct', 0)}% · "
            f"벤치마크 커버리지 {quality.get('benchmark_coverage_pct', 0)}%"
        ),
        "해석 전용 · 자동매도/주문 권한 없음",
    ])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="input JSON path or - for stdin")
    parser.add_argument("--output", help="optional report JSON path")
    parser.add_argument("--with-benchmark", action="store_true")
    args = parser.parse_args()
    payload = load_payload(args.input)
    report = build_report(payload, with_benchmark=args.with_benchmark)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(format_summary(report))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
