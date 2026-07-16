"""관세청 10일 수출 공식 XML 계약."""

import base64
from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from core.customs_export import parse_customs_export_xml


def test_official_xml_maps_all_product_amount_fields():
    payload = b"""<?xml version="1.0" encoding="UTF-8"?>
    <response>
      <header><resultCode>00</resultCode><resultMsg>NORMAL SERVICE.</resultMsg></header>
      <body>
        <items>
          <item>
            <itemUsdAmt00>1,000</itemUsdAmt00>
            <itemUsdAmt01>100</itemUsdAmt01>
            <itemUsdAmt02>90</itemUsdAmt02>
            <itemUsdAmt03>80</itemUsdAmt03>
            <itemUsdAmt04>70</itemUsdAmt04>
            <itemUsdAmt05>60</itemUsdAmt05>
            <itemUsdAmt06>50</itemUsdAmt06>
            <itemUsdAmt07>40</itemUsdAmt07>
            <itemUsdAmt08>30</itemUsdAmt08>
            <itemUsdAmt09>20</itemUsdAmt09>
            <itemUsdAmt10>10</itemUsdAmt10>
            <priodDt>01~10</priodDt>
            <priodMon>202607</priodMon>
            <priodYear>2026</priodYear>
          </item>
        </items>
        <totalCount>1</totalCount>
      </body>
    </response>"""

    result = parse_customs_export_xml(payload)

    assert result == [
        {
            "period_year": 2026,
            "period_month": 7,
            "period_end_day": 10,
            "period_kind": "day_10",
            "amounts_thousand_usd": {
                "total": 1000,
                "semiconductors": 100,
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
    ]


def test_total_count_mismatch_is_rejected_instead_of_becoming_clean_empty():
    payload = b"""<response>
      <header><resultCode>00</resultCode><resultMsg>NORMAL</resultMsg></header>
      <body><items></items><totalCount>1</totalCount></body>
    </response>"""

    with pytest.raises(ValueError, match="customs_total_count_mismatch"):
        parse_customs_export_xml(payload)


def test_fetch_without_service_key_is_typed_skip_and_never_calls_transport():
    from core.customs_export import fetch_customs_export_result
    from core.market_data_fetch import FetchErrorType, FetchStatus

    calls = []
    result = fetch_customs_export_result(
        "202507",
        "202607",
        service_key="",
        transport=lambda *_args, **_kwargs: calls.append(True),
    )

    assert calls == []
    assert result.status is FetchStatus.SKIPPED
    assert result.error_type is FetchErrorType.NOT_CONFIGURED
    assert result.value is None


def test_oversized_service_key_is_rejected_before_url_or_transport():
    from core.customs_export import (
        MAX_CUSTOMS_SERVICE_KEY_BYTES,
        fetch_customs_export_result,
    )
    from core.market_data_fetch import FetchErrorType, FetchStatus

    calls = []
    result = fetch_customs_export_result(
        "202607",
        "202607",
        service_key="x" * (MAX_CUSTOMS_SERVICE_KEY_BYTES + 1),
        transport=lambda *_args, **_kwargs: calls.append(True),
    )

    assert calls == []
    assert result.status is FetchStatus.FAILED
    assert result.error_type is FetchErrorType.MALFORMED
    assert result.value is None


def _official_success_xml(*, day="01~10", total_count="1"):
    amount_nodes = "".join(
        f"<itemUsdAmt{index:02d}>{1000 - index * 10}</itemUsdAmt{index:02d}>"
        for index in range(11)
    )
    return (
        "<response><header><resultCode>00</resultCode><resultMsg>NORMAL</resultMsg>"
        "</header><body><items><item>"
        f"{amount_nodes}<priodDt>{day}</priodDt><priodMon>202607</priodMon>"
        f"<priodYear>2026</priodYear></item></items><totalCount>{total_count}"
        "</totalCount></body></response>"
    ).encode()


def test_success_fetch_uses_exact_query_and_post_read_completion_clock():
    from core.customs_export import fetch_customs_export_result
    from core.market_data_fetch import CacheSource, FetchErrorType, FetchStatus

    started = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
    completed = started + timedelta(seconds=2)
    clock_values = iter((started, completed))
    calls = []

    class Response:
        status = 200
        headers = {"Content-Type": "application/xml"}

        def read(self, limit):
            assert limit > len(_official_success_xml())
            return _official_success_xml()

        def close(self):
            pass

    def transport(req, *, timeout):
        calls.append((req.full_url, timeout))
        return Response()

    result = fetch_customs_export_result(
        "202507",
        "202607",
        service_key="encoded+/=key",
        transport=transport,
        timeout=3.5,
        clock=lambda: next(clock_values),
    )

    assert calls == [
        (
            "https://apis.data.go.kr/1220000/prlstMmUtPrviExpAcrs/"
            "getPrlstMmUtPrviExpAcrs?serviceKey=encoded%2B%2F%3Dkey&"
            "strtYymm=202507&endYymm=202607",
            3.5,
        )
    ]
    assert result.status is FetchStatus.SUCCESS
    assert result.error_type is FetchErrorType.NONE
    assert result.cache_source is CacheSource.NETWORK
    assert result.started_at_utc == started
    assert result.completed_at_utc == completed
    assert result.source_fetched_at_utc == completed
    assert result.endpoint == "/getPrlstMmUtPrviExpAcrs"
    assert "encoded" not in result.endpoint
    assert result.value is not None
    value = result.value
    items = value["items"]
    assert isinstance(items, list)
    assert items[0]["period_end_day"] == 10
    raw = _official_success_xml()
    raw_xml_base64 = value["raw_xml_base64"]
    assert isinstance(raw_xml_base64, str)
    assert base64.b64decode(raw_xml_base64) == raw
    assert value["raw_xml_sha256"] == hashlib.sha256(raw).hexdigest()
    assert value["request_params"] == {
        "strtYymm": "202507",
        "endYymm": "202607",
    }


def test_network_exception_is_typed_failure_without_raw_exception_text():
    from urllib.error import URLError

    from core.customs_export import fetch_customs_export_result
    from core.market_data_fetch import FetchErrorType, FetchStatus

    moments = iter(
        (
            datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 16, 0, 0, 1, tzinfo=timezone.utc),
        )
    )

    def transport(*_args, **_kwargs):
        raise URLError("serviceKey=must-not-escape")

    result = fetch_customs_export_result(
        "202607",
        "202607",
        service_key="synthetic-key",
        transport=transport,
        clock=lambda: next(moments),
    )

    assert result.status is FetchStatus.FAILED
    assert result.error_type is FetchErrorType.NETWORK
    assert result.value is None
    assert "must-not-escape" not in repr(result)


def test_normal_success_with_zero_total_count_is_clean_empty():
    from core.customs_export import fetch_customs_export_result
    from core.market_data_fetch import FetchErrorType, FetchStatus

    moments = iter(
        (
            datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 16, 0, 0, 1, tzinfo=timezone.utc),
        )
    )

    class Response:
        status = 200
        headers = {"Content-Type": "application/xml"}

        def read(self, _limit):
            return (
                b"<response><header><resultCode>00</resultCode>"
                b"<resultMsg>NORMAL SERVICE.</resultMsg></header>"
                b"<body><items/><totalCount>0</totalCount></body></response>"
            )

        def close(self):
            pass

    result = fetch_customs_export_result(
        "202607",
        "202607",
        service_key="synthetic-key",
        transport=lambda *_args, **_kwargs: Response(),
        clock=lambda: next(moments),
    )

    assert result.status is FetchStatus.EMPTY
    assert result.error_type is FetchErrorType.NONE
    assert result.value is not None
    assert result.value["items"] == []
    assert result.value["total_count"] == 0


