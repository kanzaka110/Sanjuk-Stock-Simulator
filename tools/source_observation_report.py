#!/usr/bin/env python3
"""Render append-only source observation health as JSON."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.source_observations import SourceObservationStore  # noqa: E402


def build_source_observation_report(db_path: str | Path) -> dict[str, Any]:
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"source_observation_db_not_found:{path}")
    store = SourceObservationStore.open_read_only(path)
    sources = [asdict(row) for row in store.source_health()]
    return {
        "schema_version": 1,
        "db_path": str(path),
        "source_count": len(sources),
        "sources": sources,
    }


def _default_db_path() -> Path:
    from config.settings import DB_DIR

    return Path(DB_DIR) / "source_observations.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = build_source_observation_report(args.db or _default_db_path())
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__},
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {"ok": True, **report},
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
