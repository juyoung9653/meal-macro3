from dataclasses import replace
from datetime import date, datetime, UTC
from uuid import UUID, uuid4
from types import SimpleNamespace

import openpyxl
import pytest
from PySide6.QtWidgets import QApplication, QDialog

from src.domain.roster import (
    PersonProfile, Person, RosterSnapshot, Meal, CustomFieldDefinition,
    CustomFieldType, DeliveryAssignment,
)
from src.domain.revisions import RevisionActor
from src.importing.contracts import PersonObservation, SourceReference, ProfileField, ImportMappingProfile, ObservedDeliveryAssignment
from src.importing.application import apply_reconciliation, ReconciliationDecision, IdentityResolution
from src.importing.reconciliation import stage_lunch_reconciliation, stage_reconciliation, ReconciliationDisposition as D
from src.importing.lunch_values import lunch_value_rows, apply_lunch_values
from src.importing.revision import build_import_revision, ImportPurpose
from src.importing.workbook import WorkbookImportPreview, parse_workbook
from src.desktop_v2.lunch_value_review import LunchValueReviewDialog

DAY = date(2026, 8, 1)
PERSON_ID = UUID(int=1)
PHONE = "010-1234-5678"


def scenario(old=None, incoming=None, *, definitions=(), custom=None):
    old = old or PersonProfile("가상 학생", mobile_phone=PHONE, school_name="가상초등학교", grade="6")
    roster = RosterSnapshot(uuid4(), persons=(Person(PERSON_ID, old),), custom_fields=definitions)
    values = {ProfileField.NAME: old.name, ProfileField.MOBILE_PHONE: PHONE}
    values.update({ProfileField(k): v for k, v in (incoming or {}).items()})
    obs = PersonObservation(uuid4(), SourceReference("synthetic.xlsx", "명단", (2,)), values, (Meal.LUNCH,), custom_values=custom or {})
    plan = stage_lunch_reconciliation(roster, (obs,), effective_on=DAY)
    candidate = apply_reconciliation(roster, plan, new_revision_id=uuid4(), custom_fields=definitions)
    return roster, plan, candidate


@pytest.mark.parametrize("school,grade,expected_school,expected_grade", [
    ("가상중학교", "1", "가상중학교", "1"),
    ("가상초등학교", "3", "가상초등학교", "6"),
    ("가상초등학교", "6학년", "가상초등학교", "6"),
    ("가상고등학교", "1", "가상고등학교", "1"),
])
def test_school_stage_before_grade(school, grade, expected_school, expected_grade):
    roster, plan, candidate = scenario(incoming={"school_name": school, "grade": grade})
    rows = lunch_value_rows(roster, candidate, plan)
    assert all(row.automatic for row in rows)
    persons, _ = apply_lunch_values(roster, candidate, plan, {})
    assert (persons[0].profile.school_name, persons[0].profile.grade) == (expected_school, expected_grade)


def test_higher_school_does_not_keep_old_grade_or_type_when_absent():
    roster, plan, candidate = scenario(
        old=PersonProfile("가상 학생", mobile_phone=PHONE, school_type="초등", school_name="가상초등학교", grade="6"),
        incoming={"school_name": "가상중학교"},
    )
    persons, _ = apply_lunch_values(roster, candidate, plan, {})
    assert persons[0].profile.grade is None
    assert persons[0].profile.school_type is None
    assert persons[0].profile.school_name == "가상중학교"


def test_same_stage_different_school_and_unknown_grade_require_review():
    for incoming in ({"school_name": "다른초등학교", "grade": "5"}, {"grade": "졸업"},
                     {"school_name": "가상중학교", "school_type": "초등", "grade": "1"}):
        roster, plan, candidate = scenario(incoming=incoming)
        rows = lunch_value_rows(roster, candidate, plan)
        assert rows and not any(row.automatic for row in rows)
        with pytest.raises(ValueError, match="모두 확인"):
            apply_lunch_values(roster, candidate, plan, None)


