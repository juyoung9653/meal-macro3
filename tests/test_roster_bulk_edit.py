"""Selected-cell edits preserve selection, sharing, validation and one commit."""
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.roster_bulk_edit import RosterBulkEditController, RosterBulkEditDialog, plan_bulk_edit
from src.desktop_v2.roster_window import RosterWindow
from src.domain.household_shared import HOUSEHOLD_SHARED_ID, HOUSEHOLD_SHARED_KIND
from src.domain.import_shared import import_candidate_snapshot
from src.domain.projections import roster_audit_changes, roster_entity_states, roster_from_historical_snapshot
from src.domain.revisions import AuditChange, ChangeOperation, EntityRef, HistoricalSnapshot, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import InclusiveDateRange, Meal, MealEnrollment
from src.storage.database_session import DatabaseSession
from tests.test_roster_cell_edit import _roster, PERSON_A, PERSON_B, NUMBER_FIELD, CHOICE_FIELD
from tests.test_household_shared import roster as family_roster, historical

DAY = date(2026, 10, 15)
STAMP = datetime(2026, 10, 15, tzinfo=UTC)
ACTOR = RevisionActor("tester", "bulk-cells")
LABELS = {"school": "학교", "name": "성명", "note": "비고", "mobile": "핸드폰", "address": "도로명주소",
          "birth_date": "생년월일", "lunch_starts_on": "중식 시작일", "lunch_ends_on": "중식 종료일",
          f"custom:{NUMBER_FIELD}": "지원 횟수", f"custom:{CHOICE_FIELD}": "구분"}


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def snapshot(roster=None):
    roster = roster or _roster()
    return HistoricalSnapshot(roster.revision_id, roster_entity_states(roster))


def plan(current, targets, **options):
    kwargs = dict(operation="replace", match_mode="exact", find="가초", replacement="가초등학교",
                  service_day=DAY, effective_on=DAY, actor=ACTOR, recorded_at=STAMP, revision_id=uuid4())
    kwargs.update(options)
    return plan_bulk_edit(current, targets, LABELS, **kwargs)


def result(current, prepared):
    return roster_from_historical_snapshot(import_candidate_snapshot(current, prepared.record))


def test_exact_partial_fill_and_selected_cells_only():
    base = _roster()
    base = replace(base, persons=(base.persons[0], replace(base.persons[1], profile=replace(base.persons[1].profile, school_name="가초 분교"))))
    current = snapshot(base)
    targets = ((PERSON_A, "school"), (PERSON_B, "school"))
    exact = plan(current, targets)
    assert exact.selected_changes == 1
    assert result(current, exact).person(PERSON_B).profile.school_name == "가초 분교"
    partial = plan(current, targets, match_mode="partial")
    assert partial.selected_changes == 2
    assert result(current, partial).person(PERSON_B).profile.school_name == "가초등학교 분교"
    one = plan(current, targets[:1], match_mode="partial")
    assert result(current, one).person(PERSON_B).profile.school_name == "가초 분교"
    filled = plan(current, targets, operation="fill", replacement="새 학교")
    assert all(p.profile.school_name == "새 학교" for p in result(current, filled).persons)
    assert current == snapshot(base)


def test_multicolumn_clear_multiline_blank_match_and_noop():
    current = snapshot()
    prepared = plan(current, ((PERSON_A, "note"), (PERSON_B, "note")), find="둘째", replacement="마지막", match_mode="partial")
    assert result(current, prepared).person(PERSON_A).profile.note == "첫 줄\n마지막 줄"
    assert result(current, prepared).person(PERSON_B).profile.note is None
    blank = plan(current, ((PERSON_A, "note"), (PERSON_B, "note")), find="", replacement="새 비고")
    assert blank.selected_changes == 1
    assert result(current, blank).person(PERSON_B).profile.note == "새 비고"
    cleared = plan(current, ((PERSON_A, "school"), (PERSON_A, "note")), operation="fill", replacement="")
    changed = result(current, cleared).person(PERSON_A)
    assert changed.profile.school_name is None and changed.profile.note is None
    assert changed.profile.mobile_phone == _roster().person(PERSON_A).profile.mobile_phone
    assert plan(current, ((PERSON_A, "school"),), find="없음").record is None
    with pytest.raises(ValueError, match="찾을 내용"):
        plan(current, ((PERSON_A, "school"),), find="", match_mode="partial")


