"""tests/test_toss_live_pilot_hermes_message.py

format_hermes_live_pilot_verify_message() + build_default_hermes_verdict() 테스트.

1. format_message:
   - [HERMES_LIVE_PILOT_VERIFY] 포함
   - [/HERMES_LIVE_PILOT_VERIFY] 포함
   - verification_id / pilot_id / symbol / side / amount 포함
   - 민감정보 없음
   - 금지 CTA 없음
   - 상태: Hermes 검증 대기 포함
   - 아직 주문 전송 안 함 포함
   - 실주문: 비활성 포함

2. build_default_hermes_verdict:
   - sell → BLOCK
   - blocked symbol → BLOCK
   - amount exceed → BLOCK
   - price missing → HOLD
   - valid buy + transport not_configured → PASS with execution_blocked=true
   - unknown → HOLD
"""

import unittest
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.toss_live_pilot_hermes_bridge import (
    format_hermes_live_pilot_verify_message,
    build_default_hermes_verdict,
)

_BASE_CTX = {
    "verification_id": "hv_20260624_120000_1234",
    "pilot_id": "tlive_20260624_120000_5678",
    "preview_id": "tlive_20260624_120000_5678",
    "symbol": "091180.KS",
    "side": "buy",
    "quantity": 1,
    "limit_price": 30815.0,
    "estimated_amount_krw": 30815.0,
    "max_order_krw": 100_000,
    "max_daily_krw": 300_000,
    "side_mode": "BUY_ONLY",
    "sell_allowed": False,
    "live_order_allowed": False,
    "adapter_status": "disabled",
    "live_transport_status": "not_configured",
    "paper_evaluated_count": 0,
    "sample_status": "insufficient",
    "blocked_symbols": "005930.KS,161510.KS,MU",
    "allowed_symbols": "091180.KS,360750.KS",
    "expires_in_minutes": 10,
}


# ── 1. format_hermes_live_pilot_verify_message ────────────

class TestFormatHermesVerifyMessage(unittest.TestCase):
    def setUp(self):
        self._msg = format_hermes_live_pilot_verify_message(_BASE_CTX)

    def test_has_hermes_verify_block_open(self):
        self.assertIn("[HERMES_LIVE_PILOT_VERIFY]", self._msg)

    def test_has_hermes_verify_block_close(self):
        self.assertIn("[/HERMES_LIVE_PILOT_VERIFY]", self._msg)

    def test_has_verification_id(self):
        self.assertIn("hv_20260624_120000_1234", self._msg)

    def test_has_pilot_id(self):
        self.assertIn("tlive_20260624_120000_5678", self._msg)

    def test_has_symbol(self):
        self.assertIn("091180.KS", self._msg)

    def test_has_symbol_name_in_summary(self):
        # 사람용 요약에 종목명 병기
        self.assertIn("KODEX 자동차", self._msg)

    def test_has_symbol_display_format(self):
        # KODEX 자동차 (091180.KS) 형식
        self.assertIn("KODEX 자동차 (091180.KS)", self._msg)

    def test_symbol_name_in_machine_block(self):
        # 기계 블록에 symbol_name 필드
        self.assertIn("symbol_name: KODEX 자동차", self._msg)

    def test_symbol_field_unchanged_in_block(self):
        # 기계 블록에 원래 ticker 유지
        self.assertIn("symbol: 091180.KS", self._msg)

    def test_has_side(self):
        self.assertIn("side: buy", self._msg)

    def test_has_amount(self):
        self.assertIn("30815", self._msg)

    def test_has_status_waiting(self):
        self.assertIn("Hermes 검증 대기", self._msg)

    def test_has_no_order_yet(self):
        self.assertIn("아직 주문 전송 안 함", self._msg)

    def test_has_live_order_inactive(self):
        self.assertIn("실주문: 비활성", self._msg)

    def test_no_sensitive_info(self):
        for kw in ("accountNo", "Bearer", "APP_KEY", "APP_SECRET"):
            self.assertNotIn(kw, self._msg)

    def test_no_forbidden_cta(self):
        forbidden = [
            "자동매매 시작", "자동거래 시작", "실주문: 활성",
            "주문 실행", "매수하기", "매도하기",
        ]
        for cta in forbidden:
            self.assertNotIn(cta, self._msg, f"금지 CTA 발견: {cta!r}")

    def test_has_hermes_required(self):
        self.assertIn("hermes_required: true", self._msg)

    def test_has_expires_in_minutes(self):
        self.assertIn("expires_in_minutes: 10", self._msg)

    def test_has_side_mode(self):
        self.assertIn("side_mode: BUY_ONLY", self._msg)

    def test_has_pass_hold_block_cta(self):
        self.assertIn("PASS / HOLD / BLOCK", self._msg)

    def test_live_order_allowed_false_in_block(self):
        self.assertIn("live_order_allowed: false", self._msg)

    def test_adapter_status_disabled_in_block(self):
        self.assertIn("adapter_status: disabled", self._msg)

    def test_live_transport_not_configured_in_block(self):
        self.assertIn("live_transport_status: not_configured", self._msg)

    def test_blocked_symbols_in_block(self):
        self.assertIn("005930.KS", self._msg)

    def test_allowed_symbols_in_block(self):
        self.assertIn("360750.KS", self._msg)


