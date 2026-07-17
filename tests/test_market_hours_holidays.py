"""NYSE holiday/early-close execution gates.

Source: https://www.nyse.com/trade/hours-calendars (2026 table).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from core.market_hours import is_kr_market_open, is_us_market_open

KST = timezone(timedelta(hours=9))


def test_us_regular_positive_control_on_normal_weekday():
    # 2026-07-01 13:00 ET: normal full session.
    assert is_us_market_open(datetime(2026, 7, 2, 2, 0, tzinfo=KST)) is True


def test_us_holidays_are_closed_during_nominal_regular_hours():
    holidays = (
        datetime(2026, 4, 3, 22, 30, tzinfo=KST),   # Good Friday 09:30 ET
        datetime(2026, 7, 3, 22, 30, tzinfo=KST),   # Independence Day observed 09:30 ET
        datetime(2026, 11, 26, 23, 30, tzinfo=KST), # Thanksgiving 09:30 ET
        datetime(2026, 12, 25, 23, 30, tzinfo=KST), # Christmas 09:30 ET
    )
    for now in holidays:
        assert is_us_market_open(now) is False


def test_us_2026_early_close_boundaries():
    # Session before the observed Independence Day holiday: 13:00 ET close.
    assert is_us_market_open(datetime(2026, 7, 3, 1, 59, tzinfo=KST)) is True
    assert is_us_market_open(datetime(2026, 7, 3, 2, 0, tzinfo=KST)) is False
    # Day after Thanksgiving: 13:00 ET close.
    assert is_us_market_open(datetime(2026, 11, 28, 2, 59, tzinfo=KST)) is True
    assert is_us_market_open(datetime(2026, 11, 28, 3, 0, tzinfo=KST)) is False
    # Christmas Eve: 13:00 ET close.
    assert is_us_market_open(datetime(2026, 12, 25, 2, 59, tzinfo=KST)) is True
    assert is_us_market_open(datetime(2026, 12, 25, 3, 0, tzinfo=KST)) is False


@pytest.mark.parametrize("wrong_type", [1, "true"])
def test_sell_schedulers_reject_truthy_non_bool_market_authority(wrong_type):
    from core import toss_order_watch as order_watch
    from core import toss_position_review as position_review

    now = datetime(2026, 7, 2, 23, 0, tzinfo=KST)
    with patch("core.market_hours.is_us_market_open", return_value=wrong_type):
        assert order_watch._market_open_for_symbol("LRCX", now) is False
        assert position_review._market_open_for_symbol("LRCX", now) is False


def test_kr_2026_holidays_and_year_boundary_fail_closed():
    weekday_holidays = [
        (1, 1), (2, 16), (2, 17), (2, 18), (3, 2),
        (5, 1), (5, 5), (5, 25), (6, 3), (7, 17),
        (8, 17), (9, 24), (9, 25), (10, 5), (10, 9),
        (12, 25), (12, 31),
    ]
    for month, day in weekday_holidays:
        assert is_kr_market_open(
            datetime(2026, month, day, 11, 0, tzinfo=KST)
        ) is False

    assert is_kr_market_open(datetime(2026, 1, 2, 9, 59, tzinfo=KST)) is False
    assert is_kr_market_open(datetime(2026, 1, 2, 10, 0, tzinfo=KST)) is True
    assert is_kr_market_open(datetime(2026, 9, 28, 11, 0, tzinfo=KST)) is True
    assert is_kr_market_open(datetime(2027, 1, 4, 11, 0, tzinfo=KST)) is False
