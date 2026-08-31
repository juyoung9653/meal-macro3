"""Excel inspection and typed-observation Adapter tests."""

from datetime import date
from pathlib import Path
from uuid import UUID

import msoffcrypto
import openpyxl
import pytest
from msoffcrypto.format.ooxml import OOXMLFile

from src.domain import Meal
from src.importing import (
    ImportMappingProfile,
    PhoneNormalization,
    ProfileField,
    StoredImportProfile,
    WorkbookImportPasswordError,
    WorkbookImportPasswordRequiredError,
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


def _encrypt(plain: Path, encrypted: Path, password: str) -> Path:
    with plain.open("rb") as source, encrypted.open("wb") as destination:
        office_file = msoffcrypto.OfficeFile(source)
        assert isinstance(office_file, OOXMLFile)
        office_file.encrypt(password, destination)
    return encrypted


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
