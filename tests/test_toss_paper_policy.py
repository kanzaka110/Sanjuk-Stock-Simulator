"""
tests/test_toss_paper_policy.py

Toss Paper sizing/risk policy 테스트
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.toss_paper_policy as policy_mod
from core.toss_paper_policy import (
    compute_toss_paper_policy,
    apply_toss_paper_policy_to_candidate,
    get_policy_sizing_text,
)


# ─── helpers ─────────────────────────────────────────────


def _summary(
    evaluated_count=0, win_rate=0.0, avg_pnl_pct=0.0,
    consensus_anomaly=0, consensus_symbols=None, data_error=0
) -> dict:
    recent = []
    for sym in (consensus_symbols or []):
        recent.append({
            "symbol": sym,
            "outcome": "data_error",
            "error_type": "consensus_anomaly",
        })
    return {
        "summary": {
            "evaluated_count": evaluated_count,
            "win_rate": win_rate,
            "avg_pnl_pct": avg_pnl_pct,
            "consensus_anomaly": consensus_anomaly,
            "data_error": data_error,
            "open": 0, "wins": 0, "losses": 0,
            "cancelled": 0, "blocked": 0, "previewed": 0, "expired": 0,
        },
        "recent": recent,
    }


def _candidate(symbol="A.KS", side="buy", limit_price=70000, quantity=5) -> dict:
    return {
        "symbol": symbol,
        "side": side,
        "limit_price": limit_price,
        "quantity": quantity,
        "estimated_amount_krw": limit_price * quantity,
    }


# ─── 1. compute_toss_paper_policy ────────────────────────


class TestComputePolicy:
    def test_evaluated_count_zero_is_insufficient(self):
        p = compute_toss_paper_policy(_summary(evaluated_count=0))
        assert p["sample_status"] == "insufficient"

    def test_insufficient_max_budget_limit(self):
        p = compute_toss_paper_policy(_summary(evaluated_count=0))
        assert p["max_budget_krw"] <= 300_000

    def test_live_order_always_false(self):
        for ev in [0, 5, 10]:
            p = compute_toss_paper_policy(_summary(evaluated_count=ev, win_rate=80.0, avg_pnl_pct=5.0))
            assert p["live_order_allowed"] is False

    def test_mode_always_paper_only(self):
        p = compute_toss_paper_policy(_summary(evaluated_count=0))
        assert p["mode"] == "paper_only"

    def test_insufficient_sizing_multiplier(self):
        p = compute_toss_paper_policy(_summary(evaluated_count=3))
        assert p["sizing_multiplier"] <= 0.3

    def test_good_performance_relaxes_budget(self):
        p = compute_toss_paper_policy(_summary(evaluated_count=10, win_rate=70.0, avg_pnl_pct=3.0))
        assert p["sample_status"] == "good"
        assert p["max_budget_krw"] > 300_000

    def test_good_performance_live_still_false(self):
        p = compute_toss_paper_policy(_summary(evaluated_count=10, win_rate=70.0, avg_pnl_pct=3.0))
        assert p["live_order_allowed"] is False

    def test_poor_performance_tightens_budget(self):
        p = compute_toss_paper_policy(_summary(evaluated_count=10, win_rate=30.0, avg_pnl_pct=-2.0))
        assert p["sample_status"] == "poor"
        assert p["max_budget_krw"] <= 100_000

    def test_consensus_anomaly_count_in_policy(self):
        p = compute_toss_paper_policy(_summary(consensus_anomaly=1, consensus_symbols=["005930.KS"]))
        assert p["consensus_anomaly_count"] == 1
        assert "005930.KS" in p["consensus_anomaly_symbols"]

    def test_consensus_anomaly_warning_in_policy(self):
        p = compute_toss_paper_policy(_summary(consensus_anomaly=1, consensus_symbols=["005930.KS"]))
        combined = " ".join(p["warnings"])
        assert "기업행동" in combined or "entry_price" in combined or "재확인" in combined

    def test_note_field(self):
        p = compute_toss_paper_policy(_summary())
        assert "실제 주문 아님" in p["_note"]
        assert "live_order_allowed=False" in p["_note"]

    def test_reason_field_present(self):
        p = compute_toss_paper_policy(_summary(evaluated_count=0))
        assert p["reason"]
        assert "표본부족" in p["reason"]


# ─── 2. apply_toss_paper_policy_to_candidate ─────────────


class TestApplyPolicy:
    def _base_policy(self, **kw) -> dict:
        return compute_toss_paper_policy(_summary(**kw))

    def test_paper_policy_field_present(self):
        p = self._base_policy()
        result = apply_toss_paper_policy_to_candidate(_candidate(), p)
        assert "paper_policy" in result

    def test_normal_ticker_no_block(self):
        p = self._base_policy(evaluated_count=0)
        result = apply_toss_paper_policy_to_candidate(_candidate(symbol="B.KS"), p)
        assert "price_consensus_anomaly" not in result["paper_policy"]["blocks"]

    def test_normal_ticker_recommended_budget_positive(self):
        p = self._base_policy(evaluated_count=0)
        result = apply_toss_paper_policy_to_candidate(_candidate(symbol="B.KS"), p)
        assert result["paper_policy"]["recommended_budget_krw"] > 0

    def test_normal_ticker_max_budget_respected(self):
        p = self._base_policy(evaluated_count=0)
        result = apply_toss_paper_policy_to_candidate(_candidate(symbol="B.KS"), p)
        assert result["paper_policy"]["recommended_budget_krw"] <= 300_000

    def test_consensus_anomaly_ticker_blocked(self):
        p = self._base_policy(consensus_anomaly=1, consensus_symbols=["005930.KS"])
        result = apply_toss_paper_policy_to_candidate(_candidate(symbol="005930.KS"), p)
        assert "price_consensus_anomaly" in result["paper_policy"]["blocks"]

    def test_consensus_anomaly_ticker_zero_budget(self):
        p = self._base_policy(consensus_anomaly=1, consensus_symbols=["005930.KS"])
        result = apply_toss_paper_policy_to_candidate(_candidate(symbol="005930.KS"), p)
        assert result["paper_policy"]["recommended_budget_krw"] == 0

    def test_consensus_anomaly_ticker_zero_quantity(self):
        p = self._base_policy(consensus_anomaly=1, consensus_symbols=["005930.KS"])
        result = apply_toss_paper_policy_to_candidate(_candidate(symbol="005930.KS"), p)
        assert result["paper_policy"]["recommended_quantity"] == 0

    def test_consensus_anomaly_warning_in_candidate(self):
        p = self._base_policy(consensus_anomaly=1, consensus_symbols=["005930.KS"])
        result = apply_toss_paper_policy_to_candidate(_candidate(symbol="005930.KS"), p)
        combined = " ".join(result["paper_policy"]["warnings"])
        assert "기업행동" in combined or "재확인" in combined

    def test_normal_ticker_quantity_calculation(self):
        p = self._base_policy(evaluated_count=0)
        cand = _candidate(symbol="C.KS", limit_price=50000)
        result = apply_toss_paper_policy_to_candidate(cand, p)
        pp = result["paper_policy"]
        # 권장 수량 = floor(recommended_budget / limit_price)
        import math
        expected = math.floor(pp["recommended_budget_krw"] / 50000)
        assert pp["recommended_quantity"] == expected

    def test_candidate_fields_preserved(self):
        p = self._base_policy()
        cand = _candidate(symbol="D.KS", limit_price=70000, quantity=3)
        result = apply_toss_paper_policy_to_candidate(cand, p)
        assert result["symbol"] == "D.KS"
        assert result["limit_price"] == 70000
        assert result["quantity"] == 3

    def test_live_order_always_false_in_candidate_policy(self):
        p = self._base_policy(evaluated_count=10, win_rate=80.0, avg_pnl_pct=5.0)
        result = apply_toss_paper_policy_to_candidate(_candidate(), p)
        assert result["paper_policy"]["live_order_allowed"] is False


# ─── 3. get_policy_sizing_text ───────────────────────────


class TestPolicySizingText:
    def test_text_for_insufficient(self):
        p = compute_toss_paper_policy(_summary(evaluated_count=0))
        text = get_policy_sizing_text(p)
        assert "표본부족" in text or "insufficient" in text

    def test_text_includes_max_budget(self):
        p = compute_toss_paper_policy(_summary(evaluated_count=0))
        text = get_policy_sizing_text(p)
        assert "300" in text or "300,000" in text

    def test_consensus_anomaly_text_for_blocked(self):
        p = compute_toss_paper_policy(_summary(consensus_anomaly=1, consensus_symbols=["005930.KS"]))
        cand = _candidate(symbol="005930.KS")
        applied = apply_toss_paper_policy_to_candidate(cand, p)
        text = get_policy_sizing_text(p, applied)
        assert "consensus_anomaly" in text or "차단" in text or "재확인" in text

    def test_normal_candidate_shows_sizing(self):
        p = compute_toss_paper_policy(_summary(evaluated_count=0))
        cand = _candidate(symbol="E.KS", limit_price=50000)
        applied = apply_toss_paper_policy_to_candidate(cand, p)
        text = get_policy_sizing_text(p, applied)
        assert "수량" in text or "권장" in text


# ─── 4. API route ─────────────────────────────────────────


class TestPolicyAPI:
    def test_route_exists(self):
        from web.app import app
        paths = [getattr(r, "path", "") for r in app.routes]
        assert "/api/toss/paper-policy" in paths

    def test_route_is_get_only(self):
        from web.app import app
        for r in app.routes:
            if getattr(r, "path", "") == "/api/toss/paper-policy":
                methods = set(getattr(r, "methods", []))
                assert methods <= {"GET", "HEAD"}, f"write method in route: {methods}"

    def test_no_post_put_delete_patch_in_app(self):
        src = (ROOT / "web" / "app.py").read_text(encoding="utf-8")
        for decorator in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            assert decorator not in src


# ─── 5. 주문표 반영 ──────────────────────────────────────


class TestOrderPreviewPolicy:
    def _build_preview(self, cand_symbol="A.KS") -> str:
        from core.toss_order_preview import build_toss_paper_order_preview
        cands = [_candidate(symbol=cand_symbol, limit_price=50000, quantity=2)]
        ctx = {"cash_krw": 5_000_000, "usdkrw": 1400.0, "automation": {"mode": "paper", "dry_run": True}}
        ccs = [{"blocks": [], "warnings": [], "toss_readiness": "paper_only", "live_order_allowed": False}]
        # policy 조회는 mock해서 빠르게
        with patch("core.toss_paper_policy.compute_toss_paper_policy",
                   return_value=compute_toss_paper_policy(_summary(evaluated_count=0))):
            return build_toss_paper_order_preview(cands, ctx, ccs)

    def test_preview_contains_policy_text(self):
        text = self._build_preview()
        # 정책 관련 키워드가 있어야 함
        assert "sizing" in text.lower() or "표본" in text or "정책" in text or "paper" in text.lower()

    def test_preview_no_forbidden_cta(self):
        text = self._build_preview()
        for cta in ["주문 실행", "매수하기", "매도하기", "자동매매 시작", "자동거래 시작", "실주문: 활성"]:
            assert cta not in text

    def test_preview_live_order_inactive(self):
        text = self._build_preview()
        assert "실주문: 비활성" in text


# ─── 6. 가드레일 ──────────────────────────────────────────


class TestPolicyGuardrails:
    def test_no_order_functions(self):
        src = (ROOT / "core" / "toss_paper_policy.py").read_text(encoding="utf-8")
        for fn in ("place_order", "submit_order", "execute_order"):
            assert fn not in src

    def test_no_write_routes(self):
        src = (ROOT / "core" / "toss_paper_policy.py").read_text(encoding="utf-8")
        for r in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
            assert r not in src

    def test_no_sensitive_info(self):
        src = (ROOT / "core" / "toss_paper_policy.py").read_text(encoding="utf-8")
        for token in ("KIS_APP_SECRET", "KIS_APP_KEY", "TOSS_APP_SECRET", "accountNo"):
            assert token not in src

    def test_live_order_always_false_in_module(self):
        """모듈 어디서도 live_order_allowed=True를 반환하지 않는다."""
        src = (ROOT / "core" / "toss_paper_policy.py").read_text(encoding="utf-8")
        # live_order_allowed가 True로 set되는 코드 없음
        assert "live_order_allowed\": True" not in src
        assert "live_order_allowed: True" not in src

    def test_no_forbidden_cta_in_sizing_text(self):
        for ev in [0, 5, 10]:
            p = compute_toss_paper_policy(_summary(evaluated_count=ev, win_rate=60.0, avg_pnl_pct=2.0))
            text = get_policy_sizing_text(p)
            for cta in ["주문 실행", "매수하기", "매도하기", "자동매매 시작", "자동거래 시작", "실주문: 활성"]:
                assert cta not in text
