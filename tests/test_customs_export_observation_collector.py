"""관세청 10일 수출의 append-only point-in-time 수집 계약."""

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3

import pytest

from core.market_data_fetch import CacheSource, FetchErrorType, FetchResult, FetchStatus
from core.source_observations_v2 import SourceObservationStoreV2

UTC = timezone.utc
STARTED = datetime(2026, 7, 16, 0, 0, 0, tzinfo=UTC)
COMPLETED = STARTED + timedelta(seconds=2)


def _period(
    *, year=2026, month=7, day=10, kind="day_10", total=1000, semiconductors=100
):
    return {
        "period_year": year,
        "period_month": month,
        "period_end_day": day,
        "period_kind": kind,
        "amounts_thousand_usd": {
            "total": total,
            "semiconductors": semiconductors,
            "steel_products": 90,
            "passenger_cars": 80,
            "petroleum_products": 70,
            "wireless_communication_devices": 60,
            "ships": 50,
            "auto_parts": 40,
            "computer_peripherals": 30,
            "precision_instruments": 20,
            "home_appliances": 10,
        },
    }


def _fixture_raw(items):
    return ("<fixture>" + json.dumps(items, sort_keys=True) + "</fixture>").encode()


def _result(
    items,
    *,
    started=STARTED,
    completed=COMPLETED,
    raw=None,
    start_yymm="202507",
    end_yymm="202607",
):
    raw = _fixture_raw(items) if raw is None else raw
    return FetchResult(
        status=FetchStatus.SUCCESS,
        provider="CUSTOMS",
        endpoint="/getPrlstMmUtPrviExpAcrs",
        tr_id=None,
        venue="KR",
        symbol="KR_EXPORTS",
        started_at_utc=started,
        completed_at_utc=completed,
        error_type=FetchErrorType.NONE,
        cache_source=CacheSource.NETWORK,
        fallback_used=False,
        value={
            "items": items,
            "total_count": len(items),
            "raw_xml_base64": base64.b64encode(raw).decode("ascii"),
            "raw_xml_sha256": hashlib.sha256(raw).hexdigest(),
            "raw_size_bytes": len(raw),
            "request_params": {"strtYymm": start_yymm, "endYymm": end_yymm},
            "http_status": 200,
            "content_type": "application/xml",
            "parser_contract_version": 1,
        },
        source_fetched_at_utc=completed,
    )


def _clock(*moments):
    values = iter(moments)
    return lambda: next(values)


def _assert_latest_run_statuses(store, *, status, error_type):
    for dataset in (
        "ten_day_product_export_requests",
        "ten_day_product_exports_raw",
        "ten_day_product_exports",
        "ten_day_export_workdays",
        "ten_day_export_industry_features",
    ):
        run = store.latest_collection_run("korea_customs", dataset)
        assert run is not None
        assert run.status == status
        assert run.error_type == error_type


def test_sensitive_run_id_is_rejected_before_fetch(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "sensitive-run-id.db")
    calls = []

    def must_not_fetch(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("fetcher must not be called")

    with pytest.raises(ValueError, match="customs_run_id_sensitive"):
        collect_customs_export_observations(
            "202607",
            "202607",
            store=store,
            run_id="serviceKey=must-not-persist",
            fetcher=must_not_fetch,
        )

    assert calls == []
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 0


def test_encoded_service_key_in_run_id_is_rejected_before_fetch(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "encoded-sensitive-run-id.db")
    calls = []

    def must_not_fetch(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("fetcher must not be called")

    with pytest.raises(ValueError, match="customs_run_id_sensitive"):
        collect_customs_export_observations(
            "202607",
            "202607",
            store=store,
            run_id="customs-synthetic%2B%2Fcredential",
            service_key="synthetic+/credential",
            fetcher=must_not_fetch,
        )

    assert calls == []


def test_collector_clock_sets_first_seen_instead_of_old_fetch_completion(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "collector-first-seen.db")
    invocation_started = STARTED
    received_at = STARTED + timedelta(seconds=10)
    old_fetch_started = STARTED - timedelta(days=1)
    old_fetch_completed = old_fetch_started + timedelta(seconds=2)

    summary = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-first-seen",
        fetcher=lambda *_args, **_kwargs: _result(
            [_period()],
            started=old_fetch_started,
            completed=old_fetch_completed,
        ),
        clock=_clock(invocation_started, received_at),
    )

    assert summary["status"] == "success"
    assert store.latest_as_of(
        decision_at=received_at - timedelta(microseconds=1),
        source="korea_customs",
        dataset="ten_day_product_exports",
        symbol="KR_EXPORTS",
        market="KR",
    ) is None
    observation = store.latest_as_of(
        decision_at=received_at,
        source="korea_customs",
        dataset="ten_day_product_exports",
        symbol="KR_EXPORTS",
        market="KR",
    )
    assert observation is not None
    assert observation.ingested_at == "2026-07-16T00:00:10.000000Z"
    run = store.latest_collection_run("korea_customs", "ten_day_product_exports")
    assert run is not None
    assert run.started_at == "2026-07-16T00:00:00.000000Z"
    assert run.completed_at == "2026-07-16T00:00:10.000000Z"


def test_future_fetch_timestamp_fails_closed_for_every_dataset(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "future-fetch.db")
    received_at = STARTED + timedelta(seconds=2)
    future_started = received_at + timedelta(seconds=1)
    future_completed = future_started + timedelta(seconds=1)

    summary = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-future-fetch",
        fetcher=lambda *_args, **_kwargs: _result(
            [_period(year=2025, month=7)],
            started=future_started,
            completed=future_completed,
        ),
        clock=_clock(STARTED, received_at),
    )

    assert summary["status"] == "failed"
    assert summary["error_type"] == "timestamp.future"
    _assert_latest_run_statuses(
        store, status="failed", error_type="timestamp.future"
    )


def test_collector_clock_regression_fails_closed_for_every_dataset(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "clock-regression.db")
    regressed = STARTED - timedelta(seconds=1)
    result = _result(
        [_period(year=2025, month=7)],
        started=STARTED - timedelta(seconds=3),
        completed=STARTED - timedelta(seconds=2),
    )

    summary = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-clock-regression",
        fetcher=lambda *_args, **_kwargs: result,
        clock=_clock(STARTED, regressed, STARTED),
    )

    assert summary["status"] == "failed"
    assert summary["error_type"] == "timestamp.clock_regression"
    _assert_latest_run_statuses(
        store, status="failed", error_type="timestamp.clock_regression"
    )