@pytest.mark.parametrize("result_code", ("02", "03"))
def test_documented_auth_codes_are_typed_auth_failures(result_code):
    from core.customs_export import fetch_customs_export_result
    from core.market_data_fetch import FetchErrorType, FetchStatus

    moments = iter(
        (
            datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 16, 0, 0, 1, tzinfo=timezone.utc),
        )
    )
    payload = (
        f"<response><header><resultCode>{result_code}</resultCode>"
        "<resultMsg>request detail must not escape</resultMsg>"
        "</header><body><items/><totalCount>0</totalCount></body></response>"
    ).encode()

    class Response:
        status = 200
        headers = {"Content-Type": "application/xml"}

        def read(self, _limit):
            return payload

        def close(self):
            pass

    result = fetch_customs_export_result(
        "202607",
        "202607",
        service_key="synthetic-key",
        transport=lambda *_args, **_kwargs: Response(),
        clock=lambda: next(moments),
    )

    assert result.status is FetchStatus.FAILED
    assert result.error_type is FetchErrorType.AUTH
    assert "request detail" not in repr(result)


@pytest.mark.parametrize(
    ("priod_dt", "period_end_day", "period_kind"),
    (
        ("01~10", 10, "day_10"),
        ("01~20", 20, "day_20"),
        ("01~31", 31, "month_end"),
    ),
)
def test_documented_cumulative_period_labels_are_accepted(
    priod_dt, period_end_day, period_kind
):
    parsed = parse_customs_export_xml(_official_success_xml(day=priod_dt))

    assert parsed[0]["period_end_day"] == period_end_day
    assert parsed[0]["period_kind"] == period_kind


