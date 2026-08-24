"""검토 미리보기에서 원본 값을 숨기지 않는지 검증한다."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QHeaderView

from src.core.delivery_matrix import AnnualDeliveryBook, ProfileConflict
from src.widgets.review_tab import ReviewTab


class _Preferences:
    def get_output_dir(self):
        return None

    def set_output_dir(self, _path):
        return None


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_preview_removes_duplicate_columns_and_shows_full_original_values(
    qapp: QApplication,
):
    conflict = ProfileConflict(
        name="홍길동",
        birth_date="2015-03-02",
        phone="010-1111-1111",
        field="학교명",
        selected_value="미리 선택된 값은 표시하지 않음",
        values_by_row=((3, "분당초"), (14, "아주 긴 다른 학교 표기")),
    )
    book = AnnualDeliveryBook(
        year=2026,
        roster=(),
        delivery_rows=(),
        dates=(),
        profile_conflicts=(conflict,),
    )
    tab = ReviewTab(preferences=_Preferences())
    try:
        tab.set_annual_book(book)

        headers = [
            tab.review_table.horizontalHeaderItem(column).text()
            for column in range(tab.review_table.columnCount())
        ]
        original_values = "3행: 분당초\n14행: 아주 긴 다른 학교 표기"
        assert headers == ["성명", "생년월일", "핸드폰", "불일치 항목", "원본별 값"]
        assert "원본 행" not in headers
        assert "자동 선택값" not in headers
        assert tab.review_table.item(0, 4).text() == original_values
        assert tab.review_table.item(0, 4).toolTip() == original_values
        assert tab.review_table.textElideMode() is Qt.TextElideMode.ElideNone
        vertical_header = tab.review_table.verticalHeader()
        assert vertical_header.sectionResizeMode(0) is QHeaderView.ResizeMode.ResizeToContents
    finally:
        tab.close()
