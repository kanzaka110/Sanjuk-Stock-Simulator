"""
Toss Paper 주문표 테스트 발송 — dry-run / 실제 주문 아님

사용법:
  python scripts/send_toss_paper_preview_test.py          # dry-run (콘솔 출력만)
  python scripts/send_toss_paper_preview_test.py --send    # Telegram 실제 발송
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


def main() -> None:
    send_mode = "--send" in sys.argv

    from core.toss_decision_context import get_toss_decision_context
    from core.toss_cross_check import cross_check_candidate
    from core.toss_order_preview import build_toss_paper_order_preview
    from core.toss_paper_telegram import build_paper_preview_keyboard
    from core.toss_paper_ledger import create_paper_preview_records
    from core.toss_order_preview import generate_preview_id

    # ── Toss 컨텍스트 ──
    ctx = get_toss_decision_context()
    print(f"Toss enabled: {ctx.get('enabled')}")
    print(f"Cash KRW: {ctx.get('cash_krw', 0):,.0f}")

    # ── 샘플 후보 ──
    candidates = [
        {
            "symbol": "005930.KS", "side": "buy", "quantity": 2,
            "limit_price": 72000, "estimated_amount_krw": 144000,
            "confidence": 0.82, "reason": "기술적 지지선 반등",
            "quote_age_sec": 10,
        },
        {
            "symbol": "MU", "side": "buy", "quantity": 5,
            "limit_price": 28000, "estimated_amount_krw": 140000,
            "confidence": 0.75, "reason": "HBM 수요 증가",
            "quote_age_sec": 10,
        },
    ]

    # ── 교차 검증 ──
    cross_checks = [
        cross_check_candidate(c["symbol"], c["side"], c["estimated_amount_krw"], ctx)
        for c in candidates
    ]

    # ── preview 생성 ──
    preview_id = generate_preview_id()
    records = create_paper_preview_records(preview_id, candidates, cross_checks, ctx)
    print(f"\nPreview ID: {preview_id}")
    print(f"Records: {len(records)}")
    for r in records:
        print(f"  {r['symbol']}: {r['status']}")

    # ── 메시지 + 키보드 ──
    header = "[TEST] Toss Paper 주문표 dry-run\n실주문: 비활성\n\n"
    text = header + build_toss_paper_order_preview(candidates, ctx, cross_checks)
    keyboard = build_paper_preview_keyboard(preview_id, candidates, cross_checks)

    print("\n" + "=" * 50)
    print(text)
    print("=" * 50)
    print("\nKeyboard:")
    for row in keyboard:
        for btn in row:
            print(f"  [{btn['text']}] → {btn['callback_data']}")

    if send_mode:
        print("\n→ Telegram 발송 중...")
        from core.toss_paper_telegram_send import send_toss_paper_preview_message
        ok = send_toss_paper_preview_message(text, keyboard)
        print(f"→ 발송 결과: {'OK' if ok else 'FAIL'}")
    else:
        print("\n→ dry-run 모드. --send 옵션으로 실제 발송 가능.")


if __name__ == "__main__":
    main()
