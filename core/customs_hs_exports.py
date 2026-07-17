"""관세청 Itemtrade HS 월간 수출 shadow provider."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import re
from typing import Any, Callable
from urllib import error, parse, request
from xml.etree import ElementTree

from core.market_data_fetch import CacheSource, FetchErrorType, FetchResult, FetchStatus
from core.customs_export import normalize_xml_content_type
from core.sensitive_text import sensitive_text_kind


CUSTOMS_HS_EXPORT_ENDPOINT = (
    "https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"
)
MAX_HS_EXPORT_RAW_BYTES = 700_000
_XML_DTD_OR_ENTITY = re.compile(
    rb"<![ \t\r\n]*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE
)
_UNSIGNED_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_SIGNED_INTEGER = re.compile(r"(?:0|-?[1-9][0-9]*)\Z")
_UTC_TEXT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z"
)
_TAXONOMY_ID = "un-comtrade-hs2022-chapter30-v1"
_TAXONOMY_URI = "https://comtradeapi.un.org/files/v1/app/reference/H6.json"
_HS2022_FIRST_PERIOD = (2022, 1)
_ITEM_FIELDS = frozenset(
    {
        "year",
        "balPayments",
        "expDlr",
        "expWgt",
        "hsCode",
        "impDlr",
        "impWgt",
        "statKor",
    }
)


def _exact_text(value: Any, expected: str) -> bool:
    return type(value) is str and value == expected


def _exact_chapter30(value: Any) -> bool:
    return (
        type(value) is list
        and len(value) == 1
        and type(value[0]) is str
        and value[0] == "30"
    )


def _utc_text(value: Any) -> str:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("customs_hs_available_at_invalid")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_utc_text(value: Any) -> datetime | None:
    if type(value) is not str or _UTC_TEXT.fullmatch(value) is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _period(value: Any) -> tuple[int, int, str]:
    if type(value) is not str or re.fullmatch(r"[0-9]{4}\.[0-9]{2}", value) is None:
        raise ValueError("customs_hs_period_invalid")
    year = int(value[:4])
    month = int(value[5:])
    if (year, month) < _HS2022_FIRST_PERIOD or not 1 <= month <= 12:
        raise ValueError("customs_hs_period_invalid")
    return year, month, f"{year:04d}{month:02d}"


def _query_period(value: Any) -> tuple[int, int]:
    if type(value) is not str or re.fullmatch(r"[0-9]{6}", value) is None:
        raise ValueError("customs_hs_query_period_invalid")
    year = int(value[:4])
    month = int(value[4:])
    if (year, month) < _HS2022_FIRST_PERIOD or not 1 <= month <= 12:
        raise ValueError("customs_hs_query_period_invalid")
    return year, month


def _expected_periods(start: tuple[int, int], end: tuple[int, int]) -> set[str]:
    start_index = start[0] * 12 + start[1] - 1
    end_index = end[0] * 12 + end[1] - 1
    return {
        f"{index // 12:04d}{index % 12 + 1:02d}"
        for index in range(start_index, end_index + 1)
    }


def _leaf(node: ElementTree.Element) -> str:
    if node.attrib or list(node) or node.text is None:
        raise ValueError("customs_hs_xml_structure_invalid")
    value = node.text.strip()
    if not value:
        raise ValueError("customs_hs_xml_structure_invalid")
    return value


def _children(
    node: ElementTree.Element,
    expected: frozenset[str],
    *,
    repeated: str | None = None,
) -> dict[str, list[ElementTree.Element]]:
    if node.attrib or (node.text is not None and node.text.strip()):
        raise ValueError("customs_hs_xml_structure_invalid")
    grouped = {name: [] for name in expected}
    for child in list(node):
        if (
            type(child.tag) is not str
            or child.tag not in expected
            or (child.tail is not None and child.tail.strip())
        ):
            raise ValueError("customs_hs_xml_structure_invalid")
        grouped[child.tag].append(child)
    for name, rows in grouped.items():
        if repeated == name:
            if not rows or len(rows) > 12:
                raise ValueError("customs_hs_xml_structure_invalid")
        elif len(rows) != 1:
            raise ValueError("customs_hs_xml_structure_invalid")
    return grouped


def _unsigned(value: str) -> int:
    if _UNSIGNED_INTEGER.fullmatch(value) is None:
        raise ValueError("customs_hs_amount_invalid")
    return int(value)


def _signed(value: str) -> int:
    if _SIGNED_INTEGER.fullmatch(value) is None:
        raise ValueError("customs_hs_amount_invalid")
    return int(value)


def _validate_taxonomy(
    taxonomy: Any,
    *,
    available_at_utc: datetime | None = None,
) -> None:
    if type(taxonomy) is not dict or (
        not _exact_text(taxonomy.get("taxonomy_id"), _TAXONOMY_ID)
        or not _exact_text(
            taxonomy.get("factor_name"), "pharmaceutical_products_hs30"
        )
        or not _exact_text(taxonomy.get("classification_system"), "HS")
        or not _exact_text(taxonomy.get("classification_reference_code"), "H6")
        or not _exact_text(taxonomy.get("classification_version"), "HS2022")
        or not _exact_text(
            taxonomy.get("taxonomy_effective_from_period"), "202201"
        )
        or taxonomy.get("taxonomy_effective_to_period") is not None
        or not _exact_chapter30(taxonomy.get("classification_codes"))
        or not _exact_text(
            taxonomy.get("official_label"), "Pharmaceutical products"
        )
        or not _exact_text(taxonomy.get("taxonomy_evidence_uri"), _TAXONOMY_URI)
        or type(taxonomy.get("taxonomy_evidence_sha256")) is not str
        or re.fullmatch(
            r"[0-9a-f]{64}", taxonomy["taxonomy_evidence_sha256"]
        ) is None
        or taxonomy.get("is_broad_biotechnology") is not False
        or taxonomy.get("shadow_only") is not True
        or taxonomy.get("eligible_for_production_score") is not False
    ):
        raise ValueError("customs_hs_taxonomy_invalid")
    taxonomy_available = _parse_utc_text(
        taxonomy.get("taxonomy_evidence_available_at_utc")
    )
    if taxonomy_available is None:
        raise ValueError("customs_hs_taxonomy_invalid")
    if available_at_utc is not None:
        observed_at = _parse_utc_text(_utc_text(available_at_utc))
        assert observed_at is not None
        if taxonomy_available > observed_at:
            raise ValueError("customs_hs_taxonomy_invalid")


def parse_hs_export_xml(
    payload: bytes,
    *,
    start_yymm: str,
    end_yymm: str,
    requested_hs_code: str,
    taxonomy: dict[str, Any],
    available_at_utc: datetime,
) -> list[dict[str, Any]]:
    """공식 Itemtrade XML을 HS30 월간 shadow row로 정규화한다."""

    start = _query_period(start_yymm)
    end = _query_period(end_yymm)
    if start > end or (end[0] * 12 + end[1]) - (start[0] * 12 + start[1]) > 11:
        raise ValueError("customs_hs_query_period_invalid")
    if requested_hs_code != "30":
        raise ValueError("customs_hs_code_invalid")
    available_text = _utc_text(available_at_utc)
    available_value = available_at_utc.astimezone(timezone.utc)
    available_month = (available_value.year, available_value.month)
    if end > available_month:
        raise ValueError("customs_hs_period_future")
    _validate_taxonomy(taxonomy, available_at_utc=available_at_utc)
    if type(payload) is not bytes or not payload or len(payload) > MAX_HS_EXPORT_RAW_BYTES:
        raise ValueError("customs_hs_xml_size_invalid")
    if _XML_DTD_OR_ENTITY.search(payload) is not None:
        raise ValueError("customs_hs_xml_dtd_forbidden")
    try:
        text = payload.decode("utf-8")
        root = ElementTree.fromstring(text)
    except (UnicodeDecodeError, ElementTree.ParseError) as exc:
        raise ValueError("customs_hs_xml_invalid") from exc
    if root.tag != "response":
        raise ValueError("customs_hs_xml_structure_invalid")
    root_nodes = _children(root, frozenset({"header", "body"}))
    header_nodes = _children(
        root_nodes["header"][0], frozenset({"resultCode", "resultMsg"})
    )
    result_code = _leaf(header_nodes["resultCode"][0])
    _leaf(header_nodes["resultMsg"][0])
    if result_code != "00":
        raise ValueError("customs_hs_provider_result_invalid")
    body_nodes = _children(root_nodes["body"][0], frozenset({"items"}))
    item_nodes = _children(
        body_nodes["items"][0], frozenset({"item"}), repeated="item"
    )["item"]
    result: list[dict[str, Any]] = []
    periods: set[str] = set()
    for item in item_nodes:
        fields = _children(item, _ITEM_FIELDS)
        year, month, period_yymm = _period(_leaf(fields["year"][0]))
        if (year, month) > available_month:
            raise ValueError("customs_hs_period_future")
        if not start <= (year, month) <= end or period_yymm in periods:
            raise ValueError("customs_hs_period_invalid")
        periods.add(period_yymm)
        hs_code = _leaf(fields["hsCode"][0])
        if hs_code != requested_hs_code:
            raise ValueError("customs_hs_code_invalid")
        source_label = _leaf(fields["statKor"][0])
        if len(source_label.encode("utf-8")) > 240:
            raise ValueError("customs_hs_label_invalid")
        result.append(
            {
                "factor_name": "pharmaceutical_products_hs30",
                "period_year": year,
                "period_month": month,
                "period_yymm": period_yymm,
                "hs_code": hs_code,
                "source_label_ko": source_label,
                "export_amount_usd": _unsigned(_leaf(fields["expDlr"][0])),
                "export_weight_kg": _unsigned(_leaf(fields["expWgt"][0])),
                "import_amount_usd": _unsigned(_leaf(fields["impDlr"][0])),
                "import_weight_kg": _unsigned(_leaf(fields["impWgt"][0])),
                "trade_balance_usd": _signed(_leaf(fields["balPayments"][0])),
                "classification_system": "HS",
                "classification_reference_code": "H6",
                "classification_version": "HS2022",
                "taxonomy_effective_from_period": "202201",
                "taxonomy_effective_to_period": None,
                "taxonomy_evidence_sha256": taxonomy["taxonomy_evidence_sha256"],
                "available_at_utc": available_text,
                "shadow_only": True,
                "eligible_for_production_score": False,
            }
        )
    if periods != _expected_periods(start, end):
        raise ValueError("customs_hs_period_incomplete")
    return sorted(result, key=lambda row: row["period_yymm"])


def _failed_fetch(
    started_at: datetime,
    completed_at: datetime,
    error_type: FetchErrorType,
) -> FetchResult[dict[str, Any]]:
    return FetchResult(
        status=FetchStatus.FAILED,
        provider="CUSTOMS_HS",
        endpoint="/getItemtradeList",
        tr_id=None,
        venue="KR",
        symbol="HS30",
        started_at_utc=started_at,
        completed_at_utc=completed_at,
        error_type=error_type,
        cache_source=CacheSource.NETWORK,
        fallback_used=False,
        value=None,
    )


def fetch_hs_export_result(
    start_yymm: str,
    end_yymm: str,
    *,
    service_key: str,
    taxonomy: dict[str, Any],
    transport: Callable[..., Any] = request.urlopen,
    timeout: float = 10.0,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> FetchResult[dict[str, Any]]:
    """Itemtrade 조회를 secret 비노출 typed result로 반환한다."""

    start = _query_period(start_yymm)
    end = _query_period(end_yymm)
    if start > end or (end[0] * 12 + end[1]) - (start[0] * 12 + start[1]) > 11:
        raise ValueError("customs_hs_query_period_invalid")
    _validate_taxonomy(taxonomy)
    if type(timeout) not in (int, float) or isinstance(timeout, bool) or not 0 < timeout <= 60:
        raise ValueError("customs_hs_timeout_invalid")
    started_at = clock()
    if type(service_key) is not str or not service_key.strip():
        completed_at = clock()
        return FetchResult(
            status=FetchStatus.SKIPPED,
            provider="CUSTOMS_HS",
            endpoint="/getItemtradeList",
            tr_id=None,
            venue="KR",
            symbol="HS30",
            started_at_utc=started_at,
            completed_at_utc=completed_at,
            error_type=FetchErrorType.NOT_CONFIGURED,
            cache_source=CacheSource.NONE,
            fallback_used=False,
            value=None,
        )
    key = service_key.strip()
    if len(key.encode("utf-8")) > 512:
        return _failed_fetch(started_at, clock(), FetchErrorType.MALFORMED)
    url = CUSTOMS_HS_EXPORT_ENDPOINT + "?" + parse.urlencode(
        {
            "serviceKey": key,
            "strtYymm": start_yymm,
            "endYymm": end_yymm,
            "hsSgn": "30",
        }
    )
    try:
        response = transport(url, timeout=timeout)
    except error.HTTPError as exc:
        try:
            code = getattr(exc, "code", None)
        except Exception:
            code = None
        try:
            exc.close()
        except Exception:
            pass
        mapped = (
            FetchErrorType.AUTH
            if code in (401, 403)
            else FetchErrorType.HTTP
        )
        return _failed_fetch(started_at, clock(), mapped)
    except (error.URLError, TimeoutError, OSError):
        return _failed_fetch(started_at, clock(), FetchErrorType.NETWORK)
    except Exception:
        return _failed_fetch(started_at, clock(), FetchErrorType.PROVIDER)
    try:
        status = getattr(response, "status", None)
        if status in (401, 403):
            return _failed_fetch(started_at, clock(), FetchErrorType.AUTH)
        if type(status) is not int or status != 200:
            return _failed_fetch(started_at, clock(), FetchErrorType.HTTP)
        headers = getattr(response, "headers", None)
        getter = getattr(headers, "get", None)
        content_type = (
            normalize_xml_content_type(getter("Content-Type", None))
            if callable(getter)
            else None
        )
        reader = getattr(response, "read", None)
        if content_type is None or not callable(reader):
            return _failed_fetch(started_at, clock(), FetchErrorType.MALFORMED)
        raw = reader(MAX_HS_EXPORT_RAW_BYTES + 1)
    except (TimeoutError, OSError):
        return _failed_fetch(started_at, clock(), FetchErrorType.NETWORK)
    except Exception:
        return _failed_fetch(started_at, clock(), FetchErrorType.PROVIDER)
    finally:
        closer = getattr(response, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
    completed_at = clock()
    if type(raw) is not bytes or not raw or len(raw) > MAX_HS_EXPORT_RAW_BYTES:
        return _failed_fetch(started_at, completed_at, FetchErrorType.MALFORMED)
    sensitive_kind = sensitive_text_kind(raw, known_secrets=(key,))
    if sensitive_kind is not None:
        return _failed_fetch(
            started_at,
            completed_at,
            FetchErrorType.AUTH
            if sensitive_kind == "known"
            else FetchErrorType.MALFORMED,
        )
    try:
        rows = parse_hs_export_xml(
            raw,
            start_yymm=start_yymm,
            end_yymm=end_yymm,
            requested_hs_code="30",
            taxonomy=taxonomy,
            available_at_utc=completed_at,
        )
    except ValueError:
        return _failed_fetch(started_at, completed_at, FetchErrorType.MALFORMED)
    value = {
        "request_endpoint": "/getItemtradeList",
        "request_params": {
            "strtYymm": start_yymm,
            "endYymm": end_yymm,
            "hsSgn": "30",
        },
        "content_type": content_type,
        "raw_response_base64": base64.b64encode(raw).decode("ascii"),
        "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
        "rows": rows,
        "taxonomy_evidence_sha256": taxonomy["taxonomy_evidence_sha256"],
    }
    return FetchResult(
        status=FetchStatus.SUCCESS,
        provider="CUSTOMS_HS",
        endpoint="/getItemtradeList",
        tr_id=None,
        venue="KR",
        symbol="HS30",
        started_at_utc=started_at,
        completed_at_utc=completed_at,
        error_type=FetchErrorType.NONE,
        cache_source=CacheSource.NETWORK,
        fallback_used=False,
        value=value,
        source_fetched_at_utc=completed_at,
    )
