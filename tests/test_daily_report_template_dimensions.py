"""All daily reports honor template sizing/styles even when content is clipped."""
from copy import copy
from datetime import date

import pytest
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from src.generators.report_generator import make_report_publication
from src.generators.happy_delivery_report import build_happy_report_workbook
from src.services.delivery_service import DeliveryReport, DeliveryReportRow, DeliveryReportTotals
from src.services.happy_delivery_report import prepare_happy_report_data
from src.domain.roster import Meal
from tests.test_sqlite_delivery_report_service import _snapshot
from tests.test_happy_delivery_report import paths


def _customize(sheet):
    sheet.column_dimensions['H'].width = 3.25
    sheet.column_dimensions.group('B', 'C', hidden=False)
    sheet.column_dimensions['B'].width = 7.75
    sheet.sheet_format.defaultRowHeight = 22
    sheet.sheet_properties.pageSetUpPr.fitToPage = False
    sheet.page_setup.orientation = 'landscape'
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A5
    sheet.page_setup.scale = 135
    sheet.page_setup.fitToWidth = 2
    sheet.page_margins.left = 1.1
    sheet.page_margins.bottom = .3
    sheet.print_title_rows = '1:3'
    sheet.oddFooter.center.text = '합성 양식 &P'
    for row in range(4, 10):
        sheet.row_dimensions[row].height = 7 + row
        for column in range(1, 11):
            cell = sheet.cell(row, column)
            cell.font = Font(name='Arial', size=24, color='123456', italic=True)
            cell.alignment = Alignment(horizontal='right', vertical='top', wrap_text=False, shrink_to_fit=False)
            cell.fill = PatternFill('solid', fgColor='ABCDEF')
            cell.border = Border(bottom=Side(style='double', color='654321'))


def _assert_print_settings(sheet):
    assert sheet.page_setup.orientation == 'landscape'
    assert sheet.page_setup.paperSize == int(sheet.PAPERSIZE_A5)
    assert sheet.page_setup.scale == 135
    assert sheet.page_setup.fitToWidth == 2
    assert sheet.sheet_properties.pageSetUpPr.fitToPage is False
    assert sheet.page_margins.left == 1.1 and sheet.page_margins.bottom == .3
    assert sheet.print_title_rows == '$1:$3'
    assert sheet.oddFooter.center.text == '합성 양식 &P'
    assert sheet.column_dimensions['H'].width == 3.25
    assert sheet.column_dimensions['B'].width == 7.75
    assert sheet.column_dimensions['B'].min == 2 and sheet.column_dimensions['B'].max == 3


@pytest.mark.parametrize('count', [0, 2, 7])
def test_meal_report_copies_exact_row_heights_and_repeated_eighth_row(tmp_path, count):
    template = tmp_path / 'meal.xlsx'
    source = load_workbook('assets/급식/일일식수템플릿.xlsx')
    sheet = source['aaa']
    _customize(sheet)
    sheet['A9'] = '사용자 합계'
    source.save(template)
    source.close()
    memo = '=긴 메모 ' * 70 + '\n두 번째 줄'
    rows = tuple(DeliveryReportRow('=기사'+str(i), ('1', '0', '0', '0', '0'), 1, memo, '') for i in range(count))
    report = DeliveryReport(date(2026, 8, 20), '중식', rows, DeliveryReportTotals((count, 0, 0, 0, 0), count))
    output = tmp_path / 'result.xlsx'
    make_report_publication((report,), output, template)
    workbook = load_workbook(output)
    try:
        sheet = workbook.active
        _assert_print_settings(sheet)
        for row in range(4, 4+count):
            assert sheet.row_dimensions[row].height == 7 + min(row, 8)
            cell = sheet.cell(row, 8)
            assert cell.value == memo and cell.data_type == 's'
            assert cell.font.name == 'Arial' and cell.font.sz == 24
            assert not cell.alignment.wrap_text and not cell.alignment.shrink_to_fit
            assert cell.border.bottom.style == 'double'
            assert cell.fill.fgColor.rgb == '00ABCDEF'
        assert sheet.row_dimensions[4+count].height == 16
        assert sheet.cell(4+count, 1).value == '사용자 합계'
        assert sheet.cell(4+count, 7).value == count
    finally:
        workbook.close()


def test_both_happy_report_templates_copy_exact_body_and_total_styles(tmp_path):
    templates = {}
    for variant, path in paths().items():
        source = load_workbook(path)
        sheet = source.worksheets[0]
        _customize(sheet)
        # The happy layout has only one prototype body row and one total row.
        sheet.row_dimensions[4].height = 8
        sheet.row_dimensions[5].height = 19
        templates[variant] = tmp_path / (variant+'.xlsx')
        source.save(templates[variant])
        source.close()
    snapshot, _ = _snapshot(people=2, meals=tuple(Meal))
    data = prepare_happy_report_data(snapshot, date(2026, 8, 20), date(2026, 8, 20), tuple(Meal))
    workbook = build_happy_report_workbook(data.reports, templates)
    output = tmp_path / 'happy-result.xlsx'
    workbook.save(output)
    workbook.close()
    workbook = load_workbook(output)
    try:
        assert len(workbook.worksheets) == 2
        for sheet in workbook:
            _assert_print_settings(sheet)
            assert sheet.row_dimensions[4].height == sheet.row_dimensions[5].height == 8
            assert sheet.row_dimensions[6].height == 19
            assert sheet['I4'].font.sz == 24
            assert not sheet['I4'].alignment.wrap_text
            assert sheet['I4'].border.bottom.style == 'double'
            assert sheet['D4'].value == 1
    finally:
        workbook.close()
