"""공식 KCS 보도자료의 조업일수 lineage 계약."""

from datetime import date, datetime, timezone
import hashlib

import pytest


UTC = timezone.utc
FIRST_SEEN = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
SOURCE_URI = (
    "https://m.korea.kr/briefing/pressReleaseView.do?newsId=156665186&"
    "pageIndex=1&srchWord=%EC%88%98%EC%B6%9C%EC%9E%85"
)
TITLE = "’24년 12월 1일 ~ 12월 10일 수출입 현황"


def _official_html(
    *,
    workday_line=None,
    title=TITLE,
    published="2024.12.11",
    agency="관세청",
):
    line = workday_line or "※ 조업일수 [(’23) 7.0일, (’24) 7.5일] 고려 시 일평균수출액 증가"
    return f"""<!doctype html>
    <html><head><title>{title} - 대한민국 정책브리핑</title></head>
    <body><main><div class="article_head"><h1>{title}</h1><div class="info">
    <span>{published}</span><span><a class="gotosite" href="/news/ministryNewsList.do?repCode=B00003">{agency}</a></span>
    </div></div><p>{line}</p></main></body></html>""".encode()


def test_parse_day10_preserves_fixed_point_and_date_only_lineage():
    from core.customs_export_workdays import parse_kcs_workday_release_html

    raw = _official_html()
    rows = parse_kcs_workday_release_html(
        raw,
        source_uri=SOURCE_URI,
        source_title=TITLE,
        agency="관세청",
        release_date=date(2024, 12, 11),
        expected_year=2024,
        expected_month=12,
        expected_period_end_day=10,
        first_seen_at_utc=FIRST_SEEN,
    )

    assert [(row["period_year"], row["workdays_mtd_tenths"]) for row in rows] == [
        (2023, 70),
        (2024, 75),
    ]
    assert all(row["period_month"] == 12 for row in rows)
    assert all(row["period_end_day"] == 10 for row in rows)
    assert all(row["period_kind"] == "day_10" for row in rows)
    assert all(row["calendar_domain"] == "KCS_REPORTED_OPERATING_DAYS" for row in rows)
    assert all(row["method_version"] == "korea-kr-kcs-press-release-v1" for row in rows)
    assert all(row["source_document_id"] == "156665186" for row in rows)
    assert all(
        row["source_uri"]
        == "https://m.korea.kr/briefing/pressReleaseView.do?newsId=156665186"
        for row in rows
    )
    assert all(row["source_document_sha256"] == hashlib.sha256(raw).hexdigest() for row in rows)
    assert all(row["scheduled_release_date_kst"] == "2024-12-11" for row in rows)
    assert all(row["source_published_at_utc"] is None for row in rows)
    assert all(row["publication_precision"] == "date_only" for row in rows)
    assert all(row["available_at_utc"] == "2026-07-17T12:00:00.000000Z" for row in rows)
    assert all(row["shadow_only"] is True for row in rows)
    assert all(row["eligible_for_production_score"] is False for row in rows)


def test_parse_accepts_official_provisional_detail_title_as_lineage():
    from core.customs_export_workdays import parse_kcs_workday_release_html

    detail_title = TITLE + " [잠정치]"
    rows = parse_kcs_workday_release_html(
        _official_html(title=detail_title),
        source_uri=SOURCE_URI,
        source_title=TITLE,
        agency="관세청",
        release_date=date(2024, 12, 11),
        expected_year=2024,
        expected_month=12,
        expected_period_end_day=10,
        first_seen_at_utc=FIRST_SEEN,
    )

    assert all(row["source_title"] == detail_title for row in rows)
    assert all(row["detail_header_title"] == detail_title for row in rows)


