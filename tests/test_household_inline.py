"""Aggregate sequence blanks and Enter-only household sequence persistence."""
import os
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from src.desktop_v2.column_search import ALL_SEQUENCES_EMPTY, SequenceValuePicker, sequence_pair_token
from src.desktop_v2.driver_households import DriverHouseholdsDialog
from src.desktop_v2.roster_window import RosterTableModel, RosterWindow
from src.desktop_v2.sequence_edit import SequenceEditController, SequenceSaveStatus
from src.domain.roster import (
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
    DeliveryAssignment,
)
from src.domain.revisions import RevisionActor
from src.storage.database_session import DatabaseSession
from tests.test_delivery_routing import initialize
from tests.test_sequence_workflow import manifest_rows, roster_fixture


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    import src.desktop_v2.inactive_roster as inactive
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.IniFormat)
    monkeypatch.setattr(inactive, "QSettings", lambda *a, **k: settings)
    return settings


def test_aggregate_empty_is_independent_of_per_meal_blank_and_preserves_or(app, settings):
    roster = roster_fixture()
    # Nonempty common/explicit sequence prevents aggregate blank even without
    # enrollment. Completely unassigned and all-enrolled-empty both match.
    unassigned = Person(uuid4(), PersonProfile("완전 미지정"))
    roster = replace(roster, persons=(*roster.persons, unassigned))
    model = RosterTableModel(roster)
    assert ALL_SEQUENCES_EMPTY in model.column_filter_values("sequence")
    model.set_column_values("sequence", {ALL_SEQUENCES_EMPTY})
    assert {model.person_at(i).profile.name for i in range(model.rowCount())} == {"빈값", "완전 미지정"}
    pair = sequence_pair_token("조식", "2")
    model.set_column_values("sequence", {ALL_SEQUENCES_EMPTY, pair})
    assert {model.person_at(i).profile.name for i in range(model.rowCount())} == {"빈값", "완전 미지정", "둘", "혼합"}
    model.set_column_search("name", "완전")
    assert model.rowCount() == 1
    model.set_column_search("name", "")
    model.set_column_values("sequence", set())
    assert model.rowCount() == 0
    window = RosterWindow(roster, column_settings=settings, inactive_settings=settings)
    window._include_hidden_filter.setChecked(True)
    window._on_column_values_changed("sequence", {ALL_SEQUENCES_EMPTY, pair})
    window.close()
    window = RosterWindow(roster, column_settings=settings, inactive_settings=settings)
    assert window.table_model.selected_column_values("sequence") == frozenset({ALL_SEQUENCES_EMPTY, pair})
    window._column_search_header.open_value_filter("sequence")
    picker = window._column_search_header._menu.findChild(SequenceValuePicker)
    aggregate = next(item for item in picker._leaves if item.data(0, Qt.UserRole) == ALL_SEQUENCES_EMPTY)
    assert aggregate.parent() is None and aggregate.text(0) == "전체 빈 값"
    assert aggregate.checkState(0) == Qt.Checked
    picker.search.setText("전체 빈")
    assert not aggregate.isHidden()
    picker.select_none.click()
    aggregate.setCheckState(0, Qt.Checked)
    picker.search.clear()
    picker.apply.click()
    assert window.table_model.rowCount() == 2
    window.close()


def test_aggregate_rejects_mixed_blank_and_nonempty_common_with_meal_driver_only():
    first, second = (Person(uuid4(), PersonProfile(name)) for name in ("일부 빈값", "공통 보존"))
    roster = RosterSnapshot(uuid4(), persons=(first, second),
        enrollments=tuple(MealEnrollment(person.person_id, meal) for person in (first, second) for meal in Meal),
        assignments=(DeliveryAssignment(first.person_id, Meal.DINNER, sequence="1"),
                     DeliveryAssignment(second.person_id, None, sequence="C"),
                     *(DeliveryAssignment(second.person_id, meal, driver="기사") for meal in Meal)))
    model = RosterTableModel(roster)
    model.set_column_values("sequence", {sequence_pair_token("조식", "")})
    assert model.rowCount() >= 1
    model.set_column_values("sequence", {ALL_SEQUENCES_EMPTY})
    assert model.rowCount() == 0


