"""관세청 수출 주요품목별 10일 잠정치의 순수 파서와 피처 계산."""

from __future__ import annotations

import base64
from calendar import monthrange
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import re
from typing import Callable, Protocol
from urllib import error, parse, request
from xml.etree import ElementTree

from core.market_data_fetch import CacheSource, FetchErrorType, FetchResult, FetchStatus
from core.sensitive_text import decoded_text_variants, sensitive_text_kind


CUSTOMS_EXPORT_ENDPOINT = (
    "https://apis.data.go.kr/1220000/prlstMmUtPrviExpAcrs/"
    "getPrlstMmUtPrviExpAcrs"
)
MAX_CUSTOMS_RAW_BYTES = 700_000
MAX_CUSTOMS_CONTENT_TYPE_BYTES = 256
MAX_CUSTOMS_SERVICE_KEY_BYTES = 512
_ASCII_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_CANONICAL_AMOUNT = re.compile(
    r"(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)\Z"
)
_MIME_TOKEN = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
_MIME_QUOTED = r'"(?:[^"\\\r\n]|\\[\t\x20-\x7e])*"'
_CONTENT_TYPE = re.compile(
    rf"^[ \t]*(?P<type>{_MIME_TOKEN})/(?P<subtype>{_MIME_TOKEN})"
    rf"(?:[ \t]*;[ \t]*{_MIME_TOKEN}[ \t]*=[ \t]*"
    rf"(?:{_MIME_TOKEN}|{_MIME_QUOTED}))*[ \t]*$"
)
_XML_DTD_OR_ENTITY = re.compile(
    r"<![ \t\r\n]*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE
)


class _HttpResponse(Protocol):
    status: int
    headers: object

    def read(self, limit: int) -> bytes: ...

    def close(self) -> None: ...


class CustomsProviderError(ValueError):
    """원본 결과코드를 보존하지 않는 분류된 공급자 오류."""

    def __init__(self, error_type: FetchErrorType):
        if error_type not in {FetchErrorType.AUTH, FetchErrorType.PROVIDER}:
            raise ValueError("customs_provider_error_type_invalid")
        super().__init__("customs_provider_error")
        self.error_type = error_type


PRODUCT_FIELDS = {
    "itemUsdAmt00": "total",
    "itemUsdAmt01": "semiconductors",
    "itemUsdAmt02": "steel_products",
    "itemUsdAmt03": "passenger_cars",
    "itemUsdAmt04": "petroleum_products",
    "itemUsdAmt05": "wireless_communication_devices",
    "itemUsdAmt06": "ships",
    "itemUsdAmt07": "auto_parts",
    "itemUsdAmt08": "computer_peripherals",
    "itemUsdAmt09": "precision_instruments",
    "itemUsdAmt10": "home_appliances",
}


def _validate_children(
    parent: ElementTree.Element,
    expected_counts: Mapping[str, int | tuple[int, int]],
) -> dict[str, list[ElementTree.Element]]:
    if parent.attrib or (parent.text is not None and parent.text.strip()):
        raise ValueError("customs_xml_structure_invalid")
    grouped = {name: [] for name in expected_counts}
    for child in list(parent):
        if (
            type(child.tag) is not str
            or child.tag not in expected_counts
            or (child.tail is not None and child.tail.strip())
        ):
            raise ValueError("customs_xml_structure_invalid")
        grouped[child.tag].append(child)
    for name, expected in expected_counts.items():
        minimum, maximum = expected if isinstance(expected, tuple) else (expected, expected)
        if not minimum <= len(grouped[name]) <= maximum:
            raise ValueError("customs_xml_structure_invalid")
    return grouped


def _leaf_text(node: ElementTree.Element, *, strip: bool = False) -> str:
    if node.attrib or list(node) or node.text is None:
        raise ValueError("customs_xml_structure_invalid")
    text = node.text.strip() if strip else node.text
    if not text:
        raise ValueError("customs_xml_structure_invalid")
    return text


def _amount(text: str) -> int:
    canonical = text.lstrip(" ")
    if not canonical or text.endswith(" ") or _CANONICAL_AMOUNT.fullmatch(canonical) is None:
        raise ValueError("customs_amount_invalid")
    return int(canonical.replace(",", ""))


