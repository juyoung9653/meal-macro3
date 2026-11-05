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


def test_missing_database_password_file_explains_how_to_reopen():
    from src.storage.database_credentials import CredentialNotFoundError
    message = user_error(CredentialNotFoundError("internal.credentials.json"))
    assert "비밀번호 정보 파일" in message
    assert "같은 폴더" in message and "파일 이름" in message and "다시 열어" in message
    assert "internal" not in message and "Credential" not in message


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


@pytest.mark.parametrize("raw", [
    "이력이 있는 entity ID를 다시 insert할 수 없습니다: school-lunch.schedule/9e8dbad4-1ba4-4537-a41c-7412717968e6",
    "entity ID가 다시 insert되었습니다: EntityRef(entity_kind='school-lunch.schedule')",
    "이미 사용한 이사·배정 기록 ID입니다.",
    "기사 표시 순서 기록 ID가 올바르지 않습니다.",
    "상태 사건 ID는 중복될 수 없습니다.",
    "typed observation에는 원본 파일의 basename만 저장합니다.",
    "import batch의 revision source가 import가 아닙니다.",
    "봉인되지 않은 revision입니다: 9e8dbad4-1ba4-4537-a41c-7412717968e6",
    "household ID factory가 중복 UUID를 반환했습니다.",
    "학교-lunch.일정/해당 항목",
])
def test_internal_contracts_are_explained_instead_of_word_translation(raw):
    message = user_error(ValueError(raw))
    assert not re.search(r"school-lunch|EntityRef|UUID|식별 정보|해당 항목|봉인|정리된|\.일정", message)
    assert "해주세요" in message
    assert "담당자" in message


def test_stale_edit_and_integrity_failure_have_different_next_steps():
    from src.storage.encrypted_database import RevisionConflictError, DatabaseIntegrityError
    stale = user_error(RevisionConflictError("audit before가 현재 entity 상태와 다릅니다: roster.person/id"))
    corrupt = user_error(DatabaseIntegrityError("audit before chain이 일치하지 않습니다"))
    assert "최신 내용" in stale and "다시 입력" in stale
    assert "변경 이력" in corrupt and "정상 백업" in corrupt
    assert stale != corrupt


def test_school_missing_schedule_keeps_school_and_date_with_next_step():
    message = user_error("예시초의 2027.07.22 중식 일정이 없습니다.")
    assert "예시초" in message and "2027.07.22" in message
    assert "운영 일정" in message and "불러온 뒤" in message


def test_all_literal_internal_error_templates_use_actionable_messages():
    """New domain/storage assertions must not leak through word translation."""
    import ast
    root = Path(__file__).parents[1] / "src"
    tokens = re.compile(r"(?<![A-Za-z_])(?:entity|snapshot|projection|UUID|ID|typed|factory)(?![A-Za-z_])")
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call) or not node.exc.args:
                continue
            arg = node.exc.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                raw = arg.value
            elif isinstance(arg, ast.JoinedStr):
                raw = "".join(v.value if isinstance(v, ast.Constant) else "합성값" for v in arg.values)
            else:
                continue
            if not tokens.search(raw):
                continue
            message = user_error(ValueError(raw))
            assert not re.search(r"식별 정보|해당 항목|정리된|EntityRef|UUID|\btyped\b", message), (path, node.lineno, message)
            assert "해주세요" in message, (path, node.lineno, message)


def test_exception_aliases_cannot_bypass_ui_error_formatter():
    import ast
    root = Path(__file__).parents[1] / "src" / "desktop_v2"
    def exposes(node, alias):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"user_error", "_friendly_error", "type"}:
            return False
        if isinstance(node, ast.Name) and node.id == alias:
            return True
        return any(exposes(child, alias) for child in ast.iter_child_nodes(node))
    for path in root.glob("*.py"):
        if path.name == "user_messages.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for handler in ast.walk(tree):
            if not isinstance(handler, ast.ExceptHandler) or not handler.name:
                continue
            for call in ast.walk(handler):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                    continue
                if call.func.attr in {"critical", "warning", "setText", "_show_error"}:
                    # Logger calls are diagnostics, not user-facing dialogs.
                    if isinstance(call.func.value, ast.Name) and "log" in call.func.value.id.lower():
                        continue
                    assert not any(exposes(arg, handler.name) for arg in call.args), (path, call.lineno)