def test_grade_only_import_compares_with_existing_school():
    roster, plan, candidate = scenario(
        PersonProfile("가상 학생", mobile_phone=PHONE, school_name="가상초등학교", grade="3"),
        {"grade": "4"},
    )
    persons, _ = apply_lunch_values(roster, candidate, plan, {})
    assert persons[0].profile.grade == "4"
    assert persons[0].profile.school_name == "가상초등학교"


def test_identity_ignores_birth_uses_normalized_mobile_and_requires_missing_phone_review():
    roster, plan, candidate = scenario(incoming={"birth_date": date(2010, 1, 1), "mobile_phone": "01012345678"})
    assert plan.staged_people[0].disposition is D.MATCHED
    obs = plan.staged_people[0].observation
    no_phone = replace(obs, profile_values={ProfileField.NAME: obs.name})
    assert stage_lunch_reconciliation(roster, (no_phone,), effective_on=DAY).staged_people[0].disposition is D.REVIEW
    sibling = replace(obs, profile_values={ProfileField.NAME: "다른 이름", ProfileField.MOBILE_PHONE: PHONE})
    assert stage_lunch_reconciliation(roster, (sibling,), effective_on=DAY).staged_people[0].disposition is D.NEW
    duplicate = replace(obs, observation_id=uuid4())
    assert all(s.disposition is D.REVIEW for s in stage_lunch_reconciliation(roster, (obs, duplicate), effective_on=DAY).staged_people)
    duplicated_roster = replace(roster, persons=(*roster.persons, Person(uuid4(), roster.persons[0].profile)))
    assert stage_lunch_reconciliation(duplicated_roster, (obs,), effective_on=DAY).staged_people[0].disposition is D.REVIEW
    assert stage_reconciliation(roster, (obs,), effective_on=DAY).staged_people[0].disposition is D.MATCHED


def test_profile_and_custom_values_are_editable_and_blanks_default_to_existing():
    definition = CustomFieldDefinition(uuid4(), "가상 추가정보", CustomFieldType.TEXT, 0)
    old = PersonProfile("가상 학생", mobile_phone=PHONE, road_address="기존 주소", note="메모", custom_values={definition.definition_id: "이전"})
    roster, plan, candidate = scenario(old, {"road_address": "가져온 주소", "note": None}, definitions=(definition,), custom={definition.definition_id: "원본"})
    rows = lunch_value_rows(roster, candidate, plan)
    by_field = {row.field: row for row in rows}
    assert by_field["note"].proposed == "메모"
    choices = {row.key: row.proposed for row in rows}
    choices[PERSON_ID, "note"] = None
    choices[PERSON_ID, "road_address"] = "수정한 주소"
    choices[PERSON_ID, "custom:" + str(definition.definition_id)] = "수정"
    persons, _ = apply_lunch_values(roster, candidate, plan, choices)
    assert persons[0].profile.road_address == "수정한 주소"
    assert persons[0].profile.note is None
    assert persons[0].profile.custom_values[definition.definition_id] == "수정"
    assert roster.persons[0].profile.note == "메모"