def test_lineage_failure_is_typed_and_atomic_for_every_dataset(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "lineage.db")
    wrong_lineage = replace(_result([_period()]), provider="NOT_CUSTOMS")

    summary = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-lineage",
        fetcher=lambda *_args, **_kwargs: wrong_lineage,
        clock=_clock(STARTED, COMPLETED + timedelta(seconds=1)),
    )

    assert summary["status"] == "failed"
    assert summary["error_type"] == "lineage.invalid"
    _assert_latest_run_statuses(store, status="failed", error_type="lineage.invalid")


@pytest.mark.parametrize(
    ("raw", "service_key"),
    [
        (b"<response><serviceKey>synthetic-value</serviceKey></response>", "other-key"),
        (b'<response service_key="synthetic-value"/>', "other-key"),
        (b"<response><data_go_key>synthetic-value</data_go_key></response>", "other-key"),
        (
            b"<response>synthetic-collector-credential-012345</response>",
            "synthetic-collector-credential-012345",
        ),
        (
            b"<response>synthetic%2B%2Fcredential</response>",
            "synthetic+/credential",
        ),
        (
            b"<response>synthetic&amp;credential</response>",
            "synthetic&credential",
        ),
        (
            (
                "<response>"
                + base64.b64encode(b"synthetic-base64-credential").decode("ascii")
                + "</response>"
            ).encode(),
            "synthetic-base64-credential",
        ),
        (
            (
                "<response>"
                + base64.urlsafe_b64encode(b"synthetic-urlsafe-credential+/")
                .decode("ascii")
                .rstrip("=")
                + "</response>"
            ).encode(),
            "synthetic-urlsafe-credential+/",
        ),
    ],
    ids=[
        "service-key-tag",
        "service-key-attribute",
        "data-go-alias",
        "echo",
        "url-encoded-echo",
        "html-escaped-echo",
        "base64-echo",
        "urlsafe-base64-echo",
    ],
)
def test_raw_xml_sensitive_alias_or_credential_echo_is_rejected(
    tmp_path, raw, service_key
):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "raw-sensitive.db")
    summary = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-raw-sensitive",
        service_key=service_key,
        fetcher=lambda *_args, **_kwargs: _result([_period()], raw=raw),
        clock=_clock(STARTED, COMPLETED + timedelta(seconds=1)),
    )

    assert summary["status"] == "failed"
    assert summary["error_type"] == "sensitive_raw"
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    _assert_latest_run_statuses(store, status="failed", error_type="sensitive_raw")


def test_collector_revalidates_xml_content_type_before_raw_persistence(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "raw-content-type.db")
    result = _result([_period()])
    assert isinstance(result.value, dict)
    value = {**result.value, "content_type": "text/plain; note=xml"}
    spoofed = replace(result, value=value)

    summary = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-content-type-spoof",
        fetcher=lambda *_args, **_kwargs: spoofed,
        clock=_clock(STARTED, COMPLETED + timedelta(seconds=1)),
    )

    assert summary["status"] == "failed"
    assert summary["error_type"] == "malformed"
    _assert_latest_run_statuses(store, status="failed", error_type="malformed")
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_collector_rejects_content_type_encoded_credential_before_lineage(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    service_key = "synthetic-content-type-collector-secret"
    encoded = base64.b64encode(service_key.encode()).decode("ascii")
    result = _result([_period()])
    assert isinstance(result.value, dict)
    value = {
        **result.value,
        "content_type": f"application/xml; note={encoded}",
    }
    store = SourceObservationStoreV2(tmp_path / "content-type-secret.db")

    summary = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-content-type-secret",
        service_key=service_key,
        fetcher=lambda *_args, **_kwargs: replace(result, value=value),
        clock=_clock(STARTED, COMPLETED + timedelta(seconds=1)),
    )

    assert summary["status"] == "failed"
    assert summary["error_type"] == "sensitive_raw"
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_collector_rejects_oversized_base64_before_decode(tmp_path, monkeypatch):
    from core import customs_export_observation_collector as collector
    from core.customs_export import MAX_CUSTOMS_RAW_BYTES

    raw = b"x" * (MAX_CUSTOMS_RAW_BYTES + 1)
    result = _result([_period()], raw=raw)
    assert isinstance(result.value, dict)
    forged = replace(
        result,
        value={**result.value, "raw_size_bytes": MAX_CUSTOMS_RAW_BYTES},
    )
    decode_calls = []

    def forbidden_decode(*_args, **_kwargs):
        decode_calls.append(True)
        raise AssertionError("oversized artifact must fail before Base64 decode")

    monkeypatch.setattr(collector, "_decode_raw_artifact", forbidden_decode)
    store = SourceObservationStoreV2(tmp_path / "oversized-raw.db")
    summary = collector.collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-oversized-raw",
        fetcher=lambda *_args, **_kwargs: forged,
        clock=_clock(STARTED, COMPLETED + timedelta(seconds=1)),
    )

    assert decode_calls == []
    assert summary["status"] == "failed"
    assert summary["error_type"] == "malformed"
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_same_raw_response_dedupes_artifact_across_run_ids(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "raw-dedupe.db")
    result = _result([_period()])
    first_received = COMPLETED + timedelta(seconds=1)
    second_started = STARTED + timedelta(minutes=1)
    second_received = second_started + timedelta(seconds=3)

    first = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-dedupe-1",
        fetcher=lambda *_args, **_kwargs: result,
        clock=_clock(STARTED, first_received),
    )
    second = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-dedupe-2",
        fetcher=lambda *_args, **_kwargs: result,
        clock=_clock(second_started, second_received),
    )

    assert first["status"] == "success"
    assert second["status"] == "success"
    with sqlite3.connect(store.db_path) as connection:
        raw_count = connection.execute(
            "SELECT COUNT(*) FROM observations WHERE dataset = ?",
            ("ten_day_product_exports_raw",),
        ).fetchone()[0]
        raw_run_count = connection.execute(
            "SELECT COUNT(*) FROM collection_runs WHERE dataset = ?",
            ("ten_day_product_exports_raw",),
        ).fetchone()[0]
    assert raw_count == 1
    assert raw_run_count == 2
    latest_raw_run = store.latest_collection_run(
        "korea_customs", "ten_day_product_exports_raw"
    )
    assert latest_raw_run is not None
    assert latest_raw_run.rows_inserted == 0
    assert latest_raw_run.rows_duplicate == 1


