from dataclasses import replace
from datetime import date

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from src.domain.roster import PersonProfile
from src.importing.values import import_value_rows, apply_import_values
from src.desktop_v2.import_value_review import ImportValueReviewDialog
from tests.test_lunch_value_review import scenario, PERSON_ID, PHONE


def selected(rows):
    return {row.key: row.proposed for row in rows if not row.automatic}


@pytest.mark.parametrize("old_school,old_grade,new_school,new_grade,expected_school,expected_grade", [
    ("가상고등학교", "2", "가상중학교", "3", "가상고등학교", "2"),
    ("가상중학교", "3", "가상중학교", "1", "가상중학교", "3"),
    ("가상초등학교", "6", "가상중학교", "1", "가상중학교", "1"),
    ("가상고등학교", "1", "가상고등학교", "3", "가상고등학교", "3"),
    ("가상고", "2", "가상중", "3", "가상고", "2"),
])
def test_ordinary_import_keeps_higher_school_and_grade(old_school, old_grade, new_school, new_grade, expected_school, expected_grade):
    current, plan, result = scenario(PersonProfile("가상 학생", mobile_phone=PHONE, school_name=old_school, grade=old_grade),
                                     {"school_name": new_school, "grade": new_grade})
    rows = import_value_rows(current, result, plan)
    assert all(row.automatic for row in rows)
    saved = apply_import_values(current, result, plan, None).snapshot.person(PERSON_ID).profile
    assert (saved.school_name, saved.grade) == (expected_school, expected_grade)


def test_other_values_require_choices_and_blanks_preserve_current():
    current, plan, result = scenario(PersonProfile("가상 학생", mobile_phone=PHONE, note="기존 메모", road_address="기존 주소"),
                                     {"note": "새 메모", "road_address": None})
    with pytest.raises(ValueError, match="모두 선택"):
        apply_import_values(current, result, plan, None)
    saved = apply_import_values(current, result, plan, {(PERSON_ID, "note"): "기존 메모"}).snapshot
    assert saved.person(PERSON_ID).profile.note == "기존 메모"
    assert saved.person(PERSON_ID).profile.road_address == "기존 주소"
    assert current.person(PERSON_ID).profile.note == "기존 메모"


def test_unrecognized_school_requires_choice():
    current, plan, result = scenario(incoming={"school_name": "다른초등학교", "grade": "5"})
    assert any(not row.automatic for row in import_value_rows(current, result, plan))
    with pytest.raises(ValueError, match="모두 선택"):
        apply_import_values(current, result, plan, None)


def test_dialog_requires_each_choice_and_cell_click_replaces_value():
    app = QApplication.instance() or QApplication([])
    current, plan, result = scenario(PersonProfile("가상 학생", mobile_phone=PHONE, note="기존 메모", road_address="기존 주소"),
                                     {"note": "새 메모", "road_address": "새 주소"})
    dialog = ImportValueReviewDialog(current, result, plan)
    try:
        dialog.accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert "2개" in dialog.error.text()
        for index, row in enumerate(dialog.rows):
            if row.automatic:
                continue
            dialog._choose_cell(index, 3)
            dialog._choose_cell(index, 2)
            assert dialog.table.item(index, 4).text() == row.before
        dialog.accept()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.choices()[PERSON_ID, "note"] == "기존 메모"
    finally:
        dialog.close()


