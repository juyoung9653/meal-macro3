"""Merged editable samples and safe fallback to the last validated workbook."""
from hashlib import sha256
from pathlib import Path

import openpyxl
import pytest
from PySide6.QtCore import QSettings

from src.desktop_v2.delivery_template import DeliveryTemplateStore, DeliveryTemplateDialog
from src.desktop_v2.document_hub import DocumentAction
from src.desktop_v2.document_generation import DocumentGenerationController
from src.excel.delivery_templates import create_substitute_template, validate_delivery_template
from src.excel.substitute_template_columns import GROUP_ROWS, template_column_keys, expected_template_merges
from src.excel.publication import publish_workbook
from src.services.delivery_template_backup import DeliveryTemplateBackup
from tests.test_delivery_substitute_v2 import app, session, selection_for, Messages


def corrupt(path, kind):
    if kind == "zip":
        path.write_bytes(b"not an Excel workbook")
        return
    if kind == "missing":
        path.unlink()
        return
    book = openpyxl.load_workbook(path)
    sheet = book["배송대타"]
    if kind == "merge":
        sheet.unmerge_cells("A4:A5")
    elif kind == "row":
        sheet.delete_rows(9)
    elif kind == "width":
        sheet.column_dimensions["A"].width = 300
    elif kind == "height":
        sheet.row_dimensions[4].height = 500
    elif kind == "sheet":
        sheet.title = "changed"
    book.save(path)
    book.close()


def test_one_to_five_templates_show_actual_household_merges(tmp_path):
    path = tmp_path / "form.xlsx"
    create_substitute_template(path)
    validate_delivery_template(path, "delivery_substitute")
    book = openpyxl.load_workbook(path)
    try:
        sheet = book["배송대타"]
        keys = template_column_keys(sheet)
        assert {str(merged) for merged in sheet.merged_cells.ranges} == expected_template_merges(keys)
        for start, end in GROUP_ROWS:
            for row in range(start, end + 1):
                assert sheet.cell(row, keys.index("name") + 1).value == "[성명]"
    finally:
        book.close()


@pytest.mark.parametrize("kind", ["zip", "missing", "merge", "row", "width", "height", "sheet"])
def test_invalid_edits_use_last_good_without_overwriting_invalid_file(tmp_path, kind):
    path = tmp_path / "form.xlsx"
    create_substitute_template(path)
    backup = DeliveryTemplateBackup(tmp_path / "store")
    initial = backup.resolve(path)
    assert not initial.used_previous
    # A later valid save, not the factory default, becomes the fallback.
    book = openpyxl.load_workbook(path)
    book["배송대타"].row_dimensions[13].height = 37
    book.save(path)
    book.close()
    latest = backup.resolve(path).contents
    assert latest != initial.contents
    corrupt(path, kind)
    damaged = path.read_bytes() if path.exists() else None
    resolved = backup.resolve(path)
    assert resolved.used_previous and resolved.contents == latest
    assert "이전 정상 저장본" in resolved.warning
    assert (path.read_bytes() if path.exists() else None) == damaged
    assert backup.backup_path(path).read_bytes() == latest
    assert backup.resolve(path).contents == latest


def test_no_previous_valid_template_reports_failure(tmp_path):
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"broken")
    with pytest.raises(ValueError, match="이전 정상 저장본도 없습니다"):
        DeliveryTemplateBackup(tmp_path / "store").resolve(path)


def test_backups_do_not_cross_template_paths_or_store_scopes(tmp_path):
    first, second = tmp_path / "one.xlsx", tmp_path / "two.xlsx"
    create_substitute_template(first)
    store = DeliveryTemplateBackup(tmp_path / "school")
    store.remember(first)
    second.write_bytes(b"broken")
    with pytest.raises(ValueError):
        store.resolve(second)
    first.write_bytes(b"broken")
    with pytest.raises(ValueError):
        DeliveryTemplateBackup(tmp_path / "happy").resolve(first)
    assert store.resolve(first).used_previous


def test_output_uses_previous_bytes_records_hash_and_warns_on_ui_thread(app, session, tmp_path, monkeypatch):
    import threading
    import src.desktop_v2.delivery_template as module
    path = tmp_path / "form.xlsx"
    create_substitute_template(path)
    store = DeliveryTemplateStore(str(session.credentials.database_id),
        settings=QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat), root=tmp_path / "forms")
    store.save(DocumentAction.DELIVERY_SUBSTITUTE, path)
    previous = path.read_bytes()
    corrupt(path, "merge")
    broken = path.read_bytes()
    monkeypatch.setattr(module, "DeliveryTemplateStore", lambda *args, **kwargs: store)
    received = []
    def writer(data, options, output, *, template_path, **kwargs):
        received.append(template_path.read_bytes())
        book = openpyxl.Workbook()
        book.active["A1"] = "synthetic"
        return publish_workbook(book, output, password=kwargs["output_password"], overwrite=False)
    messages = Messages()
    threads = []
    original_warning = messages.warning
    def warning(*args):
        threads.append(threading.get_ident())
        original_warning(*args)
    messages.warning = warning
    result = DocumentGenerationController(session, message_box=messages, substitute_writer=writer).run(selection_for(session, tmp_path))
    assert result.completed, messages.errors
    assert received == [previous]
    assert path.read_bytes() == broken
    assert len(messages.warnings) == 1 and "이전 정상" in str(messages.warnings[0])
    assert threads == [threading.get_ident()]
    manifest = session.database.document_runs()[0].template_manifest
    assert manifest["template_used_previous_valid"] is True
    assert manifest["template_sha256"] == sha256(previous).hexdigest()


def test_editor_recovers_a_copy_and_warns_without_destroying_bad_source(app, tmp_path, monkeypatch):
    import src.desktop_v2.delivery_template as module
    store = DeliveryTemplateStore("synthetic", settings=QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat), root=tmp_path / "forms")
    source = store.create(DocumentAction.DELIVERY_SUBSTITUTE)
    store.save(DocumentAction.DELIVERY_SUBSTITUTE, source)
    corrupt(source, "zip")
    warnings, opened = [], []
    monkeypatch.setattr(module.QMessageBox, "warning", lambda *args: warnings.append(args))
    dialog = DeliveryTemplateDialog(DocumentAction.DELIVERY_SUBSTITUTE, "synthetic", store=store,
                                    opener=lambda url: opened.append(url) or True)
    dialog._open()
    assert warnings and opened
    assert dialog.path != source
    validate_delivery_template(dialog.path, "delivery_substitute")
    assert source.read_bytes() == b"not an Excel workbook"
    dialog.accept()
    assert store.selected(DocumentAction.DELIVERY_SUBSTITUTE) == dialog.path