@pytest.mark.parametrize("source_value", ["새 안내", None])
def test_new_custom_field_on_existing_person_survives_revision(source_value):
    definition = CustomFieldDefinition(uuid4(), "추가 안내", CustomFieldType.TEXT, 0)
    roster, plan, candidate = scenario(definitions=(definition,), custom={definition.definition_id: source_value})
    roster = replace(roster, custom_fields=())
    preview = WorkbookImportPreview(
        "synthetic.xlsx", "a" * 64, 1, "명단", "b" * 64,
        ImportMappingProfile(uuid4(), 1, "중식", "xlsx", {ProfileField.NAME: "A"}, default_meals=(Meal.LUNCH,)),
        (plan.staged_people[0].observation,), (), (), custom_fields=(definition,),
    )
    prepared = build_import_revision(
        roster, preview, plan, decisions={}, actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=datetime(2026, 8, 2, tzinfo=UTC), revision_id=uuid4(), batch_id=uuid4(),
        purpose=ImportPurpose.VACATION_LUNCH,
        lunch_value_choices={(PERSON_ID, "custom:" + str(definition.definition_id)): "수정 안내"} if source_value else {},
    )
    assert prepared.result.snapshot.custom_fields[0].definition_id == definition.definition_id
    if source_value:
        assert prepared.result.snapshot.person(PERSON_ID).profile.custom_values[definition.definition_id] == "수정 안내"
        assert any(c.entity_kind == "roster.person" for c in prepared.record.changes)


def test_value_dialog_keeps_invalid_input_and_accepts_edits(qapp):
    roster, plan, candidate = scenario(old=PersonProfile("가상 학생", mobile_phone=PHONE, birth_date=date(2014, 1, 1)), incoming={"school_name": "가상중학교", "grade": "1", "birth_date": date(2015, 1, 1), "note": "새 메모"})
    dialog = LunchValueReviewDialog(roster, candidate, plan)
    dialog._use_all_values(3)
    index = next(i for i, row in enumerate(dialog.rows) if row.field == "birth_date")
    dialog.table.item(index, 4).setText("날짜 아님")
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.table.item(index, 4).text() == "날짜 아님"
    assert dialog.error.text()
    dialog.table.item(index, 4).setText("2015-02-03")
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.choices()[dialog.rows[index].key] == date(2015, 2, 3)
    assert all(row.key not in dialog.choices() for row in dialog.rows if row.automatic)


def test_source_grouping_in_lunch_does_not_merge_same_birth_different_phones(tmp_path):
    path = tmp_path / "source.xlsx"
    book = openpyxl.Workbook()
    book.active.append(("이름", "생년월일", "핸드폰"))
    book.active.append(("가상 학생", "20150101", "01011111111"))
    book.active.append(("가상 학생", "20150101", "01022222222"))
    book.save(path)
    profile = ImportMappingProfile(uuid4(), 1, "중식", "xlsx", {
        ProfileField.NAME: "A", ProfileField.BIRTH_DATE: "B", ProfileField.MOBILE_PHONE: "C",
    }, default_meals=(Meal.LUNCH,))
    lunch = parse_workbook(path, profile, identity_by_mobile=True)
    general = parse_workbook(path, profile)
    assert lunch.can_continue and len(lunch.observations) == 2
    assert len(general.observations) == 1


def test_lunch_assignment_review_uses_source_even_when_existing_override_present():
    roster, plan, _ = scenario()
    roster = replace(roster, assignments=(DeliveryAssignment(PERSON_ID, None, "공통 기사", "1"), DeliveryAssignment(PERSON_ID, Meal.LUNCH, "중식 기사", "2")))
    obs = replace(plan.staged_people[0].observation, assignments=(ObservedDeliveryAssignment(None, "가져온 기사", "5"),))
    plan = stage_lunch_reconciliation(roster, (obs,), effective_on=DAY)
    candidate = apply_reconciliation(roster, plan, new_revision_id=uuid4())
    rows = lunch_value_rows(roster, candidate, plan)
    choices = {row.key: row.proposed for row in rows}
    _, assignments = apply_lunch_values(roster, candidate, plan, choices)
    updated = replace(roster, assignments=assignments)
    assert updated.resolve_assignment(PERSON_ID, Meal.LUNCH).driver == "가져온 기사"
    assert updated.resolve_assignment(PERSON_ID, Meal.DINNER).driver == "공통 기사"
    choices[PERSON_ID, "driver"] = None
    with pytest.raises(ValueError, match="비울 수 없습니다"):
        apply_lunch_values(roster, candidate, plan, choices)