@pytest.mark.parametrize(
    ("priod_mon", "priod_dt"),
    (
        ("07", "01~10"),
        ("202607", "10"),
        ("202607", "01~15"),
        ("202607", "11~20"),
        ("202607", "21~31"),
        ("202607", "21~말일"),
    ),
)
def test_undocumented_period_shapes_are_rejected(priod_mon, priod_dt):
    payload = _official_success_xml(day=priod_dt).replace(b"202607", priod_mon.encode())

    with pytest.raises(ValueError, match="customs_period_"):
        parse_customs_export_xml(payload)


def test_invalid_query_period_is_rejected_before_transport():
    from core.customs_export import fetch_customs_export_result

    calls = []
    with pytest.raises(ValueError, match="customs_query_period_invalid"):
        fetch_customs_export_result(
            "202613",
            "202607",
            service_key="synthetic-key",
            transport=lambda *_args, **_kwargs: calls.append(True),
        )
    assert calls == []


def test_successful_plain_text_response_is_malformed_not_xml_success():
    from core.customs_export import fetch_customs_export_result
    from core.market_data_fetch import FetchErrorType, FetchStatus

    moments = iter(
        (
            datetime(2026, 7, 16, tzinfo=timezone.utc),
            datetime(2026, 7, 16, 0, 0, 1, tzinfo=timezone.utc),
        )
    )

    class Response:
        status = 200
        headers = {"Content-Type": "text/plain; charset=utf-8"}

        def read(self, _limit):
            return _official_success_xml()

        def close(self):
            pass

    result = fetch_customs_export_result(
        "202607",
        "202607",
        service_key="synthetic-key",
        transport=lambda *_args, **_kwargs: Response(),
        clock=lambda: next(moments),
    )

    assert result.status is FetchStatus.FAILED
    assert result.error_type is FetchErrorType.MALFORMED


def test_response_size_is_bounded_before_xml_parse():
    from core.customs_export import fetch_customs_export_result
    from core.market_data_fetch import FetchErrorType, FetchStatus

    read_limits = []
    moments = iter(
        (
            datetime(2026, 7, 16, tzinfo=timezone.utc),
            datetime(2026, 7, 16, 0, 0, 1, tzinfo=timezone.utc),
        )
    )

    class Response:
        status = 200
        headers = {"Content-Type": "application/xml"}

        def read(self, limit):
            read_limits.append(limit)
            return b"x" * limit

        def close(self):
            pass

    result = fetch_customs_export_result(
        "202607",
        "202607",
        service_key="synthetic-key",
        transport=lambda *_args, **_kwargs: Response(),
        clock=lambda: next(moments),
    )

    assert read_limits == [700_001]
    assert result.status is FetchStatus.FAILED
    assert result.error_type is FetchErrorType.MALFORMED


