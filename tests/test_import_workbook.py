"""Excel inspection and typed-observation Adapter tests."""

from datetime import date
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile

import msoffcrypto
import openpyxl
import pytest
from openpyxl.utils import get_column_letter
from openpyxl.worksheet._read_only import ReadOnlyWorksheet
from openpyxl.worksheet.table import Table

import src.importing.workbook as workbook_module
from msoffcrypto.format.ooxml import OOXMLFile

from src.domain.roster import Meal
from src.importing.contracts import (
    ImportMappingProfile,
    PhoneNormalization,
    ProfileField,
)
from src.importing.profile_history import StoredImportProfile
from src.importing.workbook import (
    WorkbookImportPasswordError,
    WorkbookImportPasswordRequiredError,
    WorkbookStructureError,
    inspect_workbook,
    parse_workbook,
    proposed_profile,
)


def _write_survey(path: Path, *, invalid_flag: object | None = None) -> Path:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "조사표"
    worksheet["A1"] = "2026년 조사표"
    headers = {
        "A": "원본 연번",
        "C": "취학유형",
        "D": "학교명",
        "E": "학년",
        "F": "반",
        "G": "성명",
        "H": "생년월일",
        "I": "세대주 성명",
        "J": "관계",
        "L": "관할동",
        "M": "기존 주소",
        "N": "변경주소",
        "O": "핸드폰",
        "S": "연중 조식",
        "T": "연중 석식",
        "U": "토공휴일 중식",
        "V": "방학 중식",
        "AI": "토공휴일 석식",
    }
    for column, header in headers.items():
        worksheet[f"{column}2"] = header
    values = {
        "A": "001",
        "C": "초등",
        "D": "샘플초",
        "E": 5,
        "F": 2,
        "G": "홍길동",
        "H": 20150302,
        "I": "홍보호",
        "J": "부",
        "L": "정자동",
        "M": "경기 성남시 이전주소",
        "N": None,
        "O": 1012345678,
        "S": 1 if invalid_flag is None else invalid_flag,
        "T": 0,
        "U": "사용하지 않을 개인정보",
        "V": "1",
        "AI": "사용하지 않을 개인정보",
    }
    for column, value in values.items():
        worksheet[f"{column}3"] = value
    second = {
        "A": "002",
        "G": "김새봄",
        "H": date(2016, 4, 5),
        "M": "이전 주소",
        "N": "경기 성남시 변경 주소",
        "O": "010-9999-0000",
        "S": 0,
        "T": 1,
        "V": 0,
    }
    for column, value in second.items():
        worksheet[f"{column}4"] = value
    workbook.save(path)
    workbook.close()
    return path


def _survey_profile() -> ImportMappingProfile:
    return ImportMappingProfile(
        profile_id=UUID("81000000-0000-0000-0000-000000000001"),
        version=1,
        name="분당 조사표",
        source_kind="survey-xlsx",
        profile_columns={
            ProfileField.NAME: "G",
            ProfileField.BIRTH_DATE: "H",
            ProfileField.SCHOOL_TYPE: "C",
            ProfileField.SCHOOL_NAME: "D",
            ProfileField.GRADE: "E",
            ProfileField.CLASS_NAME: "F",
            ProfileField.HOUSEHOLD_HEAD_NAME: "I",
            ProfileField.RELATIONSHIP_TO_CHILD: "J",
            ProfileField.JURISDICTION: "L",
            ProfileField.ROAD_ADDRESS: "N",
            ProfileField.MOBILE_PHONE: "O",
        },
        meal_flag_columns={
            Meal.BREAKFAST: "S",
            Meal.DINNER: "T",
            Meal.LUNCH: "V",
        },
        source_reference_column="A",
        header_row=2,
        data_start_row=3,
        fallback_profile_columns={ProfileField.ROAD_ADDRESS: ("M",)},
        phone_normalization=PhoneNormalization.KOREAN,
        ignored_columns=("U", "AI"),
        provider_value="분당구 급식센터",
    )


def _birth_date_profile() -> ImportMappingProfile:
    return ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000098"),
        1,
        "생년월일 형식 확인",
        "roster-xlsx",
        {
            ProfileField.NAME: "A",
            ProfileField.BIRTH_DATE: "B",
        },
        combined_meals_column="C",
    )


def _encrypt(plain: Path, encrypted: Path, password: str) -> Path:
    with plain.open("rb") as source, encrypted.open("wb") as destination:
        office_file = msoffcrypto.OfficeFile(source)
        assert isinstance(office_file, OOXMLFile)
        office_file.encrypt(password, destination)
    return encrypted


