from uuid import uuid4

import openpyxl
import pytest

from src.domain.roster import Meal, PersonProfile
from src.importing.contracts import ImportMappingProfile, ProfileField
from src.importing.lunch_values import apply_lunch_values, lunch_value_rows, preferred_school_name
from src.importing.values import apply_import_values, import_value_rows
from src.importing.workbook import parse_workbook
from tests.test_lunch_value_review import PHONE, scenario


@pytest.mark.parametrize("suffix,full", [("초", "초등학교"), ("중", "중학교"), ("고", "고등학교")])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("general", [False, True])
@pytest.mark.parametrize("grade", [None, "3"])
def test_full_school_spelling_is_independent_of_grade(suffix, full, reverse, general, grade):
    short_name, full_name = "성남" + suffix, "성남" + full
    old_name, new_name = (full_name, short_name) if reverse else (short_name, full_name)
    old = PersonProfile("가상 학생", mobile_phone=PHONE, school_name=old_name, grade=grade)
    incoming = {"school_name": new_name}
    if grade:
        incoming["grade"] = "2"
    current, plan, candidate = scenario(old, incoming)
    rows = (import_value_rows if general else lunch_value_rows)(current, candidate, plan)
    assert all(row.automatic for row in rows)
    applied = (apply_import_values if general else apply_lunch_values)(current, candidate, plan, {})
    people = applied.snapshot.persons if general else applied[0]
    assert people[0].profile.school_name == full_name
    assert people[0].profile.grade == grade


def test_different_school_names_are_not_equivalent():
    assert preferred_school_name("성남중", "분당중학교") is None
    assert preferred_school_name("성남중", "성남고등학교") is None
    current, plan, candidate = scenario(
        PersonProfile("가상 학생", mobile_phone=PHONE, school_name="성남중", grade="1"),
        {"school_name": "분당중학교", "grade": "2"},
    )
    assert all(not row.automatic for row in lunch_value_rows(current, candidate, plan))


@pytest.mark.parametrize("identity_by_mobile", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("grades", [("3", "2"), (None, None)])
def test_duplicate_source_rows_keep_full_spelling_and_higher_grade(tmp_path, identity_by_mobile, reverse, grades):
    path = tmp_path / "synthetic.xlsx"
    book = openpyxl.Workbook()
    book.active.append(("성명", "생년월일", "핸드폰", "학교", "학년"))
    rows = [("합성 학생", "20150101", PHONE, "성남중", grades[0]),
            ("합성 학생", "20150101", PHONE, "성남중학교", grades[1])]
    for row in reversed(rows) if reverse else rows:
        book.active.append(row)
    book.save(path)
    book.close()
    profile = ImportMappingProfile(uuid4(), 1, "합성 명단", "xlsx", {
        ProfileField.NAME: "A", ProfileField.BIRTH_DATE: "B", ProfileField.MOBILE_PHONE: "C",
        ProfileField.SCHOOL_NAME: "D", ProfileField.GRADE: "E",
    }, default_meals=(Meal.LUNCH,))
    preview = parse_workbook(path, profile, identity_by_mobile=identity_by_mobile)
    assert preview.can_continue, preview.blocking_issues
    assert len(preview.observations) == 1
    values = preview.observations[0].profile_values
    assert values[ProfileField.SCHOOL_NAME] == "성남중학교"
    assert values.get(ProfileField.GRADE) == grades[0]
