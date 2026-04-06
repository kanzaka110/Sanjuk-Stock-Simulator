"""
알림 팬아웃 — Freqtrade RPCManager 패턴

다채널 알림 추상화: 텔레그램, Notion, 웹훅 등을
단일 인터페이스로 관리.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

log = logging.getLogger(__name__)


class MessageType(Enum):
    """알림 메시지 유형."""

    BRIEFING = "briefing"
    SIGNAL = "signal"
    WARNING = "warning"
    CIRCUIT_BREAKER = "circuit_breaker"
    STATUS = "status"


@dataclass(frozen=True)
class NotifyMessage:
    """알림 메시지."""

    msg_type: MessageType
    title: str
    body: str
    data: dict | None = None


class NotifyHandler(Protocol):
    """알림 핸들러 인터페이스."""

    def send(self, message: NotifyMessage) -> bool: ...


class TelegramHandler:
    """텔레그램 알림 핸들러."""

    def send(self, message: NotifyMessage) -> bool:
        from core.telegram import send_simple_message
        text = f"📢 {message.title}\n\n{message.body}"
        return send_simple_message(text)


class WebhookHandler:
    """웹훅 알림 핸들러 (Slack, Discord, IFTTT 등)."""

    def __init__(self, url: str) -> None:
        self._url = url

    def send(self, message: NotifyMessage) -> bool:
        import requests
        try:
            payload = {
                "type": message.msg_type.value,
                "title": message.title,
                "body": message.body,
            }
            if message.data:
                payload["data"] = message.data
            res = requests.post(self._url, json=payload, timeout=10)
            return res.status_code < 400
        except Exception as e:
            log.warning(f"웹훅 전송 실패: {e}")
            return False


class NotifyManager:
    """알림 매니저 — 등록된 모든 핸들러에 팬아웃."""

    def __init__(self) -> None:
        self._handlers: list[NotifyHandler] = []

    def register(self, handler: NotifyHandler) -> None:
        self._handlers.append(handler)

    def send(self, message: NotifyMessage) -> int:
        """모든 핸들러에 전송. 성공 건수 반환."""
        success = 0
        for handler in self._handlers:
            try:
                if handler.send(message):
                    success += 1
            except Exception as e:
                log.warning(f"알림 핸들러 실패: {e}")
        return success

    def send_briefing(self, title: str, body: str, data: dict | None = None) -> int:
        return self.send(NotifyMessage(MessageType.BRIEFING, title, body, data))

    def send_warning(self, title: str, body: str) -> int:
        return self.send(NotifyMessage(MessageType.WARNING, title, body))

    def send_circuit_breaker(self, reason: str) -> int:
        return self.send(NotifyMessage(
            MessageType.CIRCUIT_BREAKER,
            "🚨 서킷 브레이커 발동",
            reason,
        ))


# 글로벌 인스턴스
_manager: NotifyManager | None = None


def get_notifier() -> NotifyManager:
    """글로벌 NotifyManager 인스턴스."""
    global _manager
    if _manager is None:
        _manager = NotifyManager()
        # 텔레그램 자동 등록
        from config.settings import TELEGRAM_BOT_TOKEN
        if TELEGRAM_BOT_TOKEN:
            _manager.register(TelegramHandler())
    return _manager
