"""명단 인적사항 불일치를 독립된 검토용 통합 문서로 출력한다."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

from ..core.delivery_matrix import ProfileConflict
from ..excel.excel_utils import CENTER_ALIGN
from ..excel.publication import publish_workbook

REVIEW_SHEET_TITLE = "DB검토"
REVIEW_HEADERS = (
    "성명",
    "생년월일",
    "핸드폰",
    "불일치 항목",
    "원본별 값",
)


def make_profile_review(
    conflicts: Sequence[ProfileConflict],
    output_path: str | Path,
    *,
    output_password: str | None = None,
) -> int:
    """불일치 내역만 든 검토용 파일을 만들고 기록 건수를 반환한다."""
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    if worksheet is None:  # pragma: no cover - openpyxl 기본 계약 방어
        raise RuntimeError("검토용 워크시트를 만들 수 없습니다.")
    worksheet.title = REVIEW_SHEET_TITLE
    worksheet.append(REVIEW_HEADERS)

    header_fill = PatternFill(fill_type="solid", fgColor="1F4E78")
    for cell in worksheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = CENTER_ALIGN

    for conflict in conflicts:
        worksheet.append(
            (
                conflict.name,
                conflict.birth_date,
                conflict.phone,
                conflict.field,
                "\n".join(
                    f"{row}행: {value}" for row, value in conflict.values_by_row
                ),
            )
        )

    for row in worksheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = f"A1:E{worksheet.max_row}"
    for column, width in {
        "A": 14,
        "B": 14,
        "C": 14,
        "D": 18,
        "E": 54,
    }.items():
        worksheet.column_dimensions[column].width = width

    publish_workbook(workbook, output_path, password=output_password)
    return len(conflicts)