def _period(year_text: str, month_text: str, day_text: str) -> tuple[int, int, int, str]:
    if re.fullmatch(r"[0-9]{4}", year_text) is None:
        raise ValueError("customs_period_year_invalid")
    if (
        re.fullmatch(r"[0-9]{6}", month_text) is None
        or month_text[:4] != year_text
    ):
        raise ValueError("customs_period_month_invalid")
    year = int(year_text)
    month = int(month_text[4:])
    if not 1 <= month <= 12:
        raise ValueError("customs_period_month_invalid")
    if re.fullmatch(r"01~[0-9]{2}", day_text) is None:
        raise ValueError("customs_period_day_invalid")
    day = int(day_text[3:])
    final_day = monthrange(year, month)[1]
    kinds = {10: "day_10", 20: "day_20", final_day: "month_end"}
    if day not in kinds:
        raise ValueError("customs_period_day_invalid")
    return year, month, day, kinds[day]


def _parse_body(body: ElementTree.Element, *, require_empty: bool) -> list[dict[str, object]]:
    body_children = _validate_children(body, {"items": 1, "totalCount": 1})
    items_parent = body_children["items"][0]
    total_count_node = body_children["totalCount"][0]
    if items_parent.attrib or (items_parent.text is not None and items_parent.text.strip()):
        raise ValueError("customs_xml_structure_invalid")
    item_nodes: list[ElementTree.Element] = []
    for child in list(items_parent):
        if (
            child.tag != "item"
            or (child.tail is not None and child.tail.strip())
        ):
            raise ValueError("customs_xml_structure_invalid")
        item_nodes.append(child)
    total_count_text = _leaf_text(total_count_node)
    if _ASCII_INTEGER.fullmatch(total_count_text) is None:
        raise ValueError("customs_total_count_invalid")
    total_count = int(total_count_text)
    if total_count != len(item_nodes):
        raise ValueError("customs_total_count_mismatch")
    if require_empty and (total_count != 0 or item_nodes):
        raise ValueError("customs_xml_structure_invalid")

    result: list[dict[str, object]] = []
    periods: set[tuple[int, int, int]] = set()
    item_counts = {field: 1 for field in PRODUCT_FIELDS}
    item_counts.update({"priodYear": 1, "priodMon": 1, "priodDt": 1})
    for item in item_nodes:
        item_children = _validate_children(item, item_counts)
        year, month, day, kind = _period(
            _leaf_text(item_children["priodYear"][0]),
            _leaf_text(item_children["priodMon"][0]),
            _leaf_text(item_children["priodDt"][0]),
        )
        period_key = (year, month, day)
        if period_key in periods:
            raise ValueError("customs_period_duplicate")
        periods.add(period_key)
        amounts = {
            product: _amount(_leaf_text(item_children[field][0]))
            for field, product in PRODUCT_FIELDS.items()
        }
        result.append(
            {
                "period_year": year,
                "period_month": month,
                "period_end_day": day,
                "period_kind": kind,
                "amounts_thousand_usd": amounts,
            }
        )
    return result


def parse_customs_export_xml(payload: bytes) -> list[dict[str, object]]:
    """공식 UTF-8 XML을 정규화하고 비정규 인코딩·DTD를 차단한다."""
    if type(payload) is not bytes or not payload:
        raise ValueError("customs_xml_invalid")
    if len(payload) > MAX_CUSTOMS_RAW_BYTES:
        raise ValueError("customs_xml_too_large")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("customs_xml_encoding_invalid") from None
    if "\x00" in text:
        raise ValueError("customs_xml_encoding_invalid")
    if _XML_DTD_OR_ENTITY.search(text) is not None:
        raise ValueError("customs_xml_dtd_forbidden")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        raise ValueError("customs_xml_invalid") from None
    if root.tag != "response":
        raise ValueError("customs_xml_structure_invalid")
    root_children = _validate_children(root, {"header": 1, "body": (0, 1)})
    header = root_children["header"][0]
    header_children = _validate_children(header, {"resultCode": 1, "resultMsg": 1})
    result_code = _leaf_text(header_children["resultCode"][0])
    _leaf_text(header_children["resultMsg"][0], strip=True)

    body_nodes = [child for child in list(root) if child.tag == "body"]
    if len(body_nodes) > 1:
        raise ValueError("customs_xml_structure_invalid")
    body = body_nodes[0] if body_nodes else None

    if result_code == "00":
        if body is None:
            raise ValueError("customs_xml_structure_invalid")
        return _parse_body(body, require_empty=False)
    if result_code == "01":
        if body is not None:
            _parse_body(body, require_empty=True)
        raise CustomsProviderError(FetchErrorType.PROVIDER)
    if result_code in {"02", "03"}:
        if body is not None:
            _parse_body(body, require_empty=True)
        raise CustomsProviderError(FetchErrorType.AUTH)
    if result_code == "99":
        if body is not None:
            _parse_body(body, require_empty=True)
        raise ValueError("customs_request_contract_invalid")
    raise ValueError("customs_result_code_invalid")