def test_table_mode_uses_offset_bounds_and_excludes_totals_and_outside_cells(tmp_path: Path):
    path = tmp_path / "offset.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "원본"
    worksheet["A1"] = "표 밖"
    worksheet["D7"] = "성명"
    worksheet["E7"] = "생년월일"
    worksheet["F7"] = "식종"
    worksheet["D8"] = "표 안 아동"
    worksheet["E8"] = "20150302"
    worksheet["F8"] = "조식"
    worksheet["D10"] = "합계"
    worksheet["D11"] = "표 밖 아동"
    worksheet["E11"] = "20160403"
    worksheet["F11"] = "중식"
    worksheet.add_table(Table(displayName="명단", ref="D7:F10", totalsRowCount=1))
    workbook.save(path)
    workbook.close()

    inspection = inspect_workbook(path, require_table=True)
    assert inspection.selected_table == "명단"
    assert inspection.tables[0].ref == "D7:F10"
    assert tuple(column.letter for column in inspection.columns) == ("D", "E", "F")
    assert tuple(row.row_number for row in inspection.preview_rows) == (8,)
    assert inspection.preview_rows[0].values == ("표 안 아동", "20150302", "조식")

    stale_profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000123"),
        1,
        "위치 무시",
        "roster-xlsx",
        {ProfileField.NAME: "D", ProfileField.BIRTH_DATE: "E"},
        combined_meals_column="F",
        header_row=1,
        data_start_row=2,
    )
    preview = parse_workbook(path, stale_profile, table_name="명단")
    assert preview.can_continue
    assert preview.profile.header_row == 7
    assert preview.profile.data_start_row == 8
    assert tuple(person.name for person in preview.observations) == ("표 안 아동",)
    assert preview.observations[0].source.row_numbers == (8,)
    with pytest.raises(WorkbookStructureError, match="범위 밖"):
        parse_workbook(path, stale_profile, table_name="명단", cell_overrides={(11, "D"): "외부 수정"})
    outside_column_profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000124"), 1,
        "외부 열", "roster-xlsx", {ProfileField.NAME: "A"},
        default_meals=(Meal.LUNCH,),
    )
    with pytest.raises(WorkbookStructureError, match="연결한 원본 열"):
        parse_workbook(path, outside_column_profile, table_name="명단")


def test_table_mode_accepts_more_than_365_columns_and_mappings_after_iv(tmp_path: Path):
    path = tmp_path / "wide-table.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    for index in range(1, 371):
        worksheet.cell(1, index, f"필드 {index}")
    worksheet.cell(1, 365, "성명")
    worksheet.cell(1, 366, "식종")
    worksheet.cell(2, 365, "넓은 표 아동")
    worksheet.cell(2, 366, "중식")
    worksheet.add_table(Table(displayName="명단", ref=f"A1:{get_column_letter(370)}2"))
    workbook.save(path)
    workbook.close()

    inspection = inspect_workbook(path, require_table=True)
    assert len(inspection.columns) == 370
    assert inspection.columns[364].letter == get_column_letter(365)
    profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000128"), 1,
        "넓은 표", "roster-xlsx",
        {ProfileField.NAME: get_column_letter(365)},
        combined_meals_column=get_column_letter(366),
    )
    preview = parse_workbook(path, profile, table_name="명단")
    assert preview.can_continue
    assert tuple(person.name for person in preview.observations) == ("넓은 표 아동",)
    assert preview.observations[0].source.row_numbers == (2,)


def test_table_mode_accepts_narrow_offset_table_after_iv(tmp_path: Path):
    path = tmp_path / "offset-after-iv.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    first = get_column_letter(300)
    second = get_column_letter(301)
    last = get_column_letter(302)
    worksheet.cell(7, 300, "성명")
    worksheet.cell(7, 301, "식종")
    worksheet.cell(7, 302, "비고")
    worksheet.cell(8, 300, "뒤쪽 표 아동")
    worksheet.cell(8, 301, "조식")
    worksheet.add_table(Table(displayName="명단", ref=f"{first}7:{last}8"))
    workbook.save(path)
    workbook.close()

    inspection = inspect_workbook(path, require_table=True)
    assert tuple(column.letter for column in inspection.columns) == (first, second, last)
    assert inspection.preview_rows[0].values == ("뒤쪽 표 아동", "조식", "")
    profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000129"), 1,
        "뒤쪽 표", "roster-xlsx", {ProfileField.NAME: first},
        combined_meals_column=second,
    )
    assert parse_workbook(path, profile, table_name="명단").can_continue
    invalid_profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000130"), 1,
        "표 밖 열", "roster-xlsx", {ProfileField.NAME: "IV"},
        combined_meals_column=second,
    )
    with pytest.raises(WorkbookStructureError, match="연결한 원본 열"):
        parse_workbook(path, invalid_profile, table_name="명단")


def test_table_mode_accepts_xfd_and_rejects_columns_beyond_it(tmp_path: Path):
    path = tmp_path / "last-excel-column.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet["XFC1"] = "성명"
    worksheet["XFD1"] = "식종"
    worksheet["XFC2"] = "마지막 열 아동"
    worksheet["XFD2"] = "중식"
    worksheet.add_table(Table(displayName="명단", ref="XFC1:XFD2"))
    workbook.save(path)
    workbook.close()
    inspection = inspect_workbook(path, require_table=True)
    assert tuple(column.letter for column in inspection.columns) == ("XFC", "XFD")
    profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000131"), 1,
        "끝 열", "roster-xlsx", {ProfileField.NAME: "XFC"},
        combined_meals_column="XFD",
    )
    preview = parse_workbook(
        path, profile, table_name="명단", cell_overrides={(2, "XFD"): "조식"}
    )
    assert preview.can_continue
    assert preview.corrected_cells == ((2, "XFD"),)
    assert preview.observations[0].meals == (Meal.BREAKFAST,)

    invalid_path = tmp_path / "beyond-excel-column.xlsx"
    invalid_workbook = openpyxl.Workbook()
    invalid_sheet = invalid_workbook.active
    invalid_sheet["XFE1"] = "성명"
    invalid_sheet["XFE2"] = "불가"
    invalid_sheet.add_table(Table(displayName="명단", ref="XFE1:XFE2"))
    invalid_workbook.save(invalid_path)
    invalid_workbook.close()
    with pytest.raises(WorkbookStructureError, match="XFD"):
        inspect_workbook(invalid_path, require_table=True)