def _official_item_xml(*, period="202607", day="01~10", amount="1000"):
    amount_nodes = "".join(
        f"<itemUsdAmt{index:02d}>{amount if index == 0 else 1000 - index * 10}"
        f"</itemUsdAmt{index:02d}>"
        for index in range(11)
    )
    return (
        f"<item>{amount_nodes}<priodDt>{day}</priodDt>"
        f"<priodMon>{period}</priodMon><priodYear>{period[:4]}</priodYear></item>"
    )


def _success_payload(*, items=None, total_count="1"):
    item_xml = _official_item_xml() if items is None else items
    return (
        "<response><header><resultCode>00</resultCode><resultMsg>NORMAL</resultMsg>"
        f"</header><body><items>{item_xml}</items><totalCount>{total_count}"
        "</totalCount></body></response>"
    ).encode()


def _fetch_payload(
    payload,
    *,
    content_type="application/xml",
    service_key="synthetic-key",
    status=200,
):
    from core.customs_export import fetch_customs_export_result

    moments = iter(
        (
            datetime(2026, 7, 16, tzinfo=timezone.utc),
            datetime(2026, 7, 16, 0, 0, 1, tzinfo=timezone.utc),
        )
    )

    class Response:
        headers = {} if content_type is None else {"Content-Type": content_type}

        def read(self, _limit):
            return payload

        def close(self):
            pass

    Response.status = status
    return fetch_customs_export_result(
        "202607",
        "202607",
        service_key=service_key,
        transport=lambda *_args, **_kwargs: Response(),
        clock=lambda: next(moments),
    )


def test_documented_system_failure_code_is_provider_failure():
    from core.market_data_fetch import FetchErrorType, FetchStatus

    payload = (
        "<response><header><resultCode>01</resultCode>"
        "<resultMsg>PROVIDER FAILURE</resultMsg></header></response>"
    ).encode()

    result = _fetch_payload(payload)

    assert result.status is FetchStatus.FAILED
    assert result.error_type is FetchErrorType.PROVIDER
    assert result.value is None


def test_documented_missing_parameter_code_is_malformed_request_contract():
    from core.market_data_fetch import FetchErrorType, FetchStatus

    payload = (
        b"<response><header><resultCode>99</resultCode>"
        b"<resultMsg>REQUIRED PARAMETER MISSING</resultMsg></header></response>"
    )

    result = _fetch_payload(payload)

    assert result.status is FetchStatus.FAILED
    assert result.error_type is FetchErrorType.MALFORMED
    assert result.value is None
    assert "REQUIRED PARAMETER" not in repr(result)


def test_dtd_and_entity_declarations_are_rejected_before_xml_parse():
    payload = (
        b'<!DOCTYPE response [<!ENTITY normalCode "00">]>'
        + _success_payload().replace(b"<resultCode>00</resultCode>", b"<resultCode>&normalCode;</resultCode>")
    )

    with pytest.raises(ValueError, match="customs_xml_dtd_forbidden"):
        parse_customs_export_xml(payload)

    result = _fetch_payload(payload)
    from core.market_data_fetch import FetchErrorType, FetchStatus

    assert result.status is FetchStatus.FAILED
    assert result.error_type is FetchErrorType.MALFORMED
    assert result.value is None


def test_utf16_dtd_is_rejected_as_noncanonical_xml_encoding():
    payload = (
        '<!DOCTYPE response [<!ENTITY normalCode "00">]>'
        '<response><header><resultCode>&normalCode;</resultCode>'
        '<resultMsg>NORMAL</resultMsg></header><body><items/>'
        '<totalCount>0</totalCount></body></response>'
    ).encode("utf-16")

    with pytest.raises(ValueError, match="customs_xml_encoding_invalid"):
        parse_customs_export_xml(payload)


