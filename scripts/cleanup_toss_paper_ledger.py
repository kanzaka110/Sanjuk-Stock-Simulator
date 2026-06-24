"""
Toss paper ledger 정리 도구 — 수동 실행 전용

- previewed 레코드를 expired로 전환 (삭제 없음)
- approved duplicate open 감지 (출력만, 자동 취소 없음)
- live_order_allowed=False / dry_run=True 불변

사용법:
  python scripts/cleanup_toss_paper_ledger.py --dry-run
  python scripts/cleanup_toss_paper_ledger.py --expire-preview-minutes 60
  python scripts/cleanup_toss_paper_ledger.py --expire-preview-minutes 30 --source telegram_paper_preview
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def _print_separator(label: str = "") -> None:
    if label:
        print(f"\n{'─' * 20} {label} {'─' * 20}")
    else:
        print("─" * 55)


def _show_ledger_state() -> None:
    from core.toss_paper_ledger import paper_ledger_summary
    s = paper_ledger_summary()
    counts = s.get("counts", {})
    total = s.get("total", 0)
    print(f"  총 {total}건: ", end="")
    parts = [f"{st}={cnt}" for st, cnt in sorted(counts.items())]
    print(" / ".join(parts) if parts else "(없음)")


def _show_duplicate_opens() -> None:
    """approved(open) 중 동일 symbol 중복 감지 후 출력."""
    from core.toss_paper_ledger import list_paper_orders
    from collections import Counter

    orders = list_paper_orders(status="approved", limit=200)
    sym_counts = Counter(o["symbol"] for o in orders)
    dupes = {sym: cnt for sym, cnt in sym_counts.items() if cnt >= 2}
    if not dupes:
        print("  ✅ duplicate open 없음")
        return
    print(f"  ⚠️  duplicate open 감지 ({len(dupes)}종목) — 수동 취소 필요:")
    for sym, cnt in sorted(dupes.items()):
        entries = [o for o in orders if o["symbol"] == sym]
        print(f"    {sym}: {cnt}건")
        for e in entries:
            print(f"      paper_id={e['paper_id']}  created={e['created_at']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Toss paper ledger 정리 도구 (삭제 없음, 승인 자동 취소 없음)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="상태 조회 + 계획 출력만, 실제 변경 없음",
    )
    parser.add_argument(
        "--expire-preview-minutes",
        type=int,
        default=None,
        metavar="N",
        help="N분 이상 경과한 previewed를 expired로 전환 (기본: 실행 안 함)",
    )
    parser.add_argument(
        "--source",
        type=str,
        default=None,
        metavar="SOURCE",
        help="source 컬럼 필터 (예: telegram_paper_preview). 미지정=전체",
    )
    args = parser.parse_args()

    print("=" * 55)
    print("[Toss Paper Ledger 정리]  실제 주문 0건 · 삭제 없음")
    if args.dry_run:
        print("  모드: --dry-run (변경 없음)")
    print("=" * 55)

    _print_separator("현재 ledger 상태")
    _show_ledger_state()

    _print_separator("duplicate open 감지")
    _show_duplicate_opens()

    if args.expire_preview_minutes is not None:
        minutes = args.expire_preview_minutes
        _print_separator(f"previewed → expired (>{minutes}분 경과)")

        if args.dry_run:
            # 개수만 조회
            from core.toss_paper_ledger import list_paper_orders
            from datetime import datetime, timezone, timedelta
            KST = timezone(timedelta(hours=9))
            cutoff = datetime.now(KST) - timedelta(minutes=minutes)
            orders = list_paper_orders(status=None, limit=500)
            targets = [
                o for o in orders
                if o.get("status") == "previewed"
                and (args.source is None or o.get("source") == args.source)
            ]
            stale = []
            for o in targets:
                created_str = o.get("created_at", "")
                try:
                    created_dt = datetime.strptime(
                        created_str, "%Y-%m-%dT%H:%M:%S+09:00"
                    ).replace(tzinfo=KST)
                    if created_dt < cutoff:
                        stale.append(o)
                except ValueError:
                    pass
            print(f"  [dry-run] 만료 예정: {len(stale)}건 (변경 없음)")
            for o in stale:
                print(f"    {o['paper_id']}  {o['symbol']}  {o['created_at']}")
        else:
            from core.toss_paper_ledger import expire_stale_previews
            result = expire_stale_previews(
                older_than_minutes=minutes,
                source_filter=args.source,
            )
            print(f"  expired: {result['expired_count']}건 / kept: {result['kept_count']}건")

        _print_separator("변경 후 ledger 상태")
        _show_ledger_state()
    else:
        print("\n→ --expire-preview-minutes N 옵션으로 stale previewed 만료 가능.")

    print("\n" + "=" * 55)
    print("완료. 삭제된 레코드 없음. 승인 자동 취소 없음.")
    print("=" * 55)


if __name__ == "__main__":
    main()
