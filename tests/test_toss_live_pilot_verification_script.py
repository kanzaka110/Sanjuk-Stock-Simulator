"""tests/test_toss_live_pilot_verification_script.py

record_hermes_live_pilot_verification.py 스크립트 테스트.
- --create-request dry-run: DB 미기록
- --create-request: PENDING 기록 확인
- --status PASS dry-run: DB 미기록
- --status PASS: 기록 확인 (expires_at 있음)
- --status HOLD/BLOCK/ERROR: 기록 확인 (expires_at 없음)
- 잘못된 status → sys.exit
- --pilot-id 없음 + create-request → sys.exit
- --verification-id 없음 → sys.exit
- live_order_allowed 항상 False
- 민감정보 없음
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


def _run_script(argv: list[str], tmp_path: Path) -> tuple[int, str]:
    """스크립트 main() 실행, (exit_code, stdout) 반환."""
    import importlib
    import scripts.record_hermes_live_pilot_verification as script_mod

    # 임시 DB로 패치
    verif_db = tmp_path / "verif.db"
    captured = StringIO()

    with patch.object(sys, "argv", ["script"] + argv), \
         patch("core.toss_live_pilot_verification._db_path", return_value=verif_db), \
         patch("sys.stdout", captured):
        import core.toss_live_pilot_verification as vm
        vm._schema_created = False
        try:
            script_mod.main()
            code = 0
        except SystemExit as e:
            code = int(e.code) if e.code is not None else 0
        finally:
            vm._schema_created = False

    return code, captured.getvalue()


def _tmp():
    return Path(tempfile.mkdtemp())


class TestVerifScriptCreateRequest(unittest.TestCase):
    def _make_pilot(self, tmp_path: Path) -> str:
        """ledger에 pilot 생성."""
        with patch("core.toss_live_pilot_ledger._db_path",
                   return_value=tmp_path / "pilot.db"):
            import core.toss_live_pilot_ledger as lm
            lm._schema_created = False
            from core.toss_live_pilot_ledger import record_live_pilot_preview
            preview = {
                "ok": True, "symbol": "091180.KS", "side": "buy",
                "quantity": 1, "limit_price": 30000.0,
                "estimated_amount_krw": 30000.0, "blocks": [], "warnings": [],
            }
            rec = record_live_pilot_preview(preview)
            lm._schema_created = False
        return rec["pilot_id"]

    def test_create_request_dry_run_exit_0(self):
        tmp = _tmp()
        pilot_id = self._make_pilot(tmp)
        code, out = _run_script(
            ["--create-request", "--pilot-id", pilot_id,
             "--symbol", "091180.KS", "--side", "buy",
             "--quantity", "1", "--price", "30000", "--dry-run"],
            tmp,
        )
        self.assertEqual(code, 0)

    def test_create_request_dry_run_no_db_write(self):
        tmp = _tmp()
        pilot_id = self._make_pilot(tmp)
        _run_script(
            ["--create-request", "--pilot-id", pilot_id,
             "--dry-run"],
            tmp,
        )
        verif_db = tmp / "verif.db"
        # dry-run이면 DB 파일 없거나, 있어도 verif 레코드 없음
        if verif_db.exists():
            import sqlite3
            conn = sqlite3.connect(str(verif_db))
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM live_pilot_verification"
                ).fetchone()[0]
            except Exception:
                count = 0
            finally:
                conn.close()
            self.assertEqual(count, 0)

    def test_create_request_missing_pilot_id_exit_nonzero(self):
        tmp = _tmp()
        code, _ = _run_script(["--create-request"], tmp)
        self.assertNotEqual(code, 0)

    def test_create_request_output_has_hermes_block(self):
        tmp = _tmp()
        pilot_id = self._make_pilot(tmp)
        _, out = _run_script(
            ["--create-request", "--pilot-id", pilot_id,
             "--symbol", "091180.KS", "--dry-run"],
            tmp,
        )
        self.assertIn("HERMES_LIVE_PILOT_VERIFY", out)


class TestVerifScriptRecordStatus(unittest.TestCase):
    def _create_pending(self, tmp_path: Path) -> str:
        """PENDING 검증 요청 생성, verification_id 반환."""
        import core.toss_live_pilot_verification as vm
        vm._schema_created = False
        with patch("core.toss_live_pilot_verification._db_path",
                   return_value=tmp_path / "verif.db"):
            vm._schema_created = False
            from core.toss_live_pilot_verification import create_verification_request
            preview = {
                "symbol": "091180.KS", "side": "buy",
                "quantity": 1, "limit_price": 30000.0,
                "estimated_amount_krw": 30000.0,
                "pilot_id": "pilot_script_test",
                "preview_id": "pilot_script_test",
            }
            res = create_verification_request(preview, pilot_id="pilot_script_test")
            vm._schema_created = False
        return res["verification_id"]

    def test_pass_dry_run_exit_0(self):
        tmp = _tmp()
        vid = self._create_pending(tmp)
        code, _ = _run_script(
            ["--verification-id", vid, "--status", "PASS", "--dry-run"],
            tmp,
        )
        self.assertEqual(code, 0)

    def test_pass_dry_run_no_db_change(self):
        tmp = _tmp()
        vid = self._create_pending(tmp)
        _run_script(["--verification-id", vid, "--status", "PASS", "--dry-run"], tmp)
        # PENDING 상태 유지 확인
        import sqlite3
        db = tmp / "verif.db"
        if db.exists():
            conn = sqlite3.connect(str(db))
            row = conn.execute(
                "SELECT status FROM live_pilot_verification WHERE verification_id=?",
                (vid,),
            ).fetchone()
            conn.close()
            if row:
                self.assertEqual(row[0], "PENDING")

    def test_pass_recorded_has_expires_at(self):
        tmp = _tmp()
        vid = self._create_pending(tmp)
        code, out = _run_script(
            ["--verification-id", vid, "--status", "PASS", "--ttl-minutes", "10"],
            tmp,
        )
        self.assertEqual(code, 0)
        # expires_at이 출력에 있어야 함
        self.assertIn("expires_at", out)

    def test_hold_recorded_exit_0(self):
        tmp = _tmp()
        vid = self._create_pending(tmp)
        code, _ = _run_script(
            ["--verification-id", vid, "--status", "HOLD", "--reason", "price_stale"],
            tmp,
        )
        self.assertEqual(code, 0)

    def test_block_recorded_exit_0(self):
        tmp = _tmp()
        vid = self._create_pending(tmp)
        code, _ = _run_script(
            ["--verification-id", vid, "--status", "BLOCK", "--reason", "symbol_blocked"],
            tmp,
        )
        self.assertEqual(code, 0)

    def test_error_recorded_exit_0(self):
        tmp = _tmp()
        vid = self._create_pending(tmp)
        code, _ = _run_script(
            ["--verification-id", vid, "--status", "ERROR"],
            tmp,
        )
        self.assertEqual(code, 0)

    def test_invalid_status_exit_nonzero(self):
        tmp = _tmp()
        vid = self._create_pending(tmp)
        code, _ = _run_script(
            ["--verification-id", vid, "--status", "APPROVED"],
            tmp,
        )
        self.assertNotEqual(code, 0)

    def test_missing_verification_id_exit_nonzero(self):
        tmp = _tmp()
        code, _ = _run_script(["--status", "PASS"], tmp)
        self.assertNotEqual(code, 0)

    def test_missing_status_exit_nonzero(self):
        tmp = _tmp()
        code, _ = _run_script(["--verification-id", "hv_xxx"], tmp)
        self.assertNotEqual(code, 0)

    def test_output_says_live_order_allowed_false(self):
        tmp = _tmp()
        vid = self._create_pending(tmp)
        _, out = _run_script(
            ["--verification-id", vid, "--status", "PASS"],
            tmp,
        )
        self.assertIn("false", out.lower())

    def test_no_sensitive_in_output(self):
        tmp = _tmp()
        vid = self._create_pending(tmp)
        _, out = _run_script(
            ["--verification-id", vid, "--status", "PASS"],
            tmp,
        )
        for kw in ("accountNo", "Bearer", "APP_KEY", "APP_SECRET"):
            self.assertNotIn(kw, out)


if __name__ == "__main__":
    unittest.main()
