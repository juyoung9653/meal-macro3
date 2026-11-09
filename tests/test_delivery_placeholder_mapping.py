"""Synthetic placeholder forms, copied rows, and delivery recipient flags."""
from copy import copy
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pytest
from openpyxl.styles import Alignment, Font
from PySide6.QtCore import QSettings

from src.domain.delivery_substitute import SubstituteColumn, SubstituteOptions
from src.domain.projections import roster_entity_states
from src.domain.revisions import HistoricalSnapshot
from src.domain.roster import Meal, MealEnrollment, DeliveryAssignment
from src.excel.delivery_templates import validate_delivery_template
from src.excel.mapped_delivery_template import inspect_delivery_form
from src.excel.substitute_template_columns import sync_substitute_template
from src.generators.substitute_generator import build_substitute_workbook
from src.services.sqlite_delivery_substitute_service import prepare_delivery_substitute_data, substitute_columns_from_roster
from tests.test_delivery_substitute_v2 import source, DAY, options, app, session

TEMPLATE = Path('assets/행복도시락/배송목록템플릿.xlsx')


def test_new_template_copies_values_dates_flags_and_all_cell_styles(tmp_path):
    snapshot, _ = source(sizes=(1,) * 20)
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST)
    before = TEMPLATE.read_bytes()
    template = openpyxl.load_workbook(TEMPLATE)
    output = build_substitute_workbook(data, options(data), template_path=TEMPLATE)
    ws, original = output.active, template.active
    assert ws.title == original.title
    assert ws['B1'].number_format == original['B1'].number_format
    for col in range(1, 9):
        for row in range(3, 23):
            assert ws.cell(row, col)._style == original.cell(3, col)._style
            assert ws.row_dimensions[row].height == original.row_dimensions[3].height
    assert [ws.cell(row, 2).value for row in range(3, 23)] == [row.values['name'] for row in data.rows]
    assert all(ws.cell(row, 7).value == 1 for row in range(3, 23))
    assert ws['G2'].value == '=SUBTOTAL(3,G3:G70)'
    assert str(ws.column_dimensions) == str(original.column_dimensions)
    assert ws.page_setup == original.page_setup
    assert ws.page_margins == original.page_margins
    path = tmp_path / 'output.xlsx'
    output.save(path)
    output.close()
    template.close()
    reopened = openpyxl.load_workbook(path)
    assert reopened.active['B1'].value.date() == DAY
    reopened.close()
    assert TEMPLATE.read_bytes() == before


def test_arbitrary_names_most_tokens_tie_and_multiline_copy(tmp_path):
    path = tmp_path / 'free.xlsx'
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '자유 이름'
    ws['D1'] = '{날짜}'
    ws['D1'].number_format = 'yyyy"년" m"월" d"일"'
    ws['A4'], ws['B4'], ws['C4'] = '{누구}', '{주소}', '{조식}'
    ws['A5'] = '{누구} / {누구} / {누구}'  # Same number: earlier row wins.
    ws['B5'] = '=C4'
    ws.merge_cells('C4:D5')
    ws['A4'].font = Font(name='Arial', size=19, bold=True)
    ws['A4'].alignment = Alignment(horizontal='right')
    ws.row_dimensions[4].height = 33
    ws.row_dimensions[5].height = 27
    ws['A6'], ws['C6'] = '합계', '=SUM(C4:C5)'
    ws.print_area = 'A1:D6'
    wb.save(path)
    wb.close()
    form = inspect_delivery_form(path)
    assert form.suggested_row == 4
    snapshot, _ = source(sizes=(1, 1, 1))
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST)
    chosen = options(data, template_mapping={'누구': 'name'}, template_rows=(4, 5))
    result = build_substitute_workbook(data, chosen, template_path=path)
    ws = result.active
    assert ws['A4'].value == data.rows[0].values['name']
    assert ws['A6'].value == data.rows[1].values['name']
    assert ws['A8'].value == data.rows[2].values['name']
    assert ws['B9'].value == '=C8'
    assert ws['A10'].value == '합계'
    assert ws['C10'].value == '=SUM(C4:C9)'
    assert {str(item) for item in ws.merged_cells.ranges} == {'C4:D5','C6:D7','C8:D9'}
    assert ws['A8'].font.size == 19 and ws['A8'].alignment.horizontal == 'right'
    assert ws.row_dimensions[9].height == 27
    result.close()
    with pytest.raises(ValueError, match='병합'):
        build_substitute_workbook(data, replace(chosen, template_rows=(4, 4)), template_path=path)


