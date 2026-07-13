from pathlib import Path


def test_execution_policy_note_is_account_aware():
    from core.telegram import _execution_policy_note

    toss = _execution_policy_note("Toss AI")
    samsung = _execution_policy_note("삼성 일반")
    assert "Hermes PASS 후 결정론 안전 게이트 자동 진행" in toss
    assert "승호 최종 승인" not in toss
    assert "승호 수동 승인" in samsung
    assert "자동 진행" not in samsung


def test_toss_discovery_candidate_has_no_user_final_approval_string():
    source = (
        Path(__file__).resolve().parents[1] / "core" / "discovery_candidates.py"
    ).read_text(encoding="utf-8")
    assert "Hermes PASS와 승호 최종 승인 필요" not in source
    assert "소액 조건부 관찰매수 후보 · Hermes PASS 후 결정론 안전 게이트 자동 진행" in source
