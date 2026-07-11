from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.execution_candidate_red_team import (
    KST,
    VERSION,
    build_red_team_prompt,
    deterministic_checks,
    evaluate_execution_candidate,
    validate_staging_record,
)

AS_OF = datetime(2026, 7, 11, 13, 0, tzinfo=KST)


def _candidate(**overrides):
    row = {
        "symbol": "005930.KS",
        "name": "삼성전자",
        "side": "buy",
        "quantity": 2,
        "limit_price": 90000,
        "current_price": 90500,
        "stop_loss": 85000,
        "target_price": 101000,
        "invalidation": "HBM 고객 인증 지연과 메모리 가격 하락 동시 발생",
        "reason": "HBM 매출 확대와 메모리 업황 회복",
        "score": 78,
        "risk_reward": 2.2,
        "decision_bucket": "PASS_EXECUTE",
        "stock_agent_ready": True,
        "executable_now": True,
        "execution_status": "ready",
        "quote_age_sec": 60,
        "decision_ref": "pred:123",
        "source_signal": "stock_agent_ready",
    }
    row.update(overrides)
    return row


def _context(**overrides):
    row = {
        "thesis": "HBM 출하 확대와 범용 메모리 가격 회복이 이익 개선을 견인",
        "catalysts": ["분기 HBM 매출 증가", "DRAM 계약가격 상승"],
        "red_lines": ["고객 인증 지연", "재고 재증가"],
        "market_context": "반도체 변동성 확대",
        "fundamental_context": "이익 추정치 상향 여부 확인 필요",
        "technical_context": "진입가와 손절가가 명시됨",
        "known_evidence": ["공식 실적 발표 확인 필요"],
        "source_urls": ["https://example.com/ir"],
        "days_to_event": 10,
        "portfolio_risk": {"severity": "medium", "symbol_weight_pct": 10, "cluster_weight_pct": 35},
    }
    row.update(overrides)
    return row


def _ai(verdict="PASS", confidence=80, sources=1, breached=False):
    evidence = [
        {
            "claim": f"공식 근거 {i}",
            "url": f"https://example.com/source-{i}",
            "published_at": "2026-07-10",
            "source_type": "company_ir",
        }
        for i in range(sources)
    ]
    return {
        "review_signal": verdict,
        "confidence": confidence,
        "summary": "반증을 검토했으나 현재 논지를 즉시 무효화할 증거는 제한적",
        "strongest_bear_case": "HBM 인증 지연과 공급 증가가 가격 회복을 꺾을 수 있음",
        "thesis_assumptions": ["HBM 출하 증가", "DRAM 가격 유지"],
        "disconfirming_evidence": ["경쟁사 증설로 공급 부담 가능"],
        "missing_evidence": ["최신 고객별 HBM 매출 비중"],
        "red_lines": [{
            "condition": "고객 인증 지연",
            "status": "breached" if breached else "watch",
            "reason": "공식 일정 확인 필요",
        }],
        "scenarios": {"bull": "인증과 가격 상승", "base": "점진 회복", "bear": "인증 지연과 가격 하락"},
        "source_evidence": evidence,
        "next_checks": ["다음 실적 공시에서 HBM 매출 확인"],
    }


def _runner(payload):
    def run(prompt, *, model):
        assert model == "opus"
        assert "Red Team" in prompt
        return json.dumps(payload, ensure_ascii=False)
    return run


def test_deterministic_valid_candidate_has_no_block_or_review():
    result = deterministic_checks(_candidate(), _context())
    assert result["blocks"] == []
    assert result["reviews"] == []
    assert result["warnings"] == []


def test_deterministic_missing_decision_ref_is_trace_warning_only():
    result = deterministic_checks(_candidate(decision_ref=""), _context())
    assert result["blocks"] == []
    assert result["reviews"] == []
    assert result["warnings"] == ["decision_ref_missing"]


def test_deterministic_not_ready_and_bad_prices_block():
    result = deterministic_checks(
        _candidate(stock_agent_ready=False, decision_bucket="WATCH", stop_loss=91000, target_price=89000),
        _context(),
    )
    assert "candidate_not_execution_ready" in result["blocks"]
    assert "non_executable_bucket:WATCH" in result["blocks"]
    assert "stop_loss_not_below_buy_price" in result["blocks"]
    assert "target_not_above_buy_price" in result["blocks"]