def test_controller_cancel_then_save_reviewed_values_and_reopen_database(tmp_path):
    from src.desktop_v2.import_controller import GuidedRosterImportController, GuidedImportStatus
    from src.storage.database_session import DatabaseSession
    actor = RevisionActor("tester", "S-1-5-21-test")
    old_observation = PersonObservation(uuid4(), SourceReference("synthetic.xlsx", "명단", (2,)), {
        ProfileField.NAME: "가상 학생", ProfileField.MOBILE_PHONE: PHONE,
        ProfileField.SCHOOL_NAME: "가상초등학교", ProfileField.GRADE: "6",
        ProfileField.NOTE: "기존 메모",
    }, (Meal.LUNCH, Meal.DINNER))
    mapping = ImportMappingProfile(uuid4(), 1, "중식", "xlsx", {ProfileField.NAME: "A"}, default_meals=(Meal.LUNCH,))
    preview = WorkbookImportPreview("synthetic.xlsx", "a" * 64, 1, "명단", "b" * 64, mapping, (old_observation,), (), ())
    initial = build_import_revision(None, preview, stage_reconciliation(None, (old_observation,), effective_on=date(2026, 7, 1)),
        decisions={old_observation.observation_id: ReconciliationDecision(old_observation.observation_id, IdentityResolution.CREATE_NEW, PERSON_ID)},
        actor=actor, recorded_at=datetime(2026, 7, 1, tzinfo=UTC), revision_id=uuid4(), batch_id=uuid4())
    incoming = replace(old_observation, profile_values={
        **old_observation.profile_values, ProfileField.SCHOOL_NAME: "가상중학교", ProfileField.GRADE: "1",
        ProfileField.NOTE: "가져온 메모", ProfileField.BIRTH_DATE: date(2015, 1, 1),
    }, meals=(Meal.LUNCH,))
    preview = replace(preview, observations=(incoming,))
    class Wizard:
        def __init__(self, **kwargs): pass
        def exec(self): return QDialog.DialogCode.Accepted
        def selection(self):
            return SimpleNamespace(preview=preview, effective_on=DAY, purpose=ImportPurpose.VACATION_LUNCH, existing_profile=initial.stored_profile)
    class NoIdentityReview:
        def __init__(self, *args, **kwargs): raise AssertionError("name+mobile must match despite a missing old birth date")
    class ValueReview(LunchValueReviewDialog):
        accept_next = False
        def exec(self):
            assert {row.field for row in self.rows} == {"note"}
            if not self.accept_next:
                return QDialog.DialogCode.Rejected
            self._use_all_values(3)
            self._show_group_details("note")
            self.table.item(0, 4).setText("예외 메모")
            self.accept()
            return self.result()
    class Messages:
        @staticmethod
        def critical(*args): raise AssertionError(args)
        @staticmethod
        def warning(*args): raise AssertionError(args)
        @staticmethod
        def information(*args): pass
    path = tmp_path / "operation.sqlite3"
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(path, "test-password", "test-recovery", backup_directory=backups) as session:
        session.append_revision(initial.record)
        controller = GuidedRosterImportController(session, wizard_type=Wizard, review_type=NoIdentityReview, value_review_type=ValueReview, actor_provider=lambda: actor, message_box=Messages)
        result = controller.run()
        assert result.status is GuidedImportStatus.CANCELLED
        assert session.database.latest_revision().metadata.revision_id == initial.record.metadata.revision_id
        ValueReview.accept_next = True
        result = controller.run()
        assert result.completed
        assert result.backup_result.succeeded
        assert result.roster.person(PERSON_ID).profile.note == "예외 메모"
    with DatabaseSession.open(path, "test-password") as session:
        saved = session.database.current_roster()
        assert saved.person(PERSON_ID).profile.school_name == "가상중학교"
        assert saved.person(PERSON_ID).profile.grade == "1"
        assert saved.person(PERSON_ID).profile.note == "예외 메모"
        assert Meal.DINNER in saved.meals_for(PERSON_ID, on=DAY)


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def grouped_conflict_scenario():
    old = PersonProfile("합성 하나", mobile_phone=PHONE, road_address="기존 주소", note="기존 메모")
    current, original_plan, _ = scenario(old, {"road_address": "새 주소", "note": "새 메모"})
    second_id = uuid4()
    second = Person(second_id, replace(old, name="합성 둘", mobile_phone="010-2222-3333"))
    current = replace(current, persons=(*current.persons, second))
    first_obs = original_plan.staged_people[0].observation
    second_obs = replace(first_obs, observation_id=uuid4(),
        source=SourceReference("synthetic.xlsx", "명단", (3,)),
        profile_values={**first_obs.profile_values, ProfileField.NAME: second.profile.name,
                        ProfileField.MOBILE_PHONE: second.profile.mobile_phone})
    plan = stage_lunch_reconciliation(current, (first_obs, second_obs), effective_on=DAY)
    result = apply_reconciliation(current, plan, new_revision_id=uuid4())
    return current, plan, result


