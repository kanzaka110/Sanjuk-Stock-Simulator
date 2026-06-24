"""토스 자동투자 가드레일 테스트.

정책:
- Toss 계좌는 기존 포트폴리오와 분리된 실전 AI 자동거래 계좌
- read-only client/probe/dashboard GET endpoint 허용
- 주문/매수/매도/정정/취소/자동거래 실행은 금지
- Toss 값을 기존 /api/portfolio 총액/원금/수익률에 합산 금지
"""

import inspect
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
DOC = ROOT / "docs" / "toss_auto_invest_future_plan.md"

# ─── 허용된 Toss 파일 목록 ─────────────────────────
ALLOWED_TOSS_FILES = {
    "core/toss_client.py",
    "core/toss_automation_guard.py",
    "core/toss_paper_trading.py",
    "core/toss_candidate_builder.py",
    "core/toss_decision_context.py",
    "core/toss_cross_check.py",
    "config/toss_automation.py",
    "tools/probe_toss_account.py",
    "tests/test_toss_probe.py",
    "tests/test_toss_client.py",
    "tests/test_toss_dashboard.py",
    "tests/test_toss_automation_config.py",
    "tests/test_toss_automation_guard.py",
    "tests/test_toss_paper_trading.py",
    "tests/test_toss_decision_context.py",
    "tests/test_toss_cross_check.py",
    "core/toss_order_preview.py",
    "core/toss_paper_ledger.py",
    "tests/test_toss_order_preview.py",
    "tests/test_toss_paper_ledger.py",
    "core/toss_paper_telegram.py",
    "tests/test_toss_paper_telegram.py",
    "tests/test_telegram_bot_callback_wiring.py",
    "core/toss_paper_telegram_send.py",
    "tests/test_toss_paper_telegram_send.py",
    "scripts/send_toss_paper_preview_test.py",
    "core/toss_paper_performance.py",
    "tests/test_toss_paper_performance.py",
    "core/toss_paper_policy.py",
    "tests/test_toss_paper_policy.py",
    "tests/test_toss_paper_currency_sizing.py",
    "scripts/cleanup_toss_paper_ledger.py",
    "tests/test_toss_paper_cleanup.py",
    "core/toss_live_pilot_policy.py",
    "core/toss_live_pilot_preview.py",
    "core/toss_live_pilot_ledger.py",
    "core/toss_live_pilot_adapter.py",
    "tests/test_toss_live_pilot_policy.py",
    "tests/test_toss_live_pilot_preview.py",
    "tests/test_toss_live_pilot_guardrails.py",
    "tests/test_toss_live_pilot_adapter.py",
    "tests/test_toss_live_pilot_payload.py",
    "core/toss_live_pilot_telegram.py",
    "tests/test_toss_live_pilot_telegram.py",
    "tests/test_toss_live_pilot_callback.py",
    "tests/test_toss_live_pilot_script.py",
    "scripts/send_toss_live_pilot_preview_test.py",
    "tests/test_toss_live_pilot_live_adapter.py",
    "tests/test_toss_live_pilot_live_policy.py",
    "tests/test_toss_live_pilot_live_callback.py",
    "core/toss_live_pilot_verification.py",
    "scripts/record_hermes_live_pilot_verification.py",
    "tests/test_toss_live_pilot_verification.py",
    "tests/test_toss_live_pilot_hermes_gate.py",
    "tests/test_toss_live_pilot_verification_script.py",
    "core/toss_live_transport.py",
    "tests/test_toss_live_pilot_buy_only.py",
    "tests/test_toss_live_transport_config.py",
    "scripts/cleanup_toss_live_pilot_verifications.py",
    "tests/test_toss_live_pilot_verification_cleanup.py",
    "core/toss_live_pilot_hermes_bridge.py",
    "tests/test_toss_live_pilot_hermes_bridge.py",
    "tests/test_toss_live_pilot_hermes_message.py",
    "tests/test_record_hermes_verification_from_block.py",
    "core/toss_live_pilot_events.py",
    "tests/test_toss_live_pilot_events.py",
    "tests/test_toss_live_pilot_event_api.py",
    "tests/test_toss_live_transport_schema.py",
    "tests/test_toss_live_transport_dry_run.py",
    "core/toss_live_order_http.py",
    "tests/test_toss_live_transport_live_http.py",
    "tests/test_toss_live_pilot_event_hygiene.py",
    "scripts/reclassify_toss_live_pilot_artifacts.py",
    "tests/test_toss_live_pilot_transport_injection.py",
}


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