def test_parse_accepts_half_day_decimal_but_rejects_non_tenth_precision():
    from core.customs_export_workdays import parse_kcs_workday_release_html

    rows = parse_kcs_workday_release_html(
        _official_html(workday_line="조업일수 [(’23) 8.5일, (’24) 9.0일]"),
        source_uri=SOURCE_URI,
        source_title=TITLE,
        agency="관세청",
        release_date=date(2024, 12, 11),
        expected_year=2024,
        expected_month=12,
        expected_period_end_day=10,
        first_seen_at_utc=FIRST_SEEN,
    )
    assert [row["workdays_mtd_tenths"] for row in rows] == [85, 90]

    with pytest.raises(ValueError, match="customs_workday_count_precision_invalid"):
        parse_kcs_workday_release_html(
            _official_html(workday_line="조업일수 [(’23) 8.25일, (’24) 9.0일]"),
            source_uri=SOURCE_URI,
            source_title=TITLE,
            agency="관세청",
            release_date=date(2024, 12, 11),
            expected_year=2024,
            expected_month=12,
            expected_period_end_day=10,
            first_seen_at_utc=FIRST_SEEN,
        )


@pytest.mark.parametrize(
    ("source_uri", "agency", "error"),
    [
        ("https://example.com/briefing?newsId=156665186", "관세청", "source_uri_invalid"),
        (SOURCE_URI, "한국거래소", "agency_invalid"),
    ],
)
def test_parse_rejects_non_official_source_or_wrong_agency(source_uri, agency, error):
    from core.customs_export_workdays import parse_kcs_workday_release_html

    with pytest.raises(ValueError, match=f"customs_workday_{error}"):
        parse_kcs_workday_release_html(
            _official_html(),
            source_uri=source_uri,
            source_title=TITLE,
            agency=agency,
            release_date=date(2024, 12, 11),
            expected_year=2024,
            expected_month=12,
            expected_period_end_day=10,
            first_seen_at_utc=FIRST_SEEN,
        )


def test_parse_rejects_title_period_mismatch_and_missing_line():
    from core.customs_export_workdays import parse_kcs_workday_release_html

    kwargs = dict(
        source_uri=SOURCE_URI,
        source_title=TITLE,
        agency="관세청",
        release_date=date(2024, 12, 11),
        expected_year=2024,
        expected_month=12,
        expected_period_end_day=10,
        first_seen_at_utc=FIRST_SEEN,
    )
    with pytest.raises(ValueError, match="customs_workday_detail_title_mismatch"):
        parse_kcs_workday_release_html(
            _official_html(title="’24년 11월 1일 ~ 11월 10일 수출입 현황"),
            **kwargs,
        )
    with pytest.raises(ValueError, match="customs_workday_line_missing"):
        parse_kcs_workday_release_html(
            _official_html(workday_line="조업일수 정보 없음"),
            **kwargs,
        )


@pytest.mark.parametrize(
    ("detail_html", "error"),
    (
        (
            _official_html(title=TITLE + " (정정 아닌 다른 제목)"),
            "customs_workday_detail_title_mismatch",
        ),
        (
            _official_html(agency="한국거래소"),
            "customs_workday_detail_agency_mismatch",
        ),
        (
            _official_html(published="2024.12.31"),
            "customs_workday_detail_release_date_mismatch",
        ),
    ),
    ids=("title", "agency", "release_date"),
)
def test_detail_header_metadata_must_match_release_list(detail_html, error):
    from core.customs_export_workdays import parse_kcs_workday_release_html

    with pytest.raises(ValueError, match=error):
        parse_kcs_workday_release_html(
            detail_html,
            source_uri=SOURCE_URI,
            source_title=TITLE,
            agency="관세청",
            release_date=date(2024, 12, 11),
            expected_year=2024,
            expected_month=12,
            expected_period_end_day=10,
            first_seen_at_utc=FIRST_SEEN,
        )


def test_release_date_is_validated_but_never_becomes_available_at():
    from core.customs_export_workdays import parse_kcs_workday_release_html

    with pytest.raises(ValueError, match="customs_workday_release_date_invalid"):
        parse_kcs_workday_release_html(
            _official_html(),
            source_uri=SOURCE_URI,
            source_title=TITLE,
            agency="관세청",
            release_date=date(2024, 12, 10),
            expected_year=2024,
            expected_month=12,
            expected_period_end_day=10,
            first_seen_at_utc=FIRST_SEEN,
        )


