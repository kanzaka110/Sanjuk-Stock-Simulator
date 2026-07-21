from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from core.source_observations_v2 import SourceObservationStoreV2
from src.krx_official_daily import (
    KRXError,
    KRXStatus,
    fetch_krx_daily,
    persist_krx_daily_result,
    record_krx_non_success_result,
)


AUTH_SENTINEL = "krx-secret-sentinel"


class _Response:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self._payload = payload
        self.content = repr(payload).encode("utf-8")

    def json(self):
        return self._payload


def _quote_payload(*, rows=None):
    if rows is None:
        rows = [
            {
                "BAS_DD": "20260721",
                "ISU_CD": "005930",
                "ISU_NM": "삼성전자",
                "MKT_NM": "KOSPI",
                "TDD_CLSPRC": "70,000",
                "ACC_TRDVOL": "12,345,678",
                "ACC_TRDVAL": "864,197,460,000",
            }
        ]
    return {"OutBlock_1": rows}


def _base_payload(*, rows=None):
    if rows is None:
        rows = [
            {
                "BAS_DD": "20260721",
                "ISU_CD": "KR7005930003",
                "ISU_SRT_CD": "005930",
                "MKT_TP_NM": "KOSPI",
            }
        ]
    return {"OutBlock_1": rows}


def _transport_for(quote_payload=None, base_payload=None):
    calls = []

    def transport(url, *, headers, params, timeout, allow_redirects):
        calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "params": dict(params),
                "timeout": timeout,
                "allow_redirects": allow_redirects,
            }
        )
        payload = base_payload if "base_info" in url else quote_payload
        return _Response(200, payload)

    return calls, transport


def test_missing_or_blank_key_skips_before_transport():
    for key in (None, "", "   "):
        calls = []
        result = fetch_krx_daily(
            business_date=date(2026, 7, 21),
            symbols=["005930.KS"],
            auth_key=key,
            transport=lambda *args, **kwargs: calls.append((args, kwargs)),
        )

        assert result.status is KRXStatus.SKIPPED
        assert result.error is KRXError.NOT_CONFIGURED
        assert result.rows == ()
        assert calls == []


def test_kospi_quote_and_base_are_joined_once_with_exact_units():
    calls, transport = _transport_for(_quote_payload(), _base_payload())

    result = fetch_krx_daily(
        business_date=date(2026, 7, 21),
        symbols=["005930.KS"],
        auth_key=AUTH_SENTINEL,
        transport=transport,
    )

    assert result.status is KRXStatus.SUCCESS
    assert result.error is KRXError.NONE
    assert len(calls) == 2
    assert all(call["headers"] == {"AUTH_KEY": AUTH_SENTINEL} for call in calls)
    assert all(call["params"] == {"basDd": "20260721"} for call in calls)
    assert all(call["allow_redirects"] is False for call in calls)
    assert result.rows == (
        {
            "business_date": "20260721",
            "ticker": "005930.KS",
            "isin": "KR7005930003",
            "market": "KOSPI",
            "close_krw": 70000,
            "volume_shares": 12345678,
            "trade_value_krw": 864197460000,
        },
    )
    assert AUTH_SENTINEL not in repr(result)


def test_kosdaq_uses_supported_endpoints_and_global_base_label():
    quote = _quote_payload(
        rows=[
            {
                "BAS_DD": "20260721",
                "ISU_CD": "247540",
                "ISU_NM": "에코프로비엠",
                "MKT_NM": "KOSDAQ",
                "TDD_CLSPRC": "101,000",
                "ACC_TRDVOL": "1,234",
                "ACC_TRDVAL": "124,634,000",
            }
        ]
    )
    base = _base_payload(
        rows=[
            {
                "BAS_DD": "20260721",
                "ISU_CD": "KR7247540008",
                "ISU_SRT_CD": "247540",
                "MKT_TP_NM": "KOSDAQ GLOBAL",
            }
        ]
    )
    calls, transport = _transport_for(quote, base)

    result = fetch_krx_daily(
        business_date=date(2026, 7, 21),
        symbols=["247540.KQ"],
        auth_key=AUTH_SENTINEL,
        transport=transport,
    )

    assert result.status is KRXStatus.SUCCESS
    assert [call["url"].rsplit("/", 1)[-1] for call in calls] == [
        "ksq_bydd_trd",
        "ksq_isu_base_info",
    ]
    assert result.rows[0]["market"] == "KOSDAQ"
    assert result.rows[0]["isin"] == "KR7247540008"


