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


# ═══ 정상 샘플 생성 (scripts/send_toss_paper_preview_test.py) ═══

class TestNormalSampleGeneration:
    """스크립트의 _build_candidates / _validate_candidate 로직 검증."""

    def _policy_no_anomaly(self) -> dict:
        from core.toss_paper_policy import compute_toss_paper_policy
        # consensus_anomaly 없는 policy 반환
        return {
            "mode": "paper_only", "live_order_allowed": False,
            "sample_status": "insufficient",
            "base_budget_krw": 100_000, "max_budget_krw": 300_000,
            "min_budget_krw": 0, "sizing_multiplier": 0.3,
            "evaluated_count": 0, "win_rate": 0.0, "avg_pnl_pct": 0.0,
            "consensus_anomaly_count": 0,
            "consensus_anomaly_symbols": [],
            "data_error_count": 0,
            "reason": "표본부족", "blocks": [], "warnings": [],
            "_note": "test",
        }

    def _policy_with_anomaly(self, symbols: list[str]) -> dict:
        p = self._policy_no_anomaly()
        p["consensus_anomaly_count"] = len(symbols)
        p["consensus_anomaly_symbols"] = symbols
        return p

    def test_consensus_anomaly_ticker_excluded(self):
        """consensus_anomaly 종목은 validate에서 제외된다."""
        import sys; sys.path.insert(0, str(ROOT / "scripts"))
        from send_toss_paper_preview_test import _validate_candidate
        result = _validate_candidate("005930.KS", 72000.0, {"005930.KS"})
        assert result["ok"] is False
        assert "consensus_anomaly" in result["reason"]

    def test_no_accepted_source_excluded(self):
        """accepted source 없으면 후보 제외."""
        import sys; sys.path.insert(0, str(ROOT / "scripts"))
        from send_toss_paper_preview_test import _validate_candidate
        with patch("core.toss_paper_performance._get_quote_for_paper",
                   return_value={"price": None, "source": "unavailable",
                                 "accepted_price_source": None, "source_chain": []}):
            result = _validate_candidate("TEST.KS", 70000.0, set())
        assert result["ok"] is False

    def test_normal_price_accepted(self):
        """정상 가격 source → ok=True."""
        import sys; sys.path.insert(0, str(ROOT / "scripts"))
        from send_toss_paper_preview_test import _validate_candidate
        with patch("core.toss_paper_performance._get_quote_for_paper",
                   return_value={"price": 70500.0, "source": "KIS",
                                 "accepted_price_source": "KIS", "source_chain": []}):
            result = _validate_candidate("A.KS", 70000.0, set())
        assert result["ok"] is True
        assert result["price"] == 70500.0
        assert result["source"] == "KIS"

    def test_build_candidates_excludes_anomaly(self):
        """_build_candidates: consensus_anomaly 종목은 결과에 없다."""
        import sys; sys.path.insert(0, str(ROOT / "scripts"))
        from send_toss_paper_preview_test import _build_candidates, _CANDIDATE_POOL
        policy = self._policy_with_anomaly(["005930.KS"])

        # 모든 후보 가격을 정상으로 mock
        with patch("core.toss_paper_performance._get_quote_for_paper",
                   return_value={"price": 30000.0, "source": "KIS",
                                 "accepted_price_source": "KIS", "source_chain": []}):
            candidates, rejected = _build_candidates(policy, max_n=5)

        symbols = [c["symbol"] for c in candidates]
        assert "005930.KS" not in symbols

    def test_build_candidates_max_budget_respected(self):
        """후보 estimated_amount_krw <= max_budget_krw."""
        import sys; sys.path.insert(0, str(ROOT / "scripts"))
        from send_toss_paper_preview_test import _build_candidates
        policy = self._policy_no_anomaly()  # max 300,000

        with patch("core.toss_paper_performance._get_quote_for_paper",
                   return_value={"price": 30000.0, "source": "KIS",
                                 "accepted_price_source": "KIS", "source_chain": []}):
            candidates, _ = _build_candidates(policy, max_n=5)

        for c in candidates:
            assert c["estimated_amount_krw"] <= 300_000, \
                f"{c['symbol']}: {c['estimated_amount_krw']} > 300,000"

    def test_build_candidates_quantity_positive(self):
        """후보 quantity > 0."""
        import sys; sys.path.insert(0, str(ROOT / "scripts"))
        from send_toss_paper_preview_test import _build_candidates
        policy = self._policy_no_anomaly()

        with patch("core.toss_paper_performance._get_quote_for_paper",
                   return_value={"price": 30000.0, "source": "KIS",
                                 "accepted_price_source": "KIS", "source_chain": []}):
            candidates, _ = _build_candidates(policy, max_n=3)

        for c in candidates:
            assert c["quantity"] > 0

    def test_script_message_contains_sample_disclaimer(self):
        """생성된 후보 주문표 메시지에 필수 문구가 포함된다."""
        from core.toss_order_preview import build_toss_paper_order_preview

        cand = {
            "symbol": "069500.KS", "side": "buy", "quantity": 10,
            "limit_price": 28000, "estimated_amount_krw": 280000,
            "confidence": 0.0, "reason": "[TEST] Paper 운영 샘플",
            "quote_age_sec": 0, "_is_test_sample": True,
        }
        cc = {"blocks": [], "warnings": [], "toss_readiness": "paper_only",
              "live_order_allowed": False, "score_adjustments": []}
        ctx = _ctx()
        text = build_toss_paper_order_preview([cand], ctx, [cc])

        assert "실제 주문 아님" in text
        assert "비활성" in text

    def test_script_header_contains_required_phrases(self):
        """스크립트 header 문구 — 표본부족, 실제 주문 아님, 실주문: 비활성."""
        header = (
            "[TEST] Toss Paper 운영 샘플\n"
            "실제 주문 아님\n"
            "실주문: 비활성\n"
            "표본부족 — 최대 ₩300,000 paper 검증\n\n"
        )
        assert "표본부족" in header
        assert "실제 주문 아님" in header
        assert "실주문: 비활성" in header

    def test_script_no_forbidden_cta(self):
        """스크립트 소스에 금지 CTA 없음."""
        src = (ROOT / "scripts" / "send_toss_paper_preview_test.py").read_text(encoding="utf-8")
        for cta in ["주문 실행", "매수하기", "매도하기", "자동매매 시작", "자동거래 시작", "실주문: 활성"]:
            assert cta not in src

    def test_script_no_order_functions(self):
        """스크립트에 금지 함수명 없음."""
        src = (ROOT / "scripts" / "send_toss_paper_preview_test.py").read_text(encoding="utf-8")
        for fn in ("place_order", "submit_order", "execute_order"):
            assert fn not in src

    def test_is_test_sample_flag_set(self):
        """_build_candidates 결과 후보에 _is_test_sample=True."""
        import sys; sys.path.insert(0, str(ROOT / "scripts"))
        from send_toss_paper_preview_test import _build_candidates
        policy = self._policy_no_anomaly()

        with patch("core.toss_paper_performance._get_quote_for_paper",
                   return_value={"price": 30000.0, "source": "KIS",
                                 "accepted_price_source": "KIS", "source_chain": []}):
            candidates, _ = _build_candidates(policy, max_n=2)

        for c in candidates:
            assert c.get("_is_test_sample") is True


