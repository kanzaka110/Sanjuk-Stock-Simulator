"""
Permission 계층 — claw-code 패턴 적용

운영 모드별 허용 동작을 제한하여 안전성 확보.
- ANALYSIS: 읽기 전용 (시세 조회, 분석)
- BACKTEST: 읽기 + 로컬 시뮬레이션
- MONITOR: 읽기 + 알림 전송
- BRIEFING: 읽기 + 분석 + Notion/텔레그램 전송
- ADMIN: 모든 동작 허용
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class OperationMode(str, Enum):
    """운영 모드."""

    ANALYSIS = "ANALYSIS"       # 분석만 (터미널 대화)
    BACKTEST = "BACKTEST"       # 백테스트 (로컬 시뮬레이션)
    MONITOR = "MONITOR"         # 모니터링 (알림 전송)
    BRIEFING = "BRIEFING"       # 브리핑 (전체 파이프라인)
    ADMIN = "ADMIN"             # 관리자 (모든 동작)


class Action(str, Enum):
    """시스템 동작."""

    # 읽기
    FETCH_PRICE = "FETCH_PRICE"
    FETCH_NEWS = "FETCH_NEWS"
    CALCULATE_INDICATORS = "CALCULATE_INDICATORS"
    READ_PORTFOLIO = "READ_PORTFOLIO"

    # 분석
    RUN_AI_ANALYSIS = "RUN_AI_ANALYSIS"
    RUN_BACKTEST = "RUN_BACKTEST"
    RUN_PERSONA = "RUN_PERSONA"

    # 쓰기/전송
    SEND_TELEGRAM = "SEND_TELEGRAM"
    SAVE_NOTION = "SAVE_NOTION"
    SAVE_MEMORY = "SAVE_MEMORY"
    UPDATE_PRICE_DB = "UPDATE_PRICE_DB"

    # 관리
    MODIFY_SETTINGS = "MODIFY_SETTINGS"
    RESTART_SERVICE = "RESTART_SERVICE"


# 모드별 허용 동작 매핑
_MODE_PERMISSIONS: dict[OperationMode, frozenset[Action]] = {
    OperationMode.ANALYSIS: frozenset({
        Action.FETCH_PRICE, Action.FETCH_NEWS,
        Action.CALCULATE_INDICATORS, Action.READ_PORTFOLIO,
        Action.RUN_AI_ANALYSIS, Action.RUN_PERSONA,
    }),
    OperationMode.BACKTEST: frozenset({
        Action.FETCH_PRICE, Action.FETCH_NEWS,
        Action.CALCULATE_INDICATORS, Action.READ_PORTFOLIO,
        Action.RUN_BACKTEST,
    }),
    OperationMode.MONITOR: frozenset({
        Action.FETCH_PRICE, Action.CALCULATE_INDICATORS,
        Action.READ_PORTFOLIO,
        Action.RUN_AI_ANALYSIS,
        Action.SEND_TELEGRAM,
    }),
    OperationMode.BRIEFING: frozenset({
        Action.FETCH_PRICE, Action.FETCH_NEWS,
        Action.CALCULATE_INDICATORS, Action.READ_PORTFOLIO,
        Action.RUN_AI_ANALYSIS, Action.RUN_PERSONA,
        Action.SEND_TELEGRAM, Action.SAVE_NOTION,
        Action.SAVE_MEMORY, Action.RUN_BACKTEST,
    }),
    OperationMode.ADMIN: frozenset(Action),
}


class PermissionPolicy:
    """운영 모드 기반 권한 정책."""

    def __init__(self, mode: OperationMode = OperationMode.BRIEFING) -> None:
        self._mode = mode
        self._allowed = _MODE_PERMISSIONS.get(mode, frozenset())

    @property
    def mode(self) -> OperationMode:
        return self._mode

    def is_allowed(self, action: Action) -> bool:
        """동작 허용 여부."""
        return action in self._allowed

    def check(self, action: Action) -> None:
        """동작 검사 — 불허 시 PermissionError 발생."""
        if not self.is_allowed(action):
            msg = f"[{self._mode.value}] 모드에서 {action.value} 불허"
            log.warning(msg)
            raise PermissionError(msg)

    def allowed_actions(self) -> frozenset[Action]:
        """현재 모드에서 허용된 모든 동작."""
        return self._allowed


# 글로벌 정책 (기본: BRIEFING)
_current_policy: PermissionPolicy | None = None


def get_policy() -> PermissionPolicy:
    """현재 권한 정책 반환."""
    global _current_policy
    if _current_policy is None:
        _current_policy = PermissionPolicy(OperationMode.BRIEFING)
    return _current_policy


def set_mode(mode: OperationMode) -> PermissionPolicy:
    """운영 모드 변경."""
    global _current_policy
    _current_policy = PermissionPolicy(mode)
    log.info("운영 모드 변경: %s", mode.value)
    return _current_policy


def mode_from_command(command: str) -> OperationMode:
    """CLI 명령어에서 운영 모드 추론."""
    mapping: dict[str, OperationMode] = {
        "briefing": OperationMode.BRIEFING,
        "monitor": OperationMode.MONITOR,
        "bot": OperationMode.BRIEFING,  # 봇은 브리핑도 할 수 있으므로
        "price": OperationMode.ADMIN,
        "": OperationMode.ANALYSIS,     # TUI
    }
    return mapping.get(command, OperationMode.ANALYSIS)