def test_same_raw_bytes_across_query_ranges_share_artifact_and_keep_two_links(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "raw-query-lineage.db")
    item = _period()
    raw = _fixture_raw([item])
    second_started = STARTED + timedelta(minutes=1)
    first_result = _result(
        [item], raw=raw, start_yymm="202507", end_yymm="202607"
    )
    second_result = _result(
        [item],
        raw=raw,
        started=second_started,
        completed=second_started + timedelta(seconds=1),
        start_yymm="202607",
        end_yymm="202607",
    )

    first = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-range-wide",
        fetcher=lambda *_args, **_kwargs: first_result,
        clock=_clock(STARTED, COMPLETED),
    )
    second = collect_customs_export_observations(
        "202607",
        "202607",
        store=store,
        run_id="customs-range-narrow",
        fetcher=lambda *_args, **_kwargs: second_result,
        clock=_clock(second_started, second_started + timedelta(seconds=2)),
    )

    assert first["status"] == "success"
    assert second["status"] == "success"
    with sqlite3.connect(store.db_path) as connection:
        counts = dict(
            connection.execute(
                "SELECT dataset, COUNT(*) FROM observations GROUP BY dataset"
            ).fetchall()
        )
        lineage_payloads = [
            json.loads(row[0])
            for row in connection.execute(
                """
                SELECT payload_json FROM observations
                WHERE dataset = 'ten_day_product_export_requests'
                ORDER BY id
                """
            ).fetchall()
        ]
    assert counts["ten_day_product_exports_raw"] == 1
    assert counts["ten_day_product_export_requests"] == 2
    assert counts["ten_day_product_exports"] == 1
    assert counts["ten_day_export_industry_features"] == 10
    assert [payload["request_params"] for payload in lineage_payloads] == [
        {"strtYymm": "202507", "endYymm": "202607"},
        {"strtYymm": "202607", "endYymm": "202607"},
    ]
    assert len({payload["raw_artifact_sha256"] for payload in lineage_payloads}) == 1


def test_success_atomically_persists_period_and_collection_run(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "source_observations_v2.db")
    summary = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-run-1",
        fetcher=lambda *_args, **_kwargs: _result([_period()]),
        clock=_clock(STARTED, COMPLETED),
    )

    assert summary == {
        "source": "korea_customs",
        "dataset": "ten_day_product_exports",
        "status": "success",
        "rows_seen": 1,
        "rows_inserted": 1,
        "rows_duplicate": 0,
        "rows_skipped": 0,
        "rows_invalid": 0,
        "error_type": "",
        "workday_status": "skipped",
        "workday_rows_seen": 0,
        "workday_rows_inserted": 0,
        "workday_rows_duplicate": 0,
        "workday_error_type": "",
        "feature_rows_seen": 10,
        "feature_rows_inserted": 10,
        "feature_rows_duplicate": 0,
        "feature_rows_ready": 0,
    }
    observation = store.latest_as_of(
        decision_at=COMPLETED + timedelta(seconds=1),
        source="korea_customs",
        dataset="ten_day_product_exports",
        symbol="KR_EXPORTS",
        market="KR",
    )
    assert observation is not None
    assert observation.source_record_id == "20260710"
    assert observation.source_as_of == "2026-07-10T14:59:59.999999Z"
    assert observation.ingested_at == "2026-07-16T00:00:02.000000Z"
    assert observation.currency_or_unit == "USD"
    assert observation.payload["amounts_thousand_usd"]["semiconductors"] == 100
    assert observation.payload["units"] == {"amounts_thousand_usd": "thousand_USD"}
    assert observation.payload["provisional"] is True
    assert observation.payload["scheduled_release_date_kst"] == "2026-07-11"
    assert observation.payload["next_cutoff_release_date_kst"] == "2026-07-21"
    assert observation.payload["source_published_at_utc"] is None
    assert observation.payload["publication_precision"] == "date_only"
    assert observation.payload["available_at_field"] == "observation.ingested_at"
    assert "nominal_publication_at_utc" not in observation.payload
    assert "next_cutoff_publication_at_utc" not in observation.payload
    expected_raw = _fixture_raw([_period()])
    assert observation.payload["snapshot_group_id"] == hashlib.sha256(
        expected_raw
    ).hexdigest()
    raw_observation = store.latest_as_of(
        decision_at=COMPLETED + timedelta(seconds=1),
        source="korea_customs",
        dataset="ten_day_product_exports_raw",
        symbol="KR_EXPORTS_RAW",
        market="KR",
    )
    assert raw_observation is not None
    assert base64.b64decode(raw_observation.payload["raw_xml_base64"]) == expected_raw
    assert raw_observation.payload["artifact_identity"] == "content_sha256"
    assert "request_params" not in raw_observation.payload
    assert "content_type" not in raw_observation.payload
    assert "serviceKey" not in json.dumps(raw_observation.payload)
    request_observation = store.latest_as_of(
        decision_at=COMPLETED + timedelta(seconds=1),
        source="korea_customs",
        dataset="ten_day_product_export_requests",
        symbol="KR_EXPORT_REQUEST",
        market="KR",
    )
    assert request_observation is not None
    assert request_observation.source_record_id == "customs-run-1"
    assert request_observation.payload["request_params"] == {
        "strtYymm": "202507",
        "endYymm": "202607",
    }
    assert request_observation.payload["content_type"] == "application/xml"
    assert request_observation.payload["collection_mode"] == "research_backfill"
    feature_observation = store.latest_as_of(
        decision_at=COMPLETED + timedelta(seconds=1),
        source="korea_customs",
        dataset="ten_day_export_industry_features",
        symbol="KCS:SEMI",
        market="KR",
    )
    assert feature_observation is not None
    assert feature_observation.source_record_id == "20260710:semiconductors"
    assert feature_observation.payload["feature_ready"] is False
    assert feature_observation.payload["source_snapshot_ids"] == [
        observation.snapshot_id
    ]
    assert feature_observation.payload["eligible_for_production_score"] is False
    run = store.latest_collection_run("korea_customs", "ten_day_product_exports")
    assert run is not None
    assert run.run_id == "customs-run-1"
    assert run.status == "success"


