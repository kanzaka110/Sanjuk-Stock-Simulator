"""
실패 분류 체계 — claw-code 패턴 적용

에러를 도메인별로 분류하여 복구 전략을 결정한다.
- MARKET: 시세 조회 실패 (yfinance, KIS API)
- BROKER: 브로커 연동 실패 (KIS 토큰, 주문)
- ANALYSIS: AI 분석 실패 (Claude, Gemini API)
- INFRA: 인프라 실패 (네트워크, DB, Notion, Telegram)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ErrorDomain(str, Enum):
    """에러 도메인 분류."""

    MARKET = "MARKET"
    BROKER = "BROKER"
    ANALYSIS = "ANALYSIS"
    INFRA = "INFRA"


class ErrorSeverity(str, Enum):
    """에러 심각도."""

    LOW = "LOW"          # 로깅만, 폴백 가능
    MEDIUM = "MEDIUM"    # 폴백 시도, 경고
    HIGH = "HIGH"        # 즉시 알림, 파이프라인 영향
    CRITICAL = "CRITICAL"  # 파이프라인 중단 가능


@dataclass(frozen=True)
class ErrorContext:
    """에러 발생 컨텍스트."""

    domain: ErrorDomain
    severity: ErrorSeverity
    operation: str          # 실패한 동작 (e.g., "fetch_price", "kis_token")
    source: str             # 실패 소스 (e.g., "yfinance", "KIS API")
    ticker: str = ""        # 관련 종목
    message: str = ""       # 사람이 읽을 수 있는 설명
    retry_count: int = 0    # 재시도 횟수
    recoverable: bool = True
    timestamp: datetime = field(default_factory=datetime.now)


class StockSimulatorError(Exception):
    """프로젝트 기본 예외 — 모든 커스텀 예외의 부모."""

    def __init__(self, message: str, context: ErrorContext) -> None:
        super().__init__(message)
        self.context = context


class MarketDataError(StockSimulatorError):
    """시세 데이터 수집 실패."""

    def __init__(self, message: str, *, source: str, ticker: str = "",
                 severity: ErrorSeverity = ErrorSeverity.MEDIUM) -> None:
        ctx = ErrorContext(
            domain=ErrorDomain.MARKET,
            severity=severity,
            operation="fetch_price",
            source=source,
            ticker=ticker,
            message=message,
        )
        super().__init__(message, ctx)


class BrokerError(StockSimulatorError):
    """브로커 API 실패 (KIS 토큰, 주문 등)."""

    def __init__(self, message: str, *, operation: str = "api_call",
                 severity: ErrorSeverity = ErrorSeverity.HIGH) -> None:
        ctx = ErrorContext(
            domain=ErrorDomain.BROKER,
            severity=severity,
            operation=operation,
            source="KIS",
            message=message,
        )
        super().__init__(message, ctx)


class AnalysisError(StockSimulatorError):
    """AI 분석 실패 (Claude, Gemini)."""

    def __init__(self, message: str, *, source: str = "claude",
                 operation: str = "analyze",
                 severity: ErrorSeverity = ErrorSeverity.MEDIUM) -> None:
        ctx = ErrorContext(
            domain=ErrorDomain.ANALYSIS,
            severity=severity,
            operation=operation,
            source=source,
            message=message,
        )
        super().__init__(message, ctx)


class InfraError(StockSimulatorError):
    """인프라 실패 (네트워크, DB, Notion, Telegram)."""

    def __init__(self, message: str, *, source: str,
                 operation: str = "connect",
                 severity: ErrorSeverity = ErrorSeverity.MEDIUM,
                 recoverable: bool = True) -> None:
        ctx = ErrorContext(
            domain=ErrorDomain.INFRA,
            severity=severity,
            operation=operation,
            source=source,
            message=message,
            recoverable=recoverable,
        )
        super().__init__(message, ctx)


def classify_exception(exc: Exception, *, operation: str = "",
                       source: str = "", ticker: str = "") -> ErrorContext:
    """일반 예외를 ErrorContext로 분류.

    이미 StockSimulatorError인 경우 기존 context 반환.
    그 외에는 메시지 패턴으로 도메인 추론.
    """
    if isinstance(exc, StockSimulatorError):
        return exc.context

    msg = str(exc).lower()

    # 패턴 기반 도메인 추론
    if any(kw in msg for kw in ("timeout", "connection", "ssl", "dns")):
        domain = ErrorDomain.INFRA
        severity = ErrorSeverity.MEDIUM
    elif any(kw in msg for kw in ("token", "auth", "appkey", "접근토큰")):
        domain = ErrorDomain.BROKER
        severity = ErrorSeverity.HIGH
    elif any(kw in msg for kw in ("rate limit", "429", "quota", "api_key")):
        domain = ErrorDomain.ANALYSIS
        severity = ErrorSeverity.HIGH
    elif any(kw in msg for kw in ("price", "ticker", "yfinance", "download")):
        domain = ErrorDomain.MARKET
        severity = ErrorSeverity.MEDIUM
    else:
        domain = ErrorDomain.INFRA
        severity = ErrorSeverity.LOW

    return ErrorContext(
        domain=domain,
        severity=severity,
        operation=operation or type(exc).__name__,
        source=source or "unknown",
        ticker=ticker,
        message=str(exc),
    )
