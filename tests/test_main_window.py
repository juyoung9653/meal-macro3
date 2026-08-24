"""MainWindow의 연간 장부 로딩과 탭 배포 통합 테스트."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import msoffcrypto
import openpyxl
import pytest
from msoffcrypto.format.ooxml import OOXMLFile
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit

from src.excel.annual_workbook import TABLE_NAME, AnnualWorkbook
from src.main_window import MainWindow, _db_mapping_cache_headers
from src.widgets.db_header_mapping_dialog import DbImportSelection


@pytest.fixture
def qapp(tmp_path: Path):
    previous_format = QSettings.defaultFormat()
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path / "settings"),
    )
    settings = QSettings("분당구", "급식비 정산 프로그램")
    settings.clear()
    settings.sync()
    app = QApplication.instance() or QApplication([])
    yield app
    settings.clear()
    settings.sync()
    QSettings.setDefaultFormat(previous_format)


def _write_annual_workbook(
    path: Path,
    *,
    control_headers: tuple[str, str, str] = ("식종", "배송담당", "연번"),
    date_headers: tuple[str, ...] = ("20260728",),
) -> None:
    workbook = openpyxl.Workbook()
    roster = workbook.active
    assert roster is not None
    roster.title = "선택 파일 데이터"
    roster.append(
        [
            "성명",
            "관할동",
            "도로명주소",
            *control_headers,
            *date_headers,
        ]
    )
    profile = ["홍길동", "정자동", "성남대로 1"]
    blank_dates = [None] * len(date_headers)
    roster.append([*profile, "조식", "기사A", "001", *blank_dates])
    roster.append([*profile, "중식", "기사A", "001", *([1] * len(date_headers))])
    roster.append([*profile, "석식", "기사A", "001", *blank_dates])
    last_column = get_column_letter(6 + len(date_headers))
    roster.add_table(Table(displayName=TABLE_NAME, ref=f"A1:{last_column}4"))
    workbook.save(path)
    workbook.close()


def _save_last_db_path(path: Path) -> None:
    settings = QSettings("분당구", "급식비 정산 프로그램")
    settings.setValue("annual_workbook/last_path", str(path))
    settings.sync()
    assert settings.status() is QSettings.Status.NoError


def _save_db_header_mapping(mapping: dict[str, str]) -> None:
    settings = QSettings("분당구", "급식비 정산 프로그램")
    settings.setValue("annual_workbook/header_mapping", mapping)
    settings.sync()
    assert settings.status() is QSettings.Status.NoError


def _mapping_subset(mapping: dict[str, str], roles: dict[str, str]) -> dict[str, str]:
    return {role: mapping[role] for role in roles}


def _encrypt_workbook(
    plain_path: Path, encrypted_path: Path, *, password: str
) -> Path:
    with plain_path.open("rb") as plain, encrypted_path.open("wb") as encrypted:
        office_file = msoffcrypto.OfficeFile(plain)
        assert isinstance(office_file, OOXMLFile)
        office_file.encrypt(password, encrypted)
    return encrypted_path


def test_valid_annual_file_is_loaded_once_and_shared_by_all_tabs(
    qapp: QApplication, tmp_path: Path
):
    path = tmp_path / "2026_delivery.xlsx"
    _write_annual_workbook(path)
    window = MainWindow()
    try:
        assert window._load_annual_file(str(path)) is True
        book = window._annual_book
        assert book is not None
        assert len(book.roster) == 1
        person = book.roster[0]
        assert person.source_row == 2
        assert person.extra["성명"] == "홍길동"
        assert "ID" not in person.extra
        person_rows = book.rows_by_person[person]
        assert tuple(row.source_row for row in person_rows) == (2, 3, 4)
        assert all(row.person is person for row in person_rows)
        assert window._annual_workbook is not None
        assert window.invoice_tab._book is book
        assert window.review_tab._book is book
        assert window.report_tab._book is book
        assert window.delivery_tab._book is book
        assert window.invoice_tab.month_combo.currentData() == 7
        assert window.invoice_tab.generate_btn.isEnabled()
        assert window.report_tab.build_btn.isEnabled()
        assert window.delivery_tab.run_btn.isEnabled()
        assert "2026년 · 1명" in window.roster_label.text()
        assert window._s.get("annual_workbook", "path") == str(path)
        assert window._preferences.get_annual_workbook_path() == str(path)
    finally:
        window.close()


def test_common_db_controls_are_above_four_separate_tool_buttons(
    qapp: QApplication,
):
    window = MainWindow()
    try:
        central = window.centralWidget()
        assert central is not None
        layout = central.layout()
        assert layout is not None
        assert layout.indexOf(window.work_group) == 1
        assert list(window.work_titles) == [
            "청구서 생성",
            "확인서 생성",
            "명단 합치기",
            "검토하기",
        ]
        assert [button.text() for button in window.work_buttons] == ["열기  ›"] * 4
        assert len(window.work_item_frames) == 4
        assert not any(button.isEnabled() for button in window.work_buttons)
        assert window.invoice_tab.parent() is window.invoice_window
        assert window.confirm_tab.parent() is window.confirm_window
        assert window.merge_tab.parent() is window.merge_window
        assert window.review_tab.parent() is window.review_window

        for button in window.work_buttons:
            button.setEnabled(True)
        windows = (
            window.invoice_window,
            window.confirm_window,
            window.merge_window,
            window.review_window,
        )
        for button, tool_window in zip(window.work_buttons, windows, strict=True):
            with (
                patch.object(tool_window, "show") as show,
                patch.object(tool_window, "raise_") as raise_window,
                patch.object(tool_window, "activateWindow") as activate,
            ):
                button.click()
            show.assert_called_once_with()
            raise_window.assert_called_once_with()
            activate.assert_called_once_with()
    finally:
        window.close()


def test_invalid_replacement_preserves_the_previously_loaded_book(
    qapp: QApplication, tmp_path: Path
):
    valid_path = tmp_path / "valid.xlsx"
    invalid_path = tmp_path / "invalid.xlsx"
    _write_annual_workbook(valid_path)
    openpyxl.Workbook().save(invalid_path)

    window = MainWindow()
    try:
        assert window._load_annual_file(str(valid_path)) is True
        original_book = window._annual_book
        original_workbook = window._annual_workbook

        with patch("src.main_window.QMessageBox.critical") as critical:
            assert window._load_annual_file(str(invalid_path)) is False

        critical.assert_called_once()
        assert window._annual_book is original_book
        assert window._annual_workbook is original_workbook
        assert window.invoice_tab._book is original_book
        assert window.review_tab._book is original_book
        assert window.report_tab._book is original_book
        assert window.delivery_tab._book is original_book
        assert window._s.get("annual_workbook", "path") == str(valid_path)
        assert "기존 db 파일" in window.roster_label.text()
        assert "계속 사용" in window.statusbar.currentMessage()
    finally:
        window.close()


def test_output_directory_is_restored_and_used_as_folder_dialog_start(
    qapp: QApplication,
    tmp_path: Path,
):
    output_dir = str(tmp_path / "저장 폴더")
    settings = QSettings("분당구", "급식비 정산 프로그램")
    settings.setValue("output_dir", output_dir)
    settings.sync()

    window = MainWindow()
    try:
        assert window.invoice_tab.output_dir == output_dir
        assert window.confirm_tab.output_dir == output_dir

        with patch(
            "src.widgets.invoice_tab.QFileDialog.getExistingDirectory",
            return_value="",
        ) as invoice_dialog:
            window.invoice_tab._select_output_dir()
        invoice_dialog.assert_called_once_with(
            window.invoice_tab,
            "출력 폴더 선택",
            output_dir,
        )

        with patch(
            "src.widgets.confirm_tab.QFileDialog.getExistingDirectory",
            return_value="",
        ) as confirm_dialog:
            window.confirm_tab._select_output_dir()
        confirm_dialog.assert_called_once_with(
            window.confirm_tab,
            "출력 폴더 선택",
            output_dir,
        )
    finally:
        window.close()


def test_file_selection_opens_manual_header_mapping_and_saves_it(
    qapp: QApplication,
    tmp_path: Path,
):
    path = tmp_path / "custom_headers.xlsx"
    mapping = {
        "식종": "급식 구분",
        "배송담당": "담당 기사",
        "연번": "배달 순서",
    }
    _write_annual_workbook(path, control_headers=tuple(mapping.values()))

    window = MainWindow()
    try:
        with (
            patch(
                "src.main_window.QFileDialog.getOpenFileName",
                return_value=(str(path), "Excel 통합 문서"),
            ),
            patch(
                "src.main_window.DbHeaderMappingDialog.get_selection",
                return_value=DbImportSelection(mapping, 2026),
            ) as choose_mapping,
        ):
            window._select_annual_file()

        choose_mapping.assert_called_once()
        shown_headers = choose_mapping.call_args.args[0]
        assert tuple(shown_headers) == (
            "성명",
            "관할동",
            "도로명주소",
            "급식 구분",
            "담당 기사",
            "배달 순서",
            "20260728",
        )
        assert window._annual_book is not None
        assert window._annual_workbook is not None
        assert _mapping_subset(window._annual_workbook.header_mapping, mapping) == mapping
        assert _mapping_subset(
            window._preferences.get_db_header_mapping(), mapping
        ) == mapping
        assert window._annual_workbook.selected_year == 2026
        assert window._preferences.get_db_selected_year() == 2026
        assert window.header_mapping_btn.isEnabled()
    finally:
        window.close()


def test_file_selection_reuses_cached_mapping_for_same_header_structure(
    qapp: QApplication,
    tmp_path: Path,
):
    first_path = tmp_path / "first_layout.xlsx"
    second_path = tmp_path / "second_layout.xlsx"
    mapping = {
        "식종": "급식 구분",
        "배송담당": "담당 기사",
        "연번": "배달 순서",
    }
    control_headers = tuple(mapping.values())
    _write_annual_workbook(
        first_path,
        control_headers=control_headers,
        date_headers=("20260728",),
    )
    _write_annual_workbook(
        second_path,
        control_headers=control_headers,
        date_headers=("20270728",),
    )

    window = MainWindow()
    try:
        with patch(
            "src.main_window.DbHeaderMappingDialog.get_selection",
            return_value=DbImportSelection(mapping, 2026),
        ) as choose_mapping:
            assert window._load_annual_file(
                str(first_path),
                prompt_mapping=True,
            )
        choose_mapping.assert_called_once()

        with patch(
            "src.main_window.DbHeaderMappingDialog.get_selection"
        ) as choose_mapping:
            assert window._load_annual_file(
                str(second_path),
                prompt_mapping=True,
            )

        choose_mapping.assert_not_called()
        assert window._annual_workbook is not None
        assert window._annual_workbook.path == second_path
        assert window._annual_workbook.selected_year == 2027
        assert _mapping_subset(
            window._annual_workbook.header_mapping,
            mapping,
        ) == mapping
    finally:
        window.close()


def test_changed_header_structure_invalidates_mapping_cache(
    qapp: QApplication,
    tmp_path: Path,
):
    first_path = tmp_path / "first_layout.xlsx"
    changed_path = tmp_path / "changed_layout.xlsx"
    first_mapping = {
        "식종": "급식 구분",
        "배송담당": "담당 기사",
        "연번": "배달 순서",
    }
    changed_mapping = {
        "식종": "식사 종류",
        "배송담당": "담당자",
        "연번": "순서",
    }
    _write_annual_workbook(
        first_path,
        control_headers=tuple(first_mapping.values()),
    )
    _write_annual_workbook(
        changed_path,
        control_headers=tuple(changed_mapping.values()),
    )

    window = MainWindow()
    try:
        with patch(
            "src.main_window.DbHeaderMappingDialog.get_selection",
            return_value=DbImportSelection(first_mapping, 2026),
        ):
            assert window._load_annual_file(
                str(first_path),
                prompt_mapping=True,
            )

        with patch(
            "src.main_window.DbHeaderMappingDialog.get_selection",
            return_value=DbImportSelection(changed_mapping, 2026),
        ) as choose_mapping:
            assert window._load_annual_file(
                str(changed_path),
                prompt_mapping=True,
            )

        choose_mapping.assert_called_once()
    finally:
        window.close()


def test_saved_header_mapping_restores_custom_header_file_without_dialog(
    qapp: QApplication,
    tmp_path: Path,
):
    path = tmp_path / "remembered_custom_headers.xlsx"
    mapping = {
        "식종": "급식 구분",
        "배송담당": "담당 기사",
        "연번": "배달 순서",
    }
    _write_annual_workbook(path, control_headers=tuple(mapping.values()))
    _save_last_db_path(path)
    _save_db_header_mapping(mapping)

    with patch("src.main_window.DbHeaderMappingDialog.get_selection") as dialog:
        window = MainWindow()
    try:
        dialog.assert_not_called()
        assert window._annual_book is not None
        assert window._annual_workbook is not None
        assert _mapping_subset(window._annual_workbook.header_mapping, mapping) == mapping
        assert window.header_mapping_btn.isEnabled()
    finally:
        window.close()


def test_startup_prefers_structure_cache_before_loading_last_db(
    qapp: QApplication,
    tmp_path: Path,
):
    path = tmp_path / "cached_at_startup.xlsx"
    mapping = {
        "식종": "급식 구분",
        "배송담당": "담당 기사",
        "연번": "배달 순서",
    }
    _write_annual_workbook(path, control_headers=tuple(mapping.values()))

    bootstrap = MainWindow()
    try:
        assert bootstrap._annual_workbook is None
        table_headers = AnnualWorkbook(path).table_headers()
        bootstrap._preferences.set_cached_db_import(
            _db_mapping_cache_headers(table_headers),
            mapping,
            2026,
        )
        bootstrap._preferences.set_annual_workbook_path(str(path))
        bootstrap._preferences.set_db_header_mapping(
            {
                "식종": "다른 식종",
                "배송담당": "다른 담당",
                "연번": "다른 순서",
            }
        )
        bootstrap._preferences.set_db_selected_year(2025)
    finally:
        bootstrap.close()

    with patch("src.main_window.DbHeaderMappingDialog.get_selection") as dialog:
        window = MainWindow()
    try:
        dialog.assert_not_called()
        assert window._annual_book is not None
        assert window._annual_workbook is not None
        assert window._annual_workbook.path == path
        assert _mapping_subset(
            window._annual_workbook.header_mapping,
            mapping,
        ) == mapping
        assert window._annual_workbook.selected_year == 2026
    finally:
        window.close()


def test_file_selection_uses_and_remembers_selected_date_year(
    qapp: QApplication,
    tmp_path: Path,
):
    path = tmp_path / "mixed_years.xlsx"
    mapping = {"식종": "식종", "배송담당": "배송담당", "연번": ""}
    _write_annual_workbook(
        path,
        date_headers=("20251231", "20260728"),
    )

    window = MainWindow()
    try:
        with (
            patch(
                "src.main_window.QFileDialog.getOpenFileName",
                return_value=(str(path), "Excel 통합 문서"),
            ),
            patch(
                "src.main_window.DbHeaderMappingDialog.get_selection",
                return_value=DbImportSelection(mapping, 2025),
            ) as choose_settings,
        ):
            window._select_annual_file()

        choose_settings.assert_called_once()
        assert window._annual_book is not None
        assert window._annual_book.year == 2025
        assert tuple(day.year for day in window._annual_book.dates) == (2025,)
        assert window._annual_workbook is not None
        assert window._annual_workbook.selected_year == 2025
        assert window._annual_workbook.header_mapping["연번"] == ""
        assert window._preferences.get_db_selected_year() == 2025
    finally:
        window.close()


def test_header_mapping_button_reopens_mapping_for_active_file(
    qapp: QApplication,
    tmp_path: Path,
):
    path = tmp_path / "active.xlsx"
    _write_annual_workbook(path)

    window = MainWindow()
    try:
        assert window._load_annual_file(str(path)) is True
        mapping = {"식종": "식종", "배송담당": "배송담당", "연번": "연번"}
        with patch(
            "src.main_window.DbHeaderMappingDialog.get_selection",
            return_value=DbImportSelection(mapping, 2026),
        ) as choose_mapping:
            window.header_mapping_btn.click()

        choose_mapping.assert_called_once()
        assert window._annual_workbook is not None
        assert _mapping_subset(window._annual_workbook.header_mapping, mapping) == mapping
    finally:
        window.close()


def test_cancelling_header_mapping_keeps_active_file(
    qapp: QApplication,
    tmp_path: Path,
):
    path = tmp_path / "active.xlsx"
    _write_annual_workbook(path)

    window = MainWindow()
    try:
        assert window._load_annual_file(str(path)) is True
        original_workbook = window._annual_workbook
        original_book = window._annual_book

        with patch(
            "src.main_window.DbHeaderMappingDialog.get_selection",
            return_value=None,
        ):
            window.header_mapping_btn.click()

        assert window._annual_workbook is original_workbook
        assert window._annual_book is original_book
        assert "DB 가져오기 설정 취소" in window.statusbar.currentMessage()
        assert "계속 사용" in window.statusbar.currentMessage()
    finally:
        window.close()


def test_last_db_file_is_loaded_automatically_when_window_starts(
    qapp: QApplication, tmp_path: Path
):
    path = tmp_path / "remembered.xlsx"
    _write_annual_workbook(path)
    _save_last_db_path(path)

    window = MainWindow()
    try:
        assert window._annual_workbook is not None
        assert window._annual_workbook.path == path
        book = window._annual_book
        assert book is not None
        assert window.invoice_tab._book is book
        assert window.report_tab._book is book
        assert window.delivery_tab._book is book
        assert window.merge_tab._target_path == path
        assert all(button.isEnabled() for button in window.work_buttons)
        assert "remembered.xlsx" in window.roster_label.text()
        assert "db 파일 로드 완료" in window.statusbar.currentMessage()
    finally:
        window.close()


def test_missing_cached_db_file_falls_back_to_selection_without_error_dialog(
    qapp: QApplication, tmp_path: Path
):
    missing_path = tmp_path / "moved.xlsx"
    _save_last_db_path(missing_path)

    window = MainWindow()
    try:
        assert window._annual_book is None
        assert "저장된 db 파일 없음" in window.roster_label.text()
        assert "새 db 파일을 선택" in window.statusbar.currentMessage()
    finally:
        window.close()


def test_main_window_uses_db_file_wording(qapp: QApplication):
    window = MainWindow()
    try:
        label_texts = [label.text() for label in window.findChildren(QLabel)]
        assert "db 파일:" in label_texts
        assert window.roster_btn.text() == "db 파일 선택"
        assert window.header_mapping_btn.text() == "DB 가져오기 설정"
        assert not window.header_mapping_btn.isEnabled()
        assert window.invoice_tab.info_label.text() == "db 파일을 불러오세요."
        assert not any("연간 파일" in text for text in label_texts)
    finally:
        window.close()


def test_password_protected_db_uses_masked_ephemeral_password_and_prompts_again(
    qapp: QApplication,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    plain_path = tmp_path / "plain.xlsx"
    encrypted_path = tmp_path / "protected.xlsx"
    _write_annual_workbook(plain_path)
    _encrypt_workbook(plain_path, encrypted_path, password="test-secret")

    first_window = MainWindow()
    try:
        with (
            caplog.at_level(logging.DEBUG),
            patch(
                "src.main_window.QInputDialog.getText",
                return_value=("test-secret", True),
            ) as first_prompt,
        ):
            assert first_window._load_annual_file(str(encrypted_path)) is True

        assert first_prompt.call_count == 1
        assert first_prompt.call_args.args[3] is QLineEdit.EchoMode.Password
        assert first_window._annual_workbook is not None
        assert first_window._annual_workbook.password == ""
        assert "test-secret" not in caplog.text
        visible_text = " ".join(
            [
                first_window.roster_label.text(),
                first_window.statusbar.currentMessage(),
                *(label.text() for label in first_window.findChildren(QLabel)),
            ]
        )
        assert "test-secret" not in visible_text

        settings = QSettings("분당구", "급식비 정산 프로그램")
        assert not any("password" in key.casefold() for key in settings.allKeys())
    finally:
        first_window.close()

    with patch(
        "src.main_window.QInputDialog.getText",
        return_value=("test-secret", True),
    ) as restored_prompt:
        restored_window = MainWindow()
    try:
        assert restored_prompt.call_count == 1
        assert restored_prompt.call_args.args[3] is QLineEdit.EchoMode.Password
        assert restored_window._annual_book is not None
        assert restored_window._annual_workbook is not None
        assert restored_window._annual_workbook.password == ""
    finally:
        restored_window.close()


def test_wrong_db_password_is_reprompted_without_exposing_the_value(
    qapp: QApplication,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    plain_path = tmp_path / "plain.xlsx"
    encrypted_path = tmp_path / "protected.xlsx"
    _write_annual_workbook(plain_path)
    _encrypt_workbook(plain_path, encrypted_path, password="correct-secret")

    window = MainWindow()
    try:
        with (
            caplog.at_level(logging.DEBUG),
            patch(
                "src.main_window.QInputDialog.getText",
                side_effect=[("wrong-secret", True), ("correct-secret", True)],
            ) as prompt,
        ):
            assert window._load_annual_file(str(encrypted_path)) is True

        assert prompt.call_count == 2
        assert all(
            call.args[3] is QLineEdit.EchoMode.Password
            for call in prompt.call_args_list
        )
        assert "올바르지 않습니다" in prompt.call_args_list[1].args[2]
        visible_text = (
            window.roster_label.text() + window.statusbar.currentMessage()
        )
        assert "wrong-secret" not in visible_text
        assert "correct-secret" not in visible_text
        assert "wrong-secret" not in caplog.text
        assert "correct-secret" not in caplog.text
    finally:
        window.close()


def test_cancelling_db_password_keeps_previous_file_and_path(
    qapp: QApplication,
    tmp_path: Path,
):
    valid_path = tmp_path / "valid.xlsx"
    plain_path = tmp_path / "plain.xlsx"
    encrypted_path = tmp_path / "protected.xlsx"
    _write_annual_workbook(valid_path)
    _write_annual_workbook(plain_path)
    _encrypt_workbook(plain_path, encrypted_path, password="test-secret")

    window = MainWindow()
    try:
        assert window._load_annual_file(str(valid_path)) is True
        original_workbook = window._annual_workbook
        original_book = window._annual_book

        with patch(
            "src.main_window.QInputDialog.getText",
            return_value=("cancelled-secret", False),
        ):
            assert window._load_annual_file(str(encrypted_path)) is False

        assert window._annual_workbook is original_workbook
        assert window._annual_book is original_book
        assert window._preferences.get_annual_workbook_path() == str(valid_path)
        assert "암호 입력 취소" in window.statusbar.currentMessage()
        assert "cancelled-secret" not in window.statusbar.currentMessage()
    finally:
        window.close()
