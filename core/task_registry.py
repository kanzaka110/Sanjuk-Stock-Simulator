"""
Task 레지스트리 — claw-code 패턴 적용

멀티 에이전트 실행 상태를 추적하는 상태머신.
페르소나 분석, 브리핑 파이프라인 등의 실행 이력과 상태를 관리.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    """작업 상태."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class AgentTask:
    """개별 에이전트 작업."""

    task_id: str
    name: str               # 예: "가치투자자", "fetch_market"
    task_type: str           # "persona", "pipeline_step", "briefing"
    status: TaskStatus = TaskStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: Any = None
    error: str = ""
    team_id: str = ""       # 소속 팀 (e.g., "briefing_20260408_0830")

    @property
    def duration_sec(self) -> float:
        """실행 시간 (초)."""
        if not self.started_at:
            return 0.0
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()


@dataclass
class TeamRun:
    """팀 실행 — 여러 태스크를 묶는 단위."""

    team_id: str
    name: str               # "persona_round1", "briefing_kr"
    task_ids: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @property
    def duration_sec(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()


class TaskRegistry:
    """글로벌 태스크 레지스트리 — 스레드 안전."""

    def __init__(self) -> None:
        self._tasks: dict[str, AgentTask] = {}
        self._teams: dict[str, TeamRun] = {}
        self._lock = threading.Lock()
        self._counter = 0

    def _next_id(self, prefix: str = "task") -> str:
        self._counter += 1
        return f"{prefix}_{int(time.time())}_{self._counter}"

    # ── Task CRUD ──────────────────────────────────
    def create_task(self, name: str, task_type: str,
                    team_id: str = "") -> AgentTask:
        """새 태스크 생성."""
        with self._lock:
            task = AgentTask(
                task_id=self._next_id("task"),
                name=name,
                task_type=task_type,
                team_id=team_id,
            )
            self._tasks[task.task_id] = task
            if team_id and team_id in self._teams:
                self._teams[team_id].task_ids.append(task.task_id)
            return task

    def start_task(self, task_id: str) -> None:
        """태스크 시작."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.status = TaskStatus.RUNNING
                task.started_at = datetime.now()

    def complete_task(self, task_id: str, result: Any = None) -> None:
        """태스크 완료."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.status = TaskStatus.COMPLETED
                task.completed_at = datetime.now()
                task.result = result
                log.debug(
                    "태스크 완료: %s [%s] (%.1fs)",
                    task.name, task.task_id, task.duration_sec,
                )

    def fail_task(self, task_id: str, error: str) -> None:
        """태스크 실패."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.status = TaskStatus.FAILED
                task.completed_at = datetime.now()
                task.error = error
                log.warning(
                    "태스크 실패: %s [%s] — %s",
                    task.name, task.task_id, error,
                )

    def get_task(self, task_id: str) -> AgentTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    # ── Team CRUD ──────────────────────────────────
    def create_team(self, name: str) -> TeamRun:
        """새 팀 실행 생성."""
        with self._lock:
            team = TeamRun(
                team_id=self._next_id("team"),
                name=name,
            )
            self._teams[team.team_id] = team
            return team

    def start_team(self, team_id: str) -> None:
        with self._lock:
            team = self._teams.get(team_id)
            if team:
                team.status = TaskStatus.RUNNING
                team.started_at = datetime.now()

    def complete_team(self, team_id: str) -> None:
        with self._lock:
            team = self._teams.get(team_id)
            if team:
                team.status = TaskStatus.COMPLETED
                team.completed_at = datetime.now()
                log.info(
                    "팀 완료: %s [%s] (%.1fs, %d tasks)",
                    team.name, team.team_id,
                    team.duration_sec, len(team.task_ids),
                )

    def fail_team(self, team_id: str) -> None:
        with self._lock:
            team = self._teams.get(team_id)
            if team:
                team.status = TaskStatus.FAILED
                team.completed_at = datetime.now()

    # ── 조회 ──────────────────────────────────
    def get_team_tasks(self, team_id: str) -> list[AgentTask]:
        """팀의 모든 태스크 반환."""
        with self._lock:
            team = self._teams.get(team_id)
            if not team:
                return []
            return [self._tasks[tid] for tid in team.task_ids if tid in self._tasks]

    def get_team_summary(self, team_id: str) -> dict[str, Any]:
        """팀 실행 요약."""
        tasks = self.get_team_tasks(team_id)
        with self._lock:
            team = self._teams.get(team_id)
        if not team:
            return {}

        completed = sum(1 for t in tasks if t.status == TaskStatus.COMPLETED)
        failed = sum(1 for t in tasks if t.status == TaskStatus.FAILED)

        return {
            "team_id": team.team_id,
            "name": team.name,
            "status": team.status.value,
            "total": len(tasks),
            "completed": completed,
            "failed": failed,
            "duration_sec": round(team.duration_sec, 1),
            "tasks": [
                {
                    "name": t.name,
                    "status": t.status.value,
                    "duration_sec": round(t.duration_sec, 1),
                    "error": t.error,
                }
                for t in tasks
            ],
        }

    def cleanup(self, max_age_sec: float = 3600.0) -> int:
        """오래된 완료/실패 태스크 정리 (메모리 누수 방지)."""
        now = datetime.now()
        to_remove: list[str] = []
        with self._lock:
            for tid, task in self._tasks.items():
                if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    if task.completed_at:
                        age = (now - task.completed_at).total_seconds()
                        if age > max_age_sec:
                            to_remove.append(tid)
            for tid in to_remove:
                del self._tasks[tid]
        return len(to_remove)


# 글로벌 싱글턴
_registry: TaskRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> TaskRegistry:
    """글로벌 TaskRegistry 반환."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = TaskRegistry()
    return _registry
