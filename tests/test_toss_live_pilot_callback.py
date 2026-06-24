"""tests/test_toss_live_pilot_callback.py

handle_live_pilot_callback + telegram_bot routing 테스트.
- confirm 항상 차단 (adapter disabled)
- live_order_sent=False 항상
- tlp: prefix (Paper tp: 와 분리)
- ledger 상태 변화
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.toss_live_pilot_telegram import handle_live_pilot_callback


# ─── 1. review callback ───────────────────────────────────

class TestReviewCallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._patch = patch(
            "core.toss_live_pilot_ledger._db_path",
            return_value=Path(self.tmp) / "test_pilot.db",
        )
        self._patch.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def tearDown(self):
        self._patch.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def _create_pilot(self) -> str:
        from core.toss_live_pilot_ledger import record_live_pilot_preview
        preview = {
            "ok": True, "preview_id": "tlive_test_r",
            "symbol": "069500.KS", "side": "buy", "quantity": 1,
            "limit_price": 40000, "estimated_amount_krw": 40000,
            "blocks": [], "warnings": [],
        }
        rec = record_live_pilot_preview(preview)
        return rec["pilot_id"]

    def test_review_ok(self):
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:review:{pilot_id}")
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "review")

    def test_review_live_order_sent_false(self):
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:review:{pilot_id}")
        self.assertFalse(result["live_order_sent"])

    def test_review_message_not_sent_phrase(self):
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:review:{pilot_id}")
        self.assertIn("아직 주문 전송 안 함", result["message"])

    def test_review_message_disabled(self):
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:review:{pilot_id}")
        self.assertIn("비활성", result["message"])

    def test_review_no_buy_cta(self):
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:review:{pilot_id}")
        self.assertNotIn("매수하기", result["message"])


# ─── 2. confirm callback (항상 차단) ─────────────────────

class TestConfirmCallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._patch = patch(
            "core.toss_live_pilot_ledger._db_path",
            return_value=Path(self.tmp) / "test_pilot.db",
        )
        self._patch.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def tearDown(self):
        self._patch.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def _create_pilot(self) -> str:
        from core.toss_live_pilot_ledger import record_live_pilot_preview
        preview = {
            "ok": True, "preview_id": "tlive_test_c",
            "symbol": "069500.KS", "side": "buy", "quantity": 1,
            "limit_price": 40000, "estimated_amount_krw": 40000,
            "blocks": [], "warnings": [],
        }
        rec = record_live_pilot_preview(preview)
        return rec["pilot_id"]

    def test_confirm_ok_false(self):
        """confirm은 항상 ok=False (adapter disabled)."""
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertFalse(result["ok"])

    def test_confirm_blocked_true(self):
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertTrue(result.get("blocked"))

    def test_confirm_live_order_sent_false(self):
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertFalse(result["live_order_sent"])

    def test_confirm_reason_adapter_disabled(self):
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertEqual(result.get("reason"), "toss_order_adapter_disabled")

    def test_confirm_adapter_status_disabled(self):
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertEqual(result.get("adapter_status"), "disabled")

    def test_confirm_message_blocked_phrase(self):
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertIn("차단", result["message"])

    def test_confirm_message_not_sent(self):
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertIn("아직 주문 전송 안 함", result["message"])

    def test_confirm_message_live_order_sent_false_text(self):
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertIn("live_order_sent=false", result["message"])

    def test_confirm_message_disabled_text(self):
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertIn("비활성", result["message"])

    def test_confirm_no_buy_cta(self):
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:confirm:{pilot_id}")
        self.assertNotIn("매수하기", result["message"])

    def test_confirm_no_http_request(self):
        """confirm이 실제 HTTP 요청을 하지 않는지 확인."""
        import re
        src = (_ROOT / "core" / "toss_live_pilot_telegram.py").read_text(encoding="utf-8")
        # _handle_confirm 함수에서 requests.post 없음 (Telegram 발송 함수에는 있어도 됨)
        # confirm handler 부분만 체크
        confirm_section = src[src.find("def _handle_confirm"):src.find("def _handle_cancel")]
        src_no_doc = re.sub(r'"""[\s\S]*?"""', "", confirm_section)
        src_no_doc = re.sub(r"#[^\n]*", "", src_no_doc)
        self.assertNotIn("requests.post", src_no_doc)
        self.assertNotIn("requests.put", src_no_doc)


# ─── 3. cancel callback ───────────────────────────────────

class TestCancelCallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._patch = patch(
            "core.toss_live_pilot_ledger._db_path",
            return_value=Path(self.tmp) / "test_pilot.db",
        )
        self._patch.start()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def tearDown(self):
        self._patch.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def _create_pilot(self) -> str:
        from core.toss_live_pilot_ledger import record_live_pilot_preview
        preview = {
            "ok": True, "preview_id": "tlive_test_x",
            "symbol": "069500.KS", "side": "buy", "quantity": 1,
            "limit_price": 40000, "estimated_amount_krw": 40000,
            "blocks": [], "warnings": [],
        }
        rec = record_live_pilot_preview(preview)
        return rec["pilot_id"]

    def test_cancel_ok(self):
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:cancel:{pilot_id}")
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "cancel")

    def test_cancel_live_order_sent_false(self):
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:cancel:{pilot_id}")
        self.assertFalse(result["live_order_sent"])

    def test_cancel_message_disabled(self):
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:cancel:{pilot_id}")
        self.assertIn("비활성", result["message"])

    def test_cancel_message_not_sent(self):
        pilot_id = self._create_pilot()
        result = handle_live_pilot_callback(f"tlp:cancel:{pilot_id}")
        self.assertIn("아직 주문 전송 안 함", result["message"])

    def test_cancel_ledger_status(self):
        from core.toss_live_pilot_ledger import list_live_pilot_records
        pilot_id = self._create_pilot()
        handle_live_pilot_callback(f"tlp:cancel:{pilot_id}")
        records = list_live_pilot_records()
        matched = [r for r in records if r["pilot_id"] == pilot_id]
        self.assertTrue(matched)
        self.assertEqual(matched[0]["status"], "cancelled")

    def test_cancel_ledger_live_order_sent_false(self):
        from core.toss_live_pilot_ledger import list_live_pilot_records
        pilot_id = self._create_pilot()
        handle_live_pilot_callback(f"tlp:cancel:{pilot_id}")
        records = list_live_pilot_records()
        matched = [r for r in records if r["pilot_id"] == pilot_id]
        self.assertFalse(bool(matched[0]["live_order_sent"]))


# ─── 4. 잘못된 callback ──────────────────────────────────

class TestInvalidCallback(unittest.TestCase):
    def test_wrong_prefix_ignored(self):
        result = handle_live_pilot_callback("tp:a:preview:sym")
        self.assertFalse(result["ok"])
        self.assertFalse(result["live_order_sent"])

    def test_empty_data(self):
        result = handle_live_pilot_callback("")
        self.assertFalse(result["ok"])
        self.assertFalse(result["live_order_sent"])

    def test_unknown_action(self):
        result = handle_live_pilot_callback("tlp:send_order:preview123")
        self.assertFalse(result["ok"])
        self.assertFalse(result["live_order_sent"])

    def test_invalid_callback_message_safe(self):
        result = handle_live_pilot_callback("garbage")
        self.assertFalse(result["live_order_sent"])
        self.assertIn("비활성", result["message"])


# ─── 5. telegram_bot.py tlp: 라우팅 ──────────────────────

class TestTelegramBotRouting(unittest.TestCase):
    def test_tlp_prefix_routed_to_live_pilot(self):
        """telegram_bot.py가 tlp: callback을 live pilot handler로 라우팅."""
        src = (_ROOT / "core" / "telegram_bot.py").read_text(encoding="utf-8")
        self.assertIn("tlp:", src)
        self.assertIn("handle_live_pilot_callback", src)
        self.assertIn("toss_live_pilot_telegram", src)

    def test_tp_prefix_still_handled(self):
        """tp: Paper callback 라우팅 유지."""
        src = (_ROOT / "core" / "telegram_bot.py").read_text(encoding="utf-8")
        self.assertIn("tp:", src)
        self.assertIn("handle_toss_paper_callback", src)

    def test_prefixes_not_mixed(self):
        """tlp: 와 tp: 혼동 없음 — 각자 별도 분기."""
        src = (_ROOT / "core" / "telegram_bot.py").read_text(encoding="utf-8")
        # tlp: 는 live pilot, tp: 는 paper — 별도 if 블록에 있어야 함
        tlp_pos = src.find("tlp:")
        tp_pos = src.find('"tp:"')
        self.assertGreater(tlp_pos, 0)
        self.assertGreater(tp_pos, 0)
        self.assertNotEqual(tlp_pos, tp_pos)

    def test_live_pilot_callback_not_sent_to_paper(self):
        """tlp: callback이 toss_paper_telegram handler로 전달되지 않음."""
        import re
        src = (_ROOT / "core" / "telegram_bot.py").read_text(encoding="utf-8")
        # tlp: 분기 내에 handle_toss_paper_callback 없어야 함
        tlp_section_match = re.search(r'if data\.startswith\("tlp:"\).*?(?=if data\.startswith\("tp:"\))', src, re.DOTALL)
        if tlp_section_match:
            tlp_section = tlp_section_match.group(0)
            self.assertNotIn("handle_toss_paper_callback", tlp_section)


# ─── 6. Paper SOFI 미접촉 ────────────────────────────────

class TestPaperSOFIUnaffected(unittest.TestCase):
    def test_sofi_paper_open_unchanged(self):
        from core.toss_paper_performance import get_paper_performance_summary
        before = get_paper_performance_summary().get("summary", {}).get("open", 0)
        # live pilot callback 실행
        handle_live_pilot_callback("tlp:confirm:nonexistent_id")
        after = get_paper_performance_summary().get("summary", {}).get("open", 0)
        self.assertEqual(before, after)

    def test_paper_ledger_not_touched_by_confirm(self):
        with patch("core.toss_paper_ledger.approve_paper_order") as mock:
            handle_live_pilot_callback("tlp:confirm:test_id")
            mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
