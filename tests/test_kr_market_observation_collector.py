"""Standalone KR market observation collector contracts."""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from core.market_data_fetch import (
    CacheSource,
    FetchErrorType,
    FetchResult,
    FetchStatus,
)
from core.source_observations_v2 import SourceObservationStoreV2

UTC = timezone.utc
STARTED = datetime(2026, 7, 15, 1, 0, 0, tzinfo=UTC)
COMPLETED = STARTED + timedelta(seconds=1)
SOURCE_AS_OF = datetime(2026, 7, 14, 6, 30, 0, tzinfo=UTC)


def _result(
    *,
    status=FetchStatus.SUCCESS,
    provider="KIS",
    symbol="005930",
    value=None,
    error_type=FetchErrorType.NONE,
    cache_source=CacheSource.NETWORK,
    fallback_used=False,
    started=STARTED,
    completed=COMPLETED,
    source_fetched_at=None,
):
    return FetchResult(
        status=status,
        provider=provider,
        endpoint="/typed-test",
        tr_id="TEST123",
        venue="J",
        symbol=symbol,
        started_at_utc=started,
        completed_at_utc=completed,
        error_type=error_type,
        cache_source=cache_source,
        fallback_used=fallback_used,
        value=value,
        source_fetched_at_utc=source_fetched_at,
    )


def _orderbook_value():
    return {
        "ticker": "005930.KS",
        "symbol": "005930",
        "provider_time_hhmmss": "101530",
        "source_as_of": COMPLETED.isoformat(),
        "levels": [
            {
                "level": level,
                "ask_price": 80_100 + level,
                "ask_size": 10 + level,
                "bid_price": 80_000 - level,
                "bid_size": 20 + level,
            }
            for level in range(1, 11)
        ],
        "raw_totals": {},
        "expected_execution": {},
        "best_ask": 80_101,
        "best_bid": 79_999,
        "spread": 102,
        "mid_price": 80_050,
        "depth_total_shares": 510,
        "depth_imbalance": 0.0,
        "depth_status": "ok",
        "units": {
            "levels.ask_price": "KRW/share",
            "levels.ask_size": "shares",
            "levels.bid_price": "KRW/share",
            "levels.bid_size": "shares",
        },
        "derived_schema_version": "1",
    }


def _investor_row(*, date="20260714", source_as_of=SOURCE_AS_OF, close=80_000):
    return {
        "ticker": "005930.KS",
        "symbol": "005930",
        "date": date,
        "close": close,
        "institution_net_qty": 100,
        "foreign_net_qty": -50,
        "source_as_of": source_as_of.isoformat(),
        "source_as_of_precision": "business_date",
        "availability_as_of": COMPLETED.isoformat(),
        "intraday": False,
        "units": {
            "close": "KRW/share",
            "institution_net_qty": "shares",
            "foreign_net_qty": "shares",
        },
        "official_fields": {},
    }


def _naver_value():
    return {
        "code": "005930",
        "rows": [
            {
                "date": "20260714",
                "close": 80_000.0,
                "inst_shares": 100.0,
                "foreign_shares": -50.0,
            }
        ],
        "units": {
            "date": "business_date",
            "close": "KRW/share",
            "inst_shares": "shares",
            "foreign_shares": "shares",
        },
        "derived_schema_version": "1",
    }


def _latest(
    store,
    *,
    source,
    dataset,
    decision_at=COMPLETED + timedelta(seconds=1),
):
    return store.latest_as_of(
        decision_at=decision_at,
        source=source,
        dataset=dataset,
        symbol="005930.KS",
        market="KR",
    )


def test_orderbook_success_atomically_persists_observation_and_run(tmp_path):
    from core.kr_market_observation_collector import collect_orderbook_observations

    store = SourceObservationStoreV2(tmp_path / "observations.sqlite3")
    calls = []

    def fetcher(symbol):
        calls.append(symbol)
        return _result(value=_orderbook_value())

    summary = collect_orderbook_observations(
        ["005930.KS"],
        store=store,
        run_id="orderbook-run-1",
        fetcher=fetcher,
    )

    assert calls == ["005930.KS"]
    assert summary == {
        "source": "kis",
        "dataset": "domestic_orderbook",
        "status": "success",
        "rows_seen": 1,
        "rows_inserted": 1,
        "rows_duplicate": 0,
        "rows_skipped": 0,
        "rows_invalid": 0,
        "error_type": "",
    }
    observation = _latest(store, source="kis", dataset="domestic_orderbook")
    assert observation is not None
    assert observation.source_as_of == COMPLETED.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert observation.ingested_at == COMPLETED.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert observation.currency_or_unit == "MIXED"
    assert observation.fallback_used is False
    assert observation.payload["observation"]["levels"][0]["level"] == 1
    assert observation.payload["fetch"]["provider"] == "KIS"
    run = store.latest_collection_run("kis", "domestic_orderbook")
    assert run is not None
    assert run.status == "success"
    assert run.rows_inserted == 1


