"""User-facing errors hide implementation details without changing user data."""
import errno
from pathlib import Path
import re
import sqlite3

import pytest

from src.desktop_v2.user_messages import user_error


@pytest.mark.parametrize("error,expected", [
    (PermissionError(errno.EACCES, "Permission denied"), "쓰기 권한"),
    (FileNotFoundError(errno.ENOENT, "No such file or directory"), "선택한 위치"),
    (FileExistsError("File exists"), "다른 이름"),
    (OSError(errno.ENOSPC, "No space left on device"), "저장 공간"),
    (TimeoutError("timed out"), "연결"),
    (ConnectionError("connection reset"), "연결"),
    (sqlite3.OperationalError("database is locked"), "잠시 기다린"),
    (sqlite3.IntegrityError("UNIQUE constraint failed: persons.person_id"), "중복"),
    (sqlite3.IntegrityError("FOREIGN KEY constraint failed"), "연결된 항목"),
    (sqlite3.IntegrityError("NOT NULL constraint failed: persons.name"), "필수 항목"),
    (ValueError("Invalid isoformat string: 'not-a-date'"), "날짜"),
    (ValueError("invalid literal for int() with base 10: 'abc'"), "숫자"),
    (KeyError("birth_date"), "생년월일"),
    (TypeError("mobile_phone은 문자열 또는 None이어야 합니다."), "핸드폰"),
    (ValueError("import profile에 필수 열이 없습니다: name"), "성명"),
    (ValueError("school_name은 비어 있을 수 없습니다."), "학교"),
    (ValueError("profile은 ImportMappingProfile이어야 합니다."), "자료"),
    (ValueError("알 수 없는 내부 unknown_field입니다."), "항목"),
    (RuntimeError("Traceback: some.package.InternalError"), "다시 시도"),
    (ValueError("revision parent chain이 끊겼습니다: internal-id"), "변경 이력"),
])
def test_error_is_actionable_korean_without_internal_terms(error, expected):
    message = user_error(error)
    assert expected in message
    assert not re.search(r"[A-Za-z_]{2,}", message)


def test_windows_file_in_use_is_distinct_from_permissions():
    error = PermissionError("sharing violation")
    error.winerror = 32
    assert "다른 프로그램에서 파일을 사용 중" in user_error(error)


@pytest.mark.parametrize("message", [
    "종료일은 시작일보다 빠를 수 없습니다.",
    "'Alice_school.xlsx' 파일의 'custom_name' 시트에서 성명을 찾지 못했습니다.",
    "ABC학교 · 3행: 성명이 없습니다.",
    "school_name 값은 숫자여야 합니다.",
    "'migration_projection.sqlite3' 파일을 찾을 수 없습니다.",
    "홍길동 · 개인 항목 'school_name' 값은 숫자여야 합니다.",
])
def test_existing_validation_and_user_text_are_preserved(message):
    assert user_error(ValueError(message)) == message


def test_header_wording_does_not_rename_source_table():
    assert user_error("'머리글없는표' 표에 머리글이 없습니다.") == "'머리글없는표' 표에 헤더가 없습니다."


def test_exception_is_not_mutated_and_identifiers_are_not_exposed():
    error = ValueError("snapshot에 없는 사람 ID입니다: 9e8dbad4-1ba4-4537-a41c-7412717968e6")
    before = str(error)
    message = user_error(error)
    assert str(error) == before
    assert "9e8dbad4" not in message and "snapshot" not in message and "ID" not in message


def test_address_failure_keeps_person_but_localizes_server_reason():
    from src.desktop_v2.old_address import _failure_message
    assert _failure_message("홍길동: connection refused").startswith("홍길동: 연결")
    assert "service unavailable" not in _failure_message("Alice: service unavailable")


def test_ui_error_boundaries_do_not_display_raw_exceptions():
    root = Path(__file__).parents[1] / "src" / "desktop_v2"
    for path in root.glob("*.py"):
        if path.name == "user_messages.py":
            continue
        text = path.read_text(encoding="utf-8-sig")
        for line in text.splitlines():
            if 'str(error) ==' in line:  # A business-rule comparison, not display text.
                continue
            assert not re.search(r"str\((?:error|exc|(?:\w+\.)+error)\)|\{(?:error|exc)\}", line), path.name


def test_database_filename_extension_instruction_is_preserved():
    message = "운영 데이터 파일 이름은 .sqlite3로 끝나야 합니다."
    assert user_error(ValueError(message)) == message


def test_quoted_internal_identifiers_do_not_escape_translation():
    identifier = "9e8dbad4-1ba4-4537-a41c-7412717968e6"
    assert identifier not in user_error(ValueError(f"해당 사람을 찾을 수 없습니다: UUID('{identifier}')"))
    message = user_error(ValueError("알 수 없는 칼럼: 'custom_internal_key'"))
    assert "custom_internal_key" not in message
    assert "선택한 항목" in message