def test_general_controller_cancel_keeps_database_and_confirmed_choice_survives_reopen(tmp_path):
    from datetime import UTC, datetime
    from src.domain.revisions import RevisionActor
    from src.importing.contracts import ProfileField
    from src.desktop_v2.import_controller import GuidedRosterImportController, GuidedImportStatus
    from src.storage.database_session import DatabaseSession
    from tests.test_desktop_v2_application import _selection, _wizard_type, _session, _AcceptedReview, _Messages
    app = QApplication.instance() or QApplication([])
    selection = _selection(tmp_path)
    observation = selection.preview.observations[0]
    original = replace(observation, profile_values={**observation.profile_values,
        ProfileField.SCHOOL_NAME: "가상고등학교", ProfileField.GRADE: "2", ProfileField.NOTE: "기존 메모"})
    selection = replace(selection, preview=replace(selection.preview, observations=(original,)))
    incoming = replace(original, profile_values={**original.profile_values,
        ProfileField.SCHOOL_NAME: "가상중학교", ProfileField.GRADE: "3", ProfileField.NOTE: "새 메모"})
    next_selection = replace(selection, preview=replace(selection.preview, sha256="b"*64, observations=(incoming,)))
    class Review:
        accept_next = False
        use_incoming_note = False
        def __init__(self, current, result, plan, parent):
            self.rows = import_value_rows(current, result, plan)
        def exec(self):
            return QDialog.DialogCode.Accepted if self.accept_next else QDialog.DialogCode.Rejected
        def choices(self):
            return {row.key: row.incoming if self.use_incoming_note and row.field == "note" else row.before
                    for row in self.rows if not row.automatic}
    _Messages.reset()
    with _session(tmp_path) as session:
        initial = GuidedRosterImportController(session, wizard_type=_wizard_type(selection),
            review_type=_AcceptedReview, message_box=_Messages).run()
        assert initial.completed, _Messages.critical_calls
        initial_id = session.database.latest_revision().metadata.revision_id
        backup_paths = tuple(sorted((tmp_path / "backups").iterdir()))
        from src.importing.profile_history import import_profiles_from_snapshot
        from src.importing.households import household_matching_rules_from_snapshot
        snapshot = session.database.snapshot(initial_id)
        next_selection = replace(next_selection,
            existing_profile=import_profiles_from_snapshot(snapshot)[0],
            existing_household_rule=household_matching_rules_from_snapshot(snapshot)[0])
        controller = GuidedRosterImportController(session, wizard_type=_wizard_type(next_selection),
            review_type=_AcceptedReview, import_value_review_type=Review, message_box=_Messages)
        cancelled = controller.run()
        assert cancelled.status is GuidedImportStatus.CANCELLED
        assert session.database.latest_revision().metadata.revision_id == initial_id
        assert tuple(sorted((tmp_path / "backups").iterdir())) == backup_paths
        Review.accept_next = True
        outcome = controller.run()
        assert outcome.completed, _Messages.critical_calls
        assert outcome.backup_result is None
        assert session.database.latest_revision().metadata.revision_id == initial_id
        assert tuple(sorted((tmp_path / "backups").iterdir())) == backup_paths
        Review.use_incoming_note = True
        outcome = controller.run()
        assert outcome.completed, _Messages.critical_calls
        assert outcome.backup_result.succeeded
        revision = session.database.latest_revision()
        assert revision.metadata.parent_revision_id == initial_id
    with DatabaseSession.open(tmp_path / "operation.sqlite3", "user-password") as session:
        profile = session.database.current_roster().persons[0].profile
        assert profile.school_name == "가상고등학교"
        assert profile.grade == "2"
        assert profile.note == "새 메모"


def test_revision_builder_refuses_unreviewed_overwrite():
    from datetime import UTC, datetime
    from uuid import uuid4
    from src.domain.revisions import RevisionActor
    from src.importing.contracts import ImportMappingProfile, ProfileField
    from src.importing.workbook import WorkbookImportPreview
    from src.importing.revision import build_import_revision
    current, plan, result = scenario(PersonProfile("가상 학생", mobile_phone=PHONE, note="기존 메모"), {"note": "새 메모"})
    profile = ImportMappingProfile(uuid4(), 1, "검증", "xlsx", {ProfileField.NAME: "A"}, combined_meals_column="B")
    preview = WorkbookImportPreview("합성.xlsx", "a"*64, 1, "명단", "b"*64, profile,
                                    tuple(item.observation for item in plan.staged_people), (), ())
    with pytest.raises(ValueError, match="모두 선택"):
        build_import_revision(current, preview, plan, decisions={}, actor=RevisionActor("tester", "sid"),
                              recorded_at=datetime.now(UTC), revision_id=uuid4(), batch_id=uuid4())


