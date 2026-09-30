"""Desktop export must preserve selection, protection, cancellation and failure behavior."""
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import msoffcrypto
import openpyxl
import pytest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox

from src.desktop_v2.database_export import (
    DatabaseExportController,
    DatabaseExportDialog,
    DatabaseExportSelection,
)
from src.desktop_v2.ledger_reader import load_revision_chain
from src.domain.export_security import build_export_password_revision
from src.domain.projections import roster_audit_changes
from src.domain.revisions import (
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.storage import DatabaseSession
from tests.test_desktop_v2_roster import SERVICE_DAY, _roster


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def session(tmp_path):
    actor = RevisionActor("test", "test-sid")
    roster = _roster()
    with DatabaseSession.create(tmp_path / "export.sqlite3", "db-password", "recovery") as session:
        session.append_revision(RevisionRecord(
            RevisionMetadata(roster.revision_id, None, datetime(2026, 9, 20, tzinfo=UTC),
                             SERVICE_DAY, actor, "명단 등록", RevisionSource.IMPORT),
            roster_audit_changes(None, roster),
        ))
        prepared = build_export_password_revision(
            roster.revision_id, None, "xlsx-test-password", actor=actor,
            recorded_at=datetime(2026, 9, 21, tzinfo=UTC), revision_id=uuid4(),
        )
        session.append_revision(prepared.record)
        yield session


class AcceptedDialog:
    sections = ("roster", "complaints", "history")

    def __init__(self, day, parent):
        self.day = day

    def exec(self):
        return QDialog.DialogCode.Accepted

    def selection(self):
        return DatabaseExportSelection(self.day, self.sections)


def messages():
    errors, successes = [], []
    return SimpleNamespace(
        critical=lambda *args: errors.append(args),
        information=lambda *args: successes.append(args),
        question=lambda *args: QMessageBox.StandardButton.No,
        errors=errors, successes=successes,
    )


def test_export_selection_requires_one_section(app):
    dialog = DatabaseExportDialog(SERVICE_DAY)
    for checkbox in dialog.sections.values():
        checkbox.setChecked(False)
    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Save).isEnabled()
    with pytest.raises(ValueError):
        dialog.selection()
    dialog.sections["complaints"].setChecked(True)
    assert dialog.selection().sections == ("complaints",)
    dialog.close()


def test_export_real_encrypted_workbook_and_exact_revision_chain(app, session, tmp_path):
    target = tmp_path / "current.xlsx"
    notices = messages()
    current = session.database.current_roster()
    records = load_revision_chain(session.database, current.revision_id)
    assert len(records) == 2
    assert records[-1].metadata.revision_id == current.revision_id
    controller = DatabaseExportController(
        session, dialog_type=AcceptedDialog, message_box=notices,
        file_dialog=SimpleNamespace(getSaveFileName=lambda *args, **kwargs: (str(target), "")),
    )
    result = controller.run(SERVICE_DAY)
    assert result is not None and result.password_protected
    assert not notices.errors and len(notices.successes) == 1
    with target.open("rb") as stream:
        office = msoffcrypto.OfficeFile(stream)
        assert office.is_encrypted()
        office.load_key(password="xlsx-test-password")
        data = BytesIO()
        office.decrypt(data)
    workbook = openpyxl.load_workbook(data)
    text = " ".join(str(value) for sheet in workbook for row in sheet.values for value in row)
    assert "홍길동" in text and "김아침" in text
    assert "xlsx-test-password" not in text
    assert session.database.current_roster().revision_id == current.revision_id
    workbook.close()


@pytest.mark.parametrize("mode", ["dialog", "file", "overwrite", "failure"])
def test_export_cancel_and_failure_never_report_success(app, session, tmp_path, mode):
    notices = messages()
    target = tmp_path / "existing.xlsx"
    target.write_bytes(b"original")
    built = []

    class Dialog(AcceptedDialog):
        def exec(self):
            return QDialog.DialogCode.Rejected if mode == "dialog" else super().exec()

    def builder(*args, **kwargs):
        built.append(True)
        raise OSError("generation failed")

    if mode == "failure":
        target = tmp_path / "new.xlsx"
    controller = DatabaseExportController(
        session, dialog_type=Dialog, message_box=notices, workbook_builder=builder,
        file_dialog=SimpleNamespace(
            getSaveFileName=lambda *args, **kwargs: ("" if mode == "file" else str(target), "")
        ),
    )
    assert controller.run(SERVICE_DAY) is None
    assert not notices.successes
    assert len(built) == int(mode == "failure")
    assert len(notices.errors) == int(mode == "failure")
    assert (tmp_path / "existing.xlsx").read_bytes() == b"original"


