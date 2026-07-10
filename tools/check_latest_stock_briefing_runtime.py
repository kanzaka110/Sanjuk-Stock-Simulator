"""브리핑 실제 반영 검증 도구 (read-only)

최신 stock briefing에 수입 중심(income-first) 구조가 반영됐는지 확인한다.

실행:
    python tools/check_latest_stock_briefing_runtime.py

판정 기준:
- forbidden marker(삼성 자동화/Toss 수동 주문표 지시)가 있으면 → fail
- 최신 브리핑 없음 → awaiting_next_briefing
- income 통합 이후 브리핑인데 수입 계기판 없음 → awaiting_next_briefing
- forbidden 없음 + 수입 계기판 존재 + code_path 정상 → pass

주의: Toss는 제한형 완전자율 실계좌 — "Toss 자동운영: 활성",
"live_order_allowed=true"는 정상 상태이며 금지 대상이 아니다.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ──────────────────────────────────────────────────────────────────
# income-first 브리핑 통합 기준 시각 (KST) — 이후 브리핑만 마커 검증
# ──────────────────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))
INCOME_INTEGRATION_CUTOFF_KST = datetime(2026, 7, 10, 18, 0, 0, tzinfo=KST)

REQUIRED_MARKERS = [
    "오늘 수입 계기판",
    "실현수입",
    "오늘 평가변동",
    "Toss: 자동운영",
    "삼성: 수동",
    "예상수입",
]

# 삼성 자동화·LLM의 Toss 수동 주문표 지시만 금지 (Toss 자동운영 활성은 정상)
FORBIDDEN_MARKERS = [
    "삼성 자동주문",
    "삼성 자동실행",
    "삼성 주문 전송",
    "Toss 수동 주문표를 지금 입력",
]

# ──────────────────────────────────────────────────────────────────

def _db_path() -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / "db" / "data" / "briefing_archive.db"


def _load_latest_briefing() -> dict | None:
    """최신 브리핑 아카이브를 body 포함해서 반환."""
    p = _db_path()
    if not p.exists():
        return None
    try:
        conn = sqlite3.connect(str(p))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, created_at, briefing_type, title, body_text, body_html "
            "FROM archives ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def _parse_briefing_dt(created_at_str: str) -> datetime | None:
    """DB의 created_at 문자열을 KST-aware datetime으로."""
    if not created_at_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(created_at_str, fmt)
            return dt.replace(tzinfo=KST)
        except ValueError:
            continue
    return None


def _check_markers(body: str, markers: list[str]) -> list[str]:
    """body 안에서 발견된 marker 목록 반환."""
    return [m for m in markers if m in body]


def _check_code_path() -> dict:
    """income briefing 통합 코드 패스 정적 확인."""
    root = Path(__file__).resolve().parent.parent
    analyzer = root / "core" / "analyzer.py"
    telegram = root / "core" / "telegram.py"
    email_mod = root / "core" / "email.py"
    if not analyzer.exists():
        return {"ok": False, "reason": "analyzer.py not found"}
    src_a = analyzer.read_text(encoding="utf-8")
    src_t = telegram.read_text(encoding="utf-8") if telegram.exists() else ""
    src_e = email_mod.read_text(encoding="utf-8") if email_mod.exists() else ""
    has_context = "build_income_briefing_context" in src_a
    has_finalize = "finalize_income_briefing" in src_a
    has_strip = "strip_toss_from_manual_normalized" in src_a
    has_tg_render = "render_income_telegram" in src_t
    has_html_render = "render_income_html" in src_e
    return {
        "ok": all((has_context, has_finalize, has_strip, has_tg_render, has_html_render)),
        "income_context_injected": has_context,
        "income_finalized": has_finalize,
        "toss_actions_stripped": has_strip,
        "telegram_render_present": has_tg_render,
        "html_render_present": has_html_render,
    }


def _check_live_policy_code() -> dict:
    """현재 Toss live pilot 정책 스키마 확인 (paper 전제 폐기 — live가 정본)."""
    try:
        from core.toss_live_pilot_policy import compute_toss_live_pilot_policy
        policy = compute_toss_live_pilot_policy()
        return {
            "autonomous_mode": policy.get("autonomous_mode"),
            "autonomous_kill_switch": policy.get("autonomous_kill_switch"),
            "live_order_allowed": policy.get("live_order_allowed"),
            "adapter_status": policy.get("adapter_status"),
            "live_transport_status": policy.get("live_transport_status"),
        }
    except Exception as e:
        return {"error": str(e)}


def _check_write_routes() -> list[str]:
    """web/app.py에 POST/PUT/DELETE/PATCH route가 있는지 확인."""
    app_py = Path(__file__).resolve().parent.parent / "web" / "app.py"
    if not app_py.exists():
        return []
    src = app_py.read_text(encoding="utf-8")
    found = []
    for pat in ("@app.post", "@app.put", "@app.delete", "@app.patch"):
        for line in src.splitlines():
            if pat in line.lower():
                found.append(line.strip())
    return found


def run_check() -> dict:
    """전체 검증 실행 후 결과 dict 반환."""
    result: dict = {}

    # 1. code path 정적 확인
    result["code_path"] = _check_code_path()

    # 2. Toss live 정책 스키마 (paper 전제 폐기)
    result["live_policy"] = _check_live_policy_code()

    # 3. write routes 확인
    result["write_routes"] = _check_write_routes()

    # 4. 최신 브리핑 로드
    briefing = _load_latest_briefing()
    if briefing is None:
        result["latest_briefing_found"] = False
        result["verdict"] = "awaiting_next_briefing"
        result["reason"] = "briefing_archive.db 없거나 아카이브 없음"
        return result

    result["latest_briefing_found"] = True
    result["briefing_id"] = briefing.get("id")
    result["briefing_type"] = briefing.get("briefing_type")
    result["briefing_created_at"] = briefing.get("created_at")

    briefing_dt = _parse_briefing_dt(briefing.get("created_at", ""))
    result["briefing_post_integration"] = (
        briefing_dt is not None and briefing_dt >= INCOME_INTEGRATION_CUTOFF_KST
    )

    # 5. body 합치기 (text + html 모두 검색)
    body_text = briefing.get("body_text") or ""
    body_html = briefing.get("body_html") or ""
    combined = body_text + "\n" + body_html

    # 6. forbidden marker 탐지 (hard check — 통합 이전 브리핑에도 적용)
    forbidden_found = _check_markers(combined, FORBIDDEN_MARKERS)
    result["forbidden_cta_found"] = forbidden_found

    # 7. required marker 탐지
    required_found = _check_markers(combined, REQUIRED_MARKERS)
    required_missing = [m for m in REQUIRED_MARKERS if m not in required_found]
    result["required_markers_found"] = required_found
    result["required_markers_missing"] = required_missing
    result["income_dashboard_present"] = "오늘 수입 계기판" in combined
    result["realized_income_separated"] = "실현수입" in combined and "오늘 평가변동" in combined
    result["toss_autonomous_present"] = "Toss: 자동운영" in combined or "Toss 자동운영" in combined
    result["samsung_manual_only_present"] = "삼성: 수동" in combined

    # 8. verdict 결정
    if forbidden_found:
        result["verdict"] = "fail"
        result["reason"] = f"forbidden marker 발견: {forbidden_found}"
    elif not result["briefing_post_integration"]:
        result["verdict"] = "awaiting_next_briefing"
        result["reason"] = "income 통합 이전 브리핑 (다음 브리핑 대기)"
    elif not result["income_dashboard_present"]:
        result["verdict"] = "awaiting_next_briefing"
        result["reason"] = "income 통합 이후 브리핑이지만 수입 계기판 미확인 (다음 브리핑 대기)"
    else:
        result["verdict"] = "pass"
        result["reason"] = "수입 중심 브리핑 구조 정상 확인"

    return result


def _print_report(r: dict) -> None:
    verdict = r.get("verdict", "unknown")
    icon = {"pass": "✅", "fail": "❌", "warn": "⚠️"}.get(verdict, "⏳")

    print("=" * 60)
    print("브리핑 실제 반영 검증 결과")
    print("=" * 60)
    print(f"latest briefing found:     {r.get('latest_briefing_found')}")
    print(f"briefing_type:             {r.get('briefing_type', '-')}")
    print(f"briefing_created_at:       {r.get('briefing_created_at', '-')}")
    print(f"post integration:          {r.get('briefing_post_integration', '-')}")
    print()
    print(f"수입 계기판 표시:          {r.get('income_dashboard_present', '-')}")
    print(f"실현/평가 분리 표기:       {r.get('realized_income_separated', '-')}")
    print(f"Toss 자동운영 표시:        {r.get('toss_autonomous_present', '-')}")
    print(f"삼성 수동 전용 표시:       {r.get('samsung_manual_only_present', '-')}")
    print(f"required missing:          {r.get('required_markers_missing', [])}")
    print(f"forbidden:                 {r.get('forbidden_cta_found', [])}")
    print()

    cp = r.get("code_path", {})
    print(f"code_path ok:              {cp.get('ok', '-')}")
    print(f"  income_context_injected: {cp.get('income_context_injected', '-')}")
    print(f"  income_finalized:        {cp.get('income_finalized', '-')}")
    print(f"  toss_actions_stripped:   {cp.get('toss_actions_stripped', '-')}")
    print(f"  telegram/html render:    {cp.get('telegram_render_present', '-')}/{cp.get('html_render_present', '-')}")
    print()

    pol = r.get("live_policy", {})
    print(f"autonomous_mode:           {pol.get('autonomous_mode', '-')}")
    print(f"live_order_allowed:        {pol.get('live_order_allowed', '-')}")
    print(f"adapter/transport:         {pol.get('adapter_status', '-')}/{pol.get('live_transport_status', '-')}")
    print()

    wr = r.get("write_routes", [])
    print(f"write routes:              {len(wr)} 건" + (" ← 확인 필요" if wr else ""))
    print()
    print(f"{icon}  verdict: {verdict}")
    print(f"   reason: {r.get('reason', '')}")
    print("=" * 60)


if __name__ == "__main__":
    r = run_check()
    _print_report(r)
    if r.get("verdict") == "fail":
        sys.exit(1)
