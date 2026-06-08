"""
텔레그램 알림 전송 모듈

브리핑 결과를 텔레그램으로 전송하는 기능만 제공.
대화/챗봇 기능은 Claude Code 터미널에서 직접 수행.
"""

from __future__ import annotations

import logging
from datetime import datetime

import requests

from config.settings import (
    KST,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from core.market import signal_badge
from core.models import BriefingResult

log = logging.getLogger(__name__)

import re as _re


def _sanitize_markdown(text: str) -> str:
    """Telegram Markdown 파싱 오류 방지를 위한 정제.

    - 짝이 안 맞는 *bold* 마커 제거
    - 짝이 안 맞는 _italic_ 마커 제거
    - 짝이 안 맞는 `code` 마커 제거
    """
    # 각 마커별로 짝수인지 확인, 홀수면 마지막 하나 제거
    for marker in ("*", "_", "`"):
        count = text.count(marker)
        if count % 2 != 0:
            # 마지막 등장 위치의 마커 제거
            idx = text.rfind(marker)
            text = text[:idx] + text[idx + 1:]
    return text



# ─── 브리핑 알림 전송 ───────────────────────────────────
def send_briefing_telegram(
    result: BriefingResult,
    notion_page_id: str,
    briefing_type: str = "MANUAL",
) -> bool:
    """브리핑 결과를 텔레그램으로 전송.

    Returns:
        성공 여부
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("텔레그램 설정 없음 — 건너뜀")
        return False

    from core.notion import LABEL_MAP
    label = LABEL_MAP.get(briefing_type, "📊 수시 브리핑")

    raw = result.raw_json
    title = result.title or datetime.now(KST).strftime("%Y.%m.%d %H:%M 브리핑")

    msg = _build_impact_message(result, raw, label, title)
    return _send_message(msg)


def _verdict_emoji(v: str) -> str:
    return {"매수": "🟢", "매도": "🔴", "홀딩": "🟡", "관망": "⚪"}.get(v, "⚪")


def _persona_short(name: str) -> str:
    return {
        "가치투자자": "가치", "성장투자자": "성장",
        "기술적분석가": "기술", "매크로분석가": "매크로",
    }.get(name, name)


def _build_impact_message(
    result: BriefingResult,
    raw: dict,
    label: str,
    title: str,
) -> str:
    """텔레그램용 임팩트 메시지. 한눈에 보이되 핵심 정보만 — 중간 강도."""
    verdict = raw.get("advisor_verdict", "") or "—"
    oneliner = raw.get("advisor_oneliner", "") or ""
    next_action = raw.get("next_action", "") or ""
    persona_details = raw.get("persona_details", []) or []
    persona_summary = raw.get("persona_summary", {}) or {}
    opportunities = raw.get("advisor_opportunities", []) or []
    risks = raw.get("advisor_risks", []) or []
    buy_recs = raw.get("buy_recommendations", []) or raw.get("strategy_buy", []) or []
    sell_recs = raw.get("sell_recommendations", []) or raw.get("strategy_sell", []) or []

    SEP = "━━━━━━━━━━━━━━━━━━"

    lines: list[str] = []

    # 헤더 — 결론 강조
    lines.append(f"{label}")
    lines.append(f"📅 {title}")
    # 품질 배지
    if result.quality_warnings:
        lines.append(f"⚠️ 부분 분석: {', '.join(result.quality_warnings)}")
    lines.append("")
    lines.append(f"🎯 *판단: {verdict}*")
    if oneliner:
        lines.append(f"💬 {oneliner}")
    lines.append("")

    # 매수 추천 (한 줄에 핵심만)
    if buy_recs:
        lines.append(SEP)
        lines.append("💰 *매수 추천*")
        for rec in buy_recs[:3]:
            ticker = rec.get("ticker", "")
            name = rec.get("name", "")
            account = rec.get("account", "")
            entry = rec.get("entry_price", "")
            shares = rec.get("shares", "")
            display = name or ticker
            parts = [f"▸ *{display}*"]
            if account:
                parts.append(account)
            if shares:
                parts.append(shares)
            if entry:
                parts.append(f"진입 {entry}")
            lines.append(" · ".join(parts))
        lines.append("")

    # 야간 프리브리핑: 예약 주문 요약 (매수 + 매도)
    night_orders = raw.get("night_orders", []) or []
    if night_orders:
        lines.append(SEP)
        lines.append("🌙 *내일 예약 주문*")
        for order in night_orders:
            side = order.get("구분", "")
            if not side:
                log.warning("night_orders에 '구분' 필드 누락 — 주문 스킵: %s", order)
                continue
            name = order.get("종목", "")
            acct = order.get("계좌", "")
            price = order.get("지정가", "")
            qty = order.get("수량", "")
            valid = order.get("유효시간", order.get("유효기간", ""))
            cond = order.get("조건", "")
            reason = order.get("사유", "")
            side_icon = "🟢매수" if side == "매수" else "🔴매도"
            lines.append(f"{side_icon} *{name}* {acct} {price} × {qty}")
            if valid:
                lines.append(f"  ⏰ {valid}")
            if reason:
                lines.append(f"  💬 {reason}")
            if cond:
                lines.append(f"  📌 {cond}")
        gap = raw.get("gap_scenarios", {}) or {}
        if gap:
            lines.append("")
            lines.append("📊 *갭 시나리오*")
            for scenario, action in gap.items():
                lines.append(f"• {scenario}: {action}")
        lines.append("")

    # 매도 추천 (한 줄)
    if sell_recs:
        lines.append(SEP)
        lines.append("📉 *매도 추천*")
        for rec in sell_recs[:3]:
            ticker = rec.get("ticker", "")
            name = rec.get("name", "")
            shares = rec.get("shares", "")
            timing = rec.get("timing", "")
            display = name or ticker
            parts = [f"▸ *{display}*"]
            if shares:
                parts.append(shares)
            if timing:
                parts.append(timing)
            lines.append(" · ".join(parts))
        lines.append("")

    # 페르소나 — 한 줄씩 (key_factors[0]만)
    lines.append(SEP)
    lines.append("👥 *페르소나*")
    if persona_details:
        for pd in persona_details:
            name = _persona_short(pd.get("persona", ""))
            v = pd.get("verdict", "")
            conf = pd.get("confidence", 0)
            kf_list = pd.get("key_factors", []) or []
            point = kf_list[0] if kf_list else (pd.get("reasoning", "") or "")[:50]
            lines.append(f"{_verdict_emoji(v)} *{name}* {v} {conf}% — {point}")
    elif persona_summary:
        for full_name in ("가치투자자", "성장투자자", "기술적분석가", "매크로분석가"):
            s = persona_summary.get(full_name, "")
            if s:
                lines.append(f"⚪ *{_persona_short(full_name)}* {s}")
    lines.append("")

    # 호재/리스크 (각 2개)
    if opportunities or risks:
        lines.append(SEP)
        if opportunities:
            lines.append("⚡ *호재*")
            for o in opportunities[:2]:
                lines.append(f"• {o}")
        if risks:
            if opportunities:
                lines.append("")
            lines.append("⚠️ *리스크*")
            for r in risks[:2]:
                lines.append(f"• {r}")
        lines.append("")

    # 다음 액션
    if next_action:
        lines.append(SEP)
        lines.append(f"⏭️ *다음 액션*\n{next_action}")
        lines.append("")

    # 푸터
    lines.append(SEP)
    lines.append("📧 *상세 분석은 메일로 발송*")
    lines.append(
        "[📬 Gmail 열기](https://mail.google.com/) · "
        "[🔍 검색](https://mail.google.com/mail/u/0/#search/Sanjuk-Stock)",
    )

    return "\n".join(lines)


def _build_summary_message(
    result: BriefingResult,
    raw: dict,
    label: str,
    title: str,
    notion_url: str,
) -> str:
    """텔레그램용 핵심 요약 메시지. 상세 내용은 메일로 발송됨."""
    GMAIL_INBOX_URL = "https://mail.google.com/"
    GMAIL_SEARCH_URL = "https://mail.google.com/mail/u/0/#search/Sanjuk-Stock"

    verdict = raw.get("advisor_verdict", "") or "—"
    oneliner = raw.get("advisor_oneliner", "") or ""
    next_action = raw.get("next_action", "") or ""

    persona_summary = raw.get("persona_summary", {}) or {}

    lines: list[str] = []
    lines.append(f"{label}")
    lines.append(f"━━━━━━━━━━━━━━━━━━")
    lines.append(f"📌 {title}")
    lines.append("")
    lines.append(f"🎯 판단: *{verdict}*")
    if oneliner:
        lines.append(f"💬 {oneliner}")
    lines.append("")

    if persona_summary:
        lines.append("👥 페르소나 요약")
        for name in ("가치투자자", "성장투자자", "기술적분석가", "매크로분석가"):
            summary = persona_summary.get(name, "").strip()
            if summary:
                lines.append(f"• {name}: {summary}")
        lines.append("")

    if next_action:
        lines.append(f"⏭ 다음 액션: {next_action}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("📧 상세 분석은 메일로 발송 (검색어: Sanjuk-Stock)")
    lines.append(f"[📬 Gmail 열기]({GMAIL_INBOX_URL}) | [🔍 검색 결과]({GMAIL_SEARCH_URL})")

    return "\n".join(lines)


def _strip_leading_emoji(text: str) -> str:
    """텍스트 앞의 이모지를 제거 (섹션 아이콘과 중복 방지)."""
    import re
    # 유니코드 이모지 패턴 (연속된 이모지 + 공백 제거)
    return re.sub(
        r'^[\U0001F300-\U0001FAFF\U00002702-\U000027B0\U0000FE00-\U0000FE0F\U0000200D\u2600-\u27BF]+\s*',
        '', text,
    ).strip()


def _wrap_text(text: str, width: int = 40) -> list[str]:
    """긴 텍스트를 width 글자 근처에서 줄바꿈. 마침표/쉼표 우선 분리."""
    if len(text) <= width:
        return [text]

    result: list[str] = []
    while len(text) > width:
        # width 근처에서 마침표/쉼표/공백 찾기
        cut = -1
        for sep in ['. ', ', ', ' ']:
            idx = text.rfind(sep, 0, width + 5)
            if idx > width // 2:
                cut = idx + len(sep)
                break
        if cut == -1:
            cut = width
        result.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        result.append(text)
    return result


def _split_numbered_items(text: str) -> list[str]:
    """①②③ 또는 1. 2. 3. 번호가 있는 텍스트를 항목별로 분리."""
    import re
    # ①②③④⑤ 패턴
    parts = re.split(r'\s*([①②③④⑤⑥⑦⑧⑨⑩])', text)
    if len(parts) > 2:
        items: list[str] = []
        for i in range(1, len(parts), 2):
            num = parts[i]
            body = parts[i + 1].strip() if i + 1 < len(parts) else ""
            items.append(f"{num} {body}")
        return items

    # 1) 2) 또는 1. 2. 패턴
    parts = re.split(r'\s*(\d+[.)]\s)', text)
    if len(parts) > 2:
        items = []
        for i in range(1, len(parts), 2):
            num = parts[i].strip()
            body = parts[i + 1].strip() if i + 1 < len(parts) else ""
            items.append(f"{num} {body}")
        return items

    return [text]


def _build_urgent_alert(
    result: BriefingResult,
    raw: dict,
) -> list[str]:
    """🔥강력 매수 / 🔴즉시 매도 / 매수실행·매도실행 판단 시 긴급 알림 생성."""
    lines: list[str] = []
    raw_buy = raw.get("strategy_buy", [])
    raw_sell = raw.get("strategy_sell", [])

    # 🔥강력 매수
    for sig in result.buy_signals:
        if "강력" in sig.urgency:
            matching = next((r for r in raw_buy if r.get("ticker") == sig.ticker), {})
            account = matching.get("account", "")
            acct_tag = f" {account}" if account else ""
            lines.append(f"🔥 *매수 실행:  {sig.name}*{acct_tag}")
            lines.append(f"    진입 {sig.entry_price}  →  목표 {sig.target_price}")
            lines.append(f"    손절 {sig.stop_loss}  |  수량 {sig.shares}")
            if sig.timing:
                lines.append(f"    ⏰ {sig.timing[:40]}")
            lines.append("")

    # ⚡적극 매수
    for sig in result.buy_signals:
        if "적극" in sig.urgency:
            matching = next((r for r in raw_buy if r.get("ticker") == sig.ticker), {})
            account = matching.get("account", "")
            acct_tag = f" {account}" if account else ""
            lines.append(f"⚡ *적극 매수:  {sig.name}*{acct_tag}")
            lines.append(f"    진입 {sig.entry_price}  →  목표 {sig.target_price}")
            lines.append(f"    손절 {sig.stop_loss}  |  수량 {sig.shares}")
            lines.append("")

    # 🔴즉시 매도
    for sig in result.sell_signals:
        if "즉시" in sig.urgency:
            matching = next((r for r in raw_sell if r.get("ticker") == sig.ticker), {})
            account = matching.get("account", "")
            shares = matching.get("shares", "")
            acct_tag = f" {account}" if account else ""
            lines.append(f"🔴 *즉시 매도:  {sig.name}*{acct_tag}")
            lines.append(f"    익절 {sig.target_price}  |  손절 {sig.stop_loss}")
            if shares:
                lines.append(f"    수량 {shares}")
            lines.append("")

    # 🟠주의 매도
    for sig in result.sell_signals:
        if "주의" in sig.urgency:
            matching = next((r for r in raw_sell if r.get("ticker") == sig.ticker), {})
            account = matching.get("account", "")
            acct_tag = f" {account}" if account else ""
            lines.append(f"🟠 *매도 주의:  {sig.name}*{acct_tag}")
            lines.append(f"    익절 {sig.target_price}  |  손절 {sig.stop_loss}")
            lines.append("")

    # investment_decision이 매수실행/매도실행인데 위에서 안 잡힌 경우
    decision = result.investment_decision
    if not lines and decision in ("매수실행", "매도실행"):
        icon = "🟢" if decision == "매수실행" else "🔴"
        lines.append(f"{icon} *{decision}* — Notion 상세 확인 필요")
        lines.append("")

    return lines


def _build_briefing_message(
    result: BriefingResult,
    raw: dict,
    label: str,
    title: str,
    notion_url: str,
) -> str:
    """텔레그램 브리핑 메시지 조립."""
    lines: list[str] = []

    # ── 헤더 ──
    # label에서 이모지 제거 (📊 중복 방지)
    clean_label = _strip_leading_emoji(label)
    lines.append(f"{'━' * 24}")
    lines.append(f"📊  *{clean_label}*")
    lines.append(f"_{title}_")
    lines.append(f"{'━' * 24}")
    lines.append("")

    # ── AI 핵심 판단 ──
    verdict_icon = {
        "매수대기": "⏸️", "소액분할": "🔵",
        "적극매수": "🟢", "매도고려": "🔴",
    }.get(result.advisor_verdict, "💡")
    lines.append(f"{verdict_icon}  *AI 판단:  {result.advisor_verdict}*")
    lines.append("")
    if result.advisor_oneliner:
        oneliner_lines = _wrap_text(result.advisor_oneliner, 38)
        lines.append(f"💬  {oneliner_lines[0]}")
        for ol in oneliner_lines[1:]:
            lines.append(f"      {ol}")
        lines.append("")

    # ── 긴급 액션 알림 (🔥강력 매수 / 🔴즉시 매도) ──
    urgent_actions = _build_urgent_alert(result, raw)
    if urgent_actions:
        lines.append(f"{'━' * 24}")
        lines.append("🚨🚨🚨  *긴급 액션 필요*  🚨🚨🚨")
        lines.append("")
        lines.extend(urgent_actions)
        lines.append(f"{'━' * 24}")
        lines.append("")

    # ── 매수 전략 ──
    if result.buy_signals:
        lines.append("")
        lines.append(f"{'─' * 24}")
        lines.append("🟢  *매수 액션*")
        lines.append("")
        raw_buy = raw.get("strategy_buy", [])
        for sig in result.buy_signals:
            matching = next((r for r in raw_buy if r.get("ticker") == sig.ticker), {})
            account = matching.get("account", "")
            acct_tag = f" {account}" if account else ""
            lines.append(f"{sig.urgency}  *{sig.name}*{acct_tag}")
            lines.append(f"    진입 {sig.entry_price}  →  목표 {sig.target_price}")
            lines.append(f"    손절 {sig.stop_loss}  |  수량 {sig.shares}")
            if sig.timing:
                lines.append(f"    ⏰ {sig.timing[:50]}")
            lines.append("")

    # ── 매도 전략 ──
    if result.sell_signals:
        lines.append("")
        lines.append(f"{'─' * 24}")
        lines.append("🔴  *매도 / 주의*")
        lines.append("")
        raw_sell = raw.get("strategy_sell", [])
        for sig in result.sell_signals:
            matching = next((r for r in raw_sell if r.get("ticker") == sig.ticker), {})
            account = matching.get("account", "")
            shares = matching.get("shares", "")
            acct_tag = f" {account}" if account else ""
            lines.append(f"{sig.urgency}  *{sig.name}*{acct_tag}")
            lines.append(f"    익절 {sig.target_price}  |  손절 {sig.stop_loss}")
            if shares:
                lines.append(f"    수량 {shares}")
            lines.append("")

    # ── 매수도 매도도 없으면 ──
    if not result.buy_signals and not result.sell_signals:
        lines.append("")
        lines.append(f"{'─' * 24}")
        lines.append("⏸️  매수/매도 신호 없음 — 관망 유지")
        lines.append("")

    # ── 계좌별 전략 ──
    acct_strategy = raw.get("account_strategy", {})
    if acct_strategy:
        lines.append("")
        lines.append(f"{'─' * 24}")
        lines.append("🏦  *계좌별 전략*")
        lines.append("")
        acct_icons = {"ISA": "🟦", "RIA": "🟧", "일반": "⬜", "연금_IRP": "🟪"}
        for key, strategy in acct_strategy.items():
            if strategy:
                icon = acct_icons.get(key, "▪️")
                lines.append(f"{icon} *{key}*")
                # 전략 텍스트를 줄바꿈
                strategy_lines = _wrap_text(strategy, 36)
                for sl in strategy_lines:
                    lines.append(f"    {sl}")
                lines.append("")

    # ── 리스크 / 기회 ──
    risks = raw.get("advisor_risks", [])
    opps = raw.get("advisor_opportunities", [])
    if risks or opps:
        lines.append("")
        lines.append(f"{'─' * 24}")
        lines.append("⚖️  *리스크 vs 기회*")
        lines.append("")
        if risks:
            for r in risks[:3]:
                clean = _strip_leading_emoji(r)
                r_lines = _wrap_text(clean, 36)
                lines.append(f"⚠️ {r_lines[0]}")
                for rl in r_lines[1:]:
                    lines.append(f"    {rl}")
            lines.append("")
        if opps:
            for o in opps[:3]:
                clean = _strip_leading_emoji(o)
                o_lines = _wrap_text(clean, 36)
                lines.append(f"💡 {o_lines[0]}")
                for ol in o_lines[1:]:
                    lines.append(f"    {ol}")
            lines.append("")

    # ── 다음 액션 ──
    next_action = raw.get("next_action", "")
    if next_action:
        lines.append("")
        lines.append(f"{'─' * 24}")
        lines.append(f"🎯  *다음 액션*")
        lines.append("")
        # ①②③ 번호 항목 분리
        items = _split_numbered_items(next_action)
        for item in items:
            item_lines = _wrap_text(item.strip(), 36)
            lines.append(f"▶  {item_lines[0]}")
            for il in item_lines[1:]:
                lines.append(f"    {il}")
            lines.append("")

    # ── 푸터 ──
    lines.append("")
    lines.append(f"{'━' * 24}")
    lines.append("📧 *상세 분석은 메일로 발송*")

    return "\n".join(lines)


def send_simple_message(text: str) -> bool:
    """단순 텍스트 메시지 전송."""
    return _send_message(text)


def _send_message(text: str) -> bool:
    """텔레그램 메시지 전송. 4096자 초과 시 자동 분할.

    Markdown 파싱 실패 시 plain text로 자동 fallback (parse entities 오류 회피).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    MAX_LEN = 4000  # 약간 여유 (마크다운 파싱 오버헤드)
    chunks = _split_message(text, MAX_LEN)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    all_ok = True
    for chunk in chunks:
        # Markdown 특수문자 정제
        chunk = _sanitize_markdown(chunk)

        base_payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        try:
            # 1차 시도: Markdown
            res = requests.post(
                url,
                json={**base_payload, "parse_mode": "Markdown"},
                timeout=30,
            )
            if res.status_code == 200:
                continue

            # 2차 시도: parse entities 오류 시 plain text fallback
            if res.status_code == 400 and "parse" in res.text.lower():
                log.warning(
                    "텔레그램 Markdown 파싱 실패 → plain text 재시도: %s",
                    res.text[:160],
                )
                # Markdown 기호 제거 후 plain text
                plain = chunk.replace("*", "").replace("_", "").replace("`", "")
                res2 = requests.post(
                    url,
                    json={**base_payload, "text": plain},
                    timeout=30,
                )
                if res2.status_code == 200:
                    continue
                log.warning(
                    "텔레그램 plain text 재시도도 실패: %d %s",
                    res2.status_code,
                    res2.text[:160],
                )
                all_ok = False
            else:
                log.warning(
                    "텔레그램 전송 실패: %d %s",
                    res.status_code,
                    res.text[:200],
                )
                all_ok = False
        except Exception as e:
            log.error(f"텔레그램 전송 오류: {e}")
            all_ok = False

    if all_ok:
        log.info(f"텔레그램 전송 완료 ({len(chunks)}건)")
    return all_ok


def _split_message(text: str, max_len: int) -> list[str]:
    """긴 메시지를 구분선(━/─) 기준으로 분할."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""

    for line in text.split("\n"):
        # 이 줄을 추가하면 초과하는지 확인
        test = f"{current}\n{line}" if current else line
        if len(test) > max_len and current:
            chunks.append(current.rstrip())
            current = line
        else:
            current = test

    if current:
        chunks.append(current.rstrip())

    return chunks
