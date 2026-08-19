"""기존 일일배송 시트에서 개인정보 없는 동적 생성용 템플릿을 추출한다."""

from __future__ import annotations

import argparse
from copy import copy
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.packaging.core import DocumentProperties

HEADER_MERGES = {"A1:I1", "A2:A3", "B2:B3", "C2:C3", "D2:H2", "I2:I3"}
DATA_ROW = 4
TEMPLATE_TOTAL_ROW = 5


def _copy_row(source, source_row: int, target_row: int, max_column: int = 10) -> None:
    for column in range(1, max_column + 1):
        source_cell = source.cell(source_row, column)
        target_cell = source.cell(target_row, column)
        target_cell._style = copy(source_cell._style)
        target_cell.number_format = source_cell.number_format
        target_cell.protection = copy(source_cell.protection)
    source.row_dimensions[target_row].height = source.row_dimensions[source_row].height


def _find_total_row(source) -> int:
    for row in range(DATA_ROW + 1, source.max_row + 1):
        value = "".join(str(source.cell(row, 1).value or "").split())
        if value == "합계":
            return row
    raise ValueError("선택한 시트에서 합계 행을 찾을 수 없습니다.")


def extract_template(source_path: Path, output_path: Path, sheet_name: str | None) -> None:
    workbook = load_workbook(source_path, keep_links=False)
    source = workbook[sheet_name] if sheet_name else workbook.worksheets[0]

    for worksheet in list(workbook.worksheets):
        if worksheet is not source:
            workbook.remove(worksheet)
    source.title = "일일배송"

    for merged in list(source.merged_cells.ranges):
        if str(merged) not in HEADER_MERGES:
            source.unmerge_cells(str(merged))

    source_total_row = _find_total_row(source)
    _copy_row(source, source_total_row, TEMPLATE_TOTAL_ROW)
    source.cell(TEMPLATE_TOTAL_ROW, 1, source.cell(source_total_row, 1).value or "합계")
    for column in range(2, 11):
        source.cell(TEMPLATE_TOTAL_ROW, column).value = None

    district_label = source.cell(DATA_ROW, 1).value
    for column in range(1, 11):
        source.cell(DATA_ROW, column).value = None
    source.cell(DATA_ROW, 1, district_label)

    if source.max_row > TEMPLATE_TOTAL_ROW:
        source.delete_rows(TEMPLATE_TOTAL_ROW + 1, source.max_row - TEMPLATE_TOTAL_ROW)
    for row_index in list(source.row_dimensions):
        if row_index > TEMPLATE_TOTAL_ROW:
            del source.row_dimensions[row_index]

    source.merge_cells(
        start_row=TEMPLATE_TOTAL_ROW,
        start_column=1,
        end_row=TEMPLATE_TOTAL_ROW,
        end_column=3,
    )
    source.print_area = f"A1:I{TEMPLATE_TOTAL_ROW}"
    source.freeze_panes = "C4"

    for row in source.iter_rows():
        for cell in row:
            if isinstance(cell, MergedCell):
                continue
            cell.comment = None
            cell.hyperlink = None
    source._images = []
    source._charts = []
    workbook._external_links = []
    workbook.properties = DocumentProperties(
        title="일일배송 템플릿",
        subject="날짜별 배송표 생성용 빈 서식",
        creator="분당구 급식 정산 프로그램",
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--sheet", default=None)
    args = parser.parse_args()
    extract_template(args.source, args.output, args.sheet)


if __name__ == "__main__":
    main()