def test_real_startup_menu_saves_complaint_then_history_and_export(app, session, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QTableView

    from src.desktop_v2.application import DesktopStartupController
    from src.desktop_v2.complaints import ComplaintController, ComplaintDialog
    from src.domain import Meal
    from src.domain.complaints import complaints_from_snapshot

    notices = messages()
    notices.warning = lambda *args: notices.errors.append(args)
    target = tmp_path / "menu-export.xlsx"

    class FilledComplaint(ComplaintDialog):
        def exec(self):
            self._channel.setCurrentText("메일")
            for meal, check in self._meal_checks.items():
                check.setChecked(meal in (Meal.BREAKFAST, Meal.DINNER))
            self._content.setPlainText("배송 시간 확인 요청")
            self._save_button.click()
            return self.result()

    def complaint_controller(actual_session):
        return ComplaintController(
            actual_session, dialog_type=FilledComplaint,
            actor_provider=lambda: RevisionActor("tester", "test-sid"),
            message_box=notices,
        )

    def export_controller(actual_session):
        return DatabaseExportController(
            actual_session, dialog_type=AcceptedDialog, message_box=notices,
            file_dialog=SimpleNamespace(getSaveFileName=lambda *a, **k: (str(target), "")),
        )

    controller = DesktopStartupController(
        complaint_controller_type=complaint_controller,
        database_export_controller_type=export_controller,
        settings=SimpleNamespace(document_output_directory=tmp_path), message_box=notices,
    )
    monkeypatch.setattr(controller, "_access_dialog", lambda parent: SimpleNamespace(
        exec=lambda: QDialog.DialogCode.Accepted, session=session,
    ))
    outcome = controller.start()
    assert outcome.ready
    window = outcome.window
    window._set_day(SERVICE_DAY)
    old_id = window.roster.revision_id
    person = window.roster.persons[0]
    menu = window._create_context_menu(person, column_key="name")
    next(action for action in menu.actions() if action.text() == "민원").trigger()
    assert window.roster.revision_id != old_id
    snapshot = session.database.snapshot(window.roster.revision_id)
    complaint = complaints_from_snapshot(snapshot)[0]
    assert complaint.person_id == person.person_id
    assert complaint.channel.value == "메일" and complaint.meal is None
    assert complaint.meals == (Meal.BREAKFAST, Meal.DINNER)
    assert not complaints_from_snapshot(session.database.snapshot(old_id))
    next(action for action in menu.actions() if action.text() == "이력보기").trigger()
    assert window._history_dialogs
    dialog = window._history_dialogs[-1]
    table = dialog.findChild(QTableView, "history_민원")
    assert table.model().rowCount() == 1
    assert any(
        table.model().data(table.model().index(0, col)) == "배송 시간 확인 요청"
        for col in range(table.model().columnCount())
    )
    assert any(
        table.model().data(table.model().index(0, col)) == "조식, 석식"
        for col in range(table.model().columnCount())
    )
    window._database_export_action.trigger()
    assert target.exists() and not notices.errors
    with target.open("rb") as stream:
        office = msoffcrypto.OfficeFile(stream)
        office.load_key(password="xlsx-test-password")
        data = BytesIO()
        office.decrypt(data)
    workbook = openpyxl.load_workbook(data)
    assert any("조식, 석식" in row for row in workbook["민원"].values)
    workbook.close()
    dialog.close()
    window.close()


def test_publish_failure_preserves_existing_file_and_reports_no_success(app, session, tmp_path):
    target = tmp_path / "kept.xlsx"
    target.write_bytes(b"previous workbook")
    notices = messages()
    notices.question = lambda *args: QMessageBox.StandardButton.Yes

    class BrokenWorkbook:
        def save(self, destination):
            raise OSError("output storage failure")
        def close(self):
            pass

    result = DatabaseExportController(
        session, dialog_type=AcceptedDialog, message_box=notices,
        workbook_builder=lambda *args, **kwargs: BrokenWorkbook(),
        file_dialog=SimpleNamespace(getSaveFileName=lambda *args, **kwargs: (str(target), "")),
    ).run(SERVICE_DAY)
    assert result is None and notices.errors and not notices.successes
    assert target.read_bytes() == b"previous workbook"
    assert not list(tmp_path.glob(".kept.xlsx.*"))


@pytest.mark.parametrize("fail", [False, True])
def test_export_keeps_ui_responsive_and_worker_alive_until_finished(app, session, tmp_path, fail):
    from threading import Event, get_ident

    from PySide6.QtCore import QTimer

    from src.desktop_v2.database_export import DatabaseExportProgressDialog
    from src.services.database_export import build_database_workbook

    released = Event()
    entered = Event()
    observed = []
    ui_thread = get_ident()
    notices = messages()

    def builder(*args, **kwargs):
        assert get_ident() != ui_thread
        kwargs["progress"]("명단 테스트 처리 중", 1, 2)
        entered.set()
        assert released.wait(5), "UI event loop did not continue during export"
        if fail:
            raise OSError("worker failure")
        return build_database_workbook(*args, **kwargs)

    def heartbeat():
        if not entered.is_set():
            return
        dialog = next((w for w in app.topLevelWidgets()
                       if isinstance(w, DatabaseExportProgressDialog) and w.isVisible()), None)
        if dialog is None or dialog.bar.maximum() != 2:
            return
        observed.append((get_ident(), dialog.stage.text(), dialog.bar.value()))
        dialog.reject()
        dialog.close()
        assert dialog.isVisible(), "an active export must retain its session and thread"
        released.set()

    timer = QTimer()
    timer.timeout.connect(heartbeat)
    timer.start(10)
    target = tmp_path / "responsive.xlsx"
    try:
        result = DatabaseExportController(
            session, dialog_type=AcceptedDialog, message_box=notices, workbook_builder=builder,
            file_dialog=SimpleNamespace(getSaveFileName=lambda *a, **k: (str(target), "")),
        ).run(SERVICE_DAY)
    finally:
        timer.stop()
        released.set()
    assert observed and observed[0] == (ui_thread, "명단 테스트 처리 중", 1)
    assert bool(notices.errors) is fail
    assert (result is None) is fail
    assert target.exists() is not fail
    assert session.database.current_roster() is not None