def test_deterministic_berkshire_block_cannot_be_overridden():
    record = evaluate_execution_candidate(
        _candidate(ai_berkshire_buy_block=True),
        _context(),
        ai_runner=_runner(_ai("PASS")),
        as_of=AS_OF,
    )
    assert record["review_signal"] == "BLOCK"
    assert record["verdict_reason"] == "deterministic_block"
    assert "ai_berkshire_buy_block" in record["deterministic_checks"]["blocks"]


def test_ai_pass_with_counterevidence_passes():
    record = evaluate_execution_candidate(
        _candidate(), _context(), ai_runner=_runner(_ai("PASS")), as_of=AS_OF,
    )
    assert record["review_signal"] == "PASS"
    assert record["verdict_reason"] == "ai_pass_with_counterevidence"
    assert record["source_evidence"][0]["url"].startswith("https://")
    assert record["decision_ref"] == "pred:123"
    assert record["traceability_status"] == "direct"


def test_ai_pass_cannot_override_deterministic_review():
    record = evaluate_execution_candidate(
        _candidate(risk_reward=1.5), _context(), ai_runner=_runner(_ai("PASS")), as_of=AS_OF,
    )
    assert record["review_signal"] == "REVIEW"
    assert record["verdict_reason"] == "deterministic_review_not_overridden"


def test_ai_block_requires_confidence_two_sources_and_evidence():
    accepted = evaluate_execution_candidate(
        _candidate(), _context(), ai_runner=_runner(_ai("BLOCK", 80, 2, True)), as_of=AS_OF,
    )
    assert accepted["review_signal"] == "BLOCK"
    assert accepted["verdict_reason"] == "ai_block_evidence_threshold_met"

    downgraded = evaluate_execution_candidate(
        _candidate(), _context(), ai_runner=_runner(_ai("BLOCK", 69, 1, True)), as_of=AS_OF,
    )
    assert downgraded["review_signal"] == "REVIEW"
    assert downgraded["verdict_reason"] == "ai_block_downgraded_insufficient_evidence"


def test_invalid_source_is_removed_and_pass_downgraded():
    payload = _ai("PASS")
    payload["source_evidence"][0]["url"] = "javascript:alert(1)"
    record = evaluate_execution_candidate(
        _candidate(), _context(), ai_runner=_runner(payload), as_of=AS_OF,
    )
    assert record["source_evidence"] == []
    assert record["review_signal"] == "REVIEW"
    assert record["verdict_reason"] == "ai_pass_downgraded_missing_counterevidence"


def test_ai_error_and_disabled_fail_to_review():
    empty = evaluate_execution_candidate(
        _candidate(), _context(), ai_runner=lambda *_args, **_kwargs: "", as_of=AS_OF,
    )
    assert empty["review_signal"] == "REVIEW"
    assert empty["ai"]["error"] == "ai_empty_response"

    disabled = evaluate_execution_candidate(_candidate(), _context(), run_ai=False, as_of=AS_OF)
    assert disabled["review_signal"] == "REVIEW"
    assert disabled["verdict_reason"] == "ai_disabled"


def test_high_portfolio_cluster_risk_forces_review_not_auto_block():
    record = evaluate_execution_candidate(
        _candidate(),
        _context(portfolio_risk={"severity": "high", "cluster_weight_pct": 55}),
        ai_runner=_runner(_ai("PASS")),
        as_of=AS_OF,
    )
    assert record["review_signal"] == "REVIEW"
    assert "portfolio_cluster_risk:high" in record["deterministic_checks"]["reviews"]
    assert "cluster_weight_at_or_above_50pct" in record["deterministic_checks"]["reviews"]


def test_candidate_and_context_are_not_mutated():
    candidate = _candidate()
    context = _context()
    before_candidate = deepcopy(candidate)
    before_context = deepcopy(context)
    evaluate_execution_candidate(candidate, context, run_ai=False, as_of=AS_OF)
    assert candidate == before_candidate
    assert context == before_context


def test_prompt_contains_snapshot_but_no_order_execution_instruction():
    prompt = build_red_team_prompt(_candidate(), _context(), as_of=AS_OF.isoformat())
    assert "005930.KS" in prompt
    assert "논지의 가장 약한 가정" in prompt
    assert "주문을 실행해라" not in prompt