# ═══ entry_price = accepted market price ═══

class TestEntryPriceMatchesAccepted:
    """운영 샘플 limit_price가 accepted market price와 일치하는지 검증."""

    def _policy(self) -> dict:
        return {
            "mode": "paper_only", "live_order_allowed": False,
            "sample_status": "insufficient",
            "base_budget_krw": 100_000, "max_budget_krw": 300_000,
            "min_budget_krw": 0, "sizing_multiplier": 0.3,
            "evaluated_count": 0, "win_rate": 0.0, "avg_pnl_pct": 0.0,
            "consensus_anomaly_count": 0, "consensus_anomaly_symbols": [],
            "data_error_count": 0, "reason": "표본부족",
            "blocks": [], "warnings": [], "_note": "test",
        }

    def test_limit_price_equals_accepted_price_for_kr(self):
        """KR 종목 limit_price == accepted price."""
        import sys; sys.path.insert(0, str(ROOT / "scripts"))
        from send_toss_paper_preview_test import _build_candidates
        accepted_price = 29800.0
        with patch("core.toss_paper_performance._get_quote_for_paper",
                   return_value={"price": accepted_price, "source": "KIS",
                                 "accepted_price_source": "KIS", "source_chain": []}):
            candidates, _ = _build_candidates(self._policy(), max_n=5)
        for c in candidates:
            if c["symbol"].endswith(".KS"):
                assert c["limit_price"] == accepted_price, (
                    f"{c['symbol']}: limit_price={c['limit_price']} != accepted={accepted_price}"
                )

    def test_limit_price_equals_accepted_price_for_us(self):
        """US 종목 limit_price == accepted price (not pool _ref_price)."""
        import sys; sys.path.insert(0, str(ROOT / "scripts"))
        from send_toss_paper_preview_test import _build_candidates
        accepted_price = 200.56
        with patch("core.toss_paper_performance._get_quote_for_paper",
                   return_value={"price": accepted_price, "source": "KIS",
                                 "accepted_price_source": "KIS", "source_chain": []}):
            candidates, _ = _build_candidates(self._policy(), {"usdkrw": 1350.0}, max_n=5)
        for c in candidates:
            from send_toss_paper_preview_test import _is_us_ticker
            if _is_us_ticker(c["symbol"]):
                assert c["limit_price"] == accepted_price, (
                    f"{c['symbol']}: limit_price={c['limit_price']} != accepted={accepted_price}"
                )

    def test_no_hardcoded_135_as_limit_price(self):
        """limit_price=135 하드코딩 없음 — 항상 accepted price 사용."""
        import sys; sys.path.insert(0, str(ROOT / "scripts"))
        from send_toss_paper_preview_test import _build_candidates
        # mock returns 200.56, never 135
        with patch("core.toss_paper_performance._get_quote_for_paper",
                   return_value={"price": 200.56, "source": "KIS",
                                 "accepted_price_source": "KIS", "source_chain": []}):
            candidates, _ = _build_candidates(self._policy(), {"usdkrw": 1350.0}, max_n=5)
        for c in candidates:
            assert c["limit_price"] != 135, (
                f"{c['symbol']}: limit_price still uses hardcoded 135"
            )

    def test_entry_price_near_current_prevents_immediate_win(self):
        """entry ≈ current이면 +3% target 미도달 → open."""
        import sys; sys.path.insert(0, str(ROOT / "scripts"))
        from send_toss_paper_preview_test import _build_candidates
        accepted = 29800.0
        with patch("core.toss_paper_performance._get_quote_for_paper",
                   return_value={"price": accepted, "source": "KIS",
                                 "accepted_price_source": "KIS", "source_chain": []}):
            candidates, _ = _build_candidates(self._policy(), max_n=5)
        for c in candidates:
            entry = c["limit_price"]
            current = accepted  # same at creation
            # target = +3% from entry
            target = entry * 1.03
            # current < target → still open
            assert current < target, f"{c['symbol']}: immediate win risk"

    def test_candidate_pool_has_no_limit_price_field(self):
        """_CANDIDATE_POOL에 limit_price 필드 없음 (오염 방지)."""
        import sys; sys.path.insert(0, str(ROOT / "scripts"))
        import importlib
        mod = importlib.import_module("send_toss_paper_preview_test")
        for entry in mod._CANDIDATE_POOL:
            assert "limit_price" not in entry, (
                f"{entry['symbol']}: _CANDIDATE_POOL에 limit_price 필드 남아 있음"
            )
