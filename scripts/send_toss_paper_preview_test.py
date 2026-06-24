"""
Toss Paper 주문표 테스트 발송 — dry-run / 실제 주문 아님

- consensus_anomaly 종목은 자동 제외
- 정상 accepted price source 확인 후 후보 생성
- policy max_budget_krw 이하 수량 계산
- 미국 주식은 USD × USD/KRW로 KRW 환산 (KRW budget / USD price 직접 나누기 금지)
- live_order_allowed=false 유지

사용법:
  python scripts/send_toss_paper_preview_test.py          # dry-run (콘솔 출력만)
  python scripts/send_toss_paper_preview_test.py --send    # Telegram 실제 발송
"""

from __future__ import annotations

import math
import re
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
# _ref_price: 이상치 탐지용 기준가 (KR=KRW, US=USD).
# 실제 paper entry는 accepted market price 사용 — 즉시 win 방지.
# 순서: KR ETF(장중) → 저가 US(항상 조회 가능) → 고가 US → KR 개별주
_CANDIDATE_POOL = [
    # KR ETF (KR 장 시간대)
    {"symbol": "069500.KS",  "side": "buy", "_ref_price": 30000,  "reason": "KOSPI200 ETF · [TEST] Paper 운영 샘플"},
    {"symbol": "360750.KS",  "side": "buy", "_ref_price": 15000,  "reason": "S&P500 ETF · [TEST] Paper 운영 샘플"},
    # 저가 US (30만원 이하 1주 가능 — [TEST] 파이프라인 검증용)
    {"symbol": "SOFI",        "side": "buy", "_ref_price": 15,     "reason": "핀테크 저가 · [TEST] Paper 운영 샘플"},
    {"symbol": "PLTR",        "side": "buy", "_ref_price": 35,     "reason": "데이터분석 · [TEST] Paper 운영 샘플"},
    {"symbol": "INTC",        "side": "buy", "_ref_price": 22,     "reason": "반도체 저가 · [TEST] Paper 운영 샘플"},
    {"symbol": "F",           "side": "buy", "_ref_price": 11,     "reason": "자동차 저가 · [TEST] Paper 운영 샘플"},
    {"symbol": "AMD",         "side": "buy", "_ref_price": 110,    "reason": "반도체 · [TEST] Paper 운영 샘플"},
    # 고가 US (환율 따라 예산 초과 가능)
    {"symbol": "NVDA",        "side": "buy", "_ref_price": 200,    "reason": "반도체 · [TEST] Paper 운영 샘플"},
    {"symbol": "GOOGL",       "side": "buy", "_ref_price": 190,    "reason": "빅테크 · [TEST] Paper 운영 샘플"},
    # KR 개별주 (KR 장 시간대)
    {"symbol": "000660.KS",  "side": "buy", "_ref_price": 195000, "reason": "SK하이닉스 · [TEST] Paper 운영 샘플"},
]

_MAX_CANDIDATES = 2
_KR_SUFFIXES = (".KS", ".KQ")
_KR_CODE_RE = re.compile(r"^\d{6}$")


def _is_us_ticker(symbol: str) -> bool:
    """KR 종목이 아니면 US로 간주 (USDKRW=X, 지수 제외)."""
    if any(symbol.endswith(s) for s in _KR_SUFFIXES):
        return False
    if _KR_CODE_RE.match(symbol):
        return False
    if symbol.startswith("^"):
        return False
    return True


def _get_usdkrw(ctx: dict) -> float:
    """USD/KRW 환율 조회. ctx → yfinance → 1,350 fallback."""
    rate = ctx.get("usdkrw")
    if rate and float(rate) > 0:
        return float(rate)
    try:
        import yfinance as yf
        info = yf.Ticker("USDKRW=X").fast_info
        p = getattr(info, "last_price", None) or getattr(info, "regularMarketPrice", None)
        if p and float(p) > 0:
            return float(p)
    except Exception:
        pass
    print("  ⚠️  USD/KRW 환율 조회 실패 — 1,350원 fallback 사용")
    return 1_350.0


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


