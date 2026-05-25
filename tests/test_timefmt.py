import pytest
from timefmt import fmt_ts


@pytest.mark.parametrize("seconds, expected", [
    (0,     "00:00"),
    (59,    "00:59"),
    (65,    "01:05"),
    (3599,  "59:59"),
    (3600,  "01:00:00"),
    (3661,  "01:01:01"),
    (86399, "23:59:59"),
])
def test_fmt_ts(seconds, expected):
    assert fmt_ts(seconds) == expected


def test_fmt_ts_float_truncates():
    assert fmt_ts(65.9) == "01:05"


def test_fmt_ts_boundary_just_below_hour():
    assert fmt_ts(3599.99) == "59:59"


def test_fmt_ts_boundary_exactly_one_hour():
    assert fmt_ts(3600.0) == "01:00:00"
