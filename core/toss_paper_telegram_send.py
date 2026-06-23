"""
Toss Paper 주문표 Telegram 발송 — 별도 sender (core/telegram.py 무변경)

InlineKeyboard 버튼이 달린 paper 주문표를 Telegram으로 발송한다.
실제 주문 0건. dry_run=True 강제.
"""

from __future__ import annotations

import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

# env에서 직접 읽기 (core/telegram.py import 없이 독립)
def _get_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _get_chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "")


def send_toss_paper_preview_message(
    text: str,
    inline_keyboard: list[list[dict]],
) -> bool:
    """InlineKeyboard 버튼이 달린 paper 주문표 Telegram 발송.

    실주문 없음. paper/dry-run 메시지 전용.
    """
    token = _get_token()
    chat_id = _get_chat_id()
    if not token or not chat_id:
        logger.warning("Telegram 미설정 — paper 주문표 발송 불가")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text[:4000],
        "disable_web_page_preview": True,
        "reply_markup": json.dumps({"inline_keyboard": inline_keyboard}),
    }
    try:
        res = requests.post(url, json=payload, timeout=30)
        if res.status_code == 200:
            logger.info("Paper 주문표 발송 완료")
            return True
        logger.warning("Paper 주문표 발송 실패: %d %s", res.status_code, res.text[:160])
        return False
    except Exception as e:
        logger.error(f"Paper 주문표 발송 오류: {e}")
        return False
