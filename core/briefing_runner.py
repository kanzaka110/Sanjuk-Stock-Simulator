"""
브리핑 실행 공통 모듈

API 서버와 텔레그램 봇이 동일한 브리핑 파이프라인을 공유한다.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

log = logging.getLogger(__name__)

_briefing_lock = threading.Lock()


@dataclass(frozen=True)
class BriefingRunResult:
    """브리핑 실행 결과."""

    success: bool
    title: str = ""
    notion_url: str = ""
    telegram_sent: bool = False
    error: str = ""


def run_briefing(briefing_type: str = "MANUAL") -> BriefingRunResult:
    """브리핑 파이프라인 실행 (동기).

    동시 실행 방지 Lock 포함.
    시장 데이터 수집 → AI 분석 → Notion 저장 → 텔레그램 전송.

    Returns:
        BriefingRunResult
    """
    if not _briefing_lock.acquire(blocking=False):
        return BriefingRunResult(
            success=False,
            error="브리핑이 이미 진행 중입니다",
        )

    try:
        return _execute_briefing(briefing_type)
    finally:
        _briefing_lock.release()


def is_briefing_running() -> bool:
    """브리핑 실행 중 여부."""
    return _briefing_lock.locked()


def _execute_briefing(briefing_type: str) -> BriefingRunResult:
    """브리핑 실제 실행."""
    from core.analyzer import analyze
    from core.market import fetch_market
    from core.notion import save_to_notion
    from core.telegram import send_briefing_telegram

    try:
        log.info(f"브리핑 시작: {briefing_type}")
        snapshot = fetch_market(briefing_type)
        result = analyze(snapshot, briefing_type)

        notion_url = ""
        page_id = ""
        try:
            page_id = save_to_notion(result, snapshot, briefing_type)
            notion_url = f"https://notion.so/{page_id.replace('-', '')}"
        except Exception as e:
            log.warning(f"Notion 저장 실패: {e}")

        telegram_sent = send_briefing_telegram(result, page_id, briefing_type)

        log.info(f"브리핑 완료: {result.title}")
        return BriefingRunResult(
            success=True,
            title=result.title,
            notion_url=notion_url,
            telegram_sent=telegram_sent,
        )
    except Exception as e:
        log.error(f"브리핑 실패: {e}")
        return BriefingRunResult(success=False, error=str(e))
