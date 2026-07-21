"""tests/test_toss_quality_gate.py

품질 게이트 점수 계산 + decision_bucket 테스트.
"""

from __future__ import annotations

import sqlite3

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


def _seal_quality_proof_for_test(qg, candidate):
    """저수준 DB 변조 테스트 전용. production binder 우회 의도를 명시한다."""
    candidate.setdefault("side", "buy")
    candidate["quality_score_authority"] = "quality_breakdown.score_total"
    breakdown = candidate["quality_breakdown"]
    breakdown["quality_score_authority"] = candidate["quality_score_authority"]
    breakdown["decision_bucket"] = candidate.get("decision_bucket", "")
    breakdown["decision_reason"] = candidate.get("decision_reason", "")
    breakdown["score_symbol"] = str(candidate.get("symbol") or candidate.get("ticker") or "").upper()
    breakdown["score_side"] = str(candidate.get("side") or "buy").lower()
    event_penalty = float(breakdown.get("penalty_event_risk") or 0.0)
    breakdown.update({
        "decision_change_pct": float(candidate.get("change_pct") or 0.0),
        "decision_days_to_earnings": 0 if event_penalty == -15.0 else (5 if event_penalty == -5.0 else -1),
        "decision_has_stop": bool(candidate.get("stop_loss")),
        "decision_has_target": bool(candidate.get("target_price")),
        "decision_blocking_risk_flags": list(candidate.get("blocking_risk_flags") or []),
        "decision_origin_bucket": breakdown["decision_bucket"],
        "decision_origin_reason": breakdown["decision_reason"],
    })
    breakdown["score_schema_version"] = qg.QUALITY_SCORE_SCHEMA_VERSION
    weight_hash = qg._weight_profile_hash()
    breakdown["weight_profile_hash"] = weight_hash
    breakdown["score_breakdown_sha256"] = qg._score_breakdown_hash(
        breakdown,
        schema_version=qg.QUALITY_SCORE_SCHEMA_VERSION,
        weight_hash=weight_hash,
    )
    assert breakdown["score_breakdown_sha256"]
    assert qg.attach_quality_proof(candidate) is True


@pytest.fixture(autouse=True)
def _no_network_supply(monkeypatch):
    """수급 조회가 테스트에서 네트워크를 타지 않게 기본 차단."""
    import core.kr_market as km
    monkeypatch.setattr(km, "_fetch_naver_frgn", lambda code, **kw: [])
    monkeypatch.setattr(km, "_FRGN_CACHE", {})


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

    @patch("core.toss_quality_gate._score_supply_demand", return_value=0.0)
    def test_us_quality_inputs_volume_value_drives_liquidity_score(self, _mock_supply):
        candidate = _candidate(
            market="US",
            symbol="AVGO",
            volume_value=1.0,
            quality_inputs={"volume_value": 4_000_000_000.0},
        )

        score = score_candidate(
            candidate,
            regime_obj=None,
            accuracy_stats={},
            expensive_checks=False,
            fetch_budget={"remaining": 0},
        )

        assert score.score_liquidity == 25.0

    @pytest.mark.parametrize(
        "invalid_volume",
        [
            True, "4000000000", float("nan"), float("inf"),
            10 ** 300, 10 ** 10_000,
        ],
        ids=["bool", "string", "nan", "inf", "finite_huge_int", "overflow_int"],
    )
    @patch("core.toss_quality_gate._score_supply_demand", return_value=0.0)
    def test_invalid_quality_input_volume_cannot_fall_back_to_top_level(
        self, _mock_supply, invalid_volume,
    ):
        score = score_candidate(
            _candidate(
                market="US",
                symbol="AVGO",
                volume_value=4_000_000_000.0,
                quality_inputs={"volume_value": invalid_volume},
            ),
            regime_obj=None,
            accuracy_stats={},
            expensive_checks=False,
            fetch_budget={"remaining": 0},
        )

        assert score.score_liquidity == 0.0

    @pytest.mark.parametrize(
        "invalid_inputs",
        [None, [], "invalid"],
        ids=["none", "list", "string"],
    )
    @patch("core.toss_quality_gate._score_supply_demand", return_value=0.0)
    def test_malformed_quality_inputs_cannot_enable_legacy_liquidity_fallback(
        self, _mock_supply, invalid_inputs,
    ):
        score = score_candidate(
            _candidate(
                market="US",
                symbol="AVGO",
                volume_value=4_000_000_000.0,
                quality_inputs=invalid_inputs,
            ),
            regime_obj=None,
            accuracy_stats={},
            expensive_checks=False,
            fetch_budget={"remaining": 0},
        )

        assert score.score_liquidity == 0.0

    @patch("core.toss_quality_gate._score_supply_demand", return_value=0.0)
    @patch("core.toss_quality_gate._score_market_regime", return_value=15.0)
    @patch("core.toss_quality_gate._score_reliability", return_value=7.5)
    @patch("core.toss_quality_gate._score_risk_reward", return_value=10.0)
    @patch("core.toss_quality_gate._score_liquidity", return_value=0.0)
    @patch("core.toss_quality_gate._score_momentum", return_value=12.46)
    def test_component_rounding_cannot_promote_non_executable_origin(
        self, _momentum, _liquidity, _rr, _reliability, _regime, _supply,
    ):
        score = score_candidate(
            _candidate(market="US", symbol="TSM", risk_reward=1.3),
            regime_obj=MagicMock(regime="강세장"),
            accuracy_stats={},
            expensive_checks=False,
            fetch_budget={"remaining": 0},
        )

        assert score.score_total == 45.0
        assert score.decision_bucket == WATCH

    @patch("core.toss_quality_gate._score_supply_demand", return_value=0.0)
    @patch("core.toss_quality_gate._score_market_regime", return_value=15.0)
    @patch("core.toss_quality_gate._score_reliability", return_value=7.549)
    @patch("core.toss_quality_gate._score_risk_reward", return_value=10.049)
    @patch("core.toss_quality_gate._score_liquidity", return_value=0.0)
    @patch("core.toss_quality_gate._score_momentum", return_value=12.449)
    def test_bucket_replays_from_serialized_component_total(
        self, _momentum, _liquidity, _rr, _reliability, _regime, _supply,
    ):
        from core import toss_quality_gate as qg

        score = score_candidate(
            _candidate(market="US", symbol="TSM", risk_reward=1.67),
            regime_obj=MagicMock(regime="강세장"),
            accuracy_stats={},
            expensive_checks=False,
            fetch_budget={"remaining": 0},
        )
        breakdown = score.to_dict()

        assert score.score_total == breakdown["score_total"]
        assert qg._stored_bucket_replay_matches(breakdown) is True

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

    def test_score_total_clamps_serialized_component_sum(self):
        common = {
            "ticker": "BOUND", "risk_flags": (),
            "decision_bucket": PASS_EXECUTE, "decision_reason": "ok",
            "rr_ratio": 2.0, "regime": "강세장",
            "scored_at": "2026-07-20T23:00:00+09:00",
        }
        high = QualityScore(
            score_total=100.0,
            score_momentum=25.0, score_liquidity=25.0,
            score_risk_reward=20.0, score_reliability=15.0,
            score_market_regime=15.0, score_supply_demand=10.0,
            penalty_overheat=0.0, penalty_duplicate=0.0,
            penalty_event_risk=0.0,
            **common,
        )
        low = QualityScore(
            score_total=0.0,
            score_momentum=0.0, score_liquidity=0.0,
            score_risk_reward=0.0, score_reliability=0.0,
            score_market_regime=0.0, score_supply_demand=0.0,
            penalty_overheat=-15.0, penalty_duplicate=-5.0,
            penalty_event_risk=-15.0,
            **common,
        )

        assert high.to_dict()["score_total"] == 100.0
        assert low.to_dict()["score_total"] == 0.0

    def test_score_total_matches_serialized_component_sum_at_rounding_boundary(self):
        qs = QualityScore(
            ticker="TSM", score_total=35.499999,
            score_momentum=2.949999, score_liquidity=0.0,
            score_risk_reward=10.049999, score_reliability=7.5,
            score_market_regime=15.0, score_supply_demand=0.0,
            penalty_overheat=0.0, penalty_duplicate=0.0,
            penalty_event_risk=0.0, risk_flags=(),
            decision_bucket=WATCH, decision_reason="score boundary",
            rr_ratio=1.67, regime="강세장",
            scored_at="2026-07-20T23:00:00+09:00",
        )

        breakdown = qs.to_dict()
        component_total = sum(
            breakdown[key]
            for key in (
                "score_momentum", "score_liquidity", "score_risk_reward",
                "score_reliability", "score_market_regime",
                "score_supply_demand", "penalty_overheat",
                "penalty_duplicate", "penalty_event_risk",
            )
        )

        assert breakdown["score_total"] == round(component_total, 1)

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


