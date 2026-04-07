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

    @property
    def description(self) -> str:
        """트리거 설명 (한국어)."""
        descs = {
            TriggerType.VIX_SPIKE: f"공포지수(VIX) {self.current_value:.1f} 돌파 (임계: {self.threshold:.0f})",
            TriggerType.RSI_OVERSOLD: f"RSI {self.current_value:.1f} 과매도 진입 (임계: {self.threshold:.0f})",
            TriggerType.RSI_OVERBOUGHT: f"RSI {self.current_value:.1f} 과매수 경고 (임계: {self.threshold:.0f})",
            TriggerType.PRICE_DROP: f"일중 {self.current_value:+.1f}% 급락 (임계: {self.threshold:+.0f}%)",
            TriggerType.PRICE_SURGE: f"일중 {self.current_value:+.1f}% 급등 (임계: +{self.threshold:.0f}%)",
        }
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
