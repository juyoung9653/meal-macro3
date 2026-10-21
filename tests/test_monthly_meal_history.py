"""Monthly actions use typed applicable dates and the latest immutable state."""

import os
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from src.domain.meal_history_cancellation import CANCELLATION_KIND, MealHistoryTarget, build_meal_history_cancellation
from src.domain.projections import (
    ENROLLMENT_KIND, STATUS_EVENT_KIND, roster_audit_changes, roster_entity_states,
    status_event_audit_change, status_event_state,
)
from src.domain.revisions import (
    AuditChange, ChangeOperation, EntityRef, HistoricalSnapshot, ImportBatch,
    RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource,
)
from src.domain.roster import InclusiveDateRange, Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot
from src.domain.status import MealStatus, StatusEvent
from src.services.monthly_meal_history import build_monthly_meal_history


ACTOR = RevisionActor("합성 담당자", "synthetic-sid")
STAMP = datetime(2026, 9, 1, tzinfo=UTC)


def _initial(*, ui_add=False, ends_on=None, people=1):
    revision = uuid4()
    persons = tuple(Person(uuid4(), PersonProfile("합성 대상", birth_date=date(2010 + index, 1, 1),
                                               school_name=f"합성학교{index + 1}")) for index in range(people))
    roster = RosterSnapshot(revision, persons=persons, enrollments=tuple(
        MealEnrollment(person.person_id, Meal.DINNER, InclusiveDateRange(date(2026, 9, 1), ends_on))
        for person in persons
    ))
    decisions = {"roster_row_change": {"operation": "add", "person_id": str(persons[0].person_id)}} if ui_add else {}
    record = RevisionRecord(RevisionMetadata(
        revision, None, STAMP, None, ACTOR, "합성 명단", RevisionSource.UI if ui_add else RevisionSource.IMPORT,
        decisions=decisions,
    ), roster_audit_changes(None, roster))
    return roster, [record], dict(roster_entity_states(roster))


def _event(roster, records, states, status, start, end=None, *, person=0, decisions=None, source=RevisionSource.UI):
    revision = uuid4()
    event = StatusEvent(uuid4(), revision, roster.persons[person].person_id, Meal.DINNER, status,
                        InclusiveDateRange(start, end), STAMP + timedelta(minutes=len(records)),
                        "원문 날짜는 해석하지 않습니다: 1999-01-01")
    record = RevisionRecord(RevisionMetadata(
        revision, records[-1].metadata.revision_id, event.recorded_at, date(2026, 9, 1), ACTOR,
        "합성 상태", source, decisions=decisions or {},
    ), (status_event_audit_change(event),))
    records.append(record)
    states[EntityRef(STATUS_EVENT_KIND, event.event_id)] = status_event_state(event)
    return event


def _result(records, states, year, month):
    return build_monthly_meal_history(HistoricalSnapshot(records[-1].metadata.revision_id, states), tuple(records), year, month)


@pytest.mark.parametrize("start,end,expected", [
    (date(2026, 10, 29), date(2026, 11, 3), ((2026, 10), (2026, 11))),
    (date(2026, 12, 31), date(2027, 1, 1), ((2026, 12), (2027, 1))),
    (date(2026, 10, 31), date(2026, 10, 31), ((2026, 10),)),
    (date(2026, 11, 1), date(2026, 11, 1), ((2026, 11),)),
])
def test_inclusive_month_overlap_keeps_full_period(start, end, expected):
    roster, records, states = _initial()
    _event(roster, records, states, MealStatus.PAUSED, start, end)
    for year, month in ((2026, 9), (2026, 10), (2026, 11), (2026, 12), (2027, 1), (2027, 2)):
        rows = _result(records, states, year, month).entries
        assert bool(rows) is ((year, month) in expected)
        if rows:
            assert rows[0].period == InclusiveDateRange(start, end)
            assert rows[0].status is MealStatus.PAUSED