def start_editor(dialog, app, row=0, *, mouse=True):
    item = dialog.table.item(row, 3)
    dialog.table.scrollToItem(item)
    app.processEvents()
    if mouse:
        point = dialog.table.visualItemRect(item).center()
        QTest.mouseClick(dialog.table.viewport(), Qt.LeftButton, pos=point)
        QTest.mouseDClick(dialog.table.viewport(), Qt.LeftButton, pos=point)
    else:
        dialog.table.editItem(item)
    app.processEvents()
    editor = next((item for item in dialog.table.findChildren(QLineEdit, "householdSequenceEditor")
                   if item.isVisible()), None)
    assert editor is not None
    return editor


def test_inline_enter_uses_captured_exact_pairs_and_esc_focus_loss_never_save(app):
    roster = roster_fixture()
    merged, dinner, breakfast = manifest_rows(roster)
    other = replace(dinner, household_key=uuid4(), member_ids=(roster.persons[2].person_id,), member_names=("별도",), sequences=("10",))
    dialog = DriverHouseholdsDialog((merged, other), service_day=date(2026, 10, 13), drivers=())
    dialog.show()
    received = []
    dialog.sequence_save_requested.connect(lambda *args: received.append(args))
    dialog.table.sortItems(3, Qt.DescendingOrder)
    dialog._set_column_search("members", "하나")
    editor = start_editor(dialog, app)
    assert editor.text() == "" and editor.placeholderText()
    targets = editor._sequence_targets
    assert set(targets) == {(roster.persons[0].person_id, Meal.DINNER), (roster.persons[1].person_id, Meal.BREAKFAST)}
    assert editor._sequence_row == merged
    editor.setText("001")
    QTest.keyClick(editor, Qt.Key_Return)
    app.processEvents()
    assert received == [(targets, "001")]
    assert dialog.table.item(0, 3).text() == "001, 2"  # No optimistic display.
    editor = start_editor(dialog, app)
    editor.setText("99")
    QTest.keyClick(editor, Qt.Key_Escape)
    app.processEvents()
    assert len(received) == 1 and dialog.table.item(0, 3).text() == "001, 2"
    editor = start_editor(dialog, app)
    editor.setText("88")
    dialog.split_button.setFocus()
    app.processEvents()
    assert len(received) == 1 and dialog.table.item(0, 3).text() == "001, 2"
    assert "취소" in dialog.sequence_status.text()
    # Only sequence cells are editable, including real double-clicks.
    point = dialog.table.visualItemRect(dialog.table.item(0, 1)).center()
    QTest.mouseDClick(dialog.table.viewport(), Qt.LeftButton, pos=point)
    app.processEvents()
    assert not any(editor.isVisible() for editor in dialog.table.findChildren(QLineEdit))
    dialog._set_column_search("members", "")
    dialog.split_button.click()
    dialog._set_column_values("meals", {"조식"})
    editor = start_editor(dialog, app)
    assert editor.text() == "2"
    editor.setText("007")
    QTest.keyClick(editor, Qt.Key_Return)
    assert received[-1] == (((roster.persons[1].person_id, Meal.BREAKFAST),), "007")
    dialog.close()


def test_sort_during_inline_edit_keeps_original_snapshot_and_target(app):
    roster = roster_fixture()
    merged, dinner, breakfast = manifest_rows(roster)
    dialog = DriverHouseholdsDialog((dinner, breakfast), service_day=date(2026, 10, 13), drivers=())
    dialog.show()
    received = []
    dialog.sequence_save_requested.connect(lambda *args: received.append(args))
    editor = start_editor(dialog, app)
    target = editor._sequence_targets
    editor.setText("012")
    dialog.table.sortItems(3, Qt.DescendingOrder)
    QTest.keyClick(editor, Qt.Key_Return)
    assert received == [(target, "012")]
    assert target == ((roster.persons[0].person_id, Meal.DINNER),)
    dialog.close()


