"""core/memo.py 단위 테스트."""

from datetime import date

import pytest

from src.core.memo import _normalize_memo, get_active_memo


class TestNormalizeMemo:
    def test_fullwidth_to_halfwidth(self):
        result = _normalize_memo("일시중지　５／１～")
        assert "5/1" in result

    def test_typo_correction(self):
        result = _normalize_memo("일지중지 5/1")
        assert "일시중지" in result
        assert "일지중지" not in result

    def test_comment_removal(self):
        result = _normalize_memo("일시중지 5/1 // 주석\n배송 5/2 # 노트")
        assert "주석" not in result
        assert "노트" not in result

    def test_replacement_keywords(self):
        assert "배송" in _normalize_memo("추가 5/1")
        assert "원복" in _normalize_memo("재개 5/1")
        assert "중지" in _normalize_memo("제외 5/1")


class TestGetActiveMemo:
    def test_empty_input(self):
        assert get_active_memo("", date(2026, 6, 15)) == ""
        assert get_active_memo(None, date(2026, 6, 15)) == ""

    def test_skip_keywords(self):
        assert get_active_memo("이곳으로 배송 6/15", date(2026, 6, 15)) == ""
        assert get_active_memo("재확인 필요", date(2026, 6, 15)) == ""

    def test_single_date_suspend(self):
        result = get_active_memo("일시중지 6/15", date(2026, 6, 15))
        assert "일시중지 6/15" in result

    def test_single_date_not_matching(self):
        result = get_active_memo("일시중지 6/16", date(2026, 6, 15))
        assert result == ""

    def test_date_range(self):
        result = get_active_memo("일시중지 6/10~6/20", date(2026, 6, 15))
        assert "일시중지 6/10~6/20" in result

    def test_date_range_outside(self):
        result = get_active_memo("일시중지 6/10~6/14", date(2026, 6, 15))
        assert result == ""

    def test_ymd_format(self):
        result = get_active_memo("일시중지 20260615", date(2026, 6, 15))
        assert "일시중지 6/15" in result

    def test_in_out(self):
        result = get_active_memo("IN 6/15~", date(2026, 6, 15))
        assert "배송조정 IN - 6/15~" in result

    def test_delivery_action(self):
        result = get_active_memo("배송 6/15", date(2026, 6, 15))
        assert "배송 6/15~" in result

    def test_multiple_segments(self):
        result = get_active_memo("일시중지 6/15;배송 6/15", date(2026, 6, 15))
        assert "일시중지 6/15" in result
        assert "배송 6/15~" in result

    def test_segment_not_matching_day(self):
        result = get_active_memo("일시중지 6/16;배송 6/15", date(2026, 6, 15))
        assert "일시중지 6/16" not in result
        assert "배송 6/15~" in result

    def test_parentheses_in_segment(self):
        result = get_active_memo("(홍길동) 일시중지 6/15", date(2026, 6, 15))
        assert "일시중지 6/15" in result

    def test_suspend_no_suffix(self):
        result = get_active_memo("일시중지 6/15", date(2026, 6, 15))
        # 일시중지 has no trailing ~
        assert result == "일시중지 6/15"
