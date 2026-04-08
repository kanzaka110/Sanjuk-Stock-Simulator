"""
복구 패턴 — claw-code 패턴 적용

리트라이 데코레이터, 타임아웃 래퍼, 서킷 브레이커.
market.py 등 외부 API 호출에 적용하여 프로덕션 안정성 확보.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, TypeVar

from core.errors import ErrorDomain, ErrorSeverity, classify_exception

log = logging.getLogger(__name__)

F = TypeVar("F")


# ═══════════════════════════════════════════════════════
# 리트라이 데코레이터 (지수 백오프)
# ═══════════════════════════════════════════════════════
def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
    on_retry: Any = None,
) -> Any:
    """지수 백오프 리트라이 데코레이터.

    Args:
        max_attempts: 최대 시도 횟수 (1이면 리트라이 없음)
        base_delay: 첫 번째 재시도 대기 시간 (초)
        max_delay: 최대 대기 시간 (초)
        backoff_factor: 대기 시간 배수
        exceptions: 리트라이할 예외 타입
        on_retry: 재시도 시 호출할 콜백 (attempt, exception)
    """
    def decorator(func: Any) -> Any:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt == max_attempts:
                        break
                    delay = min(base_delay * (backoff_factor ** (attempt - 1)), max_delay)
                    ctx = classify_exception(e, operation=func.__name__)
                    log.warning(
                        "[%s] %s 재시도 %d/%d (%.1fs 후) — %s",
                        ctx.domain.value, func.__name__,
                        attempt, max_attempts, delay, e,
                    )
                    if on_retry:
                        on_retry(attempt, e)
                    time.sleep(delay)
            raise last_exc  # type: ignore[misc]
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════
# 서킷 브레이커
# ═══════════════════════════════════════════════════════
@dataclass
class CircuitBreaker:
    """서킷 브레이커 — 연속 실패 시 호출 차단.

    States:
        CLOSED  → 정상 동작, 실패 카운트 누적
        OPEN    → 차단 상태, 즉시 실패 반환
        HALF_OPEN → 테스트 호출 1회 허용
    """

    name: str
    failure_threshold: int = 5
    recovery_timeout: float = 60.0  # 초
    _failure_count: int = field(default=0, init=False, repr=False)
    _last_failure_time: datetime | None = field(default=None, init=False, repr=False)
    _state: str = field(default="CLOSED", init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == "OPEN" and self._last_failure_time:
                elapsed = (datetime.now() - self._last_failure_time).total_seconds()
                if elapsed >= self.recovery_timeout:
                    self._state = "HALF_OPEN"
            return self._state

    def record_success(self) -> None:
        """성공 기록 — CLOSED로 복귀."""
        with self._lock:
            self._failure_count = 0
            self._state = "CLOSED"

    def record_failure(self) -> None:
        """실패 기록 — 임계치 초과 시 OPEN."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = datetime.now()
            if self._failure_count >= self.failure_threshold:
                self._state = "OPEN"
                log.warning(
                    "서킷 브레이커 [%s] OPEN — %d회 연속 실패",
                    self.name, self._failure_count,
                )

    @property
    def is_available(self) -> bool:
        """호출 가능 여부."""
        return self.state != "OPEN"

    def __call__(self, func: Any) -> Any:
        """데코레이터로 사용."""
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not self.is_available:
                log.info(
                    "서킷 브레이커 [%s] 차단 중 — %s 스킵",
                    self.name, func.__name__,
                )
                return None
            try:
                result = func(*args, **kwargs)
                self.record_success()
                return result
            except Exception as e:
                self.record_failure()
                raise
        return wrapper


# ═══════════════════════════════════════════════════════
# 폴백 체인
# ═══════════════════════════════════════════════════════
@dataclass(frozen=True)
class FallbackResult:
    """폴백 체인 실행 결과."""

    value: Any
    source: str         # 성공한 소스 이름
    attempts: int       # 총 시도 횟수
    errors: tuple[tuple[str, str], ...] = ()  # (source, error_msg) 튜플


def fallback_chain(
    steps: list[tuple[str, Any]],
    ticker: str = "",
) -> FallbackResult:
    """순차 폴백 체인 실행.

    Args:
        steps: [(소스명, callable)] 리스트. 각 callable은 인자 없이 호출.
        ticker: 로깅용 티커

    Returns:
        FallbackResult (value=None이면 전체 실패)
    """
    errors: list[tuple[str, str]] = []
    for idx, (source, fn) in enumerate(steps, 1):
        try:
            result = fn()
            if result is not None:
                if idx > 1:
                    log.info(
                        "[%s] %s 폴백 성공 (시도 %d/%d)",
                        ticker or "?", source, idx, len(steps),
                    )
                return FallbackResult(
                    value=result,
                    source=source,
                    attempts=idx,
                    errors=tuple(errors),
                )
        except Exception as e:
            errors.append((source, str(e)))
            ctx = classify_exception(e, operation="fallback", source=source, ticker=ticker)
            log.debug(
                "[%s] %s 실패 (%s) — 다음 폴백 시도",
                ticker or "?", source, e,
            )

    if errors:
        log.warning(
            "[%s] 전체 폴백 실패 (%d단계): %s",
            ticker or "?", len(steps),
            "; ".join(f"{s}: {e}" for s, e in errors),
        )

    return FallbackResult(
        value=None,
        source="none",
        attempts=len(steps),
        errors=tuple(errors),
    )


# ═══════════════════════════════════════════════════════
# 사전 정의 서킷 브레이커 인스턴스
# ═══════════════════════════════════════════════════════
kis_breaker = CircuitBreaker(name="KIS_API", failure_threshold=3, recovery_timeout=120.0)
yfinance_breaker = CircuitBreaker(name="yfinance", failure_threshold=5, recovery_timeout=60.0)
claude_breaker = CircuitBreaker(name="Claude_API", failure_threshold=3, recovery_timeout=180.0)
gemini_breaker = CircuitBreaker(name="Gemini_API", failure_threshold=3, recovery_timeout=180.0)