def test_sync_does_not_rewrite_placeholder_form():
    before = TEMPLATE.read_bytes()
    validate_delivery_template(TEMPLATE, 'delivery_substitute')
    assert not sync_substitute_template(TEMPLATE, (), ())
    assert TEMPLATE.read_bytes() == before


def test_current_roster_lunch_date_columns_are_supported_after_status_change():
    snapshot, roster = source(sizes=(1,))
    person = replace(roster.persons[0], lunch_period=roster.enrollments[0].period)
    roster = replace(roster, persons=(person,))
    snapshot = HistoricalSnapshot(snapshot.revision_id, roster_entity_states(roster))
    columns = substitute_columns_from_roster([
        SimpleNamespace(key='name', label='성명'),
        SimpleNamespace(key='lunch_starts_on', label='중식 시작일'),
        SimpleNamespace(key='lunch_ends_on', label='중식 종료일'),
    ], source_snapshot=snapshot)
    prepared = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST, columns=columns, fallbacks={})
    assert prepared.rows[0].values['lunch_starts_on'] == '2026-08-01'
    assert prepared.rows[0].values['lunch_ends_on'] == ''


def test_school_list_ignores_combined_meals_even_with_breakfast_in_same_week():
    snapshot, roster = source(sizes=(1, 1, 1), meal=Meal.DINNER)
    both, breakfast_only, dinner_only = roster.persons
    enrollments = tuple(e for e in roster.enrollments if e.person_id != breakfast_only.person_id)
    for person in (both, breakfast_only):
        enrollments += (MealEnrollment(person.person_id, Meal.BREAKFAST, roster.enrollments[0].period),)
    assignments = roster.assignments + tuple(DeliveryAssignment(p.person_id, Meal.BREAKFAST, '합성기사', '001')
                                             for p in (both, breakfast_only))
    roster = replace(roster, enrollments=enrollments, assignments=assignments)
    snapshot = HistoricalSnapshot(snapshot.revision_id, roster_entity_states(roster))
    data = prepare_delivery_substitute_data(snapshot, date(2026, 8, 27), Meal.DINNER, include_breakfast=True)
    values = {row.person_id: row.values for row in data.rows}
    assert set(values) == {both.person_id, dinner_only.person_id}
    assert values[both.person_id]['meal_receives'] == '1'
    assert all(value['receives:breakfast'] == '' for value in values.values())
    assert data.calculation_manifest['included_meals'] == (Meal.DINNER.value,)
    assert data.calculation_manifest['breakfast_reference_day'] is None
    assert data == prepare_delivery_substitute_data(snapshot, date(2026, 8, 27), Meal.DINNER)
    different = tuple(replace(a, driver='다른기사') if a.meal is Meal.BREAKFAST else a for a in assignments)
    assert len(prepare_delivery_substitute_data(snapshot, date(2026, 8, 27), Meal.DINNER).rows) == 2
    split_snapshot = HistoricalSnapshot(snapshot.revision_id, roster_entity_states(replace(roster, assignments=different)))
    split = prepare_delivery_substitute_data(split_snapshot, date(2026, 8, 27), Meal.DINNER, include_breakfast=True)
    assert len(split.rows) == 2
    assert len({row.driver_key for row in split.rows if row.person_id == both.person_id}) == 1


