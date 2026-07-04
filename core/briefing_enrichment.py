"""briefing_enrichment — 수집 데이터 통합 주입 (브리핑 품질 강화).

기존에 수집만 되고 브리핑에 미주입되던 데이터를 텍스트 블록으로 변환:
  1. DART 공시 — 보유종목(전 계좌) 리스크 공시 + 최근 공시 (dart_monitor 재사용)
  2. KIS 호가 — 국내 보유종목 호가 임밸런스/유동성/스프레드 (market_kis 재사용)
  3. 품질게이트 — Toss 자동매매 당일 decision_bucket 요약 (toss_quality_gate 재사용)

모든 함수는 실패 시 빈 문자열 반환 (fail-safe — 브리핑 파이프라인 차단 금지).
read-only 조회만 수행, 주문 경로 변경 없음.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_MAX_DART_ITEMS = 8       # 리스크 외 일반 공시 표시 상한
_MAX_ORDERBOOK_TICKERS = 6


def _all_holding_kr_codes() -> dict[str, str]:
    """전 계좌 보유종목 중 국내 종목의 6자리 코드 → 이름 맵."""
    codes: dict[str, str] = {}
    try:
        from config import settings
        holdings_maps = [
            getattr(settings, name, {}) or {}
            for name in ("HOLDINGS_GENERAL", "HOLDINGS_RIA", "HOLDINGS_IRP",
                         "HOLDINGS_PENSION", "HOLDINGS_ISA")
        ]
        portfolio = getattr(settings, "PORTFOLIO", {}) or {}
        for hm in holdings_maps:
            for ticker in hm:
                base = ticker.split(".")[0]
                if base.isdigit() and len(base) == 6:
                    codes[base] = portfolio.get(ticker, ticker)
    except Exception as e:
        log.debug("보유종목 코드 수집 실패: %s", e)
    return codes


# ── 1. DART 공시 ─────────────────────────────────────────────────

def dart_briefing_text(days: int = 2) -> str:
    """보유종목 관련 DART 공시 요약. 키 미설정/실패 시 빈 문자열."""
    try:
        from core.dart_monitor import fetch_recent_disclosures, screen_disclosures
        codes = _all_holding_kr_codes()
        if not codes:
            return ""
        fetched = fetch_recent_disclosures(days=days)
        if not fetched.get("ok") or not fetched.get("items"):
            return ""
        items = fetched["items"]

        # 리스크 공시 (유상증자/CB/소송 등)
        risk_hits = screen_disclosures(items, set(codes))

        # 일반 공시 (보유종목 전체 — 리스크 외)
        risk_nos = {h.get("rcept_no") for h in risk_hits}
        normal = [
            it for it in items
            if it.get("stock_code") in codes and it.get("rcept_no") not in risk_nos
        ][:_MAX_DART_ITEMS]

        if not risk_hits and not normal:
            return ""

        lines: list[str] = []
        if risk_hits:
            lines.append("🚨 리스크 공시 (보유종목 — 반드시 해당 종목 판단에 반영):")
            for h in risk_hits:
                icon = "🚨" if h.get("severity") == "high" else "⚠️"
                lines.append(
                    f"  {icon} {h.get('corp_name')}({h.get('stock_code')}): "
                    f"{(h.get('report_nm') or '').strip()}"
                    f" [{h.get('keyword')}, {h.get('rcept_dt')}]")
        if normal:
            lines.append("📄 보유종목 최근 공시:")
            for it in normal:
                lines.append(
                    f"  · {it.get('corp_name')}({it.get('stock_code')}): "
                    f"{(it.get('report_nm') or '').strip()} ({it.get('rcept_dt')})")
        lines.append("→ 공시는 공식 1차 소스 — 뉴스와 충돌 시 공시를 우선하라.")
        return "\n".join(lines)
    except Exception as e:
        log.debug("DART 브리핑 텍스트 실패: %s", e)
        return ""


# ── 2. KIS 호가 (국내 보유종목) ──────────────────────────────────

def kis_orderbook_briefing_text(tickers: list[str] | None = None) -> str:
    """국내 보유종목 호가 요약 (임밸런스/유동성/스프레드). 실패 시 빈 문자열."""
    try:
        from core.market_kis import get_domestic_orderbook
        if tickers is None:
            codes = _all_holding_kr_codes()
            name_map = dict(codes)
            targets = list(codes)[:_MAX_ORDERBOOK_TICKERS]
        else:
            name_map = {}
            targets = [
                t.split(".")[0] for t in tickers
                if t.split(".")[0].isdigit() and len(t.split(".")[0]) == 6
            ][:_MAX_ORDERBOOK_TICKERS]
        if not targets:
            return ""

        lines: list[str] = []
        for code in targets:
            try:
                ob = get_domestic_orderbook(code)
            except Exception:
                continue
            if not ob or ob.get("error"):
                continue
            imb = float(ob.get("imbalance_pct") or 0)
            side = "매수우위" if imb > 5 else "매도우위" if imb < -5 else "균형"
            label = name_map.get(code, code)
            lines.append(
                f"  {label}({code}): 임밸런스 {imb:+.0f}% ({side})"
                f" · 유동성 {ob.get('liquidity_label', '?')}"
                f" · 스프레드 {float(ob.get('spread_pct') or 0):.2f}%")
        if not lines:
            return ""
        lines.append(
            "→ 매도우위 심화(-20% 이하) 종목은 단기 하방 압력 — 신규 매수 진입가를 보수적으로.")
        return "\n".join(lines)
    except Exception as e:
        log.debug("KIS 호가 브리핑 텍스트 실패: %s", e)
        return ""


# ── 3. Toss 품질게이트 요약 ──────────────────────────────────────

def quality_gate_briefing_text() -> str:
    """당일 Toss 품질게이트 decision_bucket 요약. 실패/데이터 없으면 빈 문자열."""
    try:
        from core.toss_quality_gate import generate_daily_quality_report
        rep = generate_daily_quality_report()
        total = sum(
            rep.get(k, 0) for k in (
                "pass_count", "small_pass_count", "wait_count",
                "watch_count", "chase_block_count", "block_count"))
        if total == 0:
            return ""
        lines = [
            f"오늘 자동매매 품질게이트 판정 {total}건: "
            f"PASS {rep['pass_count']} · SMALL_PASS {rep['small_pass_count']}"
            f" · WAIT {rep['wait_count']} · WATCH {rep['watch_count']}"
            f" · CHASE_BLOCK {rep['chase_block_count']} · BLOCK {rep['block_count']}",
        ]
        if rep.get("avg_pass_score"):
            lines.append(
                f"PASS 평균 점수 {rep['avg_pass_score']} · 평균 RR {rep['avg_pass_rr']}")
        if rep.get("outcome_hit_rate") is not None:
            lines.append(
                f"누적 실측 적중률(5일): {rep['outcome_hit_rate'] * 100:.0f}%"
                f" ({rep['outcome_evaluated']}건 평가)")
        if rep.get("top_block_reasons"):
            lines.append("주요 차단 사유: " + " / ".join(rep["top_block_reasons"][:3]))
        lines.append(
            "→ 게이트 차단 사유는 삼성증권 추천에도 동일하게 적용하라"
            " (추격 금지·RR 미달·모멘텀 부족 종목을 브리핑에서 매수 추천하지 마라).")
        return "\n".join(lines)
    except Exception as e:
        log.debug("품질게이트 브리핑 텍스트 실패: %s", e)
        return ""


# ── 통합 ─────────────────────────────────────────────────────────

def build_enrichment_context(briefing_type: str = "MANUAL") -> str:
    """브리핑 유형에 맞는 수집 데이터 통합 텍스트. 없으면 빈 문자열."""
    if briefing_type in ("US_BEFORE", "US_NIGHT"):
        return ""  # 미국장 브리핑엔 국내 공시/호가 불필요

    parts: list[str] = []
    dart = dart_briefing_text()
    if dart:
        parts.append(f"━━━ 📢 DART 전자공시 (금감원 공식) ━━━\n{dart}")
    ob = kis_orderbook_briefing_text()
    if ob:
        parts.append(f"━━━ 📊 KIS 실시간 호가 (국내 보유종목) ━━━\n{ob}")
    qg = quality_gate_briefing_text()
    if qg:
        parts.append(f"━━━ 🚦 Toss 품질게이트 판정 요약 ━━━\n{qg}")
    return "\n\n".join(parts)
