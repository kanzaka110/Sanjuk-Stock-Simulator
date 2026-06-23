"""
Toss Paper 주문표 테스트 발송 — dry-run / 실제 주문 아님

- consensus_anomaly 종목은 자동 제외
- 정상 accepted price source 확인 후 후보 생성
- policy max_budget_krw 이하 수량 계산
- live_order_allowed=false 유지

사용법:
  python scripts/send_toss_paper_preview_test.py          # dry-run (콘솔 출력만)
  python scripts/send_toss_paper_preview_test.py --send    # Telegram 실제 발송
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# ─── 정상 후보 pool (005930.KS 제외) ──────────────────────
# 가격 source 검증 후 통과한 것만 사용
_CANDIDATE_POOL = [
    {"symbol": "069500.KS",  "side": "buy", "limit_price": 30000, "reason": "KOSPI200 ETF · [TEST] Paper 운영 샘플"},
    {"symbol": "360750.KS",  "side": "buy", "limit_price": 15000, "reason": "S&P500 ETF · [TEST] Paper 운영 샘플"},
    {"symbol": "NVDA",        "side": "buy", "limit_price": 135,   "reason": "반도체 · [TEST] Paper 운영 샘플"},
    {"symbol": "GOOGL",       "side": "buy", "limit_price": 190,   "reason": "빅테크 · [TEST] Paper 운영 샘플"},
    {"symbol": "000660.KS",  "side": "buy", "limit_price": 195000, "reason": "SK하이닉스 · [TEST] Paper 운영 샘플"},
]

_MAX_CANDIDATES = 2


def _validate_candidate(symbol: str, limit_price: float, consensus_symbols: set[str]) -> dict:
    """후보 가격 검증 — 정상 source accepted 여부 확인.

    Returns:
        {"ok": bool, "price": float|None, "source": str, "reason": str}
    """
    if symbol in consensus_symbols:
        return {"ok": False, "price": None, "source": "", "reason": "consensus_anomaly — 제외"}

    try:
        from core.toss_paper_performance import _get_quote_for_paper
        q = _get_quote_for_paper(symbol, entry_price=limit_price)
        if q["accepted_price_source"] is None:
            return {"ok": False, "price": None, "source": "", "reason": "accepted source 없음"}
        price = q["price"]
        if price is None or price <= 0:
            return {"ok": False, "price": None, "source": "", "reason": "가격 없음"}
        return {"ok": True, "price": price, "source": q["accepted_price_source"], "reason": "정상"}
    except Exception as exc:
        return {"ok": False, "price": None, "source": "", "reason": f"조회 오류: {exc}"}


def _build_candidates(policy: dict, max_n: int = _MAX_CANDIDATES) -> list[dict]:
    """정상 가격 검증된 후보 리스트 생성. consensus_anomaly 제외."""
    consensus_symbols = set(policy.get("consensus_anomaly_symbols", []))
    max_budget = policy.get("max_budget_krw", 300_000)

    candidates: list[dict] = []
    rejected: list[dict] = []

    for pool_entry in _CANDIDATE_POOL:
        if len(candidates) >= max_n:
            break

        symbol = pool_entry["symbol"]
        limit_price = float(pool_entry["limit_price"])
        reason = pool_entry["reason"]
        side = pool_entry["side"]

        val = _validate_candidate(symbol, limit_price, consensus_symbols)

        if not val["ok"]:
            rejected.append({"symbol": symbol, "reject_reason": val["reason"]})
            print(f"  ❌ {symbol}: {val['reason']}")
            continue

        # 수량 계산 (policy max budget 이하)
        quantity = max(1, math.floor(max_budget / limit_price))
        estimated = round(limit_price * quantity, 2)

        # max_budget 초과 방지
        while estimated > max_budget and quantity > 0:
            quantity -= 1
            estimated = round(limit_price * quantity, 2)

        if quantity <= 0:
            rejected.append({"symbol": symbol, "reject_reason": f"지정가({limit_price:,.0f})가 max_budget({max_budget:,}) 초과"})
            print(f"  ❌ {symbol}: 가격이 예산 초과")
            continue

        candidates.append({
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "limit_price": limit_price,
            "estimated_amount_krw": estimated,
            "confidence": 0.0,   # [TEST] — 실제 신뢰도 아님
            "reason": reason,
            "quote_age_sec": 0,
            "_validated_price": val["price"],
            "_accepted_source": val["source"],
            "_is_test_sample": True,
        })
        print(f"  ✅ {symbol}: price={val['price']:,.2f} via {val['source']} · qty={quantity} · ₩{estimated:,.0f}")

    return candidates, rejected


def main() -> None:
    send_mode = "--send" in sys.argv

    from core.toss_decision_context import get_toss_decision_context
    from core.toss_cross_check import cross_check_candidate
    from core.toss_order_preview import build_toss_paper_order_preview, generate_preview_id
    from core.toss_paper_telegram import build_paper_preview_keyboard
    from core.toss_paper_ledger import create_paper_preview_records
    from core.toss_paper_policy import compute_toss_paper_policy

    print("=" * 55)
    print("[TEST] Toss Paper 운영 샘플 — 실제 주문 아님")
    print("=" * 55)

    # ── Policy 조회 ──
    policy = compute_toss_paper_policy()
    sample_status = policy.get("sample_status", "insufficient")
    max_budget = policy.get("max_budget_krw", 300_000)
    consensus_symbols = policy.get("consensus_anomaly_symbols", [])
    print(f"\n정책: {sample_status} · max ₩{max_budget:,} · live=False")
    if consensus_symbols:
        print(f"  ⚠️  consensus_anomaly 제외: {consensus_symbols}")

    # ── Toss 컨텍스트 ──
    ctx = get_toss_decision_context()
    print(f"  Toss 현금: ₩{ctx.get('cash_krw', 0):,.0f}")

    # ── 후보 생성 ──
    print("\n[후보 가격 검증]")
    candidates, rejected = _build_candidates(policy)

    if not candidates:
        print("\n⚠️  정상 후보 없음 — 발송 중단")
        if rejected:
            print("제외된 후보:")
            for r in rejected:
                print(f"  - {r['symbol']}: {r['reject_reason']}")
        return

    # ── 교차 검증 ──
    cross_checks = [
        cross_check_candidate(c["symbol"], c["side"], c["estimated_amount_krw"], ctx)
        for c in candidates
    ]

    # ── preview 생성 ──
    preview_id = generate_preview_id()
    records = create_paper_preview_records(preview_id, candidates, cross_checks, ctx)
    print(f"\n[Preview ID] {preview_id}")
    for r in records:
        print(f"  {r['symbol']}: {r['status']}")

    # ── 메시지 구성 ──
    header = (
        "[TEST] Toss Paper 운영 샘플\n"
        "실제 주문 아님\n"
        "실주문: 비활성\n"
        f"표본부족 — 최대 ₩{max_budget:,} paper 검증\n\n"
    )
    text = header + build_toss_paper_order_preview(candidates, ctx, cross_checks)
    keyboard = build_paper_preview_keyboard(preview_id, candidates, cross_checks)

    print("\n" + "=" * 55)
    print(text)
    print("=" * 55)
    print("\n[Keyboard]")
    for row in keyboard:
        for btn in row:
            print(f"  [{btn['text']}] → {btn['callback_data']}")

    if rejected:
        print(f"\n[제외된 후보] {len(rejected)}건")
        for r in rejected:
            print(f"  - {r['symbol']}: {r['reject_reason']}")

    if send_mode:
        print("\n→ Telegram 발송 중...")
        from core.toss_paper_telegram_send import send_toss_paper_preview_message
        ok = send_toss_paper_preview_message(text, keyboard)
        print(f"→ 발송 결과: {'OK' if ok else 'FAIL'}")
    else:
        print("\n→ dry-run 모드. --send 옵션으로 실제 발송 가능.")


if __name__ == "__main__":
    main()
