"""
모니터링 데이터 모델 — frozen dataclass로 불변성 보장

시장 감시 트리거, 알림 결과, 설정을 정의한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class TriggerType(str, Enum):
    """트리거 유형."""

    VIX_SPIKE = "VIX_SPIKE"
    RSI_OVERSOLD = "RSI_OVERSOLD"
    RSI_OVERBOUGHT = "RSI_OVERBOUGHT"
    PRICE_DROP = "PRICE_DROP"
    PRICE_SURGE = "PRICE_SURGE"
    TARGET_HIT = "TARGET_HIT"
    STOP_LOSS_HIT = "STOP_LOSS_HIT"
    INVALIDATION = "INVALIDATION"
    FX_CHANGE = "fx_change"


class Severity(str, Enum):
    """알림 심각도."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class AlertTrigger:
    """감시 트리거 발동 정보."""

    ticker: str
    name: str
    trigger_type: TriggerType
    current_value: float
    threshold: float
    timestamp: datetime
    market_session: str = ""  # KR_REGULAR/US_PREMARKET/US_REGULAR/US_AFTERMARKET/CLOSED

    @property
    def description(self) -> str:
        """트리거 설명 (한국어)."""
        descs = {
            TriggerType.VIX_SPIKE: f"공포지수(VIX) {self.current_value:.1f} 돌파 (임계: {self.threshold:.0f})",
            TriggerType.RSI_OVERSOLD: f"RSI {self.current_value:.1f} 과매도 진입 (임계: {self.threshold:.0f})",
            TriggerType.RSI_OVERBOUGHT: f"RSI {self.current_value:.1f} 과매수 경고 (임계: {self.threshold:.0f})",
            TriggerType.PRICE_DROP: f"일중 {self.current_value:+.1f}% 급락 (임계: {self.threshold:+.0f}%)",
            TriggerType.PRICE_SURGE: f"일중 {self.current_value:+.1f}% 급등 (임계: +{self.threshold:.0f}%)",
            TriggerType.TARGET_HIT: f"목표가 도달 — 현재가 {self.current_value:,.0f} ≥ 목표 {self.threshold:,.0f} (익절 검토)",
            TriggerType.STOP_LOSS_HIT: f"손절가 이탈 — 현재가 {self.current_value:,.0f} ≤ 손절 {self.threshold:,.0f} (손절 검토)",
            TriggerType.INVALIDATION: f"무효화 조건 도달 — 현재가 {self.current_value:,.0f} ≤ 손절선 {self.threshold:,.0f} (예약 주문 취소·셋업 재평가)",
        }
        if self.trigger_type == TriggerType.FX_CHANGE:
            direction = "원화 약세(달러 강세)" if self.current_value > 0 else "원화 강세(달러 약세)"
            return f"환율 {self.current_value:+.2f}% 변동 ({direction})"
        return descs.get(self.trigger_type, str(self.trigger_type))


@dataclass(frozen=True)
class AlertResult:
    """알림 결과 (트리거 + AI 분석)."""

    trigger: AlertTrigger
    severity: Severity
    ai_analysis: str = ""

    @property
    def icon(self) -> str:
        icons = {
            Severity.CRITICAL: "🚨",
            Severity.WARNING: "⚠️",
            Severity.INFO: "ℹ️",
        }
        return icons.get(self.severity, "📢")