def test_staging_safety_contract_and_validator():
    record = evaluate_execution_candidate(_candidate(), _context(), run_ai=False, as_of=AS_OF)
    assert validate_staging_record(record) == []
    assert record["review_signal"] == "REVIEW"
    assert "verdict" not in record
    assert record["review_only"] is True
    assert record["operational_decision_unchanged"] is True
    assert record["advisory_only"] is True
    assert record["order_signal"] is False
    assert record["order_side_effects"] is False
    assert record["can_approve_order"] is False
    assert record["can_cancel_order"] is False
    assert record["can_send_order"] is False
    assert record["data_quality"]["ai_result_available"] is False

    unsafe = dict(record)
    unsafe["can_send_order"] = True
    assert "unsafe_contract:can_send_order" in validate_staging_record(unsafe)


def test_sensitive_unknown_fields_are_not_exposed_and_forbidden_modules_are_absent():
    candidate = _candidate(accountNo="sensitive-account", bearer="secret-token")
    context = _context(api_key="secret-key")
    record = evaluate_execution_candidate(candidate, context, run_ai=False, as_of=AS_OF)
    encoded = json.dumps(record, ensure_ascii=False)
    assert "sensitive-account" not in encoded
    assert "secret-token" not in encoded
    assert "secret-key" not in encoded

    source = (ROOT / "core" / "execution_candidate_red_team.py").read_text(encoding="utf-8")
    forbidden_imports = (
        "toss_autonomous_pipeline", "toss_autonomous_finalizer",
        "toss_live_pilot_verification", "toss_live_pilot_ledger",
        "toss_live_pilot_adapter", "toss_live_transport",
        "toss_live_order_http", "toss_order_watch",
    )
    assert all(f"from core.{name} import" not in source for name in forbidden_imports)


def test_review_id_is_deterministic_for_same_snapshot_and_time():
    a = evaluate_execution_candidate(_candidate(), _context(), run_ai=False, as_of=AS_OF)
    b = evaluate_execution_candidate(_candidate(), _context(), run_ai=False, as_of=AS_OF)
    assert a["review_id"] == b["review_id"]


def test_cli_writes_valid_atomic_staging_without_ai(tmp_path, capsys):
    from tools.execution_candidate_red_team_cli import main

    input_path = tmp_path / "candidate.json"
    output_dir = tmp_path / "staging"
    input_path.write_text(
        json.dumps({"candidate": _candidate(), "analysis_context": _context()}, ensure_ascii=False),
        encoding="utf-8",
    )
    code = main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--no-ai",
        "--as-of", AS_OF.isoformat(),
    ])
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["count"] == 1
    assert summary["order_side_effects"] is False
    output_path = Path(summary["records"][0]["output"])
    record = json.loads(output_path.read_text(encoding="utf-8"))
    assert validate_staging_record(record) == []
    assert list(output_path.parent.glob("*.tmp")) == []


def test_dashboard_reads_only_valid_staging_and_filters_symbol(tmp_path, monkeypatch):
    from core.dashboard_data import execution_red_team_staging_data

    root = tmp_path / "staging"
    day = root / "2026-07-11"
    day.mkdir(parents=True)
    samsung = evaluate_execution_candidate(_candidate(), _context(), run_ai=False, as_of=AS_OF)
    micron = evaluate_execution_candidate(
        _candidate(symbol="MU", name="Micron", decision_ref="pred:456"),
        _context(), run_ai=False, as_of=AS_OF,
    )
    (day / "samsung.json").write_text(json.dumps(samsung), encoding="utf-8")
    (day / "micron.json").write_text(json.dumps(micron), encoding="utf-8")
    (day / "broken.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("EXECUTION_RED_TEAM_STAGING_DIR", str(root))

    result = execution_red_team_staging_data(limit=10, symbol="MU")
    assert result["version"] == VERSION
    assert result["read_only"] is True
    assert result["review_only"] is True
    assert result["operational_decision_unchanged"] is True
    assert result["order_side_effects"] is False
    assert result["count"] == 1
    assert result["items"][0]["symbol"] == "MU"
    assert result["invalid_count"] == 1


def test_http_endpoint_is_get_only_staging_reader(tmp_path, monkeypatch):
    root = tmp_path / "staging"
    day = root / "2026-07-11"
    day.mkdir(parents=True)
    record = evaluate_execution_candidate(_candidate(), _context(), run_ai=False, as_of=AS_OF)
    (day / "record.json").write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setenv("EXECUTION_RED_TEAM_STAGING_DIR", str(root))

    from web.app import app

    client = TestClient(app)
    response = client.get("/api/toss/execution-red-team?limit=5&symbol=005930.KS")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["advisory_only"] is True
    assert payload["order_signal"] is False
    assert client.post("/api/toss/execution-red-team").status_code == 405