class TestFormatHermesVerifyMessageMinimalCtx(unittest.TestCase):
    def test_minimal_context_no_crash(self):
        msg = format_hermes_live_pilot_verify_message({
            "verification_id": "hv_min",
            "symbol": "091180.KS",
            "side": "buy",
        })
        self.assertIn("[HERMES_LIVE_PILOT_VERIFY]", msg)

    def test_zero_price_shows_unknown(self):
        msg = format_hermes_live_pilot_verify_message({
            "symbol": "091180.KS",
            "side": "buy",
            "limit_price": 0,
        })
        self.assertIn("미확인", msg)


# ── 2. build_default_hermes_verdict ──────────────────────

class TestBuildDefaultHermesVerdictSell(unittest.TestCase):
    def test_sell_is_block(self):
        # BUY_SELL 정책: allowed_sides에 sell 명시 없으면 BLOCK, 있으면 PASS
        # allowed_sides=["buy"] → sell BLOCK
        ctx = {**_BASE_CTX, "side": "sell", "allowed_sides": ["buy"]}
        verdict = build_default_hermes_verdict(ctx)
        self.assertEqual(verdict["status"], "BLOCK")

    def test_sell_block_has_sell_reason(self):
        ctx = {**_BASE_CTX, "side": "sell", "allowed_sides": ["buy"]}
        verdict = build_default_hermes_verdict(ctx)
        self.assertTrue(any("sell" in r for r in verdict["reasons"]))

    def test_sell_checks_has_fail(self):
        ctx = {**_BASE_CTX, "side": "sell", "allowed_sides": ["buy"]}
        verdict = build_default_hermes_verdict(ctx)
        self.assertIn("FAIL", str(verdict["checks"]))


class TestBuildDefaultHermesVerdictBlockedSymbol(unittest.TestCase):
    def test_blocked_symbol_is_block(self):
        ctx = {**_BASE_CTX, "symbol": "005930.KS"}
        verdict = build_default_hermes_verdict(ctx)
        self.assertEqual(verdict["status"], "BLOCK")

    def test_blocked_symbol_reason_contains_symbol(self):
        ctx = {**_BASE_CTX, "symbol": "005930.KS"}
        verdict = build_default_hermes_verdict(ctx)
        self.assertTrue(any("005930.KS" in r for r in verdict["reasons"]))


class TestBuildDefaultHermesVerdictAmountExceed(unittest.TestCase):
    def test_amount_exceed_is_block(self):
        ctx = {**_BASE_CTX, "estimated_amount_krw": 150_000, "max_order_krw": 100_000}
        verdict = build_default_hermes_verdict(ctx)
        self.assertEqual(verdict["status"], "BLOCK")

    def test_amount_exceed_reason(self):
        ctx = {**_BASE_CTX, "estimated_amount_krw": 150_000, "max_order_krw": 100_000}
        verdict = build_default_hermes_verdict(ctx)
        self.assertTrue(any("amount" in r.lower() for r in verdict["reasons"]))


class TestBuildDefaultHermesVerdictPriceMissing(unittest.TestCase):
    def test_zero_price_is_hold(self):
        ctx = {**_BASE_CTX, "limit_price": 0}
        verdict = build_default_hermes_verdict(ctx)
        self.assertEqual(verdict["status"], "HOLD")

    def test_negative_price_is_hold(self):
        ctx = {**_BASE_CTX, "limit_price": -1}
        verdict = build_default_hermes_verdict(ctx)
        self.assertEqual(verdict["status"], "HOLD")


class TestBuildDefaultHermesVerdictValidBuy(unittest.TestCase):
    def test_valid_buy_is_pass(self):
        verdict = build_default_hermes_verdict(_BASE_CTX)
        self.assertEqual(verdict["status"], "PASS")

    def test_pass_has_execution_blocked(self):
        # BUY_SELL 정책: PASS checks에는 execution_blocked 키가 없음
        # adapter_status / live_transport_status 정보로 실행 상태 확인
        verdict = build_default_hermes_verdict(_BASE_CTX)
        self.assertNotIn("execution_blocked", verdict["checks"])

    def test_pass_checks_adapter_disabled(self):
        verdict = build_default_hermes_verdict(_BASE_CTX)
        self.assertEqual(verdict["checks"].get("adapter_status"), "disabled")

    def test_pass_checks_transport_not_configured(self):
        verdict = build_default_hermes_verdict(_BASE_CTX)
        self.assertEqual(verdict["checks"].get("live_transport_status"), "not_configured")

    def test_pass_reasons_not_empty(self):
        verdict = build_default_hermes_verdict(_BASE_CTX)
        self.assertTrue(len(verdict["reasons"]) > 0)

    def test_pass_note_in_checks(self):
        verdict = build_default_hermes_verdict(_BASE_CTX)
        note = verdict["checks"].get("note", "")
        self.assertIn("Hermes PASS", note)
        # 금지 CTA 없음
        self.assertNotIn("자동매매", note)


class TestBuildDefaultHermesVerdictEdge(unittest.TestCase):
    def test_mu_blocked_symbol(self):
        ctx = {**_BASE_CTX, "symbol": "MU", "side": "buy"}
        verdict = build_default_hermes_verdict(ctx)
        self.assertEqual(verdict["status"], "BLOCK")

    def test_allowed_symbol_360750(self):
        ctx = {**_BASE_CTX, "symbol": "360750.KS"}
        verdict = build_default_hermes_verdict(ctx)
        self.assertEqual(verdict["status"], "PASS")

    def test_no_sensitive_in_verdict(self):
        verdict = build_default_hermes_verdict(_BASE_CTX)
        verdict_str = str(verdict)
        for kw in ("accountNo", "Bearer", "APP_KEY", "APP_SECRET"):
            self.assertNotIn(kw, verdict_str)


if __name__ == "__main__":
    unittest.main()
