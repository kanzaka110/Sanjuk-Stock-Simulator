"""Typed KIS market-data parser/fetcher contract tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _utc(hour: int = 1) -> datetime:
    return datetime(2026, 7, 15, hour, 2, 3, tzinfo=timezone.utc)


def test_overseas_quote_preserves_official_volume_and_turnover(monkeypatch) -> None:
    from core import market_kis

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "rt_cd": "0",
                "output": {
                    "last": "190.00", "base": "185.00",
                    "high": "192.00", "low": "184.00",
                    "pvol": "9876543", "tvol": "12345678",
                    "tamt": "2345678901.25",
                },
            }

    monkeypatch.setattr(market_kis.requests, "get", lambda *a, **k: _Response())
    monkeypatch.setattr(market_kis.time, "time", lambda: 1_752_541_323.0)

    quote = market_kis._fetch_overseas_quote("NVDA", "NAS", "token")

    assert quote is not None
    assert quote.volume == 12_345_678.0
    assert quote.turnover == 2_345_678_901.25
    assert quote.previous_volume == 9_876_543.0
    assert quote.source == "kis"
    assert quote.as_of == 1_752_541_323.0


def _orderbook_payload(*, depth: int = 100) -> dict:
    output1: dict[str, str] = {
        "aspr_acpt_hour": "101530",
        "total_askp_rsqn": "5500",
        "total_bidp_rsqn": "6500",
        "ovtm_total_askp_rsqn": "70",
        "ovtm_total_bidp_rsqn": "80",
        "total_askp_rsqn_icdc": "-11",
        "total_bidp_rsqn_icdc": "12",
    }
    for level in range(1, 11):
        output1[f"askp{level}"] = str(70_000 + level * 100)
        output1[f"bidp{level}"] = str(70_000 - level * 100)
        output1[f"askp_rsqn{level}"] = str(depth + level)
        output1[f"bidp_rsqn{level}"] = str(depth + level * 2)
    return {
        "rt_cd": "0",
        "output1": output1,
        "output2": {"antc_cnpr": "70000", "antc_cnqn": "42", "antc_vol": "900"},
    }


def test_fetch_result_is_frozen_and_normalizes_aware_time_to_utc() -> None:
    from core.market_data_fetch import CacheSource, FetchErrorType, FetchResult, FetchStatus

    result = FetchResult(
        status=FetchStatus.SUCCESS,
        provider="KIS",
        endpoint="/endpoint",
        tr_id="TR",
        venue="J",
        symbol="005930",
        started_at_utc=_utc(),
        completed_at_utc=_utc(2),
        error_type=FetchErrorType.NONE,
        cache_source=CacheSource.NETWORK,
        fallback_used=False,
        value={"ok": True},
    )

    assert result.status == "success"
    assert result.completed_at_utc.tzinfo is timezone.utc
    with pytest.raises(FrozenInstanceError):
        result.value = None  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {"error_type": "network"},
        {"value": None},
        {"cache_source": "none"},
        {"cache_source": "memory"},
        {"source_fetched_at_utc": _utc(3)},
        {"status": "empty", "value": None},
        {"status": "empty", "error_type": "network", "value": []},
        {"status": "failed", "error_type": "none", "value": None},
        {"status": "failed", "error_type": "network", "value": {}},
        {
            "status": "failed",
            "error_type": "network",
            "cache_source": "memory",
            "value": None,
        },
        {
            "status": "skipped",
            "error_type": "auth",
            "cache_source": "none",
            "value": None,
        },
        {
            "status": "incomplete",
            "error_type": "none",
            "cache_source": "network",
            "value": {},
        },
        {
            "status": "incomplete",
            "error_type": "zero_depth",
            "cache_source": "network",
            "value": None,
        },
        {
            "status": "incomplete",
            "error_type": "zero_depth",
            "cache_source": "none",
            "value": {},
        },
        {
            "status": "failed",
            "error_type": "network",
            "cache_source": "network",
            "value": None,
            "source_fetched_at_utc": _utc(),
        },
        {
            "status": "skipped",
            "error_type": "not_configured",
            "cache_source": "none",
            "value": None,
            "source_fetched_at_utc": _utc(),
        },
        {
            "status": "incomplete",
            "error_type": "cache_timestamp_missing",
            "cache_source": "network",
            "value": {},
        },
        {
            "status": "incomplete",
            "error_type": "cache_timestamp_missing",
            "cache_source": "memory",
            "value": {},
            "source_fetched_at_utc": _utc(),
        },
        {
            "status": "failed",
            "error_type": "cache_timestamp_missing",
            "cache_source": "none",
            "value": None,
        },
        {
            "status": "incomplete",
            "error_type": "zero_depth",
            "cache_source": "memory",
            "value": {},
            "source_fetched_at_utc": _utc(),
        },
        {
            "status": "failed",
            "error_type": "zero_depth",
            "cache_source": "network",
            "value": None,
        },
        {
            "status": "incomplete",
            "error_type": "auth",
            "cache_source": "network",
            "value": {},
        },
    ],
)
def test_fetch_result_rejects_contradictory_status_combinations(overrides) -> None:
    from core.market_data_fetch import FetchResult

    values = {
        "status": "success",
        "provider": "KIS",
        "endpoint": "/endpoint",
        "tr_id": "TR",
        "venue": "J",
        "symbol": "005930",
        "started_at_utc": _utc(),
        "completed_at_utc": _utc(2),
        "error_type": "none",
        "cache_source": "network",
        "fallback_used": False,
        "value": {"ok": True},
    }
    values.update(overrides)

    with pytest.raises(ValueError, match="invalid fetch result state"):
        FetchResult(**values)


def test_parse_orderbook_keeps_all_ten_levels_raw_totals_and_derived_values() -> None:
    from core.market_kis import parse_kis_orderbook_payload

    parsed = parse_kis_orderbook_payload(
        _orderbook_payload(), "005930.KS", "J", _utc(2)
    )

    assert [row["level"] for row in parsed["levels"]] == list(range(1, 11))
    assert parsed["levels"][0] == {
        "level": 1,
        "ask_price": 70_100,
        "ask_size": 101,
        "bid_price": 69_900,
        "bid_size": 102,
    }
    assert parsed["raw_totals"]["ovtm_total_bidp_rsqn"] == "80"
    assert parsed["expected_execution"]["antc_cnqn"] == "42"
    assert parsed["provider_time_hhmmss"] == "101530"
    assert parsed["source_as_of"] == "2026-07-15T02:02:03+00:00"
    assert parsed["best_ask"] == 70_100
    assert parsed["best_bid"] == 69_900
    assert parsed["spread"] == 200
    assert parsed["mid_price"] == 70_000
    assert parsed["depth_total_shares"] == 2_165
    assert parsed["units"]["levels.ask_price"] == "KRW/share"
    assert parsed["derived_schema_version"] == "1"


def test_orderbook_parser_rejects_fractional_share_quantity() -> None:
    from core.market_kis import parse_kis_orderbook_payload

    payload = _orderbook_payload()
    payload["output1"]["askp_rsqn1"] = "1.5"

    with pytest.raises(ValueError, match="numeric"):
        parse_kis_orderbook_payload(payload, "005930.KS", "J", _utc(2))


class _Response:
    def __init__(self, payload: object, *, status_code: int = 200, http_error: bool = False):
        self._payload = payload
        self.status_code = status_code
        self._http_error = http_error

    def raise_for_status(self) -> None:
        if self._http_error:
            raise RuntimeError("secret provider body")

    def json(self) -> object:
        return self._payload


class _Clock:
    def __init__(self, *values: datetime):
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


def test_fetch_orderbook_success_uses_official_contract_once() -> None:
    from core.market_kis import fetch_domestic_orderbook_result

    calls: list[dict] = []

    def http_get(url: str, **kwargs: object) -> _Response:
        calls.append({"url": url, **kwargs})
        return _Response(_orderbook_payload())

    result = fetch_domestic_orderbook_result(
        "005930.KS",
        clock=_Clock(_utc(1), _utc(2)),
        http_get=http_get,
        token_provider=lambda: "do-not-store-this-token",
        configured=lambda: True,
    )

    assert result.status == "success"
    assert result.error_type == "none"
    assert result.cache_source == "network"
    assert result.endpoint.endswith("/inquire-asking-price-exp-ccn")
    assert result.tr_id == "FHKST01010200"
    assert result.symbol == "005930"
    assert result.value["source_as_of"] == _utc(2).isoformat()
    assert len(calls) == 1
    assert calls[0]["headers"]["tr_id"] == "FHKST01010200"
    assert calls[0]["params"] == {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": "005930",
    }


def test_legacy_orderbook_rejects_fractional_share_quantity(monkeypatch) -> None:
    import core.market_kis as kis
    from core.market_data_fetch import CacheSource, FetchErrorType, FetchResult, FetchStatus

    value = {
        "levels": [
            {
                "level": level,
                "ask_price": 70_000 + level,
                "ask_size": 1.5 if level == 1 else 10,
                "bid_price": 70_000 - level,
                "bid_size": 10,
            }
            for level in range(1, 11)
        ]
    }
    forged = FetchResult(
        status=FetchStatus.SUCCESS,
        provider="KIS",
        endpoint="/endpoint",
        tr_id="TR",
        venue="J",
        symbol="005930",
        started_at_utc=_utc(1),
        completed_at_utc=_utc(2),
        error_type=FetchErrorType.NONE,
        cache_source=CacheSource.NETWORK,
        fallback_used=False,
        value=value,
    )
    monkeypatch.setattr(kis, "fetch_domestic_orderbook_result", lambda _ticker: forged)

    assert kis.get_domestic_orderbook("005930.KS") is None


def test_fetch_orderbook_without_credentials_is_skipped_without_token_or_http() -> None:
    from core.market_kis import fetch_domestic_orderbook_result

    def forbidden() -> str:
        raise AssertionError("dependency must not be called")

    result = fetch_domestic_orderbook_result(
        "005930.KS",
        clock=_Clock(_utc(1), _utc(2)),
        http_get=forbidden,
        token_provider=forbidden,
        configured=False,
    )

    assert result.status == "skipped"
    assert result.error_type == "not_configured"
    assert result.cache_source == "none"
    assert result.value is None


@pytest.mark.parametrize(
    "fetcher_name",
    ["fetch_domestic_orderbook_result", "fetch_domestic_investor_result"],
)
def test_default_token_network_failure_is_typed_network_without_raw_log(
    monkeypatch, caplog, fetcher_name
) -> None:
    import core.market_kis as kis

    monkeypatch.setattr(kis, "_mem_token", "")
    monkeypatch.setattr(kis, "_mem_expires", 0.0)
    monkeypatch.setattr(kis, "_mem_token_fail_until", 0.0)
    monkeypatch.setattr(kis, "_mem_token_fail_error_type", None, raising=False)
    monkeypatch.setattr(kis, "_load_token_from_file", lambda: ("", 0.0))
    market_get_calls = []
    token_post_calls = []

    def token_post(*_args, **_kwargs):
        token_post_calls.append(True)
        raise kis.requests.ConnectionError("RAW_PROVIDER_SECRET")

    def market_get(*args, **kwargs):
        market_get_calls.append((args, kwargs))
        raise AssertionError("market GET must not run")

    monkeypatch.setattr(kis.requests, "post", token_post)
    fetcher = getattr(kis, fetcher_name)
    result = fetcher(
        "005930.KS",
        clock=_Clock(_utc(1), _utc(2)),
        http_get=market_get,
        configured=True,
    )

    assert (result.status, result.error_type, result.cache_source) == (
        "failed",
        "network",
        "network",
    )
    assert market_get_calls == []
    second = fetcher(
        "005930.KS",
        clock=_Clock(_utc(3), _utc(4)),
        http_get=market_get,
        configured=True,
    )
    assert (second.status, second.error_type) == ("failed", "network")
    assert token_post_calls == [True]
    assert "ConnectionError" in caplog.text
    assert "RAW_PROVIDER_SECRET" not in caplog.text


@pytest.mark.parametrize(
    "fetcher_name",
    ["fetch_domestic_orderbook_result", "fetch_domestic_investor_result"],
)
@pytest.mark.parametrize(
    ("failure_kind", "expected_error"),
    [
        (401, "auth"),
        (403, "auth"),
        (429, "http"),
        (503, "http"),
        ("connection", "network"),
        ("timeout", "network"),
    ],
)
def test_default_token_endpoint_failure_class_survives_cooldown_without_raw_log(
    monkeypatch, caplog, fetcher_name, failure_kind, expected_error
) -> None:
    import core.market_kis as kis

    monkeypatch.setattr(kis, "_mem_token", "")
    monkeypatch.setattr(kis, "_mem_expires", 0.0)
    monkeypatch.setattr(kis, "_mem_token_fail_until", 0.0)
    monkeypatch.setattr(kis, "_mem_token_fail_error_type", None)
    monkeypatch.setattr(kis, "_load_token_from_file", lambda: ("", 0.0))
    token_post_calls = []
    market_get_calls = []

    def token_post(*_args, **_kwargs):
        token_post_calls.append(True)
        if failure_kind == "connection":
            raise kis.requests.ConnectionError("RAW_TOKEN_EXCEPTION_TEXT")
        if failure_kind == "timeout":
            raise kis.requests.Timeout("RAW_TOKEN_EXCEPTION_TEXT")
        response = kis.requests.Response()
        response.status_code = failure_kind
        response.url = "https://provider.invalid/oauth2/tokenP"
        response._content = b"RAW_TOKEN_BODY"
        return response

    def market_get(*args, **kwargs):
        market_get_calls.append((args, kwargs))
        raise AssertionError("market GET must not run")

    monkeypatch.setattr(kis.requests, "post", token_post)
    fetcher = getattr(kis, fetcher_name)
    first = fetcher(
        "005930.KS",
        clock=_Clock(_utc(1), _utc(2)),
        http_get=market_get,
        configured=True,
    )
    second = fetcher(
        "005930.KS",
        clock=_Clock(_utc(3), _utc(4)),
        http_get=market_get,
        configured=True,
    )

    assert (first.status, first.error_type) == ("failed", expected_error)
    assert (second.status, second.error_type) == ("failed", expected_error)
    assert token_post_calls == [True]
    assert market_get_calls == []
    assert "RAW_TOKEN_BODY" not in caplog.text
    assert "RAW_TOKEN_EXCEPTION_TEXT" not in caplog.text


@pytest.mark.parametrize(
    ("token_provider", "http_get", "payload", "expected_error"),
    [
        (lambda: None, lambda *_a, **_k: None, None, "auth"),
        (
            lambda: "token",
            lambda *_a, **_k: (_ for _ in ()).throw(ConnectionError("raw network secret")),
            None,
            "network",
        ),
        (
            lambda: "token",
            lambda *_a, **_k: _Response({}, status_code=503, http_error=True),
            None,
            "http",
        ),
        (
            lambda: "token",
            lambda *_a, **_k: _Response({"rt_cd": "1", "msg1": "raw provider secret"}),
            None,
            "provider",
        ),
        (
            lambda: "token",
            lambda *_a, **_k: _Response("not-a-mapping"),
            None,
            "malformed",
        ),
        (
            lambda: "token",
            lambda *_a, **_k: _Response(
                {
                    **_orderbook_payload(),
                    "output1": {
                        **_orderbook_payload()["output1"],
                        "askp1": "not-a-number",
                    },
                }
            ),
            None,
            "numeric",
        ),
    ],
)
def test_fetch_orderbook_failure_classes_are_distinct_and_safe(
    token_provider, http_get, payload, expected_error: str, caplog
) -> None:
    from core.market_kis import fetch_domestic_orderbook_result

    result = fetch_domestic_orderbook_result(
        "005930.KS",
        clock=_Clock(_utc(1), _utc(2)),
        http_get=http_get,
        token_provider=token_provider,
        configured=True,
    )

    assert result.status == "failed"
    assert result.error_type == expected_error
    assert result.value is None
    assert "raw network secret" not in caplog.text
    assert "raw provider secret" not in caplog.text


def test_zero_depth_orderbook_is_incomplete_not_healthy_liquidity() -> None:
    from core.market_kis import fetch_domestic_orderbook_result

    payload = _orderbook_payload()
    for level in range(1, 11):
        payload["output1"][f"askp_rsqn{level}"] = "0"
        payload["output1"][f"bidp_rsqn{level}"] = "0"

    result = fetch_domestic_orderbook_result(
        "005930.KS",
        clock=_Clock(_utc(1), _utc(2)),
        http_get=lambda *_a, **_k: _Response(payload),
        token_provider=lambda: "token",
        configured=True,
    )

    assert result.status == "incomplete"
    assert result.error_type == "zero_depth"
    assert result.value["depth_status"] == "zero_depth"
    assert result.value["imbalance"] is None
    assert "liquidity_label" not in result.value


def test_legacy_orderbook_projects_typed_success_to_exact_five_level_shape(monkeypatch) -> None:
    import core.market_kis as kis

    typed = kis.fetch_domestic_orderbook_result(
        "005930.KS",
        clock=_Clock(_utc(1), _utc(2)),
        http_get=lambda *_a, **_k: _Response(_orderbook_payload()),
        token_provider=lambda: "token",
        configured=True,
    )
    calls: list[str] = []

    def typed_fetch(ticker: str):
        calls.append(ticker)
        return typed

    monkeypatch.setattr(kis, "fetch_domestic_orderbook_result", typed_fetch)
    legacy = kis.get_domestic_orderbook("005930.KS")

    assert calls == ["005930.KS"]
    assert legacy == {
        "ticker": "005930.KS",
        "source": "KIS",
        "updated_at": "2026-07-15T11:02:03",
        "bids": [
            {"price": 69_900, "size": 102},
            {"price": 69_800, "size": 104},
            {"price": 69_700, "size": 106},
            {"price": 69_600, "size": 108},
            {"price": 69_500, "size": 110},
        ],
        "asks": [
            {"price": 70_100, "size": 101},
            {"price": 70_200, "size": 102},
            {"price": 70_300, "size": 103},
            {"price": 70_400, "size": 104},
            {"price": 70_500, "size": 105},
        ],
        "spread": 200,
        "spread_pct": 0.286,
        "mid_price": 70_000,
        "total_bid_size": 530,
        "total_ask_size": 515,
        "imbalance_pct": 1.4,
        "liquidity_label": "유동성 보통",
        "execution_risk_label": "스프레드 주의",
        "error": "",
    }


def test_legacy_orderbook_returns_none_for_non_success(monkeypatch) -> None:
    import core.market_kis as kis

    typed = kis.fetch_domestic_orderbook_result(
        "005930.KS",
        clock=_Clock(_utc(1), _utc(2)),
        http_get=lambda *_a, **_k: _Response("broken"),
        token_provider=lambda: "token",
        configured=True,
    )
    monkeypatch.setattr(kis, "fetch_domestic_orderbook_result", lambda _ticker: typed)

    assert kis.get_domestic_orderbook("005930.KS") is None


def _investor_payload() -> dict:
    return {
        "rt_cd": "0",
        "output": [
            {
                "stck_bsop_date": "20260714",
                "stck_clpr": "70100",
                "prdy_vrss": "-200",
                "prdy_vrss_sign": "5",
                "prsn_ntby_qty": "-1000",
                "frgn_ntby_qty": "600",
                "orgn_ntby_qty": "400",
                "prsn_ntby_tr_pbmn": "-70",
                "frgn_ntby_tr_pbmn": "42",
                "orgn_ntby_tr_pbmn": "28",
                "prsn_shnu_vol": "1100",
                "frgn_shnu_vol": "2600",
                "orgn_shnu_vol": "3400",
                "prsn_shnu_tr_pbmn": "77",
                "frgn_shnu_tr_pbmn": "182",
                "orgn_shnu_tr_pbmn": "238",
                "prsn_seln_vol": "2100",
                "frgn_seln_vol": "2000",
                "orgn_seln_vol": "3000",
                "prsn_seln_tr_pbmn": "147",
                "frgn_seln_tr_pbmn": "140",
                "orgn_seln_tr_pbmn": "210",
            }
        ],
    }


def test_parse_kis_investor_keeps_all_official_fields_units_and_business_date_time() -> None:
    from core.market_kis import parse_kis_investor_payload

    rows = parse_kis_investor_payload(_investor_payload(), "005930.KS", "J")

    assert len(rows) == 1
    row = rows[0]
    assert row["date"] == "20260714"
    assert row["close"] == 70_100
    assert row["previous_day_change"] == -200
    assert row["previous_day_sign"] == "5"
    assert row["personal_net_qty"] == -1_000
    assert row["foreign_net_qty"] == 600
    assert row["institution_net_qty"] == 400
    assert row["personal_net_trade_amount"] == -70
    assert row["foreign_buy_volume"] == 2_600
    assert row["institution_sell_trade_amount"] == 210
    assert row["source_as_of"] == "2026-07-14T06:30:00+00:00"
    assert row["source_as_of_precision"] == "business_date"
    assert row["availability_as_of"] is None
    assert row["intraday"] is False
    assert row["units"]["foreign_net_trade_amount"] == "KRW million"
    assert set(row["official_fields"]) == {
        "stck_bsop_date", "stck_clpr", "prdy_vrss", "prdy_vrss_sign",
        "prsn_ntby_qty", "frgn_ntby_qty", "orgn_ntby_qty",
        "prsn_ntby_tr_pbmn", "frgn_ntby_tr_pbmn", "orgn_ntby_tr_pbmn",
        "prsn_shnu_vol", "frgn_shnu_vol", "orgn_shnu_vol",
        "prsn_shnu_tr_pbmn", "frgn_shnu_tr_pbmn", "orgn_shnu_tr_pbmn",
        "prsn_seln_vol", "frgn_seln_vol", "orgn_seln_vol",
        "prsn_seln_tr_pbmn", "frgn_seln_tr_pbmn", "orgn_seln_tr_pbmn",
    }


def test_investor_parser_rejects_fractional_share_quantity() -> None:
    from core.market_kis import parse_kis_investor_payload

    payload = _investor_payload()
    payload["output"][0]["frgn_ntby_qty"] = "1.5"

    with pytest.raises(ValueError, match="numeric"):
        parse_kis_investor_payload(payload, "005930.KS", "J")


def test_fetch_kis_investor_uses_official_endpoint_and_availability_completion() -> None:
    from core.market_kis import fetch_domestic_investor_result

    calls: list[dict] = []

    def get(url: str, **kwargs: object) -> _Response:
        calls.append({"url": url, **kwargs})
        return _Response(_investor_payload())

    result = fetch_domestic_investor_result(
        "005930.KS",
        clock=_Clock(_utc(1), _utc(2)),
        http_get=get,
        token_provider=lambda: "token",
        configured=True,
    )

    assert result.status == "success"
    assert result.tr_id == "FHKST01010900"
    assert result.endpoint.endswith("/inquire-investor")
    assert result.value[0]["availability_as_of"] == _utc(2).isoformat()
    assert result.value[0]["source_as_of"] != result.value[0]["availability_as_of"]
    assert len(calls) == 1
    assert calls[0]["headers"]["tr_id"] == "FHKST01010900"


def test_fetch_kis_investor_skips_future_close_provisional_row_before_numeric_parse() -> None:
    from core.market_kis import fetch_domestic_investor_result

    past = dict(_investor_payload()["output"][0])
    past["stck_bsop_date"] = "20260718"
    future = {field: "" for field in past}
    future.update({"stck_bsop_date": "20260721", "prdy_vrss_sign": "3"})
    payload = {"rt_cd": "0", "output": [future, past]}
    started = datetime(2026, 7, 21, 3, 59, tzinfo=timezone.utc)
    completed = datetime(2026, 7, 21, 4, 0, tzinfo=timezone.utc)

    result = fetch_domestic_investor_result(
        "005930.KS",
        clock=_Clock(started, completed),
        http_get=lambda *_a, **_k: _Response(payload),
        token_provider=lambda: "token",
        configured=True,
    )

    assert (result.status, result.error_type) == ("success", "none")
    assert isinstance(result.value, list)
    assert [row["date"] for row in result.value] == ["20260718"]
    assert result.value[0]["availability_as_of"] == completed.isoformat()


def test_fetch_kis_investor_valid_empty_is_empty() -> None:
    from core.market_kis import fetch_domestic_investor_result

    result = fetch_domestic_investor_result(
        "005930.KS",
        clock=_Clock(_utc(1), _utc(2)),
        http_get=lambda *_a, **_k: _Response({"rt_cd": "0", "output": []}),
        token_provider=lambda: "token",
        configured=True,
    )

    assert result.status == "empty"
    assert result.error_type == "none"
    assert result.value == []


def test_fetch_kis_investor_malformed_and_numeric_are_distinct() -> None:
    from core.market_kis import fetch_domestic_investor_result

    malformed = fetch_domestic_investor_result(
        "005930.KS",
        clock=_Clock(_utc(1), _utc(2)),
        http_get=lambda *_a, **_k: _Response({"rt_cd": "0"}),
        token_provider=lambda: "token",
        configured=True,
    )
    bad = _investor_payload()
    bad["output"][0]["frgn_ntby_qty"] = "bad-number"
    numeric = fetch_domestic_investor_result(
        "005930.KS",
        clock=_Clock(_utc(1), _utc(2)),
        http_get=lambda *_a, **_k: _Response(bad),
        token_provider=lambda: "token",
        configured=True,
    )

    assert (malformed.status, malformed.error_type) == ("failed", "malformed")
    assert (numeric.status, numeric.error_type) == ("failed", "numeric")


def test_fetch_kis_investor_without_credentials_makes_no_request() -> None:
    from core.market_kis import fetch_domestic_investor_result

    def forbidden(*_args, **_kwargs):
        raise AssertionError("must not call")

    result = fetch_domestic_investor_result(
        "005930.KS",
        clock=_Clock(_utc(1), _utc(2)),
        http_get=forbidden,
        token_provider=forbidden,
        configured=lambda: False,
    )

    assert (result.status, result.error_type) == ("skipped", "not_configured")
