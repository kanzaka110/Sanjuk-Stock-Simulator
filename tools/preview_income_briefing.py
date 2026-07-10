"""수입 중심 브리핑 payload preview (read-only).

실행:
    ./venv/bin/python tools/preview_income_briefing.py --type KR_OPEN
    ./venv/bin/python tools/preview_income_briefing.py --type US_NIGHT --json
    ./venv/bin/python tools/preview_income_briefing.py --type US_CLOSE

GET/read-only만 사용. LLM 호출/Telegram·email 전송/주문 없음.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="income briefing preview (read-only)")
    parser.add_argument("--type", default="KR_OPEN", dest="briefing_type",
                        help="KR_OPEN / KR_NIGHT / US_NIGHT / US_CLOSE / MANUAL")
    parser.add_argument("--json", action="store_true", help="구조화 payload JSON 출력")
    args = parser.parse_args()

    from core.income_briefing import (
        build_income_briefing_context,
        finalize_income_briefing,
        render_income_telegram,
    )

    payload = build_income_briefing_context(args.briefing_type)
    # preview는 LLM 없이 — normalized=None (fallback과 동일 경로)
    payload = finalize_income_briefing(payload, None, args.briefing_type)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1, default=str))
        return 0

    print(f"=== income briefing preview ({args.briefing_type}) — 전송 안 함 ===")
    for line in render_income_telegram(payload):
        print(line)
    q = payload.get("quality") or {}
    if q.get("warnings"):
        print("\n[warnings]")
        for w in q["warnings"]:
            print(" -", w)
    print("\n[sources]", q.get("sources"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
