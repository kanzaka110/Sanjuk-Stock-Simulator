"""
Toss paper 주문표 미리보기 테스트

- 정상/차단 후보 렌더
- MU 보호
- 금지 문구 부재
- 민감정보 부재
- live_order_allowed=false 유지
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.toss_order_preview import (
    build_toss_paper_order_preview,
    generate_preview_id,
)
from core.toss_cross_check import cross_check_candidate


def _ctx(**overrides) -> dict:
    base = {
        "enabled": True,
        "cash_krw": 10_000_000,
        "cash_usd": 5.67,
        "market_value_krw": 0,
        "total_account_value_krw": 10_000_000,
        "holdings_count": 0,
        "holdings": [],
        "usdkrw": 1539.0,
        "automation": {
            "enabled": False, "mode": "paper", "dry_run": True,
            "live_orders_allowed": False, "kill_switch": True,
        },
        "data_quality": {
            "toss_available": True, "cash_available": True,
            "fx_available": True, "calendar_available": True,
            "stale": False, "warnings": [],
        },
    }
    base.update(overrides)
    return base


def _cand(symbol="005930.KS", side="buy", qty=2, price=72000, **kw) -> dict:
    c = {
        "symbol": symbol,
        "side": side,
        "quantity": qty,
        "limit_price": price,
        "estimated_amount_krw": qty * price,
        "confidence": kw.get("confidence", 0.8),
        "reason": kw.get("reason", ""),
        "quote_age_sec": kw.get("quote_age_sec", 10),
    }
    c.update(kw)
    return c


# ═══ preview_id ═══

class TestPreviewId:
    def test_format(self):
        pid = generate_preview_id()
        assert pid.startswith("tosspaper_")
        assert len(pid) > 20

    def test_no_sensitive_info(self):
        pid = generate_preview_id()
        assert "token" not in pid.lower()
        assert "secret" not in pid.lower()


# ═══ 정상 후보 렌더 ═══

_EMPTY_POLICY = {
    "mode": "paper_only", "live_order_allowed": False, "sample_status": "insufficient",
    "base_budget_krw": 100_000, "max_budget_krw": 300_000, "min_budget_krw": 0,
    "sizing_multiplier": 0.3, "evaluated_count": 0, "win_rate": 0.0, "avg_pnl_pct": 0.0,
    "consensus_anomaly_count": 0, "consensus_anomaly_symbols": [], "data_error_count": 0,
    "reason": "test", "blocks": [], "warnings": [],
    "_note": "Paper sizing/risk policy · 실제 주문 아님 · live_order_allowed=False",
}

import unittest.mock as _mock

import pytest


@pytest.fixture(autouse=True)
def _no_network_policy():
    # compute_toss_paper_policy → get_paper_performance_summary → 실가격 조회(KIS/yfinance)
    # 네트워크 행 방지: 전 테스트에서 policy를 빈 상태 mock으로 고정
    with _mock.patch(
        "core.toss_paper_policy.compute_toss_paper_policy",
        return_value=_EMPTY_POLICY,
    ):
        yield


class TestNormalCandidate:
    # policy는 빈 상태 mock — consensus_anomaly 없는 정상 후보 렌더 검증용 (autouse fixture 적용)

    def test_renders_symbol(self):
        ctx = _ctx()
        cands = [_cand()]
        ccs = [cross_check_candidate("005930.KS", "buy", 144000, ctx)]
        text = build_toss_paper_order_preview(cands, ctx, ccs)
        assert "005930.KS" in text

    def test_shows_paper_only(self):
        ctx = _ctx()
        cands = [_cand()]
        ccs = [cross_check_candidate("005930.KS", "buy", 144000, ctx)]
        text = build_toss_paper_order_preview(cands, ctx, ccs)
        assert "paper" in text

    def test_shows_live_disabled(self):
        ctx = _ctx()
        cands = [_cand()]
        ccs = [cross_check_candidate("005930.KS", "buy", 144000, ctx)]
        text = build_toss_paper_order_preview(cands, ctx, ccs)
        assert "비활성" in text

    def test_shows_not_real_order(self):
        ctx = _ctx()
        cands = [_cand()]
        ccs = [cross_check_candidate("005930.KS", "buy", 144000, ctx)]
        text = build_toss_paper_order_preview(cands, ctx, ccs)
        assert "실제 주문 아님" in text

    def test_shows_price_and_quantity(self):
        ctx = _ctx()
        cands = [_cand(price=72000, qty=3)]
        ccs = [cross_check_candidate("005930.KS", "buy", 216000, ctx)]
        text = build_toss_paper_order_preview(cands, ctx, ccs)
        assert "72,000" in text
        assert "3주" in text

    def test_paper_record_only(self):
        ctx = _ctx()
        cands = [_cand()]
        ccs = [cross_check_candidate("005930.KS", "buy", 144000, ctx)]
        text = build_toss_paper_order_preview(cands, ctx, ccs)
        assert "paper 기록만 가능" in text

    def test_shows_cash(self):
        ctx = _ctx()
        cands = [_cand()]
        ccs = [cross_check_candidate("005930.KS", "buy", 144000, ctx)]
        text = build_toss_paper_order_preview(cands, ctx, ccs)
        assert "10,000,000" in text


# ═══ 차단 후보 렌더 ═══

class TestBlockedCandidate:
    def test_shows_blocked(self):
        ctx = _ctx(cash_krw=100_000)
        cands = [_cand(price=200000, qty=1)]
        ccs = [cross_check_candidate("AAPL", "buy", 200000, ctx)]
        text = build_toss_paper_order_preview(cands, ctx, ccs)
        assert "차단" in text
        assert "cash_insufficient" in text

    def test_buffer_breach(self):
        ctx = _ctx(cash_krw=2_100_000)
        cands = [_cand(price=150000, qty=1)]
        ccs = [cross_check_candidate("AAPL", "buy", 150000, ctx)]
        text = build_toss_paper_order_preview(cands, ctx, ccs)
        assert "대기" in text

    def test_max_order_exceeded(self):
        ctx = _ctx()
        cands = [_cand(price=400000, qty=1)]
        ccs = [cross_check_candidate("AAPL", "buy", 400000, ctx)]
        text = build_toss_paper_order_preview(cands, ctx, ccs)
        assert "max_order_exceeded" in text


# ═══ MU 보호 ═══

class TestMuProtection:
    def test_mu_blocked(self):
        ctx = _ctx()
        cands = [_cand(symbol="MU")]
        ccs = [cross_check_candidate("MU", "buy", 144000, ctx)]
        text = build_toss_paper_order_preview(cands, ctx, ccs)
        assert "mu_protected" in text or "blacklisted" in text
        assert "차단" in text


# ═══ 빈 후보 ═══

class TestEmptyPreview:
    def test_no_candidates(self):
        ctx = _ctx()
        text = build_toss_paper_order_preview([], ctx, [])
        assert "후보 없음" in text
        assert "비활성" in text


# ═══ 금지 문구 부재 ═══

class TestForbiddenWording:
    FORBIDDEN = ["주문 실행", "매수하기", "매도하기", "자동매매 시작", "자동거래 시작"]

    def _render_sample(self) -> str:
        ctx = _ctx()
        cands = [_cand(), _cand(symbol="MU")]
        ccs = [
            cross_check_candidate("005930.KS", "buy", 144000, ctx),
            cross_check_candidate("MU", "buy", 144000, ctx),
        ]
        return build_toss_paper_order_preview(cands, ctx, ccs)

    def test_no_forbidden_in_normal(self):
        text = self._render_sample()
        for word in self.FORBIDDEN:
            assert word not in text, f"Forbidden '{word}' in preview"

    def test_no_forbidden_in_empty(self):
        text = build_toss_paper_order_preview([], _ctx(), [])
        for word in self.FORBIDDEN:
            assert word not in text


# ═══ 민감정보 부재 ═══

class TestNoSensitiveInfo:
    def _render_sample(self) -> str:
        ctx = _ctx()
        cands = [_cand()]
        ccs = [cross_check_candidate("005930.KS", "buy", 144000, ctx)]
        return build_toss_paper_order_preview(cands, ctx, ccs)

    def test_no_token(self):
        text = self._render_sample()
        assert "access_token" not in text
        assert "Bearer " not in text

    def test_no_secret(self):
        text = self._render_sample()
        assert "secret" not in text.lower()
        assert "Basic " not in text

    def test_no_long_numbers(self):
        """8자리 이상 연속 숫자가 없어야 (금액 제외)."""
        text = self._render_sample()
        # 금액 형식(콤마 포함)은 OK, 연속 숫자(계좌번호성)만 체크
        raw_nums = re.findall(r"\b\d{8,}\b", text)
        assert raw_nums == [], f"Long numbers found: {raw_nums}"


# ═══ write route 없음 (기존 확인) ═══

class TestNoWriteRoutes:
    def test_no_post_put_delete(self):
        source = (ROOT / "web" / "app.py").read_text()
        for verb in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            assert verb not in source, f"{verb} found in app.py"


# ═══ live_order_allowed 유지 ═══

class TestLiveAlwaysFalse:
    def test_cross_check_live_false(self):
        ctx = _ctx()
        cc = cross_check_candidate("005930.KS", "buy", 144000, ctx)
        assert cc["live_order_allowed"] is False

    def test_preview_mentions_disabled(self):
        ctx = _ctx()
        cands = [_cand()]
        ccs = [cross_check_candidate("005930.KS", "buy", 144000, ctx)]
        text = build_toss_paper_order_preview(cands, ctx, ccs)
        assert "비활성" in text

    def test_live_true_input_still_shows_disabled(self):
        """live_order_allowed=True가 들어와도 주문표는 '비활성'만 표시."""
        ctx = _ctx()
        ctx["automation"]["live_orders_allowed"] = True
        cands = [_cand()]
        # cross_check는 항상 live=False 반환하지만, context 자체가 True여도 렌더러는 무시
        ccs = [{"blocks": [], "warnings": [], "toss_readiness": "paper_only",
                "live_order_allowed": True, "score_adjustments": []}]
        text = build_toss_paper_order_preview(cands, ctx, ccs)
        assert "실주문: 활성" not in text
        assert "실주문: 비활성" in text

    def test_no_live_active_string_anywhere(self):
        """어떤 조합에서도 '실주문: 활성'이 나오지 않음."""
        ctx = _ctx()
        ctx["automation"]["live_orders_allowed"] = True
        cands = [_cand(), _cand(symbol="MU")]
        ccs = [
            {"blocks": [], "warnings": [], "toss_readiness": "live_ready",
             "live_order_allowed": True, "score_adjustments": []},
            {"blocks": ["blacklisted"], "warnings": [], "toss_readiness": "blocked",
             "live_order_allowed": True, "score_adjustments": []},
        ]
        text = build_toss_paper_order_preview(cands, ctx, ccs)
        assert "실주문: 활성" not in text