def test_success_persistence_failure_rolls_back_raw_and_records_all_failed(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    class FailingStore(SourceObservationStoreV2):
        def __init__(self, path):
            super().__init__(path)
            self.fail_once = True

        def append(self, **kwargs):
            if self.fail_once and kwargs.get("dataset") == "ten_day_product_exports":
                self.fail_once = False
                raise sqlite3.OperationalError("synthetic persistence failure")
            return super().append(**kwargs)

    store = FailingStore(tmp_path / "success-persistence-failure.db")
    summary = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-success-persistence-failure",
        fetcher=lambda *_args, **_kwargs: _result([_period()]),
        clock=_clock(STARTED, COMPLETED),
    )

    assert summary["status"] == "failed"
    assert summary["error_type"] == "persistence.operationalerror"
    _assert_latest_run_statuses(
        store, status="failed", error_type="persistence.operationalerror"
    )
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_empty_persistence_failure_rolls_back_raw_and_records_all_failed(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    class FailingStore(SourceObservationStoreV2):
        def __init__(self, path):
            super().__init__(path)
            self.fail_once = True

        def record_collection_run(self, **kwargs):
            if self.fail_once and kwargs.get("dataset") == "ten_day_product_exports":
                self.fail_once = False
                raise sqlite3.OperationalError("synthetic empty persistence failure")
            return super().record_collection_run(**kwargs)

    store = FailingStore(tmp_path / "empty-persistence-failure.db")
    empty = replace(
        _result([], start_yymm="202607", end_yymm="202607"),
        status=FetchStatus.EMPTY,
    )
    summary = collect_customs_export_observations(
        "202607",
        "202607",
        store=store,
        run_id="customs-empty-persistence-failure",
        fetcher=lambda *_args, **_kwargs: empty,
        clock=_clock(STARTED, COMPLETED),
    )

    assert summary["status"] == "failed"
    assert summary["error_type"] == "persistence.operationalerror"
    _assert_latest_run_statuses(
        store, status="failed", error_type="persistence.operationalerror"
    )
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_typed_skip_updates_every_dataset_without_observations(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "skipped.db")
    skipped = FetchResult(
        status=FetchStatus.SKIPPED,
        provider="CUSTOMS",
        endpoint="/getPrlstMmUtPrviExpAcrs",
        tr_id=None,
        venue="KR",
        symbol="KR_EXPORTS",
        started_at_utc=STARTED,
        completed_at_utc=COMPLETED,
        error_type=FetchErrorType.NOT_CONFIGURED,
        cache_source=CacheSource.NONE,
        fallback_used=False,
        value=None,
    )

    summary = collect_customs_export_observations(
        "202607",
        "202607",
        store=store,
        run_id="customs-not-configured",
        fetcher=lambda *_args, **_kwargs: skipped,
        clock=_clock(STARTED, COMPLETED),
    )

    assert summary["status"] == "skipped"
    assert summary["error_type"] == "not_configured"
    _assert_latest_run_statuses(
        store, status="skipped", error_type="not_configured"
    )
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_typed_fetch_failure_records_failed_run_not_clean_zero(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "failed.db")
    failed = FetchResult(
        status=FetchStatus.FAILED,
        provider="CUSTOMS",
        endpoint="/getPrlstMmUtPrviExpAcrs",
        tr_id=None,
        venue="KR",
        symbol="KR_EXPORTS",
        started_at_utc=STARTED,
        completed_at_utc=COMPLETED,
        error_type=FetchErrorType.NETWORK,
        cache_source=CacheSource.NETWORK,
        fallback_used=False,
        value=None,
    )

    summary = collect_customs_export_observations(
        "202607",
        "202607",
        store=store,
        run_id="customs-network-failure",
        fetcher=lambda *_args, **_kwargs: failed,
        clock=_clock(STARTED, COMPLETED + timedelta(seconds=1)),
    )

    assert summary == {
        "source": "korea_customs",
        "dataset": "ten_day_product_exports",
        "status": "failed",
        "rows_seen": 0,
        "rows_inserted": 0,
        "rows_duplicate": 0,
        "rows_skipped": 0,
        "rows_invalid": 0,
        "error_type": "network",
    }
    run = store.latest_collection_run("korea_customs", "ten_day_product_exports")
    assert run is not None
    assert run.status == "failed"
    assert run.error_type == "network"
    _assert_latest_run_statuses(store, status="failed", error_type="network")


def test_malformed_success_discards_valid_prefix_and_records_failed_run(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "malformed.db")
    malformed = _period()
    malformed["period_year"] = "2026"
    summary = collect_customs_export_observations(
        "202607",
        "202607",
        store=store,
        run_id="customs-malformed",
        fetcher=lambda *_args, **_kwargs: _result([_period(), malformed]),
        clock=_clock(STARTED, COMPLETED + timedelta(seconds=1)),
    )

    assert summary["status"] == "failed"
    assert summary["rows_seen"] == 2
    assert summary["rows_inserted"] == 0
    assert summary["rows_invalid"] == 2
    assert summary["error_type"] == "malformed"
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    run = store.latest_collection_run("korea_customs", "ten_day_product_exports")
    assert run is not None
    assert run.status == "failed"
    _assert_latest_run_statuses(store, status="failed", error_type="malformed")


def test_revision_is_new_vintage_and_never_visible_before_second_ingestion(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "revision.db")
    collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-v1",
        fetcher=lambda *_args, **_kwargs: _result([_period(semiconductors=100)]),
        clock=_clock(STARTED, COMPLETED),
    )
    second_completed = COMPLETED + timedelta(days=1)
    collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-v2",
        fetcher=lambda *_args, **_kwargs: _result(
            [_period(semiconductors=101)],
            started=second_completed - timedelta(seconds=2),
            completed=second_completed,
        ),
        clock=_clock(second_completed - timedelta(seconds=2), second_completed),
    )

    first_view = store.latest_as_of(
        decision_at=COMPLETED + timedelta(seconds=1),
        source="korea_customs",
        dataset="ten_day_product_exports",
        symbol="KR_EXPORTS",
        market="KR",
    )
    second_view = store.latest_as_of(
        decision_at=second_completed + timedelta(seconds=1),
        source="korea_customs",
        dataset="ten_day_product_exports",
        symbol="KR_EXPORTS",
        market="KR",
    )
    assert first_view is not None and second_view is not None
    assert first_view.payload["amounts_thousand_usd"]["semiconductors"] == 100
    assert second_view.payload["amounts_thousand_usd"]["semiconductors"] == 101
    assert first_view.snapshot_id != second_view.snapshot_id


