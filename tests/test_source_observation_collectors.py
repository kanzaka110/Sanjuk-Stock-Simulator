"""Official-source collector to append-only observation-store contracts."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from core import dart_monitor, edgar_monitor
from core.source_observation_collectors import (
    record_dart_disclosure_observations,
    record_sec_filing_observations,
)
from core.source_observations import SourceObservationStore

UTC = timezone.utc


def test_sec_filing_collector_persists_valid_hits_and_counts_invalid_rows(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    ingested_at = datetime(2026, 7, 14, 12, 30, tzinfo=UTC)
    hits = [
        {
            "ticker": "MRVL",
            "form": "8-K",
            "severity": "high",
            "filing_date": "2026-07-14",
            "accession": "0001835632-26-000123",
            "description": "Current report",
            "items": ["2.02 실적 발표"],
            "url": "https://www.sec.gov/example",
        },
        {
            "ticker": "LMT",
            "form": "8-K",
            "filing_date": "2026-07-14",
            "accession": "",
        },
    ]

    first = record_sec_filing_observations(
        hits, store=store, ingested_at=ingested_at
    )
    duplicate = record_sec_filing_observations(
        hits, store=store, ingested_at=ingested_at
    )

    assert first.seen == 2
    assert first.inserted == 1
    assert first.duplicates == 0
    assert first.skipped == 0
    assert first.invalid == 1
    assert duplicate.inserted == 0
    assert duplicate.duplicates == 1
    assert duplicate.invalid == 1

    observation = store.latest_as_of(
        source="sec_submissions",
        symbol="MRVL",
        decision_at=datetime(2026, 7, 14, 13, 0, tzinfo=UTC),
    )
    assert observation is not None
    assert observation.source_record_id == "0001835632-26-000123"
    assert observation.currency == "USD"
    assert observation.fallback_used is False
    assert observation.payload["form"] == "8-K"
    assert observation.payload["items"] == ["2.02 실적 발표"]


def test_edgar_monitor_records_all_hits_even_when_alert_is_already_seen(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    state_path = tmp_path / "edgar_state.json"
    accession = "0001835632-26-000123"
    state_path.write_text(
        json.dumps({"seen_accessions": [accession]}), encoding="utf-8"
    )
    hit = {
        "ticker": "MRVL",
        "form": "8-K",
        "severity": "high",
        "filing_date": "2026-07-14",
        "accession": accession,
        "description": "Current report",
        "items": ["2.02 실적 발표"],
        "url": "https://www.sec.gov/example",
    }

    with patch.object(edgar_monitor, "_state_path", return_value=state_path), \
         patch.object(edgar_monitor, "_us_holding_tickers", return_value=["MRVL"]), \
         patch.object(edgar_monitor, "_cik_map", return_value={"MRVL": 1835632}), \
         patch.object(edgar_monitor, "fetch_recent_filings", return_value=[hit]), \
         patch.object(edgar_monitor.time, "sleep", return_value=None):
        result = edgar_monitor.run_edgar_monitor(
            now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
            force=True,
            observation_store=store,
        )

    assert result["hit_count"] == 1
    assert result["new_hit_count"] == 0
    assert result["observation_inserted"] == 1
    assert result["observation_invalid"] == 0
    assert store.count(source="sec_submissions", symbol="MRVL") == 1
    run = store.latest_collection_run(source="sec_submissions")
    assert run is not None
    assert run.status == "success"
    assert run.rows_seen == 1
    assert run.rows_inserted == 1


def test_dart_collector_persists_listed_company_disclosures_without_guessing_exchange(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    items = [
        {
            "rcept_no": "20260714000123",
            "rcept_dt": "20260714",
            "stock_code": "005930",
            "corp_name": "삼성전자",
            "report_nm": "반기보고서",
        },
        {
            "rcept_no": "20260714000124",
            "rcept_dt": "20260714",
            "stock_code": "",
            "corp_name": "비상장사",
            "report_nm": "기타공시",
        },
    ]
    ingested_at = datetime(2026, 7, 14, 12, 30, tzinfo=UTC)

    first = record_dart_disclosure_observations(
        items, store=store, ingested_at=ingested_at
    )
    duplicate = record_dart_disclosure_observations(
        items, store=store, ingested_at=ingested_at
    )

    assert first.inserted == 1
    assert first.skipped == 1
    assert first.invalid == 0
    assert duplicate.duplicates == 1
    assert duplicate.skipped == 1
    observation = store.latest_as_of(
        source="opendart_disclosures",
        symbol="KRX:005930",
        decision_at=ingested_at,
    )
    assert observation is not None
    assert observation.source_record_id == "20260714000123"
    assert observation.currency == "KRW"
    assert observation.payload["report_nm"] == "반기보고서"
    assert observation.fallback_used is False


def test_dart_monitor_records_all_official_items_even_without_risk_keyword_hit(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    state_path = tmp_path / "dart_state.json"
    item = {
        "rcept_no": "20260714000123",
        "rcept_dt": "20260714",
        "stock_code": "005930",
        "corp_name": "삼성전자",
        "report_nm": "반기보고서",
    }

    with patch.object(dart_monitor, "_api_key", return_value="configured"), \
         patch.object(dart_monitor, "_state_path", return_value=state_path), \
         patch.object(dart_monitor, "_toss_holding_codes", return_value={"005930"}), \
         patch.object(
             dart_monitor,
             "fetch_recent_disclosures",
             return_value={"ok": True, "items": [item]},
         ):
        result = dart_monitor.run_dart_monitor(
            now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
            force=True,
            observation_store=store,
        )

    assert result["hit_count"] == 0
    assert result["observation_inserted"] == 1
    assert result["observation_invalid"] == 0
    assert store.count(source="opendart_disclosures", symbol="KRX:005930") == 1
    run = store.latest_collection_run(source="opendart_disclosures")
    assert run is not None
    assert run.status == "success"
    assert run.rows_seen == 1
    assert run.rows_inserted == 1


def test_edgar_fetch_reports_source_error_without_breaking_list_api():
    error_types = []
    with patch.object(edgar_monitor.requests, "get", side_effect=OSError("network down")):
        hits = edgar_monitor.fetch_recent_filings(
            "MRVL", 1835632, error_types=error_types
        )

    assert hits == []
    assert error_types == ["OSError"]


def test_edgar_monitor_records_failed_run_for_source_request_error(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    state_path = tmp_path / "edgar_state.json"

    def failed_fetch(_ticker, _cik, *, error_types=None, **_kwargs):
        assert error_types is not None
        error_types.append("OSError")
        return []

    with patch.object(edgar_monitor, "_state_path", return_value=state_path), \
         patch.object(edgar_monitor, "_us_holding_tickers", return_value=["MRVL"]), \
         patch.object(edgar_monitor, "_cik_map", return_value={"MRVL": 1835632}), \
         patch.object(edgar_monitor, "fetch_recent_filings", side_effect=failed_fetch), \
         patch.object(edgar_monitor.time, "sleep", return_value=None):
        result = edgar_monitor.run_edgar_monitor(
            now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
            force=True,
            observation_store=store,
        )

    assert result["hit_count"] == 0
    assert result["source_fetch_error_count"] == 1
    assert result["observation_run_status"] == "failed"
    run = store.latest_collection_run(source="sec_submissions")
    assert run is not None
    assert run.status == "failed"
    assert run.error_type == "OSError"


def test_edgar_same_timestamp_retries_get_distinct_run_ids(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    state_path = tmp_path / "edgar_state.json"
    hit = {
        "ticker": "MRVL",
        "form": "8-K",
        "severity": "high",
        "filing_date": "2026-07-14",
        "accession": "0001835632-26-000123",
        "description": "Current report",
        "items": [],
        "url": "https://www.sec.gov/example",
    }
    now = datetime(2026, 7, 14, 12, 30, tzinfo=UTC)

    with patch.object(edgar_monitor, "_state_path", return_value=state_path), \
         patch.object(edgar_monitor, "_us_holding_tickers", return_value=["MRVL"]), \
         patch.object(edgar_monitor, "_cik_map", return_value={"MRVL": 1835632}), \
         patch.object(edgar_monitor, "fetch_recent_filings", return_value=[hit]), \
         patch.object(edgar_monitor.time, "sleep", return_value=None):
        first = edgar_monitor.run_edgar_monitor(
            now=now, force=True, observation_store=store
        )
        second = edgar_monitor.run_edgar_monitor(
            now=now, force=True, observation_store=store
        )

    assert first["observation_run_status"] == "success"
    assert second["observation_run_status"] == "success"
    assert second["observation_error"] == ""
    latest = store.latest_collection_run(source="sec_submissions")
    assert latest is not None
    assert latest.rows_duplicate == 1


def test_dart_monitor_records_failed_run_for_fetch_error(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    state_path = tmp_path / "dart_state.json"

    with patch.object(dart_monitor, "_api_key", return_value="configured"), \
         patch.object(dart_monitor, "_state_path", return_value=state_path), \
         patch.object(dart_monitor, "_toss_holding_codes", return_value={"005930"}), \
         patch.object(
             dart_monitor,
             "fetch_recent_disclosures",
             return_value={"ok": False, "reason": "http_500", "items": []},
         ):
        result = dart_monitor.run_dart_monitor(
            now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
            force=True,
            observation_store=store,
        )

    assert result["skipped"] == "http_500"
    assert result["observation_run_status"] == "failed"
    run = store.latest_collection_run(source="opendart_disclosures")
    assert run is not None
    assert run.status == "failed"
    assert run.error_type == "http_500"


def test_dart_monitor_records_failed_run_when_api_key_is_missing(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    state_path = tmp_path / "dart_state.json"

    with patch.object(dart_monitor, "_api_key", return_value=""), \
         patch.object(dart_monitor, "_state_path", return_value=state_path):
        result = dart_monitor.run_dart_monitor(
            now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
            force=True,
            observation_store=store,
        )

    assert result["skipped"] == "no_api_key"
    assert result["observation_run_status"] == "failed"
    run = store.latest_collection_run(source="opendart_disclosures")
    assert run is not None
    assert run.status == "failed"
    assert run.error_type == "no_api_key"


def test_edgar_monitor_marks_missing_cik_as_source_failure(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    state_path = tmp_path / "edgar_state.json"

    with patch.object(edgar_monitor, "_state_path", return_value=state_path), \
         patch.object(edgar_monitor, "_us_holding_tickers", return_value=["UNKNOWN"]), \
         patch.object(edgar_monitor, "_cik_map", return_value={}), \
         patch.object(edgar_monitor, "fetch_recent_filings") as fetch_mock, \
         patch.object(edgar_monitor.time, "sleep", return_value=None):
        result = edgar_monitor.run_edgar_monitor(
            now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
            force=True,
            observation_store=store,
        )

    fetch_mock.assert_not_called()
    assert result["source_mapping_missing_count"] == 1
    assert result["observation_run_status"] == "failed"
    run = store.latest_collection_run(source="sec_submissions")
    assert run is not None
    assert run.status == "failed"
    assert run.error_type == "missing_cik"


def test_edgar_monitor_uses_actual_fetch_completion_as_ingested_at(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    state_path = tmp_path / "edgar_state.json"
    started_at = datetime(2026, 7, 14, 12, 31, tzinfo=UTC)
    completed_at = datetime(2026, 7, 14, 12, 32, tzinfo=UTC)
    hit = {
        "ticker": "MRVL",
        "form": "8-K",
        "severity": "high",
        "filing_date": "2026-07-14",
        "accession": "0001835632-26-000123",
        "description": "Current report",
        "items": [],
        "url": "https://www.sec.gov/example",
    }

    with patch.object(edgar_monitor, "_state_path", return_value=state_path), \
         patch.object(edgar_monitor, "_us_holding_tickers", return_value=["MRVL"]), \
         patch.object(edgar_monitor, "_cik_map", return_value={"MRVL": 1835632}), \
         patch.object(edgar_monitor, "fetch_recent_filings", return_value=[hit]), \
         patch.object(edgar_monitor.time, "sleep", return_value=None), \
         patch.object(
             edgar_monitor,
             "_utc_now",
             side_effect=[started_at, completed_at],
             create=True,
         ):
        result = edgar_monitor.run_edgar_monitor(
            now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
            force=True,
            observation_store=store,
        )

    assert result["observation_run_status"] == "success"
    assert store.latest_as_of(
        source="sec_submissions",
        symbol="MRVL",
        decision_at=datetime(2026, 7, 14, 12, 31, 59, tzinfo=UTC),
    ) is None
    assert store.latest_as_of(
        source="sec_submissions",
        symbol="MRVL",
        decision_at=completed_at,
    ) is not None
    run = store.latest_collection_run(source="sec_submissions")
    assert run is not None
    assert run.started_at == started_at.isoformat()
    assert run.completed_at == completed_at.isoformat()


def test_dart_fetch_collects_every_page_and_rejects_partial_page_failure():
    item1 = {"rcept_no": "20260714000123", "stock_code": "005930"}
    item2 = {"rcept_no": "20260714000124", "stock_code": "000660"}
    page1 = Mock(status_code=200)
    page1.json.return_value = {
        "status": "000",
        "total_page": 2,
        "total_count": 2,
        "list": [item1],
    }
    page2 = Mock(status_code=200)
    page2.json.return_value = {
        "status": "000",
        "total_page": 2,
        "total_count": 2,
        "list": [item2],
    }
    with patch.object(dart_monitor, "_api_key", return_value="configured"), \
         patch("requests.get", side_effect=[page1, page2]) as get:
        result = dart_monitor.fetch_recent_disclosures(
            now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC)
        )

    assert result["ok"] is True
    assert [item["rcept_no"] for item in result["items"]] == [
        "20260714000123",
        "20260714000124",
    ]
    assert result["pages_fetched"] == 2
    assert result["total_count"] == 2
    assert get.call_count == 2
    assert {call.kwargs["timeout"] for call in get.call_args_list} == {(3.0, 5.0)}

    failed_page = Mock(status_code=500)
    with patch.object(dart_monitor, "_api_key", return_value="configured"), \
         patch("requests.get", side_effect=[page1, failed_page]):
        partial = dart_monitor.fetch_recent_disclosures(
            now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC)
        )
    assert partial["ok"] is False
    assert partial["items"] == []
    assert partial["reason"] == "http_500"

    no_more_data = Mock(status_code=200)
    no_more_data.json.return_value = {"status": "013"}
    with patch.object(dart_monitor, "_api_key", return_value="configured"), \
         patch("requests.get", side_effect=[page1, no_more_data]):
        truncated = dart_monitor.fetch_recent_disclosures(
            now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC)
        )
    assert truncated == {"ok": False, "reason": "dart_status_013", "items": []}

    too_many = Mock(status_code=200)
    too_many.json.return_value = {
        "status": "000",
        "total_page": 21,
        "total_count": 2001,
        "list": [item1],
    }
    with patch.object(dart_monitor, "_api_key", return_value="configured"), \
         patch("requests.get", return_value=too_many) as get:
        limited = dart_monitor.fetch_recent_disclosures(
            now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC)
        )
    assert limited == {"ok": False, "reason": "pagination_limit", "items": []}
    assert get.call_count == 1


def test_dart_page_failure_does_not_wait_for_slow_sibling_worker():
    first = Mock(status_code=200)
    first.json.return_value = {
        "status": "000",
        "total_page": 3,
        "total_count": 3,
        "list": [{"rcept_no": "20260714000123"}],
    }
    failed = Mock(status_code=500)
    slow = Mock(status_code=200)
    slow.json.return_value = {
        "status": "000",
        "total_page": 3,
        "total_count": 3,
        "list": [{"rcept_no": "20260714000125"}],
    }
    slow_started = threading.Event()
    slow_release = threading.Event()

    def get_page(*_args, **kwargs):
        page_no = kwargs["params"]["page_no"]
        if page_no == 1:
            return first
        if page_no == 3:
            slow_started.set()
            slow_release.wait(timeout=2)
            return slow
        assert slow_started.wait(timeout=1)
        return failed

    started = time.perf_counter()
    try:
        with patch.object(dart_monitor, "_api_key", return_value="configured"), \
             patch("requests.get", side_effect=get_page):
            result = dart_monitor.fetch_recent_disclosures(
                now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC)
            )
    finally:
        slow_release.set()
    elapsed = time.perf_counter() - started

    assert result == {"ok": False, "reason": "http_500", "items": []}
    assert elapsed < 0.5


def test_dart_total_deadline_bounds_a_hung_first_page():
    response = Mock(status_code=200)
    response.json.return_value = {
        "status": "000",
        "total_page": 1,
        "total_count": 1,
        "list": [{"rcept_no": "20260714000123"}],
    }
    release = threading.Event()

    def slow_get(*_args, **_kwargs):
        release.wait(timeout=1)
        return response

    started = time.perf_counter()
    try:
        with patch.object(dart_monitor, "_api_key", return_value="configured"), \
             patch.object(
                 dart_monitor,
                 "_DART_TOTAL_TIMEOUT_SECONDS",
                 0.05,
                 create=True,
             ), patch("requests.get", side_effect=slow_get):
            result = dart_monitor.fetch_recent_disclosures(
                now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC)
            )
    finally:
        release.set()
    elapsed = time.perf_counter() - started

    assert result == {"ok": False, "reason": "collector_timeout", "items": []}
    assert elapsed < 0.5


def test_dart_monitor_rolls_back_partial_batch_and_records_failed_run(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    state_path = tmp_path / "dart_state.json"
    items = [
        {
            "rcept_no": "20260714000123",
            "rcept_dt": "20260714",
            "stock_code": "005930",
            "corp_name": "삼성전자",
            "report_nm": "주요사항보고서(유상증자결정)",
        },
        {
            "rcept_no": "20260714000124",
            "rcept_dt": "20260714",
            "stock_code": "000660",
            "corp_name": "SK하이닉스",
            "report_nm": "반기보고서",
        },
    ]
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER fail_second_observation
            BEFORE INSERT ON source_observations
            WHEN NEW.source_record_id = '20260714000124'
            BEGIN
                SELECT RAISE(ABORT, 'injected_batch_failure');
            END
            """
        )

    with patch.object(dart_monitor, "_api_key", return_value="configured"), \
         patch.object(dart_monitor, "_state_path", return_value=state_path), \
         patch.object(dart_monitor, "_toss_holding_codes", return_value={"005930"}), \
         patch.object(
             dart_monitor,
             "fetch_recent_disclosures",
             return_value={"ok": True, "items": items},
         ), patch("core.telegram.send_simple_message", return_value=True) as send:
        result = dart_monitor.run_dart_monitor(
            now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
            force=True,
            observation_store=store,
        )

    assert store.count(source="opendart_disclosures", symbol="KRX:005930") == 0
    assert result["observation_inserted"] == 0
    assert result["observation_run_status"] == "failed"
    assert result["sent"] is True
    send.assert_called_once()
    saved_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "20260714000123" in saved_state["seen_rcept_nos"]
    run = store.latest_collection_run(source="opendart_disclosures")
    assert run is not None
    assert run.status == "failed"


