"""
이전 브리핑 컨텍스트 — 중복/반복 방지용 프롬프트 주입 텍스트 생성.

브리핑 품질 문제 중 "이미 지난 이야기를 계속 반복"을 구조적으로 막는다.
- 최근 N일 브리핑 요약(briefing_archive)을 다음 브리핑 프롬프트에 주입
- 최근 반복 추천 통계(memory.predictions)를 주입해 같은 추천 재탕 차단
- 실패해도 브리핑 자체는 멈추면 안 됨 (모든 함수 예외 삼킴)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
log = logging.getLogger(__name__)

_RECAP_DAYS = 3
_RECAP_LIMIT = 6
_REPEAT_DAYS = 14
_REPEAT_MIN_COUNT = 3
_REPEAT_LIMIT = 10
_SUMMARY_MAX_CHARS = 180


def recent_briefing_recap(days: int = _RECAP_DAYS, limit: int = _RECAP_LIMIT) -> list[dict]:
    """최근 브리핑 요약 목록. [{created_at, briefing_type, summary, tickers}] 최신순."""
    try:
        from core.briefing_archive import list_briefing_archives
        rows = list_briefing_archives(limit=limit, days=days)
        out = []
        for r in rows:
            summary = (r.get("summary") or "").strip().replace("\n", " ")
            out.append({
                "created_at": (r.get("created_at") or "")[:16],
                "briefing_type": r.get("briefing_type") or "",
                "summary": summary[:_SUMMARY_MAX_CHARS],
                "tickers": r.get("tickers") or [],
            })
        return out
    except Exception as e:
        log.warning("이전 브리핑 recap 조회 실패: %s", e)
        return []


def repeated_recommendations(
    days: int = _REPEAT_DAYS,
    min_count: int = _REPEAT_MIN_COUNT,
    limit: int = _REPEAT_LIMIT,
) -> list[dict]:
    """최근 N일간 같은 ticker+signal 반복 추천 통계.

    [{ticker, name, signal, count, last_at}] count 내림차순.
    """
    try:
        from core.memory import _get_conn
        cutoff = (datetime.now(KST) - timedelta(days=days)).isoformat()
        rows = _get_conn().execute(
            """SELECT ticker, MAX(name) AS name, signal,
                      COUNT(*) AS cnt, MAX(created_at) AS last_at
               FROM predictions
               WHERE created_at >= ?
               GROUP BY ticker, signal
               HAVING cnt >= ?
               ORDER BY cnt DESC
               LIMIT ?""",
            (cutoff, min_count, limit),
        ).fetchall()
        return [
            {
                "ticker": r["ticker"],
                "name": r["name"] or r["ticker"],
                "signal": r["signal"],
                "count": r["cnt"],
                "last_at": (r["last_at"] or "")[:16],
            }
            for r in rows
        ]
    except Exception as e:
        log.warning("반복 추천 통계 조회 실패: %s", e)
        return []


def build_previous_briefing_context() -> str:
    """프롬프트 주입용 이전 브리핑 컨텍스트 텍스트. 데이터 없으면 빈 문자열."""
    recaps = recent_briefing_recap()
    repeats = repeated_recommendations()
    if not recaps and not repeats:
        return ""

    lines: list[str] = []

    if recaps:
        lines.append("【최근 브리핑 요약 — 이미 전달된 내용】")
        for r in recaps:
            tks = ", ".join(r["tickers"][:8]) if r["tickers"] else "-"
            lines.append(f"  • {r['created_at']} [{r['briefing_type']}] {r['summary']}")
            lines.append(f"    관련 종목: {tks}")

    if repeats:
        lines.append("")
        lines.append(f"【반복 추천 경고 — 최근 {_REPEAT_DAYS}일 동일 신호 {_REPEAT_MIN_COUNT}회 이상】")
        for r in repeats:
            lines.append(
                f"  ⚠️ {r['name']}({r['ticker']}) '{r['signal']}' {r['count']}회 반복"
                f" (최근 {r['last_at']})"
            )

    lines.append("")
    lines.append("→ 중복 방지 절대 규칙:")
    lines.append(
        "  1. 위 요약과 동일한 논지/추천은 재서술 금지 — '변경 없음: 한 줄'로만 압축하고,"
        " 이번 브리핑은 직전 대비 '변한 것'(가격/뉴스/수급/이벤트)만 상세 서술하라."
    )
    lines.append(
        "  2. 반복 추천 경고 종목은 새 카탈리스트나 유의미한 가격 변화가 없으면 같은 신호를"
        " 반복하지 마라. 반복이 정당하면 반드시 '(N회째, 새 근거: ...)'를 명시하라."
    )
    lines.append(
        "  3. 관망 반복은 정보가 아니다 — 관망을 3회 이상 반복한 종목은 '관망 유지' 목록에"
        " 티커만 나열하고 본문 서술을 생략하라."
    )
    return "\n".join(lines)


__all__ = [
    "recent_briefing_recap",
    "repeated_recommendations",
    "build_previous_briefing_context",
]