@pytest.mark.parametrize("key,value", [("birth_date", "2026-02-30"), (f"custom:{NUMBER_FIELD}", "숫자 아님"),
                                       (f"custom:{CHOICE_FIELD}", "없는 선택값"), ("name", "")])
def test_invalid_values_reject_whole_plan(key, value):
    current = snapshot()
    with pytest.raises(ValueError):
        plan(current, ((PERSON_A, "note"), (PERSON_A, key)), operation="fill", replacement=value)
    assert roster_from_historical_snapshot(current) == _roster()


def test_numeric_zero_and_date_parsing():
    current = snapshot()
    prepared = plan(current, ((PERSON_A, f"custom:{NUMBER_FIELD}"),), operation="fill", replacement="0")
    assert result(current, prepared).person(PERSON_A).profile.custom_values[NUMBER_FIELD] == 0
    prepared = plan(current, ((PERSON_A, "birth_date"),), operation="fill", replacement="20160304")
    assert result(current, prepared).person(PERSON_A).profile.birth_date == date(2016, 3, 4)


def _family(keys=("note",), exceptions=()):
    roster = family_roster()
    roster = replace(roster, persons=tuple(replace(p, profile=replace(p.profile,
        mobile_phone="010-1234-5678" if i < 2 else "010-9876-5432")) for i, p in enumerate(roster.persons)))
    current = historical(roster, keys)
    if exceptions:
        entities = dict(current.entities)
        entities[EntityRef(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)] = {
            "columns": list(keys), "exceptions": [{"person_id": str(roster.persons[i].person_id), "column": key} for i, key in exceptions]}
        current = HistoricalSnapshot(roster.revision_id, entities)
    return roster, current


def test_household_sharing_is_automatic_and_preview_includes_other_member():
    roster, current = _family()
    first, second, outside = [p.person_id for p in roster.persons]
    prepared = plan(current, ((first, "note"),), find="keep", replacement="새 비고")
    updated = result(current, prepared)
    assert updated.person(first).profile.note == updated.person(second).profile.note == "새 비고"
    assert updated.person(outside).profile.note == "keep"
    assert {row[-1] for row in prepared.rows} == {"선택 셀", "함께 반영"}


def test_household_exceptions_are_preserved():
    roster, current = _family(exceptions=((1, "note"),))
    first, second = [p.person_id for p in roster.persons[:2]]
    updated = result(current, plan(current, ((first, "note"),), find="keep", replacement="새 비고"))
    assert updated.person(first).profile.note == "새 비고"
    assert updated.person(second).profile.note == "keep"


def test_shared_address_multiple_selected_members_preserve_future_moves():
    from src.domain.delivery_routing import RoutingEvent, ROUTING_EVENT_KIND, _routing_event_state, routing_state_from_snapshot
    roster, current = _family(keys=("address",))
    first, second = [p.person_id for p in roster.persons[:2]]
    entities = dict(current.entities)
    events = [RoutingEvent(uuid4(), pid, date(2026, 9, 1), i + 1, {}, "현재 주소", "101호")
              for i, pid in enumerate((first, second))]
    future = RoutingEvent(uuid4(), first, date(2026, 12, 1), 3, {}, "예약 주소", "202호")
    for event in (*events, future):
        entities[EntityRef(ROUTING_EVENT_KIND, event.event_id)] = _routing_event_state(event)
    current = HistoricalSnapshot(roster.revision_id, entities)
    prepared = plan(current, ((first, "address"), (second, "address")), operation="fill", replacement="정정 주소")
    updated_snapshot = import_candidate_snapshot(current, prepared.record)
    updated = roster_from_historical_snapshot(updated_snapshot)
    routing = routing_state_from_snapshot(updated_snapshot)
    assert routing.resolve(updated, first, DAY).road_address == "정정 주소"
    assert routing.resolve(updated, second, DAY).road_address == "정정 주소"
    assert routing.resolve(updated, first, date(2026, 12, 2)).road_address == "예약 주소"
    assert updated_snapshot.entity(ROUTING_EVENT_KIND, future.event_id) == current.entity(ROUTING_EVENT_KIND, future.event_id)
    assert routing.resolve(updated, second, DAY).detail_address == "101호"


