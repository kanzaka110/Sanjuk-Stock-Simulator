"""tests/test_latest_stock_briefing_runtime_check.py

check_latest_stock_briefing_runtime.py 검증 테스트.
- DB 없음 → awaiting_next_briefing
- forbidden CTA 탐지 → fail
- required marker 포함 → pass
- MU 보호 문구
- dashboard API schema (live_order_allowed=false, open>=0)
- write routes 없음
"""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

KST = timezone(timedelta(hours=9))

# 프로젝트 루트를 sys.path에 추가
import sys
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tools.check_latest_stock_briefing_runtime as chk


# ─── helpers ──────────────────────────────────────────────────────

def _make_archive_db(tmp_dir: str, rows: list[dict]) -> Path:
    """임시 briefing_archive.db 생성."""
    p = Path(tmp_dir) / "briefing_archive.db"
    conn = sqlite3.connect(str(p))
    conn.execute("""
        CREATE TABLE archives (
            id TEXT PRIMARY KEY,
            created_at TEXT,
            briefing_type TEXT,
            title TEXT,
            body_text TEXT,
            body_html TEXT,
            subject TEXT,
            channel TEXT,
            summary TEXT,
            action_count INTEGER,
            tickers_json TEXT
        )
    """)
    for r in rows:
        conn.execute(
            "INSERT INTO archives VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.get("id", "abc123"),
                r.get("created_at", "2026-06-24T12:00:00"),
                r.get("briefing_type", "KR_OPEN"),
                r.get("title", "test"),
                r.get("body_text", ""),
                r.get("body_html", ""),
                r.get("subject", ""),
                r.get("channel", ""),
                r.get("summary", ""),
                r.get("action_count", 0),
                r.get("tickers_json", "[]"),
            ),
        )
    conn.commit()
    conn.close()
    return p


# ─── 1. DB 없음 → awaiting_next_briefing ─────────────────────────

class TestNoBriefingDB(unittest.TestCase):
    def test_no_db_returns_awaiting(self):
        with patch.object(chk, "_db_path", return_value=Path("/nonexistent/path.db")):
            r = chk.run_check()
        self.assertEqual(r["verdict"], "awaiting_next_briefing")
        self.assertFalse(r["latest_briefing_found"])

    def test_no_db_no_forbidden(self):
        with patch.object(chk, "_db_path", return_value=Path("/nonexistent/path.db")):
            r = chk.run_check()
        self.assertEqual(r.get("forbidden_cta_found", []), [])


# ─── 2. forbidden CTA → fail ──────────────────────────────────────

