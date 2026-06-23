"""
Toss paper Telegram handler 테스트

- keyboard 생성
- approve/cancel/why callback
- fail-closed
- 금지 CTA/함수명 부재
- 민감정보 부재
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.toss_paper_telegram import (
    build_callback_data,
    build_paper_preview_keyboard,
    handle_toss_paper_callback,
    parse_callback_data,
)
import core.toss_paper_ledger as ledger


@pytest.fixture(autouse=True)
def _use_temp_db(tmp_path):
    db = tmp_path / "test_tg.db"
    with patch.object(ledger, "_DB_PATH", db):
        yield


def _setup_preview():
    """테스트용 preview 생성."""
    cands = [
        {"symbol": "005930.KS", "side": "buy", "quantity": 2, "limit_price": 72000,
         "estimated_amount_krw": 144000, "confidence": 0.8, "reason": "지지선"},
        {"symbol": "MU", "side": "buy", "quantity": 5, "limit_price": 28000,
         "estimated_amount_krw": 140000, "confidence": 0.75, "reason": "HBM"},
    ]
    ccs = [
        {"blocks": [], "warnings": [], "toss_readiness": "paper_only",
         "live_order_allowed": False, "score_adjustments": []},
        {"blocks": ["blacklisted", "mu_protected"], "warnings": ["MU 보호"],
         "toss_readiness": "blocked", "live_order_allowed": False, "score_adjustments": []},
    ]
    ctx = {"cash_krw": 10_000_000, "usdkrw": 1539}
    records = ledger.create_paper_preview_records("prev_tg1", cands, ccs, ctx)
    return cands, ccs, records


# ═══ callback data ═══

class TestCallbackData:
    def test_build(self):
        d = build_callback_data("a", "prev_001", "005930.KS")
        assert d == "tp:a:prev_001:005930.KS"

    def test_parse(self):
        r = parse_callback_data("tp:a:prev_001:005930.KS")
        assert r["action"] == "a"
        assert r["preview_id"] == "prev_001"
        assert r["symbol"] == "005930.KS"

    def test_parse_no_symbol(self):
        r = parse_callback_data("tp:c:prev_001:")
        assert r["action"] == "c"
        assert r["symbol"] == ""

    def test_parse_invalid(self):
        assert parse_callback_data("garbage") is None
        assert parse_callback_data("") is None
        assert parse_callback_data("tp:") is None

    def test_no_sensitive_info(self):
        d = build_callback_data("a", "prev_001", "005930.KS")
        assert "token" not in d.lower()
        assert "secret" not in d.lower()
        # 8자리+ 연속 숫자 없음 (005930은 6자리)
        long_nums = re.findall(r"\b\d{8,}\b", d)
        assert long_nums == []


# ═══ keyboard 생성 ═══

class TestKeyboard:
    def test_normal_candidate_buttons(self):
        cands = [{"symbol": "005930.KS"}]
        ccs = [{"blocks": [], "warnings": []}]
        kb = build_paper_preview_keyboard("prev_001", cands, ccs)
        assert len(kb) == 1
        assert len(kb[0]) == 2
        assert "Paper 승인" in kb[0][0]["text"]
        assert "Paper 취소" in kb[0][1]["text"]

    def test_blocked_candidate_button(self):
        cands = [{"symbol": "MU"}]
        ccs = [{"blocks": ["blacklisted"], "warnings": []}]
        kb = build_paper_preview_keyboard("prev_001", cands, ccs)
        assert len(kb) == 1
        assert "차단 사유" in kb[0][0]["text"]

    def test_no_forbidden_labels(self):
        cands = [{"symbol": "X"}, {"symbol": "Y"}]
        ccs = [{"blocks": []}, {"blocks": ["z"]}]
        kb = build_paper_preview_keyboard("prev_001", cands, ccs)
        all_text = " ".join(btn["text"] for row in kb for btn in row)
        for word in ["매수하기", "매도하기", "자동매매 시작", "자동거래 시작"]:
            assert word not in all_text


# ═══ approve callback ═══

class TestApproveCallback:
    def test_approve_normal(self):
        cands, ccs, records = _setup_preview()
        cb = build_callback_data("a", "prev_tg1", "005930.KS")
        result = handle_toss_paper_callback(cb)
        assert result["ok"] is True
        assert result["action"] == "approve"
        assert "Paper 승인 완료" in result["message"]
        assert "실제 주문 아님" in result["message"]
        assert "비활성" in result["message"]

    def test_approve_blocked(self):
        cands, ccs, records = _setup_preview()
        cb = build_callback_data("a", "prev_tg1", "MU")
        result = handle_toss_paper_callback(cb)
        assert "승인 거절" in result["message"]
        assert "비활성" in result["message"]

    def test_approve_nonexistent(self):
        cb = build_callback_data("a", "nonexistent", "X")
        result = handle_toss_paper_callback(cb)
        assert result["ok"] is False
        assert "비활성" in result["message"]


# ═══ cancel callback ═══

class TestCancelCallback:
    def test_cancel(self):
        _setup_preview()
        cb = build_callback_data("c", "prev_tg1", "005930.KS")
        result = handle_toss_paper_callback(cb)
        assert result["ok"] is True
        assert result["action"] == "cancel"
        assert "취소 완료" in result["message"]
        assert "실제 주문 없음" in result["message"]


# ═══ why callback ═══

class TestWhyCallback:
    def test_why_blocked(self):
        _setup_preview()
        cb = build_callback_data("w", "prev_tg1", "MU")
        result = handle_toss_paper_callback(cb)
        assert result["ok"] is True
        assert "차단" in result["message"] or "경고" in result["message"]
        assert "비활성" in result["message"]

    def test_why_nonexistent(self):
        cb = build_callback_data("w", "nonexistent", "X")
        result = handle_toss_paper_callback(cb)
        assert "찾을 수 없" in result["message"]


# ═══ malformed callback ═══

class TestMalformedCallback:
    def test_garbage(self):
        result = handle_toss_paper_callback("garbage_data")
        assert result["ok"] is False
        assert "비활성" in result["message"]

    def test_empty(self):
        result = handle_toss_paper_callback("")
        assert result["ok"] is False

    def test_unknown_action(self):
        result = handle_toss_paper_callback("tp:z:prev_001:X")
        assert result["ok"] is False
        assert "알 수 없는" in result["message"]

    def test_no_ledger_change_on_garbage(self):
        orders_before = ledger.list_paper_orders()
        handle_toss_paper_callback("garbage")
        orders_after = ledger.list_paper_orders()
        assert len(orders_before) == len(orders_after)


# ═══ fail-closed ═══

class TestFailClosed:
    def test_all_messages_say_disabled(self):
        """모든 응답에 '비활성' 포함."""
        _setup_preview()
        for cb_data in [
            build_callback_data("a", "prev_tg1", "005930.KS"),
            build_callback_data("c", "prev_tg1", "005930.KS"),
            build_callback_data("w", "prev_tg1", "MU"),
            "garbage",
            "",
        ]:
            result = handle_toss_paper_callback(cb_data)
            assert "비활성" in result["message"], f"Missing 비활성 for: {cb_data}"

    def test_no_live_active_string(self):
        _setup_preview()
        cb = build_callback_data("a", "prev_tg1", "005930.KS")
        result = handle_toss_paper_callback(cb)
        assert "실주문: 활성" not in result["message"]


# ═══ 금지 CTA/함수명 ═══

class TestForbidden:
    def _source(self) -> str:
        return (ROOT / "core" / "toss_paper_telegram.py").read_text(encoding="utf-8")

    def test_no_forbidden_cta(self):
        src = self._source()
        for w in ["매수하기", "매도하기", "자동매매 시작", "자동거래 시작"]:
            assert w not in src

    def test_no_order_functions(self):
        src = self._source()
        for fn in ["place_order", "submit_order", "execute_order"]:
            assert fn not in src

    def test_no_실주문_활성(self):
        assert "실주문: 활성" not in self._source()


# ═══ 민감정보 ═══

class TestNoSensitive:
    def _source(self) -> str:
        return (ROOT / "core" / "toss_paper_telegram.py").read_text(encoding="utf-8")

    def test_no_long_numbers(self):
        nums = re.findall(r"\b\d{8,}\b", self._source())
        assert nums == []

    def test_no_tokens(self):
        src = self._source()
        assert "TOSS_APP_SECRET" not in src
        assert "TOSS_APP_KEY" not in src
        assert "Bearer " not in src


# ═══ write routes ═══

class TestNoWriteRoutes:
    def test_no_post_put_delete(self):
        src = (ROOT / "web" / "app.py").read_text()
        for v in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            assert v not in src
