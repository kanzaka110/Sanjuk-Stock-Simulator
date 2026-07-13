"""
Gmail SMTP 메일 전송 모듈

분석/브리핑 결과를 이메일로 전송. 텔레그램과 동일한 패턴 (선택적 채널).
환경변수: GMAIL_USER, GMAIL_APP_PASSWORD, GMAIL_TO (선택, 기본은 USER 본인)
"""

from __future__ import annotations

import logging
import re
import smtplib
from datetime import datetime
from email.message import EmailMessage

from config.settings import GMAIL_APP_PASSWORD, GMAIL_TO, GMAIL_USER, KST
from core.models import BriefingResult

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SSL


def send_email(
    subject: str,
    body_text: str,
    body_html: str | None = None,
    to: str | None = None,
) -> bool:
    """Gmail SMTP로 메일 전송.

    Args:
        subject: 제목
        body_text: 평문 본문
        body_html: HTML 본문 (선택, 제공 시 multipart/alternative)
        to: 수신자 (미지정 시 GMAIL_TO → GMAIL_USER 순)

    Returns:
        성공 여부
    """
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        log.warning("Gmail 설정 없음 (GMAIL_USER/GMAIL_APP_PASSWORD) — 건너뜀")
        return False

    recipient = to or GMAIL_TO or GMAIL_USER

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = recipient
    msg.set_content(body_text)

    if body_html:
        msg.add_alternative(body_html, subtype="html")

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        log.info(f"메일 전송 완료: {recipient} | {subject}")
        return True
    except smtplib.SMTPAuthenticationError as e:
        log.error(f"Gmail 인증 실패 — 앱 비밀번호 확인: {e}")
        return False
    except Exception as e:
        log.error(f"메일 전송 오류: {e}")
        return False


def _markdown_to_plain(text: str) -> str:
    """텔레그램 Markdown 표기를 평문으로 변환."""
    text = re.sub(r'\*([^*\n]+)\*', r'\1', text)  # *bold* → bold
    text = re.sub(r'_([^_\n]+)_', r'\1', text)    # _italic_ → italic
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1\n  → \2', text)  # [텍스트](URL)
    return text


_HTML_CSS = """<style>
body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; line-height: 1.85; color: #222; max-width: 760px; margin: 0 auto; padding: 24px; }
h1 { color: #1a3a6c; border-bottom: 3px solid #1a3a6c; padding-bottom: 10px; margin-bottom: 16px; }
h2 { color: #1a3a6c; margin-top: 40px; margin-bottom: 14px; border-left: 4px solid #1a3a6c; padding: 4px 0 4px 12px; }
h3 { color: #2c4f80; margin-top: 26px; margin-bottom: 10px; padding-bottom: 4px; border-bottom: 1px dotted #ccd; }
p { margin: 10px 0; }
table { border-collapse: collapse; width: 100%; margin: 14px 0 22px; }
th, td { border: 1px solid #ddd; padding: 10px 14px; text-align: left; vertical-align: top; line-height: 1.7; }
th { background: #f0f4fa; font-weight: 600; }
.verdict { background: #e8f5e9; border-left: 5px solid #2e7d32; padding: 18px 20px; margin: 20px 0; font-size: 1.05em; line-height: 1.8; }
.verdict b { display: block; margin-bottom: 8px; font-size: 1.1em; }
.warn { background: #fff3e0; border-left: 5px solid #ef6c00; padding: 14px 18px; margin: 16px 0; line-height: 1.75; }
.crit { background: #ffebee; border-left: 5px solid #c62828; padding: 14px 18px; margin: 16px 0; line-height: 1.75; }
.opp  { background: #e3f2fd; border-left: 5px solid #1565c0; padding: 14px 18px; margin: 16px 0; line-height: 1.75; }
.warn b, .crit b, .opp b { display: block; margin-bottom: 8px; }
.warn ul, .crit ul, .opp ul { margin-top: 6px; }
.kpi { display: inline-block; background: #f5f5f5; padding: 8px 14px; margin: 6px 6px 6px 0; border-radius: 6px; font-size: 0.95em; }
.kpi b { color: #1a3a6c; }
.kpi-row { margin: 14px 0 24px; }
ul { margin: 10px 0 14px; padding-left: 24px; }
li { margin: 8px 0; line-height: 1.75; }
.footer { color: #777; font-size: 0.85em; margin-top: 40px; border-top: 1px solid #ddd; padding-top: 14px; line-height: 1.7; }
code { background: #f5f5f5; padding: 2px 6px; border-radius: 3px; font-size: 0.95em; }
.persona-card { background: #fafbfd; border: 1px solid #e0e6ee; border-radius: 8px; padding: 18px 22px; margin: 18px 0; }
.persona-card h3 { margin-top: 0; border-bottom: none; }
.persona-meta { color: #555; font-size: 0.95em; margin-bottom: 12px; }
.reasoning-block { background: #fff; border-left: 3px solid #b8c5d6; padding: 12px 16px; margin: 12px 0; line-height: 1.85; }
.reasoning-block p { margin: 8px 0; }
.section-intro { color: #555; font-size: 0.95em; margin-bottom: 16px; }
</style>"""


