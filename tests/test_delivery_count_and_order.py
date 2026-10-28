"""Regression cases for October bundles and drag-managed delivery order."""

import os
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from src.desktop_v2.driver_households import DriverHouseholdsDialog
from src.domain.delivery_routing import Driver
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.domain.roster import Meal
from src.domain.service_adjustments import (
    SERVICE_ADJUSTMENT_SCHEDULE_ID, SERVICE_ADJUSTMENT_SCHEDULE_KIND,
    ServiceAdjustmentChange, ServiceAdjustmentSchedule, ServiceTreatment,
    service_adjustment_schedule_state,
)
from src.domain.roster import InclusiveDateRange
from src.services.driver_households import DriverHouseholdRow
from src.services.monthly_meal_statistics import build_monthly_meal_statistics
from src.services.sqlite_document_service import prepare_monthly_document_data
from src.services.sqlite_delivery_report_service import prepare_delivery_report_data
from tests.test_sqlite_delivery_report_service import _snapshot


def october_source(treatment=None, replacement=None):
    source, _ = _snapshot()
    if treatment is None:
        return source
    change = ServiceAdjustmentChange(
        uuid4(), InclusiveDateRange(date(2026, 10, 9), date(2026, 10, 9)),
        (Meal.BREAKFAST,), treatment, replacement, (), "10/9 지정",
        datetime(2026, 10, 1, tzinfo=UTC),
    )
    states = dict(source.entities)
    states[EntityRef(SERVICE_ADJUSTMENT_SCHEDULE_KIND, SERVICE_ADJUSTMENT_SCHEDULE_ID)] = (
        service_adjustment_schedule_state(ServiceAdjustmentSchedule((change,)))
    )
    return HistoricalSnapshot(source.revision_id, states)


@pytest.mark.parametrize("treatment, replacement, expected", [
    (None, None, 15),
    (ServiceTreatment.NOT_PROVIDED, None, 12),
    (ServiceTreatment.NORMAL, None, 15),
    (ServiceTreatment.REPLACEMENT, date(2026, 10, 8), 15),
])
def test_october_invoice_screen_and_report_count_the_same_bundles(treatment, replacement, expected):
    source = october_source(treatment, replacement)
    start, end = date(2026, 10, 1), date(2026, 10, 31)
    invoice = prepare_monthly_document_data(source, start, end, (Meal.BREAKFAST,))
    screen = build_monthly_meal_statistics(source, 2026, 10)
    report = prepare_delivery_report_data(source, start, end, (Meal.BREAKFAST,))
    assert invoice.billing.total_count == screen.breakfast_total == expected
    assert report.calculation_manifest["target_entitlement_count"] == expected
    assert sum(week.breakfast_count for week in invoice.batch.deliveries[0].weekly) == expected
    dates = {item.service_on for item in invoice.entitlements}
    assert date(2026, 10, 3) in dates  # An unreviewed holiday is an ordinary service day.
    assert date(2026, 11, 1) in dates  # Delivered with October 30's bundle.
    if treatment is ServiceTreatment.NOT_PROVIDED:
        assert not dates.intersection({date(2026, 10, day) for day in (9, 10, 11)})
        assert screen.days[7].breakfast == screen.days[8].breakfast == 0
    if replacement is not None:
        assert screen.days[7].breakfast == 3
        assert screen.days[8].breakfast == 0


def test_adjacent_invoice_months_do_not_count_the_same_service_twice():
    source = october_source()
    october = prepare_monthly_document_data(source, date(2026, 10, 1), date(2026, 10, 31), (Meal.BREAKFAST,))
    november = prepare_monthly_document_data(source, date(2026, 11, 1), date(2026, 11, 30), (Meal.BREAKFAST,))
    assert not set(october.entitlements).intersection(november.entitlements)
    assert november.billing.total_count == build_monthly_meal_statistics(source, 2026, 11).breakfast_total


