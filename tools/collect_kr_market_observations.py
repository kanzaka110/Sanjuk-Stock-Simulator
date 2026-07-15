#!/usr/bin/env python3
"""Collect bounded KIS/Naver observations into the append-only v2 store."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.kr_market_observation_collector import (  # noqa: E402
    collect_investor_observations,
    collect_orderbook_observations,
    run_candidate_observation_cycle,
)


def _default_db_path() -> Path:
    from config.settings import DB_DIR

    return Path(DB_DIR) / "source_observations_v2.db"


def _open_store(path: Path):
    from core.source_observations_v2 import SourceObservationStoreV2

    return SourceObservationStoreV2(path)


def _parse_symbols(raw: str) -> tuple[str, ...]:
    parts = tuple(part.strip().upper() for part in raw.split(","))
    if not parts or any(not part for part in parts):
        raise ValueError("symbols_invalid")
    return parts


def _contains_failure(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("status") == "failed":
            return True
        if value.get("error_type") not in (None, "", "none"):
            return True
        return any(_contains_failure(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_failure(item) for item in value)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("all", "orderbook", "investor"), required=True)
    parser.add_argument("--symbols", required=True, help="comma-separated .KS/.KQ symbols")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    try:
        symbols = _parse_symbols(args.symbols)
        store = _open_store(args.db or _default_db_path())
        run_id = args.run_id or f"kr-observation-{uuid.uuid4().hex}"
        if args.mode == "all":
            result = run_candidate_observation_cycle(
                symbols,
                store=store,
                run_id=run_id,
            )
        elif args.mode == "orderbook":
            result = collect_orderbook_observations(
                symbols,
                store=store,
                run_id=run_id,
            )
        else:
            result = collect_investor_observations(
                symbols,
                store=store,
                run_id=run_id,
            )
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1

    failed = _contains_failure(result)
    print(
        json.dumps(
            {"ok": not failed, "result": result},
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
