"""Conflict exports preserve source/DB values without committing GUI choices."""
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace

import msoffcrypto
import openpyxl
import pytest
from PySide6.QtWidgets import QApplication

from src.desktop_v2.current_sheet_export import CurrentSheetExportController
from src.desktop_v2.import_conflict_export import build_conflict_workbook, conflict_export_snapshot
from src.desktop_v2.import_value_review import ImportValueReviewDialog
from src.desktop_v2.lunch_value_review import LunchValueReviewDialog
from src.domain.roster import PersonProfile
from src.importing.values import conflicting_value_rows, import_value_rows
from tests.test_current_sheet_export import notices
from tests.test_lunch_value_review import scenario, PHONE


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def sample():
    return scenario(old=PersonProfile("합성 학생", mobile_phone=PHONE,
        household_head_name="기존 세대주", road_address="기존 주소", note="001",
        school_name="가상초등학교", grade="6"), incoming={
        "household_head_name": "새 세대주", "road_address": "=1+1\n둘째 줄",
        "note": "002", "school_name": "가상중학교", "grade": "1",
    })


def snapshot():
    current, plan, result = sample()
    rows = conflicting_value_rows(import_value_rows(current, result, plan))
    return conflict_export_snapshot(result, plan, rows)


def test_wide_comparison_contains_only_conflicts_and_original_source_location():
    captured = snapshot()
    sheet = captured.sheets[0]
    assert sheet.headers[:6] == ("원본 파일", "시트", "원본 행번호", "이름", "핸드폰", "세대주명")
    assert sheet.rows[0][:6] == ("synthetic.xlsx", "명단", "2", "합성 학생", PHONE, "새 세대주")
    values = dict(zip(sheet.headers, sheet.rows[0]))
    assert values["DB_비고"] == "001"
    assert values["명단_비고"] == "002"
    assert values["DB_세대주 성명"] == "기존 세대주"
    assert values["명단_세대주 성명"] == "새 세대주"
    assert "DB_학년" not in values and "DB_학교" not in values
    assert len(sheet.rows) == 1
    assert captured.sheets[1].sheet_name == "안내"


@pytest.mark.parametrize("dialog_type", [ImportValueReviewDialog, LunchValueReviewDialog])
def test_gui_export_keeps_unresolved_choices_and_direct_edits(app, dialog_type):
    current, plan, result = sample()
    dialog = dialog_type(current, result, plan)
    captured = []
    dialog.export_requested.connect(captured.append)
    try:
        dialog.export_button.click()
        assert len(captured) == 1 and not dialog._confirmed
        dialog._use_all_values(2)
        dialog._show_group_details("note")
        index = next(i for i, row in enumerate(dialog.rows) if row.field == "note")
        dialog.table.item(index, 4).setText("직접 수정")
        confirmed = set(dialog._confirmed)
        dialog.export_button.click()
        assert captured[1] == captured[0]
        assert dialog._confirmed == confirmed
        assert dialog.table.item(index, 4).text() == "직접 수정"
        dialog.accept()
        assert dialog.choices()[dialog.rows[index].key] == "직접 수정"
        assert current.persons[0].profile.note == "001"
    finally:
        dialog.close()


def test_duplicate_labels_do_not_overwrite_other_fields():
    current, plan, result = sample()
    rows = conflicting_value_rows(import_value_rows(current, result, plan))
    # A user column can share a presentation label; exported headers stay distinct.
    extra = replace(rows[0], field="custom:test", label=rows[0].label, before="000", incoming="=SUM(A1:A2)")
    captured = conflict_export_snapshot(result, plan, (*rows, extra))
    headers = captured.sheets[0].headers
    assert len(headers) == len(set(headers))
    assert any("사용자 항목" in header for header in headers)
    assert captured.sheets[0].rows[0][-2:] == ("000", "=SUM(A1:A2)")


def test_encrypted_export_preserves_text_highlights_and_two_sheets(app, tmp_path):
    target = tmp_path / "conflicts.xlsx"
    messages = notices()
    result = CurrentSheetExportController(message_box=messages,
        workbook_builder=build_conflict_workbook,
        file_dialog=SimpleNamespace(getSaveFileName=lambda *a, **k: (str(target), "")),
    ).run(snapshot(), "synthetic-password", directory=tmp_path)
    assert result is not None and result.password_protected
    assert not messages.errors
    with target.open("rb") as source:
        office = msoffcrypto.OfficeFile(source)
        assert office.is_encrypted()
        office.load_key(password="synthetic-password")
        plaintext = BytesIO()
        office.decrypt(plaintext)
    workbook = openpyxl.load_workbook(plaintext)
    try:
        assert workbook.sheetnames == ["불일치", "안내"]
        sheet = workbook["불일치"]
        assert sheet.freeze_panes == "G2"
        values = dict(zip((cell.value for cell in sheet[1]), sheet[2]))
        assert values["DB_비고"].value == "001"
        formula = values["명단_도로명주소"]
        assert formula.value == "=1+1 둘째 줄" and formula.data_type == "s"
        assert formula.fill.fgColor.rgb.endswith("FFF1CC")
    finally:
        workbook.close()


