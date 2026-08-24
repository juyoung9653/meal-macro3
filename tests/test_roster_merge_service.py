"""최신 한-행 명단을 db 표에 병합하는 데이터 무결성 테스트."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import msoffcrypto
import openpyxl
import pytest
from msoffcrypto.format.ooxml import OOXMLFile
from openpyxl.worksheet.table import Table

from src.excel.annual_workbook import (
    CONTROL_HEADERS,
    PROFILE_HEADER_ROLES,
    TABLE_NAME,
    AnnualWorkbook,
    WorkbookPasswordError,
    WorkbookPasswordRequiredError,
)
from src.services.roster_merge_service import (
    MERGE_MEAL_ROLE,
    RosterMergeError,
    RosterMergeRequest,
    merge_roster,
    read_source_layout,
)


def _profile(name: str, birth: str, school: str) -> list[object]:
    values = {role: None for role in PROFILE_HEADER_ROLES}
    values.update({"성명": name, "생년월일": birth, "학교명": school})
    return [values[role] for role in PROFILE_HEADER_ROLES]


def _write_target(path: Path, *, with_status: bool = False) -> None:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = "DB"
    headers = [*PROFILE_HEADER_ROLES, *CONTROL_HEADERS, "20260801"]
    if with_status:
        headers.append("상태")
    worksheet.append(headers)
    suffix = ["기존값"] if with_status else []
    worksheet.append(
        [
            *_profile("김기존", "2010-01-15", "옛학교"),
            "조식",
            "기사A",
            "001",
            1,
            *suffix,
        ]
    )
    worksheet.append(
        [
            *_profile("김기존", "20100115", "옛학교"),
            "중식",
            "기사A",
            "001",
            None,
            *suffix,
        ]
    )
    worksheet.append(
        [
            *_profile("이유지", "2011-02-20", "그대로학교"),
            "중식",
            "기사B",
            "002",
            1,
            *suffix,
        ]
    )
    worksheet.add_table(
        Table(
            displayName=TABLE_NAME,
            ref=f"A1:{openpyxl.utils.get_column_letter(len(headers))}4",
        )
    )
    workbook.save(path)
    workbook.close()


def _write_source(
    path: Path,
    *,
    alice_school: str = "최신학교",
    alice_birth: str = "2010.01.31",
    alice_sequence: str = "009",
    meals: str = "조식, 석식",
) -> None:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = "최신 명단"
    worksheet.append(["이름 열", "생일 열", "학교 최신", "식사 묶음", "새 연번"])
    worksheet.append(
        ["김기존", alice_birth, alice_school, "조식 / 중식", alice_sequence]
    )
    worksheet.append(["박신규", 20120322, "새학교", meals, "010"])
    workbook.save(path)
    workbook.close()


def _request(
    target: Path,
    source: Path,
    *,
    password: str = "",
) -> RosterMergeRequest:
    layout = read_source_layout(source, password=password)
    return RosterMergeRequest(
        target_path=target,
        source_path=source,
        target_header_mapping={
            role: role for role in (*PROFILE_HEADER_ROLES, *CONTROL_HEADERS)
        },
        source_layout=layout,
        source_columns={
            "성명": 1,
            "생년월일": 2,
            "학교명": 3,
            MERGE_MEAL_ROLE: 4,
            "연번": 5,
        },
        source_password=password,
    )


def _encrypt_workbook(
    plain_path: Path,
    encrypted_path: Path,
    *,
    password: str,
) -> Path:
    with plain_path.open("rb") as plain, encrypted_path.open("wb") as encrypted:
        office_file = msoffcrypto.OfficeFile(plain)
        assert isinstance(office_file, OOXMLFile)
        office_file.encrypt(password, encrypted)
    return encrypted_path


def test_source_layout_skips_title_rows_and_finds_roster_headers(tmp_path: Path):
    source = tmp_path / "latest.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.append(["2026년 신규 대상", "분당구"])
    worksheet.append([])
    worksheet.append(["연번", "이름", "생년월일", "학교", "급식 유형"])
    worksheet.append([1, "홍길동", "100101", "샘플학교", "중식"])
    workbook.save(source)
    workbook.close()

    layout = read_source_layout(source)

    assert layout.sheet_name == worksheet.title
    assert layout.header_row == 3
    assert layout.headers[:5] == ("연번", "이름", "생년월일", "학교", "급식 유형")


def test_password_protected_source_requires_correct_ephemeral_password(
    tmp_path: Path,
):
    target = tmp_path / "db.xlsx"
    plain_source = tmp_path / "latest_plain.xlsx"
    encrypted_source = tmp_path / "latest_encrypted.xlsx"
    _write_target(target)
    _write_source(plain_source)
    _encrypt_workbook(
        plain_source,
        encrypted_source,
        password="source-secret",
    )

    with pytest.raises(WorkbookPasswordRequiredError):
        read_source_layout(encrypted_source)
    with pytest.raises(WorkbookPasswordError):
        read_source_layout(encrypted_source, password="wrong-secret")

    request = _request(target, encrypted_source, password="source-secret")
    assert "source-secret" not in repr(request)

    result = merge_roster(
        request,
        now=datetime(2026, 8, 19, 9, 0, 0, tzinfo=timezone.utc),
    )

    assert result.updated_people == 1
    assert result.new_people == 1
    assert "source-secret" not in result.log_path.read_bytes().decode(
        "latin-1",
        errors="ignore",
    )


def test_merge_backs_up_updates_adds_statuses_and_writes_full_log(tmp_path: Path):
    target = tmp_path / "db.xlsx"
    source = tmp_path / "latest.xlsx"
    _write_target(target)
    _write_source(source)

    result = merge_roster(
        _request(target, source),
        now=datetime(2026, 8, 19, 10, 30, 45, tzinfo=timezone.utc),
    )

    assert result.updated_people == 1
    assert result.existing_people == 0
    assert result.new_people == 1
    assert result.untouched_people == 1
    assert result.changed_cells == 6
    assert result.added_rows == 2
    assert result.backup_path.name == "db_백업_20260819_103045.xlsx"
    assert result.log_path.name == "db_명단합치기_로그_20260819_103045.xlsx"

    backup = openpyxl.load_workbook(result.backup_path)
    try:
        sheet = backup["DB"]
        assert sheet["B2"].value == "옛학교"
        assert "상태" not in [cell.value for cell in sheet[1]]
    finally:
        backup.close()

    workbook = openpyxl.load_workbook(target)
    try:
        sheet = workbook["DB"]
        headers = [cell.value for cell in sheet[1]]
        columns = {header: index for index, header in enumerate(headers, start=1)}
        assert sheet.tables[TABLE_NAME].ref.endswith("S6")
        assert [
            sheet.cell(row=row, column=columns["학교명"]).value for row in (2, 3)
        ] == [
            "최신학교",
            "최신학교",
        ]
        assert [
            sheet.cell(row=row, column=columns["연번"]).value for row in (2, 3)
        ] == [
            "009",
            "009",
        ]
        assert [
            sheet.cell(row=row, column=columns["상태"]).value for row in range(2, 7)
        ] == [
            "갱신",
            "갱신",
            None,
            "신규",
            "신규",
        ]
        assert [
            sheet.cell(row=row, column=columns["식종"]).value for row in (5, 6)
        ] == [
            "조식",
            "석식",
        ]
        assert all(
            sheet.cell(row=row, column=columns["20260801"]).value is None
            for row in (5, 6)
        )
    finally:
        workbook.close()

    log = openpyxl.load_workbook(result.log_path)
    try:
        history = log["변경이력"]
        rows = list(history.iter_rows(min_row=2, values_only=True))
        assert len(rows) == 8
        assert {(row[0], row[5]) for row in rows} == {
            ("갱신", "학교명"),
            ("갱신", "생년월일"),
            ("갱신", "연번"),
            ("신규", "행 추가"),
        }
        assert log["요약"]["B4"].value == str(result.backup_path)
    finally:
        log.close()

    loaded = AnnualWorkbook(target).load()
    assert {person.extra["성명"] for person in loaded.roster} == {
        "김기존",
        "이유지",
        "박신규",
    }


def test_matching_unchanged_person_is_existing_and_absent_person_is_blank(
    tmp_path: Path,
):
    target = tmp_path / "db.xlsx"
    source = tmp_path / "latest.xlsx"
    _write_target(target, with_status=True)
    _write_source(
        source,
        alice_school="옛학교",
        alice_birth="2010-01-15",
        alice_sequence="001",
    )

    result = merge_roster(
        _request(target, source),
        now=datetime(2026, 8, 19, 11, 0, 0, tzinfo=timezone.utc),
    )

    assert result.updated_people == 0
    assert result.existing_people == 1
    workbook = openpyxl.load_workbook(target)
    try:
        sheet = workbook["DB"]
        status_column = next(cell.column for cell in sheet[1] if cell.value == "상태")
        assert sheet.cell(2, status_column).value == "기존"
        assert sheet.cell(3, status_column).value == "기존"
        assert sheet.cell(4, status_column).value is None
    finally:
        workbook.close()


def test_invalid_combined_meal_aborts_before_backup_or_target_change(tmp_path: Path):
    target = tmp_path / "db.xlsx"
    source = tmp_path / "latest.xlsx"
    _write_target(target)
    _write_source(source, meals="대상 식사 미정")
    original = target.read_bytes()

    with pytest.raises(RosterMergeError, match="합쳐진 식종"):
        merge_roster(
            _request(target, source),
            now=datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc),
        )

    assert target.read_bytes() == original
    assert list(tmp_path.glob("*백업*")) == []
    assert list(tmp_path.glob("*로그*")) == []


def test_added_rows_do_not_overwrite_content_below_db_table(tmp_path: Path):
    target = tmp_path / "db.xlsx"
    source = tmp_path / "latest.xlsx"
    _write_target(target)
    _write_source(source)

    workbook = openpyxl.load_workbook(target)
    try:
        workbook["DB"]["A5"] = "표 아래 보존할 메모"
        workbook.save(target)
    finally:
        workbook.close()

    merge_roster(
        _request(target, source),
        now=datetime(2026, 8, 19, 13, 0, 0, tzinfo=timezone.utc),
    )

    merged = openpyxl.load_workbook(target)
    try:
        sheet = merged["DB"]
        assert sheet.tables[TABLE_NAME].ref.endswith("S6")
        assert sheet["A7"].value == "표 아래 보존할 메모"
    finally:
        merged.close()
