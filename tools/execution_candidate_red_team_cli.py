#!/usr/bin/env python3
"""실행 후보 Red Team staging 생성 CLI.

예시:
  python tools/execution_candidate_red_team_cli.py --input candidate.json --output-dir /tmp/red-team
  python tools/execution_candidate_red_team_cli.py --ready --market KR --limit 2 --no-ai

기본 동작은 Claude CLI(Opus + WebSearch) 분석이다. 결과는 JSON staging에만 기록하며
주문 preview/ledger/finalizer/transport를 호출하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.execution_candidate_red_team import (  # noqa: E402
    KST,
    evaluate_execution_candidate,
    validate_staging_record,
)


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _items_from_payload(payload: object) -> list[tuple[dict, dict]]:
    raw_items: list[object]
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, Mapping) and isinstance(payload.get("items"), list):
        raw_items = list(payload.get("items") or [])
    elif isinstance(payload, Mapping):
        raw_items = [payload]
    else:
        raise ValueError("input JSON must be object, list, or {items:[...]}")

    out: list[tuple[dict, dict]] = []
    for row in raw_items:
        if not isinstance(row, Mapping):
            continue
        candidate_value = row.get("candidate")
        context_value = row.get("analysis_context")
        candidate_raw: Mapping = candidate_value if isinstance(candidate_value, Mapping) else row
        context_raw: Mapping = context_value if isinstance(context_value, Mapping) else {}
        out.append((dict(candidate_raw.items()), dict(context_raw.items())))
    return out


def _ready_candidates(market: str, limit: int) -> list[tuple[dict, dict]]:
    # read-only 후보 조회만 사용한다. process_candidate/finalizer/transport는 import 금지.
    from core.toss_autonomous_pipeline import select_ready_candidates

    ready, _not_ready = select_ready_candidates(limit=limit, market=market)
    return [(dict(candidate), {}) for candidate in ready[:limit]]


def _atomic_write_json(path: Path, payload: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass


def _safe_symbol(value: object) -> str:
    symbol = str(value or "UNKNOWN").upper().strip()
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in symbol)[:80]


def _merge_context(base: Mapping, override: Mapping) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _parse_as_of(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="read-only 실행 후보 Red Team staging 생성")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="후보 JSON 파일")
    source.add_argument("--ready", action="store_true", help="현재 stock_agent_ready 후보 read-only 조회")
    parser.add_argument("--context", type=Path, help="모든 후보에 병합할 분석 컨텍스트 JSON")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "db" / "data" / "execution-red-team-staging")
    parser.add_argument("--market", choices=["KR", "US", "ALL"], default="KR")
    parser.add_argument("--symbol", help="특정 종목만 분석")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--model", default="opus")
    parser.add_argument("--no-ai", action="store_true", help="결정론 사전검사만 실행(REVIEW/BLOCK)")
    parser.add_argument("--as-of", help="재현용 ISO 시각")
    args = parser.parse_args(argv)

    limit = min(max(int(args.limit), 1), 20)
    try:
        items = _ready_candidates(args.market, limit) if args.ready else _items_from_payload(_load_json(args.input))
        global_context = {}
        if args.context:
            loaded_context = _load_json(args.context)
            if not isinstance(loaded_context, Mapping):
                raise ValueError("context JSON must be object")
            global_context = dict(loaded_context)
        as_of = _parse_as_of(args.as_of)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    if args.symbol:
        wanted = args.symbol.upper().strip()
        items = [
            (candidate, context)
            for candidate, context in items
            if str(candidate.get("symbol") or candidate.get("ticker") or "").upper().strip() == wanted
        ]
    items = items[:limit]

    if not items:
        print(json.dumps({"ok": True, "count": 0, "reason": "no_candidates"}, ensure_ascii=False))
        return 0

    records: list[dict] = []
    errors: list[dict] = []
    for candidate, item_context in items:
        context = _merge_context(global_context, item_context)
        record = evaluate_execution_candidate(
            candidate,
            context,
            model=args.model,
            as_of=as_of,
            run_ai=not args.no_ai,
        )
        validation = validate_staging_record(record)
        if validation:
            errors.append({"symbol": record.get("symbol"), "errors": validation})
            continue

        generated_date = str(record["generated_at"])[:10]
        filename = f"{_safe_symbol(record['symbol'])}-{record['review_id']}.json"
        output_path = args.output_dir / generated_date / filename
        _atomic_write_json(output_path, record)
        records.append({
            "symbol": record["symbol"],
            "review_id": record["review_id"],
            "verdict": record["verdict"],
            "output": str(output_path),
            "decision_ref": record.get("decision_ref"),
        })

    summary = {
        "ok": not errors,
        "count": len(records),
        "errors": errors,
        "records": records,
        "advisory_only": True,
        "order_side_effects": False,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not errors else 3


if __name__ == "__main__":
    raise SystemExit(main())