def test_direct_parser_rejects_payload_over_fetch_limit():
    from core.customs_export import MAX_CUSTOMS_RAW_BYTES

    with pytest.raises(ValueError, match="customs_xml_too_large"):
        parse_customs_export_xml(b"x" * (MAX_CUSTOMS_RAW_BYTES + 1))


def test_unknown_result_code_is_malformed_without_preserving_original_code():
    from core.market_data_fetch import FetchErrorType, FetchStatus

    payload = (
        b"<response><header><resultCode>77</resultCode>"
        b"<resultMsg>UNKNOWN</resultMsg></header></response>"
    )

    with pytest.raises(ValueError) as exc_info:
        parse_customs_export_xml(payload)
    assert "77" not in repr(exc_info.value)
    assert "77" not in repr(vars(exc_info.value))

    result = _fetch_payload(payload)
    assert result.status is FetchStatus.FAILED
    assert result.error_type is FetchErrorType.MALFORMED
    assert "77" not in repr(result)


@pytest.mark.parametrize(
    "payload",
    (
        _success_payload().replace(
            b"</header>",
            b"</header><header><resultCode>00</resultCode><resultMsg>NORMAL</resultMsg></header>",
        ),
        _success_payload().replace(b"</body>", b"</body><body/>"),
        _success_payload().replace(b"</response>", b"<unexpected/></response>"),
        _success_payload().replace(
            b"</header>", b"<resultCode>00</resultCode></header>"
        ),
        _success_payload().replace(b"</header>", b"<unexpected/></header>"),
        _success_payload().replace(b"<items>", b"<items/><items>", 1),
        _success_payload().replace(b"</totalCount>", b"</totalCount><totalCount>1</totalCount>"),
        _success_payload().replace(b"</body>", b"<unexpected/></body>"),
        _success_payload().replace(b"</items>", b"<unexpected/></items>"),
        _success_payload().replace(
            b"</item>", b"<itemUsdAmt00>1000</itemUsdAmt00></item>"
        ),
        _success_payload().replace(b"</item>", b"<unexpected/></item>"),
    ),
)
def test_duplicate_or_unexpected_xml_structure_is_rejected(payload):
    with pytest.raises(ValueError, match="customs_xml_structure_invalid"):
        parse_customs_export_xml(payload)


@pytest.mark.parametrize(
    ("content_type", "expected"),
    (
        ("application/xml", "application/xml"),
        ("text/xml; charset=UTF-8", "text/xml;charset=utf-8"),
        ("application/problem+xml", "application/problem+xml"),
        ('image/svg+xml; charset="utf-8"', "image/svg+xml;charset=utf-8"),
    ),
)
def test_only_exact_xml_media_types_are_accepted(content_type, expected):
    from core.market_data_fetch import FetchStatus

    result = _fetch_payload(_success_payload(), content_type=content_type)

    assert result.status is FetchStatus.SUCCESS
    assert result.value is not None
    assert result.value["content_type"] == expected


@pytest.mark.parametrize(
    "content_type",
    (
        None,
        "",
        "application/xmlish",
        "application/xml,text/plain",
        "text/plain; note=xml",
        "application/+xml",
        "application/xml ; broken",
        "application/vnd.api+xml evil",
        "application/xml; note=xml",
        "application/xml; charset=utf-8; charset=utf-8",
    ),
)
def test_missing_or_deceptive_content_type_is_malformed(content_type):
    from core.market_data_fetch import FetchErrorType, FetchStatus

    result = _fetch_payload(_success_payload(), content_type=content_type)

    assert result.status is FetchStatus.FAILED
    assert result.error_type is FetchErrorType.MALFORMED
    assert result.value is None
    assert "application/xml" not in repr(result)


def test_content_type_encoded_credential_is_auth_without_lineage_value():
    from core.market_data_fetch import FetchErrorType, FetchStatus

    service_key = "synthetic-content-type-secret"
    encoded = base64.b64encode(service_key.encode()).decode("ascii")
    result = _fetch_payload(
        _success_payload(),
        content_type=f"application/xml; note={encoded}",
        service_key=service_key,
    )

    assert result.status is FetchStatus.FAILED
    assert result.error_type is FetchErrorType.AUTH
    assert result.value is None
    assert service_key not in repr(result)
    assert encoded not in repr(result)


