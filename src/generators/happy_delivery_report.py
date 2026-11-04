"""Happy lunchbox daily reports: layout ported from main/daily_delivery_generator.

Only rendering is reused. Delivery eligibility and quantities come from SQLite.
"""
from collections import Counter
from copy import copy

from openpyxl import Workbook, load_workbook

from src.services.happy_delivery_report import DailyDeliveryRow, DailyDeliverySheet
from src.excel.delivery_templates import validate_delivery_template
from src.excel.publication import publish_workbook

HAPPY_REPORT_RENDER_VERSION = 4


def _literal(cell, value):
    cell.value = value
    cell.data_type = "s"


def _format_distribution(keys):
    return "/".join(f"{size}*{count}" for size, count in
                    sorted(Counter(Counter(keys).values()).items(), reverse=True))


def _driver_ranges(rows: list[DailyDeliveryRow]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(rows) + 1):
        if index == len(rows) or rows[index].route.driver != rows[start].route.driver:
            ranges.append((start, index - 1))
            start = index
    return ranges


def _copy_template_sheet(source_ws, target_wb: Workbook, title: str):
    from src.excel.template_sheet import copy_template_sheet
    target_ws = target_wb.create_sheet(title)
    copy_template_sheet(source_ws, target_ws)
    return target_ws


def _fill_daily_sheet(ws, sheet: DailyDeliverySheet, *, lunch_only: bool = False):
    """템플릿 서식을 복제해 학기중 또는 중식 전용 시트를 채운다."""

    rows = [
        row
        for row in sheet.rows
        if (row.lunch if lunch_only else row.breakfast or row.dinner)
    ]
    rows.sort(key=lambda row: (row.lunch_order if lunch_only else row.dinner_order, row.route.driver))
    data_start = 4
    data_end = data_start + len(rows) - 1
    total_row = data_end + 1

    if ws.max_row < 5:
        raise ValueError("일일배송 템플릿에는 헤더, 본문, 합계 서식이 필요합니다.")

    data_styles = [copy(ws.cell(data_start, column)._style) for column in range(1, 11)]
    total_styles = [copy(ws.cell(5, column)._style) for column in range(1, 11)]
    data_dimension = copy(ws.row_dimensions[data_start])
    total_dimension = copy(ws.row_dimensions[5])
    district_label = ws.cell(data_start, 1).value
    total_label = ws.cell(5, 1).value

    for merged in list(ws.merged_cells.ranges):
        if merged.min_row >= data_start:
            ws.unmerge_cells(str(merged))
    if ws.max_row >= 5:
        ws.delete_rows(5, ws.max_row - 4)
    for row_index in list(ws.row_dimensions):
        if row_index >= 5:
            del ws.row_dimensions[row_index]

    for row_no in range(data_start, data_end + 1):
        ws.row_dimensions[row_no] = copy(data_dimension)
        ws.row_dimensions[row_no].index = row_no
        for column, style in enumerate(data_styles, start=1):
            cell = ws.cell(row_no, column)
            cell._style = copy(style)
            cell.value = None

    ws.row_dimensions[total_row] = copy(total_dimension)
    ws.row_dimensions[total_row].index = total_row
    for column, style in enumerate(total_styles, start=1):
        cell = ws.cell(total_row, column)
        cell._style = copy(style)
        cell.value = None

    if rows:
        ws.cell(data_start, 1, district_label)
    ws.cell(total_row, 1, total_label)
    ws["A1"] = sheet.delivery_date

    for row_no, row_data in enumerate(rows, start=data_start):
        _literal(ws.cell(row_no, 3), row_data.route.region)
        if lunch_only:
            ws.cell(row_no, 4, row_data.lunch)
            ws.merge_cells(
                start_row=row_no,
                start_column=4,
                end_row=row_no,
                end_column=5,
            )
        else:
            ws.cell(row_no, 4, row_data.breakfast or None)
            ws.cell(row_no, 5, row_data.dinner or None)
            ws.cell(row_no, 7, row_data.dinner_packages)
        note = row_data.lunch_note if lunch_only else row_data.dinner_note
        _literal(ws.cell(row_no, 9), row_data.note if note is None else note)

    # 구청 및 기사별 병합/합계

    for start_idx, end_idx in _driver_ranges(rows):
        first_row = data_start + start_idx
        last_row = data_start + end_idx
        driver_rows = rows[start_idx : end_idx + 1]
        _literal(ws.cell(first_row, 2), driver_rows[0].route.driver)
        if lunch_only:
            ws.cell(first_row, 6, f"=SUM(D{first_row}:D{last_row})")
            driver_households = [
                key for row in driver_rows for key in row._lunch_household_keys
            ]
        else:
            if any(row.breakfast for row in driver_rows):
                ws.cell(first_row, 6, f"=SUM(D{first_row}:D{last_row})")
            if any(row.dinner for row in driver_rows):
                ws.cell(first_row, 8, f"=SUM(E{first_row}:E{last_row})")
            driver_households = [
                key for row in driver_rows for key in row._dinner_household_keys
            ]
        ws.cell(first_row, 10, _format_distribution(driver_households).replace("/", "\n"))

        if lunch_only:
            ws.merge_cells(
                start_row=first_row,
                start_column=6,
                end_row=last_row,
                end_column=8,
            )
            if first_row != last_row:
                for col in (2, 10):
                    ws.merge_cells(
                        start_row=first_row,
                        start_column=col,
                        end_row=last_row,
                        end_column=col,
                    )
        elif first_row != last_row:
            for col in (2, 6, 8, 10):
                ws.merge_cells(
                    start_row=first_row,
                    start_column=col,
                    end_row=last_row,
                    end_column=col,
                )

    if data_end >= data_start:
        ws.merge_cells(
            start_row=data_start, start_column=1, end_row=data_end, end_column=1
        )

    # 합계 행
    if rows:
        if lunch_only:
            ws.cell(total_row, 4, f"=SUM(D{data_start}:D{data_end})")
            ws.cell(total_row, 6, f"=SUM(F{data_start}:F{data_end})")
        else:
            for total_column, source_column, field in ((4, "D", "breakfast"), (5, "E", "dinner"),
                                                       (6, "F", "breakfast"), (8, "H", "dinner")):
                if any(getattr(row, field) for row in rows):
                    ws.cell(total_row, total_column, f"=SUM({source_column}{data_start}:{source_column}{data_end})")
    ws.merge_cells(start_row=total_row, start_column=1, end_row=total_row, end_column=3)
    if lunch_only:
        ws.merge_cells(
            start_row=total_row,
            start_column=4,
            end_row=total_row,
            end_column=5,
        )
        ws.merge_cells(
            start_row=total_row,
            start_column=6,
            end_row=total_row,
            end_column=8,
        )
    ws.print_area = f"A1:I{total_row}"
    ws.auto_filter.ref = None



