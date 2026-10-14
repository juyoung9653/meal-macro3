from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.document_template import (
    DocumentTemplateController,
    DocumentTemplateDialog,
)
from src.domain.document_templates import (
    DOCUMENT_TEMPLATE_SETTING_ID,
    DOCUMENT_TEMPLATE_SETTING_KIND,
    DocumentTemplateProfile,
    document_template_setting_from_state,
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


BASE_REVISION_ID = UUID("6c000000-0000-0000-0000-000000000001")
BASE_ENTITY_ID = UUID("6c000000-0000-0000-0000-000000000002")
SETTING_REVISION_ID = UUID("6c000000-0000-0000-0000-000000000003")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _base_revision() -> RevisionRecord:
    return RevisionRecord(
        RevisionMetadata(
            BASE_REVISION_ID,
            None,
            datetime(2026, 8, 31, 1, 0, tzinfo=UTC),
            date(2026, 8, 1),
            RevisionActor("tester", "S-1-5-21-test"),
            "문서 양식 테스트 기준",
            RevisionSource.UI,
        ),
        (
            AuditChange(
                "document-template-probe",
                BASE_ENTITY_ID,
                ChangeOperation.INSERT,
                before=None,
                after={"value": "base"},
            ),
        ),
    )


def test_first_use_dialog_requires_one_explicit_profile(app: QApplication):
    dialog = DocumentTemplateDialog(None)

    assert not dialog._save_button.isEnabled()
    with pytest.raises(ValueError, match="선택"):
        _ = dialog.selected_profile

    dialog._buttons[DocumentTemplateProfile.HAPPY_LUNCHBOX].click()
    app.processEvents()
    assert dialog._save_button.isEnabled()
    assert dialog.selected_profile is DocumentTemplateProfile.HAPPY_LUNCHBOX
    dialog._save_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted


class _AcceptedDialog:
    def __init__(self, current, parent=None):
        del parent
        assert current is None

    @staticmethod
    def exec():
        return QDialog.DialogCode.Accepted

    selected_profile = DocumentTemplateProfile.HAPPY_LUNCHBOX


class _Messages:
    @staticmethod
    def critical(*args):
        raise AssertionError(args)

    @staticmethod
    def warning(*args):
        raise AssertionError(args)


def test_controller_saves_db_scoped_profile_and_automatic_backup(
    app: QApplication,
    tmp_path: Path,
):
    del app
    backups = tmp_path / "backups"
    backups.mkdir()
    source = tmp_path / "operations.sqlite3"
    with DatabaseSession.create(
        source,
        "everyday-password",
        "institution-recovery",
        backup_directory=backups,
    ) as session:
        session.append_revision(_base_revision())
        outcome = DocumentTemplateController(
            session,
            dialog_type=_AcceptedDialog,
            actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
            recorded_at_provider=lambda: datetime(
                2026, 8, 31, 2, 0, tzinfo=UTC
            ),
            revision_id_factory=lambda: SETTING_REVISION_ID,
            message_box=_Messages,
        ).ensure_configured()

        assert outcome.completed and outcome.changed
        assert outcome.setting is not None
        assert outcome.setting.profile is DocumentTemplateProfile.HAPPY_LUNCHBOX
        assert outcome.backup_result is not None
        assert outcome.backup_result.record is not None
        backup_path = outcome.backup_result.record.path

    restored_path = tmp_path / "restored.sqlite3"
    with DatabaseSession.restore_and_open_with_user_password(
        backup_path,
        restored_path,
        "everyday-password",
    ) as restored:
        state = restored.database.snapshot(SETTING_REVISION_ID).entity(
            DOCUMENT_TEMPLATE_SETTING_KIND,
            DOCUMENT_TEMPLATE_SETTING_ID,
        )
        setting = document_template_setting_from_state(state)
        assert setting is not None
        assert setting.profile is DocumentTemplateProfile.HAPPY_LUNCHBOX
        assert setting.public_manifest()["version"] == 1
        restored.database.check_integrity()