def test_table_mode_lists_all_sheets_and_defaults_to_named_table(tmp_path: Path):
    path = tmp_path / "tables.xlsx"
    workbook = openpyxl.Workbook()
    first = workbook.active
    first.title = "첫 시트"
    first.append(("성명", "식종"))
    first.append(("첫 아동", "조식"))
    first.add_table(Table(displayName="기타", ref="A1:B2"))
    second = workbook.create_sheet("둘째 시트")
    second.append(("성명", "식종"))
    second.append(("둘째 아동", "중식"))
    second.add_table(Table(displayName="명단", ref="A1:B2"))
    workbook.save(path)
    workbook.close()

    inspection = inspect_workbook(path, require_table=True)
    assert tuple((table.name, table.sheet_name, table.ref) for table in inspection.tables) == (
        ("기타", "첫 시트", "A1:B2"), ("명단", "둘째 시트", "A1:B2")
    )
    assert inspection.selected_table == "명단"
    assert inspection.selected_sheet == "둘째 시트"
    assert inspect_workbook(path, require_table=True, table_name="기타").selected_sheet == "첫 시트"
    with pytest.raises(WorkbookStructureError, match="찾을 수 없습니다"):
        inspect_workbook(path, require_table=True, table_name="없는 표")


def test_table_mode_requires_excel_table_without_legacy_fallback(tmp_path: Path):
    path = tmp_path / "plain.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.append(("성명", "식종"))
    workbook.active.append(("아동", "중식"))
    workbook.save(path)
    workbook.close()
    assert inspect_workbook(path).columns[0].header == "성명"
    with pytest.raises(WorkbookStructureError, match="삽입 → 표"):
        inspect_workbook(path, require_table=True)


def test_table_mode_streams_selected_sheet_without_parsing_unrelated_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    path = tmp_path / "unrelated-cells.xlsx"
    workbook = openpyxl.Workbook()
    selected = workbook.active
    selected.append(("성명", "식종"))
    selected.append(("표 안 아동", "중식"))
    selected.add_table(Table(displayName="명단", ref="A1:B2"))
    unrelated = workbook.create_sheet("다른 자료")
    for row in range(1, 101):
        for column in range(1, 21):
            unrelated.cell(row, column, "가져오지 않을 값")
    workbook.save(path)
    workbook.close()

    original_load = workbook_module.openpyxl.load_workbook
    original_iter = ReadOnlyWorksheet.iter_rows
    load_modes: list[bool] = []

    def streaming_load(*args, **kwargs):
        load_modes.append(kwargs["read_only"])
        return original_load(*args, **kwargs)

    def selected_rows_only(self, *args, **kwargs):
        if self.title == "다른 자료":
            raise AssertionError("표 밖 시트의 셀을 읽었습니다")
        return original_iter(self, *args, **kwargs)

    monkeypatch.setattr(workbook_module.openpyxl, "load_workbook", streaming_load)
    monkeypatch.setattr(
        ReadOnlyWorksheet, "iter_rows", selected_rows_only
    )
    inspection = inspect_workbook(path, require_table=True)
    profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000127"), 1,
        "읽기 전용", "roster-xlsx", {ProfileField.NAME: "A"},
        combined_meals_column="B",
    )
    preview = parse_workbook(path, profile, table_name="명단")

    assert inspection.preview_rows[0].values == ("표 안 아동", "중식")
    assert preview.can_continue
    assert load_modes == [True, True]


@pytest.mark.parametrize("target", ("/xl/tables/table1.xml", "../tables/table1.xml"))
def test_table_mode_resolves_absolute_and_relative_relationship_targets(
    tmp_path: Path, target: str,
):
    original = tmp_path / "original.xlsx"
    rewritten = tmp_path / "rewritten.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "식종"))
    worksheet.append(("아동", "중식"))
    worksheet.add_table(Table(displayName="명단", ref="A1:B2"))
    workbook.save(original)
    workbook.close()
    with ZipFile(original) as source, ZipFile(rewritten, "w") as destination:
        for member in source.infolist():
            content = source.read(member.filename)
            if member.filename == "xl/worksheets/_rels/sheet1.xml.rels":
                content = content.replace(
                    b'Target="/xl/tables/table1.xml"',
                    f'Target="{target}"'.encode("utf-8"),
                )
            destination.writestr(member, content)

    inspection = inspect_workbook(rewritten, require_table=True)
    assert inspection.selected_table == "명단"
    assert inspection.preview_rows[0].values == ("아동", "중식")


def test_table_mode_keeps_reading_after_one_hundred_blank_body_rows(tmp_path: Path):
    path = tmp_path / "gaps.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "식종"))
    worksheet.append(("첫 아동", "조식"))
    worksheet["A103"] = "뒤 아동"
    worksheet["B103"] = "중식"
    worksheet.add_table(Table(displayName="명단", ref="A1:B103"))
    workbook.save(path)
    workbook.close()
    profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000125"), 1,
        "빈 행", "roster-xlsx", {ProfileField.NAME: "A"},
        combined_meals_column="B",
    )

    preview = parse_workbook(path, profile, table_name="명단")
    assert preview.can_continue
    assert tuple(person.name for person in preview.observations) == ("첫 아동", "뒤 아동")
    assert preview.observations[-1].source.row_numbers == (103,)


def test_table_mode_ignores_unsupported_other_table(tmp_path: Path):
    path = tmp_path / "mixed.xlsx"
    workbook = openpyxl.Workbook()
    first = workbook.active
    first["A1"] = "첫 값"
    first["A2"] = "둘째 값"
    first.add_table(Table(displayName="머리글없음", ref="A1:A2", headerRowCount=0))
    second = workbook.create_sheet("명단 시트")
    second.append(("성명", "식종"))
    second.append(("아동", "중식"))
    second.add_table(Table(displayName="명단", ref="A1:B2"))
    workbook.save(path)
    workbook.close()

    inspection = inspect_workbook(path, require_table=True)
    assert tuple(table.name for table in inspection.tables) == ("머리글없음", "명단")
    assert inspection.selected_table == "명단"
    with pytest.raises(WorkbookStructureError, match="머리글이 없습니다"):
        inspect_workbook(path, require_table=True, table_name="머리글없음")