@pytest.mark.parametrize("status", (MealStatus.DELIVERY, MealStatus.STOPPED))
@pytest.mark.parametrize("start,next_month", [
    (date(2026, 9, 30), date(2026, 10, 1)),
    (date(2026, 12, 31), date(2027, 1, 1)),
])
def test_open_actions_belong_only_to_original_start_month(status, start, next_month):
    roster, records, states = _initial()
    _event(roster, records, states, status, start)
    rows = _result(records, states, start.year, start.month).entries
    assert len(rows) == 1 and rows[0].period == InclusiveDateRange(start, None)
    assert _result(records, states, next_month.year, next_month.month).entries == ()


def test_open_enrollment_without_start_does_not_use_recording_date():
    roster, records, _ = _initial(ui_add=True)
    roster = replace(roster, enrollments=tuple(
        replace(item, period=InclusiveDateRange()) for item in roster.enrollments
    ))
    records[0] = replace(records[0], changes=roster_audit_changes(None, roster))
    states = dict(roster_entity_states(roster))
    assert _result(records, states, 2026, 9).entries == ()
    assert _result(records, states, 2026, 10).entries == ()


def test_superseded_open_stop_does_not_leak_or_change_raw_label():
    roster, records, states = _initial()
    _event(roster, records, states, MealStatus.STOPPED, date(2026, 10, 1))
    _event(roster, records, states, MealStatus.DELIVERY, date(2026, 11, 1))
    october = _result(records, states, 2026, 10).entries
    assert [(row.status, row.period) for row in october] == [
        (MealStatus.STOPPED, InclusiveDateRange(date(2026, 10, 1), date(2026, 10, 31))),
    ]
    november = _result(records, states, 2026, 11).entries
    assert len(november) == 1 and november[0].status is MealStatus.DELIVERY
    assert november[0].period == InclusiveDateRange(date(2026, 11, 1), None)


def test_finite_override_splits_original_action_and_enrollment_bounds_it():
    roster, records, states = _initial(ends_on=date(2026, 12, 15))
    _event(roster, records, states, MealStatus.STOPPED, date(2026, 10, 1))
    _event(roster, records, states, MealStatus.DELIVERY, date(2026, 10, 10), date(2026, 11, 3))
    november = _result(records, states, 2026, 11).entries
    assert {(row.status, row.period) for row in november} == {
        (MealStatus.DELIVERY, InclusiveDateRange(date(2026, 10, 10), date(2026, 11, 3))),
    }
    assert _result(records, states, 2027, 1).entries == ()


def test_ui_enrollment_uses_real_start_and_disambiguates_people():
    roster, records, states = _initial(ui_add=True, people=2)
    _event(roster, records, states, MealStatus.PAUSED, date(2026, 10, 29), date(2026, 11, 3), person=0)
    _event(roster, records, states, MealStatus.PAUSED, date(2026, 10, 29), date(2026, 11, 3), person=1)
    assert _result(records, states, 2026, 8).entries == ()
    september = _result(records, states, 2026, 9).entries
    assert len(september) == 1 and september[0].status is MealStatus.DELIVERY
    assert september[0].period == InclusiveDateRange(date(2026, 9, 1), date(2026, 10, 28))
    november = _result(records, states, 2026, 11).entries
    assert all(row.status is MealStatus.PAUSED for row in november)
    assert all(row.status is MealStatus.PAUSED for row in _result(records, states, 2026, 10).entries)
    paused = [row for row in november if row.status is MealStatus.PAUSED]
    assert len(paused) == 2 and len({row.person_id for row in paused}) == 2
    assert len({row.birth_date for row in paused}) == 2
    assert len({row.school_name for row in paused}) == 2