def test_redirect_fails_after_one_call_without_forwarding_secret():
    calls = []

    def transport(url, *, headers, params, timeout, allow_redirects):
        calls.append((url, headers, allow_redirects))
        return _Response(302, {"location": "https://attacker.invalid"})

    result = fetch_krx_daily(
        business_date=date(2026, 7, 21),
        symbols=["005930.KS"],
        auth_key=AUTH_SENTINEL,
        transport=transport,
    )

    assert result.status is KRXStatus.FAILED
    assert result.error is KRXError.HTTP
    assert len(calls) == 1
    assert calls[0][2] is False
    assert AUTH_SENTINEL not in repr(result)


def test_provider_401_is_typed_auth_and_message_is_not_retained():
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return _Response(401, {"respCode": "401", "respMsg": AUTH_SENTINEL})

    result = fetch_krx_daily(
        business_date=date(2026, 7, 21),
        symbols=["005930.KS"],
        auth_key=AUTH_SENTINEL,
        transport=transport,
    )

    assert result.status is KRXStatus.FAILED
    assert result.error is KRXError.AUTH
    assert len(calls) == 1
    assert AUTH_SENTINEL not in repr(result)


def test_mixed_market_empty_response_fails_closed_instead_of_partial_success():
    kospi_quote = _quote_payload()
    kospi_base = _base_payload()
    kosdaq_quote = _quote_payload(rows=[])
    kosdaq_base = _base_payload(
        rows=[
            {
                "BAS_DD": "20260721",
                "ISU_CD": "KR7035720002",
                "ISU_SRT_CD": "035720",
                "MKT_TP_NM": "KOSDAQ",
            }
        ]
    )
    responses = [kospi_quote, kospi_base, kosdaq_quote, kosdaq_base]
    calls = []

    def transport(url, *, headers, params, timeout, allow_redirects):
        calls.append((url, headers, params, timeout, allow_redirects))
        return _Response(200, responses[len(calls) - 1])

    result = fetch_krx_daily(
        business_date=date(2026, 7, 21),
        symbols=["005930.KS", "035720.KQ"],
        auth_key=AUTH_SENTINEL,
        transport=transport,
    )

    assert result.status is KRXStatus.FAILED
    assert result.error is KRXError.PROVIDER
    assert result.rows == ()
    assert len(calls) == 4


def test_unhashable_resp_code_is_typed_malformed():
    result = fetch_krx_daily(
        business_date=date(2026, 7, 21),
        symbols=["005930.KS"],
        auth_key=AUTH_SENTINEL,
        transport=lambda *args, **kwargs: _Response(
            200,
            {"respCode": ["401"], "respMsg": "bad"},
        ),
    )

    assert result.status is KRXStatus.FAILED
    assert result.error is KRXError.MALFORMED


def test_unhashable_market_labels_are_typed_malformed():
    quote = json.loads(json.dumps(_quote_payload()))
    quote["OutBlock_1"][0]["MKT_NM"] = ["KOSPI"]
    _calls, transport = _transport_for(quote, _base_payload())

    result = fetch_krx_daily(
        business_date=date(2026, 7, 21),
        symbols=["005930.KS"],
        auth_key=AUTH_SENTINEL,
        transport=transport,
    )

    assert result.status is KRXStatus.FAILED
    assert result.error is KRXError.MALFORMED


def test_malformed_later_unrequested_row_invalidates_whole_market_batch():
    quote = _quote_payload(
        rows=[
            _quote_payload()["OutBlock_1"][0],
            {
                "BAS_DD": "20260721",
                "ISU_CD": "000660",
                "ISU_NM": "SK하이닉스",
                "MKT_NM": "KOSPI",
                "TDD_CLSPRC": "not-a-number",
                "ACC_TRDVOL": "1",
                "ACC_TRDVAL": "1",
            },
        ]
    )
    calls, transport = _transport_for(quote, _base_payload())

    result = fetch_krx_daily(
        business_date=date(2026, 7, 21),
        symbols=["005930.KS"],
        auth_key=AUTH_SENTINEL,
        transport=transport,
    )

    assert result.status is KRXStatus.FAILED
    assert result.error is KRXError.NUMERIC
    assert result.rows == ()
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("quote", "base", "expected_status", "expected_error"),
    [
        ({"OutBlock_1": []}, {"OutBlock_1": []}, KRXStatus.EMPTY, KRXError.NONE),
        ({"OutBlock_1": []}, _base_payload(), KRXStatus.EMPTY, KRXError.NONE),
        (
            {"OutBlock_1": []},
            _base_payload(rows=[{
                "BAS_DD": "20260721",
                "ISU_CD": "KR7000660001",
                "ISU_SRT_CD": "000660",
                "MKT_TP_NM": "KOSPI",
            }]),
            KRXStatus.FAILED,
            KRXError.MALFORMED,
        ),
    ],
)
def test_empty_companion_matrix(quote, base, expected_status, expected_error):
    _calls, transport = _transport_for(quote, base)
    result = fetch_krx_daily(
        business_date=date(2026, 7, 21),
        symbols=["005930.KS"],
        auth_key=AUTH_SENTINEL,
        transport=transport,
    )

    assert result.status is expected_status
    assert result.error is expected_error