def test_encrypted_table_mode_retries_password_and_reads_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    plain = tmp_path / "plain.xlsx"
    encrypted = tmp_path / "encrypted.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "식종"))
    worksheet.append(("암호 아동", "중식"))
    worksheet.add_table(Table(displayName="명단", ref="A1:B2"))
    workbook.save(plain)
    workbook.close()
    _encrypt(plain, encrypted, "secret")

    with pytest.raises(WorkbookImportPasswordRequiredError):
        inspect_workbook(encrypted, require_table=True)
    with pytest.raises(WorkbookImportPasswordError):
        inspect_workbook(encrypted, require_table=True, password="wrong")
    inspection = inspect_workbook(encrypted, require_table=True, password="secret")
    assert inspection.selected_table == "명단"
    original_decrypt = OOXMLFile.decrypt
    decrypt_count = 0

    def counted_decrypt(self, *args, **kwargs):
        nonlocal decrypt_count
        decrypt_count += 1
        return original_decrypt(self, *args, **kwargs)

    monkeypatch.setattr(OOXMLFile, "decrypt", counted_decrypt)
    with pytest.raises(WorkbookStructureError, match="찾을 수 없습니다") as error:
        inspect_workbook(
            encrypted, require_table=True, table_name="없는표", password="secret"
        )
    assert tuple(table.name for table in error.value.tables) == ("명단",)
    assert decrypt_count == 1
    profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000126"), 1,
        "암호 표", "roster-xlsx", {ProfileField.NAME: "A"},
        combined_meals_column="B",
    )
    assert parse_workbook(encrypted, profile, table_name="명단", password="secret").can_continue


def test_inspection_finds_header_row_columns_and_small_preview(tmp_path: Path):
    path = _write_survey(tmp_path / "survey.xlsx")

    inspection = inspect_workbook(
        path,
        sheet_name="조사표",
        header_row=2,
        data_start_row=3,
    )

    assert inspection.source_filename == "survey.xlsx"
    assert inspection.sheet_names == ("조사표",)
    assert inspection.columns[6].label == "G · 성명"
    assert inspection.columns[-1].letter == "AI"
    assert tuple(row.row_number for row in inspection.preview_rows) == (3, 4)
    assert len(inspection.header_fingerprint) == 64


def test_blank_leading_cells_keep_their_source_row_in_read_only_mode(
    tmp_path: Path,
):
    path = tmp_path / "blank-leading-cell.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "명단"
    worksheet.append(("미사용", "성명", "생년월일", "식종"))
    worksheet.append((None, "홍길동", "20150302", "조식 / 중식"))
    workbook.save(path)
    workbook.close()

    inspection = inspect_workbook(path)
    profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000099"),
        1,
        "앞 빈칸 명단",
        "roster-xlsx",
        {
            ProfileField.NAME: "B",
            ProfileField.BIRTH_DATE: "C",
        },
        combined_meals_column="D",
        ignored_columns=("A",),
    )
    preview = parse_workbook(path, profile)

    assert inspection.preview_rows[0].row_number == 2
    assert preview.can_continue
    assert preview.observations[0].source.row_numbers == (2,)
    assert preview.observations[0].meals == (Meal.BREAKFAST, Meal.LUNCH)


def test_birth_date_column_and_cells_are_optional(tmp_path: Path):
    path = tmp_path / "birth-date-optional.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "식종", "학교명"))
    worksheet.append(("생일미입력", "조식", None))
    workbook.save(path)
    workbook.close()
    profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000097"),
        1,
        "생년월일 없는 명단",
        "roster-xlsx",
        {
            ProfileField.NAME: "A",
            ProfileField.SCHOOL_NAME: "C",
        },
        combined_meals_column="B",
    )

    preview = parse_workbook(path, profile)

    assert preview.can_continue
    assert preview.issues == ()
    assert preview.observations[0].birth_date is None
    assert preview.observations[0].profile_values[ProfileField.SCHOOL_NAME] is None


def test_missing_name_row_is_excluded_without_blocking_other_people(
    tmp_path: Path,
):
    path = tmp_path / "one-missing-name.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "생년월일", "식종"))
    # 성명이 없으면 행 전체가 제외되므로 다른 셀의 형식은 가져오기를 막지 않는다.
    worksheet.append((None, "날짜 아님", "알 수 없는 식종"))
    worksheet.append(("정상대상자", None, "중식"))
    workbook.save(path)
    workbook.close()

    preview = parse_workbook(path, _birth_date_profile())

    assert preview.can_continue
    assert tuple(item.name for item in preview.observations) == ("정상대상자",)
    assert len(preview.notices) == 1
    assert preview.notices[0].row_number == 2
    assert "가져오지 않습니다" in preview.notices[0].message
    assert preview.blocking_issues == ()


def test_six_digit_birth_dates_infer_the_latest_nonfuture_century(
    tmp_path: Path,
):
    path = tmp_path / "six-digit-birth-dates.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "생년월일", "식종"))
    worksheet.append(("문자값", "150302", "조식"))
    worksheet.append(("숫자값", 990405, "중식"))
    worksheet.append(("선행0숫자값", 10203, "석식"))
    workbook.save(path)
    workbook.close()

    preview = parse_workbook(path, _birth_date_profile())

    current_year = date.today().year

    def inferred_year(short_year: int) -> int:
        candidate = current_year - current_year % 100 + short_year
        return candidate - 100 if candidate > current_year else candidate

    assert preview.can_continue
    observations = {item.name: item for item in preview.observations}
    assert observations["문자값"].birth_date == date(inferred_year(15), 3, 2)
    assert observations["숫자값"].birth_date == date(inferred_year(99), 4, 5)
    assert observations["선행0숫자값"].birth_date == date(
        inferred_year(1), 2, 3
    )