def test_deleted_cancelled_and_restore_synthetic_actions_are_excluded():
    roster, records, states = _initial()
    deleted = _event(roster, records, states, MealStatus.PAUSED, date(2026, 10, 1), date(2026, 10, 2))
    states.pop(EntityRef(STATUS_EVENT_KIND, deleted.event_id))
    cancelled = _event(roster, records, states, MealStatus.PAUSED, date(2026, 10, 3), date(2026, 10, 4))
    target = MealHistoryTarget(records[-1].metadata.revision_id, STATUS_EVENT_KIND, cancelled.event_id)
    marker_id = uuid4()
    states[EntityRef(CANCELLATION_KIND, marker_id)] = target.state()
    metadata = RevisionMetadata(uuid4(), records[-1].metadata.revision_id, STAMP + timedelta(hours=1), None,
                                ACTOR, "합성 취소", RevisionSource.UI, decisions={"meal_history_cancellation": {}})
    records.append(RevisionRecord(metadata, (AuditChange(CANCELLATION_KIND, marker_id, ChangeOperation.INSERT, None, target.state()),)))
    restored = _event(roster, records, states, MealStatus.PAUSED, date(2026, 10, 5), date(2026, 10, 6),
                      decisions={"snapshot_restore_target": "synthetic"})
    assert _result(records, states, 2026, 10).entries == ()
    # Restoring cancellation markers changes visibility, without a synthetic restore action.
    metadata = replace(metadata, revision_id=uuid4(), parent_revision_id=records[-1].metadata.revision_id,
                       recorded_at=STAMP + timedelta(hours=2), decisions={"snapshot_restore_target": "synthetic"})
    records.append(RevisionRecord(metadata, (AuditChange(CANCELLATION_KIND, marker_id, ChangeOperation.DELETE, target.state(), None),)))
    states.pop(EntityRef(CANCELLATION_KIND, marker_id))
    rows = _result(records, states, 2026, 10).entries
    assert len(rows) == 1 and rows[0].period == cancelled.period


def test_roster_import_status_is_hidden_but_still_supersedes_prior_action():
    roster, records, states = _initial()
    _event(roster, records, states, MealStatus.STOPPED, date(2026, 10, 1))
    _event(roster, records, states, MealStatus.DELIVERY, date(2026, 11, 1), source=RevisionSource.IMPORT,
           decisions={"import_purpose": "database"})
    record = records[-1]
    batch = ImportBatch(uuid4(), record.metadata.revision_id, "synthetic.xlsx", "a" * 64, 1,
                        "명단", "synthetic", {}, date(2026, 11, 1))
    records[-1] = replace(record, import_batch=batch)
    assert _result(records, states, 2026, 11).entries == ()
    assert _result(records, states, 2026, 10).entries[0].period.ends_on == date(2026, 10, 31)


def test_invalid_month_and_mismatched_revision_are_observable():
    roster, records, states = _initial()
    with pytest.raises(ValueError):
        _result(records, states, 2026, 13)
    with pytest.raises(ValueError, match="변경번호"):
        build_monthly_meal_history(HistoricalSnapshot(uuid4(), states), tuple(records), 2026, 10)


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _filter_entries():
    from src.services.monthly_meal_history import MonthlyMealHistoryEntry
    return tuple(MonthlyMealHistoryEntry(
        uuid4(), name, None, None, meal, status,
        InclusiveDateRange(date(2026, 10, day), date(2026, 11, 3)), actor, code,
    ) for name, meal, status, day, actor, code in (
        ("김하늘", Meal.DINNER, MealStatus.PAUSED, 1, "직원A", "20261001-001"),
        ("이수민", Meal.BREAKFAST, MealStatus.DELIVERY, 2, "직원B", "20261002-002"),
        ("김바다", Meal.LUNCH, MealStatus.STOPPED, 3, "직원C", "20261003-003"),
    ))


def _visible_names(dialog):
    return tuple(dialog.table.item(row, 0).text() for row in range(dialog.table.rowCount())
                 if not dialog.table.isRowHidden(row))


