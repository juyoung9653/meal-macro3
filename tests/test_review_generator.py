"""독립 검토용 통합 문서 생성 회귀 테스트."""

from __future__ import annotations

from pathlib import Path

import openpyxl

from src.core.delivery_matrix import ProfileConflict
from src.generators.review_generator import (
    REVIEW_HEADERS,
    REVIEW_SHEET_TITLE,
    make_profile_review,
)


def test_makes_review_only_workbook_from_profile_conflicts(tmp_path: Path):
    output = tmp_path / "DB검토_2026.xlsx"
    conflict = ProfileConflict(
        name="홍길동",
        birth_date="2015-03-02",
        phone="010-1111-1111",
        field="학교명",
        selected_value="분당초",
        values_by_row=((3, "분당초"), (4, "다른 학교 표기")),
    )

    count = make_profile_review((conflict,), output)

    assert count == 1
    workbook = openpyxl.load_workbook(output)
    try:
        assert workbook.sheetnames == [REVIEW_SHEET_TITLE]
        worksheet = workbook[REVIEW_SHEET_TITLE]
        assert tuple(cell.value for cell in worksheet[1]) == REVIEW_HEADERS
        assert worksheet["A2"].value == "홍길동"
        assert worksheet["D2"].value == "학교명"
        assert worksheet["E2"].value == "3행: 분당초\n4행: 다른 학교 표기"
        assert worksheet["E2"].alignment.wrap_text is True
        assert "원본 행" not in REVIEW_HEADERS
        assert "자동 선택값" not in REVIEW_HEADERS
    finally:
        workbook.close()


def test_makes_header_only_review_when_there_are_no_conflicts(tmp_path: Path):
    output = tmp_path / "DB검토_2026.xlsx"

    assert make_profile_review((), output) == 0

    workbook = openpyxl.load_workbook(output)
    try:
        worksheet = workbook[REVIEW_SHEET_TITLE]
        assert worksheet.max_row == 1
        assert tuple(cell.value for cell in worksheet[1]) == REVIEW_HEADERS
    finally:
        workbook.close()
