"""배송대타 Excel 임시 파일 출력의 성공/실패 계약."""

import os
from pathlib import Path

import pandas as pd
import pytest

from src.excel import delivery_viewer
from src.excel.delivery_sheet_renderer import (
    DeliveryPrintPolicy,
    DeliveryRenderRequest,
    DeliverySheetRenderer,
)


def _fake_mkstemp(path: Path) -> tuple[int, str]:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR)
    return descriptor, str(path)


def _delivery_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "배송순서": "001",
                "관할동": "정자동",
                "이름": "홍길동",
                "상세주소": "101호",
                "신주소": "성남대로 1",
                "핸드폰": "010-1111-1111",
                "비고2": "",
            }
        ]
    )


def test_show_excel_returns_created_path_after_opening(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    output = tmp_path / "delivery.xlsx"
    opened: list[str] = []
    monkeypatch.setattr(
        delivery_viewer.tempfile,
        "mkstemp",
        lambda **_kwargs: _fake_mkstemp(output),
    )
    monkeypatch.setattr(
        delivery_viewer.os,
        "startfile",
        lambda path: opened.append(path),
        raising=False,
    )

    result = delivery_viewer.show_excel(
        _delivery_frame(),
        title="배송대타",
        meal="중식",
        ship="기사A",
        date_str="20260728",
        ori=1,
        split=1,
        max_rows=18,
    )

    assert result == str(output)
    assert output.is_file()
    assert opened == [str(output)]


def test_show_excel_propagates_open_failure_and_removes_temp_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    output = tmp_path / "delivery.xlsx"
    monkeypatch.setattr(
        delivery_viewer.tempfile,
        "mkstemp",
        lambda **_kwargs: _fake_mkstemp(output),
    )

    def fail_to_open(_path: str) -> None:
        raise OSError("Excel 실행 실패")

    monkeypatch.setattr(delivery_viewer.os, "startfile", fail_to_open, raising=False)

    with pytest.raises(OSError, match="Excel 실행 실패"):
        delivery_viewer.show_excel(
            _delivery_frame(),
            title="배송대타",
            meal="중식",
            ship="기사A",
            date_str="20260728",
            ori=1,
            split=1,
            max_rows=18,
        )

    assert not output.exists()


def test_delivery_sheet_renderer_owns_rendering_order_and_print_policy():
    request = DeliveryRenderRequest(
        title="배송대타",
        meal="중식",
        staff="기사A",
        date="20260728",
        print_policy=DeliveryPrintPolicy(
            orientation=1,
            split_mode=1,
            max_rows=18,
        ),
    )

    workbook = DeliverySheetRenderer().render(_delivery_frame(), request)
    try:
        worksheet = workbook.active
        assert worksheet is not None
        assert worksheet.title == "배송대타"
        assert worksheet["A1"].value == "식종: 중식 | 배송담당: 기사A | 날짜: 20260728"
        assert worksheet["A2"].value == "배송순서"
        assert worksheet["A3"].value == "001"
        assert worksheet.page_setup.orientation == "portrait"
        assert worksheet.print_title_rows == "$1:$2"
    finally:
        workbook.close()