def test_save_value_atomic_noop_invalid_failure_and_backup_warning(tmp_path):
    roster = roster_fixture()
    targets = ((roster.persons[0].person_id, Meal.DINNER), (roster.persons[1].person_id, Meal.BREAKFAST))
    with DatabaseSession.create(tmp_path / "inline.sqlite3", "password", "recovery") as session:
        initialize(session, roster)
        controller = SequenceEditController(session, actor_provider=lambda: RevisionActor("tester", "sid"))
        appends = []
        original = session.append_revision
        session.append_revision = lambda record: (appends.append(record), original(record))[1]
        for bad in ("", "  ", "1\n2", "1\r2"):
            assert controller.save_value(targets, bad).status is SequenceSaveStatus.FAILED
        assert not appends and session.database.current_roster().revision_id == roster.revision_id
        outcome = controller.save_value(targets, " 001 ")
        assert outcome.status is SequenceSaveStatus.SAVED and not outcome.backup_warning
        assert len(appends) == 1
        assert outcome.roster.resolve_assignment(*targets[0]).sequence == "001"
        assert outcome.roster.resolve_assignment(*targets[1]).sequence == "001"
        assert outcome.roster.resolve_assignment(*targets[1]).driver == "기사"
        assert outcome.roster.resolve_assignment(roster.persons[2].person_id, Meal.DINNER).sequence == "10"
        assert controller.save_value(targets, "001").status is SequenceSaveStatus.UNCHANGED
        assert len(appends) == 1
        def fail(record):
            raise OSError("저장소 오류")
        session.append_revision = fail
        failed = controller.save_value(targets, "002")
        assert failed.status is SequenceSaveStatus.FAILED and "저장소 오류" in failed.message
        assert session.database.current_roster().revision_id == outcome.roster.revision_id
        def backup_warning(record):
            appends.append(record)
            original(record)
            return SimpleNamespace(error=OSError("백업 폴더 오류"))
        session.append_revision = backup_warning
        outcome = controller.save_value(targets, "003")
        assert outcome.status is SequenceSaveStatus.SAVED and outcome.backup_warning
        assert "저장됐지만" in outcome.message and len(appends) == 2
        assert outcome.roster.revision_id == session.database.current_roster().revision_id
    with DatabaseSession.open(tmp_path / "inline.sqlite3", "password") as session:
        assert session.database.current_roster().resolve_assignment(*targets[1]).sequence == "003"


def test_application_inline_commit_refreshes_preserves_filters_and_failure_display(app, settings, tmp_path, monkeypatch):
    import src.desktop_v2.application as application
    from src.desktop_v2.portable_settings import DesktopStorageSettings
    roster = roster_fixture()
    targets = ((roster.persons[0].person_id, Meal.DINNER), (roster.persons[1].person_id, Meal.BREAKFAST))
    # Exact merged fixture shares a household only in the UI. The persistence
    # contract uses explicit targets regardless of household membership.
    merged, dinner, breakfast = manifest_rows(roster)
    with DatabaseSession.create(tmp_path / "app-inline.sqlite3", "password", "recovery") as session:
        initialize(session, roster)
        window = RosterWindow(roster, column_settings=settings, inactive_settings=settings)
        window._set_day(date(2026, 10, 13))
        manifest = DriverHouseholdsDialog((merged,), service_day=date(2026, 10, 13), drivers=())
        manifest.show()
        manifest._set_column_search("members", "하나")
        manifest.table.sortItems(3, Qt.DescendingOrder)
        monkeypatch.setattr(application, "SequenceEditController", lambda current: SequenceEditController(
            current, actor_provider=lambda: RevisionActor("tester", "sid")))
        startup = application.DesktopStartupController(settings=DesktopStorageSettings(settings))
        manifest.sequence_save_requested.connect(lambda pairs, value: startup._save_household_sequence(session, window, pairs, value, manifest))
        append = session.append_revision
        appends = []
        def failing(record):
            raise OSError("잠시 실패")
        session.append_revision = failing
        editor = start_editor(manifest, app)
        editor.setText("005")
        QTest.keyClick(editor, Qt.Key_Return)
        app.processEvents()
        assert manifest.table.item(0, 3).text() == "001, 2"
        assert "잠시 실패" in manifest.sequence_status.text()
        assert window.roster.revision_id == roster.revision_id
        def warning(record):
            appends.append(record)
            append(record)
            return SimpleNamespace(error=OSError("백업 실패"))
        session.append_revision = warning
        editor = start_editor(manifest, app)
        editor.setText("005")
        QTest.keyClick(editor, Qt.Key_Return)
        app.processEvents()
        assert len(appends) == 1 and len(appends[0].changes) == 2
        assert all(window.roster.resolve_assignment(*target).sequence == "005" for target in targets)
        assert manifest._column_searches["members"] == "하나"
        assert manifest.table.horizontalHeader().sortIndicatorOrder() == Qt.DescendingOrder
        assert manifest.table.item(0, 3).text() == "005"
        assert "저장됐지만" in manifest.sequence_status.text()
        manifest.close()
        window.close()


