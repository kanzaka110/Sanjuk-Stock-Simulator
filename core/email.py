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


def send_briefing_email(
    result: BriefingResult,
    notion_page_id: str,
    briefing_type: str = "MANUAL",
) -> bool:
    """브리핑 결과를 Gmail로 전송. 텔레그램 메시지 본문 재활용."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        log.warning("Gmail 설정 없음 — 메일 건너뜀")
        return False

    from core.notion import LABEL_MAP
    from core.telegram import _build_briefing_message

    label = LABEL_MAP.get(briefing_type, "📊 수시 브리핑")
    raw = result.raw_json
    title = result.title or datetime.now(KST).strftime("%Y.%m.%d %H:%M 브리핑")
    notion_url = f"https://notion.so/{notion_page_id.replace('-', '')}" if notion_page_id else ""

    body_md = _build_briefing_message(result, raw, label, title, notion_url)
    body_text = _markdown_to_plain(body_md)

    clean_label = re.sub(r'[^\w가-힣 ]', '', label).strip()
    subject = f"[{clean_label}] {title}"

    return send_email(subject, body_text)