def test_fetcher_exception_is_sanitized_and_recorded(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "fetch-exception.db")
    moments = iter((STARTED, COMPLETED))

    def fetcher(*_args, **_kwargs):
        raise RuntimeError("serviceKey=must-not-appear")

    summary = collect_customs_export_observations(
        "202607",
        "202607",
        store=store,
        run_id="customs-fetch-exception",
        fetcher=fetcher,
        clock=lambda: next(moments),
    )

    assert summary["status"] == "failed"
    assert summary["error_type"] == "fetch_exception.runtimeerror"
    assert "must-not-appear" not in json.dumps(summary)
    run = store.latest_collection_run("korea_customs", "ten_day_product_exports")
    assert run is not None
    assert run.error_type == "fetch_exception.runtimeerror"
    _assert_latest_run_statuses(
        store, status="failed", error_type="fetch_exception.runtimeerror"
    )


def test_persistence_failure_rolls_back_raw_normalized_and_features(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    inner = SourceObservationStoreV2(tmp_path / "persistence.db")

    class FailSecondAppend:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.db_path = wrapped.db_path
            self.calls = 0

        def atomic_write(self, callback):
            return self.wrapped.atomic_write(callback)

        def append(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise OSError("credential-like persistence detail")
            return self.wrapped.append(**kwargs)

        def record_collection_run(self, **kwargs):
            return self.wrapped.record_collection_run(**kwargs)

    store = FailSecondAppend(inner)
    summary = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-persistence-failure",
        fetcher=lambda *_args, **_kwargs: _result([_period()]),
        clock=_clock(
            STARTED,
            COMPLETED + timedelta(seconds=1),
            COMPLETED + timedelta(seconds=2),
        ),
    )

    assert summary["status"] == "failed"
    assert summary["error_type"] == "persistence.oserror"
    with sqlite3.connect(inner.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    run = inner.latest_collection_run("korea_customs", "ten_day_product_exports")
    assert run is not None
    assert run.status == "failed"
    _assert_latest_run_statuses(
        inner, status="failed", error_type="persistence.oserror"
    )


def test_clean_empty_preserves_raw_artifact_and_is_not_failed(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "empty.db")
    empty = replace(_result([]), status=FetchStatus.EMPTY)

    summary = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-empty",
        fetcher=lambda *_args, **_kwargs: empty,
        clock=_clock(STARTED, COMPLETED + timedelta(seconds=1)),
    )

    assert summary["status"] == "skipped"
    assert summary["error_type"] == ""
    raw = store.latest_as_of(
        decision_at=COMPLETED + timedelta(seconds=1),
        source="korea_customs",
        dataset="ten_day_product_exports_raw",
        symbol="KR_EXPORTS_RAW",
        market="KR",
    )
    assert raw is not None


def test_empty_persistence_failure_rolls_back_and_records_typed_runs(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    inner = SourceObservationStoreV2(tmp_path / "empty-persistence.db")

    class FailFirstAppend:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.failed = False

        def atomic_write(self, callback):
            return self.wrapped.atomic_write(callback)

        def append(self, **kwargs):
            if not self.failed:
                self.failed = True
                raise OSError("synthetic empty write failure")
            return self.wrapped.append(**kwargs)

        def record_collection_run(self, **kwargs):
            return self.wrapped.record_collection_run(**kwargs)

    empty = replace(_result([]), status=FetchStatus.EMPTY)
    summary = collect_customs_export_observations(
        "202507",
        "202607",
        store=FailFirstAppend(inner),
        run_id="customs-empty-persistence",
        fetcher=lambda *_args, **_kwargs: empty,
        clock=_clock(STARTED, COMPLETED + timedelta(seconds=1)),
    )

    assert summary["status"] == "failed"
    assert summary["error_type"] == "persistence.oserror"
    with sqlite3.connect(inner.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    _assert_latest_run_statuses(
        inner, status="failed", error_type="persistence.oserror"
    )


@pytest.mark.parametrize(
    ("terminal_status", "terminal_error", "terminal_cache"),
    (
        (FetchStatus.FAILED, FetchErrorType.NETWORK, CacheSource.NETWORK),
        (FetchStatus.SKIPPED, FetchErrorType.NOT_CONFIGURED, CacheSource.NONE),
    ),
)
def test_terminal_run_ledger_transient_failure_retries_as_persistence_failure(
    tmp_path, terminal_status, terminal_error, terminal_cache
):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    class FailOneTerminalLedgerWrite(SourceObservationStoreV2):
        fail_enabled = False
        failed_once = False

        def record_collection_run(self, **kwargs):
            if (
                self.fail_enabled
                and not self.failed_once
                and kwargs.get("dataset") == "ten_day_product_exports"
            ):
                self.failed_once = True
                raise sqlite3.OperationalError("synthetic terminal ledger failure")
            return super().record_collection_run(**kwargs)

    store = FailOneTerminalLedgerWrite(tmp_path / f"terminal-{terminal_status.value}.db")
    seed = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id=f"customs-seed-{terminal_status.value}",
        fetcher=lambda *_args, **_kwargs: _result([_period()]),
        clock=_clock(STARTED, COMPLETED),
    )
    assert seed["status"] == "success"

    store.fail_enabled = True
    later_started = STARTED + timedelta(hours=1)
    later_completed = later_started + timedelta(seconds=1)
    terminal_result = replace(
        _result(
            [_period()],
            started=later_started,
            completed=later_completed,
        ),
        status=terminal_status,
        error_type=terminal_error,
        cache_source=terminal_cache,
        value=None,
        source_fetched_at_utc=None,
    )
    summary = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id=f"customs-terminal-{terminal_status.value}",
        fetcher=lambda *_args, **_kwargs: terminal_result,
        clock=_clock(later_started, later_completed),
    )

    assert summary["status"] == "failed"
    assert summary["error_type"] == "persistence.operationalerror"
    _assert_latest_run_statuses(
        store, status="failed", error_type="persistence.operationalerror"
    )
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 10


def test_terminal_run_ledger_permanent_failure_is_explicitly_fatal(tmp_path):
    from core.customs_export_observation_collector import (
        CollectionJournalUnavailable,
        collect_customs_export_observations,
    )

    class PermanentlyUnavailableJournal(SourceObservationStoreV2):
        fail_enabled = False

        def atomic_write(self, callback):
            if self.fail_enabled:
                raise sqlite3.OperationalError("synthetic permanent journal failure")
            return super().atomic_write(callback)

    store = PermanentlyUnavailableJournal(tmp_path / "terminal-journal-unavailable.db")
    seed = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-journal-seed",
        fetcher=lambda *_args, **_kwargs: _result([_period()]),
        clock=_clock(STARTED, COMPLETED),
    )
    assert seed["status"] == "success"

    store.fail_enabled = True
    later_started = STARTED + timedelta(hours=1)
    terminal_result = replace(
        _result(
            [_period()],
            started=later_started,
            completed=later_started + timedelta(seconds=1),
        ),
        status=FetchStatus.FAILED,
        error_type=FetchErrorType.NETWORK,
        value=None,
        source_fetched_at_utc=None,
    )
    with pytest.raises(
        CollectionJournalUnavailable,
        match="customs_collection_journal_unavailable",
    ) as exc_info:
        collect_customs_export_observations(
            "202507",
            "202607",
            store=store,
            run_id="customs-journal-fatal",
            fetcher=lambda *_args, **_kwargs: terminal_result,
            clock=_clock(later_started, later_started + timedelta(seconds=2)),
        )

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    latest = store.latest_collection_run(
        "korea_customs", "ten_day_product_exports"
    )
    assert latest is not None
    assert latest.run_id == "customs-journal-seed"


