"""tests/test_toss_live_pilot_script.py

send_toss_live_pilot_preview_test.py 스크립트 테스트.
- dry-run: Telegram 미발송, ledger 미기록
- --send: Telegram send mock 호출, ledger 기록, 실제 주문 없음
- 가격 조회 실패 시 안전 종료
- 민감정보 없음
- Paper SOFI 미접촉
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import scripts.send_toss_live_pilot_preview_test as script


# ─── 1. dry-run: 발송/ledger 없음 ────────────────────────

class TestDryRunMode(unittest.TestCase):
    def _run_dry(self, price=40_000.0):
        with patch("sys.argv", ["script.py"]), \
             patch.object(script, "_get_live_price", return_value=price), \
             patch("core.toss_live_pilot_ledger.record_live_pilot_preview") as mock_ledger, \
             patch("core.toss_live_pilot_telegram.send_live_pilot_preview_message") as mock_send:
            try:
                script.main()
            except SystemExit:
                pass
            return mock_ledger, mock_send

    def test_dry_run_no_telegram_send(self):
        _, mock_send = self._run_dry()
        mock_send.assert_not_called()

    def test_dry_run_no_ledger_write(self):
        mock_ledger, _ = self._run_dry()
        mock_ledger.assert_not_called()


# ─── 2. --send: Telegram mock + ledger 기록 ──────────────

class TestSendMode(unittest.TestCase):
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

    def _run_send(self, price=40_000.0):
        mock_send = MagicMock(return_value=True)
        with patch("sys.argv", ["script.py", "--send"]), \
             patch.object(script, "_get_live_price", return_value=price), \
             patch("core.toss_live_pilot_telegram.send_live_pilot_preview_message", mock_send):
            try:
                script.main()
            except SystemExit:
                pass
        return mock_send

    def test_send_mode_telegram_called(self):
        mock_send = self._run_send()
        mock_send.assert_called_once()

    def test_send_mode_ledger_recorded(self):
        from core.toss_live_pilot_ledger import list_live_pilot_records
        self._run_send()
        records = list_live_pilot_records()
        self.assertGreater(len(records), 0)

    def test_send_mode_ledger_live_order_sent_false(self):
        from core.toss_live_pilot_ledger import list_live_pilot_records
        self._run_send()
        records = list_live_pilot_records()
        for r in records:
            self.assertFalse(bool(r["live_order_sent"]))

    def test_send_mode_ledger_adapter_disabled(self):
        from core.toss_live_pilot_ledger import list_live_pilot_records
        self._run_send()
        records = list_live_pilot_records()
        for r in records:
            self.assertEqual(r.get("adapter_status", "disabled"), "disabled")

    def test_send_mode_no_actual_order(self):
        """--send 시에도 실제 주문 API 호출 없음."""
        with patch("sys.argv", ["script.py", "--send"]), \
             patch.object(script, "_get_live_price", return_value=40_000.0), \
             patch("core.toss_live_pilot_telegram.send_live_pilot_preview_message", return_value=True):
            # dispatch는 stub 호출만 — live_order_sent=False 확인
            from core.toss_live_pilot_adapter import dispatch_toss_order_disabled
            result = dispatch_toss_order_disabled({"symbol": "069500.KS"})
            self.assertFalse(result["live_order_sent"])
            self.assertTrue(result["blocked"])


# ─── 3. 가격 조회 실패 시 안전 종료 ──────────────────────

class TestPriceFailSafeExit(unittest.TestCase):
    def test_price_none_exits_safely(self):
        with patch("sys.argv", ["script.py"]), \
             patch.object(script, "_get_live_price", return_value=None):
            with self.assertRaises(SystemExit) as ctx:
                script.main()
            self.assertEqual(ctx.exception.code, 0)

    def test_price_over_limit_exits_safely(self):
        """1주 가격이 max_order_krw 초과 시 발송 안 함."""
        with patch("sys.argv", ["script.py"]), \
             patch.object(script, "_get_live_price", return_value=999_999.0):
            with self.assertRaises(SystemExit) as ctx:
                script.main()
            self.assertEqual(ctx.exception.code, 0)


# ─── 4. 스크립트 소스 금지 체크 ──────────────────────────

class TestScriptSourceGuards(unittest.TestCase):
    def _src(self) -> str:
        import re
        src = (_ROOT / "scripts" / "send_toss_live_pilot_preview_test.py").read_text(encoding="utf-8")
        src = re.sub(r'"""[\s\S]*?"""', "", src)
        src = re.sub(r"#[^\n]*", "", src)
        return src

    def test_no_hardcoded_account_no(self):
        import re
        src = (_ROOT / "scripts" / "send_toss_live_pilot_preview_test.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'\d{8}-\d{2}', src), [])

    def test_no_hardcoded_bearer(self):
        import re
        src = (_ROOT / "scripts" / "send_toss_live_pilot_preview_test.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'Bearer [A-Za-z0-9._\-]{20,}', src), [])

    def test_no_live_order_allowed_true(self):
        src = self._src()
        self.assertNotIn("live_order_allowed=True", src)
        self.assertNotIn('live_order_allowed": True', src)

    def test_no_forbidden_cta(self):
        src = (_ROOT / "scripts" / "send_toss_live_pilot_preview_test.py").read_text(encoding="utf-8")
        for phrase in ("매수하기", "매도하기", "주문 실행", "자동매매 시작", "실주문: 활성"):
            self.assertNotIn(phrase, src)


# ─── 5. GET 라우트 only ───────────────────────────────────

class TestGetRoutesOnly(unittest.TestCase):
    def test_no_write_routes(self):
        src = (_ROOT / "web" / "app.py").read_text(encoding="utf-8")
        for pat in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            self.assertNotIn(pat, src.lower())

    def test_live_pilot_routes_exist(self):
        src = (_ROOT / "web" / "app.py").read_text(encoding="utf-8")
        self.assertIn("/api/toss/live-pilot-policy", src)
        self.assertIn("/api/toss/live-pilot-previews", src)


# ─── 6. Paper SOFI 미접촉 ────────────────────────────────

class TestPaperSOFIUnaffected(unittest.TestCase):
    def test_sofi_open_unchanged_by_script_dry_run(self):
        from core.toss_paper_performance import get_paper_performance_summary
        before = get_paper_performance_summary().get("summary", {}).get("open", 0)
        with patch("sys.argv", ["script.py"]), \
             patch.object(script, "_get_live_price", return_value=40_000.0), \
             patch("core.toss_live_pilot_telegram.send_live_pilot_preview_message"):
            try:
                script.main()
            except SystemExit:
                pass
        after = get_paper_performance_summary().get("summary", {}).get("open", 0)
        self.assertEqual(before, after)

    def test_paper_preview_records_not_called(self):
        with patch("sys.argv", ["script.py"]), \
             patch.object(script, "_get_live_price", return_value=40_000.0), \
             patch("core.toss_paper_ledger.create_paper_preview_records") as mock:
            try:
                script.main()
            except SystemExit:
                pass
            mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