def test_resident_registration_numbers_import_only_their_birth_dates(
    tmp_path: Path,
):
    path = tmp_path / "resident-numbers.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "생년월일", "식종"))
    worksheet.append(("홍길동", "150302-3******", "조식"))
    worksheet.append(("김새봄", "9904052123456", "중식"))
    worksheet.append(("외국인아동", 102037123456, "석식"))
    workbook.save(path)
    workbook.close()

    preview = parse_workbook(path, _birth_date_profile())

    assert preview.can_continue
    observations = {item.name: item for item in preview.observations}
    assert observations["홍길동"].birth_date == date(2015, 3, 2)
    assert observations["김새봄"].birth_date == date(1999, 4, 5)
    assert observations["외국인아동"].birth_date == date(2001, 2, 3)
    stored_observations = repr(preview.observations)
    assert "150302-3******" not in stored_observations
    assert "9904052123456" not in stored_observations


def test_resident_number_requires_visible_century_and_valid_birth_date(
    tmp_path: Path,
):
    path = tmp_path / "invalid-resident-numbers.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "생년월일", "식종"))
    worksheet.append(("날짜오류", "150232-3******", "조식"))
    worksheet.append(("연도오류", "150302-*******", "중식"))
    workbook.save(path)
    workbook.close()

    preview = parse_workbook(path, _birth_date_profile())

    assert not preview.can_continue
    messages = tuple(issue.message for issue in preview.issues)
    assert any("앞 6자리의 날짜가 올바르지 않습니다" in item for item in messages)
    assert any("뒷자리 첫 숫자가 가려져" in item for item in messages)
    assert "150232" not in " ".join(messages)
    assert "150302" not in " ".join(messages)


def test_in_memory_cell_corrections_fix_all_errors_without_changing_excel(
    tmp_path: Path,
):
    path = tmp_path / "correct-inside-import.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "생년월일", "식종"))
    worksheet.append(("수정대상자", "날짜 아님", "야식"))
    workbook.save(path)
    workbook.close()

    before = parse_workbook(path, _birth_date_profile())

    assert not before.can_continue
    issues = {(issue.row_number, issue.column): issue for issue in before.issues}
    assert issues[(2, "B")].current_value == "날짜 아님"
    assert issues[(2, "C")].current_value == "야식"

    corrected = parse_workbook(
        path,
        _birth_date_profile(),
        cell_overrides={
            (2, "B"): "20150302",
            (2, "C"): "조식 / 중식",
        },
    )

    assert corrected.can_continue
    assert corrected.corrected_cells == ((2, "B"), (2, "C"))
    assert corrected.observations[0].birth_date == date(2015, 3, 2)
    assert corrected.observations[0].meals == (Meal.BREAKFAST, Meal.LUNCH)
    unchanged = openpyxl.load_workbook(path, data_only=False)
    try:
        assert unchanged.active["B2"].value == "날짜 아님"
        assert unchanged.active["C2"].value == "야식"
    finally:
        unchanged.close()


def test_survey_mapping_uses_fallback_address_and_ignores_unused_columns(
    tmp_path: Path,
):
    path = _write_survey(tmp_path / "survey.xlsx")

    preview = parse_workbook(path, _survey_profile(), sheet_name="조사표")

    assert preview.can_continue
    assert preview.issues == ()
    assert len(preview.observations) == 2
    first = preview.observations[0]
    assert first.name == "홍길동"
    assert first.birth_date == date(2015, 3, 2)
    assert first.profile_values[ProfileField.ROAD_ADDRESS] == "경기 성남시 이전주소"
    assert first.profile_values[ProfileField.MOBILE_PHONE] == "010-1234-5678"
    assert first.profile_values[ProfileField.PROVIDER] == "분당구 급식센터"
    assert first.meals == (Meal.BREAKFAST, Meal.LUNCH)
    assert first.source.external_reference == "001"
    assert "사용하지 않을 개인정보" not in repr(first)
    assert preview.observations[1].profile_values[ProfileField.ROAD_ADDRESS] == (
        "경기 성남시 변경 주소"
    )
    assert (
        preview.observations[1].profile_values[ProfileField.PROVIDER]
        == "분당구 급식센터"
    )


def test_unknown_meal_flag_blocks_continue_instead_of_guessing(tmp_path: Path):
    path = _write_survey(tmp_path / "survey.xlsx", invalid_flag="확인")

    preview = parse_workbook(path, _survey_profile(), sheet_name="조사표")

    assert not preview.can_continue
    assert any(issue.row_number == 3 and issue.column == "S" for issue in preview.issues)
    assert "급식을 받는 값이나 받지 않는 값" in preview.issues[0].message


def test_split_meal_rows_merge_to_one_observation_with_per_meal_assignments(
    tmp_path: Path,
):
    path = tmp_path / "split.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "운영명단"
    worksheet.append(("연번", "성명", "생년월일", "식종", "담당", "순서"))
    worksheet.append((1, "홍길동", "20150302", "조식", "기사A", "001"))
    worksheet.append((2, "홍길동", "20150302", "석식", "기사B", "002"))
    workbook.save(path)
    workbook.close()
    profile = ImportMappingProfile(
        profile_id=UUID("81000000-0000-0000-0000-000000000002"),
        version=1,
        name="식종별 운영명단",
        source_kind="roster-split-xlsx",
        profile_columns={
            ProfileField.NAME: "B",
            ProfileField.BIRTH_DATE: "C",
        },
        combined_meals_column="D",
        driver_column="E",
        sequence_column="F",
        source_reference_column="A",
    )

    preview = parse_workbook(path, profile)

    assert preview.can_continue
    assert len(preview.observations) == 1
    observation = preview.observations[0]
    assert observation.source.row_numbers == (2, 3)
    assert observation.source.external_reference == "1 / 2"
    assert observation.meals == (Meal.BREAKFAST, Meal.DINNER)
    assert tuple(item.meal for item in observation.assignments) == (
        Meal.BREAKFAST,
        Meal.DINNER,
    )
    assert tuple(item.sequence for item in observation.assignments) == ("001", "002")


