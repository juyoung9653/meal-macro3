"""일일식수 Excel 출력 계약 테스트."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import openpyxl
import pytest
from PyQt6.QtCore import QPoint, QPointF, QSettings, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QApplication, QPushButton

from src.core.delivery_matrix import ALLOWED_MEALS, AnnualDeliveryBook
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

    with pytest.raises(FileNotFoundError, match="일일식수 템플릿"):
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


def test_make_report_publication_returns_verified_file_facts(tmp_path: Path):
    template = tmp_path / "report_template.xlsx"
    output = tmp_path / "published.xlsx"
    _write_minimal_template(template)

    publication = report_generator.make_report_publication(
        _report_results(),
        output,
        template,
    )

    assert publication.output_path == output
    assert publication.password_protected is False
    assert len(publication.content_sha256) == 64
    assert len(publication.output_sha256) == 64
    assert publication.output_byte_size == output.stat().st_size


def test_export_keeps_long_multiple_form_counts_readable(tmp_path: Path):
    template = tmp_path / "report_template.xlsx"
    output = tmp_path / "forms.xlsx"
    _write_minimal_template(template)
    form_name = "긴 배송 형태 이름 " * 12
    memo = "기본 1가구·2식\n식재료 박스 2가구·4식\n도시락 1가구·1식\n" + form_name + " 1가구·1식"
    report = DeliveryReport(
        date(2026, 8, 21), "조식",
        (DeliveryReportRow("기사A", ("5", "0", "0", "0", "0"), 5, memo, ""),),
        DeliveryReportTotals((5, 0, 0, 0, 0), 5),
    )

    report_generator.make_report_publication((report,), output, template)

    workbook = openpyxl.load_workbook(output, rich_text=True)
    try:
        sheet = workbook.active
        assert sheet is not None
        assert str(sheet["H4"].value) == memo
        assert sheet["H4"].alignment.wrap_text is True
        assert sheet.row_dimensions[4].height >= 7 * 17
        assert sheet.row_dimensions[4].height <= 409.5
        assert sheet["G4"].value == 5
        assert sheet["B5"].value == 5
    finally:
        workbook.close()


def test_export_multi_rejects_empty_results(tmp_path: Path):
    output = tmp_path / "requested.xlsx"

    with pytest.raises(ValueError, match="내보낼 일일식수"):
        report_generator.export_multi([], output)

    assert not output.exists()


def test_report_tab_removes_save_button_and_opens_excel_after_build(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    day = date(2026, 7, 28)
    book = AnnualDeliveryBook(2026, (), (), (day,))
    tab = report_tab_module.ReportTab(annual_book=book)
    results = _report_results()
    opened = tmp_path / "automatic.xlsx"
    calls: list[list[DeliveryReport]] = []
    meal_filters: list[tuple[str, ...]] = []

    class StubService:
        def build_all(
            self, *_args, meal_filter: tuple[str, ...], **_kwargs
        ) -> list[DeliveryReport]:
            meal_filters.append(meal_filter)
            return results

    def show_excel(value: list[DeliveryReport]) -> str:
        calls.append(value)
        opened.touch()
        return str(opened)

    try:
        tab._service = StubService()
        monkeypatch.setattr(report_tab_module, "show_report_excel", show_excel)

        button_texts = [button.text() for button in tab.findChildren(QPushButton)]
        assert "엑셀로 저장" not in button_texts
        assert tab.lunch_check.text() == "중식"
        assert tab.breakfast_dinner_check.text() == "석식&&조식"

        tab._on_build()

        assert calls == [results]
        assert meal_filters == [ALLOWED_MEALS]
        assert tab._last_excel_path == str(opened)
        assert tab.open_btn.isEnabled()
        assert tab.result_tabs.count() == len(results)
        assert "Excel 실행 완료" in tab.info_label.text()
    finally:
        tab.close()


def test_report_tab_dates_ignore_mouse_wheel(qapp: QApplication):
    day = date(2026, 7, 28)
    tab = report_tab_module.ReportTab(
        annual_book=AnnualDeliveryBook(2026, (), (), (day,))
    )
    try:
        for editor in (tab.date_start, tab.date_end):
            original = editor.date()
            editor.setFocus()
            wheel = QWheelEvent(
                QPointF(4, 4),
                QPointF(4, 4),
                QPoint(),
                QPoint(0, 120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.ScrollUpdate,
                False,
            )
            QApplication.sendEvent(editor, wheel)
            assert editor.date() == original
    finally:
        tab.close()


def test_report_tab_requires_a_meal_group_and_maps_each_checkbox(
    qapp: QApplication
):
    day = date(2026, 7, 28)
    book = AnnualDeliveryBook(2026, (), (), (day,))
    tab = report_tab_module.ReportTab(annual_book=book)
    meal_filters: list[tuple[str, ...]] = []

    class StubService:
        def build_all(
            self, *_args, meal_filter: tuple[str, ...], **_kwargs
        ) -> list[DeliveryReport]:
            meal_filters.append(meal_filter)
            return []

    try:
        tab._service = StubService()
        assert tab.lunch_check.isChecked()
        assert tab.breakfast_dinner_check.isChecked()
        assert tab.build_btn.isEnabled()

        tab.lunch_check.setChecked(False)
        assert tab.build_btn.isEnabled()
        tab._on_build()
        assert meal_filters == [("조식", "석식")]

        tab.breakfast_dinner_check.setChecked(False)
        assert not tab.build_btn.isEnabled()

        tab.lunch_check.setChecked(True)
        assert tab.build_btn.isEnabled()
        tab._on_build()
        assert meal_filters == [("조식", "석식"), ("중식",)]
    finally:
        tab.close()


def test_report_tab_keeps_preview_when_automatic_excel_output_fails(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
):
    day = date(2026, 7, 28)
    book = AnnualDeliveryBook(2026, (), (), (day,))
    tab = report_tab_module.ReportTab(annual_book=book)
    results = _report_results()
    critical_calls: list[str] = []

    class StubService:
        def build_all(self, *_args, **_kwargs) -> list[DeliveryReport]:
            return results

    def fail_to_show(_results: list[DeliveryReport]) -> str:
        raise OSError("Excel 실행 실패")

    try:
        tab._service = StubService()
        monkeypatch.setattr(report_tab_module, "show_report_excel", fail_to_show)
        monkeypatch.setattr(
            report_tab_module.QMessageBox,
            "critical",
            lambda _parent, _title, message: critical_calls.append(message),
        )

        tab._on_build()

        assert tab._last_results == results
        assert tab.result_tabs.count() == len(results)
        assert tab._last_excel_path is None
        assert not tab.open_btn.isEnabled()
        assert len(critical_calls) == 1
        assert "Excel 출력에 실패" in critical_calls[0]
        assert "Excel 출력 실패" in tab.info_label.text()
    finally:
        tab.close()


def test_legacy_single_report_export_keeps_existing_call_contract(monkeypatch, tmp_path):
    from src.generators import report_generator
    report = object()
    target = tmp_path / "legacy.xlsx"
    calls = []
    monkeypatch.setattr(report_generator, "export_multi", lambda reports, filepath, **kwargs: calls.append((reports, filepath, kwargs)))
    report_generator.export(report, target, output_password="password")
    assert calls == [((report,), target, {"output_password": "password"})]
