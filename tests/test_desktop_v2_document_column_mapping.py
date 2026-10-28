from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox

from src.desktop_v2.document_column_mapping import (
    DocumentColumnMappingController, DocumentColumnMappingDialog,
    DocumentColumnMappingStatus,
)
from src.domain.revisions import (
    AuditChange,
    ChangeOperation,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.services.document_column_mappings import MappingEditorSpec, load_mapping_overrides, mapping_editor_spec
from src.storage.database_session import DatabaseSession


BASE_ID = UUID("7c000000-0000-0000-0000-000000000001")
ENTITY_ID = UUID("7c000000-0000-0000-0000-000000000002")
EDIT_ID = UUID("7c000000-0000-0000-0000-000000000003")
SCOPES = tuple((p, d) for p in ("급식", "행복도시락") for d in ("invoice", "confirmation"))


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _spec() -> MappingEditorSpec:
    return MappingEditorSpec(
        defaults={"성명": "성명", "학교명": "학교명", "연번": "연번"},
        targets=("성명", "학교명", "연번"),
        source_options=("", "성명", "학교명", "custom:demo"),
        locked_targets=("연번",),
        target_labels={"성명": "성명", "학교명": "학교명", "연번": "연번"},
        source_labels={"": "비움", "성명": "성명", "학교명": "학교명", "custom:demo": "돌봄기관"},
    )


def _dialog(message_box=QMessageBox):
    return DocumentColumnMappingDialog(
        {scope: _spec() for scope in SCOPES},
        {scope: {} for scope in SCOPES},
        message_box=message_box,
    )


def test_dialog_defaults_custom_field_reset_and_locked_row(app):
    dialog = _dialog()
    assert dialog.desired_mapping() == {"성명": "성명", "학교명": "학교명"}
    assert "연번" not in dialog._combos
    assert any("자동 계산" in label.text() for label in dialog._scroll.widget().findChildren(QLabel))

    combo = dialog._combos["성명"]
    combo.setCurrentIndex(combo.findData("custom:demo"))
    assert combo.currentText() == "돌봄기관"
    assert dialog._indicators["성명"].text() == "별도 연결"
    dialog._reset_button.click()
    assert dialog.desired_mapping() == {"성명": "성명", "학교명": "학교명"}
    assert dialog._indicators["성명"].text() == "기본 연결"
    dialog._cancel_button.click()
    assert dialog.result() == QDialog.DialogCode.Rejected


class _SwitchMessages:
    answer = QMessageBox.StandardButton.Cancel
    calls = 0

    @classmethod
    def question(cls, *args):
        cls.calls += 1
        return cls.answer


def test_scope_switch_requires_explicit_discard_and_keeps_four_scopes(app):
    _SwitchMessages.calls = 0
    _SwitchMessages.answer = QMessageBox.StandardButton.Cancel
    dialog = _dialog(_SwitchMessages)
    combo = dialog._combos["성명"]
    combo.setCurrentIndex(combo.findData("학교명"))
    dialog._document_combo.setCurrentIndex(1)
    assert _SwitchMessages.calls == 1
    assert dialog.document_kind == "invoice"
    assert dialog.desired_mapping()["성명"] == "학교명"

    _SwitchMessages.answer = QMessageBox.StandardButton.Discard
    dialog._document_combo.setCurrentIndex(1)
    assert dialog.document_kind == "confirmation"
    assert dialog.desired_mapping()["성명"] == "성명"
    dialog._profile_combo.setCurrentIndex(1)
    assert dialog.selected_profile == "행복도시락"
    dialog._document_combo.setCurrentIndex(0)
    assert (dialog.selected_profile, dialog.document_kind) == ("행복도시락", "invoice")


def _base_revision() -> RevisionRecord:
    return RevisionRecord(
        RevisionMetadata(
            BASE_ID, None, datetime(2026, 9, 30, tzinfo=UTC), date(2026, 9, 1),
            RevisionActor("tester", "S-1-5-21-test"), "출력 연결 테스트 기준", RevisionSource.UI,
        ),
        (AuditChange("mapping-probe", ENTITY_ID, ChangeOperation.INSERT, before=None, after={"value": "base"}),),
    )


class _Messages:
    @staticmethod
    def critical(*args):
        raise AssertionError(args)

    @staticmethod
    def warning(*args):
        raise AssertionError(args)


class _AcceptedDialog:
    mapping = {"성명": "학교명", "학교명": "학교명"}

    def __init__(self, specs, overrides, parent=None, *, message_box=None, fallbacks=None):
        assert len(specs) == 4
        assert len(overrides) == 4

    def exec(self):
        return QDialog.DialogCode.Accepted

    selected_profile = "급식"
    document_kind = "confirmation"

    def desired_mapping(self):
        return self.mapping

    def desired_fallbacks(self):
        return {}


class _CancelledDialog(_AcceptedDialog):
    def exec(self):
        return QDialog.DialogCode.Rejected


class _ResetDialog(_AcceptedDialog):
    mapping = {"성명": "성명", "학교명": "학교명"}


def _controller(session, dialog_type, revision_id=EDIT_ID):
    return DocumentColumnMappingController(
        session, dialog_type=dialog_type, message_box=_Messages,
        actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
        recorded_at_provider=lambda: datetime(2026, 9, 30, 1, tzinfo=UTC),
        revision_id_factory=lambda: revision_id,
    )


def test_controller_save_reopen_cancel_and_noop_with_backup(app, tmp_path: Path, monkeypatch):
    from src.desktop_v2 import document_column_mapping as module

    monkeypatch.setattr(module, "mapping_editor_spec", lambda *args, **kwargs: _spec())
    # The domain revision builder accepts the synthetic confirmation defaults.
    from src.domain.document_column_mappings import build_mapping_revision as domain_build

    def build(current_id, snapshot, profile, document, mapping, **kwargs):
        kwargs.pop("roster", None)
        return domain_build(
            current_id, snapshot, profile, document, mapping,
            defaults={"성명": "성명", "학교명": "학교명"},
            allowed_sources=("", "성명", "학교명", "custom:demo"),
            locked_targets=("연번",), **kwargs,
        )

    monkeypatch.setattr(module, "build_mapping_revision", build)
    source = tmp_path / "operations.sqlite3"
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(source, "everyday-password", "institution-recovery", backup_directory=backups) as session:
        session.append_revision(_base_revision())
        original = session.database.current_roster().revision_id
        assert _controller(session, _CancelledDialog).run().status is DocumentColumnMappingStatus.CANCELLED
        assert session.database.current_roster().revision_id == original
        outcome = _controller(session, _AcceptedDialog).run()
        assert outcome.completed and outcome.changed
        assert outcome.roster.revision_id == EDIT_ID
        assert outcome.backup_result.record is not None
        backup_path = outcome.backup_result.record.path
        assert load_mapping_overrides(session.database.snapshot(EDIT_ID), "급식", "confirmation")["성명"] == "학교명"

        no_op = _controller(session, _AcceptedDialog, UUID("7c000000-0000-0000-0000-000000000004")).run()
        assert no_op.completed and not no_op.changed
        assert session.database.current_roster().revision_id == EDIT_ID

        reset_id = UUID("7c000000-0000-0000-0000-000000000005")
        reset = _controller(session, _ResetDialog, reset_id).run()
        assert reset.completed and reset.changed
        assert load_mapping_overrides(session.database.snapshot(reset_id), "급식", "confirmation") == {}

    restored_path = tmp_path / "restored.sqlite3"
    with DatabaseSession.restore_and_open_with_user_password(
        backup_path, restored_path, "everyday-password"
    ) as restored:
        assert load_mapping_overrides(restored.database.snapshot(EDIT_ID), "급식", "confirmation")["성명"] == "학교명"


def test_dialog_screenshots_default_and_small(app):
    dialog = DocumentColumnMappingDialog(
        {scope: mapping_editor_spec(*scope) for scope in SCOPES},
        {scope: {} for scope in SCOPES},
    )
    evidence = Path("artifacts/document-mapping-priority")
    evidence.mkdir(parents=True, exist_ok=True)
    dialog.show()
    app.processEvents()
    assert dialog.grab().save(str(evidence / "output_mapping_ui_default.png"))
    dialog.resize(630, 450)
    app.processEvents()
    assert dialog.grab().save(str(evidence / "output_mapping_ui_small.png"))
    assert dialog._save_button.isVisible()
    assert dialog._scroll.verticalScrollBar().maximum() > 0
    dialog.close()


@pytest.mark.parametrize("profile", ["급식", "행복도시락"])
def test_confirmation_address_and_dong_mapping_edit_reset_and_cancel(app, profile):
    specs = {scope: mapping_editor_spec(*scope) for scope in SCOPES}
    dialog = DocumentColumnMappingDialog(specs, {scope: {} for scope in SCOPES})
    dialog._profile_combo.setCurrentIndex(dialog._profile_combo.findData(profile))
    dialog._document_combo.setCurrentIndex(dialog._document_combo.findData("confirmation"))
    try:
        assert "상세주소" not in dialog._combos
        expected = {target: source for target, source in (("{addr}", "도로명주소"), ("{dong}", "관할동"))
                    if target in dialog._combos}
        assert "{addr}" in expected
        for target, source in expected.items():
            primary = dialog._combos[target]
            secondary = dialog._fallback_combos[target]
            primary.setCurrentIndex(primary.findData("비고"))
            secondary.setCurrentIndex(secondary.findData(source))
        assert all(dialog.desired_mapping()[target] == "비고" for target in expected)
        assert dialog.desired_fallbacks() == expected
        dialog._reset_button.click()
        assert all(dialog.desired_mapping()[target] == source for target, source in expected.items())
        assert not dialog.desired_fallbacks()
        dialog._cancel_button.click()
        assert dialog.result() == QDialog.DialogCode.Rejected
    finally:
        dialog.close()