def test_configured_failed_fetch_is_recorded_in_run_ledger(tmp_path):
    result = fetch_krx_daily(
        business_date=date(2026, 7, 21),
        symbols=["005930.KS"],
        auth_key=AUTH_SENTINEL,
        transport=lambda *args, **kwargs: _Response(503, {}),
    )
    assert result.status is KRXStatus.FAILED
    store = SourceObservationStoreV2(tmp_path / "source_observations_v2.db")

    summary = record_krx_non_success_result(
        result,
        store=store,
        completed_at_utc=datetime(2026, 7, 21, 7, 0, tzinfo=timezone.utc),
        run_id="krx-http-failed",
        rows_seen=1,
    )

    assert summary["status"] == "failed"
    assert summary["error_type"] == "http"
    with sqlite3.connect(store.db_path) as connection:
        run = connection.execute(
            "SELECT status,rows_seen,rows_invalid,error_type FROM collection_runs"
        ).fetchone()
    assert run == ("failed", 1, 1, "http")


def test_success_result_persists_point_in_time_row_and_run_atomically(tmp_path):
    _calls, transport = _transport_for(_quote_payload(), _base_payload())
    result = fetch_krx_daily(
        business_date=date(2026, 7, 21),
        symbols=["005930.KS"],
        auth_key=AUTH_SENTINEL,
        transport=transport,
    )
    store = SourceObservationStoreV2(tmp_path / "source_observations_v2.db")
    ingested_at = datetime(2026, 7, 21, 7, 0, tzinfo=timezone.utc)

    summary = persist_krx_daily_result(
        result,
        store=store,
        ingested_at_utc=ingested_at,
        run_id="krx-20260721-a",
    )

    assert summary == {
        "source": "krx_openapi",
        "dataset": "domestic_eod_quote",
        "status": "success",
        "rows_seen": 1,
        "rows_inserted": 1,
        "rows_duplicate": 0,
        "rows_skipped": 0,
        "rows_invalid": 0,
        "error_type": "",
    }
    with sqlite3.connect(store.db_path) as connection:
        observation = connection.execute(
            "SELECT source,dataset,source_record_id,symbol,market,currency_or_unit,"
            "source_as_of,ingested_at,fallback_used,payload_json FROM observations"
        ).fetchone()
        run = connection.execute(
            "SELECT status,rows_seen,rows_inserted,error_type FROM collection_runs"
        ).fetchone()
    assert observation[:6] == (
        "krx_openapi",
        "domestic_eod_quote",
        "20260721:KR7005930003",
        "005930.KS",
        "KR",
        "MIXED",
    )
    assert observation[6] == "2026-07-21T06:30:00.000000Z"
    assert observation[7] == "2026-07-21T07:00:00.000000Z"
    assert observation[8] == 0
    payload = json.loads(observation[9])
    assert payload["close_krw"] == 70000
    assert payload["volume_shares"] == 12345678
    assert payload["units"] == {
        "close_krw": "KRW/share",
        "trade_value_krw": "KRW",
        "volume_shares": "shares",
    }
    assert run == ("success", 1, 1, "")


def test_persistence_revalidates_mutable_fetch_rows_before_opening_transaction(tmp_path):
    _calls, transport = _transport_for(_quote_payload(), _base_payload())
    result = fetch_krx_daily(
        business_date=date(2026, 7, 21),
        symbols=["005930.KS"],
        auth_key=AUTH_SENTINEL,
        transport=transport,
    )
    result.rows[0]["close_krw"] = "70000"
    store = SourceObservationStoreV2(tmp_path / "source_observations_v2.db")

    with pytest.raises(ValueError, match="krx_result_row_invalid"):
        persist_krx_daily_result(
            result,
            store=store,
            ingested_at_utc=datetime(2026, 7, 21, 7, 0, tzinfo=timezone.utc),
            run_id="krx-mutated-row",
        )

    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 0