def test_collector_rejects_typed_row_outside_requested_period(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "out-of-range-row.db")
    result = _result(
        [_period(year=2025, month=7, day=10)],
        start_yymm="202607",
        end_yymm="202607",
    )

    summary = collect_customs_export_observations(
        "202607",
        "202607",
        store=store,
        run_id="customs-out-of-range-row",
        fetcher=lambda *_args, **_kwargs: result,
        clock=_clock(STARTED, COMPLETED),
    )

    assert summary["status"] == "failed"
    assert summary["error_type"] == "malformed"
    _assert_latest_run_statuses(store, status="failed", error_type="malformed")
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_cross_run_clock_regression_blocks_revision_and_exposes_latest_failure(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "cross-run-clock-regression.db")
    first_invocation = STARTED
    first_received = STARTED + timedelta(seconds=10)
    first_result = _result(
        [_period(semiconductors=100)],
        started=STARTED + timedelta(seconds=1),
        completed=STARTED + timedelta(seconds=2),
    )
    first = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-clock-v1",
        fetcher=lambda *_args, **_kwargs: first_result,
        clock=_clock(first_invocation, first_received),
    )
    assert first["status"] == "success"

    regressed_invocation = STARTED - timedelta(hours=1)
    regressed_received = regressed_invocation + timedelta(seconds=2)
    regressed_result = _result(
        [_period(semiconductors=101)],
        started=regressed_invocation + timedelta(milliseconds=100),
        completed=regressed_invocation + timedelta(seconds=1),
    )
    second = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-clock-v2",
        fetcher=lambda *_args, **_kwargs: regressed_result,
        clock=_clock(regressed_invocation, regressed_received),
    )

    assert second["status"] == "failed"
    assert second["error_type"] == "timestamp.clock_regression"
    _assert_latest_run_statuses(
        store, status="failed", error_type="timestamp.clock_regression"
    )
    latest = store.latest_as_of(
        decision_at=first_received + timedelta(seconds=1),
        source="korea_customs",
        dataset="ten_day_product_exports",
        symbol="KR_EXPORTS",
        market="KR",
    )
    assert latest is not None
    assert latest.payload["amounts_thousand_usd"]["semiconductors"] == 100
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM observations WHERE dataset = ?",
            ("ten_day_product_exports",),
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("collection_mode", "expected_vintage"),
    (
        ("scheduled_live", "realtime_as_observed"),
        ("research_backfill", "research_backfill_current_vintage"),
        ("manual_replay", "research_backfill_current_vintage"),
    ),
)
def test_collection_mode_controls_current_window_vintage(
    tmp_path, collection_mode, expected_vintage
):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / f"mode-{collection_mode}.db")
    result = _result(
        [_period(year=2026, month=7, day=10)],
        started=STARTED,
        completed=STARTED + timedelta(seconds=1),
        start_yymm="202607",
        end_yymm="202607",
    )
    summary = collect_customs_export_observations(
        "202607",
        "202607",
        store=store,
        run_id=f"customs-mode-{collection_mode}",
        fetcher=lambda *_args, **_kwargs: result,
        clock=_clock(STARTED, COMPLETED),
        collection_mode=collection_mode,
    )

    assert summary["status"] == "success"
    observation = store.latest_as_of(
        decision_at=COMPLETED,
        source="korea_customs",
        dataset="ten_day_product_exports",
        symbol="KR_EXPORTS",
        market="KR",
    )
    assert observation is not None
    assert observation.payload["collection_mode"] == collection_mode
    assert observation.payload["vintage_policy"] == expected_vintage


def test_explicit_historical_query_is_never_marked_realtime(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "explicit-backfill.db")
    invocation = datetime(2026, 7, 20, 3, 0, tzinfo=UTC)
    received = invocation + timedelta(seconds=2)
    result = _result(
        [_period(year=2026, month=6, day=30, kind="month_end")],
        start_yymm="202606",
        end_yymm="202606",
    )

    summary = collect_customs_export_observations(
        "202606",
        "202606",
        store=store,
        run_id="customs-explicit-backfill",
        fetcher=lambda *_args, **_kwargs: result,
        clock=_clock(invocation, received),
        collection_mode="research_backfill",
    )

    assert summary["status"] == "success"
    observation = store.latest_as_of(
        decision_at=received,
        source="korea_customs",
        dataset="ten_day_product_exports",
        symbol="KR_EXPORTS",
        market="KR",
    )
    assert observation is not None
    assert observation.payload["vintage_policy"] == "research_backfill_current_vintage"
    assert observation.payload["collection_mode"] == "research_backfill"
    assert observation.payload["historical_backtest_eligible"] is False
    assert observation.payload["eligible_for_production_score"] is False


def test_historical_rows_are_revised_references_not_realtime_vintages(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )

    store = SourceObservationStoreV2(tmp_path / "vintage-policy.db")
    collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-vintage-policy",
        fetcher=lambda *_args, **_kwargs: _result(
            [
                _period(year=2025, month=7, day=10, total=900, semiconductors=90),
                _period(year=2026, month=7, day=10, total=1000, semiconductors=100),
            ]
        ),
        collection_mode="scheduled_live",
    )

    with sqlite3.connect(store.db_path) as connection:
        rows = connection.execute(
            """
            SELECT source_record_id, payload_json
            FROM observations
            WHERE source = 'korea_customs'
              AND dataset = 'ten_day_product_exports'
            ORDER BY source_record_id
            """
        ).fetchall()
    payloads = {record_id: json.loads(payload_json) for record_id, payload_json in rows}
    assert payloads["20250710"]["vintage_policy"] == (
        "reference_latest_revised_as_observed"
    )
    assert payloads["20260710"]["vintage_policy"] == "realtime_as_observed"
    assert payloads["20250710"]["historical_backtest_eligible"] is False
    assert payloads["20260710"]["eligible_for_production_score"] is False