@pytest.mark.parametrize("key,query", (
    ("name", "이수민"), ("meal", "조식"), ("status", "배송"),
    ("period", "2026-10-02"), ("actor", "직원b"), ("revision", "002"),
))
def test_header_filter_popup_search_apply_cancel_and_reset(app, key, query):
    from src.desktop_v2.column_search import ColumnValuePicker
    from src.desktop_v2.monthly_meal_history import MonthlyMealHistoryDialog
    from src.services.monthly_meal_history import MonthlyMealHistory

    entries = _filter_entries()
    dialog = MonthlyMealHistoryDialog(date(2026, 10, 1),
        lambda year, month: MonthlyMealHistory(uuid4(), year, month, entries))
    dialog.show()
    app.processEvents()
    header = dialog._column_search_header
    assert len(header.buttons) == 6
    header.buttons[key].click()
    picker = header._menu.findChild(ColumnValuePicker)
    picker.search.setText(query)
    picker.cancel.click()
    assert len(_visible_names(dialog)) == 3
    assert not header.buttons[key].property("filterActive")
    header.buttons[key].click()
    picker = header._menu.findChild(ColumnValuePicker)
    picker.search.setText(query)
    picker.apply.click()
    assert _visible_names(dialog) == ("이수민",)
    assert "조회 1건 / 전체 3건" in dialog.summary.text()
    assert header.buttons[key].property("filterActive")
    header.buttons[key].click()
    header._menu.findChild(ColumnValuePicker).reset.click()
    assert len(_visible_names(dialog)) == 3
    assert not dialog.clear_filters_button.isEnabled()
    dialog.close()


def test_header_values_combine_preserve_on_refresh_and_empty_selection(app):
    from PySide6.QtCore import Qt
    from src.desktop_v2.column_search import ColumnValuePicker
    from src.desktop_v2.monthly_meal_history import MonthlyMealHistoryDialog
    from src.services.monthly_meal_history import MonthlyMealHistory

    entries, calls = _filter_entries(), []
    def load(year, month):
        calls.append((year, month))
        return MonthlyMealHistory(uuid4(), year, month, entries if month == 10 else entries[1:])
    dialog = MonthlyMealHistoryDialog(date(2026, 10, 1), load)
    dialog.show()
    app.processEvents()
    header = dialog._column_search_header
    header.search_changed.emit("name", "김")
    header.buttons["status"].click()
    picker = header._menu.findChild(ColumnValuePicker)
    assert picker.list.count() == 3  # All monthly values remain available.
    picker.select_none.click()
    for index in range(picker.list.count()):
        item = picker.list.item(index)
        if item.data(Qt.ItemDataRole.UserRole) == "일시중지":
            item.setCheckState(Qt.CheckState.Checked)
    picker.apply.click()
    assert _visible_names(dialog) == ("김하늘",)
    assert calls == [(2026, 10)]  # Filtering never reloads or writes data.
    dialog.refresh()
    assert _visible_names(dialog) == ("김하늘",)
    dialog._shift_month(1)
    assert not _visible_names(dialog)
    assert "조회 0건 / 전체 2건" in dialog.summary.text()
    assert dialog._column_values["status"] == frozenset({"일시중지"})
    dialog._shift_month(-1)
    assert _visible_names(dialog) == ("김하늘",)
    dialog.clear_filters_button.click()
    assert len(_visible_names(dialog)) == 3
    header.buttons["meal"].click()
    picker = header._menu.findChild(ColumnValuePicker)
    picker.select_none.click()
    picker.apply.click()
    assert not _visible_names(dialog)
    assert header.buttons["meal"].property("filterActive")
    dialog.refresh()
    assert not _visible_names(dialog)
    dialog.clear_filters_button.click()
    assert len(_visible_names(dialog)) == 3
    dialog.close()


