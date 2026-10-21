"""Real document publication stays responsive and preserves its failure boundary."""
from __future__ import annotations

import os
from datetime import date
from threading import Event, get_ident
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import msoffcrypto
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from src.desktop_v2 import document_generation as generation
from src.desktop_v2.document_completion import DocumentCompletionDialog, GeneratedDocumentResult
from src.desktop_v2.document_hub import DocumentAction
from src.desktop_v2.document_meal import DocumentMealSelection
from src.desktop_v2.document_output import DocumentOutputMode, next_available_document_output
from src.desktop_v2.document_period import DocumentPeriodSelection
from src.desktop_v2.document_template import DocumentTemplateChangeStatus
from src.desktop_v2.startup_loading import StartupLoadingDialog
from src.domain.roster import Meal
from src.storage.database_session import AutomaticBackupResult, DatabaseSession
from tests.test_desktop_v2_document_generation import _initial_revision
from tests.test_desktop_v2_document_completion import _generated_result


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def document_session(tmp_path):
    outputs, backups = tmp_path / "outputs", tmp_path / "backups"
    outputs.mkdir()
    backups.mkdir()
    with DatabaseSession.create(tmp_path / "documents.sqlite3", "password", backup_directory=backups) as session:
        session.append_revision(_initial_revision())
        yield session, outputs


def selection_for(session, outputs, action):
    source = session.database.revision_summaries()[0]
    start = date(2026, 6, 17) if action is DocumentAction.DELIVERY_REPORT else date(2026, 6, 1)
    end = start if action is DocumentAction.DELIVERY_REPORT else date(2026, 6, 30)
    period = DocumentPeriodSelection(action, source, start, Meal.LUNCH, start, end)
    return next_available_document_output(DocumentMealSelection(period, (Meal.LUNCH,)), outputs, DocumentOutputMode.SAVED_DEFAULT)


def configured_template(_session):
    gui = get_ident()
    def configured(parent):
        assert get_ident() == gui
        return SimpleNamespace(completed=True)
    return SimpleNamespace(ensure_configured=configured)


class Messages:
    def __init__(self, app):
        self.app, self.gui, self.errors, self.warnings = app, get_ident(), [], []

    def _record(self, target, args):
        assert get_ident() == self.gui
        assert not isinstance(self.app.activeModalWidget(), StartupLoadingDialog)
        target.append(args)

    def critical(self, *args):
        self._record(self.errors, args)

    def warning(self, *args):
        self._record(self.warnings, args)


class Gate:
    """A GUI timer must release every deliberately blocked worker phase."""
    def __init__(self, app):
        self.app, self.gui = app, get_ident()
        self.pending = None
        self.released = Event()
        self.threads, self.observed = [], []
        self.seen = set()
        self.timer = QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(5)

    def checkpoint(self, phase):
        self.threads.append((phase, get_ident()))
        if phase in self.seen:
            return
        self.seen.add(phase)
        self.released.clear()
        self.pending = phase
        assert self.released.wait(5), f"GUI stopped during {phase}"

    def tick(self):
        if self.pending is None:
            return
        visible = [widget for widget in self.app.topLevelWidgets()
                   if isinstance(widget, StartupLoadingDialog) and widget.isVisible()]
        if not visible:
            return
        self.observed.append((self.pending, len(visible)))
        self.pending = None
        self.released.set()

    def close(self):
        self.timer.stop()
        self.released.set()


@pytest.mark.parametrize("action", [DocumentAction.INVOICE, DocumentAction.CONFIRMATION, DocumentAction.DELIVERY_REPORT])
def test_all_document_kinds_keep_gui_alive_through_real_writer_snapshot_verification_and_backup(app, document_session, monkeypatch, action):
    session, outputs = document_session
    selection = selection_for(session, outputs, action)
    messages, gate = Messages(app), Gate(app)
    snapshot, backup, verify = session.database.snapshot, session.try_backup_now, GeneratedDocumentResult.verify_file

    def read(*args):
        gate.checkpoint("snapshot")
        return snapshot(*args)
    def save_backup():
        gate.checkpoint("backup")
        return backup()
    def verify_output(result):
        gate.checkpoint("verification")
        return verify(result)
    def wrap(writer):
        def write(*args, **kwargs):
            gate.checkpoint("writer")
            return writer(*args, **kwargs)
        return write
    monkeypatch.setattr(session.database, "snapshot", read)
    monkeypatch.setattr(session, "try_backup_now", save_backup)
    monkeypatch.setattr(GeneratedDocumentResult, "verify_file", verify_output)
    try:
        outcome = generation.DocumentGenerationController(
            session, message_box=messages, template_controller_type=configured_template,
            invoice_writer=wrap(generation.make_invoice_publication),
            confirmation_writer=wrap(generation.make_confirm_publication),
            report_writer=wrap(generation.make_report_publication),
        ).run(selection)
    finally:
        gate.close()
    assert outcome.completed and outcome.backup_result.succeeded
    assert not messages.errors and not messages.warnings
    assert {phase for phase, _ in gate.observed} == {"snapshot", "writer", "verification", "backup"}
    assert all(count == 1 for _, count in gate.observed)
    assert all(thread != gate.gui for _, thread in gate.threads)
    assert len(session.database.document_runs()) == 1
    with outcome.result.output_path.open("rb") as stream:
        assert msoffcrypto.OfficeFile(stream).is_encrypted() is (
            action is not DocumentAction.DELIVERY_REPORT
        )
    session.database.check_integrity()


