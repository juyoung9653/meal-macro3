"""History tabs and real roster signals remain connected to the selected person."""
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from PySide6.QtWidgets import QApplication, QDialog, QTableView

from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.person_history import PersonHistoryDialog, create_person_history_dialog
from src.desktop_v2.meal_history import MEAL_HISTORY_HEADINGS, build_person_meal_history_rows
from src.desktop_v2.roster_window import RosterWindow
from src.domain.roster import Meal
from src.domain.projections import STATUS_EVENT_KIND, status_event_audit_change
from src.domain.revisions import (
    AuditChange, ChangeOperation, HistoricalSnapshot, ImportBatch,
    RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource,
)
from src.domain.roster import InclusiveDateRange
from src.domain.status import MealStatus, StatusEvent
from tests.test_desktop_v2_roster import SERVICE_DAY, _roster


def test_history_tabs_show_rows_and_existing_period_view():
    app = QApplication.instance() or QApplication([])
    app.processEvents()
    person = _roster().persons[0]
    tables = {
        name: (("시각", "내용"), ((datetime(2026, 9, 21, 10), name + " 기록"),))  # noqa: DTZ001 - presentation rows are KST-naive
        for name in ("정보 변경", "식종 변경", "민원")
    }
    dialog = PersonHistoryDialog(person, tables, {}, Meal.BREAKFAST)
    assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == [
        "정보 변경", "식종 변경", "민원", "상태 적용 기간",
    ]
    for name in tables:
        table = dialog.findChild(QTableView, f"history_{name}")
        assert table.model().data(table.model().index(0, 1)) == name + " 기록"
    periods = dialog.tabs.widget(3)
    assert periods.tabText(periods.currentIndex()) == "조식"
    dialog.close()


def test_history_provider_and_export_button_use_actual_window():
    app = QApplication.instance() or QApplication([])
    window = RosterWindow(_roster())
    opened, exports = [], []

    def provider(person, meal, parent):
        opened.append((person.person_id, meal, parent))
        return QDialog(parent)

    window.set_history_provider(provider)
    window.database_export_requested.connect(lambda: exports.append(True))
    person = window.roster.persons[0]
    menu = window._create_context_menu(person, Meal.BREAKFAST)
    next(action for action in menu.actions() if action.text() == "이력보기").trigger()
    assert opened == [(person.person_id, Meal.BREAKFAST, window)]
    assert len(window._history_dialogs) == 1
    window._database_export_action.trigger()
    assert exports == [True]
    for dialog in tuple(window._history_dialogs):
        dialog.close()
    app.processEvents()
    window.close()


def test_application_routes_complaint_and_export_after_password_setup(monkeypatch):
    roster = _roster()
    calls, refreshes = [], []
    session = object()
    window = SimpleNamespace(table_model=SimpleNamespace(service_day=SERVICE_DAY))

    class Complaint:
        def __init__(self, current):
            assert current is session
        def run(self, person_id, meal, parent, *, household_day):
            assert household_day == SERVICE_DAY
            calls.append(("complaint", person_id, meal, parent))
            return SimpleNamespace(completed=True, roster=roster)

    class Password:
        def __init__(self, current):
            assert current is session
        def ensure_configured(self, parent):
            return SimpleNamespace(completed=True, roster=None)

    class Export:
        def __init__(self, current):
            assert current is session
        def run(self, day, parent, *, directory):
            calls.append(("export", day, parent, directory))

    controller = DesktopStartupController(
        complaint_controller_type=Complaint, export_password_controller_type=Password,
        database_export_controller_type=Export,
        settings=SimpleNamespace(document_output_directory=None),
    )
    monkeypatch.setattr(controller, "_replace_roster_if_changed", lambda *args: refreshes.append(args))
    controller._run_complaint(session, window, roster.persons[0].person_id, Meal.LUNCH)
    assert refreshes == [(session, window, roster)]
    controller._run_database_export(session, window)
    assert calls == [
        ("complaint", roster.persons[0].person_id, Meal.LUNCH, window),
        ("export", SERVICE_DAY, window, None),
    ]