def test_lunch_period_batch_stays_personal_when_lunch_is_shared():
    roster, current = _family(keys=("status:lunch", "note"))
    roster = replace(roster, persons=tuple(replace(p, lunch_period=InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 28))) for p in roster.persons),
        enrollments=tuple(MealEnrollment(p.person_id, Meal.LUNCH, InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 28))) for p in roster.persons))
    entities = dict(current.entities)
    entities = {ref: state for ref, state in entities.items() if not ref.entity_kind.startswith("roster.")}
    entities.update(roster_entity_states(roster))
    current = HistoricalSnapshot(roster.revision_id, entities)
    first, second = [p.person_id for p in roster.persons[:2]]
    prepared = plan(current, ((first, "lunch_starts_on"), (first, "lunch_ends_on")),
        match_mode="partial", find="2026", replacement="2027")
    updated = result(current, prepared)
    assert updated.person(first).lunch_period == InclusiveDateRange(date(2027, 8, 17), date(2027, 8, 28))
    assert updated.person(second).lunch_period == roster.person(second).lunch_period
    assert Meal.LUNCH not in updated.meals_for(first, on=date(2026, 8, 18))
    assert Meal.LUNCH in updated.meals_for(second, on=date(2026, 8, 18))


def test_right_click_keeps_selected_visible_general_cells_and_meal_menu(app, monkeypatch):
    window = RosterWindow(_roster())
    window._include_hidden_filter.setChecked(True)
    window.show()
    app.processEvents()
    model = window.table_model
    columns = {c.key: i for i, c in enumerate(model.columns)}
    keys = ("school", "note", "status:lunch")
    selection = window._table.selectionModel()
    for row in range(2):
        for key in keys:
            window._table.setColumnHidden(columns[key], False)
            selection.select(model.index(row, columns[key]), QItemSelectionModel.SelectionFlag.Select)
    index = model.index(0, columns["school"])
    window._select_for_context_menu(index)
    assert len(window.selected_editable_cells()) == 4
    window._table.setColumnHidden(columns["note"], True)
    assert len(window.selected_editable_cells()) == 2
    emitted = []
    window.bulk_cell_edit_requested.connect(lambda *args: emitted.append(args))
    menu = window._create_context_menu(model.person_at(0), column_key="school")
    action = next(a for a in menu.actions() if a.text().startswith("선택 셀 일괄 변경"))
    action.trigger()
    assert set(emitted[0][0]) == {(PERSON_A, "school"), (PERSON_B, "school")}
    meal_menu = window._create_context_menu(model.person_at(0), Meal.LUNCH, "status:lunch", window.selected_meal_targets())
    assert not any("셀 일괄 변경" in a.text() for a in meal_menu.actions())
    assert any("선택한 식종" in a.text() for a in meal_menu.actions())
    opened = []
    create_menu = window._create_context_menu
    def capture_menu(*args):
        actual = create_menu(*args)
        return SimpleNamespace(exec=lambda _point: opened.append([a.text() for a in actual.actions()]))
    monkeypatch.setattr(window, "_create_context_menu", capture_menu)
    window._table.scrollTo(index)
    app.processEvents()
    window._open_context_menu(window._table.visualRect(index).center())
    assert any(text == "선택 셀 일괄 변경 · 2개" for text in opened[-1])
    assert len(window.selected_editable_cells()) == 2
    window.close()


