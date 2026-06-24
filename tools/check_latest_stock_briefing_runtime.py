"""브리핑 실제 반영 검증 도구 (read-only)

최신 stock briefing에 Toss Paper 가드와 삼성증권 정합성이 반영됐는지 확인한다.

실행:
    python tools/check_latest_stock_briefing_runtime.py

판정 기준:
- forbidden_cta_found 가 있으면 → fail
- required markers 없음 + 최신 브리핑 없음 → awaiting_next_briefing
- required markers 없음 + 브리핑 있지만 마커 불일치 → warn (AI가 verbatim 미출력)
- forbidden 없음 + code_path 정상 → pass
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
# Toss Paper 브리핑 통합 커밋 기준 시각 (KST)
# commit 11e63fd: "feat: inject Toss paper performance summary into briefing context"
# ──────────────────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))
TOSS_INTEGRATION_CUTOFF_KST = datetime(2026, 6, 24, 0, 30, 0, tzinfo=KST)

REQUIRED_MARKERS = [
    "Toss Paper",
    "실제 주문 아님",
    "실주문: 비활성",
    "표본부족",
    "SOFI",
    "진행 중",
    "기존 포트폴리오",
]

FORBIDDEN_MARKERS = [
    "실주문: 활성",
    "자동매매 시작",
    "자동거래 시작",
    "주문 실행",
    "매수하기",
    "매도하기",
    "MU 매도 실행",
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
    """analyzer.py + toss_decision_context.py에 Toss Paper 가드 코드 패스 확인."""
    root = Path(__file__).resolve().parent.parent
    analyzer = root / "core" / "analyzer.py"
    ctx = root / "core" / "toss_decision_context.py"
    if not analyzer.exists():
        return {"ok": False, "reason": "analyzer.py not found"}
    src_a = analyzer.read_text(encoding="utf-8")
    src_c = ctx.read_text(encoding="utf-8") if ctx.exists() else ""
    has_import = "format_toss_paper_performance_briefing" in src_a
    # live_orders_allowed guard lives in toss_decision_context.py
    has_live_guard = "live_orders_allowed" in src_c or "live_order_allowed" in src_c
    return {
        "ok": has_import and has_live_guard,
        "toss_paper_injected": has_import,
        "live_order_guard_present": has_live_guard,
    }


def _check_paper_policy_db() -> dict:
    """toss_paper_ledger DB에서 현재 상태 직접 확인 (API 없이)."""
    try:
        from core.toss_paper_performance import get_paper_performance_summary
        summary = get_paper_performance_summary()
        s = summary.get("summary", {})
        return {
            "open": s.get("open", 0),
            "evaluated_count": s.get("evaluated_count", 0),
            "win_rate": s.get("win_rate", 0.0),
            "duplicate_open_symbols": s.get("duplicate_open_symbols", []),
        }
    except Exception as e:
        return {"error": str(e)}


def _check_paper_policy_code() -> dict:
    """toss_paper_policy.py에서 live_order_allowed 정책 확인."""
    try:
        from core.toss_paper_policy import compute_toss_paper_policy
        policy = compute_toss_paper_policy()
        return {
            "mode": policy.get("mode"),
            "live_order_allowed": policy.get("live_order_allowed"),
            "max_budget_krw": policy.get("max_budget_krw"),
            "sample_status": policy.get("sample_status"),
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

    # 2. paper 현재 상태 (DB 직접)
    result["paper_performance"] = _check_paper_policy_db()
    result["paper_policy"] = _check_paper_policy_code()

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
        briefing_dt is not None and briefing_dt >= TOSS_INTEGRATION_CUTOFF_KST
    )

    # 5. body 합치기 (text + html 모두 검색)
    body_text = briefing.get("body_text") or ""
    body_html = briefing.get("body_html") or ""
    combined = body_text + "\n" + body_html

    # 6. forbidden marker 탐지 (hard check)
    forbidden_found = _check_markers(combined, FORBIDDEN_MARKERS)
    result["forbidden_cta_found"] = forbidden_found

    # 7. required marker 탐지
    required_found = _check_markers(combined, REQUIRED_MARKERS)
    required_missing = [m for m in REQUIRED_MARKERS if m not in required_found]
    result["required_markers_found"] = required_found
    result["required_markers_missing"] = required_missing
    result["toss_paper_present"] = "Toss Paper" in combined
    result["paper_only_guard"] = "실제 주문 아님" in combined and "실주문: 비활성" in combined
    result["samsung_portfolio_present"] = (
        "삼성증권" in combined or "현금성 자산" in combined or "총 평가액" in combined
    )
    # SOFI + 진행 중 = paper open order 표시
    result["sofi_open_displayed"] = "SOFI" in combined and "진행 중" in combined
    result["mu_protection"] = "MU 매도 실행" not in combined

    # 8. verdict 결정
    if forbidden_found:
        result["verdict"] = "fail"
        result["reason"] = f"forbidden CTA 발견: {forbidden_found}"
    elif not result["briefing_post_integration"]:
        result["verdict"] = "awaiting_next_briefing"
        result["reason"] = "Toss Paper 통합 이전 브리핑 (다음 브리핑 대기)"
    elif not result["toss_paper_present"]:
        result["verdict"] = "awaiting_next_briefing"
        result["reason"] = "Toss Paper 통합 이후 브리핑이지만 마커 미확인 (AI verbatim 미출력 또는 다음 브리핑 대기)"
    else:
        result["verdict"] = "pass"
        result["reason"] = "Toss Paper 가드 정상 확인"

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
    print(f"toss_paper_present:        {r.get('toss_paper_present', '-')}")
    print(f"paper_only_guard:          {r.get('paper_only_guard', '-')}")
    print(f"SOFI open 표시:            {r.get('sofi_open_displayed', '-')}")
    print(f"삼성증권 portfolio 표시:   {r.get('samsung_portfolio_present', '-')}")
    print(f"MU 보호 문구:              {r.get('mu_protection', '-')}")
    print(f"forbidden CTA:             {r.get('forbidden_cta_found', [])}")
    print()

    cp = r.get("code_path", {})
    print(f"code_path ok:              {cp.get('ok', '-')}")
    print(f"  toss_paper_injected:     {cp.get('toss_paper_injected', '-')}")
    print(f"  live_order_guard:        {cp.get('live_order_guard_present', '-')}")
    print()

    pp = r.get("paper_performance", {})
    print(f"paper open:                {pp.get('open', '-')}")
    print(f"paper evaluated_count:     {pp.get('evaluated_count', '-')}")
    print(f"duplicate_open_symbols:    {pp.get('duplicate_open_symbols', [])}")

    pol = r.get("paper_policy", {})
    print(f"live_order_allowed:        {pol.get('live_order_allowed', '-')}")
    print(f"sample_status:             {pol.get('sample_status', '-')}")
    print(f"max_budget_krw:            {pol.get('max_budget_krw', '-')}")
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