def test_existing_status_period_panel_displays_korean_recorded_time():
    from src.desktop_v2.roster_window import StatusHistoryDialog
    from tests.test_desktop_v2_roster import FIRST_PERSON_ID, _histories
    app = QApplication.instance() or QApplication([])
    history = _histories()[(FIRST_PERSON_ID, Meal.LUNCH)]
    dialog = StatusHistoryDialog(_roster().persons[0], history)
    app.processEvents()
    tables = dialog.findChildren(QTableView)
    raw = next(table for table in tables if table.model().columnCount() == 5)
    assert raw.model().data(raw.model().index(0, 0)) == "2026-08-25 01:00"
    dialog.close()


def _status_revision(status, starts_on, ends_on, *, number=1):
    revision_id = UUID(int=200 + number)
    person = _roster().persons[0]
    recorded_at = datetime(2026, 10, 14, number, tzinfo=UTC)
    event = StatusEvent(
        UUID(int=100 + number), revision_id, person.person_id, Meal.LUNCH,
        status, InclusiveDateRange(starts_on, ends_on), recorded_at,
        "상태 표시 문자열을 날짜로 해석하면 안 됩니다.",
    )
    metadata = RevisionMetadata(
        revision_id, None, recorded_at, date(2026, 10, 15),
        RevisionActor("합성 담당자", "synthetic-sid"), f"합성 사유 {number}",
        RevisionSource.UI, display_code=f"20261014-{number:03d}",
    )
    return RevisionRecord(metadata, (status_event_audit_change(event),))


@pytest.mark.parametrize(("status", "category", "ends_on", "expected"), (
    (MealStatus.DELIVERY, "시작", None, "2026-10-01 ~ "),
    (MealStatus.DELIVERY, "시작", date(2026, 10, 31), "2026-10-01 ~ "),
    (MealStatus.STOPPED, "중지", None, "2026-10-01 ~ "),
    (MealStatus.STOPPED, "중지", date(2026, 10, 31), "2026-10-01 ~ "),
    (MealStatus.PAUSED, "일시중지", date(2026, 10, 5), "2026-10-01 ~ 2026-10-05"),
))
def test_meal_history_projects_exact_columns_status_period_and_metadata(status, category, ends_on, expected):
    record = _status_revision(status, date(2026, 10, 1), ends_on)
    original_after = record.changes[0].after
    rows = build_person_meal_history_rows((record,), _roster().persons[0].person_id)
    assert MEAL_HISTORY_HEADINGS == ("식종", "구분", "기간", "변경자", "변경번호")
    assert rows == (("중식", category, expected, "합성 담당자", "20261014-001"),)
    assert record.changes[0].after == original_after
    assert original_after["ends_on"] == (ends_on.isoformat() if ends_on else None)


def test_meal_history_missing_start_is_explicit_without_applied_date():
    record = _status_revision(MealStatus.STOPPED, None, date(2026, 10, 31))
    record = replace(record, metadata=replace(record.metadata, effective_on=None, display_code=None))
    row = build_person_meal_history_rows((record,), _roster().persons[0].person_id)[0]
    assert row[2] == "시작일 미지정 ~ "
    assert row[3] == "합성 담당자"
    assert row[4] == "20261014-100000"


def test_meal_history_excludes_nonstatus_deletion_import_and_other_person():
    person_id = _roster().persons[0].person_id
    original = _status_revision(MealStatus.PAUSED, date(2026, 10, 1), date(2026, 10, 5))
    change = original.changes[0]
    nonstatus = tuple(AuditChange(kind, UUID(int=500 + index), ChangeOperation.INSERT,
                                 None, {"person_id": str(person_id), "meal": "석식"})
                      for index, kind in enumerate(("roster.delivery_assignment", "roster.meal_enrollment", "routing.event")))
    deleted = AuditChange(STATUS_EVENT_KIND, change.entity_id, ChangeOperation.DELETE, change.after, None)
    other = replace(change, entity_id=UUID(int=501), after={**change.after, "person_id": str(UUID(int=999))})
    mixed = replace(original, changes=(change, *nonstatus, other))
    removal = replace(original, metadata=replace(original.metadata, revision_id=UUID(int=210)), changes=(deleted,))
    imported = _status_revision(MealStatus.STOPPED, date(2026, 10, 1), None, number=2)
    batch = ImportBatch(UUID(int=800), imported.metadata.revision_id, "synthetic.xlsx", "0" * 64,
                        0, "명단", "synthetic", {}, date(2026, 10, 1))
    imported = replace(imported, metadata=replace(imported.metadata, source=RevisionSource.IMPORT,
                       decisions={"import_purpose": "database"}), import_batch=batch)
    rows = build_person_meal_history_rows((mixed, removal, imported), person_id)
    assert len(rows) == 1
    assert rows[0][1] == "일시중지"
    # A non-roster import remains visible under the established import rule.
    lunch_import = replace(imported, metadata=replace(imported.metadata, decisions={"import_purpose": "lunch"}))
    assert len(build_person_meal_history_rows((mixed, lunch_import), person_id)) == 2
    legacy_import = replace(imported, metadata=replace(imported.metadata, decisions={}, reason="Excel 명단 가져오기"))
    assert build_person_meal_history_rows((legacy_import,), person_id) == ()


