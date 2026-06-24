"""tests/test_toss_live_pilot_guardrails.py

Live Pilot 가드레일 테스트.
- adapter stub 항상 blocked
- ledger 상태 흐름
- API GET-only 확인
- 민감정보 마스킹
- Paper와 분리
- 금지 함수명 없음
- MU 보호
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ─── 1. Adapter stub 항상 blocked ─────────────────────────────────

class TestAdapterStub(unittest.TestCase):
    def setUp(self):
        from core.toss_live_pilot_adapter import send_live_pilot_order_stub
        self.send = send_live_pilot_order_stub

    def test_always_blocked(self):
        r = self.send({"symbol": "069500.KS", "preview_id": "test"})
        self.assertTrue(r["blocked"])

    def test_ok_false(self):
        r = self.send({"symbol": "069500.KS", "preview_id": "test"})
        self.assertFalse(r["ok"])

    def test_live_order_sent_false(self):
        r = self.send({"symbol": "SOFI", "preview_id": "test"})
        self.assertFalse(r["live_order_sent"])

    def test_reason_adapter_disabled(self):
        r = self.send({"symbol": "NVDA", "preview_id": "test"})
        self.assertEqual(r["reason"], "live_pilot_order_adapter_disabled")

    def test_adapter_status_disabled(self):
        r = self.send({})
        self.assertEqual(r["adapter_status"], "disabled")

    def test_message_no_order_sent(self):
        r = self.send({})
        self.assertIn("아직 주문 전송 안 함", r["message"])


class TestAdapterStatus(unittest.TestCase):
    def test_get_adapter_status_disabled(self):
        from core.toss_live_pilot_adapter import get_adapter_status
        s = get_adapter_status()
        self.assertEqual(s["status"], "disabled")
        self.assertFalse(s["live_order_allowed"])


# ─── 2. Ledger 상태 흐름 ──────────────────────────────────────────

class TestLedgerFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._patch = patch(
            "core.toss_live_pilot_ledger._db_path",
            return_value=Path(self.tmp) / "test_pilot.db",
        )
        self._patch.start()
        # schema 재생성을 위해 flag 초기화
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def tearDown(self):
        self._patch.stop()
        import core.toss_live_pilot_ledger as m
        m._schema_created = False

    def _preview(self, symbol="069500.KS", ok=True):
        return {
            "ok": ok,
            "preview_id": "tlive_test_001",
            "symbol": symbol,
            "side": "buy",
            "quantity": 1,
            "limit_price": 40000,
            "estimated_amount_krw": 40000,
            "blocks": [] if ok else ["test_block"],
            "warnings": [],
        }

    def test_record_preview_returns_ok(self):
        from core.toss_live_pilot_ledger import record_live_pilot_preview
        r = record_live_pilot_preview(self._preview())
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "previewed")

    def test_blocked_preview_status(self):
        from core.toss_live_pilot_ledger import record_live_pilot_preview
        r = record_live_pilot_preview(self._preview(ok=False))
        self.assertEqual(r["status"], "blocked")

    def test_confirm_attempt_sets_confirmed_but_not_sent(self):
        from core.toss_live_pilot_ledger import (
            record_live_pilot_preview,
            record_confirm_attempt,
        )
        rec = record_live_pilot_preview(self._preview())
        pilot_id = rec["pilot_id"]
        result = record_confirm_attempt(pilot_id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "confirmed_but_not_sent")
        self.assertFalse(result["live_order_sent"])

    def test_confirm_reason_adapter_disabled(self):
        from core.toss_live_pilot_ledger import (
            record_live_pilot_preview,
            record_confirm_attempt,
        )
        rec = record_live_pilot_preview(self._preview())
        result = record_confirm_attempt(rec["pilot_id"])
        self.assertEqual(result["reason"], "live_pilot_order_adapter_disabled")

    def test_cancel_sets_cancelled(self):
        from core.toss_live_pilot_ledger import (
            record_live_pilot_preview,
            cancel_live_pilot,
        )
        rec = record_live_pilot_preview(self._preview())
        result = cancel_live_pilot(rec["pilot_id"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "cancelled")

    def test_no_filled_status_possible(self):
        """ledger에 'filled' 또는 'sent' status는 존재하지 않는다."""
        from core.toss_live_pilot_ledger import (
            record_live_pilot_preview,
            list_live_pilot_records,
        )
        record_live_pilot_preview(self._preview())
        records = list_live_pilot_records()
        for r in records:
            self.assertNotIn(r["status"], ("filled", "sent", "submitted"))

    def test_live_order_sent_always_zero_in_db(self):
        from core.toss_live_pilot_ledger import (
            record_live_pilot_preview,
            list_live_pilot_records,
        )
        record_live_pilot_preview(self._preview())
        records = list_live_pilot_records()
        for r in records:
            self.assertFalse(bool(r.get("live_order_sent")))

    def test_summary_live_order_sent_total_zero(self):
        from core.toss_live_pilot_ledger import live_pilot_ledger_summary
        s = live_pilot_ledger_summary()
        self.assertEqual(s["live_order_sent_total"], 0)

    def test_summary_live_order_allowed_false(self):
        from core.toss_live_pilot_ledger import live_pilot_ledger_summary
        s = live_pilot_ledger_summary()
        self.assertFalse(s["live_order_allowed"])


# ─── 3. API GET-only 확인 ─────────────────────────────────────────

class TestAPIGetOnly(unittest.TestCase):
    def test_no_post_put_delete_patch_routes(self):
        app_py = _ROOT / "web" / "app.py"
        src = app_py.read_text(encoding="utf-8")
        for forbidden in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            self.assertNotIn(forbidden, src.lower(),
                             f"write route found: {forbidden}")

    def test_live_pilot_policy_route_is_get(self):
        app_py = _ROOT / "web" / "app.py"
        src = app_py.read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/toss/live-pilot-policy")', src)

    def test_live_pilot_previews_route_is_get(self):
        app_py = _ROOT / "web" / "app.py"
        src = app_py.read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/toss/live-pilot-previews")', src)


# ─── 4. 민감정보 마스킹 ──────────────────────────────────────────

class TestNoSensitiveInfo(unittest.TestCase):
    def _all_sources(self) -> str:
        files = [
            "core/toss_live_pilot_policy.py",
            "core/toss_live_pilot_preview.py",
            "core/toss_live_pilot_ledger.py",
            "core/toss_live_pilot_adapter.py",
        ]
        return "\n".join(
            (_ROOT / f).read_text(encoding="utf-8") for f in files
        )

    def test_no_hardcoded_app_secret(self):
        src = self._all_sources()
        import re
        # hardcoded secrets (not env var references)
        matches = re.findall(r'(?<![_A-Z\'"=])(APP_SECRET|APP_KEY)\s*=\s*["\'][^"\']+["\']', src)
        self.assertEqual(matches, [], f"hardcoded secret found: {matches}")

    def test_no_bearer_token_hardcoded(self):
        src = self._all_sources()
        import re
        # Bearer followed by actual token (20+ chars)
        matches = re.findall(r'Bearer [A-Za-z0-9._\-]{20,}', src)
        self.assertEqual(matches, [])

    def test_no_account_no_hardcoded(self):
        src = self._all_sources()
        import re
        # 8자리-2자리 계좌번호 패턴
        matches = re.findall(r'\d{8}-\d{2}', src)
        self.assertEqual(matches, [])


# ─── 5. 금지 함수명 없음 ─────────────────────────────────────────

class TestNoForbiddenFunctionNames(unittest.TestCase):
    def _all_sources(self) -> str:
        files = [
            "core/toss_live_pilot_policy.py",
            "core/toss_live_pilot_preview.py",
            "core/toss_live_pilot_ledger.py",
            "core/toss_live_pilot_adapter.py",
        ]
        return "\n".join(
            (_ROOT / f).read_text(encoding="utf-8") for f in files
        )

    def test_no_place_order(self):
        import re
        src = self._all_sources()
        self.assertFalse(re.search(r'\bdef place_order\b', src))

    def test_no_submit_order(self):
        import re
        src = self._all_sources()
        self.assertFalse(re.search(r'\bdef submit_order\b', src))

    def test_no_execute_order(self):
        import re
        src = self._all_sources()
        self.assertFalse(re.search(r'\bdef execute_order\b', src))


# ─── 6. Paper와 분리 ─────────────────────────────────────────────

class TestPaperSeparation(unittest.TestCase):
    def test_paper_ledger_unaffected_by_live_pilot_policy(self):
        """live pilot policy 조회가 paper ledger를 건드리지 않는다."""
        from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
        with patch("core.toss_paper_ledger.paper_ledger_summary") as mock:
            compute_toss_live_pilot_policy(evaluated_count=0)
            mock.assert_not_called()

    def test_sofi_paper_open_unchanged(self):
        from core.toss_paper_performance import get_paper_performance_summary
        from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
        before = get_paper_performance_summary().get("summary", {}).get("open", 0)
        compute_toss_live_pilot_policy(evaluated_count=0)
        after = get_paper_performance_summary().get("summary", {}).get("open", 0)
        self.assertEqual(before, after)

    def test_live_pilot_not_in_portfolio_api(self):
        """portfolio API (/api/portfolio)에 live pilot 합산 없음."""
        app_py = _ROOT / "web" / "app.py"
        src = app_py.read_text(encoding="utf-8")
        # portfolio 라우트 주변에 live_pilot 합산 코드 없음
        import re
        portfolio_block = re.search(
            r'@app\.get\("/api/portfolio"\).*?(?=@app\.get|\Z)', src, re.DOTALL
        )
        if portfolio_block:
            block_src = portfolio_block.group(0)
            self.assertNotIn("live_pilot", block_src)


# ─── 7. MU 보호 ──────────────────────────────────────────────────

class TestMUProtection(unittest.TestCase):
    def test_mu_not_in_preferred_symbols(self):
        from core.toss_live_pilot_policy import _PREFERRED_SYMBOLS
        self.assertNotIn("MU", _PREFERRED_SYMBOLS)

    def test_mu_telegram_text_no_sell_execute(self):
        from core.toss_live_pilot_preview import build_live_pilot_preview, build_live_pilot_telegram_text
        p = build_live_pilot_preview({
            "symbol": "MU",
            "side": "sell",
            "quantity": 1,
            "limit_price": 100,
        })
        text = build_live_pilot_telegram_text(p)
        self.assertNotIn("MU 매도 실행", text)


# ─── 8. 금지 CTA in all new source files ─────────────────────────

class TestNoForbiddenCTAInSources(unittest.TestCase):
    """실행 시 Telegram 출력으로 나갈 수 있는 f-string/문자열에 금지 CTA 없음 확인.

    docstring/comment 안에서 '금지:' 설명 목적으로 등장하는 것은 허용.
    실제 return/yield 되는 문자열 리터럴만 검사한다.
    """
    FORBIDDEN = [
        "자동매매 시작",
        "자동거래 시작",
        "주문 실행",
        "매수하기",
        "매도하기",
        "MU 매도 실행",
        # "실주문: 활성" — docstring 설명에 등장하므로 여기선 제외,
        # 실제 출력 검증은 TestTelegramText에서 담당
    ]

    def _code_lines(self) -> str:
        """docstring, 주석 줄을 제거한 소스 코드만 반환."""
        files = [
            "core/toss_live_pilot_policy.py",
            "core/toss_live_pilot_preview.py",
            "core/toss_live_pilot_ledger.py",
            "core/toss_live_pilot_adapter.py",
        ]
        result_lines = []
        for f in files:
            src = (_ROOT / f).read_text(encoding="utf-8")
            in_docstring = False
            docstring_char = None
            for line in src.splitlines():
                stripped = line.strip()
                # 단순 docstring toggle (""" / ''')
                if not in_docstring:
                    for q in ('"""', "'''"):
                        if stripped.startswith(q):
                            count = stripped.count(q)
                            if count >= 2 and stripped.endswith(q) and len(stripped) > 3:
                                # single-line docstring, skip
                                break
                            else:
                                in_docstring = True
                                docstring_char = q
                                break
                    else:
                        if not stripped.startswith("#"):
                            result_lines.append(line)
                else:
                    if docstring_char and docstring_char in stripped:
                        in_docstring = False
                        docstring_char = None
                    # skip docstring body
        return "\n".join(result_lines)

    def test_no_forbidden_cta_in_code(self):
        src = self._code_lines()
        for marker in self.FORBIDDEN:
            self.assertNotIn(marker, src, f"forbidden CTA in code: {marker!r}")

    def test_no_live_order_active_in_output_strings(self):
        """실주문: 활성 은 출력 반환값에 없어야 한다 (Telegram text 함수 결과 확인)."""
        from core.toss_live_pilot_preview import build_live_pilot_telegram_text, build_live_pilot_preview
        # ok case
        p = build_live_pilot_preview({"symbol": "069500.KS", "side": "buy", "quantity": 1, "limit_price": 40000})
        text = build_live_pilot_telegram_text(p)
        self.assertNotIn("실주문: 활성", text)
        # blocked case
        p2 = build_live_pilot_preview({"symbol": "005930.KS", "side": "buy", "quantity": 1, "limit_price": 319000})
        text2 = build_live_pilot_telegram_text(p2)
        self.assertNotIn("실주문: 활성", text2)


if __name__ == "__main__":
    unittest.main()
