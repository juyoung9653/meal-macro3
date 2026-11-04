"""Household delivery rows and Excel retain all distinct output values."""
from dataclasses import replace
from io import BytesIO
from uuid import uuid4

import openpyxl
import pytest
from PySide6.QtWidgets import QApplication

from src.desktop_v2.current_sheet_export import build_current_sheet_workbook
from src.desktop_v2.driver_households import DriverHouseholdsDialog
from src.desktop_v2.driver_households_export import build_driver_households_template_snapshot
from src.domain.delivery_routing import RoutingState
from src.domain.delivery_substitute import SubstituteColumn, SubstituteOptions
from src.generators.substitute_generator import _build_sheet
from src.services.driver_households import build_driver_household_rows
from tests.test_current_households import fixture_roster, DAY
from tests.test_substitute_native_output import synthetic


def test_route_groups_different_addresses_details_and_notes_without_extra_columns():
    app = QApplication.instance() or QApplication([])
    roster = fixture_roster(shared=True)
    first, second, third = roster.persons
    roster = replace(roster, persons=(
        replace(first, profile=replace(first.profile, road_address='도로 A', detail_address=None, note='=첫째 메모')),
        replace(second, profile=replace(second.profile, road_address='도로 B', detail_address='201호', note='둘째 메모')),
        third,
    ), assignments=tuple(replace(a, driver='합성 기사') for a in roster.assignments))
    rows = build_driver_household_rows(roster, RoutingState(), DAY)
    combined = next(row for row in rows if first.person_id in row.member_ids)
    assert len(rows) == 2
    assert combined.member_ids == (first.person_id, second.person_id)
    assert combined.address == '도로 A\n도로 B'
    assert combined.sequences == ('1', '2')
    dialog = DriverHouseholdsDialog((combined,), service_day=DAY, drivers=())
    try:
        dialog.driver_combo.setCurrentIndex(next(i for i in range(dialog.driver_combo.count())
            if tuple(dialog.driver_combo.itemData(i)) == (None, '합성 기사')))
        assert dialog.table.rowCount() == 1
        current = dialog.current_view_snapshot()
        assert current.headers == ('가구원', '식종', '주소', '연락처', '중식 시작일', '중식 종료일')
    finally:
        dialog.close()
    template = build_driver_households_template_snapshot((combined,))
    for snapshot in (current, template):
        book = build_current_sheet_workbook(snapshot)
        buffer = BytesIO()
        book.save(buffer)
        book.close()
        buffer.seek(0)
        with_book = openpyxl.load_workbook(buffer)
        sheet = with_book.active
        columns = {cell.value: cell.column for cell in sheet[1]}
        assert sheet.max_row == 2
        assert sheet.cell(2, columns['주소']).value == combined.address
        assert '비고' not in columns and '상세주소' not in columns
        with_book.close()


def test_substitute_groups_nonadjacent_household_and_keeps_each_distinct_column():
    data = synthetic((1, 1, 1))
    first, outside, last = data.rows
    values = {**first.values, 'mobile_phone': '010-1111-1111', 'road_address': '도로 A',
              'detail_address': '', 'note': '=메모 A', 'sequence': '001'}
    first = replace(first, values=values)
    last = replace(last, household_id=first.household_id, values={**last.values,
        'mobile_phone': '010-2222-2222', 'road_address': '도로 B', 'detail_address': '201호',
        'note': '메모 B', 'sequence': '003', 'jurisdiction': first.values['jurisdiction']})
    outside = replace(outside, values={**outside.values, 'mobile_phone': first.values['mobile_phone']})
    data = replace(data, rows=(first, outside, last))
    options = SubstituteOptions('synthetic', data.default_columns)
    assert data.selected_rows(options) == (first, last, outside)
    book, _, layout = _build_sheet(data, options)
    sheet = book.active
    columns = {key: i + 1 for i, key in enumerate(options.columns)}
    assert [(g.source_start, g.source_end) for g in layout.groups] == [(0, 1), (2, 2)]
    assert sheet.cell(3, columns['sequence']).value == '001\n003'
    assert sheet.cell(3, columns['detail_address']).value == '201호'
    assert sheet.cell(3, columns['road_address']).value == '도로 A\n도로 B'
    assert sheet.cell(3, columns['mobile_phone']).value == '010-1111-1111\n010-2222-2222'
    assert sheet.cell(3, columns['note']).value == '=메모 A\n메모 B'
    assert sheet.cell(3, columns['note']).data_type == 's'
    assert sheet.cell(3, columns['jurisdiction']).value == first.values['jurisdiction']
    assert sheet.cell(4, columns['name']).value == last.values['name']
    assert sheet.cell(5, columns['name']).value == outside.values['name']
    assert sum(group.meal_count for group in layout.groups) == 3
    book.close()


@pytest.mark.excel
@pytest.mark.slow
def test_generated_delivery_list_keeps_combined_values_through_excel_and_encryption(tmp_path):
    import msoffcrypto
    from src.generators.substitute_generator import make_substitute_publication
    data = synthetic((2,))
    custom = f'custom:{uuid4()}'
    data = replace(data, columns=(*data.columns, SubstituteColumn(custom, '추가 안내')),
        default_columns=(*data.default_columns, custom), rows=tuple(
            replace(row, values={**row.values, 'detail_address': '' if i == 0 else '201호',
                'note': '첫째 안내' if i == 0 else '둘째 안내', custom: '=문자A' if i == 0 else '=문자B'})
            for i, row in enumerate(data.rows)))
    options = SubstituteOptions('synthetic', data.default_columns, orientation='landscape')
    path = tmp_path / 'combined.xlsx'
    publication = make_substitute_publication(data, options, path, output_password='synthetic-password',
        template_path='assets/급식/배송목록템플릿.xlsx', overwrite=False)
    assert publication.password_protected
    plain = BytesIO()
    with path.open('rb') as stream:
        office = msoffcrypto.OfficeFile(stream)
        office.load_key(password='synthetic-password')
        office.decrypt(plain)
    plain.seek(0)
    workbook = openpyxl.load_workbook(plain)
    sheet = workbook.active
    assert sheet['D3'].value == '201호'
    assert sheet['G3'].value == '첫째 안내\n둘째 안내'
    assert sheet['H3'].value == '=문자A\n=문자B'
    assert sheet['H3'].data_type == 's'
    assert 'H3:H4' in {str(r) for r in sheet.merged_cells.ranges}
    assert sheet.row_dimensions[3].height + sheet.row_dimensions[4].height > 0
    workbook.close()