def _failed_fetch(
    *,
    started_at: datetime,
    completed_at: datetime,
    error_type: FetchErrorType,
    cache_source: CacheSource = CacheSource.NETWORK,
) -> FetchResult[dict[str, object]]:
    return FetchResult(
        status=FetchStatus.FAILED,
        provider="CUSTOMS",
        endpoint="/getPrlstMmUtPrviExpAcrs",
        tr_id=None,
        venue="KR",
        symbol="KR_EXPORTS",
        started_at_utc=started_at,
        completed_at_utc=completed_at,
        error_type=error_type,
        cache_source=cache_source,
        fallback_used=False,
        value=None,
    )


def _validate_query_period(start_yymm: str, end_yymm: str, timeout: float) -> None:
    def parts(value: str) -> tuple[int, int]:
        if type(value) is not str or len(value) != 6 or not value.isdigit():
            raise ValueError("customs_query_period_invalid")
        year = int(value[:4])
        month = int(value[4:])
        if year < 2016 or not 1 <= month <= 12:
            raise ValueError("customs_query_period_invalid")
        return year, month

    start = parts(start_yymm)
    end = parts(end_yymm)
    if start > end:
        raise ValueError("customs_query_period_invalid")
    if type(timeout) not in {int, float} or not 0 < timeout <= 60:
        raise ValueError("customs_timeout_invalid")


def normalize_xml_content_type(value: object) -> str | None:
    """XML media type을 고정 allowlist 형태로 정규화한다."""
    if (
        type(value) is not str
        or len(value.encode("utf-8")) > MAX_CUSTOMS_CONTENT_TYPE_BYTES
    ):
        return None
    if sensitive_text_kind(value) is not None:
        return None
    match = _CONTENT_TYPE.fullmatch(value)
    if match is None:
        return None
    media_type = match.group("type").lower()
    subtype = match.group("subtype").lower()
    exact_xml = (media_type, subtype) in {("application", "xml"), ("text", "xml")}
    if not exact_xml and (len(subtype) <= 4 or not subtype.endswith("+xml")):
        return None

    parts = value.strip().split(";")
    canonical = f"{media_type}/{subtype}"
    if len(parts) == 1:
        return canonical
    if len(parts) != 2:
        return None
    name, separator, raw_parameter = parts[1].partition("=")
    if separator != "=" or name.strip().lower() != "charset":
        return None
    charset = raw_parameter.strip()
    if len(charset) >= 2 and charset[0] == charset[-1] == '"':
        charset = charset[1:-1]
    if charset.lower() != "utf-8":
        return None
    return f"{canonical};charset=utf-8"


def _content_type_header(response: _HttpResponse) -> object:
    headers = getattr(response, "headers", None)
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    return getter("Content-Type", None)


def decoded_customs_response_variants(payload: bytes) -> set[str]:
    return decoded_text_variants(payload)


def _reflected_secret_error(
    payload: bytes,
    service_key: str,
) -> FetchErrorType | None:
    kind = sensitive_text_kind(payload, known_secrets=(service_key,))
    if kind == "known":
        return FetchErrorType.AUTH
    if kind == "generic":
        return FetchErrorType.MALFORMED
    return None


