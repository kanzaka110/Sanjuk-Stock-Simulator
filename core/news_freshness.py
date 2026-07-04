"""
뉴스 신선도 필터 — LLM 수집 뉴스 텍스트의 날짜 기반 후처리.

문제: news.py의 "24시간 우선, 1주일 제외" 규칙이 프롬프트 지시뿐이라
LLM이 지난 기사를 섞어도 걸러지지 않았다.

해결: 수집 텍스트를 줄 단위로 스캔해 발행일을 파싱하고,
- 7일 초과 과거 기사 줄 → 드롭
- 2~7일 과거 → "⚠️ N일 전 정보" 마킹 (판단 근거 가중치 하향 유도)
- 미래 날짜(실적/경제 캘린더 일정) → 보존
- 날짜 없음 → 보존 (판단 불가)
결과 하단에 신선도 감사 요약을 붙인다.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
log = logging.getLogger(__name__)

# 드롭/마킹 임계값 (일)
STALE_DROP_DAYS = 7
STALE_MARK_DAYS = 2

# 날짜 패턴: 2026-07-03 / 2026.07.03 / 2026/07/03 / 7월 3일 / 07-03(연도 없음은 미지원)
_RE_ISO = re.compile(r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})")
_RE_KR = re.compile(r"(?:(20\d{2})년\s*)?(\d{1,2})월\s*(\d{1,2})일")


def _parse_dates(line: str, now: datetime) -> list[datetime]:
    """줄에서 날짜들을 추출. 연도 없는 한국식 날짜는 가장 가까운 해석을 채택."""
    dates: list[datetime] = []
    for m in _RE_ISO.finditer(line):
        try:
            dates.append(datetime(int(m.group(1)), int(m.group(2)),
                                  int(m.group(3)), tzinfo=KST))
        except ValueError:
            continue
    for m in _RE_KR.finditer(line):
        year_s, mon_s, day_s = m.groups()
        try:
            mon, day = int(mon_s), int(day_s)
            if year_s:
                dates.append(datetime(int(year_s), mon, day, tzinfo=KST))
                continue
            # 연도 미상: 올해로 해석하되, 6개월 이상 미래면 작년으로 본다
            cand = datetime(now.year, mon, day, tzinfo=KST)
            if (cand - now).days > 183:
                cand = datetime(now.year - 1, mon, day, tzinfo=KST)
            dates.append(cand)
        except ValueError:
            continue
    return dates


def _line_age_days(line: str, now: datetime) -> int | None:
    """줄의 '기사 나이'(일). 날짜 없으면 None, 미래 날짜만 있으면 음수."""
    dates = _parse_dates(line, now)
    if not dates:
        return None
    # 여러 날짜면 가장 최근(=가장 큰) 날짜 기준 — 기사 발행일이 보통 최신
    latest = max(dates)
    return (now.date() - latest.date()).days


def annotate_news_freshness(text: str, now: datetime | None = None) -> str:
    """뉴스 텍스트 신선도 후처리. 실패해도 원문을 그대로 반환."""
    if not text or not text.strip():
        return text
    try:
        now = now or datetime.now(KST)
        kept: list[str] = []
        dropped = 0
        marked = 0
        for line in text.splitlines():
            age = _line_age_days(line, now)
            if age is None or age <= 0:
                kept.append(line)  # 날짜 없음 / 오늘 / 미래 일정 → 보존
            elif age > STALE_DROP_DAYS:
                dropped += 1
            elif age >= STALE_MARK_DAYS:
                marked += 1
                kept.append(f"⚠️[{age}일 전 정보] {line}")
            else:
                kept.append(line)

        out = "\n".join(kept)
        audit = [
            "",
            f"[뉴스 신선도 감사] 기준 {now.strftime('%Y-%m-%d %H:%M KST')}"
            f" · {STALE_DROP_DAYS}일 초과 기사 {dropped}줄 제거"
            f" · {STALE_MARK_DAYS}~{STALE_DROP_DAYS}일 경과 {marked}줄 ⚠️ 마킹",
        ]
        if marked:
            audit.append(
                "→ ⚠️ 마킹된 줄은 오래된 정보 — 이미 가격에 반영됐을 가능성이 크므로"
                " 신규 매수/매도 근거로 쓰지 말 것."
            )
        return out + "\n".join(audit)
    except Exception as e:
        log.warning("뉴스 신선도 필터 실패 — 원문 유지: %s", e)
        return text


FRESHNESS_PROMPT_RULES = (
    "\n\n※ 최신성 규칙 (필수):"
    "\n1. 최근 24시간 이내 기사를 우선하고, 모든 사실/기사에 발행 날짜를"
    " 'YYYY-MM-DD' 형식으로 반드시 병기하라 (예: (2026-07-03, Reuters))."
    "\n2. 발행일이 48시간을 넘은 기사는 원칙적으로 제외하라"
    " (정책/구조적 이슈는 예외 — 단, 날짜 명시 필수)."
    "\n3. 날짜를 확인할 수 없는 정보는 '(날짜 미상)'으로 표기하라."
    "\n4. 검색 결과가 없으면 없다고 써라 — 기억이나 추정으로 기사를 만들어내지 마라."
)


__all__ = [
    "annotate_news_freshness",
    "FRESHNESS_PROMPT_RULES",
    "STALE_DROP_DAYS",
    "STALE_MARK_DAYS",
]