def test_dialog_month_change_empty_and_error_clear_rows(app):
    from PySide6.QtWidgets import QAbstractItemView
    from src.desktop_v2.monthly_meal_history import MonthlyMealHistoryDialog

    roster, records, states = _initial()
    _event(roster, records, states, MealStatus.PAUSED, date(2026, 12, 31), date(2027, 1, 1))
    calls = []
    broken = False

    def load(year, month):
        calls.append((year, month))
        if broken:
            raise ValueError("합성 읽기 실패")
        return _result(records, states, year, month)

    dialog = MonthlyMealHistoryDialog(date(2026, 12, 1), load)
    assert dialog.table.rowCount() == 1
    assert dialog.table.columnCount() == 6
    assert tuple(dialog.table.horizontalHeaderItem(index).text() for index in range(6)) == (
        "성명", "식종", "구분", "적용 기간", "변경자", "변경번호",
    )
    assert dialog.table.item(0, 3).text() == "2026-12-31 ~ 2027-01-01"
    assert dialog.table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    dialog._shift_month(1)
    assert calls == [(2026, 12), (2027, 1)] and dialog.table.rowCount() == 1
    dialog._shift_month(1)
    assert dialog.table.rowCount() == 0 and "없습니다" in dialog.summary.text()
    dialog._shift_month(-1)
    broken = True
    dialog.refresh()
    assert dialog.table.rowCount() == 0 and not dialog.summary.text()
    assert "합성 읽기 실패" in dialog.error.text() and not dialog.error.isHidden()
    broken = False
    dialog.refresh()
    assert dialog.table.rowCount() == 1 and dialog.error.isHidden()
    dialog.close()


def test_application_menu_opens_real_dialog_refreshes_latest_without_writes(app, tmp_path, monkeypatch):
    import src.desktop_v2.application as application
    from src.desktop_v2.monthly_meal_history import MonthlyMealHistoryDialog
    from src.storage.database_session import DatabaseSession

    roster, records, states = _initial()
    _event(roster, records, states, MealStatus.PAUSED, date(2026, 10, 29), date(2026, 11, 3))
    with DatabaseSession.create(tmp_path / "monthly.sqlite3", "password", "recovery") as session:
        for record in records:
            session.append_revision(record)
        access = SimpleNamespace(exec=lambda: application.QDialog.DialogCode.Accepted, session=session)
        controller = application.DesktopStartupController(startup_task_runner=lambda work, **kwargs: work())
        outcome = controller._start_with_dialog(access, None)
        assert outcome.ready
        window = outcome.window
        window._set_day(date(2026, 10, 1))
        window._search_input.setText("해당 대상 없음")
        assert window.table_model.rowCount() == 0
        progress_calls = []

        def progress(work, **kwargs):
            progress_calls.append(kwargs)
            return work()

        monkeypatch.setattr(application, "run_progress_task", progress)
        window._monthly_meal_history_action.trigger()
        app.processEvents()
        dialogs = window.findChildren(MonthlyMealHistoryDialog)
        assert len(dialogs) == 1 and dialogs[0].isVisible() and not dialogs[0].isModal()
        dialog = dialogs[0]
        assert dialog.table.rowCount() == 1 and len(progress_calls) == 1
        # A later cancellation removes the actual event and marks the original action.
        event = records[-1].changes[0]
        revision = uuid4()
        target = MealHistoryTarget(records[-1].metadata.revision_id, STATUS_EVENT_KIND, event.entity_id)
        _, cancelled = build_meal_history_cancellation(
            session.database.snapshot(records[-1].metadata.revision_id), tuple(records), target,
            actor=ACTOR, recorded_at=STAMP + timedelta(hours=1), revision_id=revision,
        )
        session.append_revision(cancelled)
        def unexpected_write(*args, **kwargs):
            pytest.fail("조회 중 저장 또는 백업을 실행했습니다")
        monkeypatch.setattr(session, "append_revision", unexpected_write)
        monkeypatch.setattr(session, "try_backup_now", unexpected_write)
        dialog.refresh()
        assert dialog.table.rowCount() == 0
        assert session.database.latest_revision().metadata.revision_id == revision
        assert len(progress_calls) == 2
        dialog.close()
        window.close()
