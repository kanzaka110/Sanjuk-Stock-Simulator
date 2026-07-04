"""briefing_context — 이전 브리핑 컨텍스트 주입 테스트."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.briefing_context as bc


def _archive_row(created_at="2026-07-03T20:13", btype="US_NIGHT",
                 summary="반도체 투매 지속, 관망", tickers=None):
    return {
        "created_at": created_at,
        "briefing_type": btype,
        "summary": summary,
        "tickers": tickers if tickers is not None else ["NVDA", "MU"],
    }


class TestRecap:
    def test_returns_recent_archives(self):
        with mock.patch("core.briefing_archive.list_briefing_archives",
                        return_value=[_archive_row()]):
            out = bc.recent_briefing_recap()
        assert len(out) == 1
        assert out[0]["briefing_type"] == "US_NIGHT"
        assert out[0]["tickers"] == ["NVDA", "MU"]

    def test_summary_truncated_and_flattened(self):
        long = "줄1\n줄2 " + "x" * 500
        with mock.patch("core.briefing_archive.list_briefing_archives",
                        return_value=[_archive_row(summary=long)]):
            out = bc.recent_briefing_recap()
        assert "\n" not in out[0]["summary"]
        assert len(out[0]["summary"]) <= 180

    def test_archive_failure_returns_empty(self):
        with mock.patch("core.briefing_archive.list_briefing_archives",
                        side_effect=RuntimeError("db down")):
            assert bc.recent_briefing_recap() == []


class TestRepeated:
    def _conn_with(self, rows):
        conn = mock.MagicMock()
        conn.execute.return_value.fetchall.return_value = rows
        return conn

    def test_returns_repeat_stats(self):
        row = {"ticker": "MU", "name": "마이크론", "signal": "관망",
               "cnt": 11, "last_at": "2026-07-03T09:06:33"}
        with mock.patch("core.memory._get_conn",
                        return_value=self._conn_with([row])):
            out = bc.repeated_recommendations()
        assert out == [{"ticker": "MU", "name": "마이크론", "signal": "관망",
                        "count": 11, "last_at": "2026-07-03T09:06"}]

    def test_db_failure_returns_empty(self):
        with mock.patch("core.memory._get_conn",
                        side_effect=RuntimeError("no db")):
            assert bc.repeated_recommendations() == []


class TestBuildContext:
    def test_empty_when_no_data(self):
        with mock.patch.object(bc, "recent_briefing_recap", return_value=[]), \
             mock.patch.object(bc, "repeated_recommendations", return_value=[]):
            assert bc.build_previous_briefing_context() == ""

    def test_contains_recap_and_rules(self):
        with mock.patch.object(bc, "recent_briefing_recap",
                               return_value=[_archive_row()]), \
             mock.patch.object(bc, "repeated_recommendations", return_value=[]):
            text = bc.build_previous_briefing_context()
        assert "최근 브리핑 요약" in text
        assert "US_NIGHT" in text
        assert "중복 방지 절대 규칙" in text
        assert "변경 없음" in text

    def test_contains_repeat_warning(self):
        rep = {"ticker": "MU", "name": "마이크론", "signal": "관망",
               "count": 11, "last_at": "2026-07-03T09:06"}
        with mock.patch.object(bc, "recent_briefing_recap", return_value=[]), \
             mock.patch.object(bc, "repeated_recommendations", return_value=[rep]):
            text = bc.build_previous_briefing_context()
        assert "반복 추천 경고" in text
        assert "'관망' 11회 반복" in text
        assert "새 근거" in text

    def test_watch_repeat_rule_present(self):
        rep = {"ticker": "MU", "name": "마이크론", "signal": "관망",
               "count": 3, "last_at": "2026-07-03T09:06"}
        with mock.patch.object(bc, "recent_briefing_recap", return_value=[]), \
             mock.patch.object(bc, "repeated_recommendations", return_value=[rep]):
            text = bc.build_previous_briefing_context()
        assert "관망 유지" in text
