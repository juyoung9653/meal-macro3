"""배송 리포트 Excel 임시 파일 출력의 성공/실패 계약."""

import os
from datetime import date
from pathlib import Path

import pytest

from src.excel import report_viewer
from src.services.delivery_service import DeliveryReport, DeliveryReportTotals


def _fake_mkstemp(path: Path) -> tuple[int, str]:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR)
    return descriptor, str(path)


def _results() -> list[DeliveryReport]:
    return [
        DeliveryReport(
            date(2026, 7, 28),
            "중식",
            (),
            DeliveryReportTotals((0, 0, 0, 0, 0), 0),
        )
    ]


def test_show_excel_creates_and_opens_temporary_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    output = tmp_path / "report.xlsx"
    opened: list[str] = []
    exported: list[tuple[list[DeliveryReport], Path]] = []
    results = _results()

    monkeypatch.setattr(
        report_viewer.tempfile,
        "mkstemp",
        lambda **_kwargs: _fake_mkstemp(output),
    )

    def export(value: list[DeliveryReport], filepath: Path) -> None:
        exported.append((value, filepath))
        filepath.write_bytes(b"xlsx")

    monkeypatch.setattr(report_viewer, "export_multi", export)
    monkeypatch.setattr(
        report_viewer.os,
        "startfile",
        lambda path: opened.append(path),
        raising=False,
    )

    result = report_viewer.show_excel(results)

    assert result == str(output)
    assert exported == [(results, output)]
    assert opened == [str(output)]
    assert output.is_file()


def test_show_excel_rejects_missing_output_and_removes_placeholder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    output = tmp_path / "report.xlsx"
    monkeypatch.setattr(
        report_viewer.tempfile,
        "mkstemp",
        lambda **_kwargs: _fake_mkstemp(output),
    )
    monkeypatch.setattr(report_viewer, "export_multi", lambda *_args: None)

    with pytest.raises(FileNotFoundError, match="리포트 파일이 생성되지 않았습니다"):
        report_viewer.show_excel(_results())

    assert not output.exists()


def test_show_excel_removes_temporary_report_when_opening_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    output = tmp_path / "report.xlsx"
    monkeypatch.setattr(
        report_viewer.tempfile,
        "mkstemp",
        lambda **_kwargs: _fake_mkstemp(output),
    )
    monkeypatch.setattr(
        report_viewer,
        "export_multi",
        lambda _results, filepath: filepath.write_bytes(b"xlsx"),
    )

    def fail_to_open(_path: str) -> None:
        raise OSError("Excel 실행 실패")

    monkeypatch.setattr(report_viewer.os, "startfile", fail_to_open, raising=False)

    with pytest.raises(OSError, match="Excel 실행 실패"):
        report_viewer.show_excel(_results())

    assert not output.exists()