def test_mapping_and_copy_range_are_remembered_per_template(app, session, tmp_path):
    from src.desktop_v2.delivery_substitute import DeliverySubstituteDialog
    from src.desktop_v2.document_hub import DocumentAction
    from src.desktop_v2.document_period import DocumentPeriodSelection
    from src.desktop_v2.document_meal import DocumentMealSelection
    summary = session.database.revision_summaries()[0]
    period = DocumentPeriodSelection(DocumentAction.DELIVERY_SUBSTITUTE, summary, DAY, Meal.BREAKFAST, DAY, DAY)
    document = DocumentMealSelection(period, (Meal.BREAKFAST,))
    data = prepare_delivery_substitute_data(session.database.snapshot(summary.revision_id), DAY, Meal.BREAKFAST)
    settings = QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat)
    def make():
        dialog = DeliverySubstituteDialog(document, data)
        dialog.set_preferences_scope('synthetic', settings=settings)
        dialog.set_template_path(TEMPLATE)
        return dialog
    dialog = make()
    panel = dialog._placeholder_panel
    assert panel.rows() == (3, 3)
    assert panel.combos['배송일자'].currentData() == 'delivery_day'
    combo = panel.combos['비고']
    combo.setCurrentIndex(combo.findData('telephone'))
    dialog.accept()
    reopened = make()
    assert reopened.options().template_mapping['비고'] == 'telephone'
    assert reopened.options().template_rows == (3, 3)
    reopened.close()


def test_existing_column_priority_is_imported_and_empty_primary_falls_back(app, tmp_path):
    from src.desktop_v2.delivery_placeholder_mapping import DeliveryPlaceholderMapping
    from tests.test_delivery_substitute_v2 import CUSTOM
    snapshot, roster = source(sizes=(1, 1))
    second = replace(roster.persons[1], profile=replace(roster.persons[1].profile, custom_values={CUSTOM: ''}))
    roster = replace(roster, persons=(roster.persons[0], second))
    snapshot = HistoricalSnapshot(snapshot.revision_id, roster_entity_states(roster))
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST)
    field = f'custom:{CUSTOM}'
    panel = DeliveryPlaceholderMapping(inspect_delivery_form(TEMPLATE), data.columns, legacy_primary={'note': field})
    assert panel.mapping()['비고'] == field
    assert panel.fallbacks()['비고'] == 'note'
    output = build_substitute_workbook(data, options(data, template_mapping=panel.mapping(),
        template_fallbacks=panel.fallbacks()), template_path=TEMPLATE)
    assert output.active['F3'].value == '=문자 그대로'
    assert output.active['F3'].data_type == 's'
    assert output.active['F4'].value == '표준 비고'
    output.close()


def test_school_list_keeps_selected_meal_when_both_are_delivered_same_day():
    from datetime import UTC, datetime
    from uuid import uuid4
    from src.domain.roster import InclusiveDateRange
    from src.domain.service_adjustments import ServiceAdjustmentChange, ServiceTreatment
    from src.services.happy_delivery_report import prepare_happy_report_data
    from tests.test_sqlite_delivery_report_service import _snapshot
    thursday, friday = date(2026, 9, 10), date(2026, 9, 11)
    change = ServiceAdjustmentChange(uuid4(), InclusiveDateRange(thursday, thursday), (Meal.DINNER,),
        ServiceTreatment.REPLACEMENT, friday, (), '합성 예외 배송', datetime(2026, 9, 1, tzinfo=UTC))
    snapshot, _ = _snapshot(meals=(Meal.DINNER, Meal.BREAKFAST), adjustment=change)
    prepared = prepare_delivery_substitute_data(snapshot, friday, Meal.DINNER, include_breakfast=True)
    assert len(prepared.rows) == 1
    assert prepared.rows[0].values['receives:dinner'] == '1'
    assert prepared.rows[0].values['receives:breakfast'] == ''
    report = prepare_happy_report_data(snapshot, friday, friday, (Meal.DINNER, Meal.BREAKFAST)).reports[0]
    assert report.dinner_total == report.breakfast_total == 1
    assert report.dinner_on == report.breakfast_on == friday