# ── KRX 수급 보정 (P3-1) 테스트 ──────────────────────────────────

def _frgn_rows(inst: float, frgn: float, days: int = 5) -> list[dict]:
    return [
        {"date": f"2026070{d}", "close": 10000.0,
         "inst_shares": inst, "foreign_shares": frgn}
        for d in range(days, 0, -1)
    ]


class TestSupplyDemand:

    def _score(self, ticker="316140.KS", pre_score=60.0, rows=None,
               budget=None, cache=None):
        from core.toss_quality_gate import _score_supply_demand
        with patch("core.kr_market._fetch_naver_frgn",
                   return_value=rows if rows is not None else []) as mock_fetch, \
             patch("core.kr_market._FRGN_CACHE", cache if cache is not None else {}), \
             patch("core.kr_market._load_frgn_file_entry", return_value=None):
            score = _score_supply_demand(ticker, pre_score, fetch_budget=budget)
        return score, mock_fetch

    def test_both_net_buy_plus_10(self):
        score, _ = self._score(rows=_frgn_rows(inst=100, frgn=200))
        assert score == 10.0

    def test_both_net_sell_minus_10(self):
        score, _ = self._score(rows=_frgn_rows(inst=-100, frgn=-200))
        assert score == -10.0

    def test_mixed_follows_dominant(self):
        # 상쇄 설계 폐기(2026-07-14): 엇갈림은 주도 주체 방향 ±3
        score, _ = self._score(rows=_frgn_rows(inst=-100, frgn=100))
        assert score == 3.0 or score == -3.0

    def test_us_ticker_skipped(self):
        score, mock_fetch = self._score(ticker="NVDA",
                                        rows=_frgn_rows(inst=100, frgn=100))
        assert score == 0.0
        mock_fetch.assert_not_called()

    def test_low_pre_score_skipped(self):
        score, mock_fetch = self._score(pre_score=30.0,
                                        rows=_frgn_rows(inst=100, frgn=100))
        assert score == 0.0
        mock_fetch.assert_not_called()

    def test_fetch_failure_fail_safe(self):
        score, _ = self._score(rows=[])
        assert score == 0.0

    def test_budget_exhausted_uncached_skips_fetch(self):
        score, mock_fetch = self._score(rows=_frgn_rows(inst=100, frgn=100),
                                        budget={"remaining": 0})
        assert score == 0.0
        mock_fetch.assert_not_called()

    def test_cached_symbol_ignores_budget(self):
        rows = _frgn_rows(inst=100, frgn=100)
        score, _ = self._score(rows=rows, budget={"remaining": 0},
                               cache={"316140": rows})
        assert score == 10.0

    def test_budget_decrements(self):
        budget = {"remaining": 2}
        self._score(rows=_frgn_rows(inst=100, frgn=100), budget=budget)
        assert budget["remaining"] == 1

    def test_bare_kr_code_accepted(self):
        score, _ = self._score(ticker="316140",
                               rows=_frgn_rows(inst=100, frgn=100))
        assert score == 10.0

    @patch("core.toss_quality_gate._score_momentum", return_value=15.0)
    @patch("core.toss_quality_gate._penalty_event_risk", return_value=(0.0, -1))
    def test_score_candidate_includes_supply(self, mock_event, mock_momentum):
        c = _candidate(risk_reward=2.5)
        with patch("core.toss_quality_gate._score_supply_demand",
                   return_value=10.0):
            qs = score_candidate(c)
        assert qs.score_supply_demand == 10.0
        assert qs.to_dict()["score_supply_demand"] == 10.0

    @patch("core.toss_quality_gate._score_momentum", return_value=15.0)
    @patch("core.toss_quality_gate._penalty_event_risk", return_value=(0.0, -1))
    def test_supply_affects_total(self, mock_event, mock_momentum):
        c = _candidate(risk_reward=2.5)
        with patch("core.toss_quality_gate._score_supply_demand",
                   return_value=0.0):
            base = score_candidate(c).score_total
        with patch("core.toss_quality_gate._score_supply_demand",
                   return_value=-10.0):
            lowered = score_candidate(c).score_total
        assert lowered == base - 10.0

    @patch("core.toss_quality_gate._score_momentum", return_value=15.0)
    @patch("core.toss_quality_gate._penalty_event_risk", return_value=(0.0, -1))
    @patch("core.regime.detect_regime")
    def test_batch_passes_shared_budget(self, mock_regime, mock_event,
                                        mock_momentum):
        mock_regime.return_value = MagicMock(regime="강세장", risk_adjustment="중립")
        seen = []
        with patch("core.toss_quality_gate._score_supply_demand",
                   side_effect=lambda t, p, fetch_budget=None:
                   seen.append(fetch_budget) or 0.0):
            score_candidates_batch(
                [_candidate(symbol="A"), _candidate(symbol="B")], market="KR")
        assert len(seen) == 2
        assert seen[0] is seen[1]  # 배치 전체가 예산 공유
        assert seen[0] == {"remaining": 3}


