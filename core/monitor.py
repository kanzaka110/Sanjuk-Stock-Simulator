"""
시장 모니터 엔진 — 2-tier 실시간 감시 시스템

Tier 1 (무료): 5분 간격 yfinance 수치 체크 (VIX, RSI, 가격 변동)
Tier 2 (유료): 트리거 발동 시에만 Claude Haiku AI 분석
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from config.settings import (
    CLAUDE_API_KEY,
    KST,
    MONITOR_INTERVAL_SEC,
    PORTFOLIO,
    PRICE_CHANGE_THRESHOLD,
    RSI_HIGH_THRESHOLD,
    RSI_LOW_THRESHOLD,
    VIX_THRESHOLD,
)
from core.monitor_models import AlertResult, AlertTrigger, Severity, TriggerType

log = logging.getLogger(__name__)


class MarketMonitor:
    """2-tier 시장 감시 엔진."""

    def __init__(self) -> None:
        self._running: bool = False
        self._active_alerts: set[str] = set()  # 현재 발동 중인 알림 키
        self._last_scan: datetime | None = None

    def run(self) -> None:
        """메인 감시 루프."""
        from core.market_hours import is_any_market_open, next_market_open

        self._running = True
        log.info(f"시장 모니터 시작 (간격: {MONITOR_INTERVAL_SEC}초)")

        while self._running:
            try:
                now = datetime.now(KST)

                if not is_any_market_open(now):
                    next_open = next_market_open(now)
                    wait_sec = (next_open - now).total_seconds()
                    wait_sec = max(60, min(wait_sec, 3600))  # 1분~1시간 대기
                    log.info(f"장 마감 — {wait_sec:.0f}초 대기 (다음: {next_open.strftime('%H:%M')})")
                    self._sleep(wait_sec)
                    continue

                triggers = self._scan_all()
                self._last_scan = now

                # 현재 스캔에서 발동된 키 수집
                current_keys: set[str] = set()
                for trigger in triggers:
                    key = self._alert_key(trigger)
                    current_keys.add(key)

                    if key in self._active_alerts:
                        # 이미 알림 보낸 상태 → 스킵
                        continue
                    # 새로 발동 → 알림 전송
                    result = self._process_trigger(trigger)
                    self._send_alert(result)
                    self._active_alerts.add(key)

                # 조건 해소된 알림 제거 (다음에 다시 발동하면 재전송)
                self._active_alerts -= (self._active_alerts - current_keys)

                self._sleep(MONITOR_INTERVAL_SEC)

            except Exception as e:
                log.error(f"모니터 오류: {e}")
                self._sleep(60)

    def stop(self) -> None:
        """감시 종료."""
        self._running = False
        log.info("시장 모니터 종료")

    @property
    def last_scan(self) -> datetime | None:
        return self._last_scan

    @property
    def active_cooldowns(self) -> dict[str, datetime]:
        """하위 호환용 — 활성 알림 키 반환."""
        now = datetime.now(KST)
        return {k: now for k in self._active_alerts}

    # ─── Tier 1: 무료 수치 체크 ───────────────────────

    def _scan_all(self) -> list[AlertTrigger]:
        """전체 포트폴리오 + VIX 스캔."""
        triggers: list[AlertTrigger] = []
        now = datetime.now(KST)

        # VIX 체크
        vix_trigger = self._check_vix(now)
        if vix_trigger:
            triggers.append(vix_trigger)

        # 포트폴리오 종목 체크 (미국 종목만 — 장중 확인 의미 있는 것)
        for ticker, name in PORTFOLIO.items():
            rsi_trigger = self._check_rsi(ticker, name, now)
            if rsi_trigger:
                triggers.append(rsi_trigger)

            price_trigger = self._check_price_change(ticker, name, now)
            if price_trigger:
                triggers.append(price_trigger)

            time.sleep(0.1)  # yfinance rate limit 방지

        if triggers:
            log.info(f"트리거 {len(triggers)}건 감지")
        return triggers

    def _check_vix(self, now: datetime) -> AlertTrigger | None:
        """VIX 급등 체크."""
        from core.market import _get_quote_realtime

        quote = _get_quote_realtime("^VIX")
        if quote is None:
            return None

        if quote.price >= VIX_THRESHOLD:
            return AlertTrigger(
                ticker="^VIX",
                name="VIX",
                trigger_type=TriggerType.VIX_SPIKE,
                current_value=quote.price,
                threshold=VIX_THRESHOLD,
                timestamp=now,
            )
        return None

    def _check_rsi(
        self, ticker: str, name: str, now: datetime,
    ) -> AlertTrigger | None:
        """RSI 과매도/과매수 체크."""
        from core.indicators import calculate_indicators

        result = calculate_indicators(ticker, name)
        if result is None:
            return None

        if result.rsi <= RSI_LOW_THRESHOLD:
            return AlertTrigger(
                ticker=ticker,
                name=name,
                trigger_type=TriggerType.RSI_OVERSOLD,
                current_value=result.rsi,
                threshold=RSI_LOW_THRESHOLD,
                timestamp=now,
            )
        if result.rsi >= RSI_HIGH_THRESHOLD:
            return AlertTrigger(
                ticker=ticker,
                name=name,
                trigger_type=TriggerType.RSI_OVERBOUGHT,
                current_value=result.rsi,
                threshold=RSI_HIGH_THRESHOLD,
                timestamp=now,
            )
        return None

    def _check_price_change(
        self, ticker: str, name: str, now: datetime,
    ) -> AlertTrigger | None:
        """일중 급등/급락 체크."""
        from core.market import _get_quote_realtime

        quote = _get_quote_realtime(ticker)
        if quote is None or quote.pct == 0:
            return None

        if quote.pct <= -PRICE_CHANGE_THRESHOLD:
            return AlertTrigger(
                ticker=ticker,
                name=name,
                trigger_type=TriggerType.PRICE_DROP,
                current_value=quote.pct,
                threshold=PRICE_CHANGE_THRESHOLD,
                timestamp=now,
            )
        if quote.pct >= PRICE_CHANGE_THRESHOLD:
            return AlertTrigger(
                ticker=ticker,
                name=name,
                trigger_type=TriggerType.PRICE_SURGE,
                current_value=quote.pct,
                threshold=PRICE_CHANGE_THRESHOLD,
                timestamp=now,
            )
        return None

    # ─── 알림 상태 관리 ─────────────────────────────────

    def _alert_key(self, trigger: AlertTrigger) -> str:
        return f"{trigger.ticker}:{trigger.trigger_type.value}"

    # ─── Tier 2: AI 분석 (트리거 시에만) ───────────────

    def _process_trigger(self, trigger: AlertTrigger) -> AlertResult:
        """트리거에 대해 심각도 판정 + AI 분석."""
        severity = self._classify_severity(trigger)
        ai_analysis = ""

        # CRITICAL/WARNING 시에만 AI 호출 (비용 최적화)
        if severity in (Severity.CRITICAL, Severity.WARNING) and CLAUDE_API_KEY:
            ai_analysis = self._ai_analyze(trigger)

        return AlertResult(
            trigger=trigger,
            severity=severity,
            ai_analysis=ai_analysis,
        )

    def _classify_severity(self, trigger: AlertTrigger) -> Severity:
        """트리거 심각도 분류."""
        tt = trigger.trigger_type
        val = abs(trigger.current_value)

        if tt == TriggerType.VIX_SPIKE:
            return Severity.CRITICAL if val >= 35 else Severity.WARNING
        if tt in (TriggerType.PRICE_DROP, TriggerType.PRICE_SURGE):
            return Severity.CRITICAL if val >= 8 else Severity.WARNING
        if tt == TriggerType.RSI_OVERSOLD:
            return Severity.CRITICAL if val <= 20 else Severity.WARNING
        if tt == TriggerType.RSI_OVERBOUGHT:
            return Severity.WARNING

        return Severity.INFO

    def _ai_analyze(self, trigger: AlertTrigger) -> str:
        """Claude Haiku로 간단 AI 분석."""
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

            prompt = (
                f"당신은 주식 투자 어드바이저입니다.\n"
                f"종목: {trigger.name} ({trigger.ticker})\n"
                f"상황: {trigger.description}\n"
                f"시각: {trigger.timestamp.strftime('%Y-%m-%d %H:%M KST')}\n\n"
                f"이 상황에서 투자자가 취해야 할 행동을 3문장 이내로 간결하게 조언하세요.\n"
                f"매수/매도/관망 중 하나를 명확히 권고하고, 이유를 설명하세요."
            )

            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()
        except Exception as e:
            log.warning(f"AI 분석 실패: {e}")
            return ""

    # ─── 알림 전송 ────────────────────────────────────

    def _send_alert(self, result: AlertResult) -> None:
        """텔레그램 알림 전송."""
        from core.telegram import send_simple_message

        msg = _build_alert_message(result)
        sent = send_simple_message(msg)
        if sent:
            log.info(f"알림 전송: {result.trigger.ticker} {result.trigger.trigger_type.value}")

    def _sleep(self, seconds: float) -> None:
        """인터럽트 가능한 sleep."""
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(1.0, end - time.time()))


# ─── 알림 메시지 포매터 ───────────────────────────────

def _build_alert_message(result: AlertResult) -> str:
    """텔레그램 알림 메시지 생성."""
    trigger = result.trigger
    lines: list[str] = []

    lines.append("━" * 24)
    lines.append(f"{result.icon}  *긴급 시장 알림*")
    lines.append(f"_{trigger.timestamp.strftime('%Y.%m.%d %H:%M')}_")
    lines.append("━" * 24)
    lines.append("")

    # 트리거 정보
    type_icons = {
        TriggerType.VIX_SPIKE: "🔥",
        TriggerType.RSI_OVERSOLD: "📉",
        TriggerType.RSI_OVERBOUGHT: "📈",
        TriggerType.PRICE_DROP: "🔻",
        TriggerType.PRICE_SURGE: "🔺",
    }
    icon = type_icons.get(trigger.trigger_type, "📢")
    lines.append(f"{icon} *{trigger.name}* ({trigger.ticker})")
    lines.append(f"    {trigger.description}")
    lines.append("")

    # AI 분석
    if result.ai_analysis:
        lines.append("─" * 24)
        lines.append("🤖 *AI 분석*")
        lines.append("")
        lines.append(result.ai_analysis)
        lines.append("")

    # 안내
    lines.append("─" * 24)
    lines.append("💡 상세 분석이 필요하면:")
    lines.append("    *전체 브리핑* 을 입력하세요")
    lines.append("━" * 24)

    return "\n".join(lines)