def test_next_month_switch_applies_to_invoice_and_screen_but_daily_keeps_physical_count():
    from dataclasses import asdict
    from src.domain.invoice_settings import InvoiceSettings, INVOICE_SETTINGS_KIND, INVOICE_SETTINGS_ID
    source = october_source(ServiceTreatment.NOT_PROVIDED)
    entities = dict(source.entities)
    entities[EntityRef(INVOICE_SETTINGS_KIND, INVOICE_SETTINGS_ID)] = asdict(InvoiceSettings(include_next_month=False))
    source = HistoricalSnapshot(source.revision_id, entities)
    invoice = prepare_monthly_document_data(source, date(2026, 10, 1), date(2026, 10, 31), (Meal.BREAKFAST,))
    screen = build_monthly_meal_statistics(source, 2026, 10)
    assert invoice.billing.total_count == screen.breakfast_total == 11
    assert not screen.include_next_month
    assert date(2026, 11, 1) not in {item.service_on for item in invoice.entitlements}
    report = prepare_delivery_report_data(source, date(2026, 10, 1), date(2026, 10, 31), (Meal.BREAKFAST,))
    assert report.calculation_manifest["target_entitlement_count"] == 12


def test_drag_save_is_one_revision_survives_reopen_and_rejects_stale_source(app, tmp_path):
    from src.desktop_v2.sequence_edit import SequenceEditController, SequenceSaveStatus
    from src.domain.projections import roster_from_historical_snapshot
    from src.domain.revisions import RevisionActor
    from src.storage.database_session import DatabaseSession
    from tests.test_delivery_routing import initialize
    source, people = _snapshot(people=2, same_household=False, meals=(Meal.DINNER, Meal.BREAKFAST))
    roster = roster_from_historical_snapshot(source)
    path = tmp_path / "drag.sqlite3"
    with DatabaseSession.create(path, "password") as session:
        initialize(session, roster)
        controller = SequenceEditController(session, actor_provider=lambda: RevisionActor("test", "test-sid"))
        result = controller.save_order({(people[0], Meal.DINNER): "2", (people[1], Meal.DINNER): "1"}, expected_revision_id=roster.revision_id)
        assert result.status is SequenceSaveStatus.SAVED
        after = result.roster
        assert after.resolve_assignment(people[0], Meal.BREAKFAST).sequence == "1"
        assert after.resolve_assignment(people[0], Meal.DINNER).driver == "김기사"
        stale = controller.save_order({(people[0], Meal.DINNER): "3"}, expected_revision_id=roster.revision_id)
        assert stale.status is SequenceSaveStatus.FAILED
        assert session.database.current_roster().revision_id == after.revision_id
        unchanged = controller.save_order({(people[0], Meal.DINNER): "2"}, expected_revision_id=after.revision_id)
        assert unchanged.status is SequenceSaveStatus.UNCHANGED
    with DatabaseSession.open(path, "password") as session:
        reopened = session.database.current_roster()
        assert reopened.resolve_assignment(people[0], Meal.DINNER).sequence == "2"
        assert reopened.resolve_assignment(people[1], Meal.DINNER).sequence == "1"


def test_saved_driver_registry_order_is_default_for_daily_report():
    from src.domain.delivery_routing import DRIVER_KIND, DRIVER_ORDER_KIND, DRIVER_ORDER_ID
    from src.domain.projections import roster_from_historical_snapshot, roster_entity_states
    source, people = _snapshot(people=2, same_household=False)
    roster = roster_from_historical_snapshot(source)
    roster = replace(roster, assignments=tuple(
        replace(item, driver="가기사" if item.person_id == people[0] else "나기사") for item in roster.assignments
    ))
    entities = dict(source.entities)
    entities.update(roster_entity_states(roster))
    first, second = uuid4(), uuid4()
    entities[EntityRef(DRIVER_KIND, first)] = {"name": "가기사", "telephone": ""}
    entities[EntityRef(DRIVER_KIND, second)] = {"name": "나기사", "telephone": ""}
    entities[EntityRef(DRIVER_ORDER_KIND, DRIVER_ORDER_ID)] = {"ordered_driver_ids": [str(second), str(first)]}
    source = HistoricalSnapshot(source.revision_id, entities)
    report = prepare_delivery_report_data(source, date(2026, 10, 2), date(2026, 10, 2), (Meal.BREAKFAST,))
    assert report.calculation_manifest["staff_order"][Meal.BREAKFAST.value] == ("나기사", "가기사")
    assert [item.staff_label.splitlines()[0] for item in report.reports[0].rows] == ["나기사", "가기사"]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def row(driver, name, sequence, *, household=None, person=None, meal=Meal.DINNER):
    return DriverHouseholdRow(
        household or uuid4(), driver.driver_id, driver.name,
        (person or uuid4(),), (name,), (meal,), "합성도로 1", ("010-0000-0000",), (str(sequence),),
    )


