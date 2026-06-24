"""
Toss paper ledger 테스트

- 승인/취소/만료 상태 관리
- dry_run=True, live_order_allowed=False 강제
- 차단 후보 승인 거절
- 중복 승인 방지
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

import core.toss_paper_ledger as ledger


@pytest.fixture(autouse=True)
def _use_temp_db(tmp_path):
    """테스트마다 임시 DB 사용."""
    db = tmp_path / "test_ledger.db"
    with patch.object(ledger, "_DB_PATH", db):
        yield db


def _ctx(**kw) -> dict:
    base = {"cash_krw": 10_000_000, "usdkrw": 1539.0}
    base.update(kw)
    return base


def _cands_and_ccs():
    """정상 1 + 차단 1 후보."""
    cands = [
        {"symbol": "005930.KS", "side": "buy", "quantity": 2, "limit_price": 72000,
         "estimated_amount_krw": 144000, "confidence": 0.8, "reason": "지지선 반등"},
        {"symbol": "MU", "side": "buy", "quantity": 5, "limit_price": 28000,
         "estimated_amount_krw": 140000, "confidence": 0.75, "reason": "HBM"},
    ]
    ccs = [
        {"blocks": [], "warnings": [], "toss_readiness": "paper_only",
         "live_order_allowed": False, "score_adjustments": []},
        {"blocks": ["blacklisted", "mu_protected"], "warnings": ["MU 보호"],
         "toss_readiness": "blocked", "live_order_allowed": False, "score_adjustments": []},
    ]
    return cands, ccs


# ═══ 생성 ═══

class TestCreatePreviewRecords:
    def test_creates_records(self):
        cands, ccs = _cands_and_ccs()
        records = ledger.create_paper_preview_records("prev_001", cands, ccs, _ctx())
        assert len(records) == 2

    def test_normal_is_previewed(self):
        cands, ccs = _cands_and_ccs()
        records = ledger.create_paper_preview_records("prev_002", cands, ccs, _ctx())
        assert records[0]["status"] == "previewed"

    def test_blocked_is_blocked(self):
        cands, ccs = _cands_and_ccs()
        records = ledger.create_paper_preview_records("prev_003", cands, ccs, _ctx())
        assert records[1]["status"] == "blocked"


# ═══ 승인 ═══

class TestApprove:
    def test_approve_normal(self):
        cands, ccs = _cands_and_ccs()
        ledger.create_paper_preview_records("prev_010", cands, ccs, _ctx())
        result = ledger.approve_paper_order("prev_010", "005930.KS")
        assert result["ok"] is True
        assert len(result["approved"]) == 1
        assert result["approved"][0]["status"] == "approved"
        assert result["approved"][0]["dry_run"] is True
        assert result["approved"][0]["live_order_allowed"] is False

    def test_approve_blocked_rejected(self):
        cands, ccs = _cands_and_ccs()
        ledger.create_paper_preview_records("prev_011", cands, ccs, _ctx())
        result = ledger.approve_paper_order("prev_011", "MU")
        assert result["ok"] is True
        assert len(result["approved"]) == 0
        assert len(result["rejected"]) == 1
        assert result["rejected"][0]["reason"] == "blocked"

    def test_duplicate_approve_rejected(self):
        cands, ccs = _cands_and_ccs()
        ledger.create_paper_preview_records("prev_012", cands, ccs, _ctx())
        ledger.approve_paper_order("prev_012", "005930.KS")
        result = ledger.approve_paper_order("prev_012", "005930.KS")
        assert len(result["approved"]) == 0
        assert len(result["rejected"]) == 1
        assert result["rejected"][0]["reason"] == "already_approved"

    def test_not_found(self):
        result = ledger.approve_paper_order("nonexistent")
        assert result["ok"] is False


# ═══ 취소 ═══

class TestCancel:
    def test_cancel_previewed(self):
        cands, ccs = _cands_and_ccs()
        ledger.create_paper_preview_records("prev_020", cands, ccs, _ctx())
        result = ledger.cancel_paper_order("prev_020", "005930.KS")
        assert result["ok"] is True
        assert result["cancelled_count"] == 1

    def test_cancel_then_approve_fails(self):
        cands, ccs = _cands_and_ccs()
        ledger.create_paper_preview_records("prev_021", cands, ccs, _ctx())
        ledger.cancel_paper_order("prev_021", "005930.KS")
        result = ledger.approve_paper_order("prev_021", "005930.KS")
        assert len(result["approved"]) == 0
        assert len(result["rejected"]) == 1


# ═══ 만료 ═══

class TestExpire:
    def test_expire_previewed(self):
        cands, ccs = _cands_and_ccs()
        ledger.create_paper_preview_records("prev_030", cands, ccs, _ctx())
        result = ledger.expire_paper_preview("prev_030")
        assert result["expired_count"] == 1  # 정상 후보만 (blocked 제외)

    def test_expired_cannot_approve(self):
        cands, ccs = _cands_and_ccs()
        ledger.create_paper_preview_records("prev_031", cands, ccs, _ctx())
        ledger.expire_paper_preview("prev_031")
        result = ledger.approve_paper_order("prev_031", "005930.KS")
        assert len(result["approved"]) == 0


# ═══ fail-closed ═══

class TestFailClosed:
    def test_live_true_stored_as_false(self):
        """live_order_allowed=True 입력이 와도 저장값은 0(False)."""
        cands = [{"symbol": "X", "side": "buy", "quantity": 1, "limit_price": 1000,
                   "estimated_amount_krw": 1000, "confidence": 0.9, "reason": ""}]
        ccs = [{"blocks": [], "warnings": [], "toss_readiness": "live_ready",
                "live_order_allowed": True, "score_adjustments": []}]
        ledger.create_paper_preview_records("prev_fc", cands, ccs, _ctx())
        orders = ledger.list_paper_orders()
        assert orders[0]["live_order_allowed"] == 0
        assert orders[0]["dry_run"] == 1

    def test_approved_still_false(self):
        cands = [{"symbol": "X", "side": "buy", "quantity": 1, "limit_price": 1000,
                   "estimated_amount_krw": 1000, "confidence": 0.9, "reason": ""}]
        ccs = [{"blocks": [], "warnings": [], "toss_readiness": "paper_only",
                "live_order_allowed": False, "score_adjustments": []}]
        ledger.create_paper_preview_records("prev_fc2", cands, ccs, _ctx())
        result = ledger.approve_paper_order("prev_fc2")
        for a in result["approved"]:
            assert a["live_order_allowed"] is False
            assert a["dry_run"] is True


# ═══ 조회/요약 ═══

class TestListAndSummary:
    def test_list_empty(self):
        assert ledger.list_paper_orders() == []

    def test_summary_empty(self):
        s = ledger.paper_ledger_summary()
        assert s["total"] == 0
        assert s["recent"] == []

    def test_summary_after_records(self):
        cands, ccs = _cands_and_ccs()
        ledger.create_paper_preview_records("prev_sum", cands, ccs, _ctx())
        s = ledger.paper_ledger_summary()
        assert s["total"] == 2
        assert "previewed" in s["counts"] or "blocked" in s["counts"]


# ═══ Telegram 응답 텍스트 ═══

class TestTelegramText:
    def test_approval_text(self):
        text = ledger.format_approval_response({
            "approved": [{"paper_id": "p1", "symbol": "005930.KS", "side": "buy",
                          "quantity": 2, "limit_price": 72000,
                          "estimated_amount_krw": 144000, "status": "approved",
                          "dry_run": True, "live_order_allowed": False}],
            "rejected": [],
        })
        assert "Paper 승인 완료" in text
        assert "실제 주문 아님" in text
        assert "비활성" in text
        assert "paper ledger only" in text

    def test_rejection_text(self):
        text = ledger.format_approval_response({
            "approved": [],
            "rejected": [{"paper_id": "p2", "reason": "blocked",
                          "blocks": ["mu_protected"]}],
        })
        assert "승인 거절" in text
        assert "mu_protected" in text
        assert "비활성" in text

    def test_cancel_text(self):
        text = ledger.format_cancel_response({"cancelled_count": 1})
        assert "취소 완료" in text
        assert "실제 주문 없음" in text

    def test_kr_ticker_shows_won_sign(self):
        """KR 종목 승인 응답 지정가에 ₩ 표시."""
        text = ledger.format_approval_response({
            "approved": [{"paper_id": "p1", "symbol": "069500.KS", "side": "buy",
                          "quantity": 10, "limit_price": 30000,
                          "estimated_amount_krw": 300000, "status": "approved",
                          "dry_run": True, "live_order_allowed": False,
                          "price_currency": "KRW", "usdkrw_at_creation": None}],
            "rejected": [],
        })
        assert "₩30,000" in text
        assert "$" not in text.split("지정가:")[1].split("\n")[0]

    def test_us_ticker_shows_dollar_sign(self):
        """US 종목 승인 응답 지정가에 $ 표시."""
        text = ledger.format_approval_response({
            "approved": [{"paper_id": "p2", "symbol": "NVDA", "side": "buy",
                          "quantity": 1, "limit_price": 200.56,
                          "estimated_amount_krw": 306857, "status": "approved",
                          "dry_run": True, "live_order_allowed": False,
                          "price_currency": "USD", "usdkrw_at_creation": 1530.0}],
            "rejected": [],
        })
        assert "$200.56" in text
        assert "₩200" not in text  # no ₩ prefix for USD price

    def test_us_ticker_shows_usdkrw(self):
        """US 종목 승인 응답에 환율 표시."""
        text = ledger.format_approval_response({
            "approved": [{"paper_id": "p3", "symbol": "NVDA", "side": "buy",
                          "quantity": 1, "limit_price": 200.56,
                          "estimated_amount_krw": 306857, "status": "approved",
                          "dry_run": True, "live_order_allowed": False,
                          "price_currency": "USD", "usdkrw_at_creation": 1530.0}],
            "rejected": [],
        })
        assert "1,530" in text

    def test_us_ticker_no_won_prefix_for_limit(self):
        """US 종목 지정가에 ₩135 같은 오표시 없음."""
        text = ledger.format_approval_response({
            "approved": [{"paper_id": "p4", "symbol": "NVDA", "side": "buy",
                          "quantity": 1, "limit_price": 135.0,
                          "estimated_amount_krw": 206550, "status": "approved",
                          "dry_run": True, "live_order_allowed": False,
                          "price_currency": "USD", "usdkrw_at_creation": 1530.0}],
            "rejected": [],
        })
        assert "지정가: ₩135" not in text
        assert "$135.00" in text

    def test_price_currency_stored_in_metadata_via_create(self):
        """create_paper_preview_records가 price_currency를 metadata에 저장."""
        import json as _json
        cand = {
            "symbol": "NVDA", "side": "buy", "quantity": 1,
            "limit_price": 200.56, "estimated_amount_krw": 306857,
            "confidence": 0.0, "reason": "[TEST]",
            "_price_currency": "USD",
        }
        cc = {"blocks": [], "warnings": [], "toss_readiness": "paper_only",
              "live_order_allowed": False}
        ctx = {"cash_krw": 5_000_000, "usdkrw": 1530.0}
        records = ledger.create_paper_preview_records("pid_meta_test", [cand], [cc], ctx)
        assert len(records) == 1
        # Verify metadata was persisted by reading it back
        conn = ledger._conn()
        row = conn.execute(
            "SELECT metadata FROM paper_ledger WHERE paper_id=?",
            (records[0]["paper_id"],)
        ).fetchone()
        conn.close()
        meta = _json.loads(row["metadata"])
        assert meta.get("price_currency") == "USD"

    def test_approve_returns_price_currency(self):
        """approve_paper_order 결과에 price_currency 포함."""
        # Create a USD record then approve it
        import json as _json
        cand = {
            "symbol": "NVDA", "side": "buy", "quantity": 1,
            "limit_price": 200.56, "estimated_amount_krw": 306857,
            "confidence": 0.0, "reason": "[TEST]",
            "_price_currency": "USD",
        }
        cc = {"blocks": [], "warnings": [], "toss_readiness": "paper_only",
              "live_order_allowed": False}
        ctx = {"cash_krw": 5_000_000, "usdkrw": 1530.0}
        ledger.create_paper_preview_records("pid_approve_cur", [cand], [cc], ctx)
        result = ledger.approve_paper_order("pid_approve_cur", symbol="NVDA")
        assert result["ok"] is True
        approved = result["approved"]
        assert len(approved) == 1
        assert approved[0]["price_currency"] == "USD"
        assert approved[0]["usdkrw_at_creation"] == 1530.0


# ═══ 금지 문구/함수명 ═══

class TestForbiddenWording:
    def _get_source(self) -> str:
        return (ROOT / "core" / "toss_paper_ledger.py").read_text(encoding="utf-8")

    def test_no_forbidden_cta(self):
        source = self._get_source()
        for word in ["주문 실행", "매수하기", "매도하기", "자동매매 시작", "자동거래 시작"]:
            assert word not in source, f"Forbidden CTA '{word}'"

    def test_no_live_order_functions(self):
        source = self._get_source()
        for fn in ["place_order", "submit_order", "execute_order"]:
            assert fn not in source, f"Forbidden function '{fn}'"

    def test_no_실주문_활성(self):
        source = self._get_source()
        assert "실주문: 활성" not in source


# ═══ 민감정보 ═══

class TestNoSensitiveData:
    def test_no_account_number(self):
        source = (ROOT / "core" / "toss_paper_ledger.py").read_text()
        long_nums = re.findall(r"\b\d{8,}\b", source)
        assert long_nums == []

    def test_no_token_secret(self):
        source = (ROOT / "core" / "toss_paper_ledger.py").read_text()
        assert "access_token" not in source
        assert "Bearer " not in source
        assert "TOSS_APP_SECRET" not in source


# ═══ write routes ═══

class TestNoWriteRoutes:
    def test_no_post_put_delete(self):
        source = (ROOT / "web" / "app.py").read_text()
        for verb in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            assert verb not in source
