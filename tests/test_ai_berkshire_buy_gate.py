"""tests/test_ai_berkshire_buy_gate.py

AI Berkshire ③단계 — 자동 BUY 질적 게이트 (avoid_only 1단계).

이번 단계 규칙:
- strict checklist가 있는 항목은 fail/gray_zone/unknown 및 expired/invalid에서 fail-closed
- checklist가 없는 legacy 항목은 기존 avoid-only 호환을 유지
- unscored / score 파일 없음·파손은 진단(research_status)만 남긴다
- hold / protect / trim / sell_to_fund 는 BUY 의미를 자동 결정하지 않는다 → reviewed_non_avoid
- SELL 경로(sell_to_fund 자동매도, exit sell)는 이 게이트의 영향을 받지 않는다

게이트는 두 층 모두에 있다:
1. dashboard_data 후보 정규화 (/api/toss/buy-candidates)
2. toss_autonomous_pipeline.process_candidate 의 BUY dispatch 직전 독립 재검사
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import core.ai_berkshire_toss as abt
import core.toss_autonomous_pipeline as tap
from core import dashboard_data as dd
from core import discovery_candidates as disc
from core.discovery_candidates import DiscoverySections, NewCandidate


def _seal_quality_proof_for_test(qg, candidate):
    candidate.setdefault("side", "buy")
    breakdown = candidate["quality_breakdown"]
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
        breakdown, schema_version=qg.QUALITY_SCORE_SCHEMA_VERSION,
        weight_hash=weight_hash,
    )
    assert breakdown["score_breakdown_sha256"]
    assert qg.attach_quality_proof(candidate) is True


@pytest.fixture(autouse=True)
def _isolated_quality_db(tmp_path, monkeypatch):
    from core import toss_quality_gate as qg

    monkeypatch.setattr(qg, "_outcomes_db_path", lambda: tmp_path / "quality.db")
    qg._outcomes_schema_created = False
    yield
    qg._outcomes_schema_created = False


_FRESH = {
    "as_of": "2026-07-10",
    "valid_until": "2099-12-31",
    "thesis": "test thesis",
    "red_lines": ["test red line"],
    "source_urls": ["https://example.com/ir"],
}


def _item(classification, **overrides):
    base = {"classification": classification, **_FRESH}
    base.update(overrides)
    return base


def _scores(**items):
    return {"version": "ai_berkshire_toss_v2", "read_only": True, "items": dict(items)}


def _strict_scores(**items):
    return {
        "version": "ai_berkshire_toss_v2",
        "strict_buy_gate_version": 1,
        "read_only": True,
        "items": dict(items),
    }


_AVOID_SYM = "000660.KS"


# ═══════════════════════════════════════════════════════════════
# 1. helper: evaluate_ai_berkshire_buy_gate
# ═══════════════════════════════════════════════════════════════

def test_fresh_avoid_buy_is_blocked():
    """1. valid/fresh avoid BUY는 helper가 buy_block=true."""
    scores = _scores(**{_AVOID_SYM: _item("avoid", name="SK하이닉스")})
    gate = abt.evaluate_ai_berkshire_buy_gate(_AVOID_SYM, scores=scores,
                                              as_of_date="2026-07-15")
    assert gate["buy_block"] is True
    assert gate["buy_reason"] == "ai_berkshire_avoid"
    assert gate["classification"] == "avoid"
    assert gate["stored_classification"] == "avoid"
    assert gate["freshness_valid"] is True
    assert gate["research_status"] == "ok"


def test_gate_returns_all_documented_fields():
    scores = _scores(**{_AVOID_SYM: _item("avoid", name="SK하이닉스", confidence="high")})
    gate = abt.evaluate_ai_berkshire_buy_gate(_AVOID_SYM, scores=scores,
                                              as_of_date="2026-07-15")
    for key in ("symbol", "stored_classification", "classification", "freshness_valid",
                "thesis_expired", "freshness_issues", "buy_block", "buy_reason",
                "research_status", "thesis", "red_lines", "confidence", "source_urls"):
        assert key in gate, key
    assert gate["symbol"] == _AVOID_SYM
    assert gate["confidence"] == "high"
    assert gate["source_urls"] == ["https://example.com/ir"]


def test_gate_matches_bare_code_and_suffixed_key():
    scores = _scores(**{"000660.KS": _item("avoid")})
    assert abt.evaluate_ai_berkshire_buy_gate("000660", scores=scores,
                                              as_of_date="2026-07-15")["buy_block"] is True


def test_gate_does_not_mutate_input_rows():
    raw = _item("avoid", name="SK하이닉스")
    scores = _scores(**{_AVOID_SYM: raw})
    before = dict(raw)
    abt.evaluate_ai_berkshire_buy_gate(_AVOID_SYM, scores=scores, as_of_date="2026-07-15")
    assert raw == before


def test_non_avoid_classes_are_reviewed_non_avoid_not_blocked():
    """4. checklist가 없는 기존 hold/protect/trim/sell_to_fund는 호환 유지."""
    for cls in ("hold", "protect", "trim", "sell_to_fund"):
        scores = _scores(**{_AVOID_SYM: _item(cls)})
        gate = abt.evaluate_ai_berkshire_buy_gate(_AVOID_SYM, scores=scores,
                                                  as_of_date="2026-07-15")
        assert gate["buy_block"] is False, cls
        assert gate["buy_reason"] == "reviewed_non_avoid", cls
        assert gate["research_status"] == "ok", cls
        assert gate["buy_checklist_status"] is None


def test_explicit_fail_and_gray_zone_checklists_block_buy():
    """신규 staging의 fail/gray_zone은 classification과 독립적으로 BUY 차단."""
    cases = (("hold", "gray_zone"), ("trim", "fail"))
    for classification, checklist in cases:
        scores = _scores(**{
            _AVOID_SYM: _item(classification, buy_checklist_status=checklist)
        })
        gate = abt.evaluate_ai_berkshire_buy_gate(
            _AVOID_SYM, scores=scores, as_of_date="2026-07-15")
        assert gate["buy_block"] is True
        assert gate["buy_reason"] == f"ai_berkshire_buy_checklist_{checklist}"
        assert gate["buy_checklist_status"] == checklist
        assert gate["classification"] == classification


def test_explicit_pass_checklist_does_not_grant_or_block_by_itself():
    scores = _scores(**{
        _AVOID_SYM: _item("hold", buy_checklist_status="pass")
    })
    gate = abt.evaluate_ai_berkshire_buy_gate(
        _AVOID_SYM, scores=scores, as_of_date="2026-07-15")
    assert gate["buy_block"] is False
    assert gate["buy_reason"] == "reviewed_non_avoid"
    assert gate["buy_checklist_status"] == "pass"


def test_strict_schema_blocks_missing_or_unknown_classification():
    missing = _item("hold", buy_checklist_status="pass")
    missing.pop("classification")
    cases = (missing, _item("mystery", buy_checklist_status="pass"))
    for raw in cases:
        gate = abt.evaluate_ai_berkshire_buy_gate(
            _AVOID_SYM,
            scores=_strict_scores(**{_AVOID_SYM: raw}),
            as_of_date="2026-07-15",
        )
        assert gate["buy_block"] is True
        assert gate["buy_reason"] == "ai_berkshire_strict_classification_invalid"


def test_strict_schema_blocks_missing_null_blank_or_unknown_checklist():
    missing = object()
    for checklist in (missing, None, "", "maybe"):
        raw = _item("hold", strict_buy_gate=True)
        if checklist is not missing:
            raw["buy_checklist_status"] = checklist
        gate = abt.evaluate_ai_berkshire_buy_gate(
            _AVOID_SYM,
            scores=_strict_scores(**{_AVOID_SYM: raw}),
            as_of_date="2026-07-15",
        )
        assert gate["buy_block"] is True
        assert gate["buy_reason"] in {
            "ai_berkshire_buy_checklist_missing",
            "ai_berkshire_buy_checklist_unknown",
        }


def test_strict_schema_rejects_basic_and_week_iso_dates():
    for bad_date in ("20260710", "2026-W28-5"):
        raw = _item("hold", as_of=bad_date, buy_checklist_status="pass")
        gate = abt.evaluate_ai_berkshire_buy_gate(
            _AVOID_SYM,
            scores=_strict_scores(**{_AVOID_SYM: raw}),
            as_of_date="2026-07-15",
        )
        assert gate["buy_block"] is True
        assert "invalid_as_of" in gate["freshness_issues"]


def test_expired_strict_checklist_blocks_buy_fail_closed():
    scores = _scores(**{
        _AVOID_SYM: _item(
            "hold", valid_until="2026-07-01", buy_checklist_status="fail")
    })
    gate = abt.evaluate_ai_berkshire_buy_gate(
        _AVOID_SYM, scores=scores, as_of_date="2026-07-15")
    assert gate["buy_block"] is True
    assert gate["buy_reason"] == "ai_berkshire_strict_thesis_expired"
    assert gate["research_status"] == "expired"


def test_invalid_strict_checklist_blocks_buy_fail_closed():
    scores = _scores(**{
        _AVOID_SYM: _item(
            "hold", source_urls=[], buy_checklist_status="pass")
    })
    gate = abt.evaluate_ai_berkshire_buy_gate(
        _AVOID_SYM, scores=scores, as_of_date="2026-07-15")
    assert gate["buy_block"] is True
    assert gate["buy_reason"] == "ai_berkshire_strict_thesis_invalid"
    assert gate["research_status"] == "invalid"


def test_unknown_strict_checklist_value_blocks_buy_fail_closed():
    scores = _scores(**{
        _AVOID_SYM: _item("hold", buy_checklist_status="unexpected")
    })
    gate = abt.evaluate_ai_berkshire_buy_gate(
        _AVOID_SYM, scores=scores, as_of_date="2026-07-15")
    assert gate["buy_block"] is True
    assert gate["buy_reason"] == "ai_berkshire_buy_checklist_unknown"


def test_expired_legacy_item_keeps_migration_compatibility():
    scores = _scores(**{
        _AVOID_SYM: _item("hold", valid_until="2026-07-01")
    })
    gate = abt.evaluate_ai_berkshire_buy_gate(
        _AVOID_SYM, scores=scores, as_of_date="2026-07-15")
    assert gate["buy_block"] is False
    assert gate["buy_reason"] == "ai_berkshire_thesis_expired"
    assert gate["research_status"] == "expired"


def test_unscored_symbol_is_needs_research_without_block():
    """5. unscored → needs_research 진단, 예외 없음, 차단 없음."""
    scores = _scores(**{_AVOID_SYM: _item("avoid")})
    gate = abt.evaluate_ai_berkshire_buy_gate("ZZZZ", scores=scores, as_of_date="2026-07-15")
    assert gate["buy_block"] is False
    assert gate["research_status"] == "needs_research"
    assert gate["buy_reason"] == "ai_berkshire_unscored"
    assert gate["classification"] is None
    assert gate["stored_classification"] is None


def test_missing_and_broken_score_file_is_needs_research_without_exception():
    """5. score 파일 누락/파손 → 예외 없이 needs_research."""
    for broken in ({}, {"items": {}}, {"items": ["not", "a", "dict"]}, None):
        with patch.object(abt, "load_ai_berkshire_scores", return_value={}):
            gate = abt.evaluate_ai_berkshire_buy_gate(_AVOID_SYM, scores=broken,
                                                      as_of_date="2026-07-15")
        assert gate["buy_block"] is False, repr(broken)
        assert gate["research_status"] == "needs_research", repr(broken)
        assert gate["buy_reason"] in (
            "ai_berkshire_scores_unavailable", "ai_berkshire_unscored")


def test_load_failure_does_not_raise():
    with patch.object(abt, "load_ai_berkshire_scores", side_effect=OSError("disk")):
        gate = abt.evaluate_ai_berkshire_buy_gate(_AVOID_SYM, as_of_date="2026-07-15")
    assert gate["buy_block"] is False
    assert gate["research_status"] == "needs_research"


def test_expired_avoid_is_not_blocked_and_marked_expired():
    """6. expired → research_status='expired', 하드 차단 없음."""
    scores = _scores(**{_AVOID_SYM: _item("avoid", valid_until="2026-07-01")})
    gate = abt.evaluate_ai_berkshire_buy_gate(_AVOID_SYM, scores=scores,
                                              as_of_date="2026-07-15")
    assert gate["buy_block"] is False
    assert gate["research_status"] == "expired"
    assert gate["buy_reason"] == "ai_berkshire_thesis_expired"
    assert gate["thesis_expired"] is True
    assert gate["stored_classification"] == "avoid"
    assert gate["classification"] == "gray_zone"


def test_structurally_invalid_avoid_is_not_blocked_and_marked_invalid():
    """6. invalid(근거 불량) → research_status='invalid', 차단 없음, expired와 분리."""
    scores = _scores(**{_AVOID_SYM: _item("avoid", source_urls=[])})
    gate = abt.evaluate_ai_berkshire_buy_gate(_AVOID_SYM, scores=scores,
                                              as_of_date="2026-07-15")
    assert gate["buy_block"] is False
    assert gate["research_status"] == "invalid"
    assert gate["buy_reason"] == "ai_berkshire_thesis_invalid"
    assert gate["thesis_expired"] is False
    assert "missing_source_urls" in gate["freshness_issues"]


def test_fresh_gray_zone_is_invalid_research_status_without_block():
    scores = _scores(**{_AVOID_SYM: _item("gray_zone")})
    gate = abt.evaluate_ai_berkshire_buy_gate(_AVOID_SYM, scores=scores,
                                              as_of_date="2026-07-15")
    assert gate["buy_block"] is False
    assert gate["research_status"] == "invalid"
    assert gate["buy_reason"] == "ai_berkshire_gray_zone"


# ═══════════════════════════════════════════════════════════════
# 2. dashboard 후보 정규화 게이트
# ═══════════════════════════════════════════════════════════════

def _new_cand(ticker, name, market="KR", price=50_000, score=88):
    return NewCandidate(
        ticker=ticker, name=name, market=market, price=float(price),
        score=score, idea=f"{name} 신규 발굴 아이디어",
        reasons=("거래대금 충분", "수급 개선"),
        target_price=round(price * 1.12, 2), stop_loss=round(price * 0.96, 2),
        risk_reward=3.0, change_pct=2.0, tags=("거래량급증",),
    )


def _sections(new=(), market="KR"):
    return DiscoverySections(
        holdings_management=(), watchlist_reeval=(),
        new_discovery=tuple(new), new_rejected=(), market=market,
    )


def _patch_dashboard(monkeypatch, sections, scores):
    monkeypatch.setattr(dd, "_cache", {}, raising=False)
    monkeypatch.setattr(disc, "_fallback_universe_candidates", lambda markets: [])
    monkeypatch.setattr(disc, "build_discovery_sections", lambda *a, **k: sections)
    monkeypatch.setattr(disc, "recent_recommended_tickers", lambda *a, **k: set())

    from core import toss_live_pilot_policy as tlp
    from core import toss_client as tc
    monkeypatch.setattr(tlp, "compute_toss_live_pilot_policy",
                        lambda *a, **k: {"max_order_krw": 500_000})
    monkeypatch.setattr(dd, "_cross_check_price_quality",
                        lambda sym, cur=None: {"quality": "unknown", "checks": []})
    monkeypatch.setattr(tc, "get_exchange_rate", lambda base="USD", quote="KRW": {"rate": 1500.0})
    monkeypatch.setattr("core.toss_readonly_snapshot.load_snapshot", lambda: {
        "ok": True,
        "status": "fresh",
        "usable_for_decisions": True,
    })
    monkeypatch.setattr(dd, "toss_account_summary", lambda: {
        "snapshot_status": "fresh",
        "snapshot_usable_for_decisions": True,
        "cash": {"krw": 10_000_000, "krw_native": 10_000_000, "usd": 10_000.0},
        "holdings_count": 0,
    })
    monkeypatch.setattr(dd, "_toss_holding_price_map", lambda: {})
    monkeypatch.setattr(dd, "_recent_toss_risk_sell_symbols", lambda *a, **k: {})
    monkeypatch.setattr(dd, "_pending_toss_order_symbols", lambda *a, **k: {})
    monkeypatch.setattr(abt, "load_ai_berkshire_scores", lambda *a, **k: scores)

    from core import toss_income_strategy as tis

    def positive_income(candidate, **kwargs):
        estimated = float(candidate.get("estimated_amount_krw") or 1)
        decision_expected = 12_000.0
        return {
            "version": "income_v2",
            "expected_pnl_model": "income_exit_cashflow_v2",
            "expected_pnl_scope": "next_realized_exit_only",
            "expected_pnl_krw": -decision_expected,
            "income_edge_ratio": -decision_expected / estimated,
            "decision_expected_pnl_model": "income_exit_lifecycle_v1",
            "decision_expected_pnl_scope": "full_position_threshold_exit",
            "decision_expected_pnl_krw": decision_expected,
            "decision_income_edge_ratio": decision_expected / estimated,
            "income_pass": True,
            "income_grade": "SMALL_INCOME_PASS",
            "income_block_reason": "",
            "income_block_label": "",
        }

    monkeypatch.setattr(tis, "compute_income_edge", positive_income)


def test_dashboard_avoid_candidate_is_hard_blocked(monkeypatch):
    """2. dashboard avoid 후보 → stock_agent_ready/executable_now false + hold_ai_berkshire_avoid."""
    sections = _sections(new=[_new_cand(_AVOID_SYM, "SK하이닉스")])
    _patch_dashboard(monkeypatch, sections, _scores(**{_AVOID_SYM: _item("avoid")}))

    result = dd.toss_buy_candidates_data(range_="today")
    item = next(i for i in result["items"] if i["symbol"] == _AVOID_SYM)

    assert item["ai_berkshire_buy_block"] is True
    assert item["ai_berkshire_buy_reason"] == "ai_berkshire_avoid"
    assert item["stock_agent_ready"] is False
    assert item["executable_now"] is False
    assert item["execution_status"] == "hold_ai_berkshire_avoid"
    assert "AI Berkshire" in item["block_reason"]
    assert "avoid" in item["block_reason"]


def test_dashboard_hold_candidate_keeps_existing_ready_result(monkeypatch):
    """4. checklist 없는 기존 hold는 ready 결과 유지."""
    sections = _sections(new=[_new_cand("000777.KS", "강한수입후보")])
    _patch_dashboard(monkeypatch, sections, _scores(**{"000777.KS": _item("hold")}))

    item = next(i for i in dd.toss_buy_candidates_data(range_="today")["items"]
                if i["symbol"] == "000777.KS")

    assert item["ai_berkshire_buy_block"] is False
    assert item["ai_berkshire_buy_reason"] == "reviewed_non_avoid"
    assert item["stock_agent_ready"] is True
    assert item["execution_status"] != "hold_ai_berkshire_avoid"


def test_dashboard_checklist_fail_candidate_is_hard_blocked(monkeypatch):
    sections = _sections(new=[_new_cand("000777.KS", "체크리스트실패후보")])
    scores = _scores(**{
        "000777.KS": _item("hold", buy_checklist_status="fail")
    })
    _patch_dashboard(monkeypatch, sections, scores)

    item = next(i for i in dd.toss_buy_candidates_data(range_="today")["items"]
                if i["symbol"] == "000777.KS")

    assert item["ai_berkshire_buy_block"] is True
    assert item["ai_berkshire_buy_reason"] == "ai_berkshire_buy_checklist_fail"
    assert item["ai_berkshire_buy_gate"]["buy_checklist_status"] == "fail"
    assert item["ai_berkshire_buy_gate"]["strict_buy_gate"] is True
    assert item["ai_berkshire_buy_gate"]["classification_valid"] is True
    assert item["ai_berkshire_buy_gate"]["version"] == "ai_berkshire_buy_gate_strict_v3"
    assert item["stock_agent_ready"] is False
    assert item["executable_now"] is False
    assert item["execution_status"] == "hold_ai_berkshire_buy_checklist"


def test_dashboard_unscored_candidate_keeps_ready_and_marks_needs_research(monkeypatch):
    """5. unscored는 차단하지 않고 needs_research 진단만."""
    sections = _sections(new=[_new_cand("000777.KS", "강한수입후보")])
    _patch_dashboard(monkeypatch, sections, _scores(**{_AVOID_SYM: _item("avoid")}))

    item = next(i for i in dd.toss_buy_candidates_data(range_="today")["items"]
                if i["symbol"] == "000777.KS")

    assert item["ai_berkshire_buy_block"] is False
    assert item["ai_berkshire_research_status"] == "needs_research"
    assert item["stock_agent_ready"] is True


def test_dashboard_broken_scores_file_blocks_ready_without_exception(monkeypatch):
    sections = _sections(new=[_new_cand("000777.KS", "강한수입후보")])
    _patch_dashboard(monkeypatch, sections, {})

    item = next(i for i in dd.toss_buy_candidates_data(range_="today")["items"]
                if i["symbol"] == "000777.KS")

    assert item["ai_berkshire_buy_block"] is True
    assert item["ai_berkshire_buy_reason"] == "ai_berkshire_scores_unavailable"
    assert item["ai_berkshire_research_status"] == "needs_research"
    assert item["stock_agent_ready"] is False
    assert item["executable_now"] is False


def test_dashboard_expired_and_invalid_keep_ready_with_distinct_status(monkeypatch):
    """6. expired/invalid research_status를 정확히 분리하고 기존 ready 유지."""
    cases = {
        "expired": _item("avoid", valid_until="2026-07-01"),
        "invalid": _item("avoid", thesis=""),
    }
    for expected_status, raw in cases.items():
        sections = _sections(new=[_new_cand("000777.KS", "강한수입후보")])
        _patch_dashboard(monkeypatch, sections, _scores(**{"000777.KS": raw}))

        item = next(i for i in dd.toss_buy_candidates_data(range_="today")["items"]
                    if i["symbol"] == "000777.KS")

        assert item["ai_berkshire_buy_block"] is False, expected_status
        assert item["ai_berkshire_research_status"] == expected_status
        assert item["stock_agent_ready"] is True, expected_status


# ═══════════════════════════════════════════════════════════════
# 3. pipeline 독립 재검사 (BUY dispatch 직전)
# ═══════════════════════════════════════════════════════════════

_POLICY_ON = {
    "mode": "autonomous_live_pilot",
    "autonomous_mode": True,
    "autonomous_kill_switch": False,
    "live_pilot_enabled": True,
    "requires_user_confirmation": False,
    "requires_second_confirmation": False,
    "all_live_gates_open": True,
    "env_live_pilot_enabled": True,
    "env_live_order_allowed": True,
    "env_live_adapter_enabled": True,
    "max_order_krw": 0,
    "blocked_symbols": [],
    "autonomous_allowed_sides": ["buy", "sell"],
    "side_mode": "BUY_SELL",
    "allowed_sides": ["buy", "sell"],
    "sell_allowed": True,
    "adapter_status": "enabled",
    "live_order_allowed": True,
    "live_transport_status": "configured",
}


def _candidate(symbol=_AVOID_SYM, side="buy", **kw):
    base = {
        "symbol": symbol, "side": side, "market": "KR", "currency": "KRW",
        "quantity": 10, "limit_price": 30_000.0,
        "stop_loss": 28_000, "target_price": 34_000,
        "stock_agent_ready": True, "executable_now": True,
        "quality_finalized": True, "income_execution_contract_valid": True,
        "missing_fields": [], "decision_bucket": "PASS_EXECUTE",
        "decision_reason": "quality pass", "quality_score": 88,
        "quality_breakdown": {
            "score_total": 88, "score_momentum": 20, "score_liquidity": 20,
            "score_risk_reward": 18, "score_reliability": 15,
            "score_market_regime": 15, "score_supply_demand": 0,
            "penalty_overheat": 0,
            "penalty_duplicate": 0, "penalty_event_risk": 0,
            "rr_ratio": 2.0, "regime": "강세장",
        },
        "score": 88, "risk_reward": 2.0,
        "income_strategy": {
            "income_pass": True,
            "income_grade": "INCOME_PASS",
            "expected_pnl_krw": 12_000,
            "income_edge_ratio": 0.02,
            "decision_expected_pnl_model": "income_exit_lifecycle_v1",
            "decision_expected_pnl_scope": "full_position_threshold_exit",
            "decision_expected_pnl_krw": 12_000,
            "decision_income_edge_ratio": 0.02,
        },
    }
    base.update(kw)
    income = base.get("income_strategy")
    if type(income) is dict:
        income.update({
            "planned_entry_price": base.get("limit_price"),
            "planned_stop_loss": base.get("stop_loss"),
            "planned_target_price": base.get("target_price"),
            "planned_quantity": base.get("quantity"),
        })
    if str(base.get("side") or "").lower() == "buy":
        from core import toss_quality_gate as _qg
        _seal_quality_proof_for_test(_qg, base)
    return base


def _pipeline_mocks(scores):
    return (
        patch.object(abt, "load_ai_berkshire_scores", return_value=scores),
        patch("core.toss_live_pilot_preview.build_live_pilot_preview",
              return_value={"ok": True, "symbol": _AVOID_SYM, "side": "buy",
                            "decision_ref": "execution_decision:tlive_test_1"}),
        patch("core.toss_live_pilot_ledger.record_live_pilot_preview",
              return_value={"ok": True, "pilot_id": "tlive_test_1"}),
        patch("core.toss_live_pilot_verification.create_verification_request",
              return_value={"verification_id": "hv_test_1", "status": "PENDING"}),
        patch("core.toss_live_pilot_verification.record_hermes_verification",
              return_value={"ok": True, "status": "PASS"}),
        patch("core.toss_live_pilot_hermes_bridge.build_default_hermes_verdict",
              return_value={"status": "PASS", "reasons": ["ok"], "checks": {}}),
        patch("core.toss_autonomous_finalizer.try_autonomous_finalize",
              return_value={"live_order_sent": False}),
    )


def test_pipeline_avoid_buy_never_reaches_preview_or_finalizer():
    """3. pipeline avoid BUY는 preview/finalizer/transport mock이 호출되지 않음."""
    scores = _scores(**{_AVOID_SYM: _item("avoid")})
    (m_scores, m_prev, m_ledger, m_req, m_rec, m_verdict, m_final) = _pipeline_mocks(scores)
    with m_scores, m_prev as preview, m_ledger as ledger, m_req as req, \
            m_rec as rec, m_verdict as verdict, m_final as final:
        r = tap.process_candidate(_candidate(), dict(_POLICY_ON))

    assert r["stage"] == "ai_berkshire_avoid_blocked"
    assert r["reason"] == "ai_berkshire_avoid"
    preview.assert_not_called()
    ledger.assert_not_called()
    req.assert_not_called()
    rec.assert_not_called()
    verdict.assert_not_called()
    final.assert_not_called()


def test_pipeline_checklist_gray_zone_never_reaches_preview_or_finalizer():
    scores = _scores(**{
        _AVOID_SYM: _item("hold", buy_checklist_status="gray_zone")
    })
    (m_scores, m_prev, m_ledger, m_req, m_rec, m_verdict, m_final) = _pipeline_mocks(scores)
    with m_scores, m_prev as preview, m_ledger as ledger, m_req as req, \
            m_rec as rec, m_verdict as verdict, m_final as final:
        r = tap.process_candidate(_candidate(), dict(_POLICY_ON))

    assert r["stage"] == "ai_berkshire_buy_blocked"
    assert r["reason"] == "ai_berkshire_buy_checklist_gray_zone"
    preview.assert_not_called()
    ledger.assert_not_called()
    req.assert_not_called()
    rec.assert_not_called()
    verdict.assert_not_called()
    final.assert_not_called()


def test_pipeline_sell_side_is_unaffected_by_buy_gate():
    """7. SELL pipeline은 avoid 판정에도 새 BUY 게이트 영향 없음."""
    scores = _scores(**{_AVOID_SYM: _item("avoid")})
    (m_scores, m_prev, m_ledger, m_req, m_rec, m_verdict, m_final) = _pipeline_mocks(scores)
    with m_scores, m_prev as preview, m_ledger, m_req, m_rec, m_verdict, m_final:
        r = tap.process_candidate(_candidate(side="sell", income_strategy={}),
                                  dict(_POLICY_ON), reason="auto_exit_sell")

    assert r["stage"] == "verdict_recorded"
    assert r["verdict"] == "PASS"
    preview.assert_called_once()


def test_pipeline_hold_buy_still_dispatches():
    """4. hold는 avoid_only 단계에서 기존 경로 유지."""
    scores = _scores(**{_AVOID_SYM: _item("hold")})
    (m_scores, m_prev, m_ledger, m_req, m_rec, m_verdict, m_final) = _pipeline_mocks(scores)
    with m_scores, m_prev as preview, m_ledger, m_req, m_rec, m_verdict, m_final:
        r = tap.process_candidate(_candidate(), dict(_POLICY_ON))

    assert r["stage"] == "verdict_recorded"
    preview.assert_called_once()


def test_pipeline_scores_unavailable_blocks_before_preview():
    (m_scores, m_prev, m_ledger, m_req, m_rec, m_verdict, m_final) = _pipeline_mocks({})
    with m_scores, m_prev as preview, m_ledger, m_req, m_rec, m_verdict, m_final:
        r = tap.process_candidate(_candidate(), dict(_POLICY_ON))

    assert r["stage"] == "ai_berkshire_buy_blocked"
    assert r["reason"] == "ai_berkshire_scores_unavailable"
    preview.assert_not_called()


def test_pipeline_unscored_symbol_with_available_scores_still_dispatches():
    scores = _scores(**{"OTHER": _item("hold")})
    (m_scores, m_prev, m_ledger, m_req, m_rec, m_verdict, m_final) = _pipeline_mocks(scores)
    with m_scores, m_prev as preview, m_ledger, m_req, m_rec, m_verdict, m_final:
        r = tap.process_candidate(_candidate(), dict(_POLICY_ON))

    assert r["stage"] == "verdict_recorded"
    preview.assert_called_once()


def test_pipeline_expired_avoid_buy_still_dispatches():
    """legacy expired avoid without strict marker remains compatibility-only."""
    scores = _scores(**{_AVOID_SYM: _item("avoid", valid_until="2000-01-01")})
    (m_scores, m_prev, m_ledger, m_req, m_rec, m_verdict, m_final) = _pipeline_mocks(scores)
    with m_scores, m_prev as preview, m_ledger, m_req, m_rec, m_verdict, m_final:
        r = tap.process_candidate(_candidate(), dict(_POLICY_ON))

    assert r["stage"] == "verdict_recorded"
    preview.assert_called_once()


def test_pipeline_gate_error_blocks_before_preview():
    with patch.object(abt, "evaluate_ai_berkshire_buy_gate", side_effect=RuntimeError("x")), \
            patch("core.toss_live_pilot_preview.build_live_pilot_preview",
                  return_value={"ok": True}) as preview, \
            patch("core.toss_live_pilot_ledger.record_live_pilot_preview",
                  return_value={"ok": True, "pilot_id": "p1"}), \
            patch("core.toss_live_pilot_verification.create_verification_request",
                  return_value={"verification_id": "v1"}), \
            patch("core.toss_live_pilot_verification.record_hermes_verification",
                  return_value={"ok": True}), \
            patch("core.toss_live_pilot_hermes_bridge.build_default_hermes_verdict",
                  return_value={"status": "PASS", "reasons": [], "checks": {}}):
        r = tap.process_candidate(_candidate(), dict(_POLICY_ON))

    assert r["stage"] == "ai_berkshire_buy_blocked"
    assert r["reason"] == "ai_berkshire_gate_error"
    preview.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# 4. SELL sell_to_fund 경로 회귀 (기존 게이트 불변)
# ═══════════════════════════════════════════════════════════════

def test_sell_to_fund_avoid_still_auto_sell_eligible():
    """7. SELL candidate는 새 BUY 게이트 영향 없음 — avoid는 여전히 자동매도 허용."""
    scores = _scores(**{_AVOID_SYM: _item("avoid")})
    rows = [{"symbol": _AVOID_SYM, "weakness_score": 10.0, "action": "sell_to_fund_candidate"}]
    out = abt.apply_berkshire_to_sell_to_fund(rows, scores=scores, as_of_date="2026-07-15")

    assert out[0]["auto_sell_eligible"] is True
    assert out[0]["auto_sell_block_reason"] is None
    assert "ai_berkshire_buy_block" not in out[0]
