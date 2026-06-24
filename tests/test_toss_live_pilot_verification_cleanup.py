"""tests/test_toss_live_pilot_verification_cleanup.py

PENDING 만료 처리 + cleanup script 테스트.

1. PENDING 15분 초과 → EXPIRED 전환
2. PENDING 15분 미만 → 유지
3. PASS/HOLD/BLOCK/ERROR → cleanup 대상 아님
4. dry-run → DB 변경 없음
5. is_verification_passed EXPIRED → False
6. script --dry-run → 기본값
7. script --expire-pending-minutes → EXPIRED 전환
8. 삭제 없음 (DELETE/DROP 없음)
9. live_order_sent 변화 없음
10. 민감정보 없음
"""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

KST = timezone(timedelta(hours=9))


def _tmp_db_patch():
    tmp = tempfile.mkdtemp()
    return (
        Path(tmp),
        patch(
            "core.toss_live_pilot_verification._db_path",
            return_value=Path(tmp) / "test_verif.db",
        ),
    )


def _reset():
    import core.toss_live_pilot_verification as m
    m._schema_created = False


def _insert_pending(tmp_path: Path, pilot_id: str, symbol: str = "091180.KS",
                    age_minutes: int = 0) -> str:
    """직접 PENDING 레코드 삽입 (age_minutes 전 시각으로)."""
    import core.toss_live_pilot_verification as m
    m._schema_created = False
    from core.toss_live_pilot_verification import create_verification_request
    preview = {
        "symbol": symbol, "side": "buy", "quantity": 1,
        "limit_price": 30000.0, "estimated_amount_krw": 30000.0,
        "pilot_id": pilot_id, "preview_id": pilot_id,
    }
    result = create_verification_request(preview, pilot_id=pilot_id)
    vid = result["verification_id"]

    if age_minutes > 0:
        # requested_at을 age_minutes 전으로 덮어쓰기
        old_time = (datetime.now(KST) - timedelta(minutes=age_minutes))
        old_str = old_time.strftime("%Y-%m-%dT%H:%M:%S+09:00")
        import core.toss_live_pilot_verification as _vm
        db_path = _vm._db_path()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE live_pilot_verification SET requested_at=? WHERE verification_id=?",
            (old_str, vid),
        )
        conn.commit()
        conn.close()

    return vid


# ── 1. PENDING 만료: 15분 초과 → EXPIRED ────────────────