@pytest.mark.parametrize("phase", ["snapshot", "writer", "verification", "seal"])
def test_generation_failure_closes_progress_before_error_and_leaves_no_file_or_run(app, document_session, monkeypatch, phase):
    session, outputs = document_session
    selection = selection_for(session, outputs, DocumentAction.INVOICE)
    messages, gate = Messages(app), Gate(app)
    def fail(*args, **kwargs):
        gate.checkpoint(phase)
        raise OSError(f"{phase} unavailable")
    kwargs = {}
    if phase == "snapshot":
        monkeypatch.setattr(session.database, "snapshot", fail)
    elif phase == "writer":
        kwargs["invoice_writer"] = fail
    elif phase == "verification":
        monkeypatch.setattr(GeneratedDocumentResult, "verify_file", fail)
    else:
        monkeypatch.setattr(session, "append_document_run", fail)
    try:
        outcome = generation.DocumentGenerationController(
            session, message_box=messages, template_controller_type=configured_template, **kwargs,
        ).run(selection)
    finally:
        gate.close()
    assert outcome.status is generation.DocumentGenerationStatus.FAILED
    assert len(messages.errors) == 1 and phase in messages.errors[0][-1]
    assert not messages.warnings
    assert gate.observed == [(phase, 1)]
    assert not list(outputs.iterdir())
    assert session.database.document_runs() == ()


def test_backup_only_failure_preserves_published_file_and_sealed_run(app, document_session, monkeypatch):
    session, outputs = document_session
    messages = Messages(app)
    monkeypatch.setattr(session, "try_backup_now", lambda: AutomaticBackupResult(error=OSError("backup unavailable")))
    outcome = generation.DocumentGenerationController(
        session, message_box=messages, template_controller_type=configured_template,
    ).run(selection_for(session, outputs, DocumentAction.INVOICE))
    assert outcome.completed and outcome.result.output_path.is_file()
    assert len(session.database.document_runs()) == 1
    assert not messages.errors and len(messages.warnings) == 1


def test_cancel_template_does_not_start_generation(app, document_session, monkeypatch):
    session, outputs = document_session
    selection = selection_for(session, outputs, DocumentAction.INVOICE)
    def forbidden(*args):
        pytest.fail("generation started after cancellation")
    monkeypatch.setattr(session.database, "current_roster", forbidden)
    cancelled = SimpleNamespace(completed=False, status=DocumentTemplateChangeStatus.CANCELLED)
    outcome = generation.DocumentGenerationController(
        session, message_box=Messages(app),
        template_controller_type=lambda _: SimpleNamespace(ensure_configured=lambda parent: cancelled),
    ).run(selection)
    assert outcome.status is generation.DocumentGenerationStatus.CANCELLED
    assert not list(outputs.iterdir()) and not session.database.document_runs()


def test_completion_verifies_offthread_but_opens_file_on_gui(app, tmp_path, monkeypatch):
    result = _generated_result(tmp_path)
    original = GeneratedDocumentResult.verify_file
    gate, opened = Gate(app), []
    def verify(value):
        gate.checkpoint("completion verification")
        return original(value)
    monkeypatch.setattr(GeneratedDocumentResult, "verify_file", verify)
    try:
        dialog = DocumentCompletionDialog(result, path_opener=lambda path: opened.append((get_ident(), path)) or True)
        dialog._open_file()
        dialog.close()
    finally:
        gate.close()
    assert gate.observed == [("completion verification", 1)]
    assert len(gate.threads) == 2 and all(thread != gate.gui for _, thread in gate.threads)
    assert opened == [(gate.gui, result.output_path)]


