"""Atomic Excel workbook publication tests."""

from __future__ import annotations

from pathlib import Path
from io import BytesIO
from datetime import datetime

import msoffcrypto
import openpyxl
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


def test_publish_workbook_password_protects_file_and_reports_both_hashes(
    tmp_path: Path,
):
    target = tmp_path / "protected.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active["A1"] = "개인정보"

    result = publication.publish_workbook(
        workbook,
        target,
        password="shared-1234",
    )

    with target.open("rb") as encrypted:
        office_file = msoffcrypto.OfficeFile(encrypted)
        assert office_file.is_encrypted()
        office_file.load_key(password="shared-1234")
        plaintext = BytesIO()
        office_file.decrypt(plaintext)
    plaintext.seek(0)
    reopened = openpyxl.load_workbook(plaintext, read_only=True)
    try:
        assert reopened.active["A1"].value == "개인정보"
    finally:
        reopened.close()
        plaintext.close()

    assert result.output_path == target
    assert result.password_protected
    assert len(result.content_sha256) == 64
    assert len(result.output_sha256) == 64
    assert result.content_sha256 != result.output_sha256
    assert result.output_byte_size == target.stat().st_size
    assert list(tmp_path.iterdir()) == [target]


def test_content_hash_ignores_generated_zip_and_core_timestamps_but_not_cells():
    def package(created: datetime, value: str) -> BytesIO:
        workbook = openpyxl.Workbook()
        workbook.properties.created = created
        workbook.properties.modified = created
        workbook.active["A1"] = value
        output = BytesIO()
        workbook.save(output)
        workbook.close()
        output.seek(0)
        return output

    first = package(datetime(2020, 1, 1), "same")
    second = package(datetime(2026, 8, 26), "same")
    changed = package(datetime(2026, 8, 26), "different")
    try:
        first_hash = publication.canonical_ooxml_sha256(first)
        assert publication.canonical_ooxml_sha256(second) == first_hash
        assert publication.canonical_ooxml_sha256(changed) != first_hash
    finally:
        first.close()
        second.close()
        changed.close()


def test_encryption_failure_preserves_existing_target_and_removes_staging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    target = tmp_path / "protected.xlsx"
    target.write_text("previous", encoding="utf-8")
    workbook = openpyxl.Workbook()

    class _EncryptionFailure:
        @staticmethod
        def encrypt(_password, _destination):
            raise OSError("encrypt failed")

    monkeypatch.setattr(
        publication.msoffcrypto,
        "OfficeFile",
        lambda _source: _EncryptionFailure(),
    )

    with pytest.raises(OSError, match="encrypt failed"):
        publication.publish_workbook(
            workbook,
            target,
            password="shared-1234",
        )

    assert target.read_text(encoding="utf-8") == "previous"
    assert list(tmp_path.iterdir()) == [target]


def test_invalid_password_closes_workbook_without_touching_target(tmp_path: Path):
    target = tmp_path / "protected.xlsx"
    target.write_text("previous", encoding="utf-8")
    workbook = _Workbook()

    with pytest.raises(ValueError, match="비밀번호"):
        publication.publish_workbook(workbook, target, password="   ")

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
