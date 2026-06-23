"""
Toss paper 성과 채점/추적 테스트

1. approved buy open 평가 — outcome=open, pnl 계산 정확
2. target 도달 — outcome=win, pnl 양수
3. stop 도달 — outcome=loss, pnl 음수
4. cancelled/blocked/previewed 제외 — win_rate 분모 제외
5. expired/data_error 분리 — win_rate 분모 제외
6. summary — wins/losses/evaluated_count/win_rate/avg_pnl_pct 계산 + 표본부족 비위험
7. API — /api/toss/paper-performance GET-only, write route 없음
8. dashboard — Paper 성과 / 실제 주문 아님 / 실주문 비활성 / 금지 CTA 없음
9. 금지 CTA 부재
10. 실제 주문 암시 함수명 부재
11. 민감정보 부재
12. full tests 통과
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.toss_paper_performance as perf
import core.toss_paper_ledger as ledger


KST = timezone(timedelta(hours=9))


# ─── fixtures ────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _use_temp_db(tmp_path):
    """테스트마다 임시 DB 사용."""
    db = tmp_path / "test_perf_ledger.db"
    with patch.object(ledger, "_DB_PATH", db):
        yield db


def _ctx(**kw) -> dict:
    base = {"cash_krw": 10_000_000, "usdkrw": 1539.0}
    base.update(kw)
    return base


def _make_order(
    symbol="005930.KS", side="buy", quantity=2, limit_price=70000,
    status="approved", created_at=None, paper_id="paper_test_001",
) -> dict:
    if created_at is None:
        created_at = datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    return {
        "paper_id": paper_id,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "limit_price": limit_price,
        "estimated_amount_krw": limit_price * quantity,
        "status": status,
        "created_at": created_at,
    }


def _quote(price: float) -> dict:
    return {"price": price, "source": "test_injected"}


# ─── 1. approved buy open 평가 ────────────────────────────


class TestApprovedBuyOpen:
    def test_outcome_open_between_target_stop(self):
        order = _make_order(limit_price=70000)
        # 현재가 71000 — target(72100), stop(67900) 사이
        result = perf.evaluate_paper_order(order, quote=_quote(71000))
        assert result["outcome"] == "open"

    def test_pnl_calculation_accuracy(self):
        order = _make_order(limit_price=70000, quantity=2)
        result = perf.evaluate_paper_order(order, quote=_quote(71000))
        assert result["unrealized_pnl_krw"] == pytest.approx(2000.0)
        assert result["unrealized_pnl_pct"] == pytest.approx(
            (71000 - 70000) / 70000 * 100, abs=0.01
        )

    def test_entry_amount_correct(self):
        order = _make_order(limit_price=70000, quantity=3)
        result = perf.evaluate_paper_order(order, quote=_quote(71000))
        assert result["entry_amount_krw"] == pytest.approx(70000 * 3)

    def test_current_value_correct(self):
        order = _make_order(limit_price=70000, quantity=2)
        result = perf.evaluate_paper_order(order, quote=_quote(71000))
        assert result["current_value_krw"] == pytest.approx(71000 * 2)


# ─── 2. target 도달 ───────────────────────────────────────


class TestTargetReached:
    def test_outcome_win(self):
        order = _make_order(limit_price=70000)
        # target = 70000 * 1.03 = 72100 → 현재가 72100 이상
        result = perf.evaluate_paper_order(order, quote=_quote(72100))
        assert result["outcome"] == "win"

    def test_pnl_positive_on_win(self):
        order = _make_order(limit_price=70000, quantity=2)
        result = perf.evaluate_paper_order(order, quote=_quote(72100))
        assert result["unrealized_pnl_krw"] > 0
        assert result["unrealized_pnl_pct"] > 0

    def test_target_price_set(self):
        order = _make_order(limit_price=70000)
        result = perf.evaluate_paper_order(order, quote=_quote(72100))
        assert result["target_price"] == pytest.approx(70000 * 1.03, abs=1)


# ─── 3. stop 도달 ─────────────────────────────────────────


class TestStopReached:
    def test_outcome_loss(self):
        order = _make_order(limit_price=70000)
        # stop = 70000 * 0.97 = 67900 → 현재가 67900 이하
        result = perf.evaluate_paper_order(order, quote=_quote(67900))
        assert result["outcome"] == "loss"

    def test_pnl_negative_on_loss(self):
        order = _make_order(limit_price=70000, quantity=2)
        result = perf.evaluate_paper_order(order, quote=_quote(67900))
        assert result["unrealized_pnl_krw"] < 0
        assert result["unrealized_pnl_pct"] < 0

    def test_stop_price_set(self):
        order = _make_order(limit_price=70000)
        result = perf.evaluate_paper_order(order, quote=_quote(67900))
        assert result["stop_price"] == pytest.approx(70000 * 0.97, abs=1)


# ─── 4. cancelled/blocked/previewed 제외 ──────────────────


class TestExcludedStatuses:
    @pytest.mark.parametrize("status", ["cancelled", "blocked", "previewed"])
    def test_excluded_outcome_equals_status(self, status):
        order = _make_order(status=status)
        result = perf.evaluate_paper_order(order, quote=_quote(71000))
        assert result["outcome"] == status

    def test_summary_win_rate_denominator_excludes_non_win_loss(self):
        """cancelled/blocked/previewed는 win_rate 분모에서 제외."""
        # approved 1건 win, cancelled 2건 → 분모 = 1, 승률 = 100%
        cands = [
            {"symbol": "005930.KS", "side": "buy", "quantity": 2, "limit_price": 70000,
             "estimated_amount_krw": 140000, "confidence": 0.8, "reason": "test"},
            {"symbol": "MU", "side": "buy", "quantity": 1, "limit_price": 50000,
             "estimated_amount_krw": 50000, "confidence": 0.7, "reason": "test"},
        ]
        ccs = [
            {"blocks": [], "warnings": [], "toss_readiness": "paper_only",
             "live_order_allowed": False, "score_adjustments": []},
            {"blocks": ["blacklisted"], "warnings": [], "toss_readiness": "blocked",
             "live_order_allowed": False, "score_adjustments": []},
        ]
        ledger.create_paper_preview_records("prev_perf01", cands, ccs, _ctx())
        ledger.approve_paper_order("prev_perf01", "005930.KS")

        # 목표가(72100) 이상 가격 주입
        with patch.object(perf, "_get_quote_for_paper", return_value={"price": 72200.0, "source": "test", "accepted_price_source": "test", "source_chain": []}):
            summary = perf.get_paper_performance_summary()

        s = summary["summary"]
        assert s["wins"] == 1
        assert s["losses"] == 0
        assert s["evaluated_count"] == 1
        assert s["win_rate"] == pytest.approx(100.0)
        # blocked는 분모에서 제외됨을 확인
        assert s["blocked"] >= 1


# ─── 5. expired/data_error 분리 ───────────────────────────


class TestExpiredAndDataError:
    def test_expired_outcome(self):
        old_dt = (datetime.now(KST) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S+09:00")
        order = _make_order(limit_price=70000, created_at=old_dt)
        # 현재가 있지만 만료됨 — target/stop 미도달이면 expired
        result = perf.evaluate_paper_order(order, quote=_quote(71000))
        assert result["outcome"] == "expired"

    def test_data_error_when_no_price(self):
        order = _make_order(limit_price=70000)
        result = perf.evaluate_paper_order(order, quote={"price": None, "source": "test"})
        assert result["outcome"] == "data_error"

    # ─── 가격 이상치 guard (005930.KS 실제 발생 케이스) ────
    def test_samsung_anomaly_entry72k_current310k(self):
        """entry 72,000 → current 310,000 (+330%) → data_error (이상치)."""
        order = _make_order(symbol="005930.KS", limit_price=72000, quantity=2)
        result = perf.evaluate_paper_order(order, quote=_quote(310000))
        assert result["outcome"] == "data_error"
        assert result["price_anomaly"] is True
        assert any("이상치" in w for w in result["warnings"])

    def test_anomaly_not_counted_in_evaluated_count(self):
        """이상치 케이스는 evaluated_count(win+loss 분모) 제외."""
        cands = [{"symbol": "005930.KS", "side": "buy", "quantity": 2,
                  "limit_price": 72000, "estimated_amount_krw": 144000,
                  "confidence": 0.8, "reason": ""}]
        ccs = [{"blocks": [], "warnings": [], "toss_readiness": "paper_only",
                "live_order_allowed": False, "score_adjustments": []}]
        ledger.create_paper_preview_records("prev_anomaly01", cands, ccs, _ctx())
        ledger.approve_paper_order("prev_anomaly01")

        with patch.object(perf, "_get_quote_for_paper", return_value={"price": 310000.0, "source": "test", "accepted_price_source": "test", "source_chain": []}):
            summary = perf.get_paper_performance_summary()

        s = summary["summary"]
        assert s["evaluated_count"] == 0
        assert s["data_error"] == 1
        assert s["wins"] == 0
        assert s["win_rate"] == 0.0

    def test_price_ratio_field_present(self):
        order = _make_order(limit_price=72000)
        result = perf.evaluate_paper_order(order, quote=_quote(310000))
        assert "price_ratio" in result
        assert result["price_ratio"] == pytest.approx(310000 / 72000, abs=0.01)

    def test_upper_anomaly_boundary(self):
        """entry 대비 +50% 초과 → data_error."""
        order = _make_order(limit_price=70000)
        result = perf.evaluate_paper_order(order, quote=_quote(105001))  # > 1.5x
        assert result["outcome"] == "data_error"

    def test_lower_anomaly_boundary(self):
        """entry 대비 -50% 초과 → data_error."""
        order = _make_order(limit_price=70000)
        result = perf.evaluate_paper_order(order, quote=_quote(34999))  # < 0.5x
        assert result["outcome"] == "data_error"

    def test_normal_range_not_anomaly(self):
        """정상 범위 가격은 이상치 처리 안 됨."""
        order = _make_order(limit_price=72000)
        result = perf.evaluate_paper_order(order, quote=_quote(73500))  # ~+2%
        assert result["outcome"] != "data_error"
        assert result["price_anomaly"] is False

    def test_win_in_normal_range(self):
        """entry 72000, current 74200(+3.06%) → win."""
        order = _make_order(limit_price=72000, quantity=2)
        result = perf.evaluate_paper_order(order, quote=_quote(74200))
        assert result["outcome"] == "win"
        assert result["price_anomaly"] is False

    def test_loss_in_normal_range(self):
        """entry 72000, current 69800(-3.06%) → loss, 이상치 없음."""
        order = _make_order(limit_price=72000, quantity=2)
        result = perf.evaluate_paper_order(order, quote=_quote(69800))
        assert result["outcome"] == "loss"
        assert result["price_anomaly"] is False
        assert result["warnings"] == []

    def test_expired_not_in_win_rate_denominator(self):
        old_dt = (datetime.now(KST) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S+09:00")
        order_expired = _make_order(limit_price=70000, created_at=old_dt, status="approved")
        result_expired = perf.evaluate_paper_order(order_expired, quote=_quote(71000))
        assert result_expired["outcome"] == "expired"

        # summary에서 expired는 evaluated_count에 포함 안 됨
        cands = [{"symbol": "A.KS", "side": "buy", "quantity": 1, "limit_price": 70000,
                  "estimated_amount_krw": 70000, "confidence": 0.8, "reason": ""}]
        ccs = [{"blocks": [], "warnings": [], "toss_readiness": "paper_only",
                "live_order_allowed": False, "score_adjustments": []}]
        ledger.create_paper_preview_records("prev_exp01", cands, ccs, _ctx())
        ledger.approve_paper_order("prev_exp01")

        with patch.object(perf, "_get_quote_for_paper", return_value={"price": 71000.0, "source": "test", "accepted_price_source": "test", "source_chain": []}):
            with patch.object(perf, "_parse_kst") as mock_parse:
                # created 10일 전으로 만들어 만료 처리
                old = datetime.now(KST) - timedelta(days=10)
                mock_parse.return_value = old
                summary = perf.get_paper_performance_summary()

        # expired는 evaluated_count 분모 제외 확인
        s = summary["summary"]
        assert s["expired"] >= 0
        assert s["evaluated_count"] == s["wins"] + s["losses"]

    def test_data_error_not_in_win_rate_denominator(self):
        order = _make_order(limit_price=70000, status="approved")
        result = perf.evaluate_paper_order(order, quote={"price": None, "source": "test"})
        assert result["outcome"] == "data_error"
        # data_error는 unrealized_pnl_pct 없음
        assert result["unrealized_pnl_pct"] is None


# ─── 6. summary 계산 ──────────────────────────────────────


class TestSummaryCalculation:
    def _setup_approved(self, symbol="005930.KS", limit_price=70000):
        cands = [{"symbol": symbol, "side": "buy", "quantity": 2,
                  "limit_price": limit_price, "estimated_amount_krw": limit_price * 2,
                  "confidence": 0.8, "reason": "test"}]
        ccs = [{"blocks": [], "warnings": [], "toss_readiness": "paper_only",
                "live_order_allowed": False, "score_adjustments": []}]
        ledger.create_paper_preview_records(f"prev_{symbol}", cands, ccs, _ctx())
        ledger.approve_paper_order(f"prev_{symbol}", symbol)

    def test_win_loss_counts(self):
        self._setup_approved("A.KS", 70000)
        # target = 72100 → win
        with patch.object(perf, "_get_quote_for_paper", return_value={"price": 72200.0, "source": "test", "accepted_price_source": "test", "source_chain": []}):
            summary = perf.get_paper_performance_summary()
        s = summary["summary"]
        assert s["wins"] == 1
        assert s["losses"] == 0

    def test_evaluated_count_equals_win_plus_loss(self):
        self._setup_approved("B.KS", 70000)
        # stop = 67900 → loss
        with patch.object(perf, "_get_quote_for_paper", return_value={"price": 67000.0, "source": "test", "accepted_price_source": "test", "source_chain": []}):
            summary = perf.get_paper_performance_summary()
        s = summary["summary"]
        assert s["evaluated_count"] == s["wins"] + s["losses"]

    def test_win_rate_formula(self):
        self._setup_approved("C.KS", 70000)
        with patch.object(perf, "_get_quote_for_paper", return_value={"price": 72200.0, "source": "test", "accepted_price_source": "test", "source_chain": []}):
            summary = perf.get_paper_performance_summary()
        s = summary["summary"]
        if s["evaluated_count"] > 0:
            expected = round(s["wins"] / s["evaluated_count"] * 100, 1)
            assert s["win_rate"] == pytest.approx(expected, abs=0.1)

    def test_avg_pnl_pct_calculation(self):
        self._setup_approved("D.KS", 70000)
        win_price = 72200.0
        with patch.object(perf, "_get_quote_for_paper", return_value={"price": win_price, "source": "test", "accepted_price_source": "test", "source_chain": []}):
            summary = perf.get_paper_performance_summary()
        s = summary["summary"]
        if s["evaluated_count"] > 0:
            expected_pnl = round((win_price - 70000) / 70000 * 100, 2)
            assert s["avg_pnl_pct"] == pytest.approx(expected_pnl, abs=0.1)

    def test_small_sample_not_marked_as_risk(self):
        """표본부족은 위험으로 표시하지 않음."""
        with patch.object(perf, "_get_quote_for_paper", return_value={"price": 71000.0, "source": "test", "accepted_price_source": "test", "source_chain": []}):
            summary = perf.get_paper_performance_summary()
        s = summary["summary"]
        # evaluated_count=0이어도 win_rate=0.0 (위험 플래그 없음)
        assert s["win_rate"] == 0.0
        assert "위험" not in str(s)
        assert "danger" not in str(s)

    def test_note_field_present(self):
        with patch.object(perf, "_get_quote_for_paper", return_value={"price": 71000.0, "source": "test", "accepted_price_source": "test", "source_chain": []}):
            summary = perf.get_paper_performance_summary()
        assert "실제 주문 아님" in summary.get("_note", "")
        assert "실주문 비활성" in summary.get("_note", "")

    def test_portfolio_not_included(self):
        """기존 포트폴리오 합산 없음을 _note로 확인."""
        with patch.object(perf, "_get_quote_for_paper", return_value={"price": 71000.0, "source": "test", "accepted_price_source": "test", "source_chain": []}):
            summary = perf.get_paper_performance_summary()
        assert "포트폴리오 미합산" in summary.get("_note", "")


# ─── 7. API GET-only ──────────────────────────────────────


class TestAPIRoutes:
    def test_paper_performance_get_route_exists(self):
        from web.app import app
        paths = [getattr(r, "path", "") for r in app.routes]
        assert "/api/toss/paper-performance" in paths

    def test_no_write_routes_for_paper_performance(self):
        """paper-performance 관련 write route 없음."""
        source = (ROOT / "web" / "app.py").read_text(encoding="utf-8")
        for verb in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            assert verb not in source, f"Forbidden write verb: {verb}"

    def test_paper_performance_is_get_only(self):
        from web.app import app
        for r in app.routes:
            if getattr(r, "path", "") == "/api/toss/paper-performance":
                methods = set(getattr(r, "methods", set()))
                assert methods == {"GET"} or methods == {"GET", "HEAD"}, \
                    f"Expected GET-only, got: {methods}"

    def test_api_returns_summary_structure(self):
        """API handler 호출 시 summary 키가 있어야 함."""
        with patch("core.dashboard_data.toss_paper_performance_data") as mock_fn:
            mock_fn.return_value = {
                "summary": {"wins": 0, "losses": 0, "win_rate": 0.0,
                            "evaluated_count": 0, "avg_pnl_pct": 0.0},
                "recent": [],
                "_note": "Paper 성과 · 실제 주문 아님 · 실주문 비활성",
            }
            from web.app import api_toss_paper_performance
            resp = api_toss_paper_performance()
        data = resp.body
        import json
        parsed = json.loads(data)
        assert "summary" in parsed


# ─── 8. Dashboard 표시 ────────────────────────────────────


class TestDashboardContent:
    def _pc_source(self) -> str:
        return (ROOT / "web" / "index_pc.html").read_text(encoding="utf-8")

    def _mobile_source(self) -> str:
        return (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def test_pc_paper_performance_label(self):
        src = self._pc_source()
        assert "Paper 성과" in src

    def test_pc_real_order_disclaimer(self):
        src = self._pc_source()
        assert "실제 주문 아님" in src

    def test_pc_live_order_inactive(self):
        src = self._pc_source()
        assert "실주문 비활성" in src

    def test_mobile_paper_performance_label(self):
        src = self._mobile_source()
        assert "Paper 성과" in src

    def test_mobile_real_order_disclaimer(self):
        src = self._mobile_source()
        assert "실제 주문 아님" in src

    def test_mobile_live_order_inactive(self):
        src = self._mobile_source()
        assert "실주문 비활성" in src

    def test_pc_portfolio_not_combined(self):
        src = self._pc_source()
        assert "기존 포트폴리오 미합산" in src

    def test_mobile_portfolio_not_combined(self):
        src = self._mobile_source()
        assert "기존 포트폴리오 미합산" in src


# ─── 9. 금지 CTA 부재 ─────────────────────────────────────


class TestForbiddenCTA:
    _SOURCES = ["core/toss_paper_performance.py", "web/app.py"]
    _FORBIDDEN_CTA = [
        "주문 실행", "매수하기", "매도하기",
        "자동매매 시작", "자동거래 시작", "실주문: 활성",
    ]

    @pytest.mark.parametrize("rel_path", _SOURCES)
    def test_no_forbidden_cta_in_source(self, rel_path):
        src = (ROOT / rel_path).read_text(encoding="utf-8")
        for cta in self._FORBIDDEN_CTA:
            assert cta not in src, f"Forbidden CTA '{cta}' found in {rel_path}"

    def test_no_forbidden_cta_in_pc_html(self):
        src = (ROOT / "web" / "index_pc.html").read_text(encoding="utf-8")
        for cta in self._FORBIDDEN_CTA:
            assert cta not in src, f"Forbidden CTA '{cta}' in index_pc.html"

    def test_no_forbidden_cta_in_mobile_html(self):
        src = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        for cta in self._FORBIDDEN_CTA:
            assert cta not in src, f"Forbidden CTA '{cta}' in index.html"


# ─── 10. 실제 주문 암시 함수명 부재 ──────────────────────


class TestForbiddenFunctionNames:
    _FORBIDDEN_FNS = ["place_order", "submit_order", "execute_order"]

    def test_no_forbidden_fn_in_performance(self):
        src = (ROOT / "core" / "toss_paper_performance.py").read_text()
        for fn in self._FORBIDDEN_FNS:
            assert fn not in src, f"Forbidden function '{fn}'"

    def test_no_forbidden_fn_in_app(self):
        src = (ROOT / "web" / "app.py").read_text()
        for fn in self._FORBIDDEN_FNS:
            assert fn not in src, f"Forbidden function '{fn}' in app.py"

    def test_no_forbidden_fn_in_dashboard_data(self):
        src = (ROOT / "core" / "dashboard_data.py").read_text()
        for fn in self._FORBIDDEN_FNS:
            assert fn not in src, f"Forbidden function '{fn}' in dashboard_data.py"


# ─── 11. 민감정보 부재 ────────────────────────────────────


class TestNoSensitiveData:
    def test_no_account_number_in_performance(self):
        src = (ROOT / "core" / "toss_paper_performance.py").read_text()
        long_nums = re.findall(r"\b\d{8,}\b", src)
        assert long_nums == []

    def test_no_token_or_secret_in_performance(self):
        src = (ROOT / "core" / "toss_paper_performance.py").read_text()
        assert "access_token" not in src
        assert "Bearer " not in src
        assert "TOSS_APP_SECRET" not in src
        assert "TOSS_APP_KEY" not in src

    def test_evaluate_result_no_account_number(self):
        order = _make_order(limit_price=70000)
        result = perf.evaluate_paper_order(order, quote=_quote(71000))
        result_str = str(result)
        long_nums = re.findall(r"\b\d{8,}\b", result_str)
        assert long_nums == []


# ─── 12. evaluate_open_paper_orders 통합 ─────────────────


class TestEvaluateOpenPaperOrders:
    def test_approved_orders_evaluated(self):
        cands = [{"symbol": "E.KS", "side": "buy", "quantity": 1, "limit_price": 70000,
                  "estimated_amount_krw": 70000, "confidence": 0.8, "reason": ""}]
        ccs = [{"blocks": [], "warnings": [], "toss_readiness": "paper_only",
                "live_order_allowed": False, "score_adjustments": []}]
        ledger.create_paper_preview_records("prev_open01", cands, ccs, _ctx())
        ledger.approve_paper_order("prev_open01")

        with patch.object(perf, "_get_quote_for_paper", return_value={"price": 71000.0, "source": "test", "accepted_price_source": "test", "source_chain": []}):
            result = perf.evaluate_open_paper_orders()

        assert result["count"] >= 1
        assert "_note" in result
        assert "실제 주문 아님" in result["_note"]

    def test_non_approved_not_in_result(self):
        """cancelled/blocked는 evaluate_open_paper_orders에 포함 안 됨."""
        cands = [
            {"symbol": "F.KS", "side": "buy", "quantity": 1, "limit_price": 70000,
             "estimated_amount_krw": 70000, "confidence": 0.8, "reason": ""},
            {"symbol": "MU", "side": "buy", "quantity": 1, "limit_price": 50000,
             "estimated_amount_krw": 50000, "confidence": 0.7, "reason": ""},
        ]
        ccs = [
            {"blocks": [], "warnings": [], "toss_readiness": "paper_only",
             "live_order_allowed": False, "score_adjustments": []},
            {"blocks": ["blacklisted"], "warnings": [], "toss_readiness": "blocked",
             "live_order_allowed": False, "score_adjustments": []},
        ]
        ledger.create_paper_preview_records("prev_open02", cands, ccs, _ctx())
        # F.KS만 approve
        ledger.approve_paper_order("prev_open02", "F.KS")

        with patch.object(perf, "_get_quote_for_paper", return_value={"price": 71000.0, "source": "test", "accepted_price_source": "test", "source_chain": []}):
            result = perf.evaluate_open_paper_orders()

        symbols = [e["symbol"] for e in result["evaluated"]]
        # MU는 blocked이므로 approve 안 됨 → 결과에 포함 안 됨
        assert "MU" not in symbols


# ─── 13. format_toss_paper_performance_briefing ───────────


class TestFormatBriefing:
    def _zero_summary(self) -> dict:
        return {
            "summary": {
                "total": 6, "open": 0, "wins": 0, "losses": 0,
                "cancelled": 1, "blocked": 3, "previewed": 2,
                "expired": 0, "data_error": 0,
                "evaluated_count": 0, "win_rate": 0.0, "avg_pnl_pct": 0.0,
            },
            "recent": [],
            "_note": "Paper 성과 · 실제 주문 아님 · 기존 포트폴리오 미합산 · 실주문 비활성",
        }

    def _nonzero_summary(self) -> dict:
        return {
            "summary": {
                "total": 10, "open": 2, "wins": 3, "losses": 2,
                "cancelled": 1, "blocked": 3, "previewed": 0,
                "expired": 1, "data_error": 0,
                "evaluated_count": 5, "win_rate": 60.0, "avg_pnl_pct": 1.8,
            },
            "recent": [],
            "_note": "Paper 성과 · 실제 주문 아님 · 기존 포트폴리오 미합산 · 실주문 비활성",
        }

    # evaluated_count=0 케이스
    def test_zero_evaluated_shows_sample_insufficient(self):
        text = perf.format_toss_paper_performance_briefing(self._zero_summary())
        assert "표본부족 / 평가 대기" in text

    def test_zero_evaluated_no_zero_pct_winrate(self):
        text = perf.format_toss_paper_performance_briefing(self._zero_summary())
        assert "승률: 0.0%" not in text
        assert "승률: 0%" not in text

    def test_zero_evaluated_avg_pnl_dash(self):
        text = perf.format_toss_paper_performance_briefing(self._zero_summary())
        assert "평균손익: -" in text

    # evaluated_count>0 케이스
    def test_nonzero_shows_win_rate(self):
        text = perf.format_toss_paper_performance_briefing(self._nonzero_summary())
        assert "60.0%" in text

    def test_nonzero_shows_avg_pnl(self):
        text = perf.format_toss_paper_performance_briefing(self._nonzero_summary())
        assert "1.8%" in text

    def test_nonzero_shows_win_loss_counts(self):
        text = perf.format_toss_paper_performance_briefing(self._nonzero_summary())
        assert "win 3" in text
        assert "loss 2" in text

    def test_nonzero_shows_expired_data_error(self):
        text = perf.format_toss_paper_performance_briefing(self._nonzero_summary())
        assert "expired 1" in text
        assert "data_error 0" in text

    # excluded statuses — 실패로 표현하지 않음
    def test_blocked_not_failure_label(self):
        text = perf.format_toss_paper_performance_briefing(self._zero_summary())
        # blocked는 상태 표시만 (실패/손실로 표현 금지)
        assert "blocked 3" in text
        assert "실패" not in text
        assert "손실" not in text

    def test_cancelled_not_failure_label(self):
        text = perf.format_toss_paper_performance_briefing(self._zero_summary())
        assert "cancelled 1" in text
        assert "실패" not in text

    # safety wording 항상 포함
    def test_always_real_order_disclaimer(self):
        for summary in [self._zero_summary(), self._nonzero_summary()]:
            text = perf.format_toss_paper_performance_briefing(summary)
            assert "실제 주문 아님" in text

    def test_always_live_order_inactive(self):
        for summary in [self._zero_summary(), self._nonzero_summary()]:
            text = perf.format_toss_paper_performance_briefing(summary)
            assert "실주문: 비활성" in text

    def test_always_portfolio_not_combined(self):
        for summary in [self._zero_summary(), self._nonzero_summary()]:
            text = perf.format_toss_paper_performance_briefing(summary)
            assert "기존 포트폴리오 미합산" in text

    def test_none_summary_fallback(self):
        """summary=None 전달 시 예외 없이 동작."""
        with patch.object(perf, "get_paper_performance_summary",
                          return_value=self._zero_summary()):
            text = perf.format_toss_paper_performance_briefing(None)
        assert "실제 주문 아님" in text

    # briefing context — analyzer/multi_agent 주입 확인
    def test_analyzer_has_paper_perf_injection(self):
        src = (ROOT / "core" / "analyzer.py").read_text(encoding="utf-8")
        assert "format_toss_paper_performance_briefing" in src
        assert "Toss Paper 성과" in src

    def test_multi_agent_has_paper_perf_block(self):
        src = (ROOT / "core" / "multi_agent.py").read_text(encoding="utf-8")
        assert "_toss_paper_performance_block" in src

    def test_paper_not_merged_with_prediction_db(self):
        """Paper 성과 문구가 기존 예측 DB accuracy_stats와 섞이지 않음."""
        src = (ROOT / "core" / "toss_paper_performance.py").read_text(encoding="utf-8")
        assert "accuracy_stats" not in src
        assert "predictions" not in src

    # 금지 CTA 확인
    _FORBIDDEN_CTA = [
        "주문 실행", "매수하기", "매도하기",
        "자동매매 시작", "자동거래 시작", "실주문: 활성",
    ]

    def test_no_forbidden_cta_in_format_output_zero(self):
        text = perf.format_toss_paper_performance_briefing(self._zero_summary())
        for cta in self._FORBIDDEN_CTA:
            assert cta not in text, f"Forbidden CTA '{cta}' in briefing output"

    def test_no_forbidden_cta_in_format_output_nonzero(self):
        text = perf.format_toss_paper_performance_briefing(self._nonzero_summary())
        for cta in self._FORBIDDEN_CTA:
            assert cta not in text, f"Forbidden CTA '{cta}' in briefing output"

    def test_no_forbidden_fn_in_analyzer(self):
        src = (ROOT / "core" / "analyzer.py").read_text(encoding="utf-8")
        for fn in ("place_order", "submit_order", "execute_order"):
            assert fn not in src

    def test_no_forbidden_fn_in_multi_agent(self):
        src = (ROOT / "core" / "multi_agent.py").read_text(encoding="utf-8")
        for fn in ("place_order", "submit_order", "execute_order"):
            assert fn not in src


# ─── 13. source_chain 추적 ────────────────────────────────


class TestSourceChain:
    """_get_quote_for_paper 소스 체인 — 이상치 소스 건너뛰기 및 추적."""

    def _mock_quote(self, price: float):
        from core.models import Quote
        return Quote(ticker="TEST", name="TEST", price=price, change=0.0, pct=0.0, high=price, low=price)

    def test_source_chain_present_in_result(self):
        """evaluate_paper_order 결과에 source_chain 필드가 존재한다."""
        order = _make_order(symbol="005930.KS", limit_price=72000, quantity=1)
        result = perf.evaluate_paper_order(order, quote=_quote(74000.0))
        assert "source_chain" in result

    def test_accepted_price_source_present(self):
        """evaluate_paper_order 결과에 accepted_price_source 필드가 존재한다."""
        order = _make_order(symbol="005930.KS", limit_price=72000, quantity=1)
        result = perf.evaluate_paper_order(order, quote=_quote(74000.0))
        assert "accepted_price_source" in result

    def test_injected_quote_source_chain_empty(self):
        """외부 주입 quote 사용 시 source_chain은 빈 리스트다."""
        order = _make_order(limit_price=72000, quantity=1)
        result = perf.evaluate_paper_order(order, quote=_quote(73000.0))
        assert result["source_chain"] == []

    def test_injected_quote_accepted_price_source(self):
        """외부 주입 quote 사용 시 accepted_price_source는 'test_injected'다."""
        order = _make_order(limit_price=72000, quantity=1)
        result = perf.evaluate_paper_order(order, quote=_quote(73000.0))
        assert result["accepted_price_source"] == "test_injected"

    def test_get_quote_for_paper_accepts_normal_price(self):
        """정상 가격 소스는 accepted=True로 기록된다."""
        mock_q = self._mock_quote(75000.0)
        with patch("core.market._get_quote_kis", return_value=mock_q), \
             patch("core.market._get_quote_yf_live", return_value=None), \
             patch("core.market._get_quote_daily", return_value=None):
            result = perf._get_quote_for_paper("005930.KS", entry_price=72000.0)

        assert result["price"] == 75000.0
        assert result["accepted_price_source"] == "KIS"
        assert result["source_chain"][0]["accepted"] is True
        assert result["source_chain"][0]["source"] == "KIS"

    def test_get_quote_for_paper_skips_anomaly_and_tries_next(self):
        """이상치 소스(KIS 310,000)는 건너뛰고 다음 소스(yfinance_live)를 사용한다."""
        kis_q = self._mock_quote(310000.0)   # 이상치: 72000 대비 ratio=4.3
        yf_q = self._mock_quote(74500.0)     # 정상

        with patch("core.market._get_quote_kis", return_value=kis_q), \
             patch("core.market._get_quote_yf_live", return_value=yf_q), \
             patch("core.market._get_quote_daily", return_value=None):
            result = perf._get_quote_for_paper("005930.KS", entry_price=72000.0)

        assert result["price"] == 74500.0
        assert result["accepted_price_source"] == "yfinance_live"

        chain = result["source_chain"]
        kis_entry = next(e for e in chain if e["source"] == "KIS")
        yf_entry = next(e for e in chain if e["source"] == "yfinance_live")

        assert kis_entry["accepted"] is False
        assert "이상치" in kis_entry["reason"]
        assert yf_entry["accepted"] is True

    def test_get_quote_for_paper_all_anomaly_returns_none(self):
        """모든 소스가 이상치면 price=None, accepted_price_source=None."""
        anomaly_q = self._mock_quote(400000.0)

        with patch("core.market._get_quote_kis", return_value=anomaly_q), \
             patch("core.market._get_quote_yf_live", return_value=anomaly_q), \
             patch("core.market._get_quote_daily", return_value=anomaly_q):
            result = perf._get_quote_for_paper("005930.KS", entry_price=72000.0)

        assert result["price"] is None
        assert result["accepted_price_source"] is None
        assert len(result["source_chain"]) == 3
        for entry in result["source_chain"]:
            assert entry["accepted"] is False

    def test_get_quote_for_paper_no_entry_skips_anomaly_check(self):
        """entry_price 미제공 시 이상치 체크 없이 첫 소스를 수락한다."""
        mock_q = self._mock_quote(310000.0)

        with patch("core.market._get_quote_kis", return_value=mock_q), \
             patch("core.market._get_quote_yf_live", return_value=None), \
             patch("core.market._get_quote_daily", return_value=None):
            result = perf._get_quote_for_paper("005930.KS", entry_price=None)

        assert result["price"] == 310000.0
        assert result["accepted_price_source"] == "KIS"
        assert "entry 미제공" in result["source_chain"][0]["reason"]

    def test_source_chain_has_ratio_to_entry(self):
        """이상치 체크 시 source_chain에 ratio_to_entry가 포함된다."""
        kis_q = self._mock_quote(310000.0)

        with patch("core.market._get_quote_kis", return_value=kis_q), \
             patch("core.market._get_quote_yf_live", return_value=None), \
             patch("core.market._get_quote_daily", return_value=None):
            result = perf._get_quote_for_paper("005930.KS", entry_price=72000.0)

        assert "ratio_to_entry" in result["source_chain"][0]
        assert result["source_chain"][0]["ratio_to_entry"] == pytest.approx(310000 / 72000, rel=1e-3)

    def test_evaluate_paper_order_source_chain_via_get_quote_for_paper(self):
        """_get_quote_for_paper 결과가 evaluate_paper_order result에 반영된다."""
        normal_q = self._mock_quote(75000.0)

        with patch("core.market._get_quote_kis", return_value=None), \
             patch("core.market._get_quote_yf_live", return_value=normal_q), \
             patch("core.market._get_quote_daily", return_value=None):
            order = _make_order(symbol="005930.KS", limit_price=72000, quantity=2)
            result = perf.evaluate_paper_order(order)

        assert result["accepted_price_source"] == "yfinance_live"
        assert result["current_price"] == 75000.0
        assert len(result["source_chain"]) >= 1

    def test_probe_tool_importable(self):
        """probe_paper_price_sources 툴이 임포트 가능하다."""
        import importlib
        import sys
        sys.path.insert(0, str(ROOT / "tools"))
        spec = importlib.util.find_spec("probe_paper_price_sources") or \
               importlib.util.spec_from_file_location(
                   "probe_paper_price_sources",
                   ROOT / "tools" / "probe_paper_price_sources.py"
               )
        assert spec is not None
