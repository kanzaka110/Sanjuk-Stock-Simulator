from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.portfolio_cluster_risk import (
    build_correlation_clusters,
    calculate_portfolio_cluster_risk,
    fetch_price_correlation_matrix,
    format_cluster_risk_summary,
    hermes_interpretation_payload,
    normalize_positions,
)


def _portfolio(accounts, total_asset=1_000_000, total_cash=100_000):
    return {
        "accounts": accounts,
        "total_asset": total_asset,
        "total_eval": total_asset,
        "total_cash": total_cash,
    }


def _account(name, *items):
    return {"name": name, "items": list(items)}


def _item(symbol, value, name=None, currency="KRW"):
    return {
        "ticker": symbol, "name": name or symbol,
        "eval_krw": value, "currency": currency,
    }


def test_normalize_aggregates_same_symbol_across_accounts():
    payload = _portfolio([
        _account("일반", _item("005930.KS", 200_000, "삼성전자")),
        _account("ISA", _item("005930.KS", 100_000, "삼성전자"),
                 _item("MU", 200_000, "마이크론", "USD")),
    ])
    normalized = normalize_positions(payload)
    by_symbol = {row["symbol"]: row for row in normalized["positions"]}
    assert by_symbol["005930.KS"]["eval_krw"] == 300_000
    assert by_symbol["005930.KS"]["accounts"] == ["ISA", "일반"]
    assert normalized["holdings_eval_krw"] == 500_000


def test_invested_and_total_asset_weights_are_separate():
    payload = _portfolio([
        _account("일반", _item("005930.KS", 300_000), _item("MU", 200_000, currency="USD")),
    ], total_asset=1_000_000, total_cash=500_000)
    report = calculate_portfolio_cluster_risk(payload)
    samsung = next(row for row in report["positions"] if row["symbol"] == "005930.KS")
    assert samsung["invested_weight_pct"] == 60.0
    assert samsung["asset_weight_pct"] == 30.0
    assert report["summary"]["cash_weight_pct"] == 50.0


def test_theme_weights_are_non_additive_and_disclosed():
    payload = _portfolio([
        _account("일반", _item("005930.KS", 400_000), _item("MU", 400_000, currency="USD")),
    ], total_asset=800_000, total_cash=0)
    report = calculate_portfolio_cluster_risk(payload)
    themes = {row["key"]: row["invested_weight_pct"] for row in report["clusters"]["theme"]}
    assert themes["ai_semiconductor"] == 100.0
    assert themes["memory_cycle"] == 100.0
    assert report["data_quality"]["theme_weights_non_additive"] is True


def test_known_semiconductor_cluster_hits_critical_threshold():
    payload = _portfolio([
        _account("일반",
                 _item("005930.KS", 225_000), _item("000660.KS", 225_000),
                 _item("LMT", 200_000, currency="USD"),
                 _item("005380.KS", 200_000), _item("462870.KS", 150_000)),
    ], total_asset=1_000_000, total_cash=0)
    report = calculate_portfolio_cluster_risk(payload)
    sector = next(row for row in report["clusters"]["sector"] if row["key"] == "semiconductors")
    assert sector["invested_weight_pct"] == 45.0
    alert = next(row for row in report["alerts"]
                 if row["dimension"] == "sector" and row["key"] == "semiconductors")
    assert alert["severity"] == "critical"
    assert report["overall_risk"] == "critical"


def test_threshold_boundary_is_inclusive():
    taxonomy = {
        "AAA": {"sector": "alpha", "region": "US", "economic_currency": "USD",
                "themes": ["alpha"], "instrument_type": "stock"},
        "BBB": {"sector": "beta", "region": "KR", "economic_currency": "KRW",
                "themes": ["beta"], "instrument_type": "stock"},
    }
    payload = _portfolio([_account("x", _item("AAA", 150), _item("BBB", 850))], 1_000, 0)
    report = calculate_portfolio_cluster_risk(payload, taxonomy=taxonomy)
    alert = next(row for row in report["alerts"] if row["key"] == "AAA")
    assert alert["severity"] == "warning"


