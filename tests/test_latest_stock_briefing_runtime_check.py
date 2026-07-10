"""tests/test_latest_stock_briefing_runtime_check.py

check_latest_stock_briefing_runtime.py 검증 테스트 (income-first 기준).
- DB 없음 → awaiting_next_briefing
- forbidden marker(삼성 자동화/Toss 수동 주문표 지시) → fail
- 수입 계기판 marker 포함 → pass
- Toss 자동운영 활성/live_order_allowed=true는 정상 (금지 아님)
- live policy schema (paper 전제 폐기)
- write routes 없음
"""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

KST = timezone(timedelta(hours=9))

import sys
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tools.check_latest_stock_briefing_runtime as chk


# ─── helpers ──────────────────────────────────────────────────────

_POST_CUTOFF_AT = "2026-07-11T12:00:00"   # INCOME_INTEGRATION_CUTOFF 이후

_INCOME_BODY = (
    "💰 오늘 수입 계기판\n"
    "[Toss AI] 실현수입: 산출불가\n"
    "  오늘 평가변동: +12,000원\n"
    "🤖 Toss: 자동운영 (autonomous_live_pilot)\n"
    "  🟢 후보 1주 예상수입 +7,000원 (실제 수입 아님)\n"
    "🏦 삼성: 수동 주문만 · 자동실행 없음\n"
)


def _make_archive_db(tmp_dir: str, rows: list[dict]) -> Path:
    p = Path(tmp_dir) / "briefing_archive.db"
    conn = sqlite3.connect(str(p))
    conn.execute("""
        CREATE TABLE archives (
            id TEXT PRIMARY KEY, created_at TEXT, briefing_type TEXT,
            title TEXT, body_text TEXT, body_html TEXT,
            subject TEXT, channel TEXT, summary TEXT,
            action_count INTEGER, tickers_json TEXT
        )
    """)
    for r in rows:
        conn.execute(
            "INSERT INTO archives VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.get("id", "abc123"),
                r.get("created_at", _POST_CUTOFF_AT),
                r.get("briefing_type", "KR_OPEN"),
                r.get("title", "test"),
                r.get("body_text", ""),
                r.get("body_html", ""),
                "", "", "", 0, "[]",
            ),
        )
    conn.commit()
    conn.close()
    return p


def _run_with_body(body: str, created_at: str = _POST_CUTOFF_AT,
                   body_html: str = "") -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_archive_db(tmp, [{
            "body_text": body, "created_at": created_at, "body_html": body_html,
        }])
        with patch.object(chk, "_db_path", return_value=db):
            return chk.run_check()


# ─── 1. DB 없음 → awaiting ────────────────────────────────────────

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


# ─── 2. forbidden marker → fail ───────────────────────────────────

class TestForbiddenMarkers(unittest.TestCase):
    def test_samsung_auto_order_is_fail(self):
        r = _run_with_body("삼성 자동주문을 켭니다")
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("삼성 자동주문", r["forbidden_cta_found"])

    def test_samsung_auto_execution_is_fail(self):
        r = _run_with_body("삼성 자동실행 예정")
        self.assertEqual(r["verdict"], "fail")

    def test_samsung_order_send_is_fail(self):
        r = _run_with_body("삼성 주문 전송 완료")
        self.assertEqual(r["verdict"], "fail")

    def test_toss_manual_ticket_instruction_is_fail(self):
        r = _run_with_body("Toss 수동 주문표를 지금 입력하세요")
        self.assertEqual(r["verdict"], "fail")

    def test_forbidden_in_html_also_detected(self):
        r = _run_with_body("정상 텍스트", body_html="<p>삼성 자동주문</p>")
        self.assertEqual(r["verdict"], "fail")

    def test_toss_autonomous_active_is_allowed(self):
        # Toss는 자율 실계좌 — 활성 표기는 금지 대상이 아니다
        r = _run_with_body(_INCOME_BODY + "\nToss 자동운영: 활성 live_order_allowed=true")
        self.assertNotEqual(r["verdict"], "fail")
        self.assertEqual(r["forbidden_cta_found"], [])


# ─── 3. required marker → pass / awaiting ─────────────────────────

class TestRequiredMarkers(unittest.TestCase):
    def test_income_body_gives_pass(self):
        r = _run_with_body(_INCOME_BODY)
        self.assertEqual(r["verdict"], "pass")
        self.assertTrue(r["income_dashboard_present"])
        self.assertTrue(r["realized_income_separated"])
        self.assertTrue(r["toss_autonomous_present"])
        self.assertTrue(r["samsung_manual_only_present"])
        self.assertEqual(r["required_markers_missing"], [])

    def test_empty_body_post_integration_is_awaiting(self):
        r = _run_with_body("")
        self.assertEqual(r["verdict"], "awaiting_next_briefing")

    def test_pre_integration_briefing_is_awaiting(self):
        r = _run_with_body("", created_at="2026-07-09T12:00:00")
        self.assertEqual(r["verdict"], "awaiting_next_briefing")
        self.assertFalse(r.get("briefing_post_integration", True))

    def test_pre_integration_forbidden_still_fails(self):
        r = _run_with_body("삼성 자동주문", created_at="2026-07-09T12:00:00")
        self.assertEqual(r["verdict"], "fail")


# ─── 4. live policy schema (paper 전제 폐기) ──────────────────────

class TestLivePolicySchema(unittest.TestCase):
    def test_live_policy_keys_present(self):
        pol = chk._check_live_policy_code()
        if "error" in pol:
            self.skipTest(f"policy unavailable: {pol['error']}")
        for key in ("autonomous_mode", "autonomous_kill_switch",
                    "live_order_allowed", "adapter_status", "live_transport_status"):
            self.assertIn(key, pol)

    def test_live_policy_types(self):
        pol = chk._check_live_policy_code()
        if "error" in pol:
            self.skipTest(f"policy unavailable: {pol['error']}")
        self.assertIsInstance(pol["autonomous_mode"], bool)
        self.assertIsInstance(pol["autonomous_kill_switch"], bool)


# ─── 5. write routes 없음 ─────────────────────────────────────────

class TestNoWriteRoutes(unittest.TestCase):
    def test_web_app_no_write_routes(self):
        routes = chk._check_write_routes()
        self.assertEqual(routes, [], f"write routes found: {routes}")


# ─── 6. code path 정적 확인 ──────────────────────────────────────

class TestCodePath(unittest.TestCase):
    def test_income_context_injected(self):
        r = chk._check_code_path()
        self.assertTrue(r["income_context_injected"])

    def test_income_finalized_and_stripped(self):
        r = chk._check_code_path()
        self.assertTrue(r["income_finalized"])
        self.assertTrue(r["toss_actions_stripped"])

    def test_renderers_present(self):
        r = chk._check_code_path()
        self.assertTrue(r["telegram_render_present"])
        self.assertTrue(r["html_render_present"])

    def test_code_path_ok(self):
        r = chk._check_code_path()
        self.assertTrue(r["ok"])


if __name__ == "__main__":
    unittest.main()
