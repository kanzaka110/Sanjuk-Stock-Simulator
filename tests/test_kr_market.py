"""tests/test_kr_market.py

KRX 수급(외국인·기관) 파일 캐시 테스트 — 배치 사전수집 소비 경로.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── 수급 파일 캐시 (배치 사전수집 소비 경로) ─────────────────────

class TestFrgnFileCache(unittest.TestCase):
    def _rows(self):
        return [{"date": "20260714", "close": 100.0,
                 "inst_shares": 10.0, "foreign_shares": 20.0}]

    def test_file_cache_roundtrip_and_ttl(self):
        import core.kr_market as km
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "kr_frgn_cache.json"
            with patch.object(km, "_frgn_file_cache_path", return_value=p):
                km._save_frgn_file_entry("005930", self._rows())
                self.assertEqual(km._load_frgn_file_entry("005930"), self._rows())
                # TTL 초과 → None (stale을 신선한 척 반환하지 않음)
                data = json.loads(p.read_text(encoding="utf-8"))
                data["005930"]["fetched_at"] = "2020-01-01T00:00:00+00:00"
                p.write_text(json.dumps(data), encoding="utf-8")
                self.assertIsNone(km._load_frgn_file_entry("005930"))

    def test_empty_rows_do_not_overwrite(self):
        import core.kr_market as km
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "kr_frgn_cache.json"
            with patch.object(km, "_frgn_file_cache_path", return_value=p):
                km._save_frgn_file_entry("005930", self._rows())
                km._save_frgn_file_entry("005930", [])   # 실패분은 무시
                self.assertEqual(km._load_frgn_file_entry("005930"), self._rows())

    def test_fetch_uses_file_cache_without_network(self):
        import core.kr_market as km
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "kr_frgn_cache.json"
            with patch.object(km, "_frgn_file_cache_path", return_value=p), \
                 patch.object(km, "_FRGN_CACHE", {}), \
                 patch.object(km.requests, "get",
                              side_effect=AssertionError("network hit")):
                km._save_frgn_file_entry("000660", self._rows())
                self.assertEqual(km._fetch_naver_frgn("000660"), self._rows())

    def test_corrupt_cache_file_returns_none(self):
        import core.kr_market as km
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "kr_frgn_cache.json"
            p.write_text("{broken", encoding="utf-8")
            with patch.object(km, "_frgn_file_cache_path", return_value=p):
                self.assertIsNone(km._load_frgn_file_entry("005930"))


# ── typed 네이버 수급 parser/fetcher ────────────────────────────────

def _naver_html(*rows: tuple[str, str, str, str]) -> str:
    body = []
    for date, close, inst, foreign in rows:
        body.append(
            "<tr>"
            f"<td>{date}</td><td>{close}</td><td>0</td><td>0%</td>"
            f"<td>100</td><td>{inst}</td><td>{foreign}</td><td>0</td><td>0%</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr>"
        + "".join(f"<th>c{i}</th>" for i in range(9))
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
    )


def test_parse_naver_frgn_html_keeps_exact_rows_units_and_zero_net_shares() -> None:
    from core.kr_market import parse_naver_frgn_html

    parsed = parse_naver_frgn_html(
        _naver_html(
            ("2026.07.14", "70,100", "0", "0"),
            ("2026.07.11", "69,900", "-1,200", "+800"),
        ),
        "005930",
    )

    assert parsed["code"] == "005930"
    assert parsed["rows"] == [
        {
            "date": "20260714",
            "close": 70_100.0,
            "inst_shares": 0.0,
            "foreign_shares": 0.0,
        },
        {
            "date": "20260711",
            "close": 69_900.0,
            "inst_shares": -1_200.0,
            "foreign_shares": 800.0,
        },
    ]
    assert parsed["units"] == {
        "date": "business_date",
        "close": "KRW/share",
        "inst_shares": "shares",
        "foreign_shares": "shares",
    }
    assert parsed["derived_schema_version"] == "1"


def test_parse_naver_missing_table_is_malformed() -> None:
    from core.kr_market import parse_naver_frgn_html

    with pytest.raises(ValueError, match="malformed"):
        parse_naver_frgn_html("<html><p>not a table</p></html>", "005930")


def test_parse_naver_selects_later_valid_candidate_after_malformed_large_table() -> None:
    import pandas as pd

    from core.kr_market import parse_naver_frgn_html

    malformed_row = [
        "2026.02.30", "100", "0", "0", "0", "1", "2", "0", "0"
    ]
    malformed_large = pd.DataFrame([malformed_row] * 4)
    valid_row = [
        "2026.07.14", "70,100", "0", "0", "0", "11", "-12", "0", "0"
    ]
    valid = pd.DataFrame([valid_row])
    empty = pd.DataFrame(columns=range(9))

    parsed = parse_naver_frgn_html(
        "unused",
        "005930.KS",
        table_reader=lambda _source: [malformed_large, valid],
    )
    valid_empty = parse_naver_frgn_html(
        "unused",
        "005930",
        table_reader=lambda _source: [malformed_large, empty],
    )

    assert parsed["rows"] == [
        {
            "date": "20260714",
            "close": 70_100.0,
            "inst_shares": 11.0,
            "foreign_shares": -12.0,
        }
    ]
    assert valid_empty["rows"] == []
    with pytest.raises(ValueError, match="malformed"):
        parse_naver_frgn_html(
            "unused",
            "005930",
            table_reader=lambda _source: [malformed_large],
        )


class _HttpResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


class _Clock:
    def __init__(self, *values: datetime):
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


def _utc(hour: int) -> datetime:
    return datetime(2026, 7, 15, hour, tzinfo=timezone.utc)


def test_typed_naver_network_success_persists_completion_timestamp(monkeypatch, tmp_path) -> None:
    import core.kr_market as km

    calls: list[dict] = []

    def get(url: str, **kwargs: object) -> _HttpResponse:
        calls.append({"url": url, **kwargs})
        return _HttpResponse(_naver_html(("2026.07.14", "70,100", "0", "25")))

    cache_path = tmp_path / "kr_frgn_cache.json"
    monkeypatch.setattr(km, "_frgn_file_cache_path", lambda: cache_path)
    monkeypatch.setattr(km, "_FRGN_CACHE", {})
    monkeypatch.setattr(km, "_FRGN_CACHE_FETCHED_AT", {})

    result = km.fetch_naver_frgn_result(
        "005930",
        force_refresh=True,
        clock=_Clock(_utc(1), _utc(2)),
        http_get=get,
    )

    assert result.status == "success"
    assert result.cache_source == "network"
    assert result.completed_at_utc == _utc(2)
    assert result.value["rows"][0]["foreign_shares"] == 25.0
    assert result.value["units"]["close"] == "KRW/share"
    assert result.tr_id is None
    assert result.venue == "KRX"
    assert len(calls) == 1
    assert calls[0]["params"] == {"code": "005930"}
    stored = json.loads(cache_path.read_text(encoding="utf-8"))["005930"]
    assert stored["fetched_at"] == _utc(2).isoformat()


def test_typed_naver_memory_hit_preserves_original_fetch_timestamp(monkeypatch) -> None:
    import core.kr_market as km

    rows = [{"date": "20260714", "close": 100.0,
             "inst_shares": 0.0, "foreign_shares": 0.0}]
    original = _utc(0)
    monkeypatch.setattr(km, "_FRGN_CACHE", {"005930": rows})
    monkeypatch.setattr(km, "_FRGN_CACHE_FETCHED_AT", {"005930": original})

    current_start = _utc(1)
    current_complete = _utc(2)
    result = km.fetch_naver_frgn_result(
        "005930",
        fallback_used=True,
        clock=_Clock(current_start, current_complete),
        http_get=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network")),
    )

    assert result.status == "success"
    assert result.cache_source == "memory"
    assert result.started_at_utc == current_start
    assert result.completed_at_utc == current_complete
    assert result.source_fetched_at_utc == original
    assert result.fallback_used is True
    assert result.value["rows"] == rows


def test_typed_naver_stale_memory_entry_refreshes_network_once(monkeypatch, tmp_path) -> None:
    import core.kr_market as km

    rows = [{"date": "20200101", "close": 1.0,
             "inst_shares": 1.0, "foreign_shares": 1.0}]
    monkeypatch.setattr(km, "_FRGN_CACHE", {"005930": rows})
    monkeypatch.setattr(
        km,
        "_FRGN_CACHE_FETCHED_AT",
        {"005930": datetime(2020, 1, 1, tzinfo=timezone.utc)},
    )
    monkeypatch.setattr(km, "_frgn_file_cache_path", lambda: tmp_path / "cache.json")
    calls = []

    def get(*args, **kwargs):
        calls.append((args, kwargs))
        return _HttpResponse(_naver_html(("2026.07.14", "100", "2", "3")))

    result = km.fetch_naver_frgn_result(
        "005930",
        clock=_Clock(_utc(1), _utc(2)),
        http_get=get,
    )

    assert result.status == "success"
    assert result.cache_source == "network"
    assert result.source_fetched_at_utc == _utc(2)
    assert len(calls) == 1


def test_typed_naver_malformed_memory_entry_refreshes_network_once(
    monkeypatch, tmp_path
) -> None:
    import core.kr_market as km

    monkeypatch.setattr(km, "_FRGN_CACHE", {"005930": [{}]})
    monkeypatch.setattr(km, "_FRGN_CACHE_FETCHED_AT", {"005930": _utc(0)})
    monkeypatch.setattr(km, "_frgn_file_cache_path", lambda: tmp_path / "cache.json")
    calls = []

    def get(*args, **kwargs):
        calls.append((args, kwargs))
        return _HttpResponse(_naver_html(("2026.07.14", "100", "2", "3")))

    result = km.fetch_naver_frgn_result(
        "005930",
        clock=_Clock(_utc(1), _utc(2)),
        http_get=get,
    )

    assert result.status == "success"
    assert result.cache_source == "network"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "bad_timestamp",
    ["not-an-iso-timestamp", datetime(2026, 7, 15, 0, 0)],
)
def test_typed_naver_malformed_memory_timestamp_is_miss_and_refreshes_once(
    monkeypatch, tmp_path, bad_timestamp
) -> None:
    import core.kr_market as km

    rows = [
        {
            "date": "20260714",
            "close": 100.0,
            "inst_shares": 1.0,
            "foreign_shares": 2.0,
        }
    ]
    monkeypatch.setattr(km, "_FRGN_CACHE", {"005930": rows})
    monkeypatch.setattr(
        km,
        "_FRGN_CACHE_FETCHED_AT",
        {"005930": bad_timestamp},
    )
    monkeypatch.setattr(km, "_frgn_file_cache_path", lambda: tmp_path / "cache.json")
    calls = []

    def get(*args, **kwargs):
        calls.append((args, kwargs))
        return _HttpResponse(_naver_html(("2026.07.14", "101", "3", "4")))

    result = km.fetch_naver_frgn_result(
        "005930",
        clock=_Clock(_utc(1), _utc(2)),
        http_get=get,
    )

    assert (result.status, result.cache_source) == ("success", "network")
    assert result.source_fetched_at_utc == _utc(2)
    assert result.value is not None
    assert result.value["rows"][0]["close"] == 101.0
    assert len(calls) == 1


def test_typed_naver_file_hit_preserves_original_fetch_timestamp(monkeypatch, tmp_path) -> None:
    import core.kr_market as km

    rows = [{"date": "20260714", "close": 100.0,
             "inst_shares": 1.0, "foreign_shares": 2.0}]
    original = datetime(2026, 7, 15, 0, 30, tzinfo=timezone.utc)
    cache_path = tmp_path / "kr_frgn_cache.json"
    monkeypatch.setattr(km, "_frgn_file_cache_path", lambda: cache_path)
    monkeypatch.setattr(km, "_FRGN_CACHE", {})
    monkeypatch.setattr(km, "_FRGN_CACHE_FETCHED_AT", {})
    km._save_frgn_file_entry("005930", rows, fetched_at=original)

    current_start = _utc(1)
    current_complete = _utc(2)
    result = km.fetch_naver_frgn_result(
        "005930",
        clock=_Clock(current_start, current_complete),
        http_get=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network")),
    )

    assert result.status == "success"
    assert result.cache_source == "file"
    assert result.started_at_utc == current_start
    assert result.completed_at_utc == current_complete
    assert result.source_fetched_at_utc == original
    assert result.value["rows"] == rows


@pytest.mark.parametrize(
    ("rows", "fetched_at"),
    [
        ([{}], _utc(0)),
        (
            [{"date": "20260714", "close": 100.0,
              "inst_shares": 1.0, "foreign_shares": 2.0}],
            _utc(3),
        ),
    ],
)
def test_typed_naver_corrupt_or_future_file_cache_refreshes_network_once(
    monkeypatch, tmp_path, rows, fetched_at
) -> None:
    import core.kr_market as km

    cache_path = tmp_path / "kr_frgn_cache.json"
    cache_path.write_text(
        json.dumps(
            {"005930": {"fetched_at": fetched_at.isoformat(), "rows": rows}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(km, "_frgn_file_cache_path", lambda: cache_path)
    monkeypatch.setattr(km, "_FRGN_CACHE", {})
    monkeypatch.setattr(km, "_FRGN_CACHE_FETCHED_AT", {})
    calls = []

    def get(*args, **kwargs):
        calls.append((args, kwargs))
        return _HttpResponse(_naver_html(("2026.07.14", "100", "2", "3")))

    result = km.fetch_naver_frgn_result(
        "005930",
        clock=_Clock(_utc(1), _utc(2)),
        http_get=get,
    )

    assert result.status == "success"
    assert result.cache_source == "network"
    assert len(calls) == 1


def test_legacy_memory_without_timestamp_is_incomplete_but_wrapper_returns_rows(
    monkeypatch,
) -> None:
    import core.kr_market as km

    rows = [{"date": "20260714", "close": 100.0,
             "inst_shares": 0.0, "foreign_shares": 0.0}]
    monkeypatch.setattr(km, "_FRGN_CACHE", {"005930": rows})
    monkeypatch.setattr(km, "_FRGN_CACHE_FETCHED_AT", {})

    typed = km.fetch_naver_frgn_result(
        "005930",
        clock=_Clock(_utc(1), _utc(2)),
        http_get=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network")),
    )

    assert (typed.status, typed.error_type, typed.cache_source) == (
        "incomplete", "cache_timestamp_missing", "memory"
    )
    assert typed.value["rows"] == rows
    assert km._fetch_naver_frgn("005930") == rows


def test_typed_naver_valid_empty_is_distinct_from_malformed_and_numeric(
    monkeypatch, tmp_path
) -> None:
    import pandas as pd
    import core.kr_market as km

    monkeypatch.setattr(km, "_frgn_file_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(km, "_FRGN_CACHE", {})
    monkeypatch.setattr(km, "_FRGN_CACHE_FETCHED_AT", {})

    def fetch_with_tables(tables):
        return km.fetch_naver_frgn_result(
            "005930",
            force_refresh=True,
            clock=_Clock(_utc(1), _utc(2)),
            http_get=lambda *_a, **_k: _HttpResponse("unused"),
            table_reader=lambda _html: tables,
        )

    empty = fetch_with_tables([pd.DataFrame(columns=range(9))])
    malformed = fetch_with_tables([pd.DataFrame(columns=range(3))])
    numeric_row = ["2026.07.14", "bad", "0", "0", "0", "1", "2", "0", "0"]
    numeric = fetch_with_tables([pd.DataFrame([numeric_row])])
    valid_row = ["2026.07.14", "100", "0", "0", "0", "1", "2", "0", "0"]
    valid_after_empty = fetch_with_tables(
        [
            pd.DataFrame(columns=range(9)),
            pd.DataFrame([valid_row, valid_row, valid_row, valid_row]),
        ]
    )
    invalid_date = fetch_with_tables(
        [pd.DataFrame([["not-a-date", "100", "0", "0", "0", "1", "2", "0", "0"]])]
    )

    assert (empty.status, empty.error_type, empty.value["rows"]) == (
        "empty", "none", []
    )
    assert (malformed.status, malformed.error_type, malformed.value) == (
        "failed", "malformed", None
    )
    assert (numeric.status, numeric.error_type, numeric.value) == (
        "failed", "numeric", None
    )
    assert valid_after_empty.value is not None
    assert (valid_after_empty.status, len(valid_after_empty.value["rows"])) == (
        "success",
        4,
    )
    assert (invalid_date.status, invalid_date.error_type, invalid_date.value) == (
        "failed",
        "malformed",
        None,
    )


def test_typed_naver_network_and_http_failures_are_safe(monkeypatch, tmp_path, caplog) -> None:
    import core.kr_market as km

    monkeypatch.setattr(km, "_frgn_file_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(km, "_FRGN_CACHE", {})
    monkeypatch.setattr(km, "_FRGN_CACHE_FETCHED_AT", {})

    network = km.fetch_naver_frgn_result(
        "005930",
        force_refresh=True,
        clock=_Clock(_utc(1), _utc(2)),
        http_get=lambda *_a, **_k: (_ for _ in ()).throw(
            ConnectionError("raw network provider secret")
        ),
    )
    http = km.fetch_naver_frgn_result(
        "005930",
        force_refresh=True,
        clock=_Clock(_utc(1), _utc(2)),
        http_get=lambda *_a, **_k: _HttpResponse("raw provider body", 503),
    )

    assert (network.status, network.error_type) == ("failed", "network")
    assert (http.status, http.error_type) == ("failed", "http")
    assert "raw network provider secret" not in caplog.text
    assert "raw provider body" not in caplog.text


def test_typed_naver_clean_empty_uses_memory_ttl_then_refreshes_once_stale(
    monkeypatch, tmp_path
) -> None:
    import pandas as pd

    import core.kr_market as km

    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(km, "_frgn_file_cache_path", lambda: cache_path)
    monkeypatch.setattr(km, "_FRGN_CACHE", {})
    monkeypatch.setattr(km, "_FRGN_CACHE_FETCHED_AT", {})
    calls = {"http": 0, "table": 0}
    first_started = datetime(2026, 7, 15, 0, tzinfo=timezone.utc)
    first_completed = first_started + timedelta(minutes=1)

    def get(*_args, **_kwargs):
        calls["http"] += 1
        return _HttpResponse("unused")

    def reader(_source):
        calls["table"] += 1
        return [pd.DataFrame(columns=range(9))]

    first = km.fetch_naver_frgn_result(
        "005930",
        clock=_Clock(first_started, first_completed),
        http_get=get,
        table_reader=reader,
    )
    fresh = km.fetch_naver_frgn_result(
        "005930",
        clock=_Clock(
            first_completed + timedelta(hours=1),
            first_completed + timedelta(hours=1, minutes=1),
        ),
        http_get=get,
        table_reader=reader,
    )
    stale_started = first_completed + timedelta(hours=27)
    stale_completed = stale_started + timedelta(minutes=1)
    stale = km.fetch_naver_frgn_result(
        "005930",
        clock=_Clock(stale_started, stale_completed),
        http_get=get,
        table_reader=reader,
    )

    assert first.value is not None
    assert fresh.value is not None
    assert stale.value is not None
    assert (first.status, first.cache_source, first.value["rows"]) == (
        "empty", "network", []
    )
    assert first.source_fetched_at_utc == first_completed
    assert (fresh.status, fresh.cache_source, fresh.value["rows"]) == (
        "empty", "memory", []
    )
    assert fresh.source_fetched_at_utc == first_completed
    assert (stale.status, stale.cache_source, stale.value["rows"]) == (
        "empty", "network", []
    )
    assert stale.source_fetched_at_utc == stale_completed
    assert calls == {"http": 2, "table": 2}
    assert not cache_path.exists()

    legacy_calls = []

    def typed_once(code, **kwargs):
        legacy_calls.append((code, kwargs))
        return first

    monkeypatch.setattr(km, "fetch_naver_frgn_result", typed_once)
    assert km._fetch_naver_frgn("005930") == []
    assert legacy_calls == [("005930", {"force_refresh": False})]


def test_legacy_naver_network_wrapper_keeps_rows_and_single_call(monkeypatch, tmp_path) -> None:
    import core.kr_market as km

    calls = {"http": 0, "table": 0}
    monkeypatch.setattr(km, "_frgn_file_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(km, "_FRGN_CACHE", {})
    monkeypatch.setattr(km, "_FRGN_CACHE_FETCHED_AT", {})

    def get(*_args, **_kwargs):
        calls["http"] += 1
        return _HttpResponse(_naver_html(("2026.07.14", "100", "0", "0")))

    def reader(html):
        calls["table"] += 1
        import pandas as pd
        return pd.read_html(html)

    # legacy wrapper는 기본 reader를 사용하므로 typed 함수를 감싸 DI만 고정한다.
    original = km.fetch_naver_frgn_result
    monkeypatch.setattr(
        km,
        "fetch_naver_frgn_result",
        lambda code, **kwargs: original(
            code,
            **kwargs,
            clock=_Clock(_utc(1), _utc(2)),
            http_get=get,
            table_reader=reader,
        ),
    )

    assert km._fetch_naver_frgn("005930", force_refresh=True) == [
        {"date": "20260714", "close": 100.0,
         "inst_shares": 0.0, "foreign_shares": 0.0}
    ]
    assert calls == {"http": 1, "table": 1}