class TestForbiddenCTA(unittest.TestCase):
    def _run_with_body(self, body: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_archive_db(tmp, [{"body_text": body}])
            with patch.object(chk, "_db_path", return_value=db):
                return chk.run_check()

    def test_real_order_active_is_fail(self):
        r = self._run_with_body("실주문: 활성 → 매매 진행")
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("실주문: 활성", r["forbidden_cta_found"])

    def test_auto_trading_start_is_fail(self):
        r = self._run_with_body("자동매매 시작 버튼 누름")
        self.assertEqual(r["verdict"], "fail")

    def test_buy_button_is_fail(self):
        r = self._run_with_body("매수하기 버튼을 눌러주세요")
        self.assertEqual(r["verdict"], "fail")

    def test_sell_button_is_fail(self):
        r = self._run_with_body("매도하기 버튼을 눌러주세요")
        self.assertEqual(r["verdict"], "fail")

    def test_mu_sell_execute_is_fail(self):
        r = self._run_with_body("MU 매도 실행 즉시 진행")
        self.assertEqual(r["verdict"], "fail")

    def test_order_execute_is_fail(self):
        r = self._run_with_body("주문 실행 완료")
        self.assertEqual(r["verdict"], "fail")

    def test_clean_body_not_fail(self):
        r = self._run_with_body("MU 보유 관리 — 실매도 아님")
        self.assertNotEqual(r["verdict"], "fail")
        self.assertEqual(r["forbidden_cta_found"], [])


# ─── 3. required marker → pass / awaiting ─────────────────────────

class TestRequiredMarkers(unittest.TestCase):
    _TOSS_BODY = (
        "[Toss Paper 성과 — 실제 주문 아님]\n"
        "- 진행 중: 1건\n"
        "- SOFI 1주 open\n"
        "- 승률: 표본부족 / 평가 대기\n"
        "- 실주문: 비활성\n"
        "- 기존 포트폴리오 미합산\n"
    )

    def _run_with_body(self, body: str) -> dict:
        # post-integration: 2026-06-25T12:00:00 (cutoff 이후)
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_archive_db(tmp, [{"body_text": body, "created_at": "2026-06-25T12:00:00"}])
            with patch.object(chk, "_db_path", return_value=db):
                return chk.run_check()

    def test_toss_paper_present_marker(self):
        r = self._run_with_body(self._TOSS_BODY)
        self.assertTrue(r["toss_paper_present"])

    def test_paper_only_guard_marker(self):
        r = self._run_with_body(self._TOSS_BODY)
        self.assertTrue(r["paper_only_guard"])

    def test_sofi_open_displayed(self):
        r = self._run_with_body(self._TOSS_BODY)
        self.assertTrue(r["sofi_open_displayed"])

    def test_full_toss_body_gives_pass(self):
        r = self._run_with_body(self._TOSS_BODY)
        self.assertEqual(r["verdict"], "pass")

    def test_empty_body_post_integration_is_awaiting(self):
        r = self._run_with_body("")
        self.assertEqual(r["verdict"], "awaiting_next_briefing")

    def test_pre_integration_briefing_is_awaiting(self):
        """통합 이전 브리핑이면 Toss Paper 마커 없어도 awaiting."""
        with tempfile.TemporaryDirectory() as tmp:
            # 2026-06-23T00:00 → cutoff(2026-06-24T00:30) 이전
            db = _make_archive_db(tmp, [{"body_text": "", "created_at": "2026-06-23T00:00:00"}])
            with patch.object(chk, "_db_path", return_value=db):
                r = chk.run_check()
        self.assertEqual(r["verdict"], "awaiting_next_briefing")
        self.assertFalse(r.get("briefing_post_integration", True))


# ─── 4. MU 보호 문구 ──────────────────────────────────────────────

class TestMUProtection(unittest.TestCase):
    def test_mu_hold_management_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_archive_db(tmp, [{"body_text": "MU 보유 관리 — 분할 매도 검토"}])
            with patch.object(chk, "_db_path", return_value=db):
                r = chk.run_check()
        self.assertTrue(r["mu_protection"])
        self.assertNotIn("MU 매도 실행", r["forbidden_cta_found"])

    def test_mu_sell_execute_forbidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_archive_db(tmp, [{"body_text": "MU 매도 실행 즉시"}])
            with patch.object(chk, "_db_path", return_value=db):
                r = chk.run_check()
        self.assertFalse(r["mu_protection"])
        self.assertEqual(r["verdict"], "fail")


# ─── 5. dashboard API schema (paper policy 직접 확인) ────────────

class TestPaperPolicySchema(unittest.TestCase):
    """compute_toss_paper_policy 반환값의 스키마 검증."""

    def test_live_order_allowed_false(self):
        from core.toss_paper_policy import compute_toss_paper_policy
        policy = compute_toss_paper_policy()
        self.assertFalse(policy.get("live_order_allowed"))

    def test_mode_paper_only(self):
        from core.toss_paper_policy import compute_toss_paper_policy
        policy = compute_toss_paper_policy()
        self.assertEqual(policy.get("mode"), "paper_only")

    def test_max_budget_krw_present(self):
        from core.toss_paper_policy import compute_toss_paper_policy
        policy = compute_toss_paper_policy()
        self.assertIn("max_budget_krw", policy)
        self.assertGreater(policy["max_budget_krw"], 0)

    def test_sample_status_present(self):
        from core.toss_paper_policy import compute_toss_paper_policy
        policy = compute_toss_paper_policy()
        self.assertIn(policy.get("sample_status"), ("insufficient", "stable", "good"))


# ─── 6. paper performance open=1, duplicate=[] ────────────────────

class TestPaperPerformanceState(unittest.TestCase):
    def test_open_count_non_negative(self):
        from core.toss_paper_performance import get_paper_performance_summary
        s = get_paper_performance_summary().get("summary", {})
        self.assertGreaterEqual(s.get("open", 0), 0)

    def test_duplicate_open_symbols_empty(self):
        from core.toss_paper_performance import get_paper_performance_summary
        s = get_paper_performance_summary().get("summary", {})
        self.assertEqual(s.get("duplicate_open_symbols", []), [])

    def test_evaluated_count_non_negative_int(self):
        """evaluated_count는 시간에 따라 증가하는 상태값 — 음수/비정상만 차단."""
        from core.toss_paper_performance import get_paper_performance_summary
        s = get_paper_performance_summary().get("summary", {})
        count = s.get("evaluated_count", 0)
        self.assertIsInstance(count, int)
        self.assertGreaterEqual(count, 0)


# ─── 7. write routes 없음 ─────────────────────────────────────────

class TestNoWriteRoutes(unittest.TestCase):
    def test_web_app_no_write_routes(self):
        routes = chk._check_write_routes()
        self.assertEqual(routes, [], f"write routes found: {routes}")


# ─── 8. code path 정적 확인 ──────────────────────────────────────

class TestCodePath(unittest.TestCase):
    def test_toss_paper_injected_in_analyzer(self):
        r = chk._check_code_path()
        self.assertTrue(r["toss_paper_injected"])

    def test_live_order_guard_present(self):
        r = chk._check_code_path()
        self.assertTrue(r["live_order_guard_present"])

    def test_code_path_ok(self):
        r = chk._check_code_path()
        self.assertTrue(r["ok"])


# ─── 9. forbidden marker 탐지 — html 영역 ────────────────────────

class TestForbiddenInHTML(unittest.TestCase):
    def test_forbidden_in_html_also_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_archive_db(tmp, [
                {"body_text": "정상 텍스트", "body_html": "<p>매수하기</p>"}
            ])
            with patch.object(chk, "_db_path", return_value=db):
                r = chk.run_check()
        self.assertIn("매수하기", r["forbidden_cta_found"])
        self.assertEqual(r["verdict"], "fail")


if __name__ == "__main__":
    unittest.main()