def _release_list_html(
    *,
    duplicate_exact=False,
    page_index=1,
    total_pages=1,
    include_day10=True,
    include_day20=True,
    day10_document_id="156665186",
):
    duplicate = ""
    if duplicate_exact:
        duplicate = """
        <a href="/briefing/pressReleaseView.do?newsId=156665999">
          ’24년 12월 1일 ~ 12월 10일 수출입 현황 2024-12-11 관세청
        </a>"""
    day20 = (
        """<a href="/briefing/pressReleaseView.do?newsId=156667040">
        ’24년 12월 1일 ~ 12월 20일 수출입 현황 2024-12-23 관세청
      </a>"""
        if include_day20
        else ""
    )
    day10 = (
        f"""<a href="/briefing/pressReleaseView.do?newsId={day10_document_id}&pageIndex=1">
        ’24년 12월 1일 ~ 12월 10일 수출입 현황 2024-12-11 관세청
      </a>"""
        if include_day10
        else ""
    )
    paging = "".join(
        (
            f'<span class="num on"><a href="#pageLink" onclick="return false;" '
            f'class="on" title="현재페이지">{page}</a></span>'
            if page == page_index
            else f'<span class="num"><a href="#pageLink" '
            f'onclick="pageLink({page}); return false;" title="{page}페이지">'
            f"{page}</a></span>"
        )
        for page in range(1, total_pages + 1)
    )
    return f"""<html><body>
      {day20}
      {day10}
      <a href="/briefing/pressReleaseView.do?newsId=156665187">
        ’24년 12월 1일 ~ 12월 10일 수출입 현황 2024-12-11 한국거래소
      </a>
      {duplicate}
      <div class="paging">{paging}</div>
    </body></html>""".encode()


def test_release_list_selects_one_exact_kcs_period():
    from core.customs_export_workdays import parse_kcs_workday_release_list_html

    candidate = parse_kcs_workday_release_list_html(
        _release_list_html(),
        expected_year=2024,
        expected_month=12,
        expected_period_end_day=10,
    )

    assert candidate == {
        "source_document_id": "156665186",
        "source_uri": "https://m.korea.kr/briefing/pressReleaseView.do?newsId=156665186",
        "source_title": "’24년 12월 1일 ~ 12월 10일 수출입 현황",
        "agency": "관세청",
        "release_date": date(2024, 12, 11),
    }


def test_release_list_rejects_ambiguous_or_missing_exact_period():
    from core.customs_export_workdays import parse_kcs_workday_release_list_html

    with pytest.raises(ValueError, match="customs_workday_release_ambiguous"):
        parse_kcs_workday_release_list_html(
            _release_list_html(duplicate_exact=True),
            expected_year=2024,
            expected_month=12,
            expected_period_end_day=10,
        )

    with pytest.raises(ValueError, match="customs_workday_release_not_found"):
        parse_kcs_workday_release_list_html(
            _release_list_html(),
            expected_year=2024,
            expected_month=11,
            expected_period_end_day=10,
        )


