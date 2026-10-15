"""Shared column editing uses explicit drafts and ordinary native clipboard text."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QAbstractItemView, QAbstractSlider, QDialog, QMenu, QPlainTextEdit

from src.desktop_v2.household_shared_columns import HouseholdSharedColumnController, HouseholdSharedColumnDialog
from src.desktop_v2.roster_window import RosterWindow
from src.domain.household_shared import SharedComponent, SharedHouseholdRow, HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID, shared_columns_from_state
from src.domain.projections import roster_audit_changes
from src.domain.revisions import RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import CustomFieldDefinition, CustomFieldType, Household, HouseholdMembership, Meal, Person, PersonProfile, RosterSnapshot
from src.storage.database_session import DatabaseSession

DAY = date(2026, 10, 15)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def make_dialog(enabled=True, values=(None,)):
    return HouseholdSharedColumnDialog(
        "비고", enabled, (SharedComponent("note", "비고"),),
        (SharedHouseholdRow(uuid4(), ("가상 가구원 1", "가상 가구원 2"), values, (("기존 A",), ("기존 B",))),), DAY,
    )


def test_conflict_requires_explicit_decision_and_clear_is_valid(app):
    dialog = make_dialog()
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert "공용 값을 결정" in dialog.error.text()
    dialog.table.setCurrentCell(0, 2)
    dialog.table.clear_cells()
    assert list(dialog.selected_values().values()) == [("",)]
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_ordinary_clipboard_keeps_multiline_text_in_one_value(app):
    dialog = make_dialog()
    dialog.table.setCurrentCell(0, 2)
    original = '001\n둘째 줄\t문자 그대로'
    QApplication.clipboard().setText(original)
    QTest.keyClick(dialog.table, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert list(dialog.selected_values().values()) == [(original,)]
    QTest.keyClick(dialog.table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == original
    dialog.reject()


def test_native_editor_commits_on_save_and_invalid_draft_stays_open(app):
    dialog = make_dialog(values=("현재 값",))
    dialog.show()
    dialog.table.setCurrentCell(0, 2)
    dialog.table.editItem(dialog.table.item(0, 2))
    app.processEvents()
    editor = dialog.table.findChild(QPlainTextEdit)
    assert editor is not None
    editor.setPlainText("직접 수정\n다음 줄")
    app.processEvents()
    dialog.accept()
    assert list(dialog.selected_values().values()) == [("직접 수정\n다음 줄",)]
    assert dialog.result() == QDialog.DialogCode.Accepted
    dialog.close()

    invalid = make_dialog(values=("2026-10-15",))
    invalid.validation_callback = lambda *_: (_ for _ in ()).throw(ValueError("날짜를 확인해주세요"))
    invalid.table.item(0, 2).setText("잘못된 날짜")
    invalid.accept()
    assert invalid.result() == QDialog.DialogCode.Rejected
    assert invalid.error.text() == "날짜를 확인해주세요"
    assert invalid.table.item(0, 2).text() == "잘못된 날짜"
    invalid.close()


def test_disabled_draft_preserves_values_and_cancel_does_not_resolve_conflict(app):
    dialog = make_dialog(enabled=False)
    assert not dialog.enabled.isChecked()
    assert dialog.table.isEnabled()
    assert not dialog.table.values_editable
    assert not dialog.table.item(0, 2).flags() & Qt.ItemFlag.ItemIsEditable
    assert dialog.selected_values() == {}
    dialog.enabled.setChecked(True)
    with pytest.raises(ValueError):
        dialog.selected_values()
    dialog.reject()
    assert dialog.result() == QDialog.DialogCode.Rejected


def roster_fixture():
    first, second, household = uuid4(), uuid4(), uuid4()
    return RosterSnapshot(
        uuid4(), persons=(Person(first, PersonProfile("가상 첫째", note="A")), Person(second, PersonProfile("가상 둘째", note="B"))),
        households=(Household(household),), memberships=(HouseholdMembership(first, household), HouseholdMembership(second, household)),
    )


def test_menu_has_builtin_custom_entrypoints_and_marker_preserves_export_name(app, tmp_path):
    from dataclasses import replace
    roster = replace(roster_fixture(), custom_fields=(CustomFieldDefinition(uuid4(), "사용자 항목", CustomFieldType.TEXT, 0),))
    window = RosterWindow(roster, column_settings=QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
    captured = []
    window.household_shared_column_requested.connect(captured.append)
    menu = QMenu()
    window._add_household_shared_column_actions(menu)
    entries = menu.actions()[0].menu().actions()
    assert {"성명", "생년월일", "비고", "배송 설정", "사용자 항목"} <= {a.text() for a in entries}
    next(a for a in entries if a.text() == "성명").trigger()
    assert captured == ["name"]
    index = next(i for i, c in enumerate(window.table_model.columns) if c.key == "note")
    window.set_household_shared_columns(frozenset({"note"}))
    assert window.table_model.headerData(index, Qt.Orientation.Horizontal) == "비고"
    assert window.table_model.headerData(index, Qt.Orientation.Horizontal, Qt.ItemDataRole.DecorationRole) is not None
    assert "가구 공용" in window.table_model.headerData(index, Qt.Orientation.Horizontal, Qt.ItemDataRole.ToolTipRole)
    window.close()


def test_controller_cancel_enable_disable_and_persistence(app, tmp_path):
    roster = roster_fixture()
    actor = RevisionActor("tester", "test-sid")
    initial = RevisionRecord(RevisionMetadata(roster.revision_id, None, datetime(2026, 10, 15, tzinfo=UTC), None, actor, "초기", RevisionSource.IMPORT), roster_audit_changes(None, roster))
    path = tmp_path / "shared.sqlite3"

    class Dialog:
        result = QDialog.DialogCode.Rejected
        selected_enabled = True
        text = "공용 비고\n둘째 줄\t001"

        def __init__(self, label, enabled, components, rows, day, parent):
            self.rows = rows
            self.enabled = SimpleNamespace(isChecked=lambda: self.selected_enabled)

        def exec(self):
            return self.result

        def selected_values(self):
            return {r.household_id: (self.text,) for r in self.rows} if self.selected_enabled else {}

    class Messages:
        @staticmethod
        def critical(*args):
            pytest.fail(str(args))

        @staticmethod
        def warning(*args):
            pytest.fail(str(args))

    with DatabaseSession.create(path, "password", "recovery") as session:
        session.append_revision(initial)
        controller = HouseholdSharedColumnController(session, dialog_type=Dialog, message_box=Messages, actor_provider=lambda: actor)
        assert controller.run("note", "비고", DAY).roster is None
        assert session.database.current_roster().revision_id == roster.revision_id
        Dialog.result = QDialog.DialogCode.Accepted
        saved = controller.run("note", "비고", DAY).roster
        assert saved is not None
        assert {p.profile.note for p in saved.persons} == {"공용 비고\n둘째 줄\t001"}
    with DatabaseSession.open(path, "password") as session:
        current = session.database.current_roster()
        stored = session.database.snapshot(current.revision_id).entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
        assert shared_columns_from_state(stored) == frozenset({"note"})
        result = HouseholdSharedColumnController(session, dialog_type=Dialog, message_box=Messages, actor_provider=lambda: actor).run("note", "비고", DAY)
        assert {p.profile.note for p in result.roster.persons} == {"공용 비고\n둘째 줄\t001"}
        state = session.database.snapshot(result.roster.revision_id).entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
        assert shared_columns_from_state(state) == frozenset()


def test_controller_rejects_stale_snapshot_without_writing(app):
    from src.domain.revisions import HistoricalSnapshot
    roster = roster_fixture()
    current = [roster]
    written, errors = [], []

    class Dialog:
        def __init__(self, label, enabled, components, rows, day, parent):
            self.rows = rows
            self.enabled = SimpleNamespace(isChecked=lambda: True)

        def exec(self):
            from dataclasses import replace
            current[0] = replace(roster, revision_id=uuid4())
            return QDialog.DialogCode.Accepted

        def selected_values(self):
            return {r.household_id: ("새 값",) for r in self.rows}

    session = SimpleNamespace(database=SimpleNamespace(
        current_roster=lambda: current[0], snapshot=lambda _: HistoricalSnapshot(roster.revision_id, {})),
        append_revision=written.append)
    messages = SimpleNamespace(critical=lambda *args: errors.append(args))
    result = HouseholdSharedColumnController(session, dialog_type=Dialog, message_box=messages).run("note", "비고", DAY)
    assert result.roster is None
    assert written == []
    assert "명단이 변경" in str(errors)


def test_controller_uses_committed_cache_without_fallible_postsave_read(app):
    from src.domain.revisions import EntityRef, HistoricalSnapshot
    roster = roster_fixture()
    snapshot = HistoricalSnapshot(roster.revision_id, {
        EntityRef(c.entity_kind, c.entity_id): c.after for c in roster_audit_changes(None, roster)
    })
    read_count = []
    committed = object()

    def current_roster():
        read_count.append(True)
        if len(read_count) > 2:
            raise OSError("저장 뒤 조회 실패")
        return roster

    class Dialog:
        def __init__(self, label, enabled, components, rows, day, parent):
            self.rows = rows
            self.enabled = SimpleNamespace(isChecked=lambda: True)

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selected_values(self):
            return {r.household_id: ("공용",) for r in self.rows}

    session = SimpleNamespace(database=SimpleNamespace(current_roster=current_roster, snapshot=lambda _: snapshot))

    def append(record):
        session.last_committed_roster = committed
        return SimpleNamespace(error=None)

    session.append_revision = append
    messages = SimpleNamespace(critical=lambda *args: pytest.fail(str(args)))
    result = HouseholdSharedColumnController(session, dialog_type=Dialog, message_box=messages,
        actor_provider=lambda: RevisionActor("tester", "test")).run("note", "비고", DAY)
    assert result.roster is committed
    assert len(read_count) == 2


def test_normal_roster_refresh_updates_shared_routing_display_and_search(app, tmp_path, monkeypatch):
    from dataclasses import replace
    from src.desktop_v2.application import DesktopStartupController
    from src.domain.delivery_routing import ROUTING_EVENT_KIND, RoutingEvent, _routing_event_state
    from src.domain.projections import roster_entity_states
    from src.domain.revisions import EntityRef, HistoricalSnapshot
    from src.domain.roster import Meal, MealEnrollment

    before = roster_fixture()
    before = replace(before, enrollments=tuple(MealEnrollment(p.person_id, Meal.DINNER) for p in before.persons))
    saved = replace(before, revision_id=uuid4())
    states = dict(roster_entity_states(saved))
    for index, person in enumerate(saved.persons, 1):
        event = RoutingEvent(uuid4(), person.person_id, DAY, index, {}, "새 공용 주소", "101호")
        states[EntityRef(ROUTING_EVENT_KIND, event.event_id)] = _routing_event_state(event)
    snapshot = HistoricalSnapshot(saved.revision_id, states)
    session = SimpleNamespace(database=SimpleNamespace(snapshot=lambda _: snapshot))
    controller = DesktopStartupController()
    monkeypatch.setattr(controller, "_histories", lambda *_: {})
    monkeypatch.setattr(controller, "_revision_display_code", lambda *_: None)
    monkeypatch.setattr(controller, "_refresh_school_calendar", lambda *_: None)
    window = RosterWindow(before, column_settings=QSettings(str(tmp_path / "refresh.ini"), QSettings.Format.IniFormat))
    window.table_model.set_context(DAY, Meal.DINNER)
    window.table_model.set_search("새 공용 주소")
    assert window.table_model.rowCount() == 0

    controller._replace_roster(session, window, saved)

    assert window.table_model.rowCount() == 2
    column = next(i for i, value in enumerate(window.table_model.columns) if value.key == "address")
    assert "새 공용 주소" in window.table_model.data(window.table_model.index(0, column))
    window.close()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("giant_row", [False, True])
def test_shared_review_pixel_scrolls_with_wheel_and_bars_even_when_not_editable(app, enabled, giant_row):
    long_value = "\n".join(f"중간 내용을 확인할 줄 {i}" for i in range(35)) if giant_row else "기존 값\n다음 줄"
    rows = tuple(SharedHouseholdRow(uuid4(), (f"가상 첫째 {i}", f"가상 둘째 {i}"), (None,), ((long_value,), ("서로 다른 값",)))
        for i in range(1 if giant_row else 12))
    dialog = HouseholdSharedColumnDialog("비고", enabled, (SharedComponent("note", "비고"),), rows, DAY)
    dialog.resize(720, 440)
    dialog.show()
    app.processEvents()
    table = dialog.table
    assert table.isEnabled()
    assert table.verticalScrollMode() == QAbstractItemView.ScrollMode.ScrollPerPixel
    assert table.horizontalScrollMode() == QAbstractItemView.ScrollMode.ScrollPerPixel
    vertical, horizontal = table.verticalScrollBar(), table.horizontalScrollBar()
    assert vertical.isEnabled() and vertical.maximum() > 0
    assert horizontal.isEnabled() and horizontal.maximum() > 0
    center = table.viewport().rect().center()
    event = QWheelEvent(QPointF(center), QPointF(table.viewport().mapToGlobal(center)), QPoint(), QPoint(0, -120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(table.viewport(), event)
    assert vertical.value() > 0
    vertical.setValue(vertical.maximum() // 2)
    app.processEvents()
    assert 0 < vertical.value() < vertical.maximum()
    if giant_row:
        assert table.visualRect(table.model().index(0, 1)).top() < 0
        assert table.visualRect(table.model().index(0, 1)).bottom() > table.viewport().height()
    horizontal.triggerAction(QAbstractSlider.SliderAction.SliderSingleStepAdd)
    assert horizontal.value() > 0
    if not enabled:
        table.setCurrentCell(0, 2)
        QApplication.clipboard().setText("저장하면 안 되는 값")
        table.paste_cells()
        table.clear_cells()
        assert table.item(0, 2).text() == ""
        assert not table.item(0, 2).data(Qt.ItemDataRole.UserRole)
    dialog.close()


def test_real_menu_toggles_equal_household_without_review_and_preserves_values(app, tmp_path):
    from dataclasses import replace
    from src.desktop_v2.application import DesktopStartupController
    roster = roster_fixture()
    roster = replace(roster, persons=tuple(replace(p, profile=replace(p.profile, note="이미 같은 값")) for p in roster.persons))
    actor = RevisionActor("tester", "sid")
    initial = RevisionRecord(RevisionMetadata(roster.revision_id, None, datetime(2026, 10, 15, tzinfo=UTC), None, actor, "초기", RevisionSource.IMPORT), roster_audit_changes(None, roster))

    def forbidden_dialog(*args):
        pytest.fail("값이 같은 가구의 설정 토글에서 확인창을 열었습니다")

    with DatabaseSession.create(tmp_path / "equal.sqlite3", "password") as session:
        session.append_revision(initial)
        application = DesktopStartupController(household_shared_column_controller_type=lambda s: HouseholdSharedColumnController(s, dialog_type=forbidden_dialog, actor_provider=lambda: actor))
        window = RosterWindow(roster, column_settings=QSettings(str(tmp_path / "equal.ini"), QSettings.Format.IniFormat))
        window.table_model.set_context(DAY, Meal.DINNER)
        window.household_shared_column_requested.connect(lambda key: application._run_household_shared_column(session, window, key))
        column = next(c for c in window.table_model.columns if c.key == "note")
        menu = QMenu(window)
        window._add_household_shared_column_actions(menu, column)
        action = menu.actions()[0]
        assert action.isCheckable() and not action.isChecked()
        action.trigger()
        saved = session.database.current_roster()
        assert action.isChecked()
        assert {p.profile.note for p in saved.persons} == {"이미 같은 값"}
        assert len(session.database.revision(saved.revision_id).changes) == 1
        action.trigger()
        disabled = session.database.current_roster()
        assert not action.isChecked()
        assert disabled.revision_id != saved.revision_id
        assert {p.profile.note for p in disabled.persons} == {"이미 같은 값"}
        assert shared_columns_from_state(session.database.snapshot(disabled.revision_id).entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)) == frozenset()
        window.close()


def test_real_menu_reviews_only_conflicts_cancel_then_resolve_preserves_equal_household(app, tmp_path):
    from dataclasses import replace
    from src.desktop_v2.application import DesktopStartupController
    roster = roster_fixture()
    other = Household(uuid4())
    equal_people = tuple(Person(uuid4(), PersonProfile(name, note="그대로 유지")) for name in ("가상 외부 1", "가상 외부 2"))
    roster = replace(roster, persons=(*roster.persons, *equal_people), households=(*roster.households, other),
        memberships=(*roster.memberships, *(HouseholdMembership(p.person_id, other.household_id) for p in equal_people)))
    actor = RevisionActor("tester", "sid")
    initial = RevisionRecord(RevisionMetadata(roster.revision_id, None, datetime(2026, 10, 15, tzinfo=UTC), None, actor, "초기", RevisionSource.IMPORT), roster_audit_changes(None, roster))
    opened = []

    class Review(HouseholdSharedColumnDialog):
        approve = False

        def exec(self):
            opened.append(self)
            assert self.enabled.isChecked()
            assert len(self._rows) == 1 and self._rows[0].household_id == roster.households[0].household_id
            if self.approve:
                self.table.setCurrentCell(0, 2)
                QApplication.clipboard().setText("직접 결정\n여러 줄 값")
                self.table.paste_cells()
                self.accept()
            return self.result()

    with DatabaseSession.create(tmp_path / "mixed.sqlite3", "password") as session:
        session.append_revision(initial)
        application = DesktopStartupController(household_shared_column_controller_type=lambda s: HouseholdSharedColumnController(s, dialog_type=Review, actor_provider=lambda: actor))
        window = RosterWindow(roster, column_settings=QSettings(str(tmp_path / "mixed.ini"), QSettings.Format.IniFormat))
        window.table_model.set_context(DAY, Meal.DINNER)
        window.household_shared_column_requested.connect(lambda key: application._run_household_shared_column(session, window, key))
        menu = QMenu(window)
        window._add_household_shared_column_actions(menu)
        action = next(a for a in menu._household_shared_submenu.actions() if a.text() == "비고")
        action.trigger()
        assert len(opened) == 1 and not action.isChecked()
        assert session.database.current_roster().revision_id == roster.revision_id
        Review.approve = True
        action.trigger()
        saved = session.database.current_roster()
        assert len(opened) == 2 and action.isChecked()
        assert {saved.person(p.person_id).profile.note for p in equal_people} == {"그대로 유지"}
        assert {saved.person(p.person_id).profile.note for p in roster.persons[:2]} == {"직접 결정\n여러 줄 값"}
        action.trigger()
        assert len(opened) == 2 and not action.isChecked()
        assert session.database.current_roster().persons == saved.persons
        window.close()
