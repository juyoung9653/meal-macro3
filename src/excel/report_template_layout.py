"""User-selected body rows for the school-meal daily report template."""
from copy import copy
from dataclasses import asdict, dataclass

from openpyxl.cell.cell import MergedCell
from openpyxl.formula.translate import Translator


@dataclass(frozen=True)
class ReportTemplateLayout:
    body_start: int = 4
    body_end: int = 8
    copy_row: int = 6
    total_row: int = 9

    def __post_init__(self):
        if (any(type(value) is not int for value in asdict(self).values())
                or not 3 <= self.body_start <= self.copy_row <= self.body_end < self.total_row <= 1048576):
            raise ValueError("본문 시작·끝 행 안에서 복사 기준 행을 선택하고, 합계 행은 본문 뒤로 지정해주세요. 1~2행은 날짜·식종 제목입니다.")

    def manifest(self):
        return asdict(self)

    def validate(self, sheet):
        if self.total_row > sheet.max_row:
            raise ValueError("지정한 본문·합계 행이 현재 양식에 없습니다. 양식 확인/수정에서 본문·복사 행 설정을 다시 확인해주세요.")
        for area in sheet.merged_cells.ranges:
            if (area.max_row >= self.body_start and area.min_row <= self.body_end
                    or area.min_row <= self.total_row <= area.max_row):
                raise ValueError("선택한 본문 또는 합계 행에 병합 셀이 있습니다. Excel에서 해당 병합을 풀거나 본문·합계 행을 다시 선택해주세요.")


def resize_report_body(sheet, layout, count):
    """Insert copies after the chosen row, moving all following rows intact."""
    from .mapped_delivery_template import _shift_formula

    start, end = layout.body_start, layout.body_end
    capacity = end - start + 1
    delta = count - capacity
    insertion_row = layout.copy_row + 1 if delta > 0 else end + 1
    cells = {key: copy(cell) for key, cell in sheet._cells.items()
             if not isinstance(cell, MergedCell)}
    dimensions = {row: copy(dim) for row, dim in sheet.row_dimensions.items()}
    merges = [copy(area) for area in sheet.merged_cells.ranges]
    for area in merges:
        sheet.unmerge_cells(str(area))
    sheet._cells.clear()
    sheet.row_dimensions.clear()
    destinations = {row: [row if row < start else row + delta]
                    for row in {key[0] for key in cells} | set(dimensions) if row < start or row > end}
    for index in range(count):
        destination = start + index
        if delta > 0 and destination >= insertion_row:
            prototype = layout.copy_row if destination < insertion_row + delta else destination - delta
        else:
            prototype = destination
        destinations.setdefault(prototype, []).append(start + index)
    for (row, column), source in cells.items():
        for destination in destinations.get(row, ()):
            cell = sheet.cell(destination, column)
            value = source.value
            if source.data_type == "f":
                value = _shift_formula(value, insertion_row, delta, sheet.title)
                if delta > 0 and row == layout.copy_row and destination != row:
                    value = Translator(value, origin=source.coordinate).translate_formula(cell.coordinate)
            cell.value, cell._style, cell.data_type = value, copy(source._style), source.data_type
            cell.comment = copy(source.comment)
            if source.hyperlink:
                cell._hyperlink = copy(source.hyperlink)
                cell._hyperlink.ref = cell.coordinate
    for row, dimension in dimensions.items():
        for destination in destinations.get(row, ()):
            sheet.row_dimensions[destination] = copy(dimension)
            sheet.row_dimensions[destination].index = destination
            sheet.row_dimensions[destination].parent = sheet
    for area in merges:
        if area.min_row > end:
            area.shift(row_shift=delta)
        sheet.merge_cells(str(area))
    return layout.total_row + delta
