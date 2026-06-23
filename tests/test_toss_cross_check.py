"""
Toss/KIS 교차 검증 테스트

- 현금 부족/버퍼 침해 block
- 환율 stale warning
- 블랙리스트/MU 보호
- live_order_allowed=False
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.toss_cross_check import cross_check_candidate, cross_check_summary


def _ctx(**overrides) -> dict:
    base = {
        "enabled": True,
        "cash_krw": 10_000_000,
        "cash_usd": 5.67,
        "market_value_krw": 0,
        "total_account_value_krw": 10_000_000,
        "holdings_count": 0,
        "holdings": [],
        "usdkrw": 1539.0,
        "automation": {
            "enabled": False, "mode": "paper", "dry_run": True,
            "live_orders_allowed": False, "kill_switch": True,
        },
        "data_quality": {
            "toss_available": True, "cash_available": True,
            "fx_available": True, "calendar_available": True,
            "stale": False, "warnings": [],
        },
    }
    base.update(overrides)
    return base


class TestCashInsufficient:
    def test_enough_cash(self):
        r = cross_check_candidate("AAPL", "buy", 200_000, _ctx())
        assert "cash_insufficient" not in r["blocks"]

    def test_insufficient(self):
        r = cross_check_candidate("AAPL", "buy", 200_000, _ctx(cash_krw=100_000))
        assert "cash_insufficient" in r["blocks"]


class TestCashBuffer:
    def test_ok(self):
        r = cross_check_candidate("AAPL", "buy", 200_000, _ctx(cash_krw=5_000_000))
        assert "cash_buffer_breach" not in r["blocks"]

    def test_breach(self):
        r = cross_check_candidate("AAPL", "buy", 200_000, _ctx(cash_krw=2_100_000))
        assert "cash_buffer_breach" in r["blocks"]


class TestFxUnavailable:
    def test_fx_ok(self):
        r = cross_check_candidate("AAPL", "buy", 200_000, _ctx())
        assert "fx_unavailable" not in r["warnings"]

    def test_fx_unavailable(self):
        ctx = _ctx()
        ctx["data_quality"]["fx_available"] = False
        r = cross_check_candidate("AAPL", "buy", 200_000, ctx)
        assert "fx_unavailable" in r["warnings"]


class TestBlacklist:
    def test_blacklisted(self):
        r = cross_check_candidate("MU", "buy", 200_000, _ctx())
        assert "symbol_blacklisted" in r["blocks"]

    def test_not_blacklisted(self):
        r = cross_check_candidate("AAPL", "buy", 200_000, _ctx())
        assert "symbol_blacklisted" not in r["blocks"]


class TestMuProtected:
    def test_mu_blocked(self):
        r = cross_check_candidate("MU", "buy", 200_000, _ctx())
        assert "mu_protected" in r["blocks"]
        assert any("MU" in w for w in r["warnings"])


class TestAlreadyHeld:
    def test_duplicate(self):
        ctx = _ctx(holdings=[{"symbol": "AAPL"}])
        r = cross_check_candidate("AAPL", "buy", 200_000, ctx)
        assert "already_held_in_toss" in r["warnings"]


class TestMaxOrder:
    def test_within(self):
        r = cross_check_candidate("AAPL", "buy", 200_000, _ctx())
        assert "max_order_exceeded" not in r["blocks"]

    def test_exceeded(self):
        r = cross_check_candidate("AAPL", "buy", 500_000, _ctx())
        assert "max_order_exceeded" in r["blocks"]


class TestLiveAlwaysFalse:
    def test_live_false(self):
        r = cross_check_candidate("AAPL", "buy", 200_000, _ctx())
        assert r["live_order_allowed"] is False

    def test_readiness_blocked(self):
        r = cross_check_candidate("MU", "buy", 200_000, _ctx())
        assert r["toss_readiness"] == "blocked"

    def test_readiness_paper_only(self):
        r = cross_check_candidate("AAPL", "buy", 200_000, _ctx())
        assert r["toss_readiness"] == "paper_only"


class TestCrossCheckSummary:
    def test_all_ok(self):
        s = cross_check_summary(_ctx())
        assert s["all_ok"] is True
        assert s["live_order_allowed"] is False
        assert s["toss_readiness"] == "paper_only"

    def test_fx_missing(self):
        ctx = _ctx()
        ctx["data_quality"]["fx_available"] = False
        s = cross_check_summary(ctx)
        assert s["all_ok"] is False

    def test_unavailable(self):
        ctx = _ctx()
        ctx["data_quality"]["toss_available"] = False
        s = cross_check_summary(ctx)
        assert s["toss_readiness"] == "unavailable"