def _workday_payload(year, tenths, *, available_at):
    return {
        "source_record_id": f"{year}0710:workdays",
        "period_year": year,
        "period_month": 7,
        "period_end_day": 10,
        "period_kind": "day_10",
        "workdays_mtd_tenths": tenths,
        "calendar_domain": "KCS_REPORTED_OPERATING_DAYS",
        "method_version": "korea-kr-kcs-press-release-v1",
        "source_document_id": "156800001",
        "source_uri": (
            "https://m.korea.kr/briefing/pressReleaseView.do?newsId=156800001"
        ),
        "source_title": "’26년 7월 1일 ~ 7월 10일 수출입 현황",
        "source_agency": "관세청",
        "detail_header_title": "’26년 7월 1일 ~ 7월 10일 수출입 현황",
        "detail_header_release_date_kst": "2026-07-11",
        "detail_header_verified": True,
        "source_document_sha256": "a" * 64,
        "scheduled_release_date_kst": "2026-07-11",
        "source_published_at_utc": None,
        "publication_precision": "date_only",
        "first_seen_at_utc": available_at,
        "available_at_utc": available_at,
        "revision_policy": "append_only_content_hash",
        "supersedes_snapshot_id": None,
        "shadow_only": True,
        "eligible_for_production_score": False,
    }


def test_workday_success_is_appended_before_ready_feature(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )
    from core.customs_export_workdays import WorkdayFetchResult

    amount_received = COMPLETED
    workday_started = amount_received + timedelta(seconds=1)
    workday_completed = amount_received + timedelta(seconds=2)
    batch_completed = amount_received + timedelta(seconds=3)
    available = workday_completed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    workday_result = WorkdayFetchResult(
        status="success",
        error_type="none",
        rows=(
            _workday_payload(2025, 70, available_at=available),
            _workday_payload(2026, 80, available_at=available),
        ),
        started_at_utc=workday_started,
        completed_at_utc=workday_completed,
    )
    store = SourceObservationStoreV2(tmp_path / "workday-success.db")
    seen_periods = []

    def workday_fetcher(periods):
        seen_periods.extend(periods)
        return workday_result

    summary = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-workday-success",
        fetcher=lambda *_args, **_kwargs: _result(
            [
                _period(year=2025, month=7, day=10, total=900, semiconductors=90),
                _period(year=2026, month=7, day=10, total=1000, semiconductors=100),
            ]
        ),
        workday_fetcher=workday_fetcher,
        clock=_clock(STARTED, amount_received, batch_completed),
    )

    assert len(seen_periods) == 2
    assert summary["status"] == "success"
    assert summary["workday_status"] == "success"
    assert summary["workday_rows_seen"] == 2
    assert summary["workday_rows_inserted"] == 2
    assert summary["feature_rows_ready"] == 10
    workday = store.latest_as_of(
        decision_at=batch_completed,
        source="korea_customs",
        dataset="ten_day_export_workdays",
        symbol="KR_EXPORTS",
        market="KR",
    )
    assert workday is not None
    assert workday.ingested_at == available
    assert workday.payload["source_published_at_utc"] is None
    assert workday.payload["publication_precision"] == "date_only"
    assert workday.payload["available_at_field"] == "observation.ingested_at"
    assert "available_at_utc" not in workday.payload
    assert "first_seen_at_utc" not in workday.payload
    feature = store.latest_as_of(
        decision_at=batch_completed,
        source="korea_customs",
        dataset="ten_day_export_industry_features",
        symbol="KCS:SEMI",
        market="KR",
    )
    assert feature is not None
    assert feature.ingested_at == batch_completed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert feature.payload["workday_feature_ready"] is True
    assert feature.payload["feature_ready"] is True
    assert feature.payload["eligible_for_production_score"] is False
    run = store.latest_collection_run("korea_customs", "ten_day_export_workdays")
    assert run is not None
    assert run.status == "success"


def test_workday_failure_keeps_amounts_but_returns_partial(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )
    from core.customs_export_workdays import WorkdayFetchResult

    amount_received = COMPLETED
    workday_started = amount_received + timedelta(seconds=1)
    workday_completed = amount_received + timedelta(seconds=2)
    batch_completed = amount_received + timedelta(seconds=3)
    store = SourceObservationStoreV2(tmp_path / "workday-failure.db")
    result = WorkdayFetchResult(
        status="failed",
        error_type="network",
        rows=(),
        started_at_utc=workday_started,
        completed_at_utc=workday_completed,
    )

    summary = collect_customs_export_observations(
        "202607",
        "202607",
        store=store,
        run_id="customs-workday-failure",
        fetcher=lambda *_args, **_kwargs: _result(
            [_period()],
            start_yymm="202607",
        ),
        workday_fetcher=lambda _periods: result,
        clock=_clock(STARTED, amount_received, batch_completed),
    )

    assert summary["status"] == "partial"
    assert summary["error_type"] == "workday.network"
    assert summary["rows_inserted"] == 1
    assert summary["workday_status"] == "failed"
    assert store.latest_as_of(
        decision_at=batch_completed,
        source="korea_customs",
        dataset="ten_day_product_exports",
        symbol="KR_EXPORTS",
        market="KR",
    ) is not None
    feature = store.latest_as_of(
        decision_at=batch_completed,
        source="korea_customs",
        dataset="ten_day_export_industry_features",
        symbol="KCS:SEMI",
        market="KR",
    )
    assert feature is not None
    assert feature.payload["workday_feature_ready"] is False
    run = store.latest_collection_run("korea_customs", "ten_day_export_workdays")
    assert run is not None
    assert run.status == "failed"
    assert run.error_type == "network"


