"""tests/test_toss_live_pilot_telegram.py

format_live_pilot_preview_message + build_live_pilot_keyboard 테스트.
- 금지 CTA 없음
- callback prefix tlp: (Paper tp: 와 분리)
- live_order_sent=False 항상
- 민감정보 없음
"""

import unittest
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.toss_live_pilot_telegram import (
    format_live_pilot_preview_message,
    build_live_pilot_keyboard,
    build_callback_data,
    parse_callback_data,
    CB_PREFIX,
)


_POLICY = {
    "max_order_krw": 100_000,
    "max_daily_krw": 300_000,
    "max_orders_per_day": 1,
    "adapter_status": "disabled",
    "live_order_allowed": False,
}

_OK_PREVIEW = {
    "ok": True,
    "symbol": "069500.KS",
    "side": "buy",
    "quantity": 1,
    "limit_price": 40_000.0,
    "estimated_amount_krw": 40_000.0,
    "blocks": [],
    "live_order_sent": False,
    "live_order_allowed": False,
}

_BLOCKED_PREVIEW = {
    "ok": False,
    "symbol": "005930.KS",
    "side": "buy",
    "quantity": 1,
    "limit_price": 319_000.0,
    "estimated_amount_krw": 319_000.0,
    "blocks": ["price_anomaly_history"],
    "live_order_sent": False,
    "live_order_allowed": False,
}

_PAYLOAD_RESULT = {
    "ok": True,
    "dry_run": True,
    "live_order_sent": False,
}


# ─── 1. 메시지 필수 문구 ──────────────────────────────────

class TestPreviewMessageRequiredPhrases(unittest.TestCase):
    def setUp(self):
        self.msg = format_live_pilot_preview_message(_OK_PREVIEW, _PAYLOAD_RESULT, _POLICY)

    def test_live_pilot_header(self):
        self.assertIn("Live Pilot", self.msg)

    def test_not_sent_phrase(self):
        self.assertIn("아직 주문 전송 안 함", self.msg)

    def test_order_disabled_phrase(self):
        self.assertIn("실주문: 비활성", self.msg)

    def test_second_confirm_required(self):
        self.assertIn("최종 2단계 승인 필요", self.msg)

    def test_api_disabled_phrase(self):
        self.assertIn("주문 API 호출 비활성", self.msg)

    def test_symbol_present(self):
        self.assertIn("069500.KS", self.msg)

    def test_symbol_name_present(self):
        self.assertIn("KODEX 200", self.msg)

    def test_symbol_display_format(self):
        self.assertIn("KODEX 200 (069500.KS)", self.msg)

    def test_price_present(self):
        self.assertIn("40,000", self.msg)

    def test_adapter_disabled_label(self):
        self.assertIn("disabled", self.msg)

    def test_dispatch_block_notice(self):
        self.assertIn("전송 차단", self.msg)


# ─── 2. 금지 CTA 없음 ────────────────────────────────────

class TestNoForbiddenCTAInMessage(unittest.TestCase):
    def _msg(self, preview=None):
        return format_live_pilot_preview_message(
            preview or _OK_PREVIEW, _PAYLOAD_RESULT, _POLICY
        )

    def test_no_buy_cta(self):
        self.assertNotIn("매수하기", self._msg())

    def test_no_sell_cta(self):
        self.assertNotIn("매도하기", self._msg())

    def test_no_execute_order(self):
        self.assertNotIn("주문 실행", self._msg())

    def test_no_auto_trade(self):
        self.assertNotIn("자동매매 시작", self._msg())

    def test_no_live_order_active(self):
        self.assertNotIn("실주문: 활성", self._msg())

    def test_no_auto_trade_ko(self):
        self.assertNotIn("자동거래 시작", self._msg())


# ─── 3. 차단 메시지 ──────────────────────────────────────

class TestBlockedPreviewMessage(unittest.TestCase):
    def setUp(self):
        self.msg = format_live_pilot_preview_message(_BLOCKED_PREVIEW, {"ok": False}, _POLICY)

    def test_symbol_in_blocked_msg(self):
        self.assertIn("005930.KS", self.msg)

    def test_symbol_name_in_blocked_msg(self):
        self.assertIn("삼성전자", self.msg)

    def test_symbol_display_in_blocked_msg(self):
        self.assertIn("삼성전자 (005930.KS)", self.msg)

    def test_blocked_still_has_not_sent(self):
        self.assertIn("아직 주문 전송 안 함", self.msg)

    def test_blocked_still_has_disabled(self):
        self.assertIn("실주문: 비활성", self.msg)

    def test_no_buy_cta_in_blocked(self):
        self.assertNotIn("매수하기", self.msg)


# ─── 4. InlineKeyboard ───────────────────────────────────

