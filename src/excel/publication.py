"""Atomic publication helpers for generated Excel workbooks."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

import msoffcrypto


@dataclass(frozen=True, slots=True)
class WorkbookPublicationResult:
    output_path: Path
    password_protected: bool
    content_sha256: str
    output_sha256: str
    output_byte_size: int


def publish_workbook(
    workbook: Any,
    target: str | Path,
    *,
    password: str | None = None,
) -> WorkbookPublicationResult:
    """Save ``workbook`` to a staging file, then atomically publish it.

    The staging file is created alongside ``target`` so ``os.replace`` stays on
    the same filesystem.  The previous target remains untouched unless both
    save and close complete successfully.  With a password, plaintext remains
    in memory and only the encrypted package reaches the staging file.
    """
    target_path = Path(target)
    staging_path: Path | None = None
    content_sha256: str | None = None
    workbook_closed = False

    def close_workbook() -> None:
        nonlocal workbook_closed
        if workbook_closed:
            return
        workbook_closed = True
        workbook.close()

    try:
        normalized_password = _password(password)
        descriptor, staging_name = tempfile.mkstemp(
            prefix=f".{target_path.name}.",
            suffix=target_path.suffix,
            dir=target_path.parent,
        )
        staging_path = Path(staging_name)
        os.close(descriptor)

        if normalized_password is None:
            workbook.save(staging_path)
            close_workbook()
            with staging_path.open("rb") as plaintext:
                content_sha256 = canonical_ooxml_sha256(plaintext)
        else:
            plaintext = BytesIO()
            try:
                workbook.save(plaintext)
                close_workbook()
                content_sha256 = canonical_ooxml_sha256(plaintext)
                plaintext.seek(0)
                with staging_path.open("wb") as encrypted:
                    msoffcrypto.OfficeFile(plaintext).encrypt(
                        normalized_password,
                        encrypted,
                    )
            finally:
                plaintext.close()
            _verify_password_protected(staging_path)

        output_sha256 = _file_sha256(staging_path)
        output_byte_size = staging_path.stat().st_size
        os.replace(staging_path, target_path)
        return WorkbookPublicationResult(
            target_path,
            normalized_password is not None,
            content_sha256,
            output_sha256,
            output_byte_size,
        )
    finally:
        close_workbook()
        if staging_path is not None:
            staging_path.unlink(missing_ok=True)


def _password(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("Excel 출력 비밀번호는 문자열이어야 합니다.")
    if not value or not value.strip():
        raise ValueError("Excel 출력 비밀번호를 입력해주세요.")
    if value != value.strip():
        raise ValueError("Excel 출력 비밀번호 앞뒤에는 공백을 사용할 수 없습니다.")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_ooxml_sha256(package: BinaryIO) -> str:
    """Hash logical OOXML parts without ZIP or generated core timestamps."""

    package.seek(0)
    digest = hashlib.sha256()
    try:
        with ZipFile(package) as archive:
            for name in sorted(
                item.filename for item in archive.infolist() if not item.is_dir()
            ):
                name_bytes = name.encode("utf-8")
                content = archive.read(name)
                if name == "docProps/core.xml":
                    content = _stable_core_properties(content)
                digest.update(len(name_bytes).to_bytes(8, "big"))
                digest.update(name_bytes)
                digest.update(len(content).to_bytes(8, "big"))
                digest.update(content)
    except BadZipFile:
        package.seek(0)
        for chunk in iter(lambda: package.read(1024 * 1024), b""):
            digest.update(chunk)
    package.seek(0)
    return digest.hexdigest()


def _stable_core_properties(content: bytes) -> bytes:
    root = ElementTree.fromstring(content)
    generated_tags = {
        "{http://purl.org/dc/terms/}created",
        "{http://purl.org/dc/terms/}modified",
    }
    for child in tuple(root):
        if child.tag in generated_tags:
            root.remove(child)
    return ElementTree.tostring(root, encoding="utf-8")


def _verify_password_protected(path: Path) -> None:
    with path.open("rb") as source:
        if not msoffcrypto.OfficeFile(source).is_encrypted():
            raise OSError("생성한 Excel 파일에 비밀번호가 적용되지 않았습니다.")