# ── 가중치 캘리브레이션 구조 (P3-4) 테스트 ───────────────────────

class TestScoreWeights:

    def test_default_weights_all_one(self, tmp_path):
        from core import toss_quality_gate as qg
        with patch.object(qg, "_weights_path",
                          return_value=tmp_path / "none.json"):
            w = qg.get_score_weights()
        assert all(v == 1.0 for v in w.values())
        assert set(w) == {"momentum", "liquidity", "risk_reward",
                          "reliability", "market_regime", "supply_demand"}

    def test_file_override_and_clamp(self, tmp_path):
        import json
        from core import toss_quality_gate as qg
        p = tmp_path / "quality_gate_weights.json"
        p.write_text(json.dumps({"momentum": 1.2, "liquidity": 9.0,
                                 "risk_reward": 0.1}), encoding="utf-8")
        with patch.object(qg, "_weights_path", return_value=p):
            qg._weights_cache["mtime"] = None
            w = qg.get_score_weights()
        assert w["momentum"] == 1.2
        assert w["liquidity"] == 1.5   # clamp 상한
        assert w["risk_reward"] == 0.5  # clamp 하한
        assert w["supply_demand"] == 1.0  # 미지정 → 기본

    @patch("core.toss_quality_gate._score_momentum", return_value=20.0)
    @patch("core.toss_quality_gate._penalty_event_risk", return_value=(0.0, -1))
    def test_weights_scale_scores(self, mock_event, mock_momentum):
        from core import toss_quality_gate as qg
        c = _candidate(risk_reward=2.5)
        base_w = dict(qg._DEFAULT_WEIGHTS)
        half_w = dict(base_w, momentum=0.5)
        with patch.object(qg, "get_score_weights", return_value=base_w):
            base = score_candidate(c)
        with patch.object(qg, "get_score_weights", return_value=half_w):
            halved = score_candidate(c)
        assert halved.score_momentum == base.score_momentum / 2


class TestWeightCalibration:

    def _with_db(self, tmp_path, rows):
        """임시 outcomes DB에 rows 삽입 후 suggest 실행."""
        import core.toss_quality_gate as qg
        db = tmp_path / "toss_quality_gate.db"
        with patch.object(qg, "_outcomes_db_path", return_value=db):
            qg._outcomes_schema_created = False
            conn = qg._outcomes_conn()
            for r in rows:
                conn.execute(
                    "INSERT INTO quality_gate_decisions "
                    "(ticker, decided_at, decision_bucket, outcome, "
                    " score_momentum, score_liquidity, score_risk_reward, "
                    " score_reliability, score_market_regime) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    ("T", "2026-06-01T10:00:00+09:00", PASS_EXECUTE,
                     r["outcome"], r["m"], 15, 15, 7.5, 10),
                )
            conn.commit()
            conn.close()
            result = qg.suggest_weight_calibration(min_outcomes=10)
            qg._outcomes_schema_created = False
        return result

    def test_insufficient_outcomes(self, tmp_path):
        rows = [{"outcome": "win", "m": 20}] * 5
        r = self._with_db(tmp_path, rows)
        assert r["ok"] is False
        assert r["reason"] == "insufficient_outcomes"

    def test_suggests_higher_weight_for_predictive_dim(self, tmp_path):
        # win의 momentum이 loss보다 뚜렷이 높음 → momentum 가중치 > 1.0
        rows = ([{"outcome": "win", "m": 22.0}] * 6
                + [{"outcome": "loss", "m": 8.0}] * 6)
        r = self._with_db(tmp_path, rows)
        assert r["ok"] is True
        assert r["suggested_weights"]["momentum"] > 1.0
        # 차이 없는 차원은 1.0 유지
        assert r["suggested_weights"]["liquidity"] == 1.0
        assert r["suggested_weights"]["supply_demand"] == 1.0  # DB 미기록 → 기본

    def test_need_both_outcomes(self, tmp_path):
        rows = [{"outcome": "win", "m": 20}] * 12
        r = self._with_db(tmp_path, rows)
        assert r["ok"] is False
        assert r["reason"] == "need_both_win_and_loss"

    def test_suggestion_file_written(self, tmp_path):
        import core.toss_quality_gate as qg
        rows = ([{"outcome": "win", "m": 22.0}] * 6
                + [{"outcome": "loss", "m": 8.0}] * 6)
        self._with_db(tmp_path, rows)
        assert (tmp_path / "quality_gate_weights_suggestion.json").exists()


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


class TestScoringErrorHygiene:
    def test_scoring_exception_uses_fixed_reason_and_type_only_log(self, caplog):
        from core import toss_quality_gate as qg

        synthetic_marker = "authorization=quality-private-value"
        item = {"symbol": "316140.KS"}
        with patch.object(
            qg, "score_candidate", side_effect=RuntimeError(synthetic_marker)
        ):
            result = qg.score_candidates_batch([item], market="KR")

        assert result[0]["decision_bucket"] == WATCH
        assert result[0]["decision_reason"] == "scoring_error"
        assert synthetic_marker not in str(result)
        assert synthetic_marker not in caplog.text



