"""관세청 공식 월간 HS30 수출 provider의 shadow-only 계약."""

from datetime import datetime, timezone
from email.message import Message
import hashlib
from io import BytesIO
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError

import pytest


UTC = timezone.utc


def _taxonomy():
    return {
        "taxonomy_id": "un-comtrade-hs2022-chapter30-v1",
        "factor_name": "pharmaceutical_products_hs30",
        "classification_system": "HS",
        "classification_reference_code": "H6",
        "classification_version": "HS2022",
        "taxonomy_effective_from_period": "202201",
        "taxonomy_effective_to_period": None,
        "classification_codes": ["30"],
        "official_label": "Pharmaceutical products",
        "taxonomy_evidence_uri": (
            "https://comtradeapi.un.org/files/v1/app/reference/H6.json"
        ),
        "taxonomy_evidence_sha256": "a" * 64,
        "taxonomy_evidence_available_at_utc": "2026-07-17T13:00:00.000000Z",
        "is_broad_biotechnology": False,
        "shadow_only": True,
        "eligible_for_production_score": False,
    }


def _success_xml():
    return """<?xml version="1.0" encoding="UTF-8"?>
<response>
  <header><resultCode>00</resultCode><resultMsg>OK</resultMsg></header>
  <body><items>
    <item>
      <year>2026.06</year><balPayments>-100</balPayments>
      <expDlr>123456</expDlr><expWgt>789</expWgt><hsCode>30</hsCode>
      <impDlr>123556</impDlr><impWgt>790</impWgt>
      <statKor>의료용품</statKor>
    </item>
  </items></body>
</response>""".encode("utf-8")


def test_parse_official_itemtrade_hs30_success_is_shadow_only():
    from core.customs_hs_exports import parse_hs_export_xml

    rows = parse_hs_export_xml(
        _success_xml(),
        start_yymm="202606",
        end_yymm="202606",
        requested_hs_code="30",
        taxonomy=_taxonomy(),
        available_at_utc=datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
    )

    assert rows == [
        {
            "factor_name": "pharmaceutical_products_hs30",
            "period_year": 2026,
            "period_month": 6,
            "period_yymm": "202606",
            "hs_code": "30",
            "source_label_ko": "의료용품",
            "export_amount_usd": 123456,
            "export_weight_kg": 789,
            "import_amount_usd": 123556,
            "import_weight_kg": 790,
            "trade_balance_usd": -100,
            "classification_system": "HS",
            "classification_reference_code": "H6",
            "classification_version": "HS2022",
            "taxonomy_effective_from_period": "202201",
            "taxonomy_effective_to_period": None,
            "taxonomy_evidence_sha256": "a" * 64,
            "available_at_utc": "2026-07-17T14:00:00.000000Z",
            "shadow_only": True,
            "eligible_for_production_score": False,
        }
    ]


def test_http_403_is_typed_auth_failure_without_secret_echo():
    from core.customs_hs_exports import fetch_hs_export_result
    from core.market_data_fetch import FetchErrorType, FetchStatus

    secret = "synthetic-decoding-key"
    times = iter(
        [
            datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
            datetime(2026, 7, 17, 14, 0, 1, tzinfo=UTC),
        ]
    )

    def denied(url, **_kwargs):
        headers = Message()
        headers["Content-Type"] = "text/plain"
        raise HTTPError(
            url,
            403,
            "Forbidden",
            headers,
            BytesIO(b"forbidden"),
        )

    result = fetch_hs_export_result(
        "202606",
        "202606",
        service_key=secret,
        taxonomy=_taxonomy(),
        transport=denied,
        clock=lambda: next(times),
    )

    assert result.status is FetchStatus.FAILED
    assert result.error_type is FetchErrorType.AUTH
    assert result.value is None
    assert result.endpoint == "/getItemtradeList"
    assert secret not in repr(result)


def test_unexpected_transport_exception_cannot_exfiltrate_service_key():
    from core.customs_hs_exports import fetch_hs_export_result
    from core.market_data_fetch import FetchErrorType, FetchStatus

    secret = "unexpected-transport-secret"
    times = iter(
        [
            datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
            datetime(2026, 7, 17, 14, 0, 1, tzinfo=UTC),
        ]
    )

    def leaking_transport(url, **_kwargs):
        raise RuntimeError(f"transport rejected {url}")

    result = fetch_hs_export_result(
        "202606",
        "202606",
        service_key=secret,
        taxonomy=_taxonomy(),
        transport=leaking_transport,
        clock=lambda: next(times),
    )

    assert result.status is FetchStatus.FAILED
    assert result.error_type is FetchErrorType.PROVIDER
    assert result.value is None
    assert secret not in repr(result)