@pytest.mark.parametrize(
    ("service_key", "reflected"),
    (
        ("synthetic-secret", "synthetic-secret"),
        ("synthetic+secret", "synthetic%2Bsecret"),
        ("synthetic&secret", "synthetic&amp;secret"),
        (
            "synthetic-base64-secret",
            base64.b64encode(b"synthetic-base64-secret").decode("ascii"),
        ),
        (
            "synthetic-urlsafe-secret+/",
            base64.urlsafe_b64encode(b"synthetic-urlsafe-secret+/")
            .decode("ascii")
            .rstrip("="),
        ),
    ),
)
def test_reflected_service_key_variants_are_auth_without_raw_or_hash(
    monkeypatch, service_key, reflected
):
    from core import customs_export
    from core.market_data_fetch import FetchErrorType, FetchStatus

    hash_calls = []

    def forbidden_hash(_payload):
        hash_calls.append(True)
        raise AssertionError("hash must not be created for reflected credentials")

    monkeypatch.setattr(customs_export.hashlib, "sha256", forbidden_hash)
    payload = _success_payload().replace(b"NORMAL", reflected.encode())

    result = _fetch_payload(payload, service_key=service_key)

    assert hash_calls == []
    assert result.status is FetchStatus.FAILED
    assert result.error_type is FetchErrorType.AUTH
    assert result.value is None
    assert service_key not in repr(result)
    assert reflected not in repr(result)


def test_reflected_sensitive_field_name_is_malformed_before_hash(monkeypatch):
    from core import customs_export
    from core.market_data_fetch import FetchErrorType, FetchStatus

    hash_calls = []

    def forbidden_hash(_payload):
        hash_calls.append(True)
        raise AssertionError("hash must not be created for sensitive response fields")

    monkeypatch.setattr(customs_export.hashlib, "sha256", forbidden_hash)
    payload = _success_payload().replace(b"NORMAL", b"service_key=redacted")

    result = _fetch_payload(payload)

    assert hash_calls == []
    assert result.status is FetchStatus.FAILED
    assert result.error_type is FetchErrorType.MALFORMED
    assert result.value is None
    assert "service_key" not in repr(result)


def test_response_period_outside_requested_range_is_malformed():
    from core.market_data_fetch import FetchErrorType, FetchStatus

    result = _fetch_payload(
        _success_payload(items=_official_item_xml(period="202606"))
    )

    assert result.status is FetchStatus.FAILED
    assert result.error_type is FetchErrorType.MALFORMED


def test_official_left_padded_amount_field_is_accepted():
    parsed = parse_customs_export_xml(
        _success_payload(items=_official_item_xml(amount="          12,345,678"))
    )

    amounts = parsed[0]["amounts_thousand_usd"]
    assert isinstance(amounts, dict)
    assert amounts["total"] == 12_345_678


@pytest.mark.parametrize(
    "amount",
    ("01", "1,00", "01,000", "+1", "1.0", " 1 ", "1 ", "\t1", "١"),
)
def test_noncanonical_amount_formats_are_rejected(amount):
    with pytest.raises(ValueError, match="customs_amount_invalid"):
        parse_customs_export_xml(
            _success_payload(items=_official_item_xml(amount=amount))
        )


@pytest.mark.parametrize("total_count", ("01", "+1", "1,0", " 1 ", "١"))
def test_noncanonical_total_count_formats_are_rejected(total_count):
    with pytest.raises(ValueError, match="customs_total_count_invalid"):
        parse_customs_export_xml(_success_payload(total_count=total_count))


def test_duplicate_response_periods_are_rejected():
    duplicate_items = _official_item_xml() + _official_item_xml()

    with pytest.raises(ValueError, match="customs_period_duplicate"):
        parse_customs_export_xml(
            _success_payload(items=duplicate_items, total_count="2")
        )