def test_drag_orders_only_selected_driver_exact_pairs_and_hides_columns(app):
    first, second = Driver(uuid4(), "첫 기사"), Driver(uuid4(), "둘째 기사")
    rows = (row(first, "가구 가", 1), row(first, "가구 나", 2), row(second, "다른 기사 가구", 1))
    dialog = DriverHouseholdsDialog(rows, service_day=date(2026, 10, 1), drivers=(first, second))
    values = []
    dialog.order_save_requested.connect(values.append)
    assert dialog.windowTitle() == "배송순서관리"
    assert dialog.table.isColumnHidden(0) and dialog.table.isColumnHidden(3)
    assert dialog.table.rowCount() == 2
    dialog.table.move_row(0, 2)
    assert values == [{(rows[1].member_ids[0], Meal.DINNER): "1", (rows[0].member_ids[0], Meal.DINNER): "2"}]
    assert dialog.table.item(0, 1).text() == "가구 나"
    dialog.driver_combo.setCurrentIndex(1)
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 1).text() == "다른 기사 가구"
    assert dialog.current_view_snapshot().headers == ("가구원", "식종", "주소", "연락처")
    dialog.close()


def test_different_driver_in_one_household_defaults_to_split_and_can_merge(app):
    first, second = Driver(uuid4(), "첫 기사"), Driver(uuid4(), "둘째 기사")
    household, person = uuid4(), uuid4()
    dinner = row(first, "합성 가구", 1, household=household, person=person)
    breakfast = row(first, "합성 가구", 2, household=household, person=person, meal=Meal.BREAKFAST)
    merged = replace(dinner, meals=(Meal.DINNER, Meal.BREAKFAST), meal_rows=(dinner, breakfast))
    lunch = row(second, "합성 가구", 1, household=household, person=person, meal=Meal.LUNCH)
    dialog = DriverHouseholdsDialog((merged, lunch), service_day=date(2026, 10, 1), drivers=(first, second))
    assert dialog._split_by_meal and dialog.table.rowCount() == 2
    dialog.split_button.click()
    assert not dialog._split_by_meal and dialog.table.rowCount() == 1
    dialog._set_column_search("members", "합성")
    assert dialog.table.dragEnabled()
    dialog._clear_all_filters()
    assert dialog.table.dragEnabled()
    dialog.close()
    same = DriverHouseholdsDialog((merged,), service_day=date(2026, 10, 1), drivers=(first,))
    assert not same._split_by_meal and same.table.rowCount() == 1
    same.close()


def test_filtered_meal_drag_preserves_hidden_slots_and_other_meals(app):
    driver = Driver(uuid4(), "합성 기사")
    dinners = (row(driver, "선택 가구 A", 1), row(driver, "숨긴 가구", 2), row(driver, "선택 가구 C", 3))
    breakfasts = tuple(replace(item, meals=(Meal.BREAKFAST,)) for item in dinners)
    merged = tuple(replace(dinner, meals=(Meal.DINNER, Meal.BREAKFAST), meal_rows=(dinner, breakfast))
                   for dinner, breakfast in zip(dinners, breakfasts, strict=True))
    dialog = DriverHouseholdsDialog(merged, service_day=date(2026, 10, 1), drivers=(driver,))
    saved = []
    dialog.order_save_requested.connect(saved.append)
    dialog.meal_combo.setCurrentIndex(dialog.meal_combo.findData(Meal.DINNER.value))
    assert dialog.table.rowCount() == 3
    assert all(dialog.table.item(index, 2).text() == "석식" for index in range(3))
    dialog._set_column_search("members", "선택")
    assert dialog.table.rowCount() == 2 and dialog.table.dragEnabled()
    assert [dialog.table.verticalHeaderItem(index).text() for index in range(2)] == ["1", "3"]
    assert dialog.table.corner_header.text() == "배송순서"
    dialog.table.move_row(1, 0)
    assert saved == [{
        (dinners[2].member_ids[0], Meal.DINNER): "1",
        (dinners[1].member_ids[0], Meal.DINNER): "2",
        (dinners[0].member_ids[0], Meal.DINNER): "3",
    }]
    assert dialog._column_searches == {"members": "선택"}
    assert [dialog.table.verticalHeaderItem(index).text() for index in range(2)] == ["1", "3"]
    dialog._clear_all_filters()
    assert dialog.meal_combo.currentData() == Meal.DINNER.value
    dialog.meal_combo.setCurrentIndex(dialog.meal_combo.findData(Meal.BREAKFAST.value))
    assert all(dialog.table.item(index, 2).text() == "조식" for index in range(3))
    assert len(saved) == 1
    dialog.close()


