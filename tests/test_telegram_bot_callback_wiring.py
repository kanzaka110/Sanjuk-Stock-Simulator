"""
Telegram bot callback_query wiring 테스트

- allowed_updates에 callback_query 포함
- tp: callback만 paper handler로 라우팅
- 기존 message handler 영향 없음
- 금지 CTA/함수명 부재
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.telegram_bot import TelegramBot
import core.toss_paper_ledger as ledger


@pytest.fixture(autouse=True)
def _use_temp_db(tmp_path):
    db = tmp_path / "test_wiring.db"
    with patch.object(ledger, "_DB_PATH", db):
        yield


@pytest.fixture
def bot():
    return TelegramBot()


# ═══ allowed_updates ═══

class TestAllowedUpdates:
    def test_callback_query_in_source(self):
        src = (ROOT / "core" / "telegram_bot.py").read_text()
        assert '"callback_query"' in src

    def test_allowed_updates_includes_both(self):
        src = (ROOT / "core" / "telegram_bot.py").read_text()
        assert '"message"' in src
        assert '"callback_query"' in src


# ═══ _process_update 분기 ═══

class TestProcessUpdateRouting:
    def test_callback_query_routes_to_callback_handler(self, bot):
        """callback_query가 있으면 _process_callback_query 호출."""
        update = {
            "update_id": 1,
            "callback_query": {
                "id": "cb123",
                "data": "tp:a:prev_001:005930.KS",
                "message": {"chat": {"id": 12345}},
            },
        }
        with patch.object(bot, "_process_callback_query") as mock_cb:
            bot._process_update(update)
            mock_cb.assert_called_once_with(update["callback_query"])

    def test_message_still_works(self, bot):
        """message만 있으면 기존 경로 유지."""
        update = {
            "update_id": 2,
            "message": {
                "chat": {"id": 12345},
                "text": "보유종목 확인",
            },
        }
        with patch.object(bot, "_process_callback_query") as mock_cb, \
             patch("core.telegram_bot.TELEGRAM_CHAT_ID", "12345"), \
             patch.object(bot, "_reply"):
            bot._process_update(update)
            mock_cb.assert_not_called()

    def test_callback_query_prevents_message_processing(self, bot):
        """callback_query가 있으면 message 처리 안 함 (early return)."""
        update = {
            "update_id": 3,
            "callback_query": {"id": "cb", "data": "tp:a:p:s", "message": {"chat": {"id": 1}}},
            "message": {"chat": {"id": 1}, "text": "보유종목 확인"},
        }
        with patch.object(bot, "_process_callback_query") as mock_cb, \
             patch.object(bot, "_reply") as mock_reply:
            bot._process_update(update)
            mock_cb.assert_called_once()


# ═══ tp callback 라우팅 ═══

class TestTpCallbackRouting:
    def test_tp_approve(self, bot):
        """tp:a callback이 handle_toss_paper_callback으로 전달."""
        # 먼저 ledger에 preview 생성
        cands = [{"symbol": "X", "side": "buy", "quantity": 1, "limit_price": 1000,
                   "estimated_amount_krw": 1000, "confidence": 0.9, "reason": ""}]
        ccs = [{"blocks": [], "warnings": [], "toss_readiness": "paper_only",
                "live_order_allowed": False, "score_adjustments": []}]
        ledger.create_paper_preview_records("prev_w1", cands, ccs, {"cash_krw": 1e7, "usdkrw": 1539})

        cb_query = {
            "id": "cb456",
            "data": "tp:a:prev_w1:X",
            "message": {"chat": {"id": 12345}},
        }
        with patch("core.telegram_bot.TELEGRAM_CHAT_ID", "12345"), \
             patch.object(bot, "_reply") as mock_reply, \
             patch.object(bot, "_answer_callback") as mock_answer:
            bot._process_callback_query(cb_query)
            mock_reply.assert_called_once()
            msg = mock_reply.call_args[0][0]
            assert "비활성" in msg
            mock_answer.assert_called_once()

    def test_non_tp_ignored(self, bot):
        """tp: 아닌 callback은 무시."""
        cb_query = {
            "id": "cb789",
            "data": "other:something",
            "message": {"chat": {"id": 12345}},
        }
        with patch("core.telegram_bot.TELEGRAM_CHAT_ID", "12345"), \
             patch.object(bot, "_reply") as mock_reply:
            bot._process_callback_query(cb_query)
            mock_reply.assert_not_called()


# ═══ 인증 ═══

class TestAuth:
    def test_wrong_chat_id_rejected(self, bot):
        cb_query = {
            "id": "cb",
            "data": "tp:a:p:s",
            "message": {"chat": {"id": 99999}},
        }
        with patch("core.telegram_bot.TELEGRAM_CHAT_ID", "12345"), \
             patch.object(bot, "_reply") as mock_reply:
            bot._process_callback_query(cb_query)
            mock_reply.assert_not_called()


# ═══ malformed callback ═══

class TestMalformed:
    def test_empty_data(self, bot):
        cb_query = {"id": "cb", "data": "", "message": {"chat": {"id": 12345}}}
        with patch("core.telegram_bot.TELEGRAM_CHAT_ID", "12345"), \
             patch.object(bot, "_reply") as mock_reply:
            bot._process_callback_query(cb_query)
            mock_reply.assert_not_called()

    def test_tp_garbage(self, bot):
        cb_query = {"id": "cb", "data": "tp:", "message": {"chat": {"id": 12345}}}
        with patch("core.telegram_bot.TELEGRAM_CHAT_ID", "12345"), \
             patch.object(bot, "_reply") as mock_reply, \
             patch.object(bot, "_answer_callback"):
            bot._process_callback_query(cb_query)
            mock_reply.assert_called_once()
            msg = mock_reply.call_args[0][0]
            assert "비활성" in msg


# ═══ 금지 CTA/함수명 ═══

class TestForbidden:
    def _source(self) -> str:
        return (ROOT / "core" / "telegram_bot.py").read_text(encoding="utf-8")

    def test_no_forbidden_cta(self):
        src = self._source()
        for w in ["매수하기", "매도하기", "자동매매 시작", "자동거래 시작"]:
            assert w not in src, f"Forbidden CTA '{w}'"

    def test_no_order_functions(self):
        src = self._source()
        for fn in ["place_order", "submit_order", "execute_order"]:
            assert fn not in src

    def test_no_실주문_활성(self):
        assert "실주문: 활성" not in self._source()


# ═══ write routes ═══

class TestNoWriteRoutes:
    def test_no_post_put_delete(self):
        src = (ROOT / "web" / "app.py").read_text()
        for v in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            assert v not in src