@pytest.mark.parametrize("second_sequence", (None, "001"))
def test_real_projection_prefills_only_uniform_values_including_missing(app, tmp_path, second_sequence):
    from src.domain.roster import (
        Household,
        HouseholdMembership,
    )
    from src.domain.delivery_routing import RoutingState
    from src.services.driver_households import build_driver_household_rows

    first, second = (Person(uuid4(), PersonProfile(name)) for name in ("첫째", "둘째"))
    household = Household(uuid4())
    roster = RosterSnapshot(
        uuid4(), persons=(first, second), households=(household,),
        memberships=tuple(HouseholdMembership(person.person_id, household.household_id) for person in (first, second)),
        enrollments=tuple(MealEnrollment(person.person_id, Meal.DINNER) for person in (first, second)),
        assignments=(DeliveryAssignment(first.person_id, Meal.DINNER, "기사", "001"),
                     DeliveryAssignment(second.person_id, Meal.DINNER, "기사", second_sequence)),
    )
    rows = build_driver_household_rows(roster, RoutingState(()), date(2026, 10, 13))
    assert len(rows) == 1 and rows[0].sequences == ("001",)
    assert rows[0].has_empty_sequence is (second_sequence is None)
    assert rows[0].meal_rows[0].has_empty_sequence is (second_sequence is None)
    with DatabaseSession.create(tmp_path / "missing-prefill.sqlite3", "password", "recovery") as session:
        initialize(session, roster)
        controller = SequenceEditController(session, actor_provider=lambda: RevisionActor("tester", "sid"))
        dialog = DriverHouseholdsDialog(rows, service_day=date(2026, 10, 13), drivers=())
        dialog.show()
        results = []
        def save(targets, value):
            result = controller.save_value(targets, value)
            results.append(result)
            dialog.show_sequence_result(result)
        dialog.sequence_save_requested.connect(save)
        editor = start_editor(dialog, app)
        assert editor.text() == ("" if second_sequence is None else "001")
        assert len(editor._sequence_targets) == 2
        assert "이 행의 가구원 식종에 같은 순서 적용" in dialog.sequence_status.text()
        QTest.keyClick(editor, Qt.Key_Return)
        assert results[-1].status is (SequenceSaveStatus.FAILED if second_sequence is None else SequenceSaveStatus.UNCHANGED)
        assert session.database.current_roster().revision_id == roster.revision_id
        assert session.database.current_roster().resolve_assignment(second.person_id, Meal.DINNER).sequence == second_sequence
        dialog.split_button.click()
        editor = start_editor(dialog, app)
        assert editor.text() == ("" if second_sequence is None else "001")
        QTest.keyClick(editor, Qt.Key_Escape)
        dialog.close()