def test_returned_http_403_response_is_also_typed_auth_failure():
    from core.customs_hs_exports import fetch_hs_export_result
    from core.market_data_fetch import FetchErrorType, FetchStatus

    class ForbiddenResponse:
        status = 403
        headers = Message()

        def close(self):
            return None

    times = iter(
        [
            datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
            datetime(2026, 7, 17, 14, 0, 1, tzinfo=UTC),
        ]
    )
    result = fetch_hs_export_result(
        "202606",
        "202606",
        service_key="returned-http-secret",
        taxonomy=_taxonomy(),
        transport=lambda *_args, **_kwargs: ForbiddenResponse(),
        clock=lambda: next(times),
    )

    assert result.status is FetchStatus.FAILED
    assert result.error_type is FetchErrorType.AUTH
    assert result.value is None


def test_http_200_returns_normalized_rows_and_secret_free_lineage():
    from core.customs_hs_exports import fetch_hs_export_result
    from core.market_data_fetch import FetchStatus

    captured = {}
    raw = _success_xml()
    headers = Message()
    headers["Content-Type"] = "application/xml; charset=UTF-8"

    class Response:
        status = 200

        def __init__(self):
            self.headers = headers
            self.closed = False

        def read(self, limit):
            assert limit == 700_001
            return raw

        def close(self):
            self.closed = True

    response = Response()

    def transport(url, *, timeout):
        captured["url"] = url
        captured["timeout"] = timeout
        return response

    times = iter(
        [
            datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
            datetime(2026, 7, 17, 14, 0, 1, tzinfo=UTC),
        ]
    )
    secret = "encoded+/=key"
    result = fetch_hs_export_result(
        "202606",
        "202606",
        service_key=secret,
        taxonomy=_taxonomy(),
        transport=transport,
        timeout=3.5,
        clock=lambda: next(times),
    )

    assert result.status is FetchStatus.SUCCESS
    assert result.error_type.value == "none"
    assert response.closed is True
    assert captured["timeout"] == 3.5
    query = parse_qs(urlsplit(captured["url"]).query)
    assert query == {
        "serviceKey": [secret],
        "strtYymm": ["202606"],
        "endYymm": ["202606"],
        "hsSgn": ["30"],
    }
    assert result.value is not None
    assert result.value["request_params"] == {
        "strtYymm": "202606",
        "endYymm": "202606",
        "hsSgn": "30",
    }
    assert result.value["raw_response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result.value["rows"][0]["export_amount_usd"] == 123456
    assert secret not in repr(result)


@pytest.mark.parametrize(
    ("mutated", "error"),
    (
        (
            lambda raw: b"<!DOCTYPE response [<!ENTITY x 'y'>]>" + raw,
            "customs_hs_xml_dtd_forbidden",
        ),
        (
            lambda raw: raw.replace(b"</item>", b"<extra>1</extra></item>"),
            "customs_hs_xml_structure_invalid",
        ),
        (
            lambda raw: raw.replace(b"<hsCode>30</hsCode>", b"<hsCode>31</hsCode>"),
            "customs_hs_code_invalid",
        ),
        (
            lambda raw: raw.replace(b"<year>2026.06</year>", b"<year>2026.07</year>"),
            "customs_hs_period_invalid",
        ),
    ),
    ids=("dtd", "unknown_field", "mixed_hs", "outside_period"),
)
def test_hs_xml_mutations_fail_closed(mutated, error):
    from core.customs_hs_exports import parse_hs_export_xml

    with pytest.raises(ValueError, match=error):
        parse_hs_export_xml(
            mutated(_success_xml()),
            start_yymm="202606",
            end_yymm="202606",
            requested_hs_code="30",
            taxonomy=_taxonomy(),
            available_at_utc=datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("field", "mutated"),
    (
        ("taxonomy_id", "forged-taxonomy"),
        ("taxonomy_effective_from_period", "201601"),
        ("taxonomy_effective_to_period", "202112"),
        ("taxonomy_evidence_uri", "https://example.com/H6.json"),
        ("is_broad_biotechnology", True),
        ("taxonomy_evidence_available_at_utc", "2026-07-17T15:00:00.000000Z"),
    ),
)
def test_hs_parser_rejects_forged_or_future_taxonomy(field, mutated):
    from core.customs_hs_exports import parse_hs_export_xml

    taxonomy = _taxonomy()
    taxonomy[field] = mutated
    with pytest.raises(ValueError, match="customs_hs_taxonomy_invalid"):
        parse_hs_export_xml(
            _success_xml(),
            start_yymm="202606",
            end_yymm="202606",
            requested_hs_code="30",
            taxonomy=taxonomy,
            available_at_utc=datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
        )


class _ShaLike:
    def __str__(self):
        return "a" * 64


class _ExplodingSha:
    def __str__(self):
        raise RuntimeError("must-not-escape")


class _ExplodingEq:
    def __eq__(self, _other):
        raise RuntimeError("must-not-escape")

    def __ne__(self, _other):
        raise RuntimeError("must-not-escape")


@pytest.mark.parametrize("invalid_sha", (_ShaLike(), _ExplodingSha()))
def test_hs_taxonomy_sha_must_be_exact_string_without_coercion(invalid_sha):
    from core.customs_hs_exports import parse_hs_export_xml

    taxonomy = _taxonomy()
    taxonomy["taxonomy_evidence_sha256"] = invalid_sha
    with pytest.raises(ValueError, match="customs_hs_taxonomy_invalid"):
        parse_hs_export_xml(
            _success_xml(),
            start_yymm="202606",
            end_yymm="202606",
            requested_hs_code="30",
            taxonomy=taxonomy,
            available_at_utc=datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
        )


def test_hs_taxonomy_objects_cannot_trigger_custom_equality():
    from core.customs_hs_exports import parse_hs_export_xml

    taxonomy = _taxonomy()
    taxonomy["factor_name"] = _ExplodingEq()
    with pytest.raises(ValueError, match="customs_hs_taxonomy_invalid"):
        parse_hs_export_xml(
            _success_xml(),
            start_yymm="202606",
            end_yymm="202606",
            requested_hs_code="30",
            taxonomy=taxonomy,
            available_at_utc=datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
        )


def test_http_error_close_failure_cannot_exfiltrate_service_key():
    from core.customs_hs_exports import fetch_hs_export_result
    from core.market_data_fetch import FetchErrorType, FetchStatus

    secret = "close-cleanup-secret"

    class CloseFailureHTTPError(HTTPError):
        def close(self):
            raise RuntimeError(self.url)

    def denied(url, **_kwargs):
        raise CloseFailureHTTPError(url, 403, "Forbidden", Message(), BytesIO())

    times = iter(
        (
            datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
            datetime(2026, 7, 17, 14, 0, 1, tzinfo=UTC),
        )
    )
    result = fetch_hs_export_result(
        "202606",
        "202606",
        service_key=secret,
        taxonomy=_taxonomy(),
        transport=denied,
        clock=lambda: next(times),
    )

    assert result.status is FetchStatus.FAILED
    assert result.error_type is FetchErrorType.AUTH
    assert secret not in repr(result)


def test_http_500_classification_is_representation_independent():
    from core.customs_hs_exports import fetch_hs_export_result
    from core.market_data_fetch import FetchErrorType

    class Returned500:
        status = 500
        headers = Message()

        def close(self):
            pass

    def raised(url, **_kwargs):
        raise HTTPError(url, 500, "Server Error", Message(), BytesIO())

    results = []
    for transport in (raised, lambda *_args, **_kwargs: Returned500()):
        times = iter(
            (
                datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
                datetime(2026, 7, 17, 14, 0, 1, tzinfo=UTC),
            )
        )
        results.append(
            fetch_hs_export_result(
                "202606",
                "202606",
                service_key="http-parity-secret",
                taxonomy=_taxonomy(),
                transport=transport,
                clock=lambda: next(times),
            )
        )

    assert [result.error_type for result in results] == [
        FetchErrorType.HTTP,
        FetchErrorType.HTTP,
    ]


def test_hs2022_taxonomy_rejects_pre_2022_statistics_period():
    from core.customs_hs_exports import parse_hs_export_xml

    historical = _success_xml().replace(b"2026.06", b"2016.01")
    with pytest.raises(ValueError, match="customs_hs_(?:query_)?period_invalid"):
        parse_hs_export_xml(
            historical,
            start_yymm="201601",
            end_yymm="201601",
            requested_hs_code="30",
            taxonomy=_taxonomy(),
            available_at_utc=datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
        )


def test_future_statistics_period_is_rejected_against_availability():
    from core.customs_hs_exports import parse_hs_export_xml

    future = _success_xml().replace(b"2026.06", b"2099.01")
    with pytest.raises(ValueError, match="customs_hs_period_future"):
        parse_hs_export_xml(
            future,
            start_yymm="209901",
            end_yymm="209901",
            requested_hs_code="30",
            taxonomy=_taxonomy(),
            available_at_utc=datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
        )


def test_requested_month_range_must_be_complete():
    from core.customs_hs_exports import parse_hs_export_xml

    with pytest.raises(ValueError, match="customs_hs_period_incomplete"):
        parse_hs_export_xml(
            _success_xml(),
            start_yymm="202605",
            end_yymm="202606",
            requested_hs_code="30",
            taxonomy=_taxonomy(),
            available_at_utc=datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
        )