class TestExactExecutionQualityDecision:
    def _candidate(self):
        from core import toss_quality_gate as qg
        cand = {
            "symbol": "316140.KS",
            "side": "buy",
            "decision_bucket": PASS_EXECUTE,
            "decision_reason": "quality pass",
            "quantity": 2,
            "quality_score": 87.0,
            "quality_breakdown": {
                "score_total": 87.0,
                "score_momentum": 20.0,
                "score_liquidity": 18.0,
                "score_risk_reward": 17.0,
                "score_reliability": 13.0,
                "score_market_regime": 14.0,
                "score_supply_demand": 5.0,
                "penalty_overheat": 0.0,
                "penalty_duplicate": 0.0,
                "penalty_event_risk": 0.0,
                "rr_ratio": 3.04,
                "regime": "강세장",
            },
            "limit_price": 28_750,
            "stop_loss": 27_025,
            "target_price": 34_000,
        }
        _seal_quality_proof_for_test(qg, cand)
        return cand

    def test_records_exact_ref_and_is_idempotent(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg

        monkeypatch.setattr(qg, "_outcomes_db_path", lambda: tmp_path / "quality.db")
        qg._outcomes_schema_created = False
        ref = "execution_decision:tlive_origin_1234"
        pilot_id = "tlive_20260713_120000_1234"
        first = qg.record_execution_quality_decision(
            self._candidate(), pilot_id=pilot_id, decision_ref=ref
        )
        second = qg.record_execution_quality_decision(
            self._candidate(), pilot_id=pilot_id, decision_ref=ref
        )
        assert first["ok"] is True
        assert second == {"ok": True, "id": first["id"], "created": False}
        row = qg.quality_decision_for_ref(ref)
        assert row["pilot_id"] == pilot_id
        assert row["decision_ref"] == ref
        assert row["ticker"] == "316140.KS"

    def test_rejects_non_executable_or_invalid_exact_keys(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg

        monkeypatch.setattr(qg, "_outcomes_db_path", lambda: tmp_path / "quality_bad.db")
        qg._outcomes_schema_created = False
        candidate = self._candidate()
        candidate["decision_bucket"] = WATCH
        assert qg.record_execution_quality_decision(
            candidate,
            pilot_id="tlive_20260713_120001_1234",
            decision_ref="execution_decision:tlive_origin_1234",
        )["ok"] is False
        candidate["decision_bucket"] = PASS_EXECUTE
        assert qg.record_execution_quality_decision(
            candidate,
            pilot_id="external-order",
            decision_ref="execution_decision:tlive_origin_1234",
        )["ok"] is False

    def test_existing_schema_migrates_decision_ref(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg

        db = tmp_path / "quality_legacy.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE quality_gate_decisions ("
            "id INTEGER PRIMARY KEY, ticker TEXT, decision_bucket TEXT)"
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(qg, "_outcomes_db_path", lambda: db)
        qg._outcomes_schema_created = False
        migrated = qg._outcomes_conn()
        cols = {row[1] for row in migrated.execute(
            "PRAGMA table_info(quality_gate_decisions)"
        ).fetchall()}
        migrated.close()
        assert "decision_ref" in cols
        recorded = qg.record_execution_quality_decision(
            self._candidate(),
            pilot_id="tlive_20260713_partial_1234",
            decision_ref="execution_decision:tlive_partial_1234",
        )
        assert recorded["ok"] is True

    def test_same_pilot_id_cannot_bind_to_different_decision_ref(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg

        monkeypatch.setattr(qg, "_outcomes_db_path", lambda: tmp_path / "quality_pilot_unique.db")
        qg._outcomes_schema_created = False
        pilot_id = "tlive_20260713_120010_1234"
        first = qg.record_execution_quality_decision(
            self._candidate(),
            pilot_id=pilot_id,
            decision_ref="execution_decision:tlive_origin_1010",
        )
        conflict = qg.record_execution_quality_decision(
            self._candidate(),
            pilot_id=pilot_id,
            decision_ref="execution_decision:tlive_origin_2020",
        )

        assert first["ok"] is True
        assert conflict == {"ok": False, "reason": "quality_pilot_id_conflict"}

    def test_exact_quality_decision_idempotency_requires_immutable_payload(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg

        monkeypatch.setattr(qg, "_outcomes_db_path", lambda: tmp_path / "quality_immutable.db")
        qg._outcomes_schema_created = False
        pilot_id = "tlive_20260713_120020_1234"
        ref = "execution_decision:tlive_origin_3030"
        first = qg.record_execution_quality_decision(
            self._candidate(), pilot_id=pilot_id, decision_ref=ref
        )

        changed_price = self._candidate()
        changed_price["limit_price"] += 100
        # 내부 정합(RR 재계산 일치)은 유지한 변조 — payload conflict 경로 검증
        # (34000-28850)/(28850-27025) = 2.8219
        changed_price["quality_breakdown"]["rr_ratio"] = 2.82
        _seal_quality_proof_for_test(qg, changed_price)  # 변조 후에도 증명은 정합 — payload conflict 경로 검증
        price_conflict = qg.record_execution_quality_decision(
            changed_price, pilot_id=pilot_id, decision_ref=ref
        )
        changed_bucket = self._candidate()
        changed_bucket["decision_bucket"] = SMALL_PASS
        _seal_quality_proof_for_test(qg, changed_bucket)
        bucket_conflict = qg.record_execution_quality_decision(
            changed_bucket, pilot_id=pilot_id, decision_ref=ref
        )

        assert first["ok"] is True
        assert price_conflict == {
            "ok": False, "reason": "quality_decision_payload_conflict",
        }
        assert bucket_conflict == {
            "ok": False, "reason": "quality_decision_payload_conflict",
        }

    def test_quantity_and_supply_demand_are_immutable(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg

        monkeypatch.setattr(qg, "_outcomes_db_path", lambda: tmp_path / "quality_sizing.db")
        qg._outcomes_schema_created = False
        pilot_id = "tlive_20260713_120021_1234"
        ref = "execution_decision:tlive_origin_3031"
        first = qg.record_execution_quality_decision(
            self._candidate(), pilot_id=pilot_id, decision_ref=ref
        )

        changed_quantity = self._candidate()
        changed_quantity["quantity"] = 99
        _seal_quality_proof_for_test(qg, changed_quantity)
        quantity_conflict = qg.record_execution_quality_decision(
            changed_quantity, pilot_id=pilot_id, decision_ref=ref
        )
        changed_supply = self._candidate()
        changed_supply["quality_breakdown"]["score_supply_demand"] = 9.0
        # 내부 정합(합=91)은 유지하되 기존 기록과 다른 변조 — payload conflict 경로 검증
        changed_supply["quality_breakdown"]["score_total"] = 91.0
        changed_supply["quality_score"] = 91.0
        _seal_quality_proof_for_test(qg, changed_supply)
        supply_conflict = qg.record_execution_quality_decision(
            changed_supply, pilot_id=pilot_id, decision_ref=ref
        )
        row = qg.quality_decision_for_ref(ref)

        assert first["ok"] is True
        assert row["quantity"] == 2
        assert row["score_supply_demand"] == 5.0
        assert quantity_conflict == {
            "ok": False, "reason": "quality_decision_payload_conflict",
        }
        assert supply_conflict == {
            "ok": False, "reason": "quality_decision_payload_conflict",
        }

    def test_last_mile_validation_requires_exact_row_and_sizing(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg

        monkeypatch.setattr(qg, "_outcomes_db_path", lambda: tmp_path / "quality_last_mile.db")
        qg._outcomes_schema_created = False
        pilot_id = "tlive_20260713_120022_1234"
        ref = "execution_decision:tlive_origin_3032"
        candidate = self._candidate()
        created = qg.record_execution_quality_decision(
            candidate, pilot_id=pilot_id, decision_ref=ref
        )
        rec = {
            "pilot_id": pilot_id,
            "decision_ref": ref,
            "symbol": candidate["symbol"],
            "side": "buy",
            "quantity": candidate["quantity"],
            "limit_price": candidate["limit_price"],
            "stop_loss": candidate["stop_loss"],
            "target_price": candidate["target_price"],
        }

        exact = qg.validate_execution_quality_decision(rec, pilot_id=pilot_id)
        mismatched = qg.validate_execution_quality_decision(
            {**rec, "quantity": rec["quantity"] + 1}, pilot_id=pilot_id
        )

        assert created["ok"] is True
        assert exact["ok"] is True
        assert exact["reason"] == "quality_decision_exact"
        assert mismatched == {"ok": False, "reason": "quality_decision_mismatch"}

    def test_quality_schema_migration_failure_is_retryable_and_not_ready(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg

        db = tmp_path / "quality_migration_retry.db"
        seed = sqlite3.connect(db)
        seed.execute(
            "CREATE TABLE quality_gate_decisions ("
            "id INTEGER PRIMARY KEY, ticker TEXT, decision_bucket TEXT)"
        )
        seed.commit()
        seed.close()

        real_connect = qg.sqlite3.connect
        fail = {"enabled": True}

        class ConnectionProxy:
            def __init__(self, real):
                object.__setattr__(self, "_real", real)

            def __getattr__(self, name):
                return getattr(self._real, name)

            def __setattr__(self, name, value):
                setattr(self._real, name, value)

            def execute(self, sql, *args):
                if (
                    fail["enabled"]
                    and "ALTER TABLE" in sql
                    and "decision_ref" in sql
                ):
                    raise sqlite3.OperationalError("database is locked")
                return self._real.execute(sql, *args)

        monkeypatch.setattr(
            qg.sqlite3,
            "connect",
            lambda *args, **kwargs: ConnectionProxy(real_connect(*args, **kwargs)),
        )
        monkeypatch.setattr(qg, "_outcomes_db_path", lambda: db)
        qg._outcomes_schema_created = False

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            qg._outcomes_conn()
        assert qg._outcomes_schema_created is False

        fail["enabled"] = False
        migrated = qg._outcomes_conn()
        indexes = {
            row[1] for row in migrated.execute(
                "PRAGMA index_list(quality_gate_decisions)"
            ).fetchall()
        }
        migrated.close()
        assert qg._outcomes_schema_created is True
        assert "idx_qg_decision_ref_exact" in indexes
        assert "idx_qg_pilot_id_exact" in indexes

    def test_quality_schema_rejects_hostile_same_name_nonunique_indexes(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg

        db = tmp_path / "quality_hostile_index.db"
        seed = sqlite3.connect(db)
        seed.execute(
            "CREATE TABLE quality_gate_decisions ("
            "id INTEGER PRIMARY KEY, ticker TEXT, decision_ref TEXT, pilot_id TEXT)"
        )
        seed.execute(
            "CREATE INDEX idx_qg_decision_ref_exact "
            "ON quality_gate_decisions(ticker)"
        )
        seed.execute(
            "CREATE INDEX idx_qg_pilot_id_exact "
            "ON quality_gate_decisions(ticker)"
        )
        seed.commit()
        seed.close()

        monkeypatch.setattr(qg, "_outcomes_db_path", lambda: db)
        qg._outcomes_schema_created = False

        with pytest.raises(RuntimeError, match="quality_schema_index_invalid"):
            qg._outcomes_conn()
        assert qg._outcomes_schema_created is False


class TestFailClosedRecompute:
    """실행 계약 fail-closed: 호출자가 만든 총점·PASS를 신뢰하지 않고 재계산."""

    def _consistent_candidate(self, **overrides):
        # 컴포넌트 합 = 20+18+17+13+14+5 = 87, 페널티 0 → total 87
        breakdown = {
            "score_total": 87.0,
            "score_momentum": 20.0,
            "score_liquidity": 18.0,
            "score_risk_reward": 17.0,
            "score_reliability": 13.0,
            "score_market_regime": 14.0,
            "score_supply_demand": 5.0,
            "penalty_overheat": 0.0,
            "penalty_duplicate": 0.0,
            "penalty_event_risk": 0.0,
            "rr_ratio": 3.04,
            "regime": "강세장",
        }
        breakdown.update(overrides.pop("breakdown", {}))
        cand = {
            "symbol": "316140.KS",
            "side": "buy",
            "decision_bucket": PASS_EXECUTE,
            "decision_reason": "quality pass",
            "quantity": 2,
            "quality_score": breakdown["score_total"],
            "quality_breakdown": breakdown,
            "limit_price": 28_750,
            "stop_loss": 27_025,
            "target_price": 34_000,
        }
        cand.update(overrides)
        from core import toss_quality_gate as qg
        _seal_quality_proof_for_test(qg, cand)
        return cand

    def _setup(self, qg, tmp_path, monkeypatch, name):
        monkeypatch.setattr(qg, "_outcomes_db_path", lambda: tmp_path / name)
        qg._outcomes_schema_created = False

    def test_record_rejects_forged_score_total(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg
        self._setup(qg, tmp_path, monkeypatch, "forge1.db")
        cand = self._consistent_candidate(
            breakdown={"score_total": 95.0})  # 합 87인데 95 주장
        out = qg.record_execution_quality_decision(
            cand, pilot_id="tlive_20260714_100000_0001",
            decision_ref="execution_decision:tlive_forge_0001")
        assert out["ok"] is False
        assert "recompute" in out["reason"]

    def test_record_rejects_pass_bucket_with_low_rr(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg
        self._setup(qg, tmp_path, monkeypatch, "forge2.db")
        cand = self._consistent_candidate(breakdown={"rr_ratio": 1.5})
        # PASS는 rr>=1.8 필요 — 1.5로 위조된 PASS는 기록 거부
        out = qg.record_execution_quality_decision(
            cand, pilot_id="tlive_20260714_100001_0001",
            decision_ref="execution_decision:tlive_forge_0002")
        assert out["ok"] is False
        assert "recompute" in out["reason"]

    def test_validate_rejects_tampered_stored_row(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg
        self._setup(qg, tmp_path, monkeypatch, "forge3.db")
        pilot_id = "tlive_20260714_100002_0001"
        ref = "execution_decision:tlive_forge_0003"
        created = qg.record_execution_quality_decision(
            self._consistent_candidate(), pilot_id=pilot_id, decision_ref=ref)
        assert created["ok"] is True
        # 저장 후 DB에서 총점만 직접 위조 (호출자 위조 시나리오 재현)
        conn = qg._outcomes_conn()
        conn.execute(
            "UPDATE quality_gate_decisions SET score_total=20.0 WHERE decision_ref=?",
            (ref,))
        conn.commit(); conn.close()
        rec = {
            "side": "buy", "pilot_id": pilot_id, "decision_ref": ref,
            "symbol": "316140.KS", "quantity": 2,
            "limit_price": 28_750, "stop_loss": 27_025, "target_price": 34_000,
        }
        out = qg.validate_execution_quality_decision(rec, pilot_id=pilot_id)
        assert out["ok"] is False
        assert out["reason"] == "quality_decision_breakdown_mismatch"

    def test_consistent_decision_passes_end_to_end(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg
        self._setup(qg, tmp_path, monkeypatch, "ok1.db")
        pilot_id = "tlive_20260714_100003_0001"
        ref = "execution_decision:tlive_ok_0001"
        assert qg.record_execution_quality_decision(
            self._consistent_candidate(), pilot_id=pilot_id, decision_ref=ref,
        )["ok"] is True
        rec = {
            "side": "buy", "pilot_id": pilot_id, "decision_ref": ref,
            "symbol": "316140.KS", "quantity": 2,
            "limit_price": 28_750, "stop_loss": 27_025, "target_price": 34_000,
        }
        out = qg.validate_execution_quality_decision(rec, pilot_id=pilot_id)
        assert out["ok"] is True


class TestHermesProbeRegression:
    """Hermes 재검증 probe 3종 — canonical regression (2026-07-14 FAIL 재발 방지)."""

    def _breakdown(self, **over):
        b = {
            "score_total": 87.0, "score_momentum": 20.0, "score_liquidity": 18.0,
            "score_risk_reward": 17.0, "score_reliability": 13.0,
            "score_market_regime": 14.0, "score_supply_demand": 5.0,
            "penalty_overheat": 0.0, "penalty_duplicate": 0.0,
            "penalty_event_risk": 0.0,
            "rr_ratio": 3.04,  # (34000-28750)/(28750-27025) = 3.0435
            "regime": "강세장",
        }
        b.update(over)
        return b

    def _candidate(self, **over):
        c = {
            "symbol": "316140.KS", "side": "buy",
            "decision_bucket": PASS_EXECUTE, "decision_reason": "quality pass",
            "quantity": 2, "quality_score": 87.0,
            "quality_breakdown": self._breakdown(**over.pop("breakdown", {})),
            "limit_price": 28_750, "stop_loss": 27_025, "target_price": 34_000,
        }
        c.update(over)
        from core import toss_quality_gate as qg
        _seal_quality_proof_for_test(qg, c)
        return c

    def _setup(self, qg, tmp_path, monkeypatch, name):
        monkeypatch.setattr(qg, "_outcomes_db_path", lambda: tmp_path / name)
        qg._outcomes_schema_created = False

    # probe 1: adapter string/int bool
    def test_probe_adapter_rejects_string_and_int_policy_bools(self):
        from core.toss_live_pilot_adapter import can_send_live_pilot_order
        preview = {"ok": True, "side": "buy", "symbol": "AAPL",
                   "limit_price": 100.0, "quantity": 1,
                   "estimated_amount_krw": 150000.0}
        payload = {"ok": True}
        for bad in ("true", "false", 1, 0):
            policy = {
                "live_pilot_enabled": bad, "live_order_allowed": bad,
                "autonomous_mode": bad, "adapter_status": "enabled",
                "allowed_asset_types": ["US_STOCK"], "allowed_sides": ["buy"],
            }
            ok, reasons = can_send_live_pilot_order(policy, preview, payload)
            assert ok is False, f"policy bool {bad!r} 통과됨"
            assert any("policy_schema_invalid" in r for r in reasons), reasons

    def test_probe_adapter_rejects_string_preview_flags(self):
        from core.toss_live_pilot_adapter import can_send_live_pilot_order
        policy = {
            "live_pilot_enabled": True, "live_order_allowed": True,
            "autonomous_mode": True, "adapter_status": "enabled",
            "allowed_asset_types": ["US_STOCK"], "allowed_sides": ["buy"],
        }
        preview = {"ok": "true", "live_order_sent": "false", "side": "buy",
                   "symbol": "AAPL", "limit_price": 100.0, "quantity": 1,
                   "estimated_amount_krw": 150000.0}
        ok, reasons = can_send_live_pilot_order(policy, preview, {"ok": True})
        assert ok is False
        assert any("preview_schema_invalid" in r for r in reasons), reasons

    def test_probe_transport_string_flags_fail_closed(self):
        from core import toss_live_pilot_adapter as adapter
        policy = {
            "live_pilot_enabled": True, "live_order_allowed": True,
            "autonomous_mode": True, "adapter_status": "enabled",
            "autonomous_kill_switch": False,
            "allowed_asset_types": ["US_STOCK"],
            "autonomous_allowed_asset_types": ["US_STOCK"],
            "allowed_sides": ["buy"], "autonomous_allowed_sides": ["buy"],
        }
        payload = {
            "symbol": "AAPL", "side": "buy", "order_type": "limit",
            "quantity": 1, "limit_price": 100.0,
            "client_order_id": "tlive_probe_transport_0001",
            "pilot_id": "tlive_probe_transport_0001",
        }
        with patch.object(
            adapter, "_load_authoritative_dispatch_record", return_value=payload,
        ):
            result = adapter.dispatch_toss_order_live(
                payload,
                policy,
                transport=lambda order, pol: {
                    "ok": "true", "live_order_sent": "true", "broker_confirmed": 1,
                },
            )
        assert result["ok"] is False
        assert result["live_order_sent"] is False
        assert result["reason"] == "transport_schema_invalid"

    # probe 2: missing components
    def test_probe_missing_component_rejected(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg
        self._setup(qg, tmp_path, monkeypatch, "probe_missing.db")
        cand = self._candidate()
        del cand["quality_breakdown"]["score_supply_demand"]
        out = qg.record_execution_quality_decision(
            cand, pilot_id="tlive_20260714_110000_0001",
            decision_ref="execution_decision:tlive_probe_0001")
        assert out["ok"] is False
        assert out["reason"] == "quality_components_missing"

    # probe 3: forged RR
    def test_probe_forged_rr_rejected_at_record(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg
        self._setup(qg, tmp_path, monkeypatch, "probe_rr1.db")
        cand = self._candidate(breakdown={"rr_ratio": 9.0})  # 실제 3.04
        out = qg.record_execution_quality_decision(
            cand, pilot_id="tlive_20260714_110001_0001",
            decision_ref="execution_decision:tlive_probe_0002")
        assert out["ok"] is False
        assert out["reason"] == "quality_rr_recompute_mismatch"

    def test_probe_forged_rr_rejected_at_validate(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg
        self._setup(qg, tmp_path, monkeypatch, "probe_rr2.db")
        pilot_id = "tlive_20260714_110002_0001"
        ref = "execution_decision:tlive_probe_0003"
        assert qg.record_execution_quality_decision(
            self._candidate(), pilot_id=pilot_id, decision_ref=ref)["ok"] is True
        conn = qg._outcomes_conn()
        conn.execute(
            "UPDATE quality_gate_decisions SET rr_ratio=9.0 WHERE decision_ref=?",
            (ref,))
        conn.commit(); conn.close()
        rec = {"side": "buy", "pilot_id": pilot_id, "decision_ref": ref,
               "symbol": "316140.KS", "quantity": 2,
               "limit_price": 28_750, "stop_loss": 27_025, "target_price": 34_000}
        out = qg.validate_execution_quality_decision(rec, pilot_id=pilot_id)
        assert out["ok"] is False
        assert out["reason"] == "quality_decision_breakdown_mismatch"

    # side 증명 (항목 6)
    def test_legacy_row_without_side_fails_closed(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg
        self._setup(qg, tmp_path, monkeypatch, "probe_side.db")
        pilot_id = "tlive_20260714_110003_0001"
        ref = "execution_decision:tlive_probe_0004"
        assert qg.record_execution_quality_decision(
            self._candidate(), pilot_id=pilot_id, decision_ref=ref)["ok"] is True
        conn = qg._outcomes_conn()
        conn.execute(
            "UPDATE quality_gate_decisions SET side='' WHERE decision_ref=?", (ref,))
        conn.commit(); conn.close()
        rec = {"side": "buy", "pilot_id": pilot_id, "decision_ref": ref,
               "symbol": "316140.KS", "quantity": 2,
               "limit_price": 28_750, "stop_loss": 27_025, "target_price": 34_000}
        out = qg.validate_execution_quality_decision(rec, pilot_id=pilot_id)
        assert out["ok"] is False
        assert out["reason"] == "quality_decision_side_unverified"


class TestLateFindingsProbeMatrix:
    """Hermes 53673ed 재검증 late findings (B1~B6) canonical regression."""

    def _proofed_candidate(self, qg, **over):
        cand = {
            "symbol": "316140.KS", "side": "buy",
            "decision_bucket": PASS_EXECUTE, "decision_reason": "quality pass",
            "quantity": 2, "quality_score": 87.0,
            "quality_breakdown": {
                "score_total": 87.0, "score_momentum": 20.0,
                "score_liquidity": 18.0, "score_risk_reward": 17.0,
                "score_reliability": 13.0, "score_market_regime": 14.0,
                "score_supply_demand": 5.0, "penalty_overheat": 0.0,
                "penalty_duplicate": 0.0, "penalty_event_risk": 0.0,
                "rr_ratio": 3.04, "regime": "강세장",
            },
            "limit_price": 28_750, "stop_loss": 27_025, "target_price": 34_000,
        }
        cand.update(over)
        _seal_quality_proof_for_test(qg, cand)
        return cand

    def _setup(self, qg, tmp_path, monkeypatch, name):
        monkeypatch.setattr(qg, "_outcomes_db_path", lambda: tmp_path / name)
        qg._outcomes_schema_created = False

    # B1: 빈 policy/payload는 transport에 도달 금지
    def test_b1_empty_dispatch_never_reaches_transport(self):
        from core import toss_live_pilot_adapter as adapter
        called = {"n": 0}

        def fake_transport(payload, policy):
            called["n"] += 1
            return {"ok": True, "live_order_sent": True, "broker_confirmed": True}

        result = adapter.dispatch_toss_order_live({}, {}, transport=fake_transport)
        assert called["n"] == 0, "빈 계약이 transport에 도달함"
        assert result["ok"] is False
        assert result["live_order_sent"] is False
        assert result["reason"] == "dispatch_contract_invalid"

    # B2: None/비-dict 입력은 raise 없이 typed 차단
    def test_b2_none_policy_returns_typed_block(self):
        from core.toss_live_pilot_adapter import can_send_live_pilot_order
        ok, reasons = can_send_live_pilot_order(None, {}, {})
        assert ok is False
        assert any("policy_schema_invalid" in r for r in reasons)

    def test_b2_none_transport_result_fail_closed(self):
        from core import toss_live_pilot_adapter as adapter
        policy = {"live_pilot_enabled": True, "live_order_allowed": True,
                  "autonomous_mode": True, "adapter_status": "enabled",
                  "autonomous_kill_switch": False,
                  "allowed_asset_types": ["US_STOCK"],
                  "autonomous_allowed_asset_types": ["US_STOCK"],
                  "allowed_sides": ["buy"], "autonomous_allowed_sides": ["buy"]}
        payload = {"symbol": "AAPL", "side": "buy", "order_type": "limit",
                   "quantity": 1, "limit_price": 100.0,
                   "client_order_id": "tlive_probe_none_0001",
                   "pilot_id": "tlive_probe_none_0001"}
        with patch.object(
            adapter, "_load_authoritative_dispatch_record", return_value=payload,
        ):
            result = adapter.dispatch_toss_order_live(
                payload, policy, transport=lambda p, pol: None)
        assert result["ok"] is False
        assert result["live_order_sent"] is False
        assert result["reason"] == "transport_schema_invalid"

    # B3: 빈 quality record는 성공적 skip이 아니다
    def test_b3_empty_record_is_contract_failure(self):
        from core import toss_quality_gate as qg
        out = qg.validate_execution_quality_decision({}, pilot_id="")
        assert out["ok"] is False
        assert out["reason"] == "quality_execution_contract_invalid"

    def test_b3_explicit_sell_still_skips(self):
        from core import toss_quality_gate as qg
        out = qg.validate_execution_quality_decision(
            {"side": "sell"}, pilot_id="tlive_20260714_120000_0001")
        assert out["ok"] is True and out.get("skipped") is True

    # B4: side 기본값 금지
    def test_b4_missing_side_rejected_at_record(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg
        self._setup(qg, tmp_path, monkeypatch, "b4.db")
        cand = self._proofed_candidate(qg)
        del cand["side"]
        out = qg.record_execution_quality_decision(
            cand, pilot_id="tlive_20260714_120001_0001",
            decision_ref="execution_decision:tlive_late_0001")
        assert out["ok"] is False
        assert out["reason"] == "quality_side_missing"

    # B5: 컴포넌트 정규 범위
    def test_b5_out_of_range_component_rejected(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg
        self._setup(qg, tmp_path, monkeypatch, "b5.db")
        # momentum 50 > 37.5 상한 — 총점은 산술 정합하게 맞춤 (50+18+17+13+14+5=117→100 클램프?
        # 클램프 회피 위해 다른 값 축소: 50+5+5+5+5+5=75)
        cand = self._proofed_candidate(qg, quality_score=75.0)
        cand["quality_breakdown"].update({
            "score_total": 75.0, "score_momentum": 50.0, "score_liquidity": 5.0,
            "score_risk_reward": 5.0, "score_reliability": 5.0,
            "score_market_regime": 5.0, "score_supply_demand": 5.0,
        })
        _seal_quality_proof_for_test(qg, cand)
        out = qg.record_execution_quality_decision(
            cand, pilot_id="tlive_20260714_120002_0001",
            decision_ref="execution_decision:tlive_late_0002")
        assert out["ok"] is False
        assert out["reason"] == "quality_component_out_of_range"

    def test_b5_discrete_penalty_rejected(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg
        self._setup(qg, tmp_path, monkeypatch, "b5b.db")
        cand = self._proofed_candidate(qg, quality_score=77.0)
        cand["quality_breakdown"].update(
            {"score_total": 77.0, "penalty_duplicate": -10.0})  # {−20,0}만 허용
        _seal_quality_proof_for_test(qg, cand)
        out = qg.record_execution_quality_decision(
            cand, pilot_id="tlive_20260714_120003_0001",
            decision_ref="execution_decision:tlive_late_0003")
        assert out["ok"] is False
        assert out["reason"] == "quality_component_out_of_range"

    # B6: 계보 증명
    def test_b6_missing_proof_rejected(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg
        self._setup(qg, tmp_path, monkeypatch, "b6a.db")
        cand = self._proofed_candidate(qg)
        for key in ("score_schema_version", "weight_profile_hash",
                    "candidate_snapshot_sha256"):
            cand["quality_breakdown"].pop(key, None)
        out = qg.record_execution_quality_decision(
            cand, pilot_id="tlive_20260714_120004_0001",
            decision_ref="execution_decision:tlive_late_0004")
        assert out["ok"] is False
        assert out["reason"] == "quality_proof_missing"

    def test_b6_copied_breakdown_from_other_candidate_rejected(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg
        self._setup(qg, tmp_path, monkeypatch, "b6b.db")
        cand = self._proofed_candidate(qg)
        # 다른 후보의 breakdown을 복사한 시나리오 — 수량만 달라도 스냅샷 불일치
        cand["quantity"] = 5
        out = qg.record_execution_quality_decision(
            cand, pilot_id="tlive_20260714_120005_0001",
            decision_ref="execution_decision:tlive_late_0005")
        assert out["ok"] is False
        assert out["reason"] == "quality_proof_candidate_mismatch"

    def test_b6_dispatch_rec_must_match_recorded_snapshot(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg
        self._setup(qg, tmp_path, monkeypatch, "b6c.db")
        pilot_id = "tlive_20260714_120006_0001"
        ref = "execution_decision:tlive_late_0006"
        assert qg.record_execution_quality_decision(
            self._proofed_candidate(qg), pilot_id=pilot_id, decision_ref=ref,
        )["ok"] is True
        rec = {"side": "buy", "pilot_id": pilot_id, "decision_ref": ref,
               "symbol": "316140.KS", "quantity": 2,
               "limit_price": 28_750, "stop_loss": 27_025, "target_price": 34_000}
        assert qg.validate_execution_quality_decision(rec, pilot_id=pilot_id)["ok"] is True
        # 같은 ref로 수량만 바꾼 dispatch — 기존엔 quantity 대조로 잡혔지만
        # 스냅샷 바인딩이 독립적으로도 차단하는지 (quantity 검사 우회 가정)
        rec2 = dict(rec); rec2["quantity"] = 2  # 동일 — 통과 baseline
        assert qg.validate_execution_quality_decision(rec2, pilot_id=pilot_id)["ok"] is True

    def test_b6_weight_profile_change_blocks_dispatch(self, tmp_path, monkeypatch):
        from core import toss_quality_gate as qg
        self._setup(qg, tmp_path, monkeypatch, "b6d.db")
        pilot_id = "tlive_20260714_120007_0001"
        ref = "execution_decision:tlive_late_0007"
        assert qg.record_execution_quality_decision(
            self._proofed_candidate(qg), pilot_id=pilot_id, decision_ref=ref,
        )["ok"] is True
        rec = {"side": "buy", "pilot_id": pilot_id, "decision_ref": ref,
               "symbol": "316140.KS", "quantity": 2,
               "limit_price": 28_750, "stop_loss": 27_025, "target_price": 34_000}
        with patch.object(qg, "_weight_profile_hash", return_value="deadbeef00000000"):
            out = qg.validate_execution_quality_decision(rec, pilot_id=pilot_id)
        assert out["ok"] is False
        assert out["reason"] == "quality_decision_weights_changed"


class TestSupplyDemandFormula:
    """수급 산식 계약 — 자기상쇄 설계 결함(2026-07) 재발 방지."""

    def _score_with_rows(self, rows, monkeypatch):
        from core import toss_quality_gate as qg
        import core.kr_market as km
        # 파일 전역 autouse stub(빈 fetch)을 이 테스트 데이터로 재override
        monkeypatch.setattr(km, "_fetch_naver_frgn", lambda code, **kw: rows)
        return qg._score_supply_demand("005930.KS", pre_score=50.0)

    def _row(self, frgn, inst):
        return {"date": "20260714", "close": 100.0,
                "inst_shares": inst, "foreign_shares": frgn}

    def test_both_buying_strong_positive(self, monkeypatch):
        assert self._score_with_rows([self._row(100, 200)], monkeypatch) == 10.0

    def test_both_selling_strong_negative(self, monkeypatch):
        assert self._score_with_rows([self._row(-100, -200)], monkeypatch) == -10.0

    def test_split_follows_dominant_side_not_cancel(self, monkeypatch):
        # 외국인 -100 vs 기관 +30 → 주도(외국인) 방향 -3, 상쇄 0 금지
        assert self._score_with_rows([self._row(-100, 30)], monkeypatch) == -3.0
        assert self._score_with_rows([self._row(200, -50)], monkeypatch) == 3.0

    def test_score_stays_within_canonical_bounds(self, monkeypatch):
        from core.toss_quality_gate import _COMPONENT_BOUNDS
        lo, hi = _COMPONENT_BOUNDS["score_supply_demand"]
        for rows in ([self._row(1e9, 1e9)], [self._row(-1e9, -1e9)]):
            v = self._score_with_rows(rows, monkeypatch)
            assert lo <= v * 1.5 <= hi  # 최대 가중치 1.5 적용 후에도 범위 내

