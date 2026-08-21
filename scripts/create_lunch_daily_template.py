"""조식·석식 일일배송 템플릿에서 중식 전용 빈 템플릿을 만든다."""

from __future__ import annotations

import argparse
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.packaging.core import DocumentProperties

LUNCH_MERGES = ("D3:E3", "F3:H3", "D4:E4", "F4:H4", "D5:E5", "F5:H5")


def create_lunch_template(source_path: Path, output_path: Path) -> None:
    workbook = load_workbook(source_path, keep_links=False)
    worksheet = workbook.worksheets[0]

    for extra_sheet in list(workbook.worksheets[1:]):
        workbook.remove(extra_sheet)

    worksheet.title = "중식일일배송"
    worksheet["D2"] = "중식"

    for coordinate in ("E3", "G3", "H3"):
        worksheet[coordinate] = None
    worksheet["D3"] = "중식"
    worksheet["F3"] = "중식합계"
    worksheet["J3"] = "중식갯수"

    for merged_range in LUNCH_MERGES:
        worksheet.merge_cells(merged_range)

    worksheet.print_area = "A1:I5"
    worksheet.freeze_panes = "C4"
    workbook.properties = DocumentProperties(
        title="중식 일일배송 템플릿",
        subject="날짜별 중식 배송표 생성용 빈 서식",
        creator="분당구 급식 정산 프로그램",
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    create_lunch_template(args.source, args.output)


if __name__ == "__main__":
    main()
