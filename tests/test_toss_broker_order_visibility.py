import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import dashboard_data as dd


def test_live_pilot_events_include_broker_truth_when_event_ledger_empty(monkeypatch):
    monkeypatch.setattr(dd, "_cache", {}, raising=False)

    import core.toss_live_pilot_events as events
    import core.toss_live_pilot_policy as policy

    monkeypatch.setattr(events, "list_events", lambda limit=50: [])
    monkeypatch.setattr(events, "event_summary", lambda: {
        "summary": {},
        "live_sent_real": 0,
        "live_sent_mock_or_artifact": 0,
        "blocked_policy": 0,
        "blocked_transport": 0,
        "blocked_guard": 0,
        "live_order_sent_total": 0,
    })
    monkeypatch.setattr(policy, "compute_toss_live_pilot_policy", lambda: {
        "live_order_allowed": True,
        "adapter_status": "enabled",
        "live_transport_status": "configured",
    })
    monkeypatch.setattr(dd, "_recent_toss_broker_orders", lambda limit=20: {
        "ok": True,
        "orders": [{
            "symbol": "316140.KS",
            "symbol_name": "우리금융",
            "side": "BUY",
            "broker_order_status": "FILLED",
            "filled_quantity": 1.0,
            "filled_price": 28750.0,
            "created_at": "2026-06-30T11:14:11+09:00",
            "read_only_source": "toss_broker_orders_get",
            "symbol_label": "우리금융 (316140.KS)",
        }],
        "open_count": 0,
        "closed_count": 1,
        "source": "GET /api/v1/orders OPEN+CLOSED",
    })

    data = dd.toss_live_pilot_events_data(limit=10)

    assert data["records"] == []
    assert data["broker_order_count"] == 1
    assert data["broker_orders"][0]["symbol_label"] == "우리금융 (316140.KS)"
    assert data["warnings"]
