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

    # 매수 전략
    buy_lines: list[str] = []
    for sig in result.buy_signals:
        line = f"{sig.urgency} {sig.name}"
        if sig.shares:
            line += f" [{sig.shares}]"
        line += f"\n▸ {sig.entry_price} → {sig.target_price} ✂ {sig.stop_loss}"
        buy_lines.append(line)

    # 매도 전략
    sell_lines: list[str] = []
    for sig in result.sell_signals:
        line = f"{sig.urgency} {sig.name}"
        if sig.shares:
            line += f" [{sig.shares}]"
        line += f"\n▸ 익절 {sig.target_price} ✂ {sig.stop_loss}"
        sell_lines.append(line)

    # 메시지 조립
    msg = f"📊 {label}\n{title}\n\n"
    if result.advisor_oneliner:
        msg += f"💬 {result.advisor_oneliner}\n\n"
    if buy_lines:
        msg += "🟢 매수\n" + "\n".join(buy_lines) + "\n\n"
    if sell_lines:
        msg += "🔴 매도\n" + "\n".join(sell_lines) + "\n\n"

    next_action = raw.get("next_action", "")
    msg += f"🎯 AI: {result.advisor_verdict}\n▶ {next_action}\n\n"
    msg += f"📋 [Notion 상세보기]({notion_url})"

    return _send_message(msg)


def send_simple_message(text: str) -> bool:
    """단순 텍스트 메시지 전송."""
    return _send_message(text)


def _send_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        res = requests.post(url, json=payload, timeout=30)
        if res.status_code == 200:
            log.info("텔레그램 전송 완료")
            return True
        log.warning(f"텔레그램 전송 실패: {res.status_code} {res.text[:200]}")
        return False
    except Exception as e:
        log.error(f"텔레그램 전송 오류: {e}")
        return False