def test_combined_meal_cell_matches_meal_names_inside_longer_text(tmp_path: Path):
    path = tmp_path / "combined-meal-partial-match.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "생년월일", "식종"))
    worksheet.append(("부분일치아동", "20150302", "조식, 석식, 중식(방중)"))
    workbook.save(path)
    workbook.close()

    preview = parse_workbook(path, _birth_date_profile())

    assert preview.can_continue
    assert preview.issues == ()
    assert preview.observations[0].meals == tuple(Meal)


def test_duplicate_rows_choose_latest_school_stage_merge_notes_and_drop_str_phone(
    tmp_path: Path,
):
    path = tmp_path / "school-progression.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(
        (
            "성명",
            "생년월일",
            "학교 유형",
            "학교명",
            "학년",
            "비고",
            "핸드폰",
            "식종",
            "담당",
            "순서",
        )
    )
    worksheet.append(
        (
            "김 아동",
            "20100101",
            "중학교",
            "가람 중학교",
            "3 학년",
            "이전 메모",
            "str placeholder",
            "조식",
            "기사 A",
            "0 01",
        )
    )
    worksheet.append(
        (
            "김아동",
            "20100101",
            "고등학교",
            "나래고등학교",
            "1학년",
            "진학 메모",
            "010-1234-5678",
            "중식",
            "기사B",
            "002",
        )
    )
    worksheet.append(
        (
            "김아동",
            "20100101",
            "고등학교",
            "나래 고등학교",
            "2학년",
            "현재 메모",
            None,
            "석식",
            "기사C",
            "003",
        )
    )
    workbook.save(path)
    workbook.close()
    profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000091"),
        1,
        "진학 이력 명단",
        "roster-split-xlsx",
        {
            ProfileField.NAME: "A",
            ProfileField.BIRTH_DATE: "B",
            ProfileField.SCHOOL_TYPE: "C",
            ProfileField.SCHOOL_NAME: "D",
            ProfileField.GRADE: "E",
            ProfileField.NOTE: "F",
            ProfileField.MOBILE_PHONE: "G",
        },
        combined_meals_column="H",
        driver_column="I",
        sequence_column="J",
    )

    preview = parse_workbook(path, profile)

    assert preview.can_continue
    assert preview.issues == ()
    assert len(preview.observations) == 1
    observation = preview.observations[0]
    assert observation.source.row_numbers == (2, 3, 4)
    assert observation.profile_values[ProfileField.SCHOOL_TYPE] == "고등학교"
    assert observation.profile_values[ProfileField.SCHOOL_NAME] == "나래 고등학교"
    assert observation.profile_values[ProfileField.GRADE] == "2학년"
    assert observation.profile_values[ProfileField.NOTE] == (
        "이전 메모 / 진학 메모 / 현재 메모"
    )
    assert observation.profile_values[ProfileField.MOBILE_PHONE] == (
        "010-1234-5678"
    )
    assert tuple(item.meal for item in observation.assignments) == tuple(Meal)
    assert tuple(item.sequence for item in observation.assignments) == (
        "0 01",
        "002",
        "003",
    )


def test_duplicate_move_uses_latest_location_and_keeps_every_mobile_phone(
    tmp_path: Path,
):
    path = tmp_path / "move-location-and-phones.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(
        ("성명", "생년월일", "주소", "관할동", "전화번호", "핸드폰", "식종")
    )
    worksheet.append(
        (
            "이사아동",
            "20150302",
            "이전 주소 1",
            "이전동",
            "ｓｔｒ",
            "010-1111-2222",
            "조식",
        )
    )
    worksheet.append(
        (
            "이사아동",
            "20150302",
            "현재 주소 9",
            "현재동",
            "STR placeholder",
            "010-3333-4444",
            "중식",
        )
    )
    workbook.save(path)
    workbook.close()
    profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000096"),
        1,
        "이사 연락처 명단",
        "roster-split-xlsx",
        {
            ProfileField.NAME: "A",
            ProfileField.BIRTH_DATE: "B",
            ProfileField.ROAD_ADDRESS: "C",
            ProfileField.JURISDICTION: "D",
            ProfileField.TELEPHONE: "E",
            ProfileField.MOBILE_PHONE: "F",
        },
        combined_meals_column="G",
    )

    preview = parse_workbook(path, profile)

    assert preview.can_continue
    assert preview.issues == ()
    observation = preview.observations[0]
    assert observation.profile_values[ProfileField.ROAD_ADDRESS] == "현재 주소 9"
    assert observation.profile_values[ProfileField.JURISDICTION] == "현재동"
    assert observation.profile_values[ProfileField.TELEPHONE] is None
    assert observation.profile_values[ProfileField.MOBILE_PHONE] == (
        "010-3333-4444"
    )
    assert observation.additional_mobile_phones == ("010-1111-2222",)
    resolutions = {
        item.field: item for item in preview.automatic_source_resolutions
    }
    assert set(resolutions) == {"road_address", "jurisdiction", "mobile_phone"}
    assert all(item.selected_row == 3 for item in resolutions.values())
    assert resolutions["mobile_phone"].manifest()["kind"] == "contact"


