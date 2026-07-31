"""배송대타 Excel 임시 파일 출력의 성공/실패 계약."""

import os
from pathlib import Path

import pandas as pd
import pytest

from src.excel import delivery_viewer


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
