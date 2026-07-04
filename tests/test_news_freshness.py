"""news_freshness — 뉴스 신선도 필터 테스트."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.news_freshness import (
    FRESHNESS_PROMPT_RULES,
    annotate_news_freshness,
    _line_age_days,
    _parse_dates,
)

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 7, 4, 10, 0, tzinfo=KST)


class TestParseDates:
    def test_iso(self):
        d = _parse_dates("삼성전자 급등 (2026-07-03, Reuters)", NOW)
        assert d and d[0].date().isoformat() == "2026-07-03"

    def test_dotted_and_slash(self):
        assert _parse_dates("(2026.06.30 발표)", NOW)
        assert _parse_dates("(2026/06/30)", NOW)

    def test_korean_with_year(self):
        d = _parse_dates("2026년 6월 28일 공시", NOW)
        assert d[0].date().isoformat() == "2026-06-28"

    def test_korean_without_year_past(self):
        d = _parse_dates("7월 3일 발표", NOW)
        assert d[0].date().isoformat() == "2026-07-03"

    def test_korean_without_year_far_future_rolls_back(self):
        # 지금이 7/4인데 '12월 30일'은 올해 미래(6개월 이내) → 올해로 해석
        d = _parse_dates("12월 30일 FOMC", NOW)
        assert d[0].year == 2026

    def test_no_date(self):
        assert _parse_dates("반도체 업황 개선 기대", NOW) == []

    def test_invalid_date_ignored(self):
        assert _parse_dates("2026-13-45 오류", NOW) == []


class TestLineAge:
    def test_today_is_zero(self):
        assert _line_age_days("오늘 (2026-07-04) 급등", NOW) == 0

    def test_future_negative(self):
        assert _line_age_days("실적 발표 2026-07-10 예정", NOW) < 0

    def test_multiple_dates_uses_latest(self):
        line = "2026-06-01 저점 이후 2026-07-03 신고가"
        assert _line_age_days(line, NOW) == 1

    def test_none_when_no_date(self):
        assert _line_age_days("수급 개선", NOW) is None


class TestAnnotate:
    def test_drops_older_than_7d(self):
        text = "옛날 기사 (2026-06-20, WSJ)\n최신 기사 (2026-07-04, Reuters)"
        out = annotate_news_freshness(text, now=NOW)
        assert "옛날 기사" not in out
        assert "최신 기사" in out
        assert "제거" in out

    def test_marks_2_to_7d(self):
        text = "사흘 전 기사 (2026-07-01, CNBC)"
        out = annotate_news_freshness(text, now=NOW)
        assert "⚠️[3일 전 정보]" in out
        assert "근거로 쓰지 말 것" in out

    def test_keeps_fresh_and_undated(self):
        text = "오늘 기사 (2026-07-04)\n날짜 없는 시황 코멘트"
        out = annotate_news_freshness(text, now=NOW)
        assert "오늘 기사" in out
        assert "날짜 없는 시황 코멘트" in out
        assert "⚠️" not in out.split("[뉴스 신선도 감사]")[0]

    def test_keeps_future_calendar(self):
        text = "FOMC 2026-07-15 예정\n실적 발표 7월 10일"
        out = annotate_news_freshness(text, now=NOW)
        assert "FOMC 2026-07-15 예정" in out
        assert "실적 발표 7월 10일" in out

    def test_audit_footer_present(self):
        out = annotate_news_freshness("본문", now=NOW)
        assert "[뉴스 신선도 감사]" in out

    def test_empty_passthrough(self):
        assert annotate_news_freshness("", now=NOW) == ""

    def test_yesterday_not_marked(self):
        text = "어제 기사 (2026-07-03, Reuters)"
        out = annotate_news_freshness(text, now=NOW)
        assert "⚠️" not in out.split("[뉴스 신선도 감사]")[0]


class TestPromptRules:
    def test_rules_mention_date_format_and_hallucination(self):
        assert "YYYY-MM-DD" in FRESHNESS_PROMPT_RULES
        assert "만들어내지 마라" in FRESHNESS_PROMPT_RULES