def fetch_customs_export_result(
    start_yymm: str,
    end_yymm: str,
    *,
    service_key: str,
    transport: Callable[..., _HttpResponse] = request.urlopen,
    timeout: float = 10.0,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> FetchResult[dict[str, object]]:
    """관세청 API 조회 결과를 공통 typed 계약으로 반환한다."""
    _validate_query_period(start_yymm, end_yymm, timeout)
    started_at = clock()
    if type(service_key) is not str or not service_key.strip():
        completed_at = clock()
        return FetchResult(
            status=FetchStatus.SKIPPED,
            provider="CUSTOMS",
            endpoint="/getPrlstMmUtPrviExpAcrs",
            tr_id=None,
            venue="KR",
            symbol="KR_EXPORTS",
            started_at_utc=started_at,
            completed_at_utc=completed_at,
            error_type=FetchErrorType.NOT_CONFIGURED,
            cache_source=CacheSource.NONE,
            fallback_used=False,
            value=None,
        )

    normalized_service_key = service_key.strip()
    if len(normalized_service_key.encode("utf-8")) > MAX_CUSTOMS_SERVICE_KEY_BYTES:
        completed_at = clock()
        return _failed_fetch(
            started_at=started_at,
            completed_at=completed_at,
            error_type=FetchErrorType.MALFORMED,
        )
    query = parse.urlencode(
        {
            "serviceKey": normalized_service_key,
            "strtYymm": start_yymm,
            "endYymm": end_yymm,
        }
    )
    http_request = request.Request(
        f"{CUSTOMS_EXPORT_ENDPOINT}?{query}",
        headers={"Accept": "application/xml", "User-Agent": "Sanjuk-Stock-Simulator/1"},
    )
    try:
        response = transport(http_request, timeout=timeout)
        try:
            payload = response.read(MAX_CUSTOMS_RAW_BYTES + 1)
            status = getattr(response, "status", None)
            content_type_header = _content_type_header(response)
        finally:
            response.close()
    except error.HTTPError as exc:
        completed_at = clock()
        return _failed_fetch(
            started_at=started_at,
            completed_at=completed_at,
            error_type=(
                FetchErrorType.AUTH if exc.code in {401, 403} else FetchErrorType.HTTP
            ),
        )
    except (error.URLError, TimeoutError, OSError):
        completed_at = clock()
        return _failed_fetch(
            started_at=started_at,
            completed_at=completed_at,
            error_type=FetchErrorType.NETWORK,
        )
    except Exception:
        completed_at = clock()
        return _failed_fetch(
            started_at=started_at,
            completed_at=completed_at,
            error_type=FetchErrorType.PROVIDER,
        )
    completed_at = clock()
    if type(payload) is not bytes or len(payload) > MAX_CUSTOMS_RAW_BYTES:
        return _failed_fetch(
            started_at=started_at,
            completed_at=completed_at,
            error_type=FetchErrorType.MALFORMED,
        )
    reflected_secret_error = _reflected_secret_error(payload, normalized_service_key)
    if reflected_secret_error is not None:
        return _failed_fetch(
            started_at=started_at,
            completed_at=completed_at,
            error_type=reflected_secret_error,
        )
    content_type_kind = (
        sensitive_text_kind(
            content_type_header,
            known_secrets=(normalized_service_key,),
        )
        if type(content_type_header) is str
        and len(content_type_header.encode("utf-8"))
        <= MAX_CUSTOMS_CONTENT_TYPE_BYTES
        else None
    )
    if content_type_kind is not None:
        return _failed_fetch(
            started_at=started_at,
            completed_at=completed_at,
            error_type=(
                FetchErrorType.AUTH
                if content_type_kind == "known"
                else FetchErrorType.MALFORMED
            ),
        )
    content_type = normalize_xml_content_type(content_type_header)
    if type(status) is not int or type(status) is bool or not 100 <= status <= 599:
        return _failed_fetch(
            started_at=started_at,
            completed_at=completed_at,
            error_type=FetchErrorType.MALFORMED,
        )
    if status != 200:
        return _failed_fetch(
            started_at=started_at,
            completed_at=completed_at,
            error_type=(
                FetchErrorType.AUTH if status in {401, 403} else FetchErrorType.HTTP
            ),
        )
    if content_type is None:
        return _failed_fetch(
            started_at=started_at,
            completed_at=completed_at,
            error_type=FetchErrorType.MALFORMED,
        )
    try:
        items = parse_customs_export_xml(payload)
    except CustomsProviderError as exc:
        return _failed_fetch(
            started_at=started_at,
            completed_at=completed_at,
            error_type=exc.error_type,
        )
    except ValueError:
        return _failed_fetch(
            started_at=started_at,
            completed_at=completed_at,
            error_type=FetchErrorType.MALFORMED,
        )
    if any(
        not start_yymm
        <= f"{item['period_year']:04d}{item['period_month']:02d}"
        <= end_yymm
        for item in items
    ):
        return _failed_fetch(
            started_at=started_at,
            completed_at=completed_at,
            error_type=FetchErrorType.MALFORMED,
        )
    fetch_status = FetchStatus.SUCCESS if items else FetchStatus.EMPTY
    return FetchResult(
        status=fetch_status,
        provider="CUSTOMS",
        endpoint="/getPrlstMmUtPrviExpAcrs",
        tr_id=None,
        venue="KR",
        symbol="KR_EXPORTS",
        started_at_utc=started_at,
        completed_at_utc=completed_at,
        error_type=FetchErrorType.NONE,
        cache_source=CacheSource.NETWORK,
        fallback_used=False,
        value={
            "items": items,
            "total_count": len(items),
            "raw_xml_base64": base64.b64encode(payload).decode("ascii"),
            "raw_xml_sha256": hashlib.sha256(payload).hexdigest(),
            "raw_size_bytes": len(payload),
            "request_params": {"strtYymm": start_yymm, "endYymm": end_yymm},
            "http_status": status,
            "content_type": content_type,
            "parser_contract_version": 1,
        },
        source_fetched_at_utc=completed_at,
    )
