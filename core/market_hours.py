"""
장 시간 판별 유틸리티

한국장(KRX)과 미국장(NYSE/NASDAQ)의 개장 여부를 판단한다.
써머타임(DST) 자동 처리 포함.
"""

from __future__ import annotations

from datetime import date, datetime, time as dt_time, timedelta, timezone

from config.settings import KST

# 미국 동부 표준시 (EST: UTC-5, EDT: UTC-4)
_EST = timezone(timedelta(hours=-5))
_EDT = timezone(timedelta(hours=-4))

# KRX 2026 weekday closures: annual KRX calendar (16 days) plus the
# 2026-05-19 KRX update adding Constitution Day (Jul 17). Sep 28 is open.
_KRX_HOLIDAYS_2026 = frozenset({
    date(2026, 1, 1),
    date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 3, 2),
    date(2026, 5, 1), date(2026, 5, 5), date(2026, 5, 25),
    date(2026, 6, 3),
    date(2026, 7, 17),
    date(2026, 8, 17),
    date(2026, 9, 24), date(2026, 9, 25),
    date(2026, 10, 5), date(2026, 10, 9),
    date(2026, 12, 25), date(2026, 12, 31),
})


def _observed_fixed_holiday(day: date) -> date:
    """NYSE fixed-date holiday observation (Saturday→Friday, Sunday→Monday)."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (nth - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        first_next = date(year + 1, 1, 1)
    else:
        first_next = date(year, month + 1, 1)
    last = first_next - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Gregorian Easter (Meeus/Jones/Butcher), used for NYSE Good Friday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _us_market_holidays(year: int) -> set[date]:
    """NYSE recurring full-day holidays for the given ET calendar year."""
    holidays = {
        _nth_weekday(year, 1, 0, 3),   # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),   # Washington's Birthday
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),     # Memorial Day
        _observed_fixed_holiday(date(year, 6, 19)),
        _observed_fixed_holiday(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),   # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed_fixed_holiday(date(year, 12, 25)),
    }
    # New Year's observation can fall on Dec 31 of the previous year.
    for new_year in (year, year + 1):
        observed = _observed_fixed_holiday(date(new_year, 1, 1))
        if observed.year == year:
            holidays.add(observed)
    return holidays


def _us_market_close_time(day: date) -> dt_time:
    """NYSE core-session close, including recurring 13:00 ET early closes."""
    thanksgiving = _nth_weekday(day.year, 11, 3, 4)
    day_after_thanksgiving = thanksgiving + timedelta(days=1)
    christmas_eve = date(day.year, 12, 24)
    july_fourth = date(day.year, 7, 4)
    if july_fourth.weekday() == 5:  # Saturday holiday observed Friday → Thursday close
        july_early_close = date(day.year, 7, 2)
    elif july_fourth.weekday() in (1, 2, 3, 4):
        july_early_close = date(day.year, 7, 3)
    else:
        july_early_close = None
    if day in {day_after_thanksgiving, christmas_eve, july_early_close}:
        if day.weekday() < 5 and day not in _us_market_holidays(day.year):
            return dt_time(13, 0)
    return dt_time(16, 0)


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


# 거래 세션 상수
KR_REGULAR = "KR_REGULAR"
US_PREMARKET = "US_PREMARKET"
US_REGULAR = "US_REGULAR"
US_AFTERMARKET = "US_AFTERMARKET"
CLOSED = "CLOSED"


def get_market_session(now: datetime | None = None) -> dict[str, str]:
    """현재 시장 세션 판별. 한국/미국 각각 반환.

    Returns:
        {"kr": KR_REGULAR|CLOSED, "us": US_PREMARKET|US_REGULAR|US_AFTERMARKET|CLOSED}
    """
    kst_now = _to_kst(now or datetime.now(KST))
    us_tz = _EDT if _is_us_dst(kst_now) else _EST
    et_now = kst_now.astimezone(us_tz)

    # 한국장: 09:00~15:30 KST, 평일
    kr = CLOSED
    if is_weekday(kst_now):
        t = kst_now.time()
        if dt_time(9, 0) <= t < dt_time(15, 30):
            kr = KR_REGULAR

    # 미국장: 평일 ET 기준
    us = CLOSED
    et_day = et_now.date()
    if is_weekday(et_now) and et_day not in _us_market_holidays(et_day.year):
        t = et_now.time()
        regular_close = _us_market_close_time(et_day)
        if dt_time(4, 0) <= t < dt_time(9, 30):
            us = US_PREMARKET
        elif dt_time(9, 30) <= t < regular_close:
            us = US_REGULAR
        elif regular_close == dt_time(16, 0) and dt_time(16, 0) <= t < dt_time(20, 0):
            us = US_AFTERMARKET

    return {"kr": kr, "us": us}


def is_us_tradeable(now: datetime | None = None) -> bool:
    """미국장 주문 가능 시간 (프리+정규+애프터)."""
    session = get_market_session(now)
    return session["us"] in (US_PREMARKET, US_REGULAR, US_AFTERMARKET)


def is_kr_market_open(now: datetime | None = None) -> bool:
    """한국장 개장 여부 (2026 KRX authoritative calendar)."""
    kst_now = _to_kst(now or datetime.now(KST))
    # 검증되지 않은 연도를 평일이라는 이유만으로 열지 않는다.
    if kst_now.year != 2026:
        return False
    if not is_weekday(kst_now) or kst_now.date() in _KRX_HOLIDAYS_2026:
        return False
    # 연초 개장식으로 첫 거래일은 10:00 개장, 종료는 15:30 유지.
    start = dt_time(10, 0) if kst_now.date() == date(2026, 1, 2) else dt_time(9, 0)
    return start <= kst_now.time() < dt_time(15, 30)


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

    et_day = et_now.date()
    if not is_weekday(et_now) or et_day in _us_market_holidays(et_day.year):
        return False

    t = et_now.time()
    return dt_time(9, 30) <= t < _us_market_close_time(et_day)


def is_any_market_open(now: datetime | None = None) -> bool:
    """한국장 또는 미국장 개장 여부 (정규장만)."""
    return is_kr_market_open(now) or is_us_market_open(now)


def is_any_market_tradeable(now: datetime | None = None) -> bool:
    """주문 가능한 시간인지 (한국 정규장 + 미국 프리/정규/애프터).

    모니터 run() 루프의 스캔 기준으로 사용.
    """
    session = get_market_session(now)
    kr_tradeable = session["kr"] == KR_REGULAR
    us_tradeable = session["us"] in (US_PREMARKET, US_REGULAR, US_AFTERMARKET)
    return kr_tradeable or us_tradeable


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


def next_tradeable_session(now: datetime | None = None) -> datetime:
    """다음 주문 가능 시간 (KST) 반환. 미국 프리마켓 포함."""
    kst_now = _to_kst(now or datetime.now(KST))

    for offset_min in range(0, 7 * 24 * 60, 5):
        candidate = kst_now + timedelta(minutes=offset_min)
        if is_any_market_tradeable(candidate):
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


# ─── 시세 신뢰도 레이어 (read-only) ────────────────
_KR_SESSION_LABEL = {
    KR_REGULAR: "한국장 장중",
}
_US_SESSION_LABEL = {
    US_PREMARKET: "미국장 프리장",
    US_REGULAR: "미국장 장중",
    US_AFTERMARKET: "미국장 애프터장",
}


def market_reliability_context(now: datetime | None = None) -> dict:
    """현재 시장 상태 기반 시세 신뢰도 컨텍스트. read-only 참고용."""
    kst_now = _to_kst(now or datetime.now(KST))
    session = get_market_session(kst_now)
    # 한국장 상태
    kr_sess = session["kr"]
    kr_is_open = kr_sess == KR_REGULAR
    if kr_is_open:
        kr_label = "한국장 장중"
        kr_note = "KIS 장중 시세"
    elif is_weekday(kst_now):
        t = kst_now.time()
        if dt_time(8, 30) <= t < dt_time(9, 0):
            kr_label = "한국장 장전"
            kr_note = "동시호가 전 · 시세 지연 가능"
        elif dt_time(15, 30) <= t < dt_time(18, 0):
            kr_label = "한국장 마감"
            kr_note = "시간외 · 마감 후 참고"
        else:
            kr_label = "한국장 마감"
            kr_note = "마감 후 참고"
    else:
        kr_label = "한국장 휴장"
        kr_note = "캐시 참고"

    # 미국장 상태
    us_sess = session["us"]
    us_is_open = us_sess == US_REGULAR
    if us_sess == US_REGULAR:
        us_label = "미국장 장중"
        us_note = "yfinance 장중 시세"
    elif us_sess == US_PREMARKET:
        us_label = "미국장 프리장"
        us_note = "시간외 · 시세 지연 가능"
    elif us_sess == US_AFTERMARKET:
        us_label = "미국장 애프터장"
        us_note = "시간외 · 시세 지연 가능"
    else:
        us_label = "미국장 마감"
        us_note = "마감 후 참고"

    # 통합 summary
    parts = []
    if kr_is_open:
        parts.append("한국장 장중 · KIS 시세 우선")
    else:
        parts.append(f"{kr_label} · {kr_note}")
    if us_is_open:
        parts.append("미국장 장중")
    else:
        parts.append(f"{us_label}")
    summary = " / ".join(parts)

    # trust label/tone
    if kr_is_open or us_is_open:
        trust_label = "장중 시세"
        trust_tone = "live"
    elif us_sess in (US_PREMARKET, US_AFTERMARKET) or (is_weekday(kst_now) and kr_label == "한국장 마감"):
        trust_label = "마감 후 참고"
        trust_tone = "stale"
    else:
        trust_label = "캐시 참고"
        trust_tone = "closed"

    return {
        "kr": {"session": kr_sess, "label": kr_label, "is_open": kr_is_open, "data_note": kr_note},
        "us": {"session": us_sess, "label": us_label, "is_open": us_is_open, "data_note": us_note},
        "summary": summary,
        "trust_label": trust_label,
        "trust_tone": trust_tone,
        "warning": "",
    }
