"""Tile placement, wrapping, drag scrolling, and document priority regressions."""
import os
from types import SimpleNamespace
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QPointF, Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget

from src.desktop_v2.import_columns import ImportColumnsTable, _MIME
from src.importing.workbook import SourceColumn
from src.services import import_column_priority as priority
from tests.test_import_database_columns import roster, source, wizard_for


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def board(app):
    widget = ImportColumnsTable()
    widget.set_document_targets({"profile:mobile_phone"})
    inspection = SimpleNamespace(columns=(SourceColumn(1, "A", "성명"), SourceColumn(2, "B", "연락처"), SourceColumn(3, "C", "보호자")),
        preview_rows=(SimpleNamespace(values=("합성 아동", "010-0000-0000", "합성 보호자")),))
    targets = [("ignore", "가져오지 않음"), ("new", "새 칼럼 추가"), ("profile:name", "성명"),
               ("profile:mobile_phone", "핸드폰"), ("meal:combined", "식종")]
    targets += [(f"custom:{i}", f"추가 칼럼 {i}") for i in range(15)]
    widget.populate(inspection, targets, {"A": "profile:name", "B": "profile:mobile_phone", "C": "ignore"})
    widget.resize(760, 700)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_preposition_priority_replacement_unmap_and_new_column(board):
    assert board.section_layouts["document"].itemAt(0).widget() is board.cards["profile:mobile_phone"]
    assert board.cards["meal:combined"].parentWidget() is board.sections["meal"]
    assert board.cards["profile:name"].parentWidget() is board.sections["other"]
    assert board.source_tiles["A"].parentWidget() is board.cards["profile:name"]
    changes = []
    board.destination_changed.connect(lambda *args: changes.append(args))
    assert board.assign_source("C", "profile:mobile_phone")
    assert changes == [("B", "ignore"), ("C", "profile:mobile_phone")]
    assert board.source_tiles["B"].parentWidget() is board.pool
    board.select_source("C")
    board.pool.heading.click()
    assert board._selected["C"] == "ignore"
    board.select_source("B")
    board.new_area.heading.click()
    assert board._selected["B"] == "new"
    assert board.source_tiles["B"].parentWidget() is board.new_area


