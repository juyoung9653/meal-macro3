"""core/diff_format.py 단위 테스트."""

import pytest

from src.core.diff_format import fmt_cell, has_diff, strip_diff


class TestFmtCell:
    def test_equal_values(self):
        assert fmt_cell(5, 5) == "5"
        assert fmt_cell(0, 0) == ""

    def test_increase(self):
        assert fmt_cell(5, 3) == "5(+2)"

    def test_decrease(self):
        assert fmt_cell(3, 5) == "3(-2)"

    def test_zero_current(self):
        assert fmt_cell(0, 3) == "0(-3)"

    def test_zero_prev(self):
        assert fmt_cell(3, 0) == "3(+3)"


class TestHasDiff:
    def test_has_diff_true(self):
        assert has_diff("3(+1)") is True
        assert has_diff("5(-2)") is True
        assert has_diff("0(+3)") is True

    def test_has_diff_false(self):
        assert has_diff("3") is False
        assert has_diff("") is False
        assert has_diff(None) is False
        assert has_diff(5) is False

    def test_has_diff_edge(self):
        assert has_diff("10(+0)") is True


class TestStripDiff:
    def test_strip_diff_value(self):
        assert strip_diff("3(+1)") == "3"
        assert strip_diff("5(-2)") == "5"
        assert strip_diff("10(+0)") == "10"

    def test_strip_diff_plain(self):
        assert strip_diff("3") == "3"
        assert strip_diff("") == ""
        assert strip_diff(None) == ""

    def test_strip_diff_int(self):
        assert strip_diff(3) == "3"