@pytest.mark.parametrize('main', [Meal.DINNER, Meal.LUNCH])
def test_happy_list_ignores_pauses_delivery_dates_and_school_calendar(main):
    from datetime import UTC, datetime
    from uuid import uuid4
    from src.domain.document_templates import (DOCUMENT_TEMPLATE_SETTING_KIND, DOCUMENT_TEMPLATE_SETTING_ID,
        DocumentTemplateSetting, DocumentTemplateProfile, document_template_setting_state)
    from src.domain.revisions import EntityRef
    from src.domain.status import StatusEvent, MealStatus
    from src.domain.roster import InclusiveDateRange
    from src.domain.projections import status_event_state, STATUS_EVENT_KIND
    from src.services.lunch_schedule_exclusions import lunch_schedule_exclusion_lines
    snapshot, roster = source(sizes=(1, 1, 1, 1), meal=main)
    active, paused, only_breakfast, both_stopped = roster.persons
    roster = replace(roster,
        enrollments=roster.enrollments + tuple(MealEnrollment(p.person_id, Meal.BREAKFAST,
            roster.enrollments[0].period) for p in roster.persons),
        assignments=roster.assignments + tuple(DeliveryAssignment(p.person_id, Meal.BREAKFAST, '합성기사', str(i))
            for i,p in enumerate(roster.persons)))
    states = dict(roster_entity_states(roster))
    for person, meal, status in ((paused, main, MealStatus.PAUSED), (paused, Meal.BREAKFAST, MealStatus.PAUSED),
            (only_breakfast, main, MealStatus.STOPPED), (both_stopped, main, MealStatus.STOPPED),
            (both_stopped, Meal.BREAKFAST, MealStatus.STOPPED)):
        event = StatusEvent(uuid4(), snapshot.revision_id, person.person_id, meal, status,
            InclusiveDateRange(date(2026,8,1), date(2026,8,31) if status is MealStatus.PAUSED else None),
            datetime(2026,8,1,tzinfo=UTC))
        states[EntityRef(STATUS_EVENT_KIND, event.event_id)] = status_event_state(event)
    states[EntityRef(DOCUMENT_TEMPLATE_SETTING_KIND,DOCUMENT_TEMPLATE_SETTING_ID)] = document_template_setting_state(
        DocumentTemplateSetting(1, DocumentTemplateProfile.HAPPY_LUNCHBOX, snapshot.revision_id))
    snapshot = HistoricalSnapshot(snapshot.revision_id, states)
    # Sunday with no school calendar: only the enrollment/status list matters.
    data = prepare_delivery_substitute_data(snapshot, date(2026,8,30), main, include_breakfast=True)
    values = {row.person_id: row.values for row in data.rows}
    assert set(values) == {active.person_id, paused.person_id, only_breakfast.person_id}
    assert values[paused.person_id]['meal_receives'] == values[paused.person_id]['receives:breakfast'] == '1'
    assert values[only_breakfast.person_id]['meal_receives'] == ''
    assert data.calculation_manifest['recipient_basis'] == 'enrolled_delivery_or_paused'
    assert lunch_schedule_exclusion_lines(data.calculation_manifest) == ()
    from src.services.happy_delivery_report import prepare_happy_report_data
    daily = prepare_happy_report_data(snapshot, date(2026,8,30), date(2026,8,30), (main, Meal.BREAKFAST))
    assert daily.reports[0].breakfast_total == 3
    assert getattr(daily.reports[0], 'dinner_total' if main is Meal.DINNER else 'lunch_total') == 2
    assert lunch_schedule_exclusion_lines(daily.calculation_manifest) == ()
    output = build_substitute_workbook(data, options(data), template_path=TEMPLATE)
    output_rows = {output.active.cell(row,2).value: row for row in range(3,6)}
    row = output_rows[only_breakfast.profile.name]
    assert output.active.cell(row,7).value == 1
    assert output.active.cell(row,8).value is None
    output.close()
