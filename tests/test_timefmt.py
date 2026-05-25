import unittest
from timefmt import fmt_ts


class FmtTsTests(unittest.TestCase):
    def test_fmt_ts_parametrized(self):
        cases = [
            (0,     "00:00"),
            (59,    "00:59"),
            (65,    "01:05"),
            (3599,  "59:59"),
            (3600,  "01:00:00"),
            (3661,  "01:01:01"),
            (86399, "23:59:59"),
        ]
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(fmt_ts(seconds), expected)

    def test_fmt_ts_float_truncates(self):
        self.assertEqual(fmt_ts(65.9), "01:05")

    def test_fmt_ts_boundary_just_below_hour(self):
        self.assertEqual(fmt_ts(3599.99), "59:59")

    def test_fmt_ts_boundary_exactly_one_hour(self):
        self.assertEqual(fmt_ts(3600.0), "01:00:00")


if __name__ == '__main__':
    unittest.main()