def test_dialog_preview_invalidates_when_inputs_change_and_cancel_keeps_source(app):
    current = snapshot()
    dialog = RosterBulkEditDialog(current, ((PERSON_A, "school"),), LABELS, DAY, actor=ACTOR)
    assert dialog.match_mode.currentData() == "exact"
    dialog.find_text.setPlainText("가초")
    dialog.replacement.setPlainText("새 학교")
    dialog.refresh_preview()
    assert dialog.save_button.isEnabled() and dialog.table.rowCount() == 1
    dialog.replacement.setPlainText("다른 학교")
    assert not dialog.save_button.isEnabled() and dialog.plan.record is None
    dialog.reject()
    assert current == snapshot()


def test_controller_saves_one_revision_and_backup_with_automatic_sharing(app, tmp_path):
    roster, current = _family()
    first, second = [p.person_id for p in roster.persons[:2]]
    initial = RevisionRecord(RevisionMetadata(roster.revision_id, None, STAMP, DAY, ACTOR, "합성 명단", RevisionSource.UI),
        tuple(AuditChange(ref.entity_kind, ref.entity_id, ChangeOperation.INSERT, None, state) for ref, state in current.entities.items()))
    prepared = plan(current, ((first, "note"),), find="keep", replacement="새 비고")
    class Dialog:
        def __init__(self, *_args):
            self.plan = prepared
        def exec(self):
            return QDialog.DialogCode.Accepted
    class Messages:
        @staticmethod
        def critical(*args):
            raise AssertionError(args)
        warning = critical
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    with DatabaseSession.create(tmp_path / "operation.db", "password", "recovery", backup_directory=backup_dir) as session:
        session.append_revision(initial)
        before = session.database.latest_revision().metadata.revision_id
        outcome = RosterBulkEditController(session, dialog_type=Dialog, message_box=Messages).run(
            ((first, "note"),), LABELS, before, DAY)
        assert outcome.completed and outcome.backup_result.succeeded
        assert outcome.roster.person(first).profile.note == outcome.roster.person(second).profile.note == "새 비고"
        latest = session.database.latest_revision()
        assert latest.metadata.parent_revision_id == before
        assert latest.metadata.revision_id == prepared.record.metadata.revision_id
        assert roster_from_historical_snapshot(session.database.snapshot(before)).person(first).profile.note == "keep"
        session.database.check_integrity()


def test_stale_selection_is_rejected_before_dialog(app):
    errors = []
    session = SimpleNamespace(database=SimpleNamespace(current_roster=_roster))
    messages = SimpleNamespace(critical=lambda *_args: errors.append(_args[-1]))
    outcome = RosterBulkEditController(session, message_box=messages).run(((PERSON_A, "school"),), LABELS, uuid4(), DAY)
    assert not outcome.completed and "최신 내용" in errors[0]


@pytest.mark.parametrize("mode", ["cancel", "conflict", "save_failure"])
def test_cancel_conflict_and_save_failure_never_partially_apply(app, mode):
    current = snapshot()
    prepared = plan(current, ((PERSON_A, "school"), (PERSON_B, "school")), operation="fill", replacement="새 학교")
    roster = _roster()
    records, errors = [], []
    class Database:
        def current_roster(self):
            return roster
        def snapshot(self, _revision):
            return current
    class Session:
        database = Database()
        def append_revision(self, record):
            records.append(record)
            raise OSError("저장 실패")
    class Dialog:
        def __init__(self, *_args):
            self.plan = prepared
        def exec(self):
            nonlocal roster
            if mode == "conflict":
                roster = replace(roster, revision_id=uuid4())
            return QDialog.DialogCode.Rejected if mode == "cancel" else QDialog.DialogCode.Accepted
    outcome = RosterBulkEditController(Session(), dialog_type=Dialog,
        message_box=SimpleNamespace(critical=lambda *_args: errors.append(_args[-1]))).run(
            ((PERSON_A, "school"), (PERSON_B, "school")), LABELS, roster.revision_id, DAY)
    assert not outcome.completed
    assert len(records) == (1 if mode == "save_failure" else 0)
    assert roster.person(PERSON_A).profile.school_name == "가초"
    if mode == "conflict":
        assert "최신 내용" in errors[0]
