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
    notion_url = f"https://notion.so/{notion_page_id.replace('-', '')}"

    msg = _build_briefing_message(result, raw, label, title, notion_url)
    return _send_message(msg)


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
    lines.append(f"📋  [Notion 상세보기]({notion_url})")

    return "\n".join(lines)


def send_simple_message(text: str) -> bool:
    """단순 텍스트 메시지 전송."""
    return _send_message(text)


def _send_message(text: str) -> bool:
    """텔레그램 메시지 전송. 4096자 초과 시 자동 분할."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    MAX_LEN = 4000  # 약간 여유 (마크다운 파싱 오버헤드)
    chunks = _split_message(text, MAX_LEN)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    all_ok = True
    for chunk in chunks:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            res = requests.post(url, json=payload, timeout=30)
            if res.status_code != 200:
                log.warning(f"텔레그램 전송 실패: {res.status_code} {res.text[:200]}")
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
