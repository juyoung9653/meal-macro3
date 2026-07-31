"""MainWindow의 연간 장부 로딩과 탭 배포 통합 테스트."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import openpyxl
import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from src.main_window import MainWindow


@pytest.fixture
def qapp(tmp_path: Path):
    previous_format = QSettings.defaultFormat()
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path / "settings"),
    )
    app = QApplication.instance() or QApplication([])
    yield app
    QSettings.setDefaultFormat(previous_format)


def _write_annual_workbook(path: Path) -> None:
    workbook = openpyxl.Workbook()
    roster = workbook.active
    assert roster is not None
    roster.title = "명단"
    roster.append(
        [
            "성명",
            "관할동",
            "도로명주소",
            "식종",
            "배송담당",
            "연번",
            "20260728",
        ]
    )
    profile = ["홍길동", "정자동", "성남대로 1"]
    roster.append([*profile, "조식", "기사A", "001", None])
    roster.append([*profile, "중식", "기사A", "001", 1])
    roster.append([*profile, "석식", "기사A", "001", None])
    workbook.save(path)
    workbook.close()


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
        assert window.report_tab._book is book
        assert window.delivery_tab._book is book
        assert window.invoice_tab.month_combo.currentData() == 7
        assert window.invoice_tab.generate_btn.isEnabled()
        assert window.report_tab.build_btn.isEnabled()
        assert window.delivery_tab.run_btn.isEnabled()
        assert "2026년 · 1명" in window.roster_label.text()
        assert window._s.get("annual_workbook", "path") == str(path)
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
        assert window.report_tab._book is original_book
        assert window.delivery_tab._book is original_book
        assert window._s.get("annual_workbook", "path") == str(valid_path)
        assert "기존 파일" in window.roster_label.text()
        assert "계속 사용" in window.statusbar.currentMessage()
    finally:
        window.close()
