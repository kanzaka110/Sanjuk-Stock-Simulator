"""
Toss live pilot verification PENDING 위생 정리 스크립트.

오래된 PENDING을 EXPIRED로 전환한다 (삭제 없음).

사용법:
  # 기본 dry-run (DB 변경 없음, 예측 수치만 출력)
  python scripts/cleanup_toss_live_pilot_verifications.py --dry-run

  # 15분 이상 된 PENDING을 EXPIRED로 전환
  python scripts/cleanup_toss_live_pilot_verifications.py --expire-pending-minutes 15

  # 60분 이상 된 PENDING을 EXPIRED로 전환
  python scripts/cleanup_toss_live_pilot_verifications.py --expire-pending-minutes 60

금지:
  - row 삭제 없음 (DELETE/DROP 없음)
  - PASS/HOLD/BLOCK/ERROR/EXPIRED는 건드리지 않음
  - live_order_allowed 변경 없음
  - 주문 API 호출 없음
  - 민감정보 출력 없음
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def _parse_args() -> dict:
    args = sys.argv[1:]

    def _flag(name: str) -> bool:
        return name in args

    def _val(name: str, default: str = "") -> str:
        for i, a in enumerate(args):
            if a == name and i + 1 < len(args):
                return args[i + 1]
        return default

    dry_run = _flag("--dry-run")
    expire_minutes_str = _val("--expire-pending-minutes", "")

    # 기본은 dry-run (명시적 --expire-pending-minutes 없으면)
    if not expire_minutes_str:
        dry_run = True
        expire_minutes = 15
    else:
        try:
            expire_minutes = int(expire_minutes_str)
        except ValueError:
            print(f"⚠️  --expire-pending-minutes 값이 정수가 아님: {expire_minutes_str!r}")
            sys.exit(1)

    return {
        "dry_run": dry_run,
        "expire_minutes": expire_minutes,
    }


def main() -> None:
    opts = _parse_args()
    dry_run = opts["dry_run"]
    expire_minutes = opts["expire_minutes"]

    print("=" * 55)
    print("Toss live pilot verification cleanup")
    print("=" * 55)
    print(f"dry_run: {dry_run}")
    print(f"expire_pending_minutes: {expire_minutes}")
    print()

    from core.toss_live_pilot_verification import (
        expire_pending_verifications,
        verification_summary,
        PENDING_EXPIRE_MINUTES,
    )

    # 현재 상태 조회
    before = verification_summary()
    before_summary = before.get("summary", {})
    pending_total = before_summary.get("PENDING", 0)
    expired_existing = before_summary.get("EXPIRED", 0)
    oldest_age = before.get("oldest_pending_age_minutes")

    print("[현재 상태]")
    for status in ("PENDING", "PASS", "HOLD", "BLOCK", "ERROR", "EXPIRED", "STALE"):
        cnt = before_summary.get(status, 0)
        if cnt > 0 or status in ("PENDING", "EXPIRED"):
            print(f"  {status}: {cnt}")
    if oldest_age is not None:
        print(f"  oldest_pending_age: {oldest_age}분")
    print(f"  pending_expire_minutes (기본): {PENDING_EXPIRE_MINUTES}분")
    print()

    # 만료 처리
    result = expire_pending_verifications(
        older_than_minutes=expire_minutes,
        dry_run=dry_run,
    )

    print("[처리 결과]")
    print(f"  expired_count: {result['expired_count']}")
    print(f"  kept_count: {result['kept_count']}")
    print(f"  dry_run: {result['dry_run']}")
    print(f"  live_order_sent: {result['live_order_sent']}")

    if not dry_run and result["expired_count"] > 0:
        # 처리 후 상태 조회
        after = verification_summary()
        after_summary = after.get("summary", {})
        print()
        print("[처리 후 상태]")
        for status in ("PENDING", "PASS", "HOLD", "BLOCK", "ERROR", "EXPIRED", "STALE"):
            cnt = after_summary.get(status, 0)
            if cnt > 0 or status in ("PENDING", "EXPIRED"):
                print(f"  {status}: {cnt}")

    print()
    if dry_run:
        print("→ dry-run 모드. DB 변경 없음.")
        print(f"   --expire-pending-minutes {expire_minutes} 옵션으로 실제 만료 처리 가능.")
    else:
        print(f"✅ 완료: {result['expired_count']}건 PENDING → EXPIRED")

    print(f"✅ deleted: 0 (삭제 없음)")
    print(f"✅ live_order_sent: 0")
    print(f"✅ adapter_status: disabled (기본)")


if __name__ == "__main__":
    main()
