"""배송 리포트 Excel 출력 계약 테스트."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import openpyxl
import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from src.generators import report_generator
from src.services.delivery_service import (
    DeliveryReport,
    DeliveryReportRow,
    DeliveryReportTotals,
)
from src.widgets import report_tab as report_tab_module

_HEADERS = ["배송담당", "1", "2", "3", "4", "5", "Total", "비고", "참고"]


def _report_results() -> list[DeliveryReport]:
    return [
        DeliveryReport(
            date(2026, 7, 28),
            "조식",
            (DeliveryReportRow("기사A", ("1(+1)", "0", "0", "0", "0"), 1, "", ""),),
            DeliveryReportTotals((1, 0, 0, 0, 0), 1),
        ),
        DeliveryReport(
            date(2026, 7, 28),
            "석식",
            (DeliveryReportRow("기사B", ("1", "0", "0", "0", "0"), 1, "", ""),),
            DeliveryReportTotals((1, 0, 0, 0, 0), 1),
        ),
    ]


def _write_minimal_template(path: Path) -> None:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = report_generator.TEMPLATE_SHEET
    for column, header in enumerate(_HEADERS, start=1):
        worksheet.cell(row=3, column=column, value=header)
    workbook.save(path)
    workbook.close()


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


def test_export_multi_raises_when_required_template_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    missing_template = tmp_path / "missing_report_template.xlsx"
    output = tmp_path / "requested.xlsx"
    monkeypatch.setattr(
        report_generator, "get_template_path", lambda: str(missing_template)
    )

    with pytest.raises(FileNotFoundError, match="배송 리포트 템플릿"):
        report_generator.export_multi(_report_results(), output)

    assert not output.exists()
    assert not (tmp_path / "requested_20260728.xlsx").exists()


def test_export_multi_creates_the_exact_requested_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    template = tmp_path / "report_template.xlsx"
    output = tmp_path / "requested.xlsx"
    _write_minimal_template(template)
    monkeypatch.setattr(report_generator, "get_template_path", lambda: str(template))

    report_generator.export_multi(_report_results(), output)

    assert output.is_file()
    assert not (tmp_path / "requested_20260728.xlsx").exists()
    workbook = openpyxl.load_workbook(output)
    try:
        assert workbook.sheetnames == ["20260728(조식)", "20260728(석식)"]
        worksheet = workbook["20260728(조식)"]
        assert worksheet["A1"].value == "7월 28일"
        assert worksheet["A2"].value == "*아동급식 조식*"
        assert [worksheet.cell(4, column).value for column in range(1, 10)] == [
            "기사A",
            1,
            0,
            0,
            0,
            0,
            1,
            None,
            None,
        ]
        assert worksheet["A4"].fill.fill_type == "solid"
        assert worksheet["B4"].fill.fill_type == "solid"
        assert [worksheet.cell(5, column).value for column in range(2, 10)] == [
            1,
            0,
            0,
            0,
            0,
            1,
            None,
            None,
        ]
    finally:
        workbook.close()


def test_export_multi_rejects_empty_results(tmp_path: Path):
    output = tmp_path / "requested.xlsx"

    with pytest.raises(ValueError, match="내보낼 리포트"):
        report_generator.export_multi([], output)

    assert not output.exists()


def test_report_tab_does_not_mark_missing_output_as_success(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    tab = report_tab_module.ReportTab()
    information_calls: list[str] = []
    critical_calls: list[str] = []
    try:
        tab._last_results = _report_results()
        tab._last_export_path = str(tmp_path / "previous.xlsx")
        tab.open_btn.setEnabled(True)
        monkeypatch.setattr(
            report_tab_module.QFileDialog,
            "getExistingDirectory",
            lambda *_args: str(tmp_path),
        )
        monkeypatch.setattr(report_tab_module, "export_multi", lambda *_args: None)
        monkeypatch.setattr(
            report_tab_module.QMessageBox,
            "information",
            lambda _parent, _title, message: information_calls.append(message),
        )
        monkeypatch.setattr(
            report_tab_module.QMessageBox,
            "critical",
            lambda _parent, _title, message: critical_calls.append(message),
        )

        tab._on_export()

        assert tab._last_export_path is None
        assert not tab.open_btn.isEnabled()
        assert information_calls == []
        assert len(critical_calls) == 1
        assert "리포트 파일이 생성되지 않았습니다" in critical_calls[0]
        assert "리포트 저장 실패" in tab.info_label.text()
    finally:
        tab.close()


def test_report_tab_marks_existing_output_as_success(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    tab = report_tab_module.ReportTab()
    information_calls: list[str] = []
    critical_calls: list[str] = []

    def create_requested_file(_results, filepath: str) -> None:
        Path(filepath).touch()

    try:
        tab._last_results = _report_results()
        monkeypatch.setattr(
            report_tab_module.QFileDialog,
            "getExistingDirectory",
            lambda *_args: str(tmp_path),
        )
        monkeypatch.setattr(report_tab_module, "export_multi", create_requested_file)
        monkeypatch.setattr(
            report_tab_module.QMessageBox,
            "information",
            lambda _parent, _title, message: information_calls.append(message),
        )
        monkeypatch.setattr(
            report_tab_module.QMessageBox,
            "critical",
            lambda _parent, _title, message: critical_calls.append(message),
        )

        tab._on_export()

        assert tab._last_export_path is not None
        assert Path(tab._last_export_path).is_file()
        assert tab.open_btn.isEnabled()
        assert len(information_calls) == 1
        assert critical_calls == []
    finally:
        tab.close()
