from datetime import datetime

from config.settings import KST
from core.monitor import (
    AI_ANALYSIS_DEFAULT_MODEL,
    AI_ANALYSIS_HIGH_STAKES_MODEL,
    AI_FAILURE_CIRCUIT_THRESHOLD,
    MarketMonitor,
)
from core.monitor_models import AlertTrigger, Severity, TriggerType


def _trigger(ticker="005930.KS", trigger_type=TriggerType.PRICE_DROP, current_value=-8.0):
    return AlertTrigger(
        ticker=ticker,
        name="삼성전자",
        trigger_type=trigger_type,
        current_value=current_value,
        threshold=7.0,
        timestamp=datetime.now(KST),
        market_session="KR_REGULAR",
    )


def test_monitor_ai_uses_low_cost_default_and_sonnet_for_high_stakes():
    assert AI_ANALYSIS_DEFAULT_MODEL == "haiku"
    assert AI_ANALYSIS_HIGH_STAKES_MODEL == "sonnet"

    monitor = MarketMonitor()
    assert monitor._analysis_model_for(_trigger(), Severity.WARNING) == "haiku"
    assert monitor._analysis_model_for(_trigger(current_value=-10.5), Severity.CRITICAL) == "sonnet"
    assert monitor._analysis_model_for(_trigger(trigger_type=TriggerType.STOP_LOSS_HIT), Severity.CRITICAL) == "sonnet"


def test_same_trigger_ai_analysis_runs_once_with_cooldown(monkeypatch):
    monitor = MarketMonitor()
    calls = []

    def fake_ai(trigger, model=None):
        calls.append((trigger.ticker, model))
        return "[관망] 단순 변동성, 추가 모니터링."

    monkeypatch.setattr(monitor, "_ai_analyze", fake_ai)

    first = monitor._process_trigger(_trigger())
    second = monitor._process_trigger(_trigger())

    assert first.ai_analysis.startswith("[관망]")
    assert second.ai_analysis == ""
    assert calls == [("005930.KS", "haiku")]


def test_ai_failure_circuit_breaker_blocks_new_calls():
    monitor = MarketMonitor()
    for _ in range(AI_FAILURE_CIRCUIT_THRESHOLD):
        monitor._record_ai_analysis_failure()

    assert monitor._should_run_ai_analysis(_trigger("000660.KS")) is False