def test_dart_errors_never_persist_exception_credentials(tmp_path):
    fake = "ghp_" + "A" * 36
    with patch.object(dart_monitor, "_api_key", return_value="configured"), \
         patch("requests.get", side_effect=RuntimeError(fake)):
        fetched = dart_monitor.fetch_recent_disclosures(
            now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC)
        )
    assert fake not in fetched["reason"]

    store = SourceObservationStore(tmp_path / "source_observations.db")
    state_path = tmp_path / "dart_state.json"
    with patch.object(dart_monitor, "_api_key", return_value="configured"), \
         patch.object(dart_monitor, "_state_path", return_value=state_path), \
         patch.object(dart_monitor, "_toss_holding_codes", return_value={"005930"}), \
         patch.object(
             dart_monitor,
             "fetch_recent_disclosures",
             return_value={"ok": False, "reason": f"fetch_error:{fake}", "items": []},
         ):
        dart_monitor.run_dart_monitor(
            now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
            force=True,
            observation_store=store,
        )
    run = store.latest_collection_run(source="opendart_disclosures")
    assert run is not None
    assert fake not in run.error_type


def test_latest_sec_filing_preserves_newest_first_source_order(tmp_path):
    store = SourceObservationStore(tmp_path / "source_observations.db")
    ingested_at = datetime(2026, 7, 14, 12, 30, tzinfo=UTC)
    hits = [
        {
            "ticker": "MRVL",
            "form": "8-K",
            "filing_date": "2026-07-14",
            "accession": "0001835632-26-000124",
            "description": "newest",
        },
        {
            "ticker": "MRVL",
            "form": "8-K",
            "filing_date": "2026-07-14",
            "accession": "0001835632-26-000123",
            "description": "older",
        },
    ]
    record_sec_filing_observations(hits, store=store, ingested_at=ingested_at)

    latest = store.latest_as_of(
        source="sec_submissions", symbol="MRVL", decision_at=ingested_at
    )
    assert latest is not None
    assert latest.payload["description"] == "newest"