def test_meal_history_keeps_affirmative_updates_and_uses_oldest_first_order():
    original = _status_revision(MealStatus.DELIVERY, date(2026, 10, 1), None)
    updated = _status_revision(MealStatus.STOPPED, date(2026, 10, 12), None, number=2)
    updated = replace(updated, changes=(replace(
        updated.changes[0], operation=ChangeOperation.UPDATE,
        before=original.changes[0].after,
    ),))
    rows = build_person_meal_history_rows((updated, original), _roster().persons[0].person_id)
    assert tuple(row[1] for row in rows) == ("시작", "중지")
    assert rows[1][2] == "2026-10-12 ~ "
    assert tuple(row[4] for row in rows) == ("20261014-001", "20261014-002")


def test_same_typed_status_record_preserves_shared_export_format():
    from src.services.person_history import build_history_tables
    roster = _roster()
    person_id = roster.persons[0].person_id
    record = _status_revision(MealStatus.PAUSED, date(2026, 10, 1), date(2026, 10, 5))
    source = record.changes[0].after
    tables = build_history_tables(roster, HistoricalSnapshot(roster.revision_id, {}),
                                  (record,), person_id=person_id)
    export_headings, export_rows = tables["식종 변경"]
    assert len(export_headings) == 12
    assert export_rows[0][export_headings.index("구분")] == "상태 변경"
    assert export_rows[0][export_headings.index("변경 값")] == (
        "상태: 일시중지\n기간: 2026-10-01 ~ 2026-10-05\n"
        "사유: 상태 표시 문자열을 날짜로 해석하면 안 됩니다."
    )
    ui_rows = build_person_meal_history_rows((record,), person_id)
    assert ui_rows[0][1:3] == ("일시중지", "2026-10-01 ~ 2026-10-05")
    assert source["status"] == "일시중지"
    assert source["starts_on"] == "2026-10-01" and source["ends_on"] == "2026-10-05"


def test_person_history_provider_uses_ui_projection_without_changing_shared_tables(monkeypatch):
    from src.desktop_v2 import person_history as module
    app = QApplication.instance() or QApplication([])
    roster = _roster()
    person = roster.persons[0]
    record = _status_revision(MealStatus.PAUSED, date(2026, 10, 1), date(2026, 10, 5))
    shared = {
        "정보 변경": (("항목",), (("학교명",),)),
        "식종 변경": (("이전 값", "변경 값"), (("이전", "공유 출력 값"),)),
        "민원": (("내용",), (("합성 민원",),)),
    }
    monkeypatch.setattr(module, "load_revision_chain", lambda *args: (record,))
    monkeypatch.setattr(module, "build_history_tables", lambda *args, **kwargs: dict(shared))
    database = SimpleNamespace(
        current_roster=lambda: roster,
        snapshot=lambda _: HistoricalSnapshot(roster.revision_id, {}),
        status_history=lambda *args: None,
    )
    dialog = create_person_history_dialog(SimpleNamespace(database=database), person, Meal.LUNCH)
    view = dialog.findChild(QTableView, "history_식종 변경")
    assert view.model().headings == MEAL_HISTORY_HEADINGS
    assert view.model().rowCount() == 1
    assert view.model().data(view.model().index(0, 2)) == "2026-10-01 ~ 2026-10-05"
    assert shared["식종 변경"][0] == ("이전 값", "변경 값")
    assert dialog.findChild(QTableView, "history_정보 변경").model().rows == (("학교명",),)
    assert dialog.findChild(QTableView, "history_민원").model().rows == (("합성 민원",),)
    dialog.close()