def test_real_drop_event_accepts_local_tiles_and_rejects_foreign_payload(board, app):
    target = board.cards["profile:mobile_phone"]
    for owner, accepted in (("foreign-board", False), (board.drag_id, True)):
        mime = QMimeData()
        mime.setData(_MIME, (owner + ":C").encode())
        enter = QDragEnterEvent(QPoint(20, 20), Qt.DropAction.MoveAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        app.sendEvent(target, enter)
        assert enter.isAccepted() is accepted
        if accepted:
            drop = QDropEvent(QPointF(20, 20), Qt.DropAction.MoveAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            app.sendEvent(target, drop)
            assert drop.isAccepted()
    assert board._selected["C"] == "profile:mobile_phone"
    assert board._selected["B"] == "ignore"


def test_wrap_and_drag_scroll_to_hidden_rows(board, app):
    def rows():
        return len({item.widget().y() for item in board.section_layouts["other"].items})
    wide_rows = rows()
    board.resize(470, 700)
    for _ in range(3):
        app.processEvents()
    assert rows() > wide_rows
    assert board.scroll.horizontalScrollBar().maximum() == 0
    bar = board.scroll.verticalScrollBar()
    assert bar.maximum() > 0
    viewport = board.scroll.viewport()
    bottom = viewport.mapToGlobal(QPoint(20, viewport.height() - 5))
    board._scroll_drag_at(bottom)
    assert bar.value() > 0
    top = viewport.mapToGlobal(QPoint(20, 5))
    board._scroll_drag_at(top)
    assert bar.value() == 0


def test_drag_scroll_reaches_pool_below_outer_viewport(board, app):
    outer = QScrollArea()
    outer.setWidgetResizable(True)
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.addWidget(board)
    outer.setWidget(container)
    outer.resize(600, 350)
    outer.show()
    for _ in range(5):
        app.processEvents()
    viewport = outer.viewport()
    point = viewport.mapToGlobal(QPoint(viewport.width() - 3, viewport.height() - 3))
    board._scroll_drag_at(point)
    assert outer.verticalScrollBar().value() > 0
    board.setParent(None)
    outer.close()
    outer.deleteLater()


def test_lunch_disabled_slots_and_saved_sync(board):
    board.set_meal_targets_enabled(False)
    assert not board.assign_source("C", "meal:combined")
    assert board._selected["C"] == "ignore"
    board.set_meal_targets_enabled(True)
    assert board.assign_source("C", "meal:combined")
    board.sync({"A": "profile:name", "B": "new", "C": "ignore"})
    assert board.source_tiles["B"].parentWidget() is board.new_area
    assert board.source_tiles["C"].parentWidget() is board.pool


def test_tile_replacement_updates_real_import_profile(app, tmp_path):
    current = roster()
    wizard = wizard_for(source(tmp_path / "source.xlsx"), current)
    board = wizard.destination_table
    key = "custom:" + str(current.custom_fields[0].definition_id)
    board.assign_source("D", key)
    board.assign_source("H", key)
    profile = wizard._build_profile()
    assert profile.custom_field_columns[current.custom_fields[0].definition_id] == "H"
    assert "D" in profile.ignored_columns
    board.assign_source("H", "ignore")
    assert current.custom_fields[0].definition_id not in wizard._build_profile().custom_field_columns
    wizard.close()
    wizard.deleteLater()


def test_document_priority_uses_effective_mapping_and_fallbacks(monkeypatch):
    monkeypatch.setattr(priority, "document_template_setting_from_snapshot", lambda snapshot: SimpleNamespace(profile=SimpleNamespace(value="급식")))
    specs = {
        "invoice": SimpleNamespace(defaults={"이름": "성명", "주소": priority.FULL_ADDRESS_FIELD, "메모": "비고"}, targets=("이름", "주소", "메모")),
        "confirmation": SimpleNamespace(defaults={"연락처": "핸드폰"}, targets=("연락처",)),
    }
    monkeypatch.setattr(priority, "mapping_editor_spec", lambda profile, kind, **kwargs: specs[kind])
    monkeypatch.setattr(priority, "load_mapping_overrides", lambda snapshot, profile, kind: {"메모": "", "이름": "custom:stable-id", "삭제된 항목": "학교명"} if kind == "invoice" else {})
    monkeypatch.setattr(priority, "load_mapping_fallbacks", lambda snapshot, profile, kind: {"연락처": "전화번호", "삭제된 항목": "학년"} if kind == "confirmation" else {})
    targets, unavailable = priority.document_import_targets(object())
    assert not unavailable
    assert targets == {"custom:stable-id", "profile:road_address", "profile:detail_address", "profile:mobile_phone", "profile:telephone"}


def test_unavailable_template_preserves_saved_priority_without_blocking(monkeypatch):
    monkeypatch.setattr(priority, "document_template_setting_from_snapshot", lambda snapshot: SimpleNamespace(profile=SimpleNamespace(value="급식")))
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("missing template")
    monkeypatch.setattr(priority, "mapping_editor_spec", unavailable)
    monkeypatch.setattr(priority, "load_mapping_overrides", lambda *args: {"연락처": "핸드폰"})
    monkeypatch.setattr(priority, "load_mapping_fallbacks", lambda *args: {})
    assert priority.document_import_targets(object()) == (frozenset({"profile:mobile_phone"}), ("청구서", "확인서"))


def test_no_document_profile_has_no_invented_priority(monkeypatch):
    monkeypatch.setattr(priority, "document_template_setting_from_snapshot", lambda snapshot: None)
    assert priority.document_import_targets(object()) == (frozenset(), ())


def test_current_templates_and_legacy_confirmation_aliases(monkeypatch):
    from uuid import uuid4
    from src.domain.revisions import HistoricalSnapshot, EntityRef
    from src.domain.document_templates import (
        DOCUMENT_TEMPLATE_SETTING_ID, DOCUMENT_TEMPLATE_SETTING_KIND,
        DocumentTemplateProfile, DocumentTemplateSetting, document_template_setting_state,
    )
    revision = uuid4()
    setting = DocumentTemplateSetting(1, DocumentTemplateProfile.SCHOOL_MEAL, revision)
    snapshot = HistoricalSnapshot(revision, {
        EntityRef(DOCUMENT_TEMPLATE_SETTING_KIND, DOCUMENT_TEMPLATE_SETTING_ID): document_template_setting_state(setting),
    })
    # Older configurations named fields instead of the current {phone1} placeholders.
    monkeypatch.setattr(priority, "load_mapping_overrides", lambda snapshot, profile, kind:
                        {"핸드폰": "custom:legacy-contact"} if kind == "confirmation" else {})
    monkeypatch.setattr(priority, "load_mapping_fallbacks", lambda snapshot, profile, kind:
                        {"핸드폰": "custom:legacy-fallback"} if kind == "confirmation" else {})
    targets, unavailable = priority.document_import_targets(snapshot)
    assert not unavailable
    assert {"profile:name", "profile:road_address", "custom:legacy-contact", "custom:legacy-fallback"} <= targets


def test_mapping_opens_only_on_request_and_preserves_edits_on_close(app, tmp_path):
    wizard = wizard_for(source(tmp_path / "새 명단.xlsx"), roster())
    wizard._set_page(1)
    wizard.show()
    app.processEvents()
    assert not wizard.mapping_dialog.isVisible()
    assert not wizard.destination_table.isVisible()
    assert not hasattr(wizard, "profile_name_edit")
    assert wizard._build_profile().name == "새 명단"
    wizard.mapping_open_button.click()
    app.processEvents()
    assert wizard.mapping_dialog.isVisible()
    assert wizard.mapping_dialog.isMaximized()
    assert wizard.destination_table.isVisible()
    wizard.destination_table.assign_source("H", "new")
    expected = wizard._build_profile()
    summary = wizard.mapping_summary.text()
    wizard.mapping_dialog.close()
    app.processEvents()
    assert not wizard.mapping_dialog.isVisible()
    assert wizard.isVisible()
    assert wizard._build_profile() == expected
    assert wizard.mapping_summary.text() == summary
    wizard.mapping_open_button.click()
    app.processEvents()
    assert wizard._build_profile() == expected
    wizard.mapping_dialog.done_button.click()
    assert not wizard.mapping_dialog.isVisible()
    wizard.mapping_open_button.click()
    wizard.reject()
    assert not wizard.mapping_dialog.isVisible()
    wizard.deleteLater()


def test_mapping_dialog_closes_when_leaving_step(app, tmp_path):
    wizard = wizard_for(source(tmp_path / "명단.xlsx"), roster())
    wizard._set_page(1)
    wizard.show()
    wizard._open_mapping_dialog()
    app.processEvents()
    wizard._set_page(0)
    assert not wizard.mapping_dialog.isVisible()
    wizard._set_page(1)
    wizard._open_mapping_dialog()
    wizard._set_busy("검사", "명단 검사")
    assert not wizard.mapping_dialog.isVisible()
    wizard._clear_busy()
    wizard.close()
    wizard.deleteLater()


@pytest.mark.parametrize("modal", (True, False), ids=("first-import-exec", "existing-import"))
def test_mapping_mouse_input_under_initial_import_modal(app, tmp_path, modal):
    from PySide6.QtCore import QTimer
    from PySide6.QtTest import QTest
    from src.desktop_v2.import_wizard import RosterImportWizard
    from src.importing.contracts import ProfileField

    path = source(tmp_path / "initial.xlsx", headers=("성명", "생년월일", "식종"),
                  rows=(("합성 아동", "20150302", "석식"),))
    wizard = RosterImportWizard()
    wizard.set_database_columns(None)
    wizard.set_initial_import_defaults()
    wizard.path_edit.setText(str(path))
    assert wizard._inspect_source()
    wizard._set_page(1)
    failures = []
    completed = []

    def exercise():
        try:
            assert wizard.isModal() is modal
            wizard.mapping_open_button.click()
            QTest.qWait(50)
            dialog = wizard.mapping_dialog
            handle = dialog.windowHandle()
            assert handle.transientParent() == (wizard.windowHandle() if modal else None)
            board = wizard.destination_table
            tile = board.source_tiles["B"]
            board.scroll.ensureWidgetVisible(tile)
            dialog.scroll.ensureWidgetVisible(board)
            app.processEvents()
            # Use QWindow events so Qt's modal blocking participates in the test.
            QTest.mouseClick(handle, Qt.MouseButton.LeftButton, pos=tile.mapTo(dialog, tile.rect().center()))
            assert tile.isChecked()
            target = board.cards["profile:mobile_phone"].heading
            board.scroll.ensureWidgetVisible(target)
            app.processEvents()
            QTest.mouseClick(handle, Qt.MouseButton.LeftButton, pos=target.mapTo(dialog, target.rect().center()))
            assert wizard._build_profile().profile_columns[ProfileField.MOBILE_PHONE] == "B"
            QTest.mouseClick(handle, Qt.MouseButton.LeftButton,
                             pos=dialog.done_button.mapTo(dialog, dialog.done_button.rect().center()))
            assert not dialog.isVisible()
            assert wizard._mapping_review_complete()
            wizard._open_mapping_dialog()
            app.processEvents()
            QTest.keyClick(dialog.windowHandle(), Qt.Key.Key_Escape)
            assert not dialog.isVisible()
            completed.append(True)
        except BaseException as error:
            failures.append(error)
        finally:
            wizard.reject()

    if modal:
        QTimer.singleShot(0, exercise)
        wizard.exec()
    else:
        wizard.setModal(False)
        wizard.show()
        app.processEvents()
        exercise()
    wizard.deleteLater()
    if failures:
        raise failures[0]
    assert completed == [True]
