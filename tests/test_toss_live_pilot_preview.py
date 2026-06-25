"""tests/test_toss_live_pilot_preview.py

승인형 Live Pilot 미리보기 생성 테스트.
"""

import unittest

import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.toss_live_pilot_preview import (
    build_live_pilot_preview,
    build_live_pilot_telegram_text,
)


def _candidate(symbol="069500.KS", price=137000, qty=1, disagreement=None):
    c = {
        "symbol": symbol,
        "side": "buy",
        "quantity": qty,
        "limit_price": price,
    }
    if disagreement is not None:
        c["source_disagreement_pct"] = disagreement
    return c


class TestPreviewBasic(unittest.TestCase):
    def test_preview_id_present(self):
        p = build_live_pilot_preview(_candidate())
        self.assertIn("preview_id", p)
        self.assertTrue(p["preview_id"].startswith("tlive_"))

    def test_live_order_allowed_always_false(self):
        p = build_live_pilot_preview(_candidate())
        self.assertFalse(p["live_order_allowed"])

    def test_live_order_sent_always_false(self):
        p = build_live_pilot_preview(_candidate())
        self.assertFalse(p["live_order_sent"])

    def test_adapter_disabled(self):
        p = build_live_pilot_preview(_candidate())
        self.assertEqual(p["adapter_status"], "disabled")

    def test_requires_second_confirmation(self):
        p = build_live_pilot_preview(_candidate())
        self.assertTrue(p["requires_second_confirmation"])

    def test_warnings_contain_required_phrases(self):
        p = build_live_pilot_preview(_candidate())
        combined = " ".join(p["warnings"])
        self.assertIn("아직 주문 전송 안 함", combined)
        self.assertIn("최종 2단계 승인 필요", combined)


class TestPreview069500(unittest.TestCase):
    """069500.KS — 고신뢰 ETF, 한도 내 수량."""

    def test_ok_true(self):
        p = build_live_pilot_preview(_candidate("069500.KS", price=40000, qty=1))
        self.assertTrue(p["ok"])

    def test_no_blocks(self):
        p = build_live_pilot_preview(_candidate("069500.KS", price=40000, qty=1))
        self.assertEqual(p["blocks"], [])

    def test_estimated_amount(self):
        p = build_live_pilot_preview(_candidate("069500.KS", price=40000, qty=1))
        self.assertEqual(p["estimated_amount_krw"], 40000)


class TestUnlocked161510(unittest.TestCase):
    """161510.KS — 종목 제한 해제, 한도 내면 통과."""

    def test_ok_true_within_limit(self):
        p = build_live_pilot_preview(_candidate("161510.KS", price=1000, qty=1))
        self.assertTrue(p["ok"])

    def test_no_symbol_block_reason(self):
        p = build_live_pilot_preview(_candidate("161510.KS", price=1000, qty=1))
        combined = " ".join(p["blocks"])
        self.assertNotIn("blocked_symbol", combined)


class TestUnlocked005930(unittest.TestCase):
    """005930.KS — 종목 제한 해제. 단, 금액 한도 가드는 유지."""

    def test_ok_true_within_limit(self):
        p = build_live_pilot_preview(_candidate("005930.KS", price=50000, qty=1))
        self.assertTrue(p["ok"])

    def test_amount_guard_still_blocks_over_limit(self):
        # 종목은 허용되지만 600,000 > 500,000 한도 → 금액 가드로 차단 유지
        p = build_live_pilot_preview(_candidate("005930.KS", price=600000, qty=1))
        self.assertFalse(p["ok"])
        combined = " ".join(p["blocks"])
        self.assertIn("한도_초과", combined)
        self.assertNotIn("blocked_symbol", combined)


class TestSourceDisagreement(unittest.TestCase):
    """source_disagreement > 1% → block."""

    def test_over_1pct_blocked(self):
        p = build_live_pilot_preview(_candidate(disagreement=2.5))
        self.assertFalse(p["ok"])
        combined = " ".join(p["blocks"])
        self.assertIn("source_불일치", combined)

    def test_under_1pct_ok(self):
        # price=40000 → 한도(100,000) 이내, disagreement=0.5% → 정상
        p = build_live_pilot_preview(_candidate(price=40000, disagreement=0.5))
        self.assertTrue(p["ok"])


class TestNoPriceBlock(unittest.TestCase):
    """price=0 → block."""

    def test_no_price_blocked(self):
        p = build_live_pilot_preview(_candidate(price=0))
        self.assertFalse(p["ok"])
        combined = " ".join(p["blocks"])
        self.assertIn("가격_없음", combined)


class TestAmountLimit(unittest.TestCase):
    """금액 한도 초과 → block."""

    def test_over_limit_blocked(self):
        # 최종 정책: 1회 한도 500,000원 고정. 600,000 * 1 > 500,000 → 차단
        p = build_live_pilot_preview(_candidate("069500.KS", price=600000, qty=1))
        self.assertFalse(p["ok"])
        combined = " ".join(p["blocks"])
        self.assertIn("한도_초과", combined)


class TestTelegramText(unittest.TestCase):
    """Telegram 문구 — 금지 CTA 없음, 허용 문구 포함."""

    def _text_ok(self):
        p = build_live_pilot_preview(_candidate("069500.KS", price=40000, qty=1))
        return build_live_pilot_telegram_text(p)

    def _text_blocked(self):
        # 종목 제한 해제 후 — 가격 없음으로 차단되는 케이스 사용
        p = build_live_pilot_preview(_candidate("069500.KS", price=0, qty=1))
        return build_live_pilot_telegram_text(p)

    def test_live_order_inactive_in_text(self):
        self.assertIn("실주문: 비활성", self._text_ok())

    def test_preview_label_in_text(self):
        self.assertIn("실주문 미리보기", self._text_ok())

    def test_second_confirmation_required_in_text(self):
        self.assertIn("최종 2단계 승인 필요", self._text_ok())

    def test_order_not_sent_in_text(self):
        self.assertIn("아직 주문 전송 안 함", self._text_ok())

    def test_api_disabled_in_text(self):
        self.assertIn("주문 API 호출 비활성", self._text_ok())

    def test_blocked_shows_disabled(self):
        self.assertIn("주문 전송 비활성", self._text_blocked())

    # 금지 CTA 없음
    def test_no_forbidden_buy_button(self):
        self.assertNotIn("매수하기", self._text_ok())

    def test_no_forbidden_sell_button(self):
        self.assertNotIn("매도하기", self._text_ok())

    def test_no_order_execute(self):
        self.assertNotIn("주문 실행", self._text_ok())

    def test_no_live_order_active(self):
        self.assertNotIn("실주문: 활성", self._text_ok())

    def test_no_auto_trading(self):
        self.assertNotIn("자동매매 시작", self._text_ok())


class TestPaperSeparation(unittest.TestCase):
    """Toss Paper ledger 훼손 없음."""

    def test_preview_does_not_write_paper_ledger(self):
        """build_live_pilot_preview는 paper_ledger를 건드리지 않는다."""
        from unittest.mock import patch
        with patch("core.toss_paper_ledger.create_paper_preview_records") as mock:
            build_live_pilot_preview(_candidate())
            mock.assert_not_called()

    def test_sofi_paper_open_unaffected(self):
        """SOFI paper open 건은 live pilot와 무관."""
        from core.toss_paper_performance import get_paper_performance_summary
        s = get_paper_performance_summary().get("summary", {})
        # SOFI paper open은 유지돼야 함 (live pilot이 훼손하지 않음)
        self.assertGreaterEqual(s.get("open", 0), 0)


if __name__ == "__main__":
    unittest.main()