@pytest.mark.parametrize("cancel", [True, False])
def test_cancel_or_failed_export_preserves_existing_file(app, tmp_path, cancel):
    target = tmp_path / "conflicts.xlsx"
    target.write_bytes(b"existing file")
    messages = notices()
    from PySide6.QtWidgets import QMessageBox
    messages.question = lambda *a: QMessageBox.StandardButton.Yes

    def fail(*args, **kwargs):
        raise OSError("synthetic write failure")

    result = CurrentSheetExportController(message_box=messages,
        workbook_builder=fail,
        file_dialog=SimpleNamespace(getSaveFileName=lambda *a, **k: ("" if cancel else str(target), "")),
    ).run(snapshot(), "synthetic-password", directory=tmp_path)
    assert result is None and target.read_bytes() == b"existing file"
    assert bool(messages.errors) is not cancel


def test_sparse_conflicts_keep_source_order_and_multiple_source_rows():
    from uuid import uuid4
    from src.importing.application import apply_reconciliation
    from src.importing.contracts import ProfileField, SourceReference
    from src.importing.reconciliation import stage_reconciliation
    from tests.test_import_identity_review import roster, observation, DAY
    current = roster()
    current = replace(current, persons=(
        replace(current.persons[0], profile=replace(current.persons[0].profile, note="옛 메모")),
        replace(current.persons[1], profile=replace(current.persons[1].profile, road_address="옛 주소")),
    ))
    first = observation(phone=current.persons[0].profile.mobile_phone)
    first = replace(first, profile_values={**first.profile_values, ProfileField.NOTE: "새 메모"},
        source=SourceReference("synthetic.xlsx", "명단", (2, 5)))
    second = observation(name="자동연결", phone=current.persons[1].profile.mobile_phone, head="다른보호자", row=3)
    second = replace(second, profile_values={**second.profile_values, ProfileField.ROAD_ADDRESS: "새 주소"})
    plan = stage_reconciliation(current, (second, first), effective_on=DAY)
    result = apply_reconciliation(current, plan, new_revision_id=uuid4())
    rows = conflicting_value_rows(import_value_rows(current, result, plan))
    sheet = conflict_export_snapshot(result, plan, rows).sheets[0]
    first_export, second_export = (dict(zip(sheet.headers, row)) for row in sheet.rows)
    assert first_export["이름"] == "자동연결" and second_export["원본 행번호"] == "2, 5"
    assert first_export["DB_비고"] == first_export["명단_비고"] == ""
    assert second_export["DB_도로명주소"] == second_export["명단_도로명주소"] == ""


def test_controller_connects_export_and_cancel_leaves_database_unchanged(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from src.desktop_v2.import_controller import GuidedRosterImportController, GuidedImportStatus
    from src.importing.contracts import ProfileField
    from tests.test_desktop_v2_application import _selection, _wizard_type, _session, _Messages
    selection = _selection(tmp_path)
    obs = selection.preview.observations[0]
    original = replace(obs, profile_values={**obs.profile_values, ProfileField.NOTE: "기존 메모"})
    selection = replace(selection, preview=replace(selection.preview, observations=(original,)))
    incoming = replace(original, profile_values={**original.profile_values, ProfileField.NOTE: "새 메모"})
    updated = replace(selection, preview=replace(selection.preview, observations=(incoming,)))
    exported = []

    class Review(ImportValueReviewDialog):
        def exec(self):
            self.export_button.click()
            assert not self._confirmed
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(GuidedRosterImportController, "_export_value_conflicts",
        lambda self, dialog, captured: exported.append(captured))
    _Messages.reset()
    with _session(tmp_path) as session:
        first = GuidedRosterImportController(session, wizard_type=_wizard_type(selection), message_box=_Messages).run()
        assert first.completed
        before = session.database.latest_revision().metadata.revision_id
        outcome = GuidedRosterImportController(session, wizard_type=_wizard_type(updated),
            import_value_review_type=Review, message_box=_Messages).run()
        assert outcome.status is GuidedImportStatus.CANCELLED
        assert len(exported) == 1
        assert session.database.latest_revision().metadata.revision_id == before
        assert not _Messages.critical_calls