def test_orderbook_duplicate_retry_counts_duplicate_without_new_row(tmp_path):
    from core.kr_market_observation_collector import collect_orderbook_observations

    store = SourceObservationStoreV2(tmp_path / "duplicate.sqlite3")

    def fetcher(_symbol):
        return _result(value=_orderbook_value())

    first = collect_orderbook_observations(
        ["005930.KS"], store=store, run_id="orderbook-run-a", fetcher=fetcher
    )
    second = collect_orderbook_observations(
        ["005930.KS"], store=store, run_id="orderbook-run-b", fetcher=fetcher
    )

    assert first["rows_inserted"] == 1
    assert second["rows_duplicate"] == 1
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 2


def test_orderbook_success_with_incomplete_depth_is_rejected_not_persisted(tmp_path):
    from core.kr_market_observation_collector import collect_orderbook_observations

    store = SourceObservationStoreV2(tmp_path / "invalid-orderbook.sqlite3")
    malformed = _orderbook_value()
    malformed["levels"] = malformed["levels"][:9]

    summary = collect_orderbook_observations(
        ["005930.KS"],
        store=store,
        run_id="orderbook-invalid-depth",
        fetcher=lambda _symbol: _result(value=malformed),
    )

    assert summary["status"] == "failed"
    assert summary["rows_invalid"] == 1
    assert _latest(store, source="kis", dataset="domestic_orderbook") is None


def test_kis_investor_success_does_not_call_naver(tmp_path):
    from core.kr_market_observation_collector import collect_investor_observations

    store = SourceObservationStoreV2(tmp_path / "investor-kis.sqlite3")
    naver_calls = []

    result = collect_investor_observations(
        ["005930.KS"],
        store=store,
        run_id="investor-run-1",
        kis_fetcher=lambda _symbol: _result(value=[_investor_row()]),
        naver_fetcher=lambda symbol, **_kwargs: naver_calls.append(symbol),
    )

    assert naver_calls == []
    assert result["kis"]["status"] == "success"
    assert result["kis"]["rows_inserted"] == 1
    assert result["naver"] is None
    observation = _latest(store, source="kis", dataset="domestic_investor_flow")
    assert observation is not None
    assert observation.source_as_of == SOURCE_AS_OF.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert observation.ingested_at == COMPLETED.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert observation.fallback_used is False


def test_cached_naver_fallback_preserves_fetch_lineage_without_backdating_ingestion(
    tmp_path,
):
    from core.kr_market_observation_collector import collect_investor_observations

    store = SourceObservationStoreV2(tmp_path / "investor-fallback.sqlite3")
    fallback_calls = []

    def naver(symbol, *, fallback_used):
        fallback_calls.append((symbol, fallback_used))
        return _result(
            provider="NAVER",
            symbol="005930",
            value=_naver_value(),
            fallback_used=True,
            cache_source=CacheSource.MEMORY,
            started=COMPLETED + timedelta(seconds=5),
            completed=COMPLETED + timedelta(seconds=6),
            source_fetched_at=COMPLETED,
        )

    result = collect_investor_observations(
        ["005930.KS"],
        store=store,
        run_id="investor-run-2",
        kis_fetcher=lambda _symbol: _result(
            status=FetchStatus.SKIPPED,
            value=None,
            error_type=FetchErrorType.NOT_CONFIGURED,
            cache_source=CacheSource.NONE,
        ),
        naver_fetcher=naver,
    )

    assert fallback_calls == [("005930", True)]
    assert result["kis"]["status"] == "skipped"
    assert result["kis"]["error_type"] == "not_configured"
    assert result["naver"]["status"] == "success"
    assert _latest(
        store, source="naver", dataset="domestic_investor_flow"
    ) is None
    observation = _latest(
        store,
        source="naver",
        dataset="domestic_investor_flow",
        decision_at=COMPLETED + timedelta(seconds=7),
    )
    assert observation is not None
    assert observation.fallback_used is True
    current_completed = COMPLETED + timedelta(seconds=6)
    assert observation.ingested_at == current_completed.strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    assert observation.payload["fetch"]["provider"] == "NAVER"
    normalized = observation.payload["observation"]
    assert normalized["source_as_of"] == SOURCE_AS_OF.isoformat()
    assert normalized["availability_as_of"] == COMPLETED.isoformat()
    availability = datetime.fromisoformat(normalized["availability_as_of"])
    assert SOURCE_AS_OF <= availability <= current_completed
    assert normalized["units"]["inst_shares"] == "shares"
    assert observation.payload["fetch"]["started_at_utc"] == (
        COMPLETED + timedelta(seconds=5)
    ).isoformat()
    assert observation.payload["fetch"]["completed_at_utc"] == (
        COMPLETED + timedelta(seconds=6)
    ).isoformat()
    assert observation.payload["fetch"]["source_fetched_at_utc"] == (
        COMPLETED.isoformat()
    )
    assert _latest(store, source="kis", dataset="domestic_investor_flow") is None


