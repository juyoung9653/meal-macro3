"""Atomic Excel workbook publication tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.excel import publication


class _Workbook:
    def __init__(self, content: str = "new") -> None:
        self.content = content
        self.closed = False
        self.saved_path: Path | None = None

    def save(self, path: str | Path) -> None:
        self.saved_path = Path(path)
        self.saved_path.write_text(self.content, encoding="utf-8")

    def close(self) -> None:
        self.closed = True


class _SaveFailureWorkbook(_Workbook):
    def save(self, path: str | Path) -> None:
        super().save(path)
        raise OSError("save failed")


def test_publish_workbook_stages_in_target_directory_and_closes_before_replace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    target = tmp_path / "report.xlsx"
    workbook = _Workbook()
    replace_calls: list[tuple[Path, Path]] = []
    real_replace = publication.os.replace

    def replace_after_close(source: str | Path, destination: str | Path) -> None:
        assert workbook.closed
        replace_calls.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(publication.os, "replace", replace_after_close)

    publication.publish_workbook(workbook, target)

    assert workbook.saved_path is not None
    assert workbook.saved_path.parent == target.parent
    assert workbook.saved_path != target
    assert replace_calls == [(workbook.saved_path, target)]
    assert target.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.iterdir()) == [target]


def test_publish_workbook_preserves_target_and_removes_staging_when_save_fails(
    tmp_path: Path,
):
    target = tmp_path / "invoice.xlsx"
    target.write_text("previous", encoding="utf-8")
    workbook = _SaveFailureWorkbook()

    with pytest.raises(OSError, match="save failed"):
        publication.publish_workbook(workbook, target)

    assert workbook.closed
    assert target.read_text(encoding="utf-8") == "previous"
    assert list(tmp_path.iterdir()) == [target]


def test_publish_workbook_preserves_target_and_removes_staging_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    target = tmp_path / "confirm.xlsx"
    target.write_text("previous", encoding="utf-8")
    workbook = _Workbook()

    def replace_failure(_source: str | Path, _destination: str | Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(publication.os, "replace", replace_failure)

    with pytest.raises(OSError, match="replace failed"):
        publication.publish_workbook(workbook, target)

    assert workbook.closed
    assert target.read_text(encoding="utf-8") == "previous"
    assert list(tmp_path.iterdir()) == [target]
