from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace

from core.shadow_measurements import ShadowMeasurementStore
from core.source_observations_v2 import SourceObservationStoreV2
from src.shadow_price_observations import (
    DATASET,
    _shadow_quote_fetcher,
    collect_price_observations,
    load_shadow_symbols,
)

UTC = timezone.utc
OBSERVED_AT = datetime(2026, 7, 21, 7, 10, tzinfo=UTC)


def _quote(*, source="KIS", as_of=OBSERVED_AT, price=100.0):
    return SimpleNamespace(
        price=price,
        high=price + 2,
        low=price - 2,
        change=1.0,
        pct=1.0,
        source=source,
        as_of=as_of.timestamp(),
    )


def test_collect_price_observation_persists_point_in_time_lineage(tmp_path):
    store = SourceObservationStoreV2(tmp_path / "source_observations_v2.db")

    result = collect_price_observations(
        ["005930.KS"],
        market="KR",
        observed_at_utc=OBSERVED_AT,
        quote_fetcher=lambda _symbol: _quote(),
        store=store,
    )

    assert result == {
        "seen": 1,
        "inserted": 1,
        "duplicate": 0,
        "invalid": 0,
        "unavailable": 0,
    }
    with sqlite3.connect(store.db_path) as connection:
        row = connection.execute(
            """SELECT source, dataset, symbol, market, currency_or_unit,
                      source_as_of, ingested_at, fallback_used, payload_json
               FROM observations"""
        ).fetchone()
    payload = json.loads(row[8])
    assert row[:5] == ("kis", DATASET, "005930.KS", "KR", "KRW")
    assert row[5] == "2026-07-21T07:10:00.000000Z"
    assert row[6] == "2026-07-21T07:10:00.000000Z"
    assert row[7] == 0
    assert payload == {
        "change": 1.0,
        "high": 102.0,
        "low": 98.0,
        "price": 100.0,
        "quote_source": "KIS",
    }


def test_yfinance_quote_is_persisted_as_fallback(tmp_path):
    store = SourceObservationStoreV2(tmp_path / "source_observations_v2.db")

    result = collect_price_observations(
        ["MU"],
        market="US",
        observed_at_utc=OBSERVED_AT,
        quote_fetcher=lambda _symbol: _quote(source="yf_daily", price=200.0),
        store=store,
    )

    assert result["inserted"] == 1
    with sqlite3.connect(store.db_path) as connection:
        source, unit, fallback = connection.execute(
            "SELECT source, currency_or_unit, fallback_used FROM observations"
        ).fetchone()
    assert (source, unit, fallback) == ("yfinance", "USD", 1)


def test_future_unknown_and_malformed_quotes_fail_closed(tmp_path):
    store = SourceObservationStoreV2(tmp_path / "source_observations_v2.db")
    quotes = {
        "005930.KS": _quote(as_of=OBSERVED_AT + timedelta(seconds=1)),
        "000660.KS": _quote(source="unknown"),
        "035420.KS": _quote(price=-1.0),
        "051910.KS": None,
    }

    result = collect_price_observations(
        list(quotes),
        market="KR",
        observed_at_utc=OBSERVED_AT,
        quote_fetcher=quotes.get,
        store=store,
    )

    assert result == {
        "seen": 4,
        "inserted": 0,
        "duplicate": 0,
        "invalid": 3,
        "unavailable": 1,
    }
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_market_symbol_scope_is_rejected_before_fetch(tmp_path):
    import pytest

    store = SourceObservationStoreV2(tmp_path / "source_observations_v2.db")
    calls = []

    for market, symbol in (("KR", "MU"), ("US", "005930.KS")):
        with pytest.raises(ValueError, match="market_symbol_invalid"):
            collect_price_observations(
                [symbol],
                market=market,
                observed_at_utc=OBSERVED_AT,
                quote_fetcher=lambda value: calls.append(value),
                store=store,
            )
    assert calls == []


def test_shadow_quote_fetcher_never_calls_kis_or_realtime_chain(monkeypatch):
    import core.market as market

    expected = _quote(source="yf_fast")
    monkeypatch.setattr(market, "_get_quote_yf_live", lambda _symbol: expected)
    monkeypatch.setattr(
        market,
        "_get_quote_daily",
        lambda _symbol: (_ for _ in ()).throw(AssertionError("daily must not run")),
    )
    monkeypatch.setattr(
        market,
        "_get_quote_realtime",
        lambda _symbol: (_ for _ in ()).throw(AssertionError("KIS chain called")),
    )

    assert _shadow_quote_fetcher("MU") is expected


def _append_decision(store, *, decision_id, symbol, market, decided_at):
    return store.append_decision(
        decision_id=decision_id,
        decision_ref=f"shadow:{decision_id}",
        symbol=symbol,
        side="BUY",
        decided_at_utc=decided_at,
        production_bucket="WATCH",
        production_score=70.0,
        feature_set_version="test_v1",
        features={"market": market, "currency": "KRW" if market == "KR" else "USD"},
        source_snapshots=[{
            "snapshot_id": "srcobs_" + ("a" * 64),
            "source": "toss_final_candidate",
            "ingested_at_utc": decided_at,
            "payload_sha256": "b" * 64,
        }],
        candidate_snapshot_sha256="c" * 64,
    )


def test_load_shadow_symbols_is_recent_market_scoped_and_read_only(tmp_path):
    db_path = tmp_path / "shadow_measurements.db"
    store = ShadowMeasurementStore(db_path, now_fn=lambda: OBSERVED_AT)
    _append_decision(
        store,
        decision_id="kr_recent_1",
        symbol="005930.KS",
        market="KR",
        decided_at=OBSERVED_AT - timedelta(hours=1),
    )
    _append_decision(
        store,
        decision_id="kr_recent_2",
        symbol="005930.KS",
        market="KR",
        decided_at=OBSERVED_AT - timedelta(hours=2),
    )
    _append_decision(
        store,
        decision_id="us_recent",
        symbol="MU",
        market="US",
        decided_at=OBSERVED_AT - timedelta(hours=1),
    )
    _append_decision(
        store,
        decision_id="kr_stale",
        symbol="000660.KS",
        market="KR",
        decided_at=OBSERVED_AT - timedelta(days=10),
    )
    before = db_path.read_bytes()

    symbols = load_shadow_symbols(
        db_path,
        market="KR",
        decided_since_utc=OBSERVED_AT - timedelta(days=7),
    )

    assert symbols == ("005930.KS",)
    assert db_path.read_bytes() == before