def test_kis_empty_is_not_laundered_through_naver(tmp_path):
    from core.kr_market_observation_collector import collect_investor_observations

    store = SourceObservationStoreV2(tmp_path / "investor-empty.sqlite3")
    naver_calls = []

    result = collect_investor_observations(
        ["005930.KS"],
        store=store,
        run_id="investor-run-empty",
        kis_fetcher=lambda _symbol: _result(status=FetchStatus.EMPTY, value=[]),
        naver_fetcher=lambda symbol, **_kwargs: naver_calls.append(symbol),
    )

    assert naver_calls == []
    assert result["kis"]["status"] == "skipped"
    assert result["kis"]["rows_seen"] == 0
    assert result["naver"] is None


def test_malformed_kis_success_is_all_or_nothing_then_uses_naver_fallback(tmp_path):
    from core.kr_market_observation_collector import collect_investor_observations

    store = SourceObservationStoreV2(tmp_path / "investor-malformed.sqlite3")
    malformed_rows = [
        _investor_row(),
        {"date": "bad", "source_as_of": SOURCE_AS_OF.isoformat()},
    ]
    fallback_calls = []

    def naver(symbol, *, fallback_used):
        fallback_calls.append((symbol, fallback_used))
        return _result(
            provider="NAVER",
            symbol="005930",
            value=_naver_value(),
            fallback_used=True,
        )

    result = collect_investor_observations(
        ["005930.KS"],
        store=store,
        run_id="investor-run-malformed",
        kis_fetcher=lambda _symbol: _result(value=malformed_rows),
        naver_fetcher=naver,
    )

    assert result["kis"]["status"] == "failed"
    assert result["kis"]["rows_inserted"] == 0
    assert result["kis"]["rows_invalid"] == 1
    assert fallback_calls == [("005930", True)]
    assert result["naver"]["status"] == "success"
    assert _latest(store, source="kis", dataset="domestic_investor_flow") is None
    assert _latest(store, source="naver", dataset="domestic_investor_flow") is not None