def test_collector_rejects_forged_detail_metadata_before_storage(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )
    from core.customs_export_workdays import WorkdayFetchResult

    amount_received = COMPLETED
    workday_started = amount_received + timedelta(seconds=1)
    workday_completed = amount_received + timedelta(seconds=2)
    batch_completed = amount_received + timedelta(seconds=3)
    available = workday_completed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    forged = _workday_payload(2026, 80, available_at=available)
    forged["source_agency"] = "한국거래소"
    result = WorkdayFetchResult(
        status="success",
        error_type="none",
        rows=(forged,),
        started_at_utc=workday_started,
        completed_at_utc=workday_completed,
    )
    store = SourceObservationStoreV2(tmp_path / "workday-forged-detail.db")

    summary = collect_customs_export_observations(
        "202607",
        "202607",
        store=store,
        run_id="customs-workday-forged-detail",
        fetcher=lambda *_args, **_kwargs: _result(
            [_period()],
            start_yymm="202607",
        ),
        workday_fetcher=lambda _periods: result,
        clock=_clock(STARTED, amount_received, batch_completed),
    )

    assert summary["status"] == "partial"
    assert summary["error_type"] == "workday.lineage.invalid"
    assert summary["workday_status"] == "failed"
    assert summary["workday_rows_inserted"] == 0
    assert summary["feature_rows_ready"] == 0
    assert store.latest_as_of(
        decision_at=batch_completed,
        source="korea_customs",
        dataset="ten_day_export_workdays",
        symbol="KR_EXPORTS",
        market="KR",
    ) is None


def test_workday_feature_error_type_never_trusts_non_valueerror_text():
    from core.customs_export_observation_collector import _workday_feature_error_type

    exc = TypeError("customs_workday_title_variant_invalid")

    assert _workday_feature_error_type(exc) == "lineage.invalid"


def test_collector_preserves_specific_workday_feature_error_type(tmp_path):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )
    from core.customs_export_workdays import WorkdayFetchResult

    amount_received = COMPLETED
    workday_started = amount_received + timedelta(seconds=1)
    workday_completed = amount_received + timedelta(seconds=2)
    batch_completed = amount_received + timedelta(seconds=3)
    available = workday_completed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    rows = [
        _workday_payload(2025, 70, available_at=available),
        _workday_payload(2026, 80, available_at=available),
    ]
    for row in rows:
        row["source_title"] += " [잠정치]x"
        row["detail_header_title"] = row["source_title"]
    result = WorkdayFetchResult(
        status="success",
        error_type="none",
        rows=tuple(rows),
        started_at_utc=workday_started,
        completed_at_utc=workday_completed,
    )
    store = SourceObservationStoreV2(tmp_path / "workday-specific-lineage.db")

    summary = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id="customs-workday-specific-lineage",
        fetcher=lambda *_args, **_kwargs: _result(
            [
                _period(year=2025, month=7, day=10, total=900, semiconductors=90),
                _period(year=2026, month=7, day=10, total=1000, semiconductors=100),
            ],
        ),
        workday_fetcher=lambda _periods: result,
        clock=_clock(STARTED, amount_received, batch_completed),
    )

    assert summary["status"] == "partial"
    assert summary["error_type"] == "workday.title_variant_invalid"
    assert summary["workday_error_type"] == "title_variant_invalid"
    assert summary["workday_rows_inserted"] == 0
    assert summary["feature_rows_ready"] == 0
    assert store.latest_as_of(
        decision_at=batch_completed,
        source="korea_customs",
        dataset="ten_day_export_workdays",
        symbol="KR_EXPORTS",
        market="KR",
    ) is None
    run = store.latest_collection_run("korea_customs", "ten_day_export_workdays")
    assert run is not None
    assert run.error_type == "title_variant_invalid"


def _collect_workday_revision(store, *, run_id, offset_hours, sha_char="a", current=80):
    from core.customs_export_observation_collector import (
        collect_customs_export_observations,
    )
    from core.customs_export_workdays import WorkdayFetchResult

    amount_started = STARTED + timedelta(hours=offset_hours)
    amount_completed = amount_started + timedelta(seconds=2)
    workday_started = amount_completed + timedelta(seconds=1)
    workday_completed = amount_completed + timedelta(seconds=2)
    batch_completed = amount_completed + timedelta(seconds=3)
    available = workday_completed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    rows = [
        _workday_payload(2025, 70, available_at=available),
        _workday_payload(2026, current, available_at=available),
    ]
    for row in rows:
        row["source_document_sha256"] = sha_char * 64
    result = WorkdayFetchResult(
        status="success",
        error_type="none",
        rows=tuple(rows),
        started_at_utc=workday_started,
        completed_at_utc=workday_completed,
    )
    summary = collect_customs_export_observations(
        "202507",
        "202607",
        store=store,
        run_id=run_id,
        fetcher=lambda *_args, **_kwargs: _result(
            [
                _period(year=2025, month=7, day=10, total=900, semiconductors=90),
                _period(year=2026, month=7, day=10, total=1000, semiconductors=100),
            ],
            started=amount_started,
            completed=amount_completed,
        ),
        workday_fetcher=lambda _periods: result,
        clock=_clock(amount_started, amount_completed, batch_completed),
    )
    return summary


def test_workday_rerun_converges_and_revision_supersedes_once(tmp_path):
    store = SourceObservationStoreV2(tmp_path / "workday-revision.db")

    first = _collect_workday_revision(
        store,
        run_id="customs-workday-revision-1",
        offset_hours=0,
    )
    duplicate = _collect_workday_revision(
        store,
        run_id="customs-workday-revision-2",
        offset_hours=1,
    )
    corrected = _collect_workday_revision(
        store,
        run_id="customs-workday-revision-3",
        offset_hours=2,
        sha_char="b",
        current=85,
    )
    corrected_duplicate = _collect_workday_revision(
        store,
        run_id="customs-workday-revision-4",
        offset_hours=3,
        sha_char="b",
        current=85,
    )

    assert first["workday_rows_inserted"] == 2
    assert duplicate["workday_rows_inserted"] == 0
    assert duplicate["workday_rows_duplicate"] == 2
    assert corrected["workday_rows_inserted"] == 2
    assert corrected_duplicate["workday_rows_inserted"] == 0
    assert corrected_duplicate["workday_rows_duplicate"] == 2
    with sqlite3.connect(store.db_path) as connection:
        rows = connection.execute(
            """
            SELECT snapshot_id, payload_json
            FROM observations
            WHERE dataset = 'ten_day_export_workdays'
              AND source_record_id = '20260710:workdays'
            ORDER BY id
            """
        ).fetchall()
        total = connection.execute(
            """
            SELECT COUNT(*) FROM observations
            WHERE dataset = 'ten_day_export_workdays'
            """
        ).fetchone()[0]
    assert total == 4
    assert len(rows) == 2
    initial_snapshot = rows[0][0]
    initial_payload = json.loads(rows[0][1])
    corrected_payload = json.loads(rows[1][1])
    assert initial_payload["revision_seq"] == 0
    assert initial_payload["supersedes_snapshot_id"] is None
    assert corrected_payload["revision_seq"] == 1
    assert corrected_payload["supersedes_snapshot_id"] == initial_snapshot