def test_fetch_day20_discovers_once_and_returns_complete_workday_vector():
    from core.customs_export_workdays import fetch_kcs_workday_observations

    d20_title = "’24년 12월 1일 ~ 12월 20일 수출입 현황"
    detail_by_id = {
        "156665186": _official_html(
            workday_line="조업일수 [(’23) 7.0일, (’24) 7.5일]"
        ),
        "156667040": _official_html(
            title=d20_title,
            published="2024.12.23",
            workday_line="조업일수 [(’23) 14.0일, (’24) 15.0일]",
        ),
    }
    calls = []

    class Response:
        status = 200
        headers = {"Content-Type": "text/html; charset=UTF-8"}

        def __init__(self, body):
            self.body = body

        def read(self, limit):
            return self.body[:limit]

        def close(self):
            pass

    def transport(request, *, timeout):
        calls.append((request.full_url, timeout))
        if "pressReleaseList.do" in request.full_url:
            return Response(_release_list_html())
        news_id = request.full_url.split("newsId=", 1)[1]
        return Response(detail_by_id[news_id])

    moments = iter(
        (
            datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
            datetime(2026, 7, 17, 12, 0, 2, tzinfo=UTC),
        )
    )
    result = fetch_kcs_workday_observations(
        [{"period_year": 2024, "period_month": 12, "period_end_day": 20}],
        transport=transport,
        timeout=3.5,
        clock=lambda: next(moments),
    )

    assert result.status == "success"
    assert result.error_type == "none"
    assert len(result.rows) == 4
    assert [(row["period_year"], row["period_end_day"]) for row in result.rows] == [
        (2023, 10),
        (2024, 10),
        (2023, 20),
        (2024, 20),
    ]
    assert all(
        row["available_at_utc"] == "2026-07-17T12:00:02.000000Z"
        for row in result.rows
    )
    assert len(calls) == 3
    assert sum("pressReleaseList.do" in url for url, _ in calls) == 1
    assert all(timeout == 3.5 for _, timeout in calls)


@pytest.mark.parametrize(
    ("detail_html", "expected_error_type"),
    (
        (_official_html(agency="한국거래소"), "detail_agency_mismatch"),
        (
            _official_html(published="2024.12.31"),
            "detail_release_date_mismatch",
        ),
        (_official_html(workday_line="조업일수 정보 없음"), "line_missing"),
    ),
    ids=("agency", "release_date", "workday_line"),
)
def test_fetch_preserves_specific_detail_lineage_error_type(
    detail_html, expected_error_type
):
    from core.customs_export_workdays import fetch_kcs_workday_observations

    class Response:
        status = 200
        headers = {"Content-Type": "text/html; charset=UTF-8"}

        def __init__(self, body):
            self.body = body

        def read(self, limit):
            return self.body[:limit]

        def close(self):
            pass

    def transport(request, *, timeout):
        del timeout
        if "pressReleaseList.do" in request.full_url:
            return Response(_release_list_html(include_day20=False))
        return Response(detail_html)

    moments = iter((FIRST_SEEN, FIRST_SEEN.replace(second=2)))
    result = fetch_kcs_workday_observations(
        [{"period_year": 2024, "period_month": 12, "period_end_day": 10}],
        transport=transport,
        clock=lambda: next(moments),
    )

    assert result.status == "failed"
    assert result.error_type == expected_error_type
    assert result.rows == ()


def test_fetch_scans_later_pages_before_selecting_exact_release():
    from urllib.parse import parse_qs, urlsplit

    from core.customs_export_workdays import fetch_kcs_workday_observations

    calls = []

    class Response:
        status = 200
        headers = {"Content-Type": "text/html"}

        def __init__(self, body):
            self.body = body

        def read(self, limit):
            return self.body[:limit]

        def close(self):
            pass

    def transport(request, *, timeout):
        calls.append(request.full_url)
        if "pressReleaseList.do" in request.full_url:
            page = int(parse_qs(urlsplit(request.full_url).query)["pageIndex"][0])
            return Response(
                _release_list_html(
                    page_index=page,
                    total_pages=2,
                    include_day10=page == 2,
                    include_day20=False,
                    day10_document_id="156665999",
                )
            )
        return Response(_official_html())

    moments = iter((FIRST_SEEN, FIRST_SEEN.replace(second=2)))
    result = fetch_kcs_workday_observations(
        [{"period_year": 2024, "period_month": 12, "period_end_day": 10}],
        transport=transport,
        clock=lambda: next(moments),
    )

    assert result.status == "success"
    assert {row["source_document_id"] for row in result.rows} == {"156665999"}
    assert sum("pressReleaseList.do" in url for url in calls) == 2