def test_assignment_conflicts_include_inherited_values_and_keep_blank_sequence():
    from uuid import uuid4
    from src.domain.roster import DeliveryAssignment, Meal
    from src.importing.contracts import ObservedDeliveryAssignment
    from src.importing.reconciliation import stage_lunch_reconciliation
    from src.importing.application import apply_reconciliation
    from tests.test_lunch_value_review import DAY
    current, plan, _ = scenario()
    current = replace(current, assignments=(DeliveryAssignment(PERSON_ID, None, "기존 기사", "2"),))
    obs = replace(plan.staged_people[0].observation,
                  assignments=(ObservedDeliveryAssignment(None, "새 기사", None),))
    plan = stage_lunch_reconciliation(current, (obs,), effective_on=DAY)
    candidate = apply_reconciliation(current, plan, new_revision_id=uuid4())
    rows = import_value_rows(current, candidate, plan)
    with pytest.raises(ValueError, match="모두 선택"):
        apply_import_values(current, candidate, plan, None)
    choices = {row.key: row.before for row in rows if not row.automatic}
    saved = apply_import_values(current, candidate, plan, choices).snapshot
    assert saved.resolve_assignment(PERSON_ID, Meal.LUNCH).driver == "기존 기사"
    assert saved.resolve_assignment(PERSON_ID, Meal.LUNCH).sequence == "2"
    obs = replace(obs, assignments=(ObservedDeliveryAssignment(Meal.LUNCH, "새 기사", None),))
    plan = stage_lunch_reconciliation(current, (obs,), effective_on=DAY)
    candidate = apply_reconciliation(current, plan, new_revision_id=uuid4())
    rows = import_value_rows(current, candidate, plan)
    row = next(row for row in rows if row.field.endswith(":driver"))
    assert row.before == "기존 기사" and not row.automatic


def test_keeping_existing_meal_restores_its_full_enrollment_history():
    from uuid import uuid4
    from src.domain.roster import Meal, MealEnrollment, InclusiveDateRange
    from src.importing.reconciliation import stage_lunch_reconciliation
    from src.importing.application import apply_reconciliation
    from tests.test_lunch_value_review import DAY
    current, plan, _ = scenario()
    current = replace(current, enrollments=(MealEnrollment(PERSON_ID, Meal.DINNER, InclusiveDateRange(date(2026,1,1))),))
    obs = plan.staged_people[0].observation
    plan = stage_lunch_reconciliation(current, (obs,), effective_on=DAY)
    candidate = apply_reconciliation(current, plan, new_revision_id=uuid4())
    rows = import_value_rows(current, candidate, plan)
    choices = {row.key: row.before for row in rows if not row.automatic}
    assert {row.field for row in rows if not row.automatic} == {"meal:석식", "meal:중식"}
    saved = apply_import_values(current, candidate, plan, choices).snapshot
    assert saved.enrollments == current.enrollments


def test_choosing_existing_custom_text_keeps_exact_whitespace():
    from uuid import uuid4
    from src.domain.roster import CustomFieldDefinition, CustomFieldType
    app = QApplication.instance() or QApplication([])
    definition = CustomFieldDefinition(uuid4(), "안내", CustomFieldType.TEXT, 0)
    original = "  원문 첫 줄\n둘째 줄  "
    current, plan, result = scenario(PersonProfile("가상 학생", mobile_phone=PHONE,
        custom_values={definition.definition_id: original}), definitions=(definition,), custom={definition.definition_id: "새 안내"})
    dialog = ImportValueReviewDialog(current, result, plan)
    try:
        index = next(i for i, row in enumerate(dialog.rows) if row.field.startswith("custom:"))
        dialog._choose_cell(index, 2)
        dialog.accept()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.choices()[PERSON_ID, "custom:" + str(definition.definition_id)] == original
    finally:
        dialog.close()