def test_phone_text_containing_str_is_kept_when_it_also_has_digits(tmp_path: Path):
    path = tmp_path / "phone-str-with-digits.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "생년월일", "전화번호", "식종"))
    worksheet.append(("전화아동", "20150302", "str 031-123-4567", "조식"))
    workbook.save(path)
    workbook.close()
    profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000097"),
        1,
        "전화번호 자리표시자 명단",
        "roster-xlsx",
        {
            ProfileField.NAME: "A",
            ProfileField.BIRTH_DATE: "B",
            ProfileField.TELEPHONE: "C",
        },
        combined_meals_column="D",
    )

    preview = parse_workbook(path, profile)

    assert preview.can_continue
    assert preview.observations[0].profile_values[ProfileField.TELEPHONE] == (
        "031-123-4567"
    )


def test_duplicate_telephone_numbers_become_numbered_contact_values(tmp_path: Path):
    path = tmp_path / "multiple-telephones.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "생년월일", "전화번호", "식종"))
    worksheet.append(("전화아동", "20150302", "031-111-2222", "조식"))
    worksheet.append(("전화아동", "20150302", "031-333-4444", "중식"))
    workbook.save(path)
    workbook.close()
    profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000099"),
        1,
        "복수 전화번호 명단",
        "roster-xlsx",
        {
            ProfileField.NAME: "A",
            ProfileField.BIRTH_DATE: "B",
            ProfileField.TELEPHONE: "C",
        },
        combined_meals_column="D",
    )

    preview = parse_workbook(path, profile)

    assert preview.can_continue
    observation = preview.observations[0]
    assert observation.profile_values[ProfileField.TELEPHONE] == "031-333-4444"
    assert observation.additional_telephones == ("031-111-2222",)
    resolution = next(
        item
        for item in preview.automatic_source_resolutions
        if item.field == "telephone"
    )
    assert resolution.field_label == "전화번호1"
    assert resolution.selected_row == 3
    assert resolution.manifest()["kind"] == "contact"


def test_preserved_phone_format_differences_do_not_create_duplicate_columns(
    tmp_path: Path,
):
    path = tmp_path / "same-phone-formatting.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "생년월일", "핸드폰", "식종"))
    worksheet.append(("같은번호아동", "20150302", "010-1111-2222", "조식"))
    worksheet.append(("같은번호아동", "20150302", "01011112222", "중식"))
    workbook.save(path)
    workbook.close()
    profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000098"),
        1,
        "전화번호 원형 유지 명단",
        "roster-xlsx",
        {
            ProfileField.NAME: "A",
            ProfileField.BIRTH_DATE: "B",
            ProfileField.MOBILE_PHONE: "C",
        },
        combined_meals_column="D",
        phone_normalization=PhoneNormalization.PRESERVE,
    )

    preview = parse_workbook(path, profile)

    assert preview.can_continue
    observation = preview.observations[0]
    assert observation.profile_values[ProfileField.MOBILE_PHONE] == "01011112222"
    assert observation.additional_mobile_phones == ()


def test_duplicate_comparison_ignores_all_whitespace(tmp_path: Path):
    path = tmp_path / "whitespace-equivalent.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "생년월일", "주소", "식종", "담당", "순서"))
    worksheet.append(
        ("홍 길동", "20150302", "경기 성남시", "조식", "기사 A", "0 01")
    )
    worksheet.append(
        ("홍길동", "20150302", "경기성남시", "조식", "기사A", "001")
    )
    workbook.save(path)
    workbook.close()
    profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000092"),
        1,
        "공백 비교 명단",
        "roster-split-xlsx",
        {
            ProfileField.NAME: "A",
            ProfileField.BIRTH_DATE: "B",
            ProfileField.ROAD_ADDRESS: "C",
        },
        combined_meals_column="D",
        driver_column="E",
        sequence_column="F",
    )

    preview = parse_workbook(path, profile)

    assert preview.can_continue
    assert preview.issues == ()
    assert len(preview.observations) == 1
    assert preview.observations[0].source.row_numbers == (2, 3)
    assert len(preview.observations[0].assignments) == 1


def test_duplicate_addresses_choose_the_longest_containing_value(tmp_path: Path):
    path = tmp_path / "address-partial-match.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "생년월일", "주소", "상세주소", "식종"))
    worksheet.append(("주소아동", "20150302", "수정로1", "101호", "조식"))
    worksheet.append(
        (
            "주소아동",
            "20150302",
            "수정구 수정로 1",
            "수정아파트 101호",
            "석식",
        )
    )
    workbook.save(path)
    workbook.close()
    profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000094"),
        1,
        "주소 부분일치 명단",
        "roster-split-xlsx",
        {
            ProfileField.NAME: "A",
            ProfileField.BIRTH_DATE: "B",
            ProfileField.ROAD_ADDRESS: "C",
            ProfileField.DETAIL_ADDRESS: "D",
        },
        combined_meals_column="E",
    )

    preview = parse_workbook(path, profile)

    assert preview.can_continue
    assert preview.issues == ()
    observation = preview.observations[0]
    assert observation.profile_values[ProfileField.ROAD_ADDRESS] == (
        "수정구 수정로 1"
    )
    assert observation.profile_values[ProfileField.DETAIL_ADDRESS] == (
        "수정아파트 101호"
    )
    assert observation.meals == (Meal.BREAKFAST, Meal.DINNER)
    resolutions = {
        item.field: item for item in preview.automatic_source_resolutions
    }
    assert set(resolutions) == {"road_address", "detail_address"}
    assert all(
        item.selection_rule == "more_complete_address"
        for item in resolutions.values()
    )
    assert all(item.selected_row == 3 for item in resolutions.values())