def test_fetch_rejects_exact_release_duplicated_across_pages():
    from urllib.parse import parse_qs, urlsplit

    from core.customs_export_workdays import fetch_kcs_workday_observations

    list_calls = []

    class Response:
        status = 200
        headers = {"Content-Type": "text/html"}

        def __init__(self, body):
            self.body = body

        def read(self, limit):
            return self.body[:limit]

        def close(self):
            pass

    def transport(request, *, timeout):
        if "pressReleaseList.do" not in request.full_url:
            return Response(_official_html())
        page = int(parse_qs(urlsplit(request.full_url).query)["pageIndex"][0])
        list_calls.append(page)
        return Response(
            _release_list_html(
                page_index=page,
                total_pages=2,
                include_day10=True,
                include_day20=False,
                day10_document_id="156665186" if page == 1 else "156665999",
            )
        )

    moments = iter((FIRST_SEEN, FIRST_SEEN.replace(second=2)))
    result = fetch_kcs_workday_observations(
        [{"period_year": 2024, "period_month": 12, "period_end_day": 10}],
        transport=transport,
        clock=lambda: next(moments),
    )

    assert result.status == "failed"
    assert result.error_type == "ambiguous"
    assert result.rows == ()
    assert list_calls == [1, 2]


def test_fetch_rejects_pagination_above_explicit_scan_bound():
    from core.customs_export_workdays import fetch_kcs_workday_observations

    class Response:
        status = 200
        headers = {"Content-Type": "text/html"}

        def read(self, limit):
            return _release_list_html(total_pages=21)[:limit]

        def close(self):
            pass

    moments = iter((FIRST_SEEN, FIRST_SEEN.replace(second=2)))
    result = fetch_kcs_workday_observations(
        [{"period_year": 2024, "period_month": 12, "period_end_day": 10}],
        transport=lambda *_args, **_kwargs: Response(),
        clock=lambda: next(moments),
    )

    assert result.status == "failed"
    assert result.error_type == "incomplete"
    assert result.rows == ()


@pytest.mark.parametrize("mode", ("missing", "wrong_current_page"))
def test_fetch_rejects_incomplete_or_mismatched_pagination_metadata(mode):
    from urllib.parse import parse_qs, urlsplit

    from core.customs_export_workdays import fetch_kcs_workday_observations

    class Response:
        status = 200
        headers = {"Content-Type": "text/html"}

        def __init__(self, body):
            self.body = body

        def read(self, limit):
            return self.body[:limit]

        def close(self):
            pass

    def transport(request, *, timeout):
        if mode == "missing":
            return Response(b"<html><body>no pagination metadata</body></html>")
        page = int(parse_qs(urlsplit(request.full_url).query)["pageIndex"][0])
        return Response(
            _release_list_html(
                page_index=1 if page == 2 else page,
                total_pages=2,
                include_day10=False,
                include_day20=False,
            )
        )

    moments = iter((FIRST_SEEN, FIRST_SEEN.replace(second=2)))
    result = fetch_kcs_workday_observations(
        [{"period_year": 2024, "period_month": 12, "period_end_day": 10}],
        transport=transport,
        clock=lambda: next(moments),
    )

    assert result.status == "failed"
    assert result.error_type == "malformed"
    assert result.rows == ()


def test_fetch_network_failure_is_typed_without_exception_text():
    from urllib.error import URLError

    from core.customs_export_workdays import fetch_kcs_workday_observations

    moments = iter(
        (
            datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
            datetime(2026, 7, 17, 12, 0, 1, tzinfo=UTC),
        )
    )

    def transport(*_args, **_kwargs):
        raise URLError("serviceKey=must-not-escape")

    result = fetch_kcs_workday_observations(
        [{"period_year": 2024, "period_month": 12, "period_end_day": 10}],
        transport=transport,
        clock=lambda: next(moments),
    )

    assert result.status == "failed"
    assert result.error_type == "network"
    assert result.rows == ()
    assert "must-not-escape" not in repr(result)