def test_output_filename_probe_is_responsive_and_cancellation_creates_nothing(app, tmp_path, monkeypatch):
    from src.desktop_v2 import document_output as output
    from tests.test_desktop_v2_document_completion import _document
    gate = Gate(app)
    original = output.next_available_document_output
    def probe(*args):
        gate.checkpoint("output filename")
        return original(*args)
    monkeypatch.setattr(output, "next_available_document_output", probe)
    try:
        dialog = output.DocumentOutputDialog(_document(), tmp_path)
        dialog.reject()
    finally:
        gate.close()
    assert gate.observed == [("output filename", 1)]
    assert all(thread != gate.gui for _, thread in gate.threads)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("kind", ["template", "password"])
def test_document_prerequisites_read_offthread_before_returning_to_gui(app, document_session, monkeypatch, kind):
    from src.desktop_v2.document_template import DocumentTemplateController
    from src.desktop_v2.export_password import ExportPasswordController
    session, _ = document_session
    original = session.database.snapshot
    gate, messages = Gate(app), Messages(app)
    def snapshot(*args):
        gate.checkpoint("prerequisite snapshot")
        return original(*args)
    monkeypatch.setattr(session.database, "snapshot", snapshot)
    controller_type = DocumentTemplateController if kind == "template" else ExportPasswordController
    try:
        outcome = controller_type(session, message_box=messages).ensure_configured()
    finally:
        gate.close()
    assert outcome.completed and not messages.errors
    assert gate.observed == [("prerequisite snapshot", 1)]
    assert all(thread != gate.gui for _, thread in gate.threads)


@pytest.mark.parametrize("custom_factory", [False, True])
def test_application_verifies_once_when_opening_completion(app, tmp_path, monkeypatch, custom_factory):
    from src.desktop_v2.application import DesktopStartupController
    result = _generated_result(tmp_path)
    original = GeneratedDocumentResult.verify_file
    calls = []
    def verify(value):
        calls.append(get_ident())
        return original(value)
    monkeypatch.setattr(GeneratedDocumentResult, "verify_file", verify)
    monkeypatch.setattr(DocumentCompletionDialog, "exec", lambda self: 0)
    factory = (lambda result, parent=None: SimpleNamespace(exec=lambda: 0)) if custom_factory else DocumentCompletionDialog
    controller = DesktopStartupController(document_completion_dialog_type=factory, message_box=Messages(app))
    controller._show_document_completion(result, None)
    assert len(calls) == 1 and calls[0] != get_ident()


@pytest.mark.parametrize("action", [DocumentAction.INVOICE, DocumentAction.CONFIRMATION, DocumentAction.DELIVERY_REPORT])
def test_concurrent_output_created_after_name_choice_is_preserved(app, document_session, action):
    session, outputs = document_session
    selection = selection_for(session, outputs, action)
    messages = Messages(app)
    sentinel = b"another program's document"
    def wrap(writer):
        def concurrent(*args, **kwargs):
            assert kwargs["overwrite"] is False
            selection.output_path.write_bytes(sentinel)
            return writer(*args, **kwargs)
        return concurrent
    outcome = generation.DocumentGenerationController(
        session, message_box=messages, template_controller_type=configured_template,
        invoice_writer=wrap(generation.make_invoice_publication),
        confirmation_writer=wrap(generation.make_confirm_publication),
        report_writer=wrap(generation.make_report_publication),
    ).run(selection)
    assert outcome.status is generation.DocumentGenerationStatus.FAILED
    assert selection.output_path.read_bytes() == sentinel
    assert list(outputs.iterdir()) == [selection.output_path]
    assert not session.database.document_runs()
    assert len(messages.errors) == 1 and "덮어쓰지 않았습니다" in messages.errors[0][-1]


def test_seal_failure_preserves_external_replacement_even_when_size_matches(app, document_session, monkeypatch):
    session, outputs = document_session
    selection = selection_for(session, outputs, DocumentAction.INVOICE)
    messages = Messages(app)
    replacements = []
    def fail(run):
        replacement = b"x" * selection.output_path.stat().st_size
        selection.output_path.write_bytes(replacement)
        replacements.append(replacement)
        raise OSError("seal unavailable")
    monkeypatch.setattr(session, "append_document_run", fail)
    outcome = generation.DocumentGenerationController(
        session, message_box=messages, template_controller_type=configured_template,
    ).run(selection)
    assert outcome.status is generation.DocumentGenerationStatus.FAILED
    assert selection.output_path.read_bytes() == replacements[0]
    assert not session.database.document_runs()
    assert len(messages.errors) == 1 and "파일을 그대로 두었습니다" in messages.errors[0][-1]
