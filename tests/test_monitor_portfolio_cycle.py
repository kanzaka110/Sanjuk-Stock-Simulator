from __future__ import annotations

import sys
from datetime import datetime
from types import SimpleNamespace

from config.settings import KST
from core.monitor import MarketMonitor


def test_auxiliary_tasks_review_positions_before_new_buys(monkeypatch):
    calls = []

    monkeypatch.setitem(sys.modules, "core.toss_order_watch", SimpleNamespace(
        run_toss_order_watch=lambda now=None: calls.append("order_watch") or {"ok": True}
    ))
    monkeypatch.setitem(sys.modules, "core.toss_position_review", SimpleNamespace(
        run_toss_position_review=lambda now=None: calls.append("position_review") or {"candidate_count": 0}
    ))
    monkeypatch.setitem(sys.modules, "core.toss_autonomous_pipeline", SimpleNamespace(
        run_toss_autonomous_pipeline=lambda now=None: calls.append("buy_pipeline") or {"attempted": 0},
        send_daily_pipeline_report=lambda now=None: calls.append("daily_report") or {"sent": False},
    ))
    monkeypatch.setitem(sys.modules, "core.dart_monitor", SimpleNamespace(
        run_dart_monitor=lambda now=None: calls.append("dart") or {"hit_count": 0}
    ))
    monkeypatch.setitem(sys.modules, "core.edgar_monitor", SimpleNamespace(
        run_edgar_monitor=lambda now=None: calls.append("edgar") or {"hit_count": 0}
    ))
    monkeypatch.setitem(sys.modules, "core.earnings_alert", SimpleNamespace(
        run_earnings_alert=lambda now=None: calls.append("earnings") or {"hit_count": 0}
    ))

    MarketMonitor()._run_auxiliary_tasks(datetime(2026, 7, 8, 11, 0, tzinfo=KST))

    assert calls.index("position_review") < calls.index("buy_pipeline")


def test_auxiliary_tasks_skip_new_buys_when_position_risk_exists(monkeypatch):
    calls = []

    monkeypatch.setitem(sys.modules, "core.toss_order_watch", SimpleNamespace(
        run_toss_order_watch=lambda now=None: calls.append("order_watch") or {"ok": True}
    ))
    monkeypatch.setitem(sys.modules, "core.toss_position_review", SimpleNamespace(
        run_toss_position_review=lambda now=None: calls.append("position_review") or {"candidate_count": 2, "candidates": [{"symbol": "HPSP"}]}
    ))
    monkeypatch.setitem(sys.modules, "core.toss_autonomous_pipeline", SimpleNamespace(
        run_toss_autonomous_pipeline=lambda now=None: calls.append("buy_pipeline") or {"attempted": 1},
        retry_retryable_orders=lambda now=None: calls.append("retry_sweep") or {"retried": 0},
        send_daily_pipeline_report=lambda now=None: calls.append("daily_report") or {"sent": False},
    ))
    monkeypatch.setitem(sys.modules, "core.dart_monitor", SimpleNamespace(
        run_dart_monitor=lambda now=None: calls.append("dart") or {"hit_count": 0}
    ))
    monkeypatch.setitem(sys.modules, "core.edgar_monitor", SimpleNamespace(
        run_edgar_monitor=lambda now=None: calls.append("edgar") or {"hit_count": 0}
    ))
    monkeypatch.setitem(sys.modules, "core.earnings_alert", SimpleNamespace(
        run_earnings_alert=lambda now=None: calls.append("earnings") or {"hit_count": 0}
    ))

    MarketMonitor()._run_auxiliary_tasks(datetime(2026, 7, 8, 11, 0, tzinfo=KST))

    assert "position_review" in calls
    assert "retry_sweep" in calls
    assert "buy_pipeline" not in calls
