"""
Telegram Paper 주문표 발송 payload 테스트

- 별도 sender (core/telegram.py 무변경)
- inline_keyboard 포함
- callback_data tp: prefix
- 금지 CTA/민감정보 부재
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.toss_order_preview import build_toss_paper_order_preview, generate_preview_id
from core.toss_paper_telegram import build_paper_preview_keyboard
from core.toss_cross_check import cross_check_candidate
import core.toss_paper_ledger as ledger


@pytest.fixture(autouse=True)
def _use_temp_db(tmp_path):
    db = tmp_path / "test_send.db"
    with patch.object(ledger, "_DB_PATH", db):
        yield


def _ctx(**kw) -> dict:
    base = {
        "enabled": True, "cash_krw": 10_000_000, "cash_usd": 5.67,
        "market_value_krw": 0, "total_account_value_krw": 10_000_000,
        "holdings_count": 0, "holdings": [], "usdkrw": 1539.0,
        "automation": {"enabled": False, "mode": "paper", "dry_run": True,
                       "live_orders_allowed": False, "kill_switch": True},
        "data_quality": {"toss_available": True, "cash_available": True,
                         "fx_available": True, "calendar_available": True,
                         "stale": False, "warnings": []},
    }
    base.update(kw)
    return base


def _sample():
    ctx = _ctx()
    cands = [
        {"symbol": "005930.KS", "side": "buy", "quantity": 2, "limit_price": 72000,
         "estimated_amount_krw": 144000, "confidence": 0.82, "reason": "지지선",
         "quote_age_sec": 10},
        {"symbol": "MU", "side": "buy", "quantity": 5, "limit_price": 28000,
         "estimated_amount_krw": 140000, "confidence": 0.75, "reason": "HBM",
         "quote_age_sec": 10},
    ]
    ccs = [cross_check_candidate(c["symbol"], c["side"], c["estimated_amount_krw"], ctx)
           for c in cands]
    return cands, ccs, ctx


# ═══ 별도 sender ═══

class TestTossPaperSender:
    def test_keyboard_included_in_payload(self):
        from core.toss_paper_telegram_send import send_toss_paper_preview_message
        with patch("core.toss_paper_telegram_send.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            with patch("core.toss_paper_telegram_send._get_token", return_value="tok"), \
                 patch("core.toss_paper_telegram_send._get_chat_id", return_value="123"):
                kb = [[{"text": "Paper 승인", "callback_data": "tp:a:p1:X"}]]
                ok = send_toss_paper_preview_message("test msg", kb)
                assert ok is True
                call_kwargs = mock_post.call_args[1]["json"]
                assert "reply_markup" in call_kwargs

    def test_unconfigured_returns_false(self):
        from core.toss_paper_telegram_send import send_toss_paper_preview_message
        with patch("core.toss_paper_telegram_send._get_token", return_value=""), \
             patch("core.toss_paper_telegram_send._get_chat_id", return_value=""):
            ok = send_toss_paper_preview_message("test", [[]])
            assert ok is False

    def test_does_not_import_core_telegram(self):
        """core/telegram.py를 import하지 않음."""
        src = (ROOT / "core" / "toss_paper_telegram_send.py").read_text()
        assert "from core.telegram" not in src
        assert "import core.telegram" not in src


# ═══ core/telegram.py 무변경 확인 ═══

class TestTelegramUnchanged:
    def test_no_send_message_with_keyboard(self):
        """core/telegram.py에 send_message_with_keyboard가 없어야 함."""
        src = (ROOT / "core" / "telegram.py").read_text()
        assert "send_message_with_keyboard" not in src


# ═══ preview payload ═══

class TestPreviewPayload:
    def test_text_has_not_real_order(self):
        cands, ccs, ctx = _sample()
        text = build_toss_paper_order_preview(cands, ctx, ccs)
        assert "실제 주문 아님" in text

    def test_text_has_disabled(self):
        cands, ccs, ctx = _sample()
        text = build_toss_paper_order_preview(cands, ctx, ccs)
        assert "비활성" in text

    def test_keyboard_has_tp_prefix(self):
        cands, ccs, ctx = _sample()
        pid = generate_preview_id()
        kb = build_paper_preview_keyboard(pid, cands, ccs)
        all_data = [btn["callback_data"] for row in kb for btn in row]
        assert all(d.startswith("tp:") for d in all_data)

    def test_keyboard_no_sensitive_info(self):
        cands, ccs, ctx = _sample()
        pid = generate_preview_id()
        kb = build_paper_preview_keyboard(pid, cands, ccs)
        all_data = " ".join(btn["callback_data"] for row in kb for btn in row)
        assert "token" not in all_data.lower()
        assert "secret" not in all_data.lower()
        long_nums = re.findall(r"\b\d{8,}\b", all_data)
        assert long_nums == []


# ═══ 차단 후보 버튼 ═══

class TestBlockedButtons:
    def test_blocked_has_why_only(self):
        cands, ccs, ctx = _sample()
        kb = build_paper_preview_keyboard("p1", cands, ccs)
        mu_row = kb[1]
        assert len(mu_row) == 1
        assert "차단 사유" in mu_row[0]["text"]
        assert all("Paper 승인" not in btn["text"] for btn in mu_row)

    def test_normal_has_approve_cancel(self):
        cands, ccs, ctx = _sample()
        kb = build_paper_preview_keyboard("p1", cands, ccs)
        normal_row = kb[0]
        texts = [btn["text"] for btn in normal_row]
        assert any("Paper 승인" in t for t in texts)
        assert any("Paper 취소" in t for t in texts)


# ═══ 금지 CTA ═══

class TestForbiddenCTA:
    FORBIDDEN = ["매수하기", "매도하기", "자동매매 시작", "자동거래 시작"]

    def test_not_in_text(self):
        cands, ccs, ctx = _sample()
        text = build_toss_paper_order_preview(cands, ctx, ccs)
        for w in self.FORBIDDEN:
            assert w not in text

    def test_not_in_keyboard(self):
        cands, ccs, ctx = _sample()
        kb = build_paper_preview_keyboard("p1", cands, ccs)
        all_text = " ".join(btn["text"] for row in kb for btn in row)
        for w in self.FORBIDDEN:
            assert w not in all_text

    def test_not_in_source(self):
        for f in ("core/toss_paper_telegram_send.py", "scripts/send_toss_paper_preview_test.py"):
            src = (ROOT / f).read_text(encoding="utf-8")
            for w in self.FORBIDDEN:
                assert w not in src, f"'{w}' in {f}"


# ═══ fail-closed ═══

class TestFailClosed:
    def test_no_live_active_in_text(self):
        cands, ccs, ctx = _sample()
        text = build_toss_paper_order_preview(cands, ctx, ccs)
        assert "실주문: 활성" not in text

    def test_no_live_active_in_sources(self):
        for f in ("core/toss_paper_telegram_send.py", "scripts/send_toss_paper_preview_test.py"):
            src = (ROOT / f).read_text(encoding="utf-8")
            assert "실주문: 활성" not in src


# ═══ write routes ═══

class TestNoWriteRoutes:
    def test_no_post_put_delete(self):
        src = (ROOT / "web" / "app.py").read_text()
        for v in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            assert v not in src


# ═══ 실제 주문 함수명 ═══

class TestNoOrderFunctions:
    def test_no_order_functions_in_new_files(self):
        for f in ("core/toss_paper_telegram_send.py", "scripts/send_toss_paper_preview_test.py"):
            src = (ROOT / f).read_text(encoding="utf-8")
            for fn in ("place_order", "submit_order", "execute_order"):
                assert fn not in src, f"'{fn}' in {f}"