def _split_into_paragraphs(text: str) -> list[str]:
    """긴 문장을 단락으로 분리 (마침표/물음표/느낌표 기준 + 2-3문장씩 묶음)."""
    if not text:
        return []
    import re
    text = text.strip()
    sentences = re.split(r"(?<=[\.!\?。])\s+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return [text]

    paragraphs: list[str] = []
    buffer: list[str] = []
    for s in sentences:
        buffer.append(s)
        # 2-3문장 모이거나 80자 이상이면 단락 끊기
        joined = " ".join(buffer)
        if len(buffer) >= 3 or len(joined) >= 140:
            paragraphs.append(joined)
            buffer = []
    if buffer:
        paragraphs.append(" ".join(buffer))
    return paragraphs


def _esc(text: object) -> str:
    """HTML escape 안전 변환."""
    if text is None:
        return ""
    s = str(text)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _num_safe(v) -> float:
    """문자열/None에서 숫자 추출. 실패 시 0."""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        import re as _re
        m = _re.search(r"[\d,.]+", str(v).replace(",", ""))
        return float(m.group()) if m else 0
    except Exception:
        return 0


def _build_briefing_html(
    result: BriefingResult,
    raw: dict,
    label: str,
    title: str,
) -> str:
    """raw_json을 풍부한 HTML 메일로 변환 (참고 .eml 디자인)."""
    advisor_verdict = raw.get("advisor_verdict", "") or "—"
    advisor_oneliner = raw.get("advisor_oneliner", "") or ""
    advisor_conclusion = raw.get("advisor_conclusion", "") or ""
    next_action = raw.get("next_action", "") or ""

    persona_summary = raw.get("persona_summary", {}) or {}
    persona_details = raw.get("persona_details", []) or []
    account_strategy_raw = raw.get("account_strategy", {}) or {}
    account_strategy = account_strategy_raw if isinstance(account_strategy_raw, dict) else {}
    risks = raw.get("advisor_risks", []) or []
    opportunities = raw.get("advisor_opportunities", []) or []
    scenarios_raw = raw.get("advisor_scenarios", []) or []
    scenarios = scenarios_raw if isinstance(scenarios_raw, list) else []
    checklist = raw.get("advisor_checklist", []) or []
    buy_recs = raw.get("buy_recommendations", []) or []
    sell_recs = raw.get("sell_recommendations", []) or []
    strategy_summary = raw.get("strategy_summary", "") or ""
    market_summary = getattr(result, "market_summary", "") or ""
    risk_level = raw.get("risk_level", "") or ""
    regime = raw.get("regime", "") or ""
    regime_adj = raw.get("regime_adjustment", "") or ""

    from core.telegram import _filter_blocked_from_text, _normalized_from_raw

    normalized = _normalized_from_raw(raw)
    # raw action-like 필드는 normalized 유무와 무관하게 안전 필터를 통과한다.
    # normalized=None legacy 경로도 명백한 매수 CTA 조각은 제거한다.
    advisor_oneliner = _filter_blocked_from_text(advisor_oneliner, normalized)
    advisor_conclusion = _filter_blocked_from_text(advisor_conclusion, normalized)
    next_action = _filter_blocked_from_text(next_action, normalized)
    strategy_summary = _filter_blocked_from_text(strategy_summary, normalized)
    account_strategy = {
        account: filtered
        for account, text in account_strategy.items()
        if (filtered := _filter_blocked_from_text(str(text or ""), normalized))
    }
    sanitized_scenarios = []
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue
        item = dict(scenario)
        item["action"] = _filter_blocked_from_text(
            str(item.get("action") or ""), normalized
        )
        if item["action"]:
            sanitized_scenarios.append(item)
    scenarios = sanitized_scenarios

    # BriefingResult.buy_signals/sell_signals (raw_json에 buy_recommendations 없을 때 fallback)
    buy_signals = getattr(result, "buy_signals", ()) or ()
    sell_signals = getattr(result, "sell_signals", ()) or ()
    portfolio_signals = getattr(result, "portfolio_signals", ()) or ()

    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")

    parts: list[str] = []
    parts.append('<!DOCTYPE html><html><head><meta charset="UTF-8">')
    parts.append(f"<title>{_esc(title)}</title>")
    parts.append(_HTML_CSS)
    parts.append("</head><body>")

    parts.append(f"<h1>{_esc(label)}</h1>")
    parts.append(f'<p style="color:#666;">{_esc(title)}<br>분석: 멀티 에이전트 (가치/성장/기술/매크로) + CIO · 생성: {_esc(now_str)}</p>')

    # 수입 계기판 — 결정론 payload (verdict보다 먼저, 텔레그램과 동일 수치)
    try:
        from core.income_briefing import render_income_html
        income_html = render_income_html(raw.get("income_briefing") or {})
        if income_html:
            parts.append(income_html)
    except Exception:
        pass

    parts.append('<div class="verdict">')
    parts.append(f"<b>결론: {_esc(advisor_verdict)}</b><br>")
    if advisor_oneliner:
        parts.append(f"{_esc(advisor_oneliner)}<br>")
    if next_action:
        parts.append(f"<i>다음 액션: {_esc(next_action)}</i>")
    parts.append("</div>")

    section = 0

    # KPI 칩 (Regime / Risk / 의견 분기)
    if regime or risk_level:
        parts.append('<div class="kpi-row">')
        if regime:
            parts.append(f'<div class="kpi">시장 체제 <b>{_esc(regime)}</b></div>')
        if regime_adj:
            parts.append(f'<div class="kpi">리스크 조정 <b>{_esc(regime_adj)}</b></div>')
        if risk_level:
            parts.append(f'<div class="kpi">전체 리스크 <b>{_esc(risk_level)}</b></div>')
        parts.append("</div>")

    if market_summary:
        section += 1
        parts.append(f"<h2>{section}. 시장 요약</h2>")
        for p in _split_into_paragraphs(market_summary):
            parts.append(f"<p>{_esc(p)}</p>")

    # 페르소나 풀 디테일 (요약 표 + 각자 카드)
    if persona_details:
        section += 1
        parts.append(f"<h2>{section}. 페르소나 4인 상세 분석</h2>")
        parts.append('<p class="section-intro">4인의 독립 분석가가 동일한 시장 데이터를 두고 각자의 관점에서 분석했습니다. 아래는 의견 분포이며, 상세 분석은 각 카드에서 확인할 수 있습니다.</p>')
        parts.append('<table><tr><th>관점</th><th>판단</th><th>확신도</th><th>핵심 포인트</th></tr>')
        for pd in persona_details:
            verdict_color = {
                "매수": "#2e7d32", "매도": "#c62828", "홀딩": "#1565c0", "관망": "#666",
            }.get(pd.get("verdict", ""), "#666")
            kf_list = pd.get("key_factors", []) or []
            first_factor = kf_list[0] if kf_list else (pd.get("reasoning", "") or "")[:60]
            parts.append(
                f'<tr><td><b>{_esc(pd.get("persona", ""))}</b></td>'
                f'<td><span style="color:{verdict_color};font-weight:600;">{_esc(pd.get("verdict", ""))}</span></td>'
                f'<td>{_esc(pd.get("confidence", 0))}%</td>'
                f'<td>{_esc(first_factor)}</td></tr>',
            )
        parts.append("</table>")

        for pd in persona_details:
            verdict = pd.get("verdict", "")
            verdict_color = {
                "매수": "#2e7d32", "매도": "#c62828", "홀딩": "#1565c0", "관망": "#666",
            }.get(verdict, "#666")

            parts.append('<div class="persona-card">')
            parts.append(f'<h3>{_esc(pd.get("persona", ""))}</h3>')
            parts.append(
                f'<div class="persona-meta">'
                f'판단 <span style="color:{verdict_color};font-weight:600;">{_esc(verdict)}</span> '
                f'· 확신도 <b>{_esc(pd.get("confidence", 0))}%</b>'
                f'</div>',
            )

            reasoning = pd.get("reasoning", "")
            if reasoning:
                parts.append('<div class="reasoning-block">')
                for p in _split_into_paragraphs(reasoning):
                    parts.append(f"<p>{_esc(p)}</p>")
                parts.append("</div>")

            kf = pd.get("key_factors", []) or []
            if kf:
                parts.append("<p><b>핵심 요인</b></p><ul>")
                for f in kf:
                    parts.append(f"<li>{_esc(f)}</li>")
                parts.append("</ul>")

            rw = pd.get("risk_warning", "")
            if rw:
                parts.append('<div class="warn"><b>주의해야 할 리스크</b>')
                for p in _split_into_paragraphs(rw):
                    parts.append(f"<p>{_esc(p)}</p>")
                parts.append("</div>")

            sv = pd.get("stock_views", []) or []
            if sv:
                parts.append("<p><b>종목별 의견</b></p>")
                parts.append("<table><tr><th>종목</th><th>의견</th><th>근거</th></tr>")
                for v in sv:
                    parts.append(
                        f'<tr><td>{_esc(v.get("ticker", ""))}</td>'
                        f'<td>{_esc(v.get("view", ""))}</td>'
                        f'<td>{_esc(v.get("reason", ""))}</td></tr>',
                    )
                parts.append("</table>")

            parts.append("</div>")
    elif persona_summary:
        section += 1
        parts.append(f"<h2>{section}. 페르소나 요약</h2>")
        parts.append('<table><tr><th>관점</th><th>판단</th></tr>')
        for name in ("가치투자자", "성장투자자", "기술적분석가", "매크로분석가"):
            summary = persona_summary.get(name, "")
            if summary:
                parts.append(
                    f'<tr class="persona-row"><td><b>{_esc(name)}</b></td><td>{_esc(summary)}</td></tr>',
                )
        parts.append("</table>")

    if advisor_conclusion:
        section += 1
        parts.append(f"<h2>{section}. CIO 종합 결론</h2>")
        for p in _split_into_paragraphs(advisor_conclusion):
            parts.append(f"<p>{_esc(p)}</p>")

    # ── normalizer 결과 우선 표시 (raw buy_recommendations 직접 노출 금지) ──
    if normalized is not None:
        exec_buys = [a for a in (normalized.get("executable_actions") or []) if a.get("side") == "buy"]
        exec_sells = [a for a in (normalized.get("executable_actions") or []) if a.get("side") == "sell"]
        cond_buys = normalized.get("conditional_buy_candidates") or []
        cond_sells = normalized.get("conditional_sell_candidates") or []
        cancelled_sells_norm = normalized.get("cancelled_sells") or []
        blocked = normalized.get("blocked_buys") or []

        if exec_buys:
            section += 1
            parts.append(f"<h2>{section}. ⚡ 실행 매수</h2>")
            parts.append("<table><tr><th>종목</th><th>계좌</th><th>수량</th><th>매수가</th><th>손절</th><th>근거</th></tr>")
            for a in exec_buys:
                parts.append(f"<tr><td><b>{_esc(a.get('name', ''))}</b></td>")
                parts.append(f"<td>{_esc(a.get('account', ''))}</td>")
                parts.append(f"<td>{_esc(a.get('qty', ''))}</td>")
                parts.append(f"<td>{_esc(a.get('price', ''))}</td>")
                parts.append(f"<td>{_esc(a.get('stop', ''))}</td>")
                parts.append(f"<td>{_esc(a.get('reason', ''))}</td></tr>")
            parts.append("</table>")

        if exec_sells:
            section += 1
            parts.append(f"<h2>{section}. 🔴 실행 매도</h2>")
            parts.append("<table><tr><th>종목</th><th>계좌</th><th>수량</th><th>기준가</th><th>손절/목표</th><th>근거</th></tr>")
            for a in exec_sells:
                parts.append(f"<tr><td><b>{_esc(a.get('name', '') or a.get('ticker', ''))}</b></td>")
                parts.append(f"<td>{_esc(a.get('account', ''))}</td>")
                parts.append(f"<td>{_esc(a.get('qty', ''))}</td>")
                parts.append(f"<td>{_esc(a.get('price', ''))}</td>")
                parts.append(f"<td>{_esc(a.get('stop', '') or a.get('target', ''))}</td>")
                parts.append(f"<td>{_esc(a.get('reason', ''))}</td></tr>")
            parts.append("</table>")

        if cond_buys:
            section += 1
            parts.append(f"<h2>{section}. 🕐 조건부 매수 후보</h2>")
            parts.append("<p style='color:#666;font-size:12px'>조건 도달 시만 체결 — 즉시 실행 아님</p>")
            parts.append("<table><tr><th>종목</th><th>계좌</th><th>수량</th><th>지정가</th><th>현재가/거리</th><th>무효화</th></tr>")
            for a in cond_buys:
                # 현재가/조건거리 계산
                cur_p = a.get("current_price_num") or 0
                entry_p = a.get("entry_price_num") or _num_safe(a.get("price"))
                dist_str = ""
                if cur_p and entry_p:
                    gap = (cur_p - entry_p) / entry_p * 100
                    status = "조건 도달" if cur_p <= entry_p else ("조건 근접" if gap <= 1.0 else "조건 대기")
                    dist_str = f"{cur_p:,.0f} ({gap:+.1f}%) {status}"
                else:
                    dist_str = "데이터 부족"
                parts.append(f"<tr><td><b>{_esc(a.get('name', ''))}</b></td>")
                parts.append(f"<td>{_esc(a.get('account', ''))}</td>")
                parts.append(f"<td>{_esc(a.get('qty', ''))}</td>")
                parts.append(f"<td>{_esc(a.get('price', ''))}</td>")
                parts.append(f"<td>{_esc(dist_str)}</td>")
                parts.append(f"<td>{_esc(a.get('invalidation_note', ''))}</td></tr>")
                # execution risk warning (has_warning true만)
                er = a.get("execution_risk") or {}
                if er.get("has_warning"):
                    parts.append(f'<tr><td colspan="6" style="color:#f59e0b;font-size:11px;padding:2px 4px">⚠ {_esc(er.get("label","스프레드 주의"))} · 호가 기준 판단 보조 · 주문 지시 아님</td></tr>')
            parts.append("</table>")

        if cond_sells:
            section += 1
            parts.append(f"<h2>{section}. 🕐🔴 조건부 매도·손절 감시</h2>")
            parts.append("<p style='color:#666;font-size:12px'>조건 확인 전 실행 매도 아님</p>")
            parts.append("<table><tr><th>종목</th><th>계좌</th><th>기준가</th><th>손절/목표</th><th>조건/근거</th></tr>")
            for a in cond_sells:
                parts.append(f"<tr><td><b>{_esc(a.get('name', '') or a.get('ticker', ''))}</b></td>")
                parts.append(f"<td>{_esc(a.get('account', ''))}</td>")
                parts.append(f"<td>{_esc(a.get('price', ''))}</td>")
                parts.append(f"<td>{_esc(a.get('stop', '') or a.get('target', ''))}</td>")
                parts.append(f"<td>{_esc(a.get('hold_note', '') or a.get('reason', ''))}</td></tr>")
            parts.append("</table>")

        if cancelled_sells_norm:
            section += 1
            parts.append(f"<h2>{section}. 🟡 매도 취소·보유 관리</h2>")
            parts.append("<table><tr><th>종목</th><th>계좌</th><th>판정</th><th>근거</th></tr>")
            for a in cancelled_sells_norm:
                parts.append(f"<tr><td><b>{_esc(a.get('name', '') or a.get('ticker', ''))}</b></td>")
                parts.append(f"<td>{_esc(a.get('account', ''))}</td>")
                parts.append(f"<td>{_esc(a.get('hold_note', '') or '실행 매도 아님')}</td>")
                parts.append(f"<td>{_esc(a.get('cancel_reason', '') or a.get('reason', ''))}</td></tr>")
            parts.append("</table>")

        gate_blocked = [a for a in blocked if not a.get("incomplete_order")]
        incomplete = [a for a in blocked if a.get("incomplete_order")]

        if gate_blocked:
            section += 1
            parts.append(f"<h2>{section}. 🚫 주문 차단 / 실행 금지</h2>")
            parts.append("<table><tr><th>종목</th><th>차단 사유</th></tr>")
            for a in gate_blocked:
                parts.append(f"<tr><td><b>{_esc(a.get('name', ''))}</b></td>")
                parts.append(f"<td>{_esc(a.get('block_reason', ''))}</td></tr>")
            parts.append("</table>")

        if incomplete:
            section += 1
            parts.append(f"<h2>{section}. ⚠️ 주문 차단·정보 부족</h2>")
            parts.append("<table><tr><th>종목</th><th>계좌</th><th>누락 필드</th></tr>")
            for a in incomplete:
                miss = ", ".join(a.get("missing_fields") or [])
                parts.append(f"<tr><td><b>{_esc(a.get('name', '') or a.get('ticker', '') or '종목미상')}</b></td>")
                parts.append(f"<td>{_esc(a.get('account', '') or '[계좌미상]')}</td>")
                parts.append(f"<td>정보 부족으로 주문표 제외 — {_esc(miss)}</td></tr>")
            parts.append("</table>")

        if not exec_buys and not cond_buys:
            no_reason = normalized.get("no_buy_reason", "")
            if no_reason:
                section += 1
                parts.append(f"<h2>{section}. 매수 후보 없음</h2>")
                parts.append(f"<p>{_esc(no_reason)}</p>")
    elif buy_recs:
        # fallback: normalized 없는 구버전 호환 (raw 직접 표시)
        section += 1
        parts.append(f"<h2>{section}. 매수 추천</h2>")
        parts.append("<table><tr><th>종목</th><th>계좌</th><th>수량</th><th>매수가</th><th>손절</th><th>익절</th><th>근거</th></tr>")
        for rec in buy_recs:
            parts.append("<tr>")
            parts.append(f"<td><b>{_esc(rec.get('ticker', ''))}</b></td>")
            parts.append(f"<td>{_esc(rec.get('account', ''))}</td>")
            parts.append(f"<td>{_esc(rec.get('shares', ''))}</td>")
            parts.append(f"<td>{_esc(rec.get('entry_price', ''))}</td>")
            parts.append(f"<td>{_esc(rec.get('stop_loss', ''))}</td>")
            parts.append(f"<td>{_esc(rec.get('take_profit', ''))}</td>")
            parts.append(f"<td>{_esc(rec.get('reason', ''))}</td>")
            parts.append("</tr>")
        parts.append("</table>")

    if sell_recs and normalized is None:
        section += 1
        parts.append(f"<h2>{section}. 매도 추천</h2>")
        parts.append("<table><tr><th>종목</th><th>수량</th><th>익절가</th><th>손절가</th><th>타이밍</th><th>근거</th></tr>")
        for rec in sell_recs:
            parts.append("<tr>")
            parts.append(f"<td><b>{_esc(rec.get('ticker', ''))}</b></td>")
            parts.append(f"<td>{_esc(rec.get('shares', ''))}</td>")
            parts.append(f"<td>{_esc(rec.get('take_profit', ''))}</td>")
            parts.append(f"<td>{_esc(rec.get('stop_loss', ''))}</td>")
            parts.append(f"<td>{_esc(rec.get('timing', ''))}</td>")
            parts.append(f"<td>{_esc(rec.get('reason', ''))}</td>")
            parts.append("</tr>")
        parts.append("</table>")

    if account_strategy:
        section += 1
        parts.append(f"<h2>{section}. 계좌별 전략</h2>")
        parts.append("<table><tr><th>계좌</th><th>전략</th></tr>")
        for acct in ("ISA", "RIA", "일반", "연금_IRP"):
            strategy = account_strategy.get(acct, "")
            if strategy:
                parts.append(f"<tr><td><b>{_esc(acct)}</b></td><td>{_esc(strategy)}</td></tr>")
        parts.append("</table>")

    if opportunities:
        parts.append('<div class="opp"><b>💡 호재 / 기회</b><ul>')
        for o in opportunities:
            parts.append(f"<li>{_esc(o)}</li>")
        parts.append("</ul></div>")

    if risks:
        parts.append('<div class="warn"><b>⚠️ 리스크</b><ul>')
        for r in risks:
            parts.append(f"<li>{_esc(r)}</li>")
        parts.append("</ul></div>")

    if scenarios:
        section += 1
        parts.append(f"<h2>{section}. 시나리오</h2>")
        parts.append("<table><tr><th>시나리오</th><th>조건</th><th>액션</th><th>금액/수량</th></tr>")
        for s in scenarios:
            parts.append("<tr>")
            parts.append(f"<td><b>{_esc(s.get('label', ''))}</b></td>")
            parts.append(f"<td>{_esc(s.get('condition', ''))}</td>")
            parts.append(f"<td>{_esc(s.get('action', ''))}</td>")
            parts.append(f"<td>{_esc(s.get('amount', ''))}</td>")
            parts.append("</tr>")
        parts.append("</table>")

    if checklist:
        section += 1
        parts.append(f"<h2>{section}. 체크리스트</h2><ul>")
        emoji_map = {"충족": "✅", "미충족": "❌", "부분충족": "🟡"}
        for c in checklist:
            mark = emoji_map.get(c.get("status", ""), "☐")
            parts.append(
                f"<li>{mark} <b>{_esc(c.get('condition', ''))}</b>: "
                f"{_esc(c.get('detail', ''))}</li>",
            )
        parts.append("</ul>")

    if strategy_summary:
        section += 1
        parts.append(f"<h2>{section}. 전략 요약</h2>")
        for p in _split_into_paragraphs(strategy_summary):
            parts.append(f"<p>{_esc(p)}</p>")

    parts.append('<div class="footer">')
    parts.append("<b>면책:</b> 본 분석은 정보 제공 목적이며 투자 권유가 아닙니다. 최종 판단은 본인 책임이며, 투자 손실에 대한 어떠한 책임도 지지 않습니다.<br>")
    parts.append(f"생성: Claude Code (Sanjuk-Stock-Simulator) / {_esc(now_str)}")
    parts.append("</div>")

    parts.append("</body></html>")

    return "".join(parts)


def send_briefing_email(
    result: BriefingResult,
    notion_page_id: str,
    briefing_type: str = "MANUAL",
) -> bool:
    """브리핑 결과를 Gmail로 HTML 메일 전송 (텍스트 fallback 포함)."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        log.warning("Gmail 설정 없음 — 메일 건너뜀")
        return False

    from core.notion import LABEL_MAP
    from core.telegram import _build_briefing_message

    label = LABEL_MAP.get(briefing_type, "📊 수시 브리핑")
    raw = result.raw_json
    title = result.title or datetime.now(KST).strftime("%Y.%m.%d %H:%M 브리핑")

    body_html = _build_briefing_html(result, raw, label, title)
    body_text = _markdown_to_plain(_build_briefing_message(result, raw, label, title, ""))

    clean_label = re.sub(r"[^\w가-힣 ]", "", label).strip()
    subject = f"[Sanjuk-Stock][{clean_label}] {title}"

    # 브리핑 아카이브 저장 (실패해도 전송 계속)
    try:
        from core.briefing_archive import save_briefing_archive
        save_briefing_archive(
            briefing_type=briefing_type, title=title, subject=subject,
            body_text=body_text, body_html=body_html,
            raw_json=raw, channel="email",
        )
    except Exception as e:
        log.warning("briefing archive hook failed: %s", e)

    return send_email(subject, body_text, body_html=body_html)