def test_exact_retry_is_duplicate_not_revision(tmp_path):
    _calls, transport = _transport_for(_quote_payload(), _base_payload())
    result = fetch_krx_daily(
        business_date=date(2026, 7, 21),
        symbols=["005930.KS"],
        auth_key=AUTH_SENTINEL,
        transport=transport,
    )
    store = SourceObservationStoreV2(tmp_path / "source_observations_v2.db")
    ingested_at = datetime(2026, 7, 21, 7, 0, tzinfo=timezone.utc)
    first = persist_krx_daily_result(
        result, store=store, ingested_at_utc=ingested_at, run_id="krx-a"
    )
    second = persist_krx_daily_result(
        result, store=store, ingested_at_utc=ingested_at, run_id="krx-b"
    )

    assert first["rows_inserted"] == 1
    assert second["rows_inserted"] == 0
    assert second["rows_duplicate"] == 1


def test_changed_official_row_appends_explicit_correction_lineage(tmp_path):
    _calls, transport = _transport_for(_quote_payload(), _base_payload())
    first_result = fetch_krx_daily(
        business_date=date(2026, 7, 21),
        symbols=["005930.KS"],
        auth_key=AUTH_SENTINEL,
        transport=transport,
    )
    corrected_result = replace(
        first_result,
        rows=({**first_result.rows[0], "close_krw": 70100},),
    )
    store = SourceObservationStoreV2(tmp_path / "source_observations_v2.db")
    ingested_at = datetime(2026, 7, 21, 7, 0, tzinfo=timezone.utc)

    persist_krx_daily_result(
        first_result, store=store, ingested_at_utc=ingested_at, run_id="krx-original"
    )
    corrected = persist_krx_daily_result(
        corrected_result,
        store=store,
        ingested_at_utc=ingested_at + timedelta(minutes=1),
        run_id="krx-correction",
    )
    replay = persist_krx_daily_result(
        corrected_result,
        store=store,
        ingested_at_utc=ingested_at + timedelta(minutes=2),
        run_id="krx-correction-replay",
    )

    with sqlite3.connect(store.db_path) as connection:
        rows = connection.execute(
            "SELECT snapshot_id,source_event_sequence,payload_json "
            "FROM observations ORDER BY id"
        ).fetchall()
    assert corrected["rows_inserted"] == 1
    assert replay["rows_inserted"] == 0
    assert replay["rows_duplicate"] == 1
    assert [row[1] for row in rows] == [0, 1]
    corrected_payload = json.loads(rows[1][2])
    assert corrected_payload["correction_of_snapshot_id"] == rows[0][0]
    assert corrected_payload["close_krw"] == 70100


def test_cli_not_configured_is_zero_network_and_creates_no_database(tmp_path):
    db_path = tmp_path / "must-not-exist.db"
    env = dict(os.environ)
    env.pop("KRX_AUTH_KEY", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.krx_official_daily",
            "--date",
            "20260721",
            "--symbols",
            "005930.KS",
            "--db",
            str(db_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stderr == ""
    output = json.loads(completed.stdout)
    assert output == {
        "source": "krx_openapi",
        "dataset": "domestic_eod_quote",
        "status": "skipped",
        "rows_seen": 1,
        "rows_inserted": 0,
        "rows_duplicate": 0,
        "rows_skipped": 1,
        "rows_invalid": 0,
        "error_type": "not_configured",
    }
    assert "005930" not in completed.stdout
    assert not db_path.exists()
    assert not Path(str(db_path) + "-wal").exists()
    assert not Path(str(db_path) + "-shm").exists()


def test_cli_rejects_invalid_symbol_before_database_open(tmp_path):
    db_path = tmp_path / "invalid-must-not-exist.db"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.krx_official_daily",
            "--date",
            "20260721",
            "--symbols",
            "MU",
            "--db",
            str(db_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "KRX_AUTH_KEY": AUTH_SENTINEL, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "status": "failed",
        "error_type": "ValueError",
    }
    assert AUTH_SENTINEL not in completed.stdout
    assert not db_path.exists()
