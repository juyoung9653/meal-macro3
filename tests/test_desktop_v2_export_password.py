"""Offscreen shared Excel password settings and backup checks."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit

from src.desktop_v2.export_password import (
    ExportPasswordController,
    ExportPasswordDialog,
)
from src.desktop_v2.export_password import (
    CURRENT_PASSWORD_ACCESS_MILLISECONDS,
    _clear_copied_password_if_current,
)
from src.domain.export_security import (
    EXPORT_PASSWORD_SETTING_ID,
    EXPORT_PASSWORD_SETTING_KIND,
    ExportPasswordSetting,
    export_password_setting_from_state,
)
from src.domain.revisions import (
    AuditChange,
    ChangeOperation,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.storage.database_session import DatabaseSession


BASE_REVISION_ID = UUID("6b000000-0000-0000-0000-000000000001")
BASE_ENTITY_ID = UUID("6b000000-0000-0000-0000-000000000002")
PASSWORD_REVISION_ID = UUID("6b000000-0000-0000-0000-000000000003")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _base_revision() -> RevisionRecord:
    return RevisionRecord(
        RevisionMetadata(
            revision_id=BASE_REVISION_ID,
            parent_revision_id=None,
            recorded_at=datetime(2026, 8, 26, 1, 0, tzinfo=UTC),
            effective_on=date(2026, 8, 1),
            actor=RevisionActor("tester", "S-1-5-21-test"),
            reason="Excel 설정 화면 기준 명단",
            source=RevisionSource.UI,
        ),
        (
            AuditChange(
                "export-password-ui-probe",
                BASE_ENTITY_ID,
                ChangeOperation.INSERT,
                before=None,
                after={"value": "base"},
            ),
        ),
    )


def test_dialog_masks_both_fields_and_rejects_mismatch(app: QApplication):
    dialog = ExportPasswordDialog(None)

    assert dialog.password_edit.echoMode() is QLineEdit.EchoMode.Password
    assert dialog.confirmation_edit.echoMode() is QLineEdit.EchoMode.Password
    assert "아직" in dialog._status.text()

    dialog.password_edit.setText("shared-1234")
    dialog.confirmation_edit.setText("different")
    dialog.save_button.click()
    app.processEvents()
    assert dialog.result() == 0
    assert "서로 다릅니다" in dialog._error.text()

    dialog.confirmation_edit.setText("shared-1234")
    dialog.save_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_dialog_reveals_and_copies_only_current_password_temporarily(
    app: QApplication,
):
    current = ExportPasswordSetting(
        version=2,
        password="current-shared-1234",
        changed_in_revision_id=PASSWORD_REVISION_ID,
    )
    dialog = ExportPasswordDialog(current)

    assert dialog.current_password_edit is not None
    assert dialog.reveal_current_password_button is not None
    assert dialog.copy_current_password_button is not None
    assert dialog.current_password_edit.isReadOnly()
    assert (
        dialog.current_password_edit.echoMode()
        is QLineEdit.EchoMode.Password
    )
    assert dialog._reveal_timer.isSingleShot()
    assert dialog._reveal_timer.interval() == CURRENT_PASSWORD_ACCESS_MILLISECONDS
    assert len(dialog.findChildren(QLineEdit)) == 3

    dialog.reveal_current_password_button.click()
    assert dialog.current_password_edit.echoMode() is QLineEdit.EchoMode.Normal
    assert dialog.current_password_edit.text() == "current-shared-1234"
    assert dialog.reveal_current_password_button.text() == "다시 가리기"

    dialog._reveal_timer.timeout.emit()
    assert dialog.current_password_edit.echoMode() is QLineEdit.EchoMode.Password
    assert dialog.reveal_current_password_button.text() == "현재 비밀번호 보기"

    clipboard = QApplication.clipboard()
    clipboard.clear()
    dialog.copy_current_password_button.click()
    assert clipboard.text() == "current-shared-1234"
    assert dialog.copy_current_password_button.text() == "복사됨"
    assert dialog._clipboard_copy_sequence is not None
    assert _clear_copied_password_if_current(
        dialog._clipboard_copy_sequence,
        "current-shared-1234",
    )
    assert clipboard.text() == ""
    dialog.close()


class _AcceptedDialog:
    def __init__(self, current, parent=None):
        del parent
        assert current is None

    @staticmethod
    def exec():
        return QDialog.DialogCode.Accepted

    @staticmethod
    def selected_password():
        return "shared-1234"


class _Messages:
    @staticmethod
    def critical(*args):
        raise AssertionError(args)

    @staticmethod
    def warning(*args):
        raise AssertionError(args)


class _UnexpectedDialog:
    def __init__(self, *_args, **_kwargs):
        raise AssertionError("이미 설정된 비밀번호에는 대화상자를 열면 안 됩니다.")


def test_controller_saves_password_revision_and_verified_backup_restores_it(
    app: QApplication,
    tmp_path: Path,
):
    del app
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    source = tmp_path / "operations.sqlite3"
    with DatabaseSession.create(
        source,
        "everyday-password",
        "institution-recovery",
        backup_directory=backup_directory,
    ) as session:
        session.append_revision(_base_revision())
        outcome = ExportPasswordController(
            session,
            dialog_type=_AcceptedDialog,
            actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
            recorded_at_provider=lambda: datetime(
                2026, 8, 26, 2, 0, tzinfo=UTC
            ),
            revision_id_factory=lambda: PASSWORD_REVISION_ID,
            message_box=_Messages,
        ).run()

        assert outcome.completed
        assert outcome.roster is not None
        assert outcome.roster.revision_id == PASSWORD_REVISION_ID
        assert outcome.setting is not None
        assert outcome.setting.password == "shared-1234"
        assert outcome.backup_result is not None
        assert outcome.backup_result.record is not None
        backup_path = outcome.backup_result.record.path

        unchanged = ExportPasswordController(
            session,
            dialog_type=_UnexpectedDialog,
            message_box=_Messages,
        ).ensure_configured()
        assert unchanged.completed
        assert unchanged.roster is not None
        assert unchanged.roster.revision_id == PASSWORD_REVISION_ID
        assert unchanged.setting == outcome.setting
        assert unchanged.backup_result is None

    restored_path = tmp_path / "restored.sqlite3"
    with DatabaseSession.restore_and_open_with_user_password(
        backup_path,
        restored_path,
        "everyday-password",
    ) as restored:
        snapshot = restored.database.snapshot(PASSWORD_REVISION_ID)
        state = snapshot.entity(
            EXPORT_PASSWORD_SETTING_KIND,
            EXPORT_PASSWORD_SETTING_ID,
        )
        setting = export_password_setting_from_state(state)
        assert setting is not None and setting.password == "shared-1234"
        assert setting.public_manifest()["protected"] is True
        restored.database.check_integrity()
