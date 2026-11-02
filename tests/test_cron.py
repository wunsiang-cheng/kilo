from datetime import datetime

import pytest

from kilo.scheduling.cron import Cron


def test_weekday_range_and_step():
    cron = Cron.parse("*/30 9-18 * * mon-fri")
    assert cron.next_after(datetime(2026, 10, 30, 18, 30)) == datetime(2026, 11, 2, 9, 0)  # Friday to Monday


def test_next_after_is_strict():
    assert Cron.parse("0 9 * * *").next_after(datetime(2026, 1, 1, 9, 0)) == datetime(2026, 1, 2, 9, 0)


def test_day_and_weekday_match_either():
    assert Cron.parse("0 0 13 * fri").next_after(datetime(2026, 11, 1)) == datetime(2026, 11, 6)


def test_seven_means_sunday():
    assert Cron.parse("0 0 * * 7").weekdays == frozenset({0})


def test_month_names():
    assert Cron.parse("0 0 1 jan,dec *").months == frozenset({1, 12})


def test_macro():
    assert Cron.parse("@daily").hours == frozenset({0})


def test_impossible_date_never_matches():
    assert Cron.parse("0 0 30 2 *").next_after(datetime(2026, 1, 1)) is None


@pytest.mark.parametrize("bad", ["* * * *", "60 * * * *", "*/0 * * * *", "5-1 * * * *", "x * * * *", "* 24 * * *"])
def test_invalid_expression(bad):
    with pytest.raises(ValueError):
        Cron.parse(bad)