def test_broad_etf_uses_higher_single_position_threshold():
    payload = _portfolio([
        _account("x", _item("133690.KS", 240), _item("LMT", 200, currency="USD"),
                 _item("005380.KS", 200), _item("462870.KS", 180),
                 _item("090430.KS", 180)),
    ], 1_000, 0)
    report = calculate_portfolio_cluster_risk(payload)
    assert not any(row["type"] == "position_concentration" and row["key"] == "133690.KS"
                   for row in report["alerts"])


def test_unknown_taxonomy_weight_creates_data_quality_alert():
    payload = _portfolio([
        _account("일반", _item("UNKNOWN", 300_000), _item("LMT", 700_000, currency="USD")),
    ], total_asset=1_000_000, total_cash=0)
    report = calculate_portfolio_cluster_risk(payload)
    assert report["data_quality"]["unknown_invested_weight_pct"] == 30.0
    alert = next(row for row in report["alerts"] if row["type"] == "data_quality")
    assert alert["severity"] == "critical"
    assert alert["symbols"] == ["UNKNOWN"]


def test_positive_correlation_connected_components_form_one_cluster():
    positions = normalize_positions(_portfolio([
        _account("x", _item("AAA", 300), _item("BBB", 300), _item("CCC", 400)),
    ], 1_000, 0), taxonomy={
        key: {"sector": key, "region": "US", "economic_currency": "USD",
              "themes": [], "instrument_type": "stock"}
        for key in ("AAA", "BBB", "CCC")
    })["positions"]
    matrix = pd.DataFrame(
        [[1.0, 0.8, 0.2], [0.8, 1.0, 0.76], [0.2, 0.76, 1.0]],
        index=["AAA", "BBB", "CCC"], columns=["AAA", "BBB", "CCC"],
    )
    clusters, coverage = build_correlation_clusters(positions, matrix, threshold=0.75)
    assert len(clusters) == 1
    assert clusters[0]["symbols"] == ["AAA", "BBB", "CCC"]
    assert clusters[0]["invested_weight_pct"] == 100.0
    assert coverage == 100.0


def test_negative_correlation_does_not_create_risk_cluster():
    positions = normalize_positions(_portfolio([
        _account("x", _item("AAA", 500), _item("BBB", 500)),
    ], 1_000, 0), taxonomy={
        key: {"sector": key, "region": "US", "economic_currency": "USD",
              "themes": [], "instrument_type": "stock"}
        for key in ("AAA", "BBB")
    })["positions"]
    matrix = {"AAA": {"BBB": -0.9}, "BBB": {"AAA": -0.9}}
    clusters, coverage = build_correlation_clusters(positions, matrix)
    assert clusters == []
    assert coverage == 100.0


def test_missing_correlation_is_explicit_not_requested():
    report = calculate_portfolio_cluster_risk(_portfolio([], 0, 0))
    assert report["data_quality"]["correlation_status"] == "not_requested"
    assert report["correlation_clusters"] == []
    assert report["overall_risk"] == "low"


def test_engine_does_not_mutate_input():
    payload = _portfolio([_account("일반", _item("005930.KS", 500_000))], 600_000, 100_000)
    before = deepcopy(payload)
    calculate_portfolio_cluster_risk(payload)
    assert payload == before


def test_hermes_payload_contains_facts_and_safety_rules_only():
    report = calculate_portfolio_cluster_risk(_portfolio([
        _account("일반", _item("005930.KS", 500_000), _item("LMT", 500_000, currency="USD")),
    ], 1_000_000, 0))
    payload = hermes_interpretation_payload(report)
    assert payload["read_only"] is True
    assert payload["overall_risk"] == report["overall_risk"]
    assert payload["top_alerts"] == report["alerts"][:8]
    assert any("자동매도" in rule for rule in payload["interpretation_rules"])


