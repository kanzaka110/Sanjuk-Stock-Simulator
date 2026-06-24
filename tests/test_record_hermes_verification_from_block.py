"""tests/test_record_hermes_verification_from_block.py

record_hermes_live_pilot_verification.py --from-verify-block / --from-stdin 테스트.

1. verification_id 자동 추출 성공
2. verification_id 없으면 None
3. 민감정보 포함 텍스트 → None
4. --from-stdin dry-run
5. --from-verify-block dry-run (파일)
6. --from-stdin status 없으면 sys.exit(1)
7. --from-stdin invalid status sys.exit(1)
8. PASS/HOLD/BLOCK 기록 성공 (mock DB)
9. verification_id not found → 실패
"""

import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


_SAMPLE_BLOCK = """\
[Hermes 교차검증 요청 · Toss BUY_ONLY Live Pilot]
상태: Hermes 검증 대기
실주문: 비활성
아직 주문 전송 안 함

[HERMES_LIVE_PILOT_VERIFY]
verification_id: hv_20260624_120000_9999
pilot_id: tlive_20260624_120000_1234
preview_id: tlive_20260624_120000_1234
symbol: 091180.KS
side: buy
quantity: 1
limit_price: 30815.0
estimated_amount_krw: 30815.0
[/HERMES_LIVE_PILOT_VERIFY]

Hermes 응답 기대:
PASS / HOLD / BLOCK 중 하나.
"""

_BLOCK_NO_ID = """\
[HERMES_LIVE_PILOT_VERIFY]
pilot_id: tlive_test
symbol: 091180.KS
[/HERMES_LIVE_PILOT_VERIFY]
"""

_BLOCK_WITH_SENSITIVE = """\
[HERMES_LIVE_PILOT_VERIFY]
verification_id: hv_sensitive
accountNo: 12345678-01
[/HERMES_LIVE_PILOT_VERIFY]
"""


# ── helper: _extract_verification_id_from_block ───────────

class TestExtractVerificationId(unittest.TestCase):
    def _extract(self, text: str):
        from scripts.record_hermes_live_pilot_verification import (
            _extract_verification_id_from_block,
        )
        return _extract_verification_id_from_block(text)

    def test_extracts_id_from_valid_block(self):
        vid = self._extract(_SAMPLE_BLOCK)
        self.assertEqual(vid, "hv_20260624_120000_9999")

    def test_returns_none_when_no_id(self):
        vid = self._extract(_BLOCK_NO_ID)
        self.assertIsNone(vid)

    def test_returns_none_for_sensitive_content(self):
        vid = self._extract(_BLOCK_WITH_SENSITIVE)
        self.assertIsNone(vid)

    def test_returns_none_for_empty_text(self):
        vid = self._extract("")
        self.assertIsNone(vid)

    def test_returns_none_without_block_markers(self):
        vid = self._extract("verification_id: hv_orphan\n")
        self.assertIsNone(vid)

    def test_handles_whitespace_in_id(self):
        block = "[HERMES_LIVE_PILOT_VERIFY]\nverification_id:  hv_space  \n[/HERMES_LIVE_PILOT_VERIFY]"
        vid = self._extract(block)
        self.assertEqual(vid, "hv_space")

    def test_bearer_sensitive(self):
        block = "[HERMES_LIVE_PILOT_VERIFY]\nverification_id: hv_b\nBearer abc123\n[/HERMES_LIVE_PILOT_VERIFY]"
        vid = self._extract(block)
        self.assertIsNone(vid)


# ── 1. --from-stdin dry-run ───────────────────────────────

def _run_script(argv: list[str], stdin_text: str = "") -> tuple[int, str]:
    import importlib
    import scripts.record_hermes_live_pilot_verification as sm
    importlib.reload(sm)

    captured = StringIO()
    fake_stdin = StringIO(stdin_text)
    with patch.object(sys, "argv", ["script"] + argv), \
         patch("sys.stdout", captured), \
         patch("sys.stdin", fake_stdin):
        try:
            sm.main()
            code = 0
        except SystemExit as e:
            code = int(e.code) if e.code is not None else 0
    return code, captured.getvalue()