# 1) 문서 존재
def test_doc_exists():
    assert DOC.exists(), "toss_auto_invest_future_plan.md 미존재"


# 2) 주문 구현 금지 문구
def test_doc_states_no_order_impl():
    html = _doc()
    assert ("실제 주문 구현 금지" in html) or ("주문 endpoint 구현 금지" in html), \
        "주문 구현 금지 문구 없음"


# 3) 삼성/토스 계좌 분리 명시
def test_doc_account_separation():
    html = _doc()
    assert "삼성증권" in html and "토스증권" in html, "계좌 분리 명시 없음"


# 4) 1,000만원 실험 계좌 규모 명시
def test_doc_experiment_size():
    html = _doc()
    assert ("1,000만원" in html) or ("1000만원" in html), "실험 계좌 규모 명시 없음"


# 5) read-only / paper trading / approval gate 포함
def test_doc_phase_keywords():
    html = _doc()
    assert "read-only" in html.lower(), "read-only 단계 없음"
    assert "paper trading" in html.lower(), "paper trading 단계 없음"
    assert ("approval gate" in html.lower()) or ("승인" in html), "approval gate 단계 없음"


# 6) Toss 코드가 read-only만 포함하는지 검증
def test_toss_code_is_read_only_only():
    """core/toss_client.py에 주문/변경 관련 코드가 없는지 검증."""
    client = ROOT / "core" / "toss_client.py"
    if not client.exists():
        return  # 아직 없으면 통과
    source = client.read_text(encoding="utf-8")

    # POST는 oauth2/token만 허용
    post_lines = [l for l in source.splitlines() if re.search(r"\bPOST\b", l, re.IGNORECASE)]
    bad_posts = [l for l in post_lines
                 if not any(kw in l.lower() for kw in ("oauth2", "token", "requests.post"))]
    assert not bad_posts, f"toss_client.py에 비인증 POST: {bad_posts}"

    # PUT/DELETE/PATCH 금지
    for verb in ("PUT", "DELETE", "PATCH"):
        assert verb not in source, f"toss_client.py에 {verb} 사용 — write 금지"

    # 주문 관련 함수명/키워드 금지
    for forbidden in ("buy", "sell", "cancel"):
        matches = re.findall(rf"\b{forbidden}\b", source, re.IGNORECASE)
        assert not matches, f"toss_client.py에 '{forbidden}' 키워드"


# 7) POST/PUT/DELETE route가 app.py에 추가되지 않음
def test_no_write_routes_added():
    app_code = (ROOT / "web" / "app.py").read_text(encoding="utf-8")
    for verb in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
        assert verb not in app_code, f"app.py에 {verb} 핸들러 추가 — write route 위반"


# 8) 허용 목록 밖의 Toss 파이썬 파일이 없는지
def test_no_toss_trading_or_write_paths():
    """허용 목록 밖에 toss 관련 .py 파일이 git에 추가되지 않았는지."""
    out = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout + subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout
    toss_files = [ln for ln in out.splitlines()
                  if "toss" in ln.lower() and ln.endswith(".py")]
    unexpected = [f for f in toss_files if f not in ALLOWED_TOSS_FILES]
    assert unexpected == [], f"허용 목록 밖 토스 파일: {unexpected}"


# 9) 기존 portfolio에 Toss 합산 없음
def test_portfolio_not_contaminated_by_toss():
    """_fetch_portfolio_raw에 toss 참조가 없는지."""
    dd_path = ROOT / "core" / "dashboard_data.py"
    source = dd_path.read_text(encoding="utf-8")
    # _fetch_portfolio_raw 함수만 추출
    match = re.search(
        r"def _fetch_portfolio_raw\(.*?\n(?=\ndef |\Z)",
        source, re.DOTALL,
    )
    if match:
        fn_src = match.group()
        assert "toss" not in fn_src.lower(), \
            "_fetch_portfolio_raw에 toss 참조 — 합산 오염 위험"


# 10) .env, .claude/settings.json이 git staged 아닌지
def test_no_secrets_staged():
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout
    for forbidden in (".env", ".claude/settings.json"):
        assert forbidden not in staged.splitlines(), \
            f"{forbidden}이 git staged — 커밋 금지"