def test_report_contract_has_no_order_authority():
    report = calculate_portfolio_cluster_risk(_portfolio([
        _account("일반", _item("005930.KS", 100_000)),
    ], 100_000, 0))
    assert report["read_only"] is True
    assert report["order_side_effects"] is False
    assert "order" not in report
    assert "sell" not in report


def test_dashboard_cluster_risk_data_is_read_only_and_cached(monkeypatch):
    from core import dashboard_data as dd

    payload = _portfolio([
        _account("일반", _item("005930.KS", 300_000), _item("MU", 200_000, currency="USD")),
    ], 700_000, 200_000)
    calls = {"count": 0}

    def fake_portfolio():
        calls["count"] += 1
        return payload

    monkeypatch.setattr(dd, "portfolio_data", fake_portfolio)
    monkeypatch.setattr(dd, "_cache", {}, raising=False)
    first = dd.portfolio_cluster_risk_data()
    second = dd.portfolio_cluster_risk_data()

    assert first == second
    assert calls["count"] == 1
    assert first["scope"] == "samsung_manual_portfolio_only"
    assert first["source"] == "dashboard_portfolio_read_only"
    assert first["order_side_effects"] is False
    assert first["interpretation_payload"]["read_only"] is True


def test_dashboard_cluster_risk_route_is_get_only():
    from web import app as webapp

    routes = [route for route in webapp.app.routes
              if getattr(route, "path", "") == "/api/portfolio/cluster-risk"]
    assert routes
    assert set(getattr(routes[0], "methods", set())) <= {"GET", "HEAD"}


def test_batch_price_history_builds_correlation_matrix(monkeypatch):
    columns = pd.MultiIndex.from_tuples([
        ("Close", "AAA"), ("Close", "BBB"), ("Close", "MISS"),
    ])
    data = pd.DataFrame(
        [[100 + i, 200 + i * 2, None] for i in range(60)],
        columns=columns,
    )
    fake_yf = SimpleNamespace(download=lambda *a, **k: data)
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    matrix, meta = fetch_price_correlation_matrix(
        ["AAA", "BBB", "MISS"], period="6mo", min_points=40)

    assert meta["status"] == "ok"
    assert meta["available_symbols"] == ["AAA", "BBB"]
    assert meta["missing_symbols"] == ["MISS"]
    assert meta["return_points"] == 59
    assert matrix.loc["AAA", "BBB"] > 0.99


def test_cluster_risk_summary_is_fact_only():
    report = calculate_portfolio_cluster_risk(_portfolio([
        _account("일반", _item("005930.KS", 600_000), _item("LMT", 400_000, currency="USD")),
    ], 1_000_000, 0))
    text = format_cluster_risk_summary(report)
    assert "포트폴리오 군집 위험" in text
    assert "자동매도/주문 권한 없음" in text
    assert "005930.KS" in text


def test_cli_build_report_attaches_correlation_metadata(monkeypatch):
    from tools import portfolio_cluster_risk_cli as cli

    payload = _portfolio([
        _account("일반", _item("005930.KS", 500_000), _item("MU", 500_000, currency="USD")),
    ], 1_000_000, 0)
    matrix = pd.DataFrame(
        [[1.0, 0.9], [0.9, 1.0]],
        index=["005930.KS", "MU"], columns=["005930.KS", "MU"],
    )
    meta = {
        "status": "ok", "requested_symbols": ["005930.KS", "MU"],
        "available_symbols": ["005930.KS", "MU"], "missing_symbols": [],
        "return_points": 100,
    }
    monkeypatch.setattr(cli, "fetch_price_correlation_matrix", lambda *a, **k: (matrix, meta))
    report = cli.build_report(payload, with_correlation=True)

    assert report["data_quality"]["correlation_status"] == "available"
    assert report["data_quality"]["correlation_source"] == meta
    assert report["correlation_clusters"][0]["symbols"] == ["005930.KS", "MU"]
    assert report["interpretation_payload"]["read_only"] is True
    assert report["order_side_effects"] is False