def _build_candidates(
    policy: dict,
    ctx: dict | None = None,
    max_n: int = _MAX_CANDIDATES,
) -> tuple[list[dict], list[dict]]:
    """정상 가격 검증된 후보 리스트 생성. consensus_anomaly 제외.

    미국 주식: limit_price(USD) × usdkrw → KRW 환산 후 수량 계산.
    KR 주식: limit_price(KRW) 그대로 사용.
    """
    if ctx is None:
        ctx = {}
    consensus_symbols = set(policy.get("consensus_anomaly_symbols", []))
    max_budget = policy.get("max_budget_krw", 300_000)
    usdkrw = _get_usdkrw(ctx)

    candidates: list[dict] = []
    rejected: list[dict] = []

    for pool_entry in _CANDIDATE_POOL:
        if len(candidates) >= max_n:
            break

        symbol = pool_entry["symbol"]
        ref_price = float(pool_entry["_ref_price"])  # 이상치 탐지 기준가
        reason = pool_entry["reason"]
        side = pool_entry["side"]

        # ref_price는 이상치 탐지용. accepted market price를 실제 entry로 사용.
        val = _validate_candidate(symbol, ref_price, consensus_symbols)
        if not val["ok"]:
            rejected.append({"symbol": symbol, "reject_reason": val["reason"]})
            print(f"  ❌ {symbol}: {val['reason']}")
            continue

        # accepted market price → paper entry (즉시 win 방지)
        entry_price = val["price"]

        # 통화 변환: 미국 주식은 USD × usdkrw
        is_us = _is_us_ticker(symbol)
        if is_us:
            price_krw_per_share = entry_price * usdkrw
        else:
            price_krw_per_share = entry_price

        # 1주 금액이 최대 예산 초과 → 제외
        if price_krw_per_share > max_budget:
            reject_reason = (
                f"budget_too_small_for_one_share: "
                f"1주 예상금액 ₩{price_krw_per_share:,.0f} > 최대 ₩{max_budget:,}"
            )
            rejected.append({"symbol": symbol, "reject_reason": reject_reason})
            print(
                f"  ❌ {symbol}: 예산 부족 "
                f"(1주 ₩{price_krw_per_share:,.0f} > ₩{max_budget:,})"
            )
            continue

        # 수량 계산 (policy max budget 이하)
        quantity = max(1, math.floor(max_budget / price_krw_per_share))
        estimated = round(price_krw_per_share * quantity, 2)

        # max_budget 초과 방지
        while estimated > max_budget and quantity > 0:
            quantity -= 1
            estimated = round(price_krw_per_share * quantity, 2)

        if quantity <= 0:
            rejected.append({
                "symbol": symbol,
                "reject_reason": f"시장가 환산금액(₩{price_krw_per_share:,.0f})이 max_budget({max_budget:,}) 초과",
            })
            print(f"  ❌ {symbol}: 가격이 예산 초과")
            continue

        if is_us:
            print(
                f"  ✅ {symbol}: price=${entry_price:,.2f} via {val['source']} "
                f"· qty={quantity} · ₩{estimated:,.0f} (환율 {usdkrw:,.0f})"
            )
        else:
            print(
                f"  ✅ {symbol}: price={entry_price:,.0f} via {val['source']} "
                f"· qty={quantity} · ₩{estimated:,.0f}"
            )

        candidates.append({
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "limit_price": entry_price,  # accepted market price — not hardcoded ref
            "estimated_amount_krw": estimated,
            "confidence": 0.0,   # [TEST] — 실제 신뢰도 아님
            "reason": reason,
            "quote_age_sec": 0,
            "_validated_price": entry_price,
            "_accepted_source": val["source"],
            "_is_test_sample": True,
            "_price_currency": "USD" if is_us else "KRW",
            "_usdkrw": usdkrw if is_us else None,
            "_limit_price_usd": entry_price if is_us else None,
        })

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
    usdkrw = ctx.get("usdkrw", 0)
    if usdkrw:
        print(f"  USD/KRW: {usdkrw:,.0f}")

    # ── 후보 생성 ──
    print("\n[후보 가격 검증]")
    candidates, rejected = _build_candidates(policy, ctx)

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

    # ── preview ID 생성 (ledger write는 --send 시에만) ──
    preview_id = generate_preview_id()
    print(f"\n[Preview ID] {preview_id}  (dry-run: ledger write 없음)")

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
        # --send 시에만 ledger에 기록 + Telegram 발송
        print("\n→ Ledger 기록 중...")
        records = create_paper_preview_records(preview_id, candidates, cross_checks, ctx)
        for r in records:
            print(f"  {r['symbol']}: {r['status']}")

        print("\n→ Telegram 발송 중...")
        from core.toss_paper_telegram_send import send_toss_paper_preview_message
        ok = send_toss_paper_preview_message(text, keyboard)
        print(f"→ 발송 결과: {'OK' if ok else 'FAIL'}")
    else:
        print("\n→ dry-run 모드. ledger 미기록. --send 옵션으로 실제 발송 가능.")


if __name__ == "__main__":
    main()
