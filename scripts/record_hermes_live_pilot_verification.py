"""
Hermes 교차검증 결과 기록 CLI

사용법:
  # 검증 요청 생성 (PENDING)
  python scripts/record_hermes_live_pilot_verification.py \\
      --pilot-id tlive_20250624_123456_1234 \\
      --symbol 091180.KS --side buy --quantity 1 --price 30000 \\
      --create-request

  # 검증 결과 기록
  python scripts/record_hermes_live_pilot_verification.py \\
      --verification-id hv_20250624_123456_1234 \\
      --status PASS \\
      --reason "price_ok" --reason "amount_ok" \\
      --check amount_guard=ok --check price_nonzero=ok \\
      --ttl-minutes 10

  # 드라이런 (기록 없음, 출력만)
  python scripts/record_hermes_live_pilot_verification.py \\
      --verification-id hv_... --status PASS --dry-run

금지:
  - accountNo/token/key/secret 출력 금지
  - live_order_allowed=True 반환 금지
  - 자동매매 실행 금지
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
    """간단한 CLI 인수 파싱."""
    args = sys.argv[1:]

    def _flag(name: str) -> bool:
        return name in args

    def _val(name: str, default: str = "") -> str:
        for i, a in enumerate(args):
            if a == name and i + 1 < len(args):
                return args[i + 1]
        return default

    def _multi(name: str) -> list[str]:
        result = []
        for i, a in enumerate(args):
            if a == name and i + 1 < len(args):
                result.append(args[i + 1])
        return result

    return {
        "create_request": _flag("--create-request"),
        "pilot_id": _val("--pilot-id"),
        "symbol": _val("--symbol", "091180.KS"),
        "side": _val("--side", "buy"),
        "quantity": int(_val("--quantity", "1")),
        "price": float(_val("--price", "0")),
        "verification_id": _val("--verification-id"),
        "status": _val("--status", ""),
        "reasons": _multi("--reason"),
        "checks": _multi("--check"),
        "hermes_message": _val("--message", ""),
        "ttl_minutes": int(_val("--ttl-minutes", "10")),
        "dry_run": _flag("--dry-run"),
    }


def _parse_checks(check_list: list[str]) -> dict:
    """'key=value' 목록 → dict."""
    result = {}
    for c in check_list:
        if "=" in c:
            k, v = c.split("=", 1)
            result[k.strip()] = v.strip()
        else:
            result[c.strip()] = "ok"
    return result


def main() -> None:
    opts = _parse_args()

    print("=" * 55)
    print("[Hermes 교차검증 기록] — 실제 주문 아님")
    print("=" * 55)

    from core.toss_live_pilot_verification import (
        create_verification_request,
        record_hermes_verification,
        get_verification_for_pilot,
        format_hermes_verification_request,
        build_hermes_verification_context,
    )
    from core.toss_live_pilot_policy import compute_toss_live_pilot_policy

    if opts["create_request"]:
        # ── 검증 요청 생성 ──
        pilot_id = opts["pilot_id"]
        if not pilot_id:
            print("⚠️  --pilot-id 필수 (create-request 모드)")
            sys.exit(1)

        preview_record = {
            "symbol": opts["symbol"],
            "side": opts["side"],
            "quantity": opts["quantity"],
            "limit_price": opts["price"],
            "estimated_amount_krw": opts["price"] * opts["quantity"],
            "pilot_id": pilot_id,
        }
        policy = compute_toss_live_pilot_policy()

        ctx = build_hermes_verification_context(preview_record, policy)
        print(f"\n[검증 컨텍스트]")
        print(f"  symbol={ctx['symbol']} side={ctx['side']} qty={ctx['quantity']} price={ctx['limit_price']}")
        print(f"  adapter_status={ctx['adapter_status']} live_order_allowed=false")
        verif_block = format_hermes_verification_request(ctx)
        print(f"\n{verif_block}\n")

        if opts["dry_run"]:
            print("→ dry-run 모드. DB 기록 없음.")
            return

        result = create_verification_request(preview_record, pilot_id=pilot_id)
        print(f"\n✅ 검증 요청 생성")
        print(f"  verification_id: {result['verification_id']}")
        print(f"  pilot_id: {result['pilot_id']}")
        print(f"  status: {result['status']}")
        print(f"  requested_at: {result['requested_at']}")

    else:
        # ── 검증 결과 기록 ──
        verification_id = opts["verification_id"]
        status = opts["status"].upper() if opts["status"] else ""

        if not verification_id:
            print("⚠️  --verification-id 필수")
            sys.exit(1)
        if not status:
            print("⚠️  --status 필수 (PASS | HOLD | BLOCK | ERROR)")
            sys.exit(1)
        if status not in ("PENDING", "PASS", "HOLD", "BLOCK", "ERROR"):
            print(f"⚠️  유효하지 않은 status: {status}")
            sys.exit(1)

        reasons = opts["reasons"]
        checks = _parse_checks(opts["checks"])
        hermes_message = opts["hermes_message"]
        ttl = opts["ttl_minutes"]

        print(f"\n[검증 결과]")
        print(f"  verification_id: {verification_id}")
        print(f"  status: {status}")
        print(f"  reasons: {reasons}")
        print(f"  checks: {checks}")
        if status == "PASS":
            print(f"  ttl_minutes: {ttl}")

        # 민감정보 체크
        for kw in ("accountNo", "Bearer", "APP_KEY", "APP_SECRET"):
            if any(kw in str(v) for v in [*reasons, *checks.values(), hermes_message]):
                print(f"⚠️  민감정보 감지: {kw} — 기록 중단")
                sys.exit(1)

        if opts["dry_run"]:
            print("\n→ dry-run 모드. DB 기록 없음.")
            print("✅ 민감정보 없음. 형식 검증 통과.")
            return

        result = record_hermes_verification(
            verification_id=verification_id,
            status=status,
            reasons=reasons,
            checks=checks,
            hermes_message=hermes_message,
            ttl_minutes=ttl,
        )

        if result.get("ok"):
            print(f"\n✅ 검증 결과 기록 완료")
            print(f"  status: {result['status']}")
            print(f"  verified_at: {result['verified_at']}")
            if result.get("expires_at"):
                print(f"  expires_at: {result['expires_at']} (TTL {ttl}분)")
            print(f"  live_order_allowed: false (항상)")
        else:
            print(f"\n⚠️  기록 실패: {result.get('reason', 'unknown')}")
            sys.exit(1)


if __name__ == "__main__":
    main()