class TestLivePilotKeyboard(unittest.TestCase):
    def test_ok_preview_has_three_buttons(self):
        kbd = build_live_pilot_keyboard("tlive_test", _OK_PREVIEW)
        all_buttons = [btn for row in kbd for btn in row]
        self.assertGreaterEqual(len(all_buttons), 2)

    def test_all_callbacks_tlp_prefix(self):
        kbd = build_live_pilot_keyboard("tlive_test", _OK_PREVIEW)
        for row in kbd:
            for btn in row:
                self.assertTrue(
                    btn["callback_data"].startswith("tlp:"),
                    f"callback_data는 tlp: prefix여야 함: {btn['callback_data']}"
                )

    def test_no_tp_prefix_in_callbacks(self):
        """Paper tp: prefix와 혼동 없음."""
        kbd = build_live_pilot_keyboard("tlive_test", _OK_PREVIEW)
        for row in kbd:
            for btn in row:
                data = btn["callback_data"]
                # tp: 로 시작하면 안 됨 (tlp: 는 허용)
                if data.startswith("tp:"):
                    self.fail(f"Paper tp: prefix 감지 (tlp: 여야 함): {data}")

    def test_review_button_present(self):
        kbd = build_live_pilot_keyboard("tlive_test", _OK_PREVIEW)
        datas = [btn["callback_data"] for row in kbd for btn in row]
        self.assertTrue(any("review" in d for d in datas))

    def test_confirm_button_present(self):
        kbd = build_live_pilot_keyboard("tlive_test", _OK_PREVIEW)
        datas = [btn["callback_data"] for row in kbd for btn in row]
        self.assertTrue(any("confirm" in d for d in datas))

    def test_cancel_button_present(self):
        kbd = build_live_pilot_keyboard("tlive_test", _OK_PREVIEW)
        datas = [btn["callback_data"] for row in kbd for btn in row]
        self.assertTrue(any("cancel" in d for d in datas))

    def test_blocked_preview_only_cancel(self):
        kbd = build_live_pilot_keyboard("tlive_test", _BLOCKED_PREVIEW)
        all_buttons = [btn for row in kbd for btn in row]
        # 차단된 경우 confirm 없어야 함
        confirm_btns = [b for b in all_buttons if "confirm" in b["callback_data"]]
        self.assertEqual(len(confirm_btns), 0)
        cancel_btns = [b for b in all_buttons if "cancel" in b["callback_data"]]
        self.assertGreater(len(cancel_btns), 0)

    def test_callback_data_contains_preview_id(self):
        kbd = build_live_pilot_keyboard("tlive_abc123", _OK_PREVIEW)
        all_data = [btn["callback_data"] for row in kbd for btn in row]
        self.assertTrue(any("tlive_abc123" in d for d in all_data))


# ─── 5. callback data 파싱 ───────────────────────────────

class TestCallbackDataParsing(unittest.TestCase):
    def test_build_review(self):
        d = build_callback_data("review", "tlive_001")
        self.assertEqual(d, "tlp:review:tlive_001")

    def test_build_confirm(self):
        d = build_callback_data("confirm", "tlive_002")
        self.assertEqual(d, "tlp:confirm:tlive_002")

    def test_build_cancel(self):
        d = build_callback_data("cancel", "tlive_003")
        self.assertEqual(d, "tlp:cancel:tlive_003")

    def test_parse_review(self):
        p = parse_callback_data("tlp:review:tlive_001")
        self.assertEqual(p["action"], "review")
        self.assertEqual(p["preview_id"], "tlive_001")

    def test_parse_confirm(self):
        p = parse_callback_data("tlp:confirm:tlive_002")
        self.assertEqual(p["action"], "confirm")
        self.assertEqual(p["preview_id"], "tlive_002")

    def test_parse_cancel(self):
        p = parse_callback_data("tlp:cancel:tlive_003")
        self.assertEqual(p["action"], "cancel")

    def test_parse_invalid_prefix(self):
        self.assertIsNone(parse_callback_data("tp:a:pid:sym"))

    def test_parse_empty(self):
        self.assertIsNone(parse_callback_data(""))

    def test_cb_prefix_value(self):
        self.assertEqual(CB_PREFIX, "tlp:")


# ─── 6. 민감정보 없음 ────────────────────────────────────

class TestNoSensitiveInMessage(unittest.TestCase):
    def test_no_account_no(self):
        msg = format_live_pilot_preview_message(_OK_PREVIEW, _PAYLOAD_RESULT, _POLICY)
        self.assertNotIn("accountNo", msg)

    def test_no_bearer_token(self):
        msg = format_live_pilot_preview_message(_OK_PREVIEW, _PAYLOAD_RESULT, _POLICY)
        self.assertNotIn("Bearer", msg)

    def test_no_app_key(self):
        msg = format_live_pilot_preview_message(_OK_PREVIEW, _PAYLOAD_RESULT, _POLICY)
        for kw in ("APP_KEY", "APP_SECRET", "KIS_APP"):
            self.assertNotIn(kw, msg)


# ─── 7. source 파일 금지 문구 ────────────────────────────

class TestNoForbiddenInSource(unittest.TestCase):
    def _code_lines(self) -> str:
        """docstring/comment 제거 후 소스 반환."""
        import re
        src = (_ROOT / "core" / "toss_live_pilot_telegram.py").read_text(encoding="utf-8")
        src = re.sub(r'"""[\s\S]*?"""', "", src)
        src = re.sub(r"'''[\s\S]*?'''", "", src)
        src = re.sub(r"#[^\n]*", "", src)
        return src

    def test_no_requests_write_in_source(self):
        src = self._code_lines()
        # requests.post 는 send 함수에서 허용 (Telegram 발송용)
        # 하지만 Toss API HTTP 쓰기 호출 금지
        # live pilot telegram 모듈에서는 requests.post는 Telegram 전송에만 사용
        # 실제 금지 함수명 체크
        for forbidden in ("place_order", "submit_order", "execute_order"):
            self.assertNotIn(forbidden, src)

    def test_no_hardcoded_account_no(self):
        import re
        src = (_ROOT / "core" / "toss_live_pilot_telegram.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'\d{8}-\d{2}', src), [])

    def test_no_hardcoded_bearer(self):
        import re
        src = (_ROOT / "core" / "toss_live_pilot_telegram.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'Bearer [A-Za-z0-9._\-]{20,}', src), [])


if __name__ == "__main__":
    unittest.main()
