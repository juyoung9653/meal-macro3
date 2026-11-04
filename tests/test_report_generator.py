"""일일식수 Excel 출력 계약 테스트."""

from __future__ import annotations

from datetime import date
from pathlib import Path


import openpyxl
import pytest

from src.generators import report_generator
from src.services.delivery_service import (
    DeliveryReport,
    DeliveryReportRow,
    DeliveryReportTotals,
)

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
        assert worksheet["A4"].fill.fill_type is None
        assert worksheet["B4"].fill.fill_type is None
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


def test_export_preserves_template_size_even_for_clipped_long_form_counts(tmp_path: Path):
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
        assert sheet["H4"].alignment.wrap_text is None
        assert sheet.row_dimensions[4].height is None
        assert sheet["G4"].value == 5
        assert sheet["B5"].value == 5
    finally:
        workbook.close()


def test_export_multi_rejects_empty_results(tmp_path: Path):
    output = tmp_path / "requested.xlsx"

    with pytest.raises(ValueError, match="내보낼 일일식수"):
        report_generator.export_multi([], output)

    assert not output.exists()


def test_legacy_single_report_export_keeps_existing_call_contract(monkeypatch, tmp_path):
    from src.generators import report_generator
    report = object()
    target = tmp_path / "legacy.xlsx"
    calls = []
    monkeypatch.setattr(report_generator, "export_multi", lambda reports, filepath, **kwargs: calls.append((reports, filepath, kwargs)))
    report_generator.export(report, target, output_password="password")
    assert calls == [((report,), target, {"output_password": "password"})]


def test_report_reference_area_stays_literal(tmp_path):
    from dataclasses import replace

    template = tmp_path / "report.xlsx"
    _write_minimal_template(template)
    report = _report_results()[0]
    report = replace(report, rows=(replace(report.rows[0], reference_areas="=1+1"),))
    output = tmp_path / "literal-report.xlsx"
    report_generator.make_report_publication((report,), output, template)
    workbook = openpyxl.load_workbook(output)
    try:
        assert workbook.active["I4"].value == "=1+1"
        assert workbook.active["I4"].data_type == "s"
        assert workbook.active["G4"].value == 1
    finally:
        workbook.close()