def test_address_partial_match_does_not_merge_different_house_numbers(
    tmp_path: Path,
):
    path = tmp_path / "address-number-boundary.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "생년월일", "주소", "식종"))
    worksheet.append(("주소아동", "20150302", "수정로 1", "조식"))
    worksheet.append(("주소아동", "20150302", "수정로 11", "석식"))
    workbook.save(path)
    workbook.close()
    profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000095"),
        1,
        "주소 숫자 경계 명단",
        "roster-split-xlsx",
        {
            ProfileField.NAME: "A",
            ProfileField.BIRTH_DATE: "B",
            ProfileField.ROAD_ADDRESS: "C",
        },
        combined_meals_column="D",
    )

    preview = parse_workbook(path, profile)

    assert preview.can_continue
    assert preview.blocking_issues == ()
    assert preview.observations[0].profile_values[ProfileField.ROAD_ADDRESS] == (
        "수정로 11"
    )
    resolution = preview.automatic_source_resolutions[0]
    assert resolution.field == "road_address"
    assert resolution.selection_rule == "later_excel_row"
    assert resolution.previous_row_values == ((2, "수정로 1"),)
    assert (resolution.selected_row, resolution.selected_value) == (3, "수정로 11")


def test_same_meal_move_uses_later_row_and_preserves_imported_history(
    tmp_path: Path,
):
    path = tmp_path / "move-conflict.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "생년월일", "주소", "식종", "담당", "순서"))
    worksheet.append(
        ("이사아동", "20150302", "이전 주소", "조식", "기사A", "001")
    )
    worksheet.append(
        ("이사아동", "20150302", "현재 주소", "조식", "기사B", "009")
    )
    workbook.save(path)
    workbook.close()
    profile = ImportMappingProfile(
        UUID("81000000-0000-0000-0000-000000000093"),
        1,
        "이사 전후 명단",
        "roster-split-xlsx",
        {
            ProfileField.NAME: "A",
            ProfileField.BIRTH_DATE: "B",
            ProfileField.ROAD_ADDRESS: "C",
        },
        combined_meals_column="D",
        driver_column="E",
        sequence_column="F",
    )

    preview = parse_workbook(path, profile)

    assert preview.can_continue
    assert preview.blocking_issues == ()
    observation = preview.observations[0]
    assert observation.profile_values[ProfileField.ROAD_ADDRESS] == "현재 주소"
    assert len(observation.assignments) == 1
    assignment = observation.assignments[0]
    assert assignment.meal is Meal.BREAKFAST
    assert assignment.driver == "기사B"
    assert assignment.sequence == "009"

    resolutions = {
        item.field: item for item in preview.automatic_source_resolutions
    }
    assert set(resolutions) == {"road_address", "driver", "sequence"}
    assert all(
        item.selection_rule == "later_excel_row"
        for item in resolutions.values()
    )
    assert all(item.selected_row == 3 for item in resolutions.values())
    assert resolutions["road_address"].previous_row_values == ((2, "이전 주소"),)
    assert resolutions["driver"].previous_row_values == ((2, "기사A"),)
    assert resolutions["sequence"].previous_row_values == ((2, "001"),)
    assert resolutions["driver"].field_label == "조식 배송 담당"
    assert resolutions["sequence"].field_label == "조식 배송 순서"


def test_password_protected_workbook_uses_ephemeral_password(tmp_path: Path):
    plain = _write_survey(tmp_path / "plain.xlsx")
    encrypted = _encrypt(plain, tmp_path / "protected.xlsx", "excel-password")

    with pytest.raises(WorkbookImportPasswordRequiredError):
        inspect_workbook(encrypted, header_row=2)
    with pytest.raises(WorkbookImportPasswordError):
        inspect_workbook(encrypted, password="wrong", header_row=2)

    inspection = inspect_workbook(
        encrypted,
        password="excel-password",
        header_row=2,
    )
    assert inspection.columns[6].header == "성명"


def test_profile_is_proposed_only_for_one_exact_header_fingerprint(tmp_path: Path):
    path = _write_survey(tmp_path / "survey.xlsx")
    inspection = inspect_workbook(path, header_row=2)
    profile = _survey_profile()
    stored = StoredImportProfile(profile, inspection.header_fingerprint)

    assert proposed_profile(inspection, (stored,)) == profile
    assert proposed_profile(inspection, (stored, stored)) is None


def test_formula_in_mapped_cell_is_never_imported_as_personal_data(tmp_path: Path):
    path = _write_survey(tmp_path / "formula.xlsx")
    workbook = openpyxl.load_workbook(path)
    workbook["조사표"]["G3"] = '=CONCAT("홍", "길동")'
    workbook.save(path)
    workbook.close()

    preview = parse_workbook(path, _survey_profile(), sheet_name="조사표")

    assert not preview.can_continue
    assert any("수식 셀" in issue.message for issue in preview.issues)


def test_raw_header_preview_preserves_blank_rows_and_limits_large_sheet(tmp_path):
    from src.importing.workbook import preview_workbook_sheet
    import openpyxl
    path = tmp_path / "header-preview.xlsx"
    book = openpyxl.Workbook()
    book.active.cell(220, 1, "성명")
    book.active.cell(221, 1, "합성아동")
    book.save(path)
    book.close()
    initial = preview_workbook_sheet(path)
    assert len(initial.rows) == 200
    assert initial.rows[0].row_number == 1
    assert initial.rows[0].values == ("",)
    later = preview_workbook_sheet(path, start_row=200)
    assert later.rows[20].row_number == 220
    assert later.rows[20].values == ("성명",)
    assert later.rows[21].values == ("합성아동",)