def test_bulk_fields_show_all_conflicts_and_allow_individual_exceptions(qapp):
    current, plan, result = grouped_conflict_scenario()
    dialog = LunchValueReviewDialog(current, result, plan)
    assert len(dialog.rows) == 4
    assert dialog.group_table.rowCount() == 2
    assert dialog.detail_panel.isHidden()
    dialog._use_all_values(3)
    dialog._show_group_details("note")
    assert sum(not dialog.table.isRowHidden(index) for index in range(len(dialog.rows))) == 2
    selected = next(index for index, row in enumerate(dialog.rows) if row.person_id == PERSON_ID and row.field == "note")
    dialog.table.item(selected, 4).setText("예외 메모")
    assert dialog._group_combos["note"].currentData() == "custom"
    assert dialog._group_combos["road_address"].currentData() == "incoming"
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    people, _ = apply_lunch_values(current, result, plan, dialog.choices())
    assert next(person for person in people if person.person_id == PERSON_ID).profile.note == "예외 메모"
    assert next(person for person in people if person.person_id != PERSON_ID).profile.note == "새 메모"
    assert all(person.profile.road_address == "새 주소" for person in people)
    dialog.close()


def test_unselected_group_cannot_be_saved_and_bulk_direction_can_be_changed(qapp):
    current, plan, result = grouped_conflict_scenario()
    dialog = LunchValueReviewDialog(current, result, plan)
    combo = dialog._group_combos["road_address"]
    combo.setCurrentIndex(combo.findData("incoming"))
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "2개" in dialog.error.text()
    dialog._use_all_values(2)
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    people, _ = apply_lunch_values(current, result, plan, dialog.choices())
    assert all(person.profile.road_address == "기존 주소" and person.profile.note == "기존 메모" for person in people)
    dialog.close()


def test_exception_removal_and_bulk_reset_restore_group_choice(qapp):
    current, plan, result = grouped_conflict_scenario()
    dialog = LunchValueReviewDialog(current, result, plan)
    dialog._use_all_values(3)
    dialog._show_group_details("note")
    index = dialog._groups["note"][0]
    dialog._exception_rows.add(index)
    dialog._refresh_exception_rows()
    dialog.table.item(index, 4).setText("개인 수정")
    dialog.table.setCurrentCell(index, 4)
    dialog._remove_exceptions()
    assert not dialog.table.isRowHidden(index)
    assert dialog.table.item(index, 4).text() == dialog.table.item(index, 3).text()
    dialog.table.item(index, 4).setText("다시 수정")
    assert not dialog.table.isRowHidden(index)
    dialog._set_group_values("note", 2)
    assert not dialog.table.isRowHidden(index)
    assert not dialog._exception_rows
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    dialog.close()
