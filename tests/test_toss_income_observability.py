"""Toss 수입/시장 read-only 관측 계약 회귀 테스트."""

from datetime import datetime, timezone

from core import dashboard_data as dd


_MARKET_NOW = datetime(2026, 7, 16, 1, 30, tzinfo=timezone.utc)


def _trusted_index(pct, *, source="kis", age_sec=0):
    return {
        "pct": pct,
        "source": source,
        "as_of": _MARKET_NOW.timestamp() - age_sec,
    }


def test_local_index_crash_overrides_low_vix_normal_mode():
    context = dd._market_risk_context(
        {
            "KOSPI": _trusted_index(-6.46),
            "KOSDAQ": _trusted_index(-4.09),
        },
        vix_price=15.67,
        now_utc=_MARKET_NOW,
    )

    assert context["mode"] == "위험"
    assert context["local_market_shock"] is True
    assert context["trigger_index"] == "KOSPI"
    assert context["trigger_pct"] == -6.46


def test_healthy_local_indices_keep_low_vix_market_normal():
    context = dd._market_risk_context(
        {
            "KOSPI": _trusted_index(0.4),
            "KOSDAQ": _trusted_index(-0.8),
        },
        vix_price=18.0,
        now_utc=_MARKET_NOW,
    )

    assert context["mode"] == "정상"
    assert context["local_market_shock"] is False
    assert context["local_indices_complete"] is True
    assert context["missing_indices"] == []


def test_missing_local_index_never_reports_normal_market():
    context = dd._market_risk_context(
        {"KOSPI": _trusted_index(0.4)},
        vix_price=18.0,
        now_utc=_MARKET_NOW,
    )

    assert context["mode"] == "주의"
    assert context["local_market_shock"] is False
    assert context["local_indices_complete"] is False
    assert context["missing_indices"] == ["KOSDAQ"]


def test_nonfinite_local_index_pct_is_treated_as_missing():
    context = dd._market_risk_context(
        {
            "KOSPI": _trusted_index(float("nan")),
            "KOSDAQ": _trusted_index(0.2),
        },
        vix_price=18.0,
        now_utc=_MARKET_NOW,
    )

    assert context["mode"] == "주의"
    assert context["local_indices_complete"] is False
    assert context["missing_indices"] == ["KOSPI"]


def test_missing_market_provenance_never_reports_normal():
    context = dd._market_risk_context(
        {"KOSPI": {"pct": 0.4}, "KOSDAQ": {"pct": 0.2}},
        vix_price=18.0,
        now_utc=_MARKET_NOW,
    )

    assert context["mode"] == "주의"
    assert context["local_indices_trusted"] is False
    assert context["untrusted_indices"] == ["KOSPI", "KOSDAQ"]


def test_daily_fallback_market_provenance_never_reports_normal():
    context = dd._market_risk_context(
        {
            "KOSPI": _trusted_index(0.4, source="yf_daily"),
            "KOSDAQ": _trusted_index(0.2, source="yf_daily"),
        },
        vix_price=18.0,
        now_utc=_MARKET_NOW,
    )

    assert context["mode"] == "주의"
    assert context["local_indices_trusted"] is False
    assert context["untrusted_indices"] == ["KOSPI", "KOSDAQ"]


def test_stale_or_future_market_provenance_never_reports_normal():
    stale = dd._market_risk_context(
        {
            "KOSPI": _trusted_index(0.4, age_sec=181),
            "KOSDAQ": _trusted_index(0.2),
        },
        vix_price=18.0,
        now_utc=_MARKET_NOW,
    )
    future = dd._market_risk_context(
        {
            "KOSPI": _trusted_index(0.4, age_sec=-31),
            "KOSDAQ": _trusted_index(0.2),
        },
        vix_price=18.0,
        now_utc=_MARKET_NOW,
    )

    assert stale["mode"] == "주의"
    assert stale["stale_indices"] == ["KOSPI"]
    assert future["mode"] == "주의"
    assert future["stale_indices"] == ["KOSPI"]


def test_fetch_market_raw_accepts_legacy_quote_without_new_attributes(monkeypatch):
    from core import market

    class LegacyQuote:
        price = 100.0
        change = 0.0
        pct = 0.0
        high = 100.0
        low = 100.0

    monkeypatch.setattr(
        market,
        "_batch_quotes",
        lambda ticker_map: {ticker: LegacyQuote() for ticker in ticker_map},
    )

    result = dd._fetch_market_raw()

    assert result["indices"]
    assert all(row["source"] == "" for row in result["indices"].values())
    assert all(row["as_of"] is None for row in result["indices"].values())


def test_fetch_market_raw_preserves_missing_pct_as_incomplete(monkeypatch):
    from core import market

    class MissingPctQuote:
        price = 100.0
        change = 0.0
        pct = None
        high = 100.0
        low = 100.0
        source = "yf_fast"
        as_of = 1.0

    monkeypatch.setattr(
        market,
        "_batch_quotes",
        lambda ticker_map: {ticker: MissingPctQuote() for ticker in ticker_map},
    )

    result = dd._fetch_market_raw()

    assert all(row["pct"] is None for row in result["indices"].values())
    assert result["mode"] != "정상"
    assert result["market_risk"]["local_indices_complete"] is False
    assert result["market_risk"]["missing_indices"] == ["KOSPI", "KOSDAQ"]


def test_toss_pnl_scope_metadata_explicitly_excludes_realized_sales():
    scope = dd._toss_pnl_scope_metadata()

    assert scope["profit_loss"] == "open_positions_unrealized_after_cost"
    assert scope["today_profit_loss"] == "open_positions_daily_change_excludes_closed_realized"
    assert scope["realized_profit_loss"] == "unavailable"
    assert scope["true_daily_account_pnl_available"] is False
    assert "매도" in scope["warning"]


def test_unavailable_account_summary_keeps_pnl_scope_contract():
    summary = dd._toss_account_summary_unavailable("stock_bot_snapshot_unavailable")

    assert summary["pnl_scope"]["realized_profit_loss"] == "unavailable"
    assert summary["realized_profit_loss"]["krw"] is None
    assert "매도" in " ".join(summary["warnings"])


def test_unconfigured_account_summary_keeps_pnl_scope_contract(monkeypatch):
    from core import toss_client as tc

    monkeypatch.setattr(dd, "_dashboard_toss_broker_reads_isolated", lambda: False)
    monkeypatch.setattr(dd, "_toss_live_policy_fast", lambda **kwargs: {})
    monkeypatch.setattr(tc, "is_configured", lambda: False)

    summary = dd._fetch_toss_account_summary_raw()

    assert summary["pnl_scope"]["realized_profit_loss"] == "unavailable"
    assert summary["realized_profit_loss"]["krw"] is None


def test_empty_accounts_summary_keeps_pnl_scope_contract(monkeypatch):
    from core import toss_client as tc

    monkeypatch.setattr(dd, "_dashboard_toss_broker_reads_isolated", lambda: False)
    monkeypatch.setattr(dd, "_toss_live_policy_fast", lambda **kwargs: {})
    monkeypatch.setattr(tc, "is_configured", lambda: True)
    monkeypatch.setattr(tc, "get_accounts", lambda: [])

    summary = dd._fetch_toss_account_summary_raw()

    assert summary["error"] == "Toss account unavailable"
    assert summary["pnl_scope"]["realized_profit_loss"] == "unavailable"
    assert summary["realized_profit_loss"]["krw"] is None