def test_native_drop_before_first_visible_cell_preserves_complete_rows(app, monkeypatch):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QDropEvent
    import src.desktop_v2.delivery_routing as module
    driver = Driver(uuid4(), "합성 기사")
    rows = (row(driver, "가구 가", 1), row(driver, "가구 나", 2))
    dialog = DriverHouseholdsDialog(rows, service_day=date(2026, 10, 1), drivers=(driver,))
    dialog.show()
    app.processEvents()
    saved = []
    dialog.order_save_requested.connect(saved.append)

    class Drag:
        def __init__(self, table):
            self.table = table
        def setMimeData(self, data):
            self.data = data
        def exec(self, action):
            table = self.table
            class Drop(QDropEvent):
                def source(self):
                    return table
            rectangle = table.visualItemRect(table.item(0, 1))
            event = Drop(QPointF(rectangle.left() + 5, rectangle.top() + 2), action, self.data,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            table.dropEvent(event)
            return action

    monkeypatch.setattr(module, "QDrag", Drag)
    dialog.table.selectRow(1)
    dialog.table.startDrag(Qt.DropAction.MoveAction)
    assert dialog.table.item(0, 1).text() == "가구 나"
    assert dialog.table.rowCount() == 2
    assert len(saved) == 1 and len(saved[0]) == 2
    assert all(dialog.table.item(r, c) is not None for r in range(2) for c in range(6))
    dialog.close()



@pytest.mark.parametrize("year,month,next_year,next_month,carry_days", (
    (2026, 10, 2026, 11, (1,)),
    (2027, 12, 2028, 1, (1, 2)),
))
def test_service_month_rollover_keeps_every_meal_once_and_weekly_totals(year, month, next_year, next_month, carry_days):
    from calendar import monthrange
    from dataclasses import asdict
    from src.domain.invoice_settings import InvoiceSettings, INVOICE_SETTINGS_KIND, INVOICE_SETTINGS_ID
    from src.services.monthly_meal_statistics import MealCountBasis
    source = october_source()
    entities = dict(source.entities)
    entities[EntityRef(INVOICE_SETTINGS_KIND, INVOICE_SETTINGS_ID)] = asdict(InvoiceSettings(include_next_month=False))
    source = HistoricalSnapshot(source.revision_id, entities)
    first = prepare_monthly_document_data(source, date(year, month, 1), date(year, month, monthrange(year, month)[1]), (Meal.BREAKFAST,))
    following = prepare_monthly_document_data(source, date(next_year, next_month, 1), date(next_year, next_month, monthrange(next_year, next_month)[1]), (Meal.BREAKFAST,))
    dates = {item.service_on for item in following.entitlements}
    assert {date(next_year, next_month, day) for day in carry_days} <= dates
    assert not set(first.entitlements) & set(following.entitlements)
    for document, y, m in ((first, year, month), (following, next_year, next_month)):
        assert all((item.service_on.year, item.service_on.month) == (y, m) for item in document.entitlements)
        assert sum(w.breakfast_count for row in document.batch.deliveries for w in row.weekly) == document.billing.total_count
        screen = build_monthly_meal_statistics(source, y, m)
        assert screen.breakfast_total == document.billing.total_count
        assert screen.basis is MealCountBasis.SERVICE
        assert document.calculation_manifest["period_basis"] == "service_day"
    screen = build_monthly_meal_statistics(source, next_year, next_month)
    for day in carry_days:
        assert screen.for_basis(MealCountBasis.SERVICE).days[day - 1].breakfast == 1
        assert screen.for_basis(MealCountBasis.DELIVERY).days[day - 1].breakfast == 0