def test_other_history_tabs_focus_values_and_keep_full_details():
    from PySide6.QtWidgets import QPlainTextEdit
    from src.services.person_history import _INFO_HEADINGS, _COMPLAINT_HEADINGS
    from src.domain.status import resolve_status_history
    app = QApplication.instance() or QApplication([])
    person = _roster().persons[0]
    text = "긴 민원 내용과 줄바꿈\n" * 8
    tables = {
        "정보 변경": (_INFO_HEADINGS, ((datetime(2026, 10, 20, 10), date(2026, 10, 1),
            "20261020-001", str(person.person_id), person.profile.name, "개인정보", "주소",
            "이전 주소", "새 주소", "합성 정정 사유", "합성 담당자"),)),
        "식종 변경": (MEAL_HISTORY_HEADINGS, ()),
        "민원": (_COMPLAINT_HEADINGS, ((datetime(2026, 10, 19, 9), datetime(2026, 10, 20, 10),
            "합성 기록자", str(UUID(int=123)), str(person.person_id), person.profile.name,
            "중식", "전화", text),)),
    }
    original = repr(tables)
    record = _status_revision(MealStatus.PAUSED, date(2026, 10, 1), date(2026, 10, 5))
    from src.domain.projections import status_event_from_state
    event = status_event_from_state(record.changes[0].entity_id, record.changes[0].after)
    history = resolve_status_history(person.person_id, Meal.LUNCH, (event,))
    dialog = PersonHistoryDialog(person, tables, {Meal.LUNCH: history}, Meal.LUNCH)
    dialog.resize(1080, 680)
    dialog.show()
    app.processEvents()
    info = dialog.findChild(QTableView, "history_정보 변경")
    assert info.model().headings == ("항목", "이전 값", "변경 값", "변경자", "변경번호")
    assert info.model().rows[0] == ("주소", "이전 주소", "새 주소", "합성 담당자", "20261020-001")
    detail = dialog.findChild(QPlainTextEdit, "historyDetails_정보 변경").toPlainText()
    assert "적용일: 2026-10-01" in detail and "합성 정정 사유" in detail
    assert str(person.person_id) not in detail
    complaint = dialog.findChild(QTableView, "history_민원")
    assert complaint.model().headings == ("접수일시", "식종", "접수 경로", "내용", "기록자")
    assert complaint.model().data(complaint.model().index(0, 3)) == text
    detail = dialog.findChild(QPlainTextEdit, "historyDetails_민원").toPlainText()
    assert text in detail and "기록 시각(KST): 2026-10-20 10:00" in detail
    assert str(UUID(int=123)) not in detail
    periods = dialog.findChild(QTableView, "historyPeriods_LUNCH")
    assert periods.model().headings == ("기간", "상태")
    assert periods.model().rows == (("2026-10-01 ~ 2026-10-05", "일시중지"),)
    detail = dialog.findChild(QPlainTextEdit, "historyPeriodDetails_LUNCH").toPlainText()
    assert "2026-10-14 10:00" in detail and event.source_text in detail
    assert str(event.event_id) not in detail
    assert repr(tables) == original
    for index, table in ((0, info), (2, complaint), (3, periods)):
        dialog.tabs.setCurrentIndex(index)
        app.processEvents()
        assert table.horizontalScrollBar().maximum() == 0
    dialog.close()


def test_status_period_empty_tab_does_not_invent_periods():
    from PySide6.QtWidgets import QLabel
    app = QApplication.instance() or QApplication([])
    tables = {name: (("내용",), ()) for name in ("정보 변경", "식종 변경", "민원")}
    dialog = PersonHistoryDialog(_roster().persons[0], tables, {}, Meal.DINNER)
    view = dialog.findChild(QTableView, "historyPeriods_DINNER")
    assert view.model().rowCount() == 0
    assert any(label.text() == "기록된 상태 변경이 없습니다." for label in dialog.findChildren(QLabel))
    dialog.close()