def test_batch_persistence_failure_rolls_back_rows_and_records_failed_run(tmp_path):
    from core.kr_market_observation_collector import collect_investor_observations

    real_store = SourceObservationStoreV2(tmp_path / "atomic-failure.sqlite3")

    class FailSecondAppend:
        def __init__(self, inner):
            self.inner = inner
            self.calls = 0
            self.db_path = inner.db_path

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def append(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise sqlite3.OperationalError("must-not-be-logged")
            return self.inner.append(**kwargs)

    store = FailSecondAppend(real_store)
    rows = [
        _investor_row(date="20260714", source_as_of=SOURCE_AS_OF),
        _investor_row(
            date="20260713", source_as_of=SOURCE_AS_OF - timedelta(days=1)
        ),
    ]

    result = collect_investor_observations(
        ["005930.KS"],
        store=store,
        run_id="investor-run-fail",
        kis_fetcher=lambda _symbol: _result(value=rows),
        naver_fetcher=lambda *_args, **_kwargs: pytest.fail("fallback must not run"),
    )

    assert result["kis"]["status"] == "failed"
    assert result["kis"]["error_type"] == "persistence_operationalerror"
    with sqlite3.connect(real_store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        run = conn.execute(
            "SELECT status, rows_invalid, error_type FROM collection_runs"
        ).fetchone()
    assert run == ("failed", 2, "persistence_operationalerror")


def test_unknown_persistence_exception_class_is_reduced_to_fixed_safe_enum():
    from core.kr_market_observation_collector import _persistence_error_type

    unusual_error = type("X" * 100, (Exception,), {})()

    assert _persistence_error_type(unusual_error) == "persistence_failed"


def test_fetcher_exception_records_failed_run_without_exception_text(tmp_path, caplog):
    from core.kr_market_observation_collector import collect_orderbook_observations

    store = SourceObservationStoreV2(tmp_path / "fetch-exception.sqlite3")

    def fetcher(_symbol):
        raise RuntimeError("credential-like-text-must-not-be-logged")

    with caplog.at_level(logging.WARNING):
        summary = collect_orderbook_observations(
            ["005930.KS"],
            store=store,
            run_id="orderbook-fetch-error",
            fetcher=fetcher,
        )

    assert summary["status"] == "failed"
    assert summary["error_type"] == "provider"
    run = store.latest_collection_run("kis", "domestic_orderbook")
    assert run is not None
    assert run.status == "failed"
    assert run.error_type == "provider"
    assert "RuntimeError" in caplog.text
    assert "credential-like-text-must-not-be-logged" not in caplog.text


def test_cycle_throttles_recent_investor_success_but_still_collects_orderbook(tmp_path):
    from core.kr_market_observation_collector import run_candidate_observation_cycle

    store = SourceObservationStoreV2(tmp_path / "cycle-throttle.sqlite3")
    store.record_collection_run(
        source="kis",
        dataset="domestic_investor_flow",
        run_id="prior-investor-success",
        started_at=STARTED,
        completed_at=COMPLETED,
        status="success",
        rows_seen=0,
        rows_inserted=0,
        rows_duplicate=0,
        rows_skipped=0,
        rows_invalid=0,
        error_type="",
    )
    investor_calls = []

    result = run_candidate_observation_cycle(
        ["005930.KS"],
        run_id="cycle-throttled",
        store=store,
        now_utc=COMPLETED + timedelta(hours=1),
        orderbook_fetcher=lambda _symbol: _result(value=_orderbook_value()),
        kis_investor_fetcher=lambda symbol: investor_calls.append(symbol),
        naver_fetcher=lambda *_args, **_kwargs: pytest.fail("naver must not run"),
    )

    assert result["orderbook"]["status"] == "success"
    assert result["investor"] == {"skipped": "throttled"}
    assert investor_calls == []


@pytest.mark.parametrize("prior_status", ["partial", "degraded"])
def test_cycle_retries_partial_or_degraded_investor_after_two_hours(
    tmp_path, prior_status
):
    from core.kr_market_observation_collector import run_candidate_observation_cycle

    inner_store = SourceObservationStoreV2(
        tmp_path / f"cycle-retry-{prior_status}.sqlite3"
    )

    class StoreWithPriorInvestorRun:
        def __getattr__(self, name):
            return getattr(inner_store, name)

        def latest_collection_run(self, source, dataset):
            assert (source, dataset) == ("kis", "domestic_investor_flow")
            return type(
                "PriorRun",
                (),
                {
                    "status": prior_status,
                    "completed_at": COMPLETED.strftime(
                        "%Y-%m-%dT%H:%M:%S.%fZ"
                    ),
                },
            )()

    investor_calls = []
    retry_started = COMPLETED + timedelta(hours=2)

    def investor(symbol):
        investor_calls.append(symbol)
        return _result(
            status=FetchStatus.EMPTY,
            value=[],
            started=retry_started,
            completed=retry_started + timedelta(seconds=1),
        )

    result = run_candidate_observation_cycle(
        ["005930.KS"],
        run_id=f"cycle-retry-{prior_status}",
        store=StoreWithPriorInvestorRun(),
        now_utc=retry_started,
        orderbook_fetcher=lambda _symbol: _result(value=_orderbook_value()),
        kis_investor_fetcher=investor,
        naver_fetcher=lambda *_args, **_kwargs: pytest.fail("empty must not fallback"),
    )

    assert investor_calls == ["005930.KS"]
    assert result["investor"]["kis"]["status"] == "skipped"


def test_cycle_retries_failed_investor_after_one_hour(tmp_path):
    from core.kr_market_observation_collector import run_candidate_observation_cycle

    store = SourceObservationStoreV2(tmp_path / "cycle-retry.sqlite3")
    store.record_collection_run(
        source="kis",
        dataset="domestic_investor_flow",
        run_id="prior-investor-failure",
        started_at=STARTED,
        completed_at=COMPLETED,
        status="failed",
        rows_seen=0,
        rows_inserted=0,
        rows_duplicate=0,
        rows_skipped=0,
        rows_invalid=0,
        error_type="network",
    )
    investor_calls = []

    def investor(symbol):
        investor_calls.append(symbol)
        return _result(status=FetchStatus.EMPTY, value=[])

    result = run_candidate_observation_cycle(
        ["005930.KS"],
        run_id="cycle-retry",
        store=store,
        now_utc=COMPLETED + timedelta(hours=2),
        orderbook_fetcher=lambda _symbol: _result(value=_orderbook_value()),
        kis_investor_fetcher=investor,
        naver_fetcher=lambda *_args, **_kwargs: pytest.fail("empty must not fallback"),
    )

    assert investor_calls == ["005930.KS"]
    assert result["investor"]["kis"]["status"] == "skipped"


@pytest.mark.parametrize(
    "symbols,error",
    [
        ([], "symbols_empty"),
        (["AAPL"], "kr_symbol_invalid"),
        (["005930.KS"] * 11, "symbols_limit_exceeded"),
        (["005930.KS", "005930.KS"], "symbols_duplicate"),
    ],
)
def test_symbol_scope_is_strict_before_any_fetch(tmp_path, symbols, error):
    from core.kr_market_observation_collector import collect_orderbook_observations

    store = SourceObservationStoreV2(tmp_path / f"invalid-{error}.sqlite3")
    calls = []
    with pytest.raises(ValueError, match=error):
        collect_orderbook_observations(
            symbols,
            store=store,
            run_id="invalid-run",
            fetcher=lambda symbol: calls.append(symbol),
        )
    assert calls == []


def test_enqueue_is_single_flight_daemon_and_carries_symbols_only(monkeypatch):
    import core.kr_market_observation_collector as collector

    started = []

    class InlineThread:
        def __init__(self, *, target, args, daemon, name):
            started.append((target, args, daemon, name))

        def start(self):
            target, args, _, _ = started[-1]
            target(*args)

    monkeypatch.setattr(collector.threading, "Thread", InlineThread)
    monkeypatch.setattr(
        collector,
        "run_candidate_observation_cycle",
        lambda symbols, **_kwargs: {"symbols": symbols},
    )

    accepted = collector.enqueue_candidate_observation_cycle(
        ["005930.KS"], run_id="enqueue-run"
    )

    assert accepted is True
    assert len(started) == 1
    assert started[0][2] is True
    assert started[0][3] == "kr-market-observation-collector"
    assert started[0][1][0] == ("005930.KS",)


def test_busy_enqueue_drops_without_blocking(monkeypatch):
    import core.kr_market_observation_collector as collector

    class BusyLock:
        def __init__(self):
            self.blocking = []

        def acquire(self, blocking=True):
            self.blocking.append(blocking)
            return False

        def release(self):
            raise AssertionError("busy path must not release")

    busy = BusyLock()
    monkeypatch.setattr(collector, "_WORKER_LOCK", busy)

    assert collector.enqueue_candidate_observation_cycle(
        ["005930.KS"], run_id="busy-run"
    ) is False
    assert busy.blocking == [False]


def test_real_daemon_returns_before_cycle_completion_and_never_runs_on_main_thread(
    monkeypatch,
):
    import core.kr_market_observation_collector as collector

    main_thread_id = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    observed = {}

    def blocking_cycle(symbols, **_kwargs):
        observed["thread_id"] = threading.get_ident()
        observed["symbols"] = symbols
        entered.set()
        assert release.wait(timeout=2)
        finished.set()

    monkeypatch.setattr(collector, "run_candidate_observation_cycle", blocking_cycle)

    accepted = collector.enqueue_candidate_observation_cycle(
        ["005930.KS"], run_id="actual-thread-run"
    )

    assert accepted is True
    assert entered.wait(timeout=1)
    assert finished.is_set() is False
    assert observed == {
        "thread_id": observed["thread_id"],
        "symbols": ("005930.KS",),
    }
    assert observed["thread_id"] != main_thread_id
    release.set()
    assert finished.wait(timeout=1)


def test_worker_start_failure_releases_lock_and_logs_exception_class_only(
    monkeypatch, caplog
):
    import core.kr_market_observation_collector as collector

    class FailingThread:
        def __init__(self, *, target, args, daemon, name):
            assert daemon is True

        def start(self):
            raise RuntimeError("collector-start-secret")

    monkeypatch.setattr(collector.threading, "Thread", FailingThread)
    with caplog.at_level(logging.WARNING):
        accepted = collector.enqueue_candidate_observation_cycle(
            ["005930.KS"], run_id="start-failure-run"
        )

    assert accepted is False
    assert collector._WORKER_LOCK.acquire(blocking=False) is True
    collector._WORKER_LOCK.release()
    assert "RuntimeError" in caplog.text
    assert "collector-start-secret" not in caplog.text
