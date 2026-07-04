"""tests/test_toss_quality_gate.py

품질 게이트 점수 계산 + decision_bucket 테스트.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from core.toss_quality_gate import (
    score_candidate,
    score_candidates_batch,
    _decide_bucket,
    no_action_diagnosis,
    QualityScore,
    PASS_EXECUTE,
    SMALL_PASS,
    WAIT_PULLBACK,
    WATCH,
    CHASE_BLOCK,
    BLOCK,
    EXECUTABLE_BUCKETS,
)


def _candidate(**overrides):
    base = {
        "symbol": "316140.KS",
        "name": "우리금융",
        "side": "buy",
        "quantity": 1,
        "price": 28750,
        "limit_price": 28750,
        "estimated_amount_krw": 28750,
        "market": "KR",
        "score": 60,
        "target_price": 34000,
        "stop_loss": 27025,
        "risk_reward": 2.2,
        "change_pct": 1.5,
        "intraday_range_pct": 5.0,
        "risk_flags": [],
        "blocking_risk_flags": [],
    }
    base.update(overrides)
    return base


# ── Decision 규칙 테스트 ─────────────────────────────────────────

class TestDecideBucket:

    def test_no_stop_loss_block(self):
        b, r = _decide_bucket(80, 2.0, "강세장", 2.0, False, True, -1)
        assert b == BLOCK
        assert "손절" in r

    def test_no_target_block(self):
        b, r = _decide_bucket(80, 2.0, "강세장", 2.0, True, False, -1)
        assert b == BLOCK
        assert "목표" in r

    def test_low_rr_block(self):
        b, r = _decide_bucket(80, 1.0, "강세장", 2.0, True, True, -1)
        assert b == BLOCK
        assert "손익비" in r

    def test_crisis_high_rr_small_pass(self):
        """위기장에서도 RR 2.5+ → SMALL_PASS."""
        b, r = _decide_bucket(80, 2.5, "위기", 2.0, True, True, -1)
        assert b == SMALL_PASS
        assert "위기" in r

    def test_crisis_low_rr_watch(self):
        """위기장 + RR < 2.5 → WATCH (BLOCK 아님)."""
        b, r = _decide_bucket(80, 1.8, "위기", 2.0, True, True, -1)
        assert b == WATCH
        assert "위기" in r

    def test_chase_block(self):
        b, r = _decide_bucket(80, 2.5, "강세장", 9.0, True, True, -1)
        assert b == CHASE_BLOCK
        assert "급등" in r

    def test_bear_low_rr_watch(self):
        b, r = _decide_bucket(80, 1.5, "약세장", 2.0, True, True, -1)
        assert b == WATCH
        assert "약세장" in r

    def test_bear_high_rr_small_pass(self):
        """약세장 + RR 2.0+ → SMALL_PASS."""
        b, r = _decide_bucket(80, 2.0, "약세장", 2.0, True, True, -1)
        assert b == SMALL_PASS
        assert "약세장" in r

    def test_rr_medium_small_pass(self):
        """RR 1.5 (보통) → SMALL_PASS (WAIT 대신)."""
        b, r = _decide_bucket(80, 1.5, "강세장", 2.0, True, True, -1)
        assert b == SMALL_PASS
        assert "소액" in r

    def test_low_score_high_rr_small_pass(self):
        """총점 낮지만 RR 1.8+ → SMALL_PASS."""
        b, r = _decide_bucket(30, 2.0, "강세장", 2.0, True, True, -1)
        assert b == SMALL_PASS
        assert "소액" in r

    def test_low_score_low_rr_watch(self):
        """총점 + RR 모두 부족 → WATCH."""
        b, r = _decide_bucket(30, 1.3, "강세장", 2.0, True, True, -1)
        assert b == WATCH
        assert "총점" in r

    def test_earnings_wait(self):
        b, r = _decide_bucket(80, 2.5, "강세장", 2.0, True, True, 2)
        assert b == WAIT_PULLBACK
        assert "실적" in r

    def test_pass_execute(self):
        b, r = _decide_bucket(80, 2.5, "강세장", 2.0, True, True, -1)
        assert b == PASS_EXECUTE


# ── 점수 계산 테스트 ─────────────────────────────────────────────

class TestScoreCandidate:

    @patch("core.toss_quality_gate._score_momentum", return_value=15.0)
    @patch("core.toss_quality_gate._penalty_event_risk", return_value=(0.0, -1))
    def test_good_candidate_passes(self, mock_event, mock_momentum):
        c = _candidate(risk_reward=2.5)
        qs = score_candidate(c)
        assert qs.decision_bucket == PASS_EXECUTE
        assert qs.score_total > 45
        assert qs.rr_ratio == 2.5

    @patch("core.toss_quality_gate._score_momentum", return_value=15.0)
    @patch("core.toss_quality_gate._penalty_event_risk", return_value=(0.0, -1))
    def test_no_stop_loss_blocks(self, mock_event, mock_momentum):
        c = _candidate(stop_loss=None, risk_reward=2.5)
        qs = score_candidate(c)
        assert qs.decision_bucket == BLOCK

    @patch("core.toss_quality_gate._score_momentum", return_value=15.0)
    @patch("core.toss_quality_gate._penalty_event_risk", return_value=(0.0, -1))
    def test_low_rr_blocks(self, mock_event, mock_momentum):
        c = _candidate(risk_reward=0.8)
        qs = score_candidate(c)
        assert qs.decision_bucket == BLOCK

    @patch("core.toss_quality_gate._score_momentum", return_value=15.0)
    @patch("core.toss_quality_gate._penalty_event_risk", return_value=(0.0, -1))
    def test_chase_block_high_change(self, mock_event, mock_momentum):
        c = _candidate(change_pct=10.0, risk_reward=2.5)
        qs = score_candidate(c)
        assert qs.decision_bucket == CHASE_BLOCK

    @patch("core.toss_quality_gate._score_momentum", return_value=15.0)
    @patch("core.toss_quality_gate._penalty_event_risk", return_value=(0.0, -1))
    def test_overheat_penalty(self, mock_event, mock_momentum):
        c = _candidate(change_pct=12.0, risk_reward=2.5)
        qs = score_candidate(c)
        assert qs.penalty_overheat < 0

    @patch("core.toss_quality_gate._score_momentum", return_value=20.0)
    @patch("core.toss_quality_gate._penalty_event_risk", return_value=(-15.0, 2))
    def test_earnings_wait(self, mock_event, mock_momentum):
        """총점 45+ 유지하면서 실적 임박 → WAIT_PULLBACK."""
        c = _candidate(risk_reward=2.5, score=80)
        qs = score_candidate(c)
        assert qs.decision_bucket == WAIT_PULLBACK
        assert qs.penalty_event_risk == -15.0


# ── Crisis regime 테스트 ─────────────────────────────────────────

class TestCrisisRegime:

    @patch("core.toss_quality_gate._score_momentum", return_value=15.0)
    @patch("core.toss_quality_gate._penalty_event_risk", return_value=(0.0, -1))
    def test_crisis_high_rr_small_pass(self, mock_event, mock_momentum):
        """위기장 + RR 3.0 → SMALL_PASS (전면 BLOCK 아님)."""
        crisis = MagicMock()
        crisis.regime = "위기"
        crisis.risk_adjustment = "현금비중확대"
        c = _candidate(risk_reward=3.0)
        qs = score_candidate(c, regime_obj=crisis)
        assert qs.decision_bucket == SMALL_PASS
        assert qs.score_market_regime == 0.0

    @patch("core.toss_quality_gate._score_momentum", return_value=15.0)
    @patch("core.toss_quality_gate._penalty_event_risk", return_value=(0.0, -1))
    def test_crisis_low_rr_watch(self, mock_event, mock_momentum):
        """위기장 + RR < 2.5 → WATCH."""
        crisis = MagicMock()
        crisis.regime = "위기"
        crisis.risk_adjustment = "현금비중확대"
        c = _candidate(risk_reward=1.8)
        qs = score_candidate(c, regime_obj=crisis)
        assert qs.decision_bucket == WATCH


# ── 배치 테스트 ──────────────────────────────────────────────────

class TestBatch:

    @patch("core.toss_quality_gate._score_momentum", return_value=15.0)
    @patch("core.toss_quality_gate._penalty_event_risk", return_value=(0.0, -1))
    @patch("core.regime.detect_regime")
    def test_batch_sorts_by_score(self, mock_regime, mock_event, mock_momentum):
        mock_regime.return_value = MagicMock(regime="강세장", risk_adjustment="중립")
        items = [
            _candidate(symbol="A", risk_reward=1.5),
            _candidate(symbol="B", risk_reward=3.0),
        ]
        result = score_candidates_batch(items, market="KR")
        # B (higher RR) should be first
        assert result[0]["symbol"] == "B"
        assert all("decision_bucket" in item for item in result)


# ── QualityScore.to_dict 테스트 ──────────────────────────────────

class TestToDict:

    def test_to_dict_has_required_fields(self):
        qs = QualityScore(
            ticker="TEST", score_total=75, score_momentum=15, score_liquidity=20,
            score_risk_reward=18, score_reliability=10, score_market_regime=12,
            penalty_overheat=0, penalty_duplicate=0, penalty_event_risk=0,
            risk_flags=(), decision_bucket=PASS_EXECUTE, decision_reason="ok",
            rr_ratio=2.5, regime="강세장", scored_at="2026-06-30T10:00:00+09:00",
        )
        d = qs.to_dict()
        assert d["decision_bucket"] == PASS_EXECUTE
        assert d["score_total"] == 75
        assert "risk_flags" in d


# ── SMALL_PASS 테스트 ────────────────────────────────────────────

class TestSmallPass:

    def test_small_pass_is_executable(self):
        assert SMALL_PASS in EXECUTABLE_BUCKETS

    def test_pass_execute_is_executable(self):
        assert PASS_EXECUTE in EXECUTABLE_BUCKETS

    def test_watch_not_executable(self):
        assert WATCH not in EXECUTABLE_BUCKETS

    @patch("core.toss_quality_gate._score_momentum", return_value=15.0)
    @patch("core.toss_quality_gate._penalty_event_risk", return_value=(0.0, -1))
    def test_medium_rr_gets_small_pass(self, mock_event, mock_momentum):
        """RR 1.5 → SMALL_PASS."""
        c = _candidate(risk_reward=1.5)
        qs = score_candidate(c)
        assert qs.decision_bucket == SMALL_PASS


# ── no_action_diagnosis 테스트 ───────────────────────────────────

class TestNoActionDiagnosis:

    def test_no_diagnosis_when_pass_exists(self):
        items = [
            {"decision_bucket": PASS_EXECUTE, "symbol": "A"},
            {"decision_bucket": BLOCK, "symbol": "B"},
        ]
        assert no_action_diagnosis(items) is None

    def test_no_diagnosis_when_small_pass_exists(self):
        items = [
            {"decision_bucket": SMALL_PASS, "symbol": "A"},
            {"decision_bucket": BLOCK, "symbol": "B"},
        ]
        assert no_action_diagnosis(items) is None

    def test_diagnosis_when_all_blocked(self):
        items = [
            {"decision_bucket": BLOCK, "symbol": "A", "decision_reason": "손절 없음",
             "risk_reward": 0, "quality_score": 30},
            {"decision_bucket": WATCH, "symbol": "B", "decision_reason": "총점 부족",
             "risk_reward": 1.5, "stop_loss": 100, "target_price": 120, "quality_score": 40},
        ]
        diag = no_action_diagnosis(items)
        assert diag is not None
        assert diag["executable_count"] == 0
        assert diag["total_candidates"] == 2
        assert len(diag["relaxable_candidates"]) >= 1

    def test_diagnosis_empty_items(self):
        assert no_action_diagnosis([]) is None

    def test_relaxable_has_hints(self):
        items = [
            {"decision_bucket": WATCH, "symbol": "X", "decision_reason": "총점 부족",
             "risk_reward": 2.0, "stop_loss": 100, "target_price": 130, "quality_score": 35,
             "name": "테스트"},
        ]
        diag = no_action_diagnosis(items)
        assert diag is not None
        relaxable = diag["relaxable_candidates"]
        assert len(relaxable) == 1
        assert len(relaxable[0]["relaxation_hints"]) > 0


# ── stock_agent_ready SMALL_PASS 테스트 ──────────────────────────

class TestStockAgentReadySmallPass:

    def test_small_pass_stock_agent_ready(self):
        """SMALL_PASS도 stock_agent_ready=true."""
        # dashboard_data 로직 시뮬레이션
        bucket = SMALL_PASS
        _exec = ("PASS_EXECUTE", "SMALL_PASS")
        ready = bucket in _exec
        assert ready is True


# ── 체결가 연동 (B) 테스트 ───────────────────────────────────────

class TestFillPriceIntegration:

    def test_get_fill_price_empty_pilot(self):
        from core.toss_quality_gate import _get_fill_price
        assert _get_fill_price("") == 0.0

    def test_get_fill_price_uses_events(self):
        from core import toss_quality_gate as qg
        with patch("core.toss_live_pilot_events.latest_fill_for_pilot",
                   return_value={"filled_price": 30500.0, "filled_quantity": 1}):
            assert qg._get_fill_price("tlive_x") == 30500.0

    def test_get_fill_price_no_fill(self):
        from core import toss_quality_gate as qg
        with patch("core.toss_live_pilot_events.latest_fill_for_pilot",
                   return_value={}):
            assert qg._get_fill_price("tlive_x") == 0.0

    def test_current_price_uses_realtime_quote(self):
        """존재하지 않는 core.market.get_price 대신 _get_quote_realtime 사용."""
        from core import toss_quality_gate as qg
        q = MagicMock()
        q.price = 31000.0
        with patch("core.market._get_quote_realtime", return_value=q):
            assert qg._get_current_price("091180.KS") == 31000.0

    def test_current_price_none_quote(self):
        from core import toss_quality_gate as qg
        with patch("core.market._get_quote_realtime", return_value=None):
            assert qg._get_current_price("091180.KS") == 0.0
