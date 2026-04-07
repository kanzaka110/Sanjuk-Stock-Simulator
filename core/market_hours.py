"""
장 시간 판별 유틸리티

한국장(KRX)과 미국장(NYSE/NASDAQ)의 개장 여부를 판단한다.
써머타임(DST) 자동 처리 포함.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config.settings import KST

# 미국 동부 표준시 (EST: UTC-5, EDT: UTC-4)
_EST = timezone(timedelta(hours=-5))
_EDT = timezone(timedelta(hours=-4))


def _is_us_dst(dt: datetime) -> bool:
    """미국 써머타임 여부 판별.

    3월 둘째 일요일 02:00 ~ 11월 첫째 일요일 02:00.
    """
    year = dt.year

    # 3월 둘째 일요일
    march_first = datetime(year, 3, 1)
    days_to_sunday = (6 - march_first.weekday()) % 7
    dst_start = march_first + timedelta(days=days_to_sunday + 7)

    # 11월 첫째 일요일
    nov_first = datetime(year, 11, 1)
    days_to_sunday = (6 - nov_first.weekday()) % 7
    dst_end = nov_first + timedelta(days=days_to_sunday)

    naive = dt.replace(tzinfo=None)
    return dst_start <= naive < dst_end


def _to_kst(dt: datetime) -> datetime:
    """datetime을 KST로 변환."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def is_weekday(dt: datetime) -> bool:
    """평일 여부 (월=0 ~ 금=4)."""
    return dt.weekday() < 5


def is_kr_market_open(now: datetime | None = None) -> bool:
    """한국장 개장 여부 (09:00~15:30 KST, 평일)."""
    kst_now = _to_kst(now or datetime.now(KST))
    if not is_weekday(kst_now):
        return False
    t = kst_now.time()
    from datetime import time as dt_time
    return dt_time(9, 0) <= t < dt_time(15, 30)


def is_us_market_open(now: datetime | None = None) -> bool:
    """미국장 개장 여부 (09:30~16:00 ET, 평일).

    KST 기준:
    - 써머타임: 22:30 ~ 익일 05:00
    - 표준시:   23:30 ~ 익일 06:00
    """
    kst_now = _to_kst(now or datetime.now(KST))

    # ET 시간으로 변환
    us_tz = _EDT if _is_us_dst(kst_now) else _EST
    et_now = kst_now.astimezone(us_tz)

    if not is_weekday(et_now):
        return False

    t = et_now.time()
    from datetime import time as dt_time
    return dt_time(9, 30) <= t < dt_time(16, 0)


def is_any_market_open(now: datetime | None = None) -> bool:
    """한국장 또는 미국장 개장 여부."""
    return is_kr_market_open(now) or is_us_market_open(now)


def next_market_open(now: datetime | None = None) -> datetime:
    """다음 장 개장 시각 (KST) 반환. 최대 7일 탐색."""
    kst_now = _to_kst(now or datetime.now(KST))

    for offset_min in range(0, 7 * 24 * 60, 5):
        candidate = kst_now + timedelta(minutes=offset_min)
        if is_any_market_open(candidate):
            return candidate

    # 폴백: 다음 월요일 09:00 KST
    days_ahead = (7 - kst_now.weekday()) % 7 or 7
    next_monday = kst_now.replace(hour=9, minute=0, second=0, microsecond=0)
    return next_monday + timedelta(days=days_ahead)


def market_status_text(now: datetime | None = None) -> str:
    """현재 장 상태 텍스트."""
    kr = is_kr_market_open(now)
    us = is_us_market_open(now)
    if kr and us:
        return "🟢 한국장 + 미국장 개장 중"
    if kr:
        return "🟢 한국장 개장 중"
    if us:
        return "🟢 미국장 개장 중"
    return "🔴 장 마감"
