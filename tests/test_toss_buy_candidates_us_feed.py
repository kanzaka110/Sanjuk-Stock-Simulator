
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_toss_buy_candidates_data_can_request_us_universe():
    import core.dashboard_data as dd

    captured = {}

    def fake_fallback(markets):
        captured["markets"] = list(markets)
        return [{
            "ticker": "NVDA", "name": "엔비디아", "market": "US",
            "price": 190.0, "change_pct": 0.1, "ret_20d": 0.0,
            "ret_60d": 0.0, "rsi": 50.0, "vol_surge": 1.0,
            "pct_from_52w_high": 0.0, "volume_value": 5_000_000_000,
            "source": "test", "tags": ("AI",), "has_catalyst": False,
        }]

    def fake_sections(scan_candidates, briefing_type):
        captured["briefing_type"] = briefing_type
        return {"scan_candidates": scan_candidates, "briefing_type": briefing_type}

    def fake_toss_eligible(sections, max_order_krw):
        item = sections["scan_candidates"][0]
        return {
            "items": [{
                "symbol": item["ticker"], "name": item["name"], "side": "buy",
                "market": item["market"], "price": item["price"],
                "current_price": item["price"], "limit_price": item["price"],
                "stop_loss": 178.6, "target_price": 210.0,
                "risk_reward": 2.0, "score": 80,
                "decision_bucket": "PASS_EXECUTE",
                "quantity": 1,
            }],
            "excluded": [], "count": 1, "excluded_count": 0,
            "scan_summary": {}, "note": "test",
        }

    with patch("core.discovery_candidates._fallback_universe_candidates", side_effect=fake_fallback), \
         patch("core.discovery_candidates.build_discovery_sections", side_effect=fake_sections), \
         patch("core.discovery_candidates.toss_eligible_new_candidates", side_effect=fake_toss_eligible), \
         patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy", return_value={"max_order_krw": 500_000}), \
         patch("core.dashboard_data._cross_check_price_quality", return_value={"quality": "high"}), \
         patch("core.dashboard_data._cached", side_effect=lambda _key, _ttl, fn: fn()):
        data = dd.toss_buy_candidates_data(market="US", limit=5)

    assert captured["markets"] == ["US"]
    assert captured["briefing_type"] == "US_BEFORE"
    assert data["items"][0]["symbol"] == "NVDA"
    assert data["items"][0]["market"] == "US"
    assert data["scan_summary"]["markets"] == ["US"]


def test_toss_buy_candidates_data_allows_combined_kr_us_universe():
    import core.dashboard_data as dd
    captured = {}

    with patch("core.discovery_candidates._fallback_universe_candidates", side_effect=lambda markets: captured.setdefault("markets", list(markets)) or []), \
         patch("core.discovery_candidates.build_discovery_sections", return_value={}), \
         patch("core.discovery_candidates.toss_eligible_new_candidates", return_value={"items": [], "excluded": [], "count": 0, "excluded_count": 0, "scan_summary": {}, "note": "test"}), \
         patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy", return_value={"max_order_krw": 500_000}), \
         patch("core.dashboard_data._cached", side_effect=lambda _key, _ttl, fn: fn()):
        dd.toss_buy_candidates_data(market="ALL", limit=5)

    assert captured["markets"] == ["KR", "US"]



def test_us_fallback_candidate_is_not_rejected_only_because_chart_metrics_are_missing():
    from core.discovery_candidates import build_new_discovery

    passed, rejected = build_new_discovery([
        {
            "ticker": "NVDA", "name": "엔비디아", "market": "US",
            "price": 190.0, "change_pct": 0.2,
            "ret_20d": 0.0, "ret_60d": 0.0,
            "rsi": 50.0, "vol_surge": 1.0,
            "pct_from_52w_high": 0.0,
            "volume_value": 5_000_000_000,
            "source": "유니버스(fallback)",
            "tags": ("AI",), "has_catalyst": False,
        }
    ], held=set(), watchlist=set(), ria=set(), recent_reco=set())

    assert [c.ticker for c in passed] == ["NVDA"]
    assert not [r for r in rejected if r.ticker == "NVDA"]
    assert passed[0].risk_reward >= 1.2



def test_us_candidate_sizing_uses_usd_price_not_krw_budget_divided_by_usd_price():
    import core.dashboard_data as dd

    def fake_fallback(markets):
        return [{
            "ticker": "NVDA", "name": "엔비디아", "market": "US",
            "price": 190.0, "change_pct": 0.1, "ret_20d": 0.0,
            "ret_60d": 0.0, "rsi": 50.0, "vol_surge": 1.0,
            "pct_from_52w_high": 0.0, "volume_value": 5_000_000_000,
            "source": "test", "tags": ("AI",), "has_catalyst": False,
        }]

    def fake_sections(scan_candidates, briefing_type):
        return {"scan_candidates": scan_candidates, "briefing_type": briefing_type}

    def fake_toss_eligible(sections, max_order_krw):
        return {
            "items": [{
                "symbol": "NVDA", "name": "엔비디아", "side": "buy",
                "market": "US", "asset_type": "US_STOCK", "currency": "USD",
                "price": 190.0, "current_price": 190.0, "limit_price": 190.0,
                "estimated_amount_usd": 190.0, "estimated_amount_krw": 285_000.0,
                "fx_usdkrw": 1500.0,
                "stop_loss": 178.6, "target_price": 210.0,
                "risk_reward": 1.67, "score": 80,
                "decision_bucket": "PASS_EXECUTE",
                "quantity": 1,
            }],
            "excluded": [], "count": 1, "excluded_count": 0,
            "scan_summary": {}, "note": "test",
        }

    with patch("core.discovery_candidates._fallback_universe_candidates", side_effect=fake_fallback),          patch("core.discovery_candidates.build_discovery_sections", side_effect=fake_sections),          patch("core.discovery_candidates.toss_eligible_new_candidates", side_effect=fake_toss_eligible),          patch("core.toss_live_pilot_policy.compute_toss_live_pilot_policy", return_value={"max_order_krw": 500_000}),          patch("core.dashboard_data._cross_check_price_quality", return_value={"quality": "high"}),          patch("core.dashboard_data._cached", side_effect=lambda _key, _ttl, fn: fn()):
        data = dd.toss_buy_candidates_data(market="US", limit=5)

    item = data["items"][0]
    assert item["quantity"] == 1
    assert item["estimated_amount_usd"] == 190.0
    assert item["estimated_amount_krw"] == 285_000.0
    assert item["quantity_source"] in {"provided_usd", "provided"}
