"""
Toss Live Pilot 미리보기 테스트 발송 — dry-run / 실제 주문 아님

- live_order_allowed=false 유지
- live_order_sent=false 유지
- adapter_status=disabled 유지
- 가격 조회 실패 시 안전 종료
- 금액 한도 초과 시 발송 안 함

사용법:
  python scripts/send_toss_live_pilot_preview_test.py          # dry-run (콘솔 출력만)
  python scripts/send_toss_live_pilot_preview_test.py --send    # Telegram 실제 발송
  python scripts/send_toss_live_pilot_preview_test.py --send --mirror-hermes  # Hermes 방도 미러링
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

# ─── 기본 후보 ────────────────────────────────────────────
_DEFAULT_SYMBOL = "091180.KS"   # TIGER 단기채권 ETF · 1주 약 ₩30,000대 (한도 내)
_DEFAULT_SIDE = "buy"
_DEFAULT_REF_PRICE = 30_000  # 이상치 탐지용 기준가 (실제 조회가 우선)


def _get_live_price(symbol: str, ref_price: float) -> float | None:
    """실시간 가격 조회. 실패 시 None."""
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        info = t.fast_info
        price = getattr(info, "last_price", None) or getattr(info, "regularMarketPrice", None)
        if price and float(price) > 0:
            return float(price)
    except Exception as e:
        print(f"  ⚠️  yfinance 조회 실패: {e}")
    return None


def _parse_symbol() -> str:
    """--symbol XXXX.KS 옵션 파싱. 없으면 기본값."""
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--symbol" and i + 1 < len(args):
            return args[i + 1]
    return _DEFAULT_SYMBOL


def main() -> None:
    send_mode = "--send" in sys.argv
    mirror_hermes = "--mirror-hermes" in sys.argv

    from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
    from core.toss_live_pilot_preview import build_live_pilot_preview
    from core.toss_live_pilot_adapter import build_toss_order_payload, dispatch_toss_order_disabled
    from core.toss_live_pilot_telegram import (
        format_live_pilot_preview_message,
        build_live_pilot_keyboard,
    )

    print("=" * 55)
    print("[TEST] Toss Live Pilot 미리보기 — 실제 주문 아님")
    print("=" * 55)

    # ── Policy ──
    policy = compute_toss_live_pilot_policy()
    max_krw = policy.get("max_order_krw", 100_000)
    print(f"\n정책: live_order_allowed={policy['live_order_allowed']} · max ₩{max_krw or 0:,} · adapter={policy['adapter_status']}")

    # ── 가격 조회 ──
    symbol = _parse_symbol()
    print(f"\n[가격 조회] {symbol}")
    price = _get_live_price(symbol, _DEFAULT_REF_PRICE)

    if price is None:
        print(f"  ⚠️  {symbol} 가격 조회 실패 — 안전 종료")
        sys.exit(0)

    print(f"  ✅ {symbol}: ₩{price:,.0f}")

    # ── 금액 한도 체크 ──
    if max_krw and price > max_krw:
        print(f"\n⚠️  1주 금액(₩{price:,.0f}) > 1회 한도(₩{max_krw:,}) — 발송 안 함")
        sys.exit(0)

    # ── preview 생성 ──
    candidate = {
        "symbol": symbol,
        "side": _DEFAULT_SIDE,
        "quantity": 1,
        "limit_price": price,
    }
    preview = build_live_pilot_preview(candidate, policy=policy)
    print(f"\n[Preview]")
    print(f"  ok={preview['ok']} · blocks={preview.get('blocks', [])} · live_order_sent={preview['live_order_sent']}")

    if not preview["ok"]:
        print(f"  ⚠️  preview 차단 — {preview.get('block_summary', '')}")

    # ── payload 생성 ──
    payload_result = build_toss_order_payload(preview, policy=policy)
    print(f"\n[Payload]")
    print(f"  ok={payload_result['ok']} · dry_run={payload_result.get('dry_run')} · live_order_sent={payload_result['live_order_sent']}")

    # ── dispatch stub ──
    dispatch_result = dispatch_toss_order_disabled(
        payload_result.get("payload", {}), policy=policy
    )
    print(f"\n[Dispatch]")
    print(f"  blocked={dispatch_result['blocked']} · reason={dispatch_result['reason']} · live_order_sent={dispatch_result['live_order_sent']}")

    # ── 메시지 구성 ──
    text = format_live_pilot_preview_message(preview, payload_result, policy)
    # preview_id: ledger 기록 없는 dry-run용 임시 ID
    from core.toss_live_pilot_ledger import _gen_pilot_id
    preview_id = _gen_pilot_id()
    keyboard = build_live_pilot_keyboard(preview_id, preview)

    print("\n" + "=" * 55)
    print(text)
    print("=" * 55)
    print("\n[Keyboard]")
    for row in keyboard:
        for btn in row:
            print(f"  [{btn['text']}] → {btn['callback_data']}")

    # ── 민감정보 체크 ──
    full_output = text + str(keyboard)
    for kw in ("accountNo", "Bearer", "APP_KEY", "APP_SECRET", "KIS_APP"):
        assert kw not in full_output, f"민감정보 감지: {kw}"
    print("\n✅ 민감정보 없음")
    print(f"✅ live_order_sent=false · adapter={dispatch_result['adapter_status']}")

    if send_mode:
        # ledger 기록 (previewed 상태)
        print("\n→ Ledger 기록 중...")
        from core.toss_live_pilot_ledger import record_live_pilot_preview
        ledger_rec = record_live_pilot_preview(preview)
        pilot_id = ledger_rec.get("pilot_id", preview_id)
        print(f"  pilot_id: {pilot_id} · status: {ledger_rec.get('status')}")

        # Hermes 검증 요청 생성 (PENDING)
        print("\n→ Hermes 검증 요청 생성 중...")
        from core.toss_live_pilot_verification import (
            create_verification_request,
            build_hermes_verification_context,
            format_hermes_verification_request,
        )
        verif_preview = {**preview, "pilot_id": pilot_id, "preview_id": pilot_id}
        verif_result = create_verification_request(verif_preview, pilot_id=pilot_id)
        verification_id = verif_result.get("verification_id", "")
        print(f"  verification_id: {verification_id} · status: {verif_result.get('status')}")

        # Hermes 검증 컨텍스트 출력 (Hermes가 읽을 블록)
        verif_ctx = build_hermes_verification_context(verif_preview, policy)
        verif_ctx["verification_id"] = verification_id
        verif_block = format_hermes_verification_request(verif_ctx)
        print(f"\n[Hermes 검증 요청 블록]")
        print(verif_block)

        # 메시지에 verification_id + Hermes 대기 상태 추가
        text_with_verif = (
            text
            + f"\n\nverification_id: {verification_id}"
            + "\nHermes 검증 상태: PENDING (검증 대기)"
            + "\n최종 승인 전 Hermes PASS 필요"
        )

        # keyboard는 실제 pilot_id로 재생성
        keyboard = build_live_pilot_keyboard(pilot_id, preview)

        print("\n→ Telegram 발송 중...")
        from core.toss_live_pilot_telegram import send_live_pilot_preview_message
        ok = send_live_pilot_preview_message(text_with_verif, keyboard)
        print(f"→ 발송 결과: {'OK' if ok else 'FAIL'}")
        print("   (버튼 눌러도 최종 승인 차단됨 — Hermes PENDING + adapter disabled)")

        # Hermes 미러링 (--mirror-hermes 옵션 + env target 설정 시)
        if mirror_hermes:
            print("\n→ Hermes 미러링 시도 중...")
            from core.toss_live_pilot_hermes_bridge import maybe_send_hermes_verification_request
            mirror_result = maybe_send_hermes_verification_request(
                preview_record=verif_preview,
                verification=verif_result,
                policy=policy,
            )
            if mirror_result.get("skipped"):
                print(f"   skipped: {mirror_result.get('reason', 'unknown')}")
                print("   (env target 미설정이면 정상 — HERMES_VERIFY_MIRROR_ENABLED / HERMES_VERIFY_CHAT_ID 설정 필요)")
            elif mirror_result.get("ok"):
                print(f"   ✅ Hermes 미러 발송 완료: verification_id={mirror_result.get('verification_id', '')}")
            else:
                print(f"   ⚠️  Hermes 미러 발송 실패: {mirror_result.get('reason', 'unknown')}")
                print("   (preview/verification은 유지됨)")
    else:
        print("\n→ dry-run 모드. ledger 미기록. Telegram 미발송.")
        print("   --send 옵션으로 실제 발송 가능.")


if __name__ == "__main__":
    main()