class TestFromStdinDryRun(unittest.TestCase):
    def test_dry_run_exit_0(self):
        code, _ = _run_script(
            ["--from-stdin", "--status", "PASS", "--reason", "테스트", "--dry-run"],
            stdin_text=_SAMPLE_BLOCK,
        )
        self.assertEqual(code, 0)

    def test_dry_run_no_db_change(self):
        """dry-run이면 record_hermes_verification이 호출되지 않아야 함."""
        with patch("core.toss_live_pilot_verification.record_hermes_verification") as mock_rec:
            code, _ = _run_script(
                ["--from-stdin", "--status", "HOLD", "--dry-run"],
                stdin_text=_SAMPLE_BLOCK,
            )
        mock_rec.assert_not_called()
        self.assertEqual(code, 0)

    def test_dry_run_output_says_no_db(self):
        _, out = _run_script(
            ["--from-stdin", "--status", "PASS", "--dry-run"],
            stdin_text=_SAMPLE_BLOCK,
        )
        self.assertIn("dry-run", out)

    def test_missing_status_exits_1(self):
        code, _ = _run_script(
            ["--from-stdin"],
            stdin_text=_SAMPLE_BLOCK,
        )
        self.assertEqual(code, 1)

    def test_invalid_status_exits_1(self):
        code, _ = _run_script(
            ["--from-stdin", "--status", "APPROVE"],
            stdin_text=_SAMPLE_BLOCK,
        )
        self.assertEqual(code, 1)

    def test_no_id_in_block_exits_1(self):
        code, _ = _run_script(
            ["--from-stdin", "--status", "PASS", "--dry-run"],
            stdin_text=_BLOCK_NO_ID,
        )
        self.assertEqual(code, 1)


# ── 2. --from-verify-block FILE dry-run ──────────────────

class TestFromVerifyBlockFile(unittest.TestCase):
    def test_file_dry_run_exit_0(self):
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
        tmp.write(_SAMPLE_BLOCK)
        tmp.close()
        try:
            code, _ = _run_script(
                ["--from-verify-block", tmp.name, "--status", "HOLD", "--dry-run"],
            )
            self.assertEqual(code, 0)
        finally:
            import os
            os.unlink(tmp.name)

    def test_missing_file_exits_1(self):
        code, _ = _run_script(
            ["--from-verify-block", "/tmp/no_such_file_hermes_test.txt",
             "--status", "PASS", "--dry-run"],
        )
        self.assertEqual(code, 1)

    def test_verification_id_extracted_from_file(self):
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
        tmp.write(_SAMPLE_BLOCK)
        tmp.close()
        try:
            _, out = _run_script(
                ["--from-verify-block", tmp.name, "--status", "PASS", "--dry-run"],
            )
            self.assertIn("hv_20260624_120000_9999", out)
        finally:
            import os
            os.unlink(tmp.name)


# ── 3. PASS/HOLD/BLOCK 기록 성공 (mock) ──────────────────

class TestFromStdinRecord(unittest.TestCase):
    def _mock_record(self, status: str) -> tuple[int, str]:
        with patch("core.toss_live_pilot_verification.record_hermes_verification") as mock_rec:
            mock_rec.return_value = {
                "ok": True,
                "verification_id": "hv_20260624_120000_9999",
                "status": status,
                "verified_at": "2026-06-24T12:00:00+09:00",
                "expires_at": "2026-06-24T12:10:00+09:00" if status == "PASS" else None,
            }
            code, out = _run_script(
                ["--from-stdin", "--status", status,
                 "--reason", f"Hermes {status} 판정", "--ttl-minutes", "10"],
                stdin_text=_SAMPLE_BLOCK,
            )
        return code, out, mock_rec

    def test_pass_record_ok(self):
        code, out, mock_rec = self._mock_record("PASS")
        self.assertEqual(code, 0)
        mock_rec.assert_called_once()
        call_kwargs = mock_rec.call_args[1]
        self.assertEqual(call_kwargs["verification_id"], "hv_20260624_120000_9999")
        self.assertEqual(call_kwargs["status"], "PASS")

    def test_hold_record_ok(self):
        code, out, mock_rec = self._mock_record("HOLD")
        self.assertEqual(code, 0)
        mock_rec.assert_called_once()

    def test_block_record_ok(self):
        code, out, mock_rec = self._mock_record("BLOCK")
        self.assertEqual(code, 0)
        mock_rec.assert_called_once()

    def test_output_shows_status(self):
        _, out, _ = self._mock_record("PASS")
        self.assertIn("PASS", out)

    def test_live_order_allowed_false_in_output(self):
        _, out, _ = self._mock_record("PASS")
        self.assertIn("live_order_allowed: false", out)


# ── 4. verification_id not found → 실패 ──────────────────

class TestVerificationIdNotFound(unittest.TestCase):
    def test_not_found_exits_1(self):
        with patch("core.toss_live_pilot_verification.record_hermes_verification") as mock_rec:
            mock_rec.return_value = {"ok": False, "reason": "verification_id not found"}
            code, _ = _run_script(
                ["--from-stdin", "--status", "PASS", "--reason", "test"],
                stdin_text=_SAMPLE_BLOCK,
            )
        self.assertEqual(code, 1)


# ── 5. 민감정보 포함 블록 → exit 0 (추출 실패 → exit 1) ──

class TestSensitiveBlockProtection(unittest.TestCase):
    def test_sensitive_block_not_extracted(self):
        code, _ = _run_script(
            ["--from-stdin", "--status", "PASS", "--dry-run"],
            stdin_text=_BLOCK_WITH_SENSITIVE,
        )
        # verification_id 추출 실패 → exit 1
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