def build_happy_report_workbook(reports, template_paths):
    if not reports:
        raise ValueError("선택한 기간에 실제 배송할 대상이 없습니다.")
    workbook = Workbook()
    workbook.remove(workbook.active)
    templates = {}
    try:
        for report in sorted(reports, key=lambda item: item.delivery_date, reverse=True):
            for variant, count in (("dinner", report.breakfast_total + report.dinner_total),
                                   ("lunch", report.lunch_total)):
                show_empty = report.show_lunch if variant == "lunch" else report.show_term
                if not count and not show_empty:
                    continue
                if variant not in templates:
                    path = template_paths[variant]
                    validate_delivery_template(path, "happy_report_" + variant)
                    templates[variant] = load_workbook(path)
                title = report.delivery_date.strftime("%y.%m.%d")
                if variant == "lunch":
                    title = "중식_" + title
                worksheet = _copy_template_sheet(templates[variant].worksheets[0], workbook, title)
                _fill_daily_sheet(worksheet, report, lunch_only=variant == "lunch")
        if not workbook.worksheets:
            raise ValueError("선택한 기간에 실제 배송할 대상이 없습니다.")
        return workbook
    except BaseException:
        workbook.close()
        raise
    finally:
        for template in templates.values():
            template.close()


def make_happy_report_publication(reports, output_path, template_paths, *,
                                  output_password=None, overwrite=False):
    workbook = build_happy_report_workbook(reports, template_paths)
    try:
        return publish_workbook(workbook, output_path, password=output_password, overwrite=overwrite)
    finally:
        workbook.close()
