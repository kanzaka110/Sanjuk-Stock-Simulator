"""공식 KCS 보도자료에 기재된 누적 조업일수를 정규화한다."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from html.parser import HTMLParser
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


CALENDAR_DOMAIN = "KCS_REPORTED_OPERATING_DAYS"
METHOD_VERSION = "korea-kr-kcs-press-release-v1"
MAX_RELEASE_HTML_BYTES = 2_000_000
MAX_RELEASE_SEARCH_PAGES = 20
_SOURCE_HOST = "m.korea.kr"
_SOURCE_PATH = "/briefing/pressReleaseView.do"
_NEWS_ID_RE = re.compile(r"[0-9]{6,12}")


@dataclass(frozen=True)
class WorkdayFetchResult:
    status: str
    error_type: str
    rows: tuple[dict[str, Any], ...]
    started_at_utc: datetime
    completed_at_utc: datetime


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self.parts.append(data)


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._href: str | None = None
        self._parts: list[str] = []
        self.anchors: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a" or self._href is not None:
            return
        href = dict(attrs).get("href")
        if href is not None:
            self._href = href
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            text = re.sub(r"\s+", " ", " ".join(self._parts)).strip()
            self.anchors.append((self._href, text))
            self._href = None
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._parts.append(data)


class _PagingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._paging_depth = 0
        self._anchor_depth = 0
        self._anchor_attrs: dict[str, str | None] = {}
        self._anchor_parts: list[str] = []
        self.paging_count = 0
        self.anchors: list[tuple[dict[str, str | None], str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._paging_depth:
            if tag not in _DetailHeaderParser._VOID_TAGS:
                self._paging_depth += 1
                if self._anchor_depth:
                    self._anchor_depth += 1
        elif tag == "div" and "paging" in set((dict(attrs).get("class") or "").split()):
            self._paging_depth = 1
            self.paging_count += 1
            return
        else:
            return
        if tag == "a" and not self._anchor_depth:
            self._anchor_depth = 1
            self._anchor_attrs = dict(attrs)
            self._anchor_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self._paging_depth or tag in _DetailHeaderParser._VOID_TAGS:
            return
        if self._anchor_depth == 1 and tag == "a":
            text = re.sub(r"\s+", " ", " ".join(self._anchor_parts)).strip()
            self.anchors.append((dict(self._anchor_attrs), text))
            self._anchor_attrs = {}
            self._anchor_parts = []
        if self._anchor_depth:
            self._anchor_depth -= 1
        self._paging_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._anchor_depth:
            self._anchor_parts.append(data)


class _DetailHeaderParser(HTMLParser):
    _VOID_TAGS = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._article_depth = 0
        self._h1_depth = 0
        self._info_depth = 0
        self._span_depth = 0
        self._agency_depth = 0
        self._h1_parts: list[str] = []
        self._span_parts: list[str] = []
        self._agency_parts: list[str] = []
        self._agency_href: str | None = None
        self.article_count = 0
        self.titles: list[str] = []
        self.info_spans: list[str] = []
        self.agencies: list[tuple[str, str]] = []

    @staticmethod
    def _tokens(attrs: list[tuple[str, str | None]], name: str) -> set[str]:
        value = dict(attrs).get(name) or ""
        return set(value.split())

    @staticmethod
    def _clean(parts: list[str]) -> str:
        return re.sub(r"\s+", " ", " ".join(parts)).strip()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        is_void = tag in self._VOID_TAGS
        if self._article_depth:
            if not is_void:
                self._article_depth += 1
                if self._h1_depth:
                    self._h1_depth += 1
                if self._info_depth:
                    self._info_depth += 1
                if self._span_depth:
                    self._span_depth += 1
                if self._agency_depth:
                    self._agency_depth += 1
        elif tag == "div" and "article_head" in self._tokens(attrs, "class"):
            self._article_depth = 1
            self.article_count += 1
            return
        else:
            return

        if tag == "h1" and not self._h1_depth:
            self._h1_depth = 1
            self._h1_parts = []
        if tag == "div" and "info" in self._tokens(attrs, "class"):
            self._info_depth = 1
        elif self._info_depth and tag == "span" and not self._span_depth:
            self._span_depth = 1
            self._span_parts = []
        if self._info_depth and tag == "a" and not self._agency_depth:
            self._agency_depth = 1
            self._agency_parts = []
            self._agency_href = dict(attrs).get("href")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self._article_depth or tag in self._VOID_TAGS:
            return
        if self._h1_depth == 1 and tag == "h1":
            self.titles.append(self._clean(self._h1_parts))
        if self._span_depth == 1 and tag == "span":
            self.info_spans.append(self._clean(self._span_parts))
        if self._agency_depth == 1 and tag == "a":
            self.agencies.append(
                (self._agency_href or "", self._clean(self._agency_parts))
            )
            self._agency_href = None
        if self._h1_depth:
            self._h1_depth -= 1
        if self._span_depth:
            self._span_depth -= 1
        if self._agency_depth:
            self._agency_depth -= 1
        if self._info_depth:
            self._info_depth -= 1
        self._article_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._h1_depth:
            self._h1_parts.append(data)
        if self._span_depth:
            self._span_parts.append(data)
        if self._agency_depth:
            self._agency_parts.append(data)


def _detail_header(raw: bytes) -> tuple[str, date, str]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_RELEASE_HTML_BYTES:
        raise ValueError("customs_workday_detail_header_invalid")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("customs_workday_html_encoding_invalid") from exc
    parser = _DetailHeaderParser()
    try:
        parser.feed(decoded)
        parser.close()
    except Exception as exc:
        raise ValueError("customs_workday_detail_header_invalid") from exc
    if parser.article_count != 1 or len(parser.titles) != 1:
        raise ValueError("customs_workday_detail_header_invalid")
    date_values = {
        value
        for value in parser.info_spans
        if re.fullmatch(r"20[0-9]{2}\.[0-9]{2}\.[0-9]{2}", value)
    }
    if len(date_values) != 1:
        raise ValueError("customs_workday_detail_header_invalid")
    try:
        published = date.fromisoformat(next(iter(date_values)).replace(".", "-"))
    except ValueError as exc:
        raise ValueError("customs_workday_detail_header_invalid") from exc
    valid_agencies: list[str] = []
    for href, text in parser.agencies:
        try:
            parsed = urlsplit(urljoin("https://m.korea.kr", href))
            query = parse_qs(parsed.query, strict_parsing=True)
        except (TypeError, UnicodeError, ValueError):
            continue
        if (
            parsed.scheme == "https"
            and parsed.netloc == _SOURCE_HOST
            and parsed.path == "/news/ministryNewsList.do"
            and query == {"repCode": ["B00003"]}
        ):
            valid_agencies.append(text)
    if len(valid_agencies) != 1:
        raise ValueError("customs_workday_detail_header_invalid")
    return parser.titles[0], published, valid_agencies[0]


def _pagination_has_next(raw: bytes, *, expected_page: int) -> bool:
    if (
        type(raw) is not bytes
        or not raw
        or len(raw) > MAX_RELEASE_HTML_BYTES
        or type(expected_page) is not int
        or not 1 <= expected_page <= MAX_RELEASE_SEARCH_PAGES
    ):
        raise ValueError("customs_workday_pagination_invalid")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("customs_workday_pagination_invalid") from exc
    parser = _PagingParser()
    try:
        parser.feed(decoded)
        parser.close()
    except Exception as exc:
        raise ValueError("customs_workday_pagination_invalid") from exc
    if parser.paging_count != 1 or not parser.anchors:
        raise ValueError("customs_workday_pagination_invalid")
    current_pages: list[int] = []
    linked_pages: set[int] = set()
    for attrs, text in parser.anchors:
        href = attrs.get("href")
        onclick = re.sub(r"\s+", " ", attrs.get("onclick") or "").strip()
        title = attrs.get("title")
        classes = set((attrs.get("class") or "").split())
        if title == "현재페이지":
            if (
                href != "#pageLink"
                or onclick != "return false;"
                or "on" not in classes
                or not text.isascii()
                or not text.isdigit()
            ):
                raise ValueError("customs_workday_pagination_invalid")
            current_pages.append(int(text))
            linked_pages.add(int(text))
            continue
        match = re.fullmatch(r"pageLink\(([1-9][0-9]*)\); return false;", onclick)
        if href != "#pageLink" or match is None:
            raise ValueError("customs_workday_pagination_invalid")
        linked_pages.add(int(match.group(1)))
    if current_pages != [expected_page] or expected_page not in linked_pages:
        raise ValueError("customs_workday_pagination_invalid")
    if max(linked_pages) > MAX_RELEASE_SEARCH_PAGES:
        raise ValueError("customs_workday_release_list_incomplete")
    later_pages = {page for page in linked_pages if page > expected_page}
    if later_pages and expected_page + 1 not in later_pages:
        raise ValueError("customs_workday_pagination_invalid")
    return bool(later_pages)


def _visible_text(raw: bytes) -> str:
    if type(raw) is not bytes or not raw:
        raise ValueError("customs_workday_html_invalid")
    if len(raw) > MAX_RELEASE_HTML_BYTES:
        raise ValueError("customs_workday_html_oversized")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("customs_workday_html_encoding_invalid") from exc
    parser = _VisibleTextParser()
    try:
        parser.feed(decoded)
        parser.close()
    except Exception as exc:
        raise ValueError("customs_workday_html_invalid") from exc
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def _canonical_source_uri(value: Any) -> tuple[str, str]:
    if type(value) is not str or len(value) > 2_048:
        raise ValueError("customs_workday_source_uri_invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != _SOURCE_HOST
        or parsed.path != _SOURCE_PATH
        or parsed.fragment
    ):
        raise ValueError("customs_workday_source_uri_invalid")
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=False)
    news_ids = query.get("newsId", [])
    if len(news_ids) != 1 or not _NEWS_ID_RE.fullmatch(news_ids[0]):
        raise ValueError("customs_workday_source_uri_invalid")
    news_id = news_ids[0]
    canonical = urlunsplit(("https", _SOURCE_HOST, _SOURCE_PATH, urlencode({"newsId": news_id}), ""))
    return canonical, news_id


def _canonical_utc(value: Any) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("customs_workday_first_seen_invalid")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _period_kind(year: int, month: int, day: int) -> str:
    if day == 10:
        return "day_10"
    if day == 20:
        return "day_20"
    if day == monthrange(year, month)[1]:
        return "month_end"
    raise ValueError("customs_workday_period_invalid")


def _release_date_valid(*, year: int, month: int, day: int, released: date) -> bool:
    if day == 10:
        return released.year == year and released.month == month and 11 <= released.day <= 13
    if day == 20:
        return released.year == year and released.month == month and 21 <= released.day <= 23
    next_month = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    return next_month <= released <= next_month + timedelta(days=2)


def _period_title_pattern(*, year: int, month: int, day: int) -> re.Pattern[str]:
    year_token = rf"(?:[’']?{year % 100:02d}|{year})"
    if day in {10, 20}:
        expression = (
            rf"{year_token}\s*년\s*{month}\s*월\s*1\s*일\s*[~∼-]\s*"
            rf"(?:{month}\s*월\s*)?{day}\s*일\s*수출입\s*현황"
        )
    else:
        expression = rf"{year_token}\s*년\s*{month}\s*월\s*수출입\s*현황"
    return re.compile(expression)


def _count_to_tenths(value: str) -> int:
    try:
        count = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("customs_workday_count_invalid") from exc
    scaled = count * 10
    if not count.is_finite() or scaled != scaled.to_integral_value():
        raise ValueError("customs_workday_count_precision_invalid")
    tenths = int(scaled)
    if tenths <= 0 or tenths > 310:
        raise ValueError("customs_workday_count_invalid")
    return tenths


def parse_kcs_workday_release_list_html(
    raw_html: bytes,
    *,
    expected_year: int,
    expected_month: int,
    expected_period_end_day: int,
) -> dict[str, Any]:
    """정책브리핑 목록에서 정확한 관세청 D10/D20/EOM 문서 한 건을 고른다."""
    if type(raw_html) is not bytes or not raw_html:
        raise ValueError("customs_workday_release_list_invalid")
    if len(raw_html) > MAX_RELEASE_HTML_BYTES:
        raise ValueError("customs_workday_release_list_oversized")
    try:
        decoded = raw_html.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("customs_workday_release_list_encoding_invalid") from exc
    period_kind = _period_kind(
        expected_year,
        expected_month,
        expected_period_end_day,
    )
    del period_kind
    title_pattern = _period_title_pattern(
        year=expected_year,
        month=expected_month,
        day=expected_period_end_day,
    )
    parser = _AnchorParser()
    try:
        parser.feed(decoded)
        parser.close()
    except Exception as exc:
        raise ValueError("customs_workday_release_list_invalid") from exc
    candidates: dict[str, dict[str, Any]] = {}
    for href, text in parser.anchors:
        title_match = title_pattern.search(text)
        if title_match is None:
            continue
        agency_dates = re.findall(
            r"(20[0-9]{2})-([0-9]{2})-([0-9]{2})\s*관세청(?:\s|$)",
            text,
        )
        if not agency_dates:
            continue
        try:
            release_dates = {
                date(int(year), int(month), int(day))
                for year, month, day in agency_dates
            }
        except ValueError:
            continue
        valid_dates = {
            released
            for released in release_dates
            if _release_date_valid(
                year=expected_year,
                month=expected_month,
                day=expected_period_end_day,
                released=released,
            )
        }
        if len(valid_dates) != 1:
            continue
        absolute = urljoin("https://m.korea.kr", href)
        try:
            canonical, document_id = _canonical_source_uri(absolute)
        except ValueError:
            continue
        candidate = {
            "source_document_id": document_id,
            "source_uri": canonical,
            "source_title": title_match.group(0),
            "agency": "관세청",
            "release_date": next(iter(valid_dates)),
        }
        previous = candidates.get(document_id)
        if previous is not None and previous != candidate:
            raise ValueError("customs_workday_release_ambiguous")
        candidates[document_id] = candidate
    if not candidates:
        raise ValueError("customs_workday_release_not_found")
    if len(candidates) != 1:
        raise ValueError("customs_workday_release_ambiguous")
    return next(iter(candidates.values()))


def _extract_counts(text: str, *, expected_year: int) -> dict[int, int]:
    year = r"[’']?([0-9]{2}|[0-9]{4})"
    count = r"([0-9]+(?:\.[0-9]+)?)"
    pattern = re.compile(
        rf"조업일수\s*\[\s*\(?\s*{year}\s*\)?\s*{count}\s*일\s*,\s*"
        rf"\(?\s*{year}\s*\)?\s*{count}\s*일\s*\]"
    )
    matches = pattern.findall(text)
    if not matches:
        raise ValueError("customs_workday_line_missing")
    normalized: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for first_year, first_count, second_year, second_count in matches:
        pairs = []
        for raw_year, raw_count in (
            (first_year, first_count),
            (second_year, second_count),
        ):
            parsed_year = int(raw_year)
            if len(raw_year) == 2:
                parsed_year += 2000
            pairs.append((parsed_year, _count_to_tenths(raw_count)))
        normalized.add((pairs[0], pairs[1]))
    if len(normalized) != 1:
        raise ValueError("customs_workday_line_ambiguous")
    pairs = next(iter(normalized))
    result = dict(pairs)
    if set(result) != {expected_year - 1, expected_year}:
        raise ValueError("customs_workday_year_pair_invalid")
    return result


def parse_kcs_workday_release_html(
    raw_html: bytes,
    *,
    source_uri: str,
    source_title: str,
    agency: str,
    release_date: date,
    expected_year: int,
    expected_month: int,
    expected_period_end_day: int,
    first_seen_at_utc: datetime,
) -> list[dict[str, Any]]:
    """KCS 보도자료 한 건에서 전년/당년 누적 조업일수를 추출한다."""
    if agency != "관세청":
        raise ValueError("customs_workday_agency_invalid")
    if type(source_title) is not str or not source_title or len(source_title) > 500:
        raise ValueError("customs_workday_title_invalid")
    if (
        type(expected_year) is not int
        or type(expected_month) is not int
        or not 1 <= expected_month <= 12
        or type(expected_period_end_day) is not int
    ):
        raise ValueError("customs_workday_period_invalid")
    period_kind = _period_kind(expected_year, expected_month, expected_period_end_day)
    if type(release_date) is not date or not _release_date_valid(
        year=expected_year,
        month=expected_month,
        day=expected_period_end_day,
        released=release_date,
    ):
        raise ValueError("customs_workday_release_date_invalid")
    source_uri_canonical, document_id = _canonical_source_uri(source_uri)
    detail_title, detail_release_date, detail_agency = _detail_header(raw_html)
    normalized_source_title = re.sub(r"\s+", " ", source_title).strip()
    if detail_title != normalized_source_title:
        raise ValueError("customs_workday_detail_title_mismatch")
    if detail_agency != agency:
        raise ValueError("customs_workday_detail_agency_mismatch")
    if detail_release_date != release_date:
        raise ValueError("customs_workday_detail_release_date_mismatch")
    text = _visible_text(raw_html)
    title_pattern = _period_title_pattern(
        year=expected_year,
        month=expected_month,
        day=expected_period_end_day,
    )
    if not title_pattern.search(source_title) or not title_pattern.search(text):
        raise ValueError("customs_workday_title_period_mismatch")
    counts = _extract_counts(text, expected_year=expected_year)
    available_at = _canonical_utc(first_seen_at_utc)
    source_hash = hashlib.sha256(raw_html).hexdigest()
    rows: list[dict[str, Any]] = []
    for year in (expected_year - 1, expected_year):
        period_day = (
            monthrange(year, expected_month)[1]
            if period_kind == "month_end"
            else expected_period_end_day
        )
        rows.append(
            {
                "source_record_id": f"{year:04d}{expected_month:02d}{period_day:02d}:workdays",
                "period_year": year,
                "period_month": expected_month,
                "period_end_day": period_day,
                "period_kind": period_kind,
                "workdays_mtd_tenths": counts[year],
                "calendar_domain": CALENDAR_DOMAIN,
                "method_version": METHOD_VERSION,
                "source_document_id": document_id,
                "source_uri": source_uri_canonical,
                "source_title": source_title,
                "source_agency": detail_agency,
                "detail_header_title": detail_title,
                "detail_header_release_date_kst": detail_release_date.isoformat(),
                "detail_header_verified": True,
                "source_document_sha256": source_hash,
                "scheduled_release_date_kst": release_date.isoformat(),
                "source_published_at_utc": None,
                "publication_precision": "date_only",
                "first_seen_at_utc": available_at,
                "available_at_utc": available_at,
                "revision_policy": "append_only_content_hash",
                "supersedes_snapshot_id": None,
                "shadow_only": True,
                "eligible_for_production_score": False,
            }
        )
    return rows


def _utc_clock_value(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("customs_workday_clock_invalid")
    return value.astimezone(timezone.utc)


def _search_url(*, year: int, month: int, page_index: int = 1) -> str:
    if type(page_index) is not int or not 1 <= page_index <= MAX_RELEASE_SEARCH_PAGES:
        raise ValueError("customs_workday_pagination_invalid")
    start = date(year, month, 1)
    next_month = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    params = {
        "pageIndex": str(page_index),
        "repCodeType": "",
        "repCode": "",
        "startDate": start.isoformat(),
        "endDate": (next_month + timedelta(days=2)).isoformat(),
        "srchWord": "수출입 현황",
        "period": "",
    }
    return "https://www.korea.kr/briefing/pressReleaseList.do?" + urlencode(params)


def _read_html_response(
    url: str,
    *,
    transport: Callable[..., Any],
    timeout: float,
) -> bytes:
    request = Request(
        url,
        headers={"User-Agent": "Sanjuk-Shadow-Collector/2B", "Accept": "text/html"},
        method="GET",
    )
    response = transport(request, timeout=timeout)
    try:
        if getattr(response, "status", None) != 200:
            raise ValueError("customs_workday_http_status")
        headers = getattr(response, "headers", {})
        content_type = "" if headers is None else str(headers.get("Content-Type", ""))
        if "text/html" not in content_type.lower():
            raise ValueError("customs_workday_content_type")
        raw = response.read(MAX_RELEASE_HTML_BYTES + 1)
        if type(raw) is not bytes:
            raise ValueError("customs_workday_html_invalid")
        if len(raw) > MAX_RELEASE_HTML_BYTES:
            raise ValueError("customs_workday_html_oversized")
        return raw
    finally:
        response.close()


def _read_release_list_pages(
    *,
    year: int,
    month: int,
    transport: Callable[..., Any],
    timeout: float,
) -> list[bytes]:
    pages: list[bytes] = []
    for page_index in range(1, MAX_RELEASE_SEARCH_PAGES + 1):
        raw = _read_html_response(
            _search_url(year=year, month=month, page_index=page_index),
            transport=transport,
            timeout=timeout,
        )
        pages.append(raw)
        if not _pagination_has_next(raw, expected_page=page_index):
            return pages
    raise ValueError("customs_workday_release_list_incomplete")


def _select_release_candidate(
    pages: list[bytes],
    *,
    expected_year: int,
    expected_month: int,
    expected_period_end_day: int,
) -> dict[str, Any]:
    candidates: dict[str, dict[str, Any]] = {}
    for raw in pages:
        try:
            candidate = parse_kcs_workday_release_list_html(
                raw,
                expected_year=expected_year,
                expected_month=expected_month,
                expected_period_end_day=expected_period_end_day,
            )
        except ValueError as exc:
            if str(exc) == "customs_workday_release_not_found":
                continue
            raise
        document_id = candidate["source_document_id"]
        previous = candidates.get(document_id)
        if previous is not None and previous != candidate:
            raise ValueError("customs_workday_release_ambiguous")
        candidates[document_id] = candidate
    if not candidates:
        raise ValueError("customs_workday_release_not_found")
    if len(candidates) != 1:
        raise ValueError("customs_workday_release_ambiguous")
    return next(iter(candidates.values()))


def _error_type(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return "http"
    if isinstance(exc, (URLError, TimeoutError, OSError)):
        return "network"
    if isinstance(exc, ValueError):
        code = str(exc)
        if code in {
            "customs_workday_http_status",
            "customs_workday_content_type",
            "customs_workday_html_oversized",
        }:
            return code.removeprefix("customs_workday_")
        if code == "customs_workday_release_not_found":
            return "not_found"
        if code == "customs_workday_release_ambiguous":
            return "ambiguous"
        if code == "customs_workday_release_list_incomplete":
            return "incomplete"
        if code == "customs_workday_clock_regression":
            return "clock_regression"
        return "malformed"
    return "internal"


def fetch_kcs_workday_observations(
    periods: list[dict[str, Any]],
    *,
    transport: Callable[..., Any] = urlopen,
    timeout: float = 10.0,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> WorkdayFetchResult:
    """최신 period에 필요한 KCS 조업일수 문서를 공식 목록에서 수집한다."""
    started = _utc_clock_value(clock)
    try:
        if type(periods) is not list or not periods:
            raise ValueError("customs_workday_periods_invalid")
        period_keys: list[tuple[int, int, int]] = []
        for row in periods:
            if type(row) is not dict:
                raise ValueError("customs_workday_period_invalid")
            year = row.get("period_year")
            month = row.get("period_month")
            day = row.get("period_end_day")
            if type(year) is not int or type(month) is not int or type(day) is not int:
                raise ValueError("customs_workday_period_invalid")
            _period_kind(year, month, day)
            period_keys.append((year, month, day))
        year, month, day = max(period_keys)
        required_days = [10] if day == 10 else [10, 20] if day == 20 else [20, day]
        list_pages = _read_release_list_pages(
            year=year,
            month=month,
            transport=transport,
            timeout=timeout,
        )
        documents: list[tuple[int, dict[str, Any], bytes]] = []
        for required_day in required_days:
            candidate = _select_release_candidate(
                list_pages,
                expected_year=year,
                expected_month=month,
                expected_period_end_day=required_day,
            )
            detail_html = _read_html_response(
                candidate["source_uri"],
                transport=transport,
                timeout=timeout,
            )
            documents.append((required_day, candidate, detail_html))
        completed = _utc_clock_value(clock)
        if completed < started:
            raise ValueError("customs_workday_clock_regression")
        rows: list[dict[str, Any]] = []
        for required_day, candidate, detail_html in documents:
            rows.extend(
                parse_kcs_workday_release_html(
                    detail_html,
                    source_uri=candidate["source_uri"],
                    source_title=candidate["source_title"],
                    agency=candidate["agency"],
                    release_date=candidate["release_date"],
                    expected_year=year,
                    expected_month=month,
                    expected_period_end_day=required_day,
                    first_seen_at_utc=completed,
                )
            )
        return WorkdayFetchResult(
            status="success",
            error_type="none",
            rows=tuple(rows),
            started_at_utc=started,
            completed_at_utc=completed,
        )
    except Exception as exc:
        try:
            completed = _utc_clock_value(clock)
        except Exception:
            completed = started
        return WorkdayFetchResult(
            status="failed",
            error_type=_error_type(exc),
            rows=(),
            started_at_utc=started,
            completed_at_utc=completed,
        )
