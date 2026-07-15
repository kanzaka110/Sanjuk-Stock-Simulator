"""공급자별 시장 데이터 조회 결과의 공통 typed 계약."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Generic, TypeVar


class FetchStatus(str, Enum):
    """외부 데이터 조회의 안전한 최종 상태."""

    SUCCESS = "success"
    EMPTY = "empty"
    SKIPPED = "skipped"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


class FetchErrorType(str, Enum):
    """원문 오류나 자격 증명을 포함하지 않는 안정적인 실패 분류."""

    NONE = "none"
    NOT_CONFIGURED = "not_configured"
    AUTH = "auth"
    NETWORK = "network"
    HTTP = "http"
    PROVIDER = "provider"
    MALFORMED = "malformed"
    NUMERIC = "numeric"
    ZERO_DEPTH = "zero_depth"
    CACHE_TIMESTAMP_MISSING = "cache_timestamp_missing"


class CacheSource(str, Enum):
    """결과 값이 실제로 유래한 계층."""

    NONE = "none"
    MEMORY = "memory"
    FILE = "file"
    NETWORK = "network"


T = TypeVar("T")


@dataclass(frozen=True)
class FetchResult(Generic[T]):
    """비밀·공급자 원문 오류를 담지 않는 공통 조회 결과."""

    status: FetchStatus
    provider: str
    endpoint: str
    tr_id: str | None
    venue: str
    symbol: str
    started_at_utc: datetime
    completed_at_utc: datetime
    error_type: FetchErrorType
    cache_source: CacheSource
    fallback_used: bool
    value: T | None
    source_fetched_at_utc: datetime | None = None

    def __post_init__(self) -> None:
        try:
            status = FetchStatus(self.status)
            error_type = FetchErrorType(self.error_type)
            cache_source = CacheSource(self.cache_source)
        except ValueError as exc:
            raise ValueError("invalid fetch result enum") from exc
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "error_type", error_type)
        object.__setattr__(self, "cache_source", cache_source)

        value_required = status in {
            FetchStatus.SUCCESS,
            FetchStatus.EMPTY,
            FetchStatus.INCOMPLETE,
        }
        clean_result = status in {FetchStatus.SUCCESS, FetchStatus.EMPTY}
        status_lineage_valid = (
            (
                clean_result
                and error_type is FetchErrorType.NONE
                and cache_source is not CacheSource.NONE
            )
            or (
                status is FetchStatus.SKIPPED
                and error_type is FetchErrorType.NOT_CONFIGURED
                and cache_source is CacheSource.NONE
            )
            or (
                status is FetchStatus.FAILED
                and error_type
                in {
                    FetchErrorType.AUTH,
                    FetchErrorType.NETWORK,
                    FetchErrorType.HTTP,
                    FetchErrorType.PROVIDER,
                    FetchErrorType.MALFORMED,
                    FetchErrorType.NUMERIC,
                }
                and cache_source in {CacheSource.NONE, CacheSource.NETWORK}
            )
            or (
                status is FetchStatus.INCOMPLETE
                and error_type is FetchErrorType.CACHE_TIMESTAMP_MISSING
                and cache_source in {CacheSource.MEMORY, CacheSource.FILE}
            )
            or (
                status is FetchStatus.INCOMPLETE
                and error_type is FetchErrorType.ZERO_DEPTH
                and cache_source is CacheSource.NETWORK
            )
        )
        invalid_state = (
            (value_required and self.value is None)
            or (not value_required and self.value is not None)
            or not status_lineage_valid
        )
        if invalid_state:
            raise ValueError("invalid fetch result state")

        if type(self.fallback_used) is not bool:
            raise TypeError("fallback_used must be bool")

        started = _as_aware_utc(self.started_at_utc)
        completed = _as_aware_utc(self.completed_at_utc)
        if completed < started:
            raise ValueError("completed_at_utc precedes started_at_utc")
        object.__setattr__(self, "started_at_utc", started)
        object.__setattr__(self, "completed_at_utc", completed)

        source_fetched_at = self.source_fetched_at_utc
        if source_fetched_at is not None and (
            not value_required
            or error_type is FetchErrorType.CACHE_TIMESTAMP_MISSING
        ):
            raise ValueError("invalid fetch result state")
        if (
            source_fetched_at is None
            and value_required
            and cache_source is CacheSource.NETWORK
        ):
            source_fetched_at = completed
        if (
            source_fetched_at is None
            and value_required
            and cache_source in {CacheSource.MEMORY, CacheSource.FILE}
            and error_type is not FetchErrorType.CACHE_TIMESTAMP_MISSING
        ):
            raise ValueError("invalid fetch result state")
        if source_fetched_at is not None:
            source_fetched_at = _as_aware_utc(source_fetched_at)
            if source_fetched_at > completed:
                raise ValueError("invalid fetch result state")
        object.__setattr__(self, "source_fetched_at_utc", source_fetched_at)


def _as_aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("fetch timestamps must be timezone-aware")
    if value.utcoffset() is None:
        raise ValueError("fetch timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)