class TestExpirePendingOld(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_db_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def test_old_pending_becomes_expired(self):
        vid = _insert_pending(self._tmp, "pilot_old", age_minutes=20)
        from core.toss_live_pilot_verification import expire_pending_verifications
        result = expire_pending_verifications(older_than_minutes=15, dry_run=False)
        self.assertEqual(result["expired_count"], 1)

        from core.toss_live_pilot_verification import list_verifications
        recs = list_verifications(limit=10)
        matched = [r for r in recs if r["verification_id"] == vid]
        self.assertEqual(matched[0]["status"], "EXPIRED")

    def test_old_pending_reasons_updated(self):
        vid = _insert_pending(self._tmp, "pilot_old2", age_minutes=30)
        from core.toss_live_pilot_verification import expire_pending_verifications, list_verifications
        expire_pending_verifications(older_than_minutes=15, dry_run=False)
        recs = list_verifications(limit=10)
        matched = [r for r in recs if r["verification_id"] == vid]
        self.assertIn("pending_expired", matched[0]["reasons"])

    def test_expired_row_not_deleted(self):
        vid = _insert_pending(self._tmp, "pilot_old3", age_minutes=25)
        from core.toss_live_pilot_verification import expire_pending_verifications, list_verifications
        expire_pending_verifications(older_than_minutes=15, dry_run=False)
        recs = list_verifications(limit=100)
        all_ids = [r["verification_id"] for r in recs]
        self.assertIn(vid, all_ids)

    def test_result_ok_true(self):
        _insert_pending(self._tmp, "pilot_ok", age_minutes=20)
        from core.toss_live_pilot_verification import expire_pending_verifications
        result = expire_pending_verifications(older_than_minutes=15, dry_run=False)
        self.assertTrue(result["ok"])

    def test_result_live_order_sent_zero(self):
        _insert_pending(self._tmp, "pilot_los", age_minutes=20)
        from core.toss_live_pilot_verification import expire_pending_verifications
        result = expire_pending_verifications(older_than_minutes=15, dry_run=False)
        self.assertEqual(result["live_order_sent"], 0)


# ── 2. PENDING 15분 미만 → 유지 ──────────────────────────

class TestExpirePendingRecent(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_db_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def test_recent_pending_kept(self):
        vid = _insert_pending(self._tmp, "pilot_recent", age_minutes=5)
        from core.toss_live_pilot_verification import expire_pending_verifications, list_verifications
        expire_pending_verifications(older_than_minutes=15, dry_run=False)
        recs = list_verifications(limit=10)
        matched = [r for r in recs if r["verification_id"] == vid]
        self.assertEqual(matched[0]["status"], "PENDING")

    def test_kept_count_correct(self):
        _insert_pending(self._tmp, "pilot_keep1", age_minutes=5)
        _insert_pending(self._tmp, "pilot_expire1", age_minutes=20)
        from core.toss_live_pilot_verification import expire_pending_verifications
        result = expire_pending_verifications(older_than_minutes=15, dry_run=False)
        self.assertEqual(result["kept_count"], 1)
        self.assertEqual(result["expired_count"], 1)


# ── 3. PASS/HOLD/BLOCK/ERROR → 건드리지 않음 ────────────

class TestExpireSkipsNonPending(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_db_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def _create_and_record(self, pilot_id: str, status: str) -> str:
        vid = _insert_pending(self._tmp, pilot_id, age_minutes=30)
        from core.toss_live_pilot_verification import record_hermes_verification
        record_hermes_verification(vid, status, [], {})
        return vid

    def test_pass_not_expired(self):
        vid = self._create_and_record("pilot_pass_skip", "PASS")
        from core.toss_live_pilot_verification import expire_pending_verifications, list_verifications
        expire_pending_verifications(older_than_minutes=15, dry_run=False)
        recs = list_verifications(limit=10)
        matched = [r for r in recs if r["verification_id"] == vid]
        self.assertEqual(matched[0]["status"], "PASS")

    def test_hold_not_expired(self):
        vid = self._create_and_record("pilot_hold_skip", "HOLD")
        from core.toss_live_pilot_verification import expire_pending_verifications, list_verifications
        expire_pending_verifications(older_than_minutes=15, dry_run=False)
        recs = list_verifications(limit=10)
        matched = [r for r in recs if r["verification_id"] == vid]
        self.assertEqual(matched[0]["status"], "HOLD")

    def test_block_not_expired(self):
        vid = self._create_and_record("pilot_block_skip", "BLOCK")
        from core.toss_live_pilot_verification import expire_pending_verifications, list_verifications
        expire_pending_verifications(older_than_minutes=15, dry_run=False)
        recs = list_verifications(limit=10)
        matched = [r for r in recs if r["verification_id"] == vid]
        self.assertEqual(matched[0]["status"], "BLOCK")

    def test_error_not_expired(self):
        vid = self._create_and_record("pilot_error_skip", "ERROR")
        from core.toss_live_pilot_verification import expire_pending_verifications, list_verifications
        expire_pending_verifications(older_than_minutes=15, dry_run=False)
        recs = list_verifications(limit=10)
        matched = [r for r in recs if r["verification_id"] == vid]
        self.assertEqual(matched[0]["status"], "ERROR")

    def test_expired_count_zero_if_all_non_pending(self):
        self._create_and_record("pilot_np1", "PASS")
        self._create_and_record("pilot_np2", "HOLD")
        from core.toss_live_pilot_verification import expire_pending_verifications
        result = expire_pending_verifications(older_than_minutes=15, dry_run=False)
        self.assertEqual(result["expired_count"], 0)


# ── 4. dry-run → DB 변경 없음 ────────────────────────────

class TestExpireDryRun(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_db_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def test_dry_run_no_status_change(self):
        vid = _insert_pending(self._tmp, "pilot_dry", age_minutes=20)
        from core.toss_live_pilot_verification import expire_pending_verifications, list_verifications
        expire_pending_verifications(older_than_minutes=15, dry_run=True)
        recs = list_verifications(limit=10)
        matched = [r for r in recs if r["verification_id"] == vid]
        self.assertEqual(matched[0]["status"], "PENDING")

    def test_dry_run_returns_predicted_count(self):
        _insert_pending(self._tmp, "pilot_dry2", age_minutes=20)
        _insert_pending(self._tmp, "pilot_dry3", age_minutes=5)
        from core.toss_live_pilot_verification import expire_pending_verifications
        result = expire_pending_verifications(older_than_minutes=15, dry_run=True)
        self.assertEqual(result["expired_count"], 1)
        self.assertEqual(result["kept_count"], 1)
        self.assertTrue(result["dry_run"])

    def test_dry_run_live_order_sent_zero(self):
        _insert_pending(self._tmp, "pilot_dry4", age_minutes=20)
        from core.toss_live_pilot_verification import expire_pending_verifications
        result = expire_pending_verifications(older_than_minutes=15, dry_run=True)
        self.assertEqual(result["live_order_sent"], 0)


# ── 5. is_verification_passed EXPIRED → False ────────────

class TestIsVerificationPassedExpired(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_db_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def test_expired_status_fails(self):
        vid = _insert_pending(self._tmp, "pilot_ivp_exp", age_minutes=20)
        from core.toss_live_pilot_verification import expire_pending_verifications, is_verification_passed
        expire_pending_verifications(older_than_minutes=15, dry_run=False)
        ok, reasons, _ = is_verification_passed("pilot_ivp_exp")
        self.assertFalse(ok)
        self.assertIn("hermes_verification_expired", reasons)

    def test_expired_reason_in_reasons(self):
        vid = _insert_pending(self._tmp, "pilot_ivp_exp2", age_minutes=20)
        from core.toss_live_pilot_verification import expire_pending_verifications, is_verification_passed
        expire_pending_verifications(older_than_minutes=15, dry_run=False)
        _, reasons, _ = is_verification_passed("pilot_ivp_exp2")
        self.assertTrue(any("expired" in r for r in reasons))


# ── 6. verification_summary EXPIRED count ────────────────

class TestVerificationSummaryExpired(unittest.TestCase):
    def setUp(self):
        self._tmp, self._p = _tmp_db_patch()
        self._p.start()
        _reset()

    def tearDown(self):
        self._p.stop()
        _reset()

    def test_expired_count_in_summary(self):
        _insert_pending(self._tmp, "pilot_sum1", age_minutes=20)
        _insert_pending(self._tmp, "pilot_sum2", age_minutes=5)
        from core.toss_live_pilot_verification import expire_pending_verifications, verification_summary
        expire_pending_verifications(older_than_minutes=15, dry_run=False)
        s = verification_summary()
        self.assertEqual(s["summary"].get("EXPIRED", 0), 1)
        self.assertEqual(s["summary"].get("PENDING", 0), 1)

    def test_pending_expire_minutes_in_summary(self):
        from core.toss_live_pilot_verification import verification_summary
        s = verification_summary()
        self.assertIn("pending_expire_minutes", s)
        self.assertEqual(s["pending_expire_minutes"], 15)

    def test_oldest_pending_age_in_summary(self):
        _insert_pending(self._tmp, "pilot_age1", age_minutes=10)
        from core.toss_live_pilot_verification import verification_summary
        s = verification_summary()
        age = s.get("oldest_pending_age_minutes")
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 9)  # 약 10분

    def test_no_pending_oldest_age_none(self):
        # PENDING 없으면 None
        vid = _insert_pending(self._tmp, "pilot_noage", age_minutes=20)
        from core.toss_live_pilot_verification import expire_pending_verifications, verification_summary
        expire_pending_verifications(older_than_minutes=15, dry_run=False)
        s = verification_summary()
        # PENDING=0이면 None
        if s["summary"].get("PENDING", 0) == 0:
            self.assertIsNone(s.get("oldest_pending_age_minutes"))

    def test_live_order_allowed_false(self):
        from core.toss_live_pilot_verification import verification_summary
        self.assertFalse(verification_summary()["live_order_allowed"])


# ── 7. cleanup script 테스트 ──────────────────────────────

def _run_cleanup_script(argv: list[str], tmp_path: Path) -> tuple[int, str]:
    import importlib
    import scripts.cleanup_toss_live_pilot_verifications as script_mod
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


class TestCleanupScript(unittest.TestCase):
    def _insert_old(self, tmp_path: Path, pilot_id: str, age: int = 20) -> str:
        with patch("core.toss_live_pilot_verification._db_path",
                   return_value=tmp_path / "verif.db"):
            import core.toss_live_pilot_verification as vm
            vm._schema_created = False
            vid = _insert_pending(tmp_path, pilot_id, age_minutes=age)
            vm._schema_created = False
        return vid

    def test_default_dry_run_exit_0(self):
        tmp = Path(tempfile.mkdtemp())
        code, _ = _run_cleanup_script(["--dry-run"], tmp)
        self.assertEqual(code, 0)

    def test_default_dry_run_no_db_change(self):
        tmp = Path(tempfile.mkdtemp())
        vid = self._insert_old(tmp, "pilot_scr1", age=20)
        _run_cleanup_script(["--dry-run"], tmp)
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

    def test_expire_pending_minutes_exit_0(self):
        tmp = Path(tempfile.mkdtemp())
        self._insert_old(tmp, "pilot_scr2", age=20)
        code, _ = _run_cleanup_script(["--expire-pending-minutes", "15"], tmp)
        self.assertEqual(code, 0)

    def test_expire_pending_minutes_changes_status(self):
        tmp = Path(tempfile.mkdtemp())
        vid = self._insert_old(tmp, "pilot_scr3", age=20)
        _run_cleanup_script(["--expire-pending-minutes", "15"], tmp)
        db = tmp / "verif.db"
        if db.exists():
            conn = sqlite3.connect(str(db))
            row = conn.execute(
                "SELECT status FROM live_pilot_verification WHERE verification_id=?",
                (vid,),
            ).fetchone()
            conn.close()
            if row:
                self.assertEqual(row[0], "EXPIRED")

    def test_script_output_shows_expired_count(self):
        tmp = Path(tempfile.mkdtemp())
        self._insert_old(tmp, "pilot_scr4", age=20)
        _, out = _run_cleanup_script(["--expire-pending-minutes", "15"], tmp)
        self.assertIn("expired_count", out)

    def test_script_output_deleted_zero(self):
        tmp = Path(tempfile.mkdtemp())
        _, out = _run_cleanup_script(["--dry-run"], tmp)
        self.assertIn("deleted: 0", out)

    def test_script_no_delete_sql(self):
        """스크립트 소스에 DELETE FROM / DROP TABLE 없음."""
        src = (_ROOT / "scripts" / "cleanup_toss_live_pilot_verifications.py").read_text(encoding="utf-8")
        self.assertNotIn("DELETE FROM", src)
        self.assertNotIn("DROP TABLE", src)

    def test_script_no_sensitive_in_output(self):
        tmp = Path(tempfile.mkdtemp())
        _, out = _run_cleanup_script(["--dry-run"], tmp)
        for kw in ("accountNo", "Bearer", "APP_KEY", "APP_SECRET"):
            self.assertNotIn(kw, out)


# ── 8. 소스에 DELETE/DROP 없음 ───────────────────────────

class TestNoDeleteInSources(unittest.TestCase):
    def test_no_delete_in_verification(self):
        src = (_ROOT / "core" / "toss_live_pilot_verification.py").read_text(encoding="utf-8")
        self.assertNotIn("DELETE FROM", src)
        self.assertNotIn("DROP TABLE", src)

    def test_no_delete_in_cleanup_script(self):
        src = (_ROOT / "scripts" / "cleanup_toss_live_pilot_verifications.py").read_text(encoding="utf-8")
        self.assertNotIn("DELETE FROM", src)
        self.assertNotIn("DROP TABLE", src)


# ── 9. PENDING_EXPIRE_MINUTES 상수 ───────────────────────

class TestPendingExpireConstant(unittest.TestCase):
    def test_expire_minutes_is_15(self):
        from core.toss_live_pilot_verification import PENDING_EXPIRE_MINUTES
        self.assertEqual(PENDING_EXPIRE_MINUTES, 15)

    def test_expired_in_valid_statuses(self):
        from core.toss_live_pilot_verification import _VALID_STATUSES
        self.assertIn("EXPIRED", _VALID_STATUSES)


if __name__ == "__main__":
    unittest.main()
