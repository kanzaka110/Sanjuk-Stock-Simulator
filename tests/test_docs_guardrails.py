"""토스 자동투자 future plan 문서 가드 테스트 (5단계).

문서만 추가됐고 실제 토스 API/주문 코드가 새로 생기지 않았음을 보장한다.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
DOC = ROOT / "docs" / "toss_auto_invest_future_plan.md"


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


# 6) 실제 토스 API 코드 파일이 새로 생기지 않음
def test_no_toss_code_files():
    matches = list((ROOT / "core").glob("toss_*.py"))
    matches += list((ROOT / "web").glob("toss_*.py"))
    assert matches == [], f"토스 코드 파일이 생성됨(문서만 허용): {matches}"


# 7) POST/PUT/DELETE route가 추가되지 않음
def test_no_write_routes_added():
    app_code = (ROOT / "web" / "app.py").read_text(encoding="utf-8")
    for verb in ("POST", "PUT", "DELETE"):
        assert verb not in app_code, f"app.py에 {verb} 핸들러 추가 — write route 위반"


# 보강) 이번 변경에 토스 API 코드 파일이 git에 새로 추가되지 않았는지 확인
def test_git_no_new_toss_code():
    out = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout + subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout
    bad = [ln for ln in out.splitlines()
           if "toss" in ln.lower() and ln.endswith(".py")]
    assert bad == [], f"토스 파이썬 파일 변경/추가 감지: {bad}"
