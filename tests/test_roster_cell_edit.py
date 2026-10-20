"""Behavioral coverage for inline roster profile-cell editing."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QPlainTextEdit

from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.roster_cell_edit import (
    RosterCellEditController,
    RosterCellEditOutcome,
    RosterCellEditStatus,
    updated_profile_for_cell,
)
from src.desktop_v2.roster_window import RosterWindow
from src.domain.revisions import RevisionActor
from src.domain.roster import (
    CustomFieldDefinition,
    CustomFieldType,
    HouseholdDeliverySetting,
    Meal,
    Person,
    PersonProfile,
    RosterSnapshot,
)


PERSON_A = UUID("99000000-0000-0000-0000-000000000001")
PERSON_B = UUID("99000000-0000-0000-0000-000000000002")
REVISION = UUID("99000000-0000-0000-0000-000000000003")
NEXT_REVISION = UUID("99000000-0000-0000-0000-000000000004")
NUMBER_FIELD = UUID("99000000-0000-0000-0000-000000000005")
DATE_FIELD = UUID("99000000-0000-0000-0000-000000000006")
CHOICE_FIELD = UUID("99000000-0000-0000-0000-000000000007")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _definitions():
    return (
        CustomFieldDefinition(NUMBER_FIELD, "지원 횟수", CustomFieldType.NUMBER, 0),
        CustomFieldDefinition(DATE_FIELD, "등록일", CustomFieldType.DATE, 1),
        CustomFieldDefinition(
            CHOICE_FIELD, "구분", CustomFieldType.CHOICE, 2, ("가", "나")
        ),
    )


def _roster():
    return RosterSnapshot(
        REVISION,
        persons=(
            Person(
                PERSON_A,
                PersonProfile(
                    "가 대상",
                    birth_date=date(2015, 3, 2),
                    school_name="가초",
                    grade="1",
                    telephone="031-1",
                    mobile_phone="010-1",
                    jurisdiction="분당동",
                    road_address="성남대로 1",
                    detail_address="101호",
                    note="첫 줄\n둘째 줄",
                    custom_values={
                        NUMBER_FIELD: 2,
                        DATE_FIELD: date(2026, 1, 2),
                        CHOICE_FIELD: "가",
                    },
                ),
                delivery_settings=(
                    HouseholdDeliverySetting(Meal.BREAKFAST, (0,)),
                ),
            ),
            Person(PERSON_B, PersonProfile("나 대상", school_name="나초")),
        ),
        custom_fields=_definitions(),
    )


def _column(window, key):
    return next(index for index, item in enumerate(window.table_model.columns) if item.key == key)


def _double_click(window, row, key, app):
    column = _column(window, key)
    window._table.setColumnHidden(column, False)
    index = window.table_model.index(row, column)
    window._table.scrollTo(index)
    app.processEvents()
    QTest.mouseClick(
        window._table.viewport(),
        Qt.MouseButton.LeftButton,
        pos=window._table.visualRect(index).center(),
    )
    QTest.mouseDClick(
        window._table.viewport(),
        Qt.MouseButton.LeftButton,
        pos=window._table.visualRect(index).center(),
    )
    app.processEvents()
    return index


def _active_editor(window, app, editor_type):
    focused = app.focusWidget()
    if isinstance(focused, editor_type) and focused.objectName() == "rosterCellEditor":
        return focused
    return next(
        (editor for editor in window.findChildren(editor_type, "rosterCellEditor") if editor.isVisible()),
        None,
    )


def test_profile_parser_reuses_date_number_and_choice_validation_without_touching_other_fields():
    person = _roster().person(PERSON_A)
    definitions = _definitions()
    changed_date = updated_profile_for_cell(person, "birth_date", "20160304", definitions)
    assert changed_date.birth_date == date(2016, 3, 4)
    assert changed_date.mobile_phone == "010-1"
    changed_number = updated_profile_for_cell(
        person, f"custom:{NUMBER_FIELD}", "3.5", definitions
    )
    assert changed_number.custom_values[NUMBER_FIELD] == 3.5
    changed_custom_date = updated_profile_for_cell(
        person, f"custom:{DATE_FIELD}", "2026-04-05", definitions
    )
    assert changed_custom_date.custom_values[DATE_FIELD] == date(2026, 4, 5)
    changed_choice = updated_profile_for_cell(
        person, f"custom:{CHOICE_FIELD}", "나", definitions
    )
    assert changed_choice.custom_values[CHOICE_FIELD] == "나"
    with pytest.raises(ValueError, match="선택 목록"):
        updated_profile_for_cell(person, f"custom:{CHOICE_FIELD}", "다", definitions)
    with pytest.raises(ValueError, match="숫자"):
        updated_profile_for_cell(person, f"custom:{NUMBER_FIELD}", "셋", definitions)


def test_controller_commits_once_skips_unchanged_and_does_not_copy_mobile_to_household(app):
    roster = _roster()

    class Database:
        def current_roster(self):
            return roster

    records = []

    class Session:
        database = Database()

        def append_revision(self, record):
            records.append(record)
            return SimpleNamespace(error=None)

    controller = RosterCellEditController(
        Session(),
        actor_provider=lambda: RevisionActor("tester", "cell"),
        recorded_at_provider=lambda: datetime(2026, 10, 14, tzinfo=UTC),
        effective_date_provider=lambda: date(2026, 10, 14),
        revision_id_factory=lambda: NEXT_REVISION,
    )
    unchanged = controller.save(PERSON_A, "note", "첫 줄\n둘째 줄")
    assert unchanged.status is RosterCellEditStatus.CANCELLED
    assert records == []
    changed = controller.save(PERSON_A, "mobile", "010-9999")
    assert changed.completed and len(records) == 1
    assert changed.roster.person(PERSON_A).profile.mobile_phone == "010-9999"
    assert changed.roster.person(PERSON_B).profile.mobile_phone is None
    assert changed.roster.person(PERSON_A).profile.note == roster.person(PERSON_A).profile.note


def test_controller_failure_reports_error_without_mutating_visible_or_source_roster(app):
    roster = _roster()
    errors = []

    class Session:
        database = SimpleNamespace(current_roster=lambda: roster)

        @staticmethod
        def append_revision(_record):
            raise OSError("쓰기 실패")

    class Messages:
        @staticmethod
        def critical(_parent, title, message):
            errors.append((title, message))

        @staticmethod
        def warning(*_args):
            raise AssertionError("저장 실패 뒤 백업 경고를 표시하면 안 됩니다.")

    outcome = RosterCellEditController(
        Session(), message_box=Messages,
        actor_provider=lambda: RevisionActor("tester", "cell"),
        recorded_at_provider=lambda: datetime(2026, 10, 14, tzinfo=UTC),
        effective_date_provider=lambda: date(2026, 10, 14),
        revision_id_factory=lambda: NEXT_REVISION,
    ).save(PERSON_A, "school", "새 학교")
    assert outcome.status is RosterCellEditStatus.FAILED
    assert roster.person(PERSON_A).profile.school_name == "가초"
    assert "운영 데이터에는 반영하지 않았습니다" in errors[0][1]


def test_real_double_click_enter_escape_focus_and_multiline_note(app):
    saved = []
    window = RosterWindow(_roster(), cell_edit_callback=lambda *args: saved.append(args))
    # These profile-only fixtures have no meal history and belong to the archive.
    window._include_hidden_filter.setChecked(True)
    window.show()

    _double_click(window, 0, "name", app)
    editor = _active_editor(window, app, QLineEdit)
    assert editor is not None and editor.text() == "가 대상"
    editor.selectAll()
    QTest.keyClicks(editor, "changed")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    app.processEvents()
    assert saved == [(PERSON_A, "name", "changed")]

    _double_click(window, 0, "birth_date", app)
    editor = _active_editor(window, app, QLineEdit)
    editor.selectAll()
    QTest.keyClicks(editor, "20160304")
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    app.processEvents()
    assert len(saved) == 1

    _double_click(window, 0, "school", app)
    editor = _active_editor(window, app, QLineEdit)
    editor.selectAll()
    QTest.keyClicks(editor, "focus school")
    window._table.setFocus()
    app.processEvents()
    assert saved[-1] == (PERSON_A, "school", "focus school")
    assert saved.count(saved[-1]) == 1

    _double_click(window, 0, "note", app)
    note = _active_editor(window, app, QPlainTextEdit)
    assert note is not None and note.toPlainText() == _roster().person(PERSON_A).profile.note
    QTest.keyClick(note, Qt.Key.Key_Escape)
    app.processEvents()
    assert all(item[1] != "note" for item in saved)
    window.close()


def test_choice_editor_and_sorted_identity_are_bound_at_edit_start(app):
    saved = []
    window = RosterWindow(_roster(), cell_edit_callback=lambda *args: saved.append(args))
    window._include_hidden_filter.setChecked(True)
    window.show()
    name_column = _column(window, "name")
    window.table_model.cycle_sort(name_column)
    window.table_model.cycle_sort(name_column)
    assert window.table_model.person_at(0).person_id == PERSON_B

    _double_click(window, 1, f"custom:{CHOICE_FIELD}", app)
    choice = _active_editor(window, app, QComboBox)
    assert choice is not None and choice.currentData() == "가"
    choice.showPopup()
    app.processEvents()
    assert choice.isVisible() and not choice._roster_finished and saved == []
    option = choice.model().index(choice.findData("나"), 0)
    QTest.mouseClick(
        choice.view().viewport(),
        Qt.MouseButton.LeftButton,
        pos=choice.view().visualRect(option).center(),
    )
    app.processEvents()
    assert choice.currentData() == "나" and not choice._roster_finished
    QTest.keyClick(choice, Qt.Key.Key_Return)
    app.processEvents()
    assert saved == [(PERSON_A, f"custom:{CHOICE_FIELD}", "나")]
    window.close()

    filtered = []
    window = RosterWindow(_roster(), cell_edit_callback=lambda *args: filtered.append(args))
    window._include_hidden_filter.setChecked(True)
    window.table_model.set_search("가 대상")
    window.show()
    assert window.table_model.rowCount() == 1
    assert window.table_model.person_at(0).person_id == PERSON_A
    _double_click(window, 0, "grade", app)
    grade = _active_editor(window, app, QLineEdit)
    grade.selectAll()
    QTest.keyClicks(grade, "2")
    QTest.keyClick(grade, Qt.Key.Key_Return)
    app.processEvents()
    assert filtered == [(PERSON_A, "grade", "2")]
    window.close()


def test_double_click_routes_special_columns_and_keeps_meals_read_only(app):
    window = RosterWindow(_roster())
    window._include_hidden_filter.setChecked(True)
    sequence, relocation, delivery, cell = [], [], [], []
    window.sequence_edit_requested.connect(sequence.append)
    window.relocation_requested.connect(relocation.append)
    window.delivery_settings_requested.connect(delivery.append)
    window.cell_edit_requested.connect(lambda *args: cell.append(args))
    window.show()

    row = next(
        index for index in range(window.table_model.rowCount())
        if window.table_model.person_at(index).person_id == PERSON_A
    )
    for key, original, changed in (
        ("address", "성남대로 1", "성남대로 2"),
        ("detail_address", "101호", "202호"),
        ("jurisdiction", "분당동", "정자동"),
    ):
        index = _double_click(window, row, key, app)
        editor = _active_editor(window, app, QLineEdit)
        assert editor is not None and editor.text() == original
        assert window.table_model.flags(index) & Qt.ItemFlag.ItemIsEditable
        editor.selectAll()
        editor.setText(changed)
        QTest.keyClick(editor, Qt.Key.Key_Return)
        app.processEvents()
        assert "우클릭 → 이사 등록" in index.data(Qt.ItemDataRole.ToolTipRole)
    assert relocation == []
    assert cell == [
        (PERSON_A, "address", "성남대로 2"),
        (PERSON_A, "detail_address", "202호"),
        (PERSON_A, "jurisdiction", "정자동"),
    ]

    person = window.table_model.person_at(row)
    for key in ("address", "detail_address", "jurisdiction"):
        menu = window._create_context_menu(person, column_key=key)
        next(action for action in menu.actions() if action.text() == "이사 등록").trigger()
    sequence_index = window.table_model.index(row, _column(window, "sequence"))
    window._table.doubleClicked.emit(sequence_index)
    meal_index = window.table_model.index(row, _column(window, "status:lunch"))
    assert not (window.table_model.flags(meal_index) & Qt.ItemFlag.ItemIsEditable)
    window._table.doubleClicked.emit(meal_index)

    window._special_only_filter.setChecked(True)
    app.processEvents()
    delivery_row = next(
        index for index in range(window.table_model.rowCount())
        if window.table_model.person_at(index).person_id == PERSON_A
    )
    delivery_index = window.table_model.index(
        delivery_row, _column(window, "delivery_settings")
    )
    window._table.doubleClicked.emit(delivery_index)
    assert relocation == [PERSON_A, PERSON_A, PERSON_A]
    assert sequence == [PERSON_A]
    assert delivery == [PERSON_A]
    window.close()


def test_application_refreshes_roster_histories_and_calendar_without_replacing_routing_state():
    roster = _roster()
    saved_roster = RosterSnapshot(
        NEXT_REVISION,
        persons=roster.persons,
        custom_fields=roster.custom_fields,
    )

    class CellController:
        def save(self, person_id, column_key, text, parent):
            assert (person_id, column_key, text, parent) == (
                PERSON_A, "grade", "2", window
            )
            return RosterCellEditOutcome(RosterCellEditStatus.COMPLETED, saved_roster)

    class Window:
        routing_state = object()

        def replace_roster(self, *args, **kwargs):
            self.replacement = (args, kwargs)

    window = Window()
    original_routing = window.routing_state
    startup = DesktopStartupController.__new__(DesktopStartupController)
    startup._roster_cell_edit_controller_type = lambda _session: CellController()
    startup._histories = lambda _session, value: {"for": value.revision_id}
    startup._revision_display_code = lambda _session, value: str(value.revision_id)
    calendar_refreshes = []
    startup._refresh_school_calendar = lambda _session, target, value, **_kwargs: calendar_refreshes.append(
        (target, value.revision_id)
    )
    from types import SimpleNamespace
    startup._prepare_startup = lambda _session, value: SimpleNamespace(
        histories={"for": value.revision_id}, revision_display_code=str(value.revision_id),
        snapshot=None, imported_profile_fields=frozenset(),
    )
    startup._run_roster_cell_edit(object(), window, PERSON_A, "grade", "2")
    assert window.replacement[0] == (saved_roster, {"for": NEXT_REVISION})
    assert window.replacement[1]["revision_display_code"] == str(NEXT_REVISION)
    assert calendar_refreshes == [(window, NEXT_REVISION)]
    assert window.routing_state is original_routing


def test_application_passes_view_day_only_for_routing_cell():
    service_day = date(2026, 10, 10)
    calls = []

    class CellController:
        def save(self, *args, **kwargs):
            calls.append((args, kwargs))
            return RosterCellEditOutcome(RosterCellEditStatus.CANCELLED)

    class Window:
        table_model = SimpleNamespace(service_day=service_day)

    startup = DesktopStartupController.__new__(DesktopStartupController)
    startup._roster_cell_edit_controller_type = lambda _session: CellController()
    window = Window()
    startup._run_roster_cell_edit(object(), window, PERSON_A, "address", "새 주소")
    startup._run_roster_cell_edit(object(), window, PERSON_A, "grade", "2")

    assert calls[0] == (
        (PERSON_A, "address", "새 주소", window),
        {"service_day": service_day},
    )
    assert calls[1] == ((PERSON_A, "grade", "2", window), {})
