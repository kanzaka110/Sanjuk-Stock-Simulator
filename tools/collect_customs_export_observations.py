#!/usr/bin/env python3
"""관세청 10일 수출 원문·정규화·shadow 피처를 append-only로 수집한다."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.customs_export_observation_collector import (  # noqa: E402
    collect_customs_export_observations,
)
from core.customs_export_workdays import (  # noqa: E402
    fetch_kcs_workday_observations,
)

_KST = timezone(timedelta(hours=9))


def _default_db_path() -> Path:
    from config.settings import DB_DIR

    return Path(DB_DIR) / "source_observations_v2.db"


def _open_store(path: Path):
    from core.source_observations_v2 import SourceObservationStoreV2

    return SourceObservationStoreV2(path)


def _load_service_key() -> str:
    from config.settings import DATA_GO_KR_SERVICE_KEY

    return DATA_GO_KR_SERVICE_KEY.strip()


def _default_query_range(now: datetime) -> tuple[str, str]:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now_must_be_timezone_aware")
    local = now.astimezone(_KST)
    current_index = local.year * 12 + local.month - 1
    start_year, start_month_zero = divmod(current_index - 13, 12)
    return (
        f"{start_year:04d}{start_month_zero + 1:02d}",
        f"{local.year:04d}{local.month:02d}",
    )


def _is_failure(result: dict) -> bool:
    return result.get("status") == "failed" or result.get("error_type") not in (
        None,
        "",
        "none",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", help="포함 시작연월 YYYYMM")
    parser.add_argument("--end", help="포함 종료연월 YYYYMM")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument(
        "--collection-mode",
        choices=("scheduled_live", "research_backfill", "manual_replay"),
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    try:
        if (args.start is None) != (args.end is None):
            raise ValueError("start_end_pair_required")
        explicit_range = args.start is not None
        start_yymm, end_yymm = (
            (args.start, args.end)
            if explicit_range
            else _default_query_range(datetime.now(_KST))
        )
        collection_mode = args.collection_mode or (
            "research_backfill" if explicit_range else "scheduled_live"
        )
        store = _open_store(args.db or _default_db_path())
        run_id = args.run_id or f"customs-export-{uuid.uuid4().hex}"
        result = collect_customs_export_observations(
            start_yymm,
            end_yymm,
            store=store,
            run_id=run_id,
            service_key=_load_service_key(),
            workday_fetcher=(
                fetch_kcs_workday_observations
                if collection_mode == "scheduled_live"
                else None
            ),
            collection_mode=collection_mode,
        )
    except Exception as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "ok": False},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1

    failed = _is_failure(result)
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
