"""Name/head review and bulk new-person registration use synthetic records."""
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from src.desktop_v2.import_review import ImportReviewDialog
from src.domain.revisions import RevisionActor
from src.domain.roster import Person, PersonProfile, RosterSnapshot, Meal
from src.importing.application import IdentityResolution, apply_reconciliation
from src.importing.contracts import ImportMappingProfile, PersonObservation, ProfileField, SourceReference
from src.importing.reconciliation import (
    ReconciliationDisposition as D, ReviewReason, stage_reconciliation, stage_lunch_reconciliation,
)
from src.importing.revision import ImportPurpose, build_import_revision
from src.importing.workbook import WorkbookImportPreview

DAY = date(2026, 11, 2)


def observation(name="합성아동", phone="010-9999-0000", head="합성보호자", row=2):
    return PersonObservation(uuid4(), SourceReference("synthetic.xlsx", "명단", (row,)), {
        ProfileField.NAME: name, ProfileField.MOBILE_PHONE: phone,
        ProfileField.HOUSEHOLD_HEAD_NAME: head,
    }, (Meal.LUNCH,))


def roster():
    return RosterSnapshot(uuid4(), persons=(
        Person(UUID(int=1), PersonProfile("합성아동", mobile_phone="010-1234-5678", household_head_name="합성보호자")),
        Person(UUID(int=2), PersonProfile("자동연결", mobile_phone="010-5555-6666", household_head_name="다른보호자")),
    ))


@pytest.mark.parametrize("stage", [stage_reconciliation, stage_lunch_reconciliation])
@pytest.mark.parametrize("phone", ["010-9999-0000", None])
def test_name_head_is_reviewed_with_phone_change_and_normalized_spaces(stage, phone):
    current = roster()
    obs = observation(name="합성 아동", head="합성 보호자", phone=phone)
    staged = stage(current, (obs,), effective_on=DAY).staged_people[0]
    assert staged.disposition is D.REVIEW
    assert staged.matched_person_id is None
    assert UUID(int=1) in staged.candidate_person_ids
    assert ReviewReason.SAME_NAME_HOUSEHOLD_HEAD in staged.review_reasons


@pytest.mark.parametrize("stage", [stage_reconciliation, stage_lunch_reconciliation])
def test_exact_phone_stays_automatic_even_when_head_points_to_namesake(stage):
    current = roster()
    current = replace(current, persons=(*current.persons,
        Person(UUID(int=3), PersonProfile("합성아동", mobile_phone="010-2222-3333", household_head_name="다른보호자"))))
    obs = observation(phone="010 1234 5678", head="다른보호자")
    staged = stage(current, (obs,), effective_on=DAY).staged_people[0]
    assert staged.disposition is D.MATCHED
    assert staged.matched_person_id == UUID(int=1)
    assert staged.review_reasons == ()


@pytest.mark.parametrize("stage", [stage_reconciliation, stage_lunch_reconciliation])
def test_blank_head_does_not_create_head_match(stage):
    current = roster()
    current = replace(current, persons=(replace(current.persons[0],
        profile=replace(current.persons[0].profile, household_head_name=None)),))
    staged = stage(current, (observation(head=None),), effective_on=DAY).staged_people[0]
    assert ReviewReason.SAME_NAME_HOUSEHOLD_HEAD not in staged.review_reasons


@pytest.mark.parametrize("stage", [stage_reconciliation, stage_lunch_reconciliation])
def test_head_alone_does_not_match_and_source_duplicate_head_is_reviewed(stage):
    first = observation(name="새아동")
    staged = stage(roster(), (first,), effective_on=DAY).staged_people[0]
    assert ReviewReason.SAME_NAME_HOUSEHOLD_HEAD not in staged.review_reasons
    assert staged.candidate_person_ids == ()
    second = observation(name="새아동", phone="010-7777-8888", row=3)
    plan = stage(None, (first, second), effective_on=DAY)
    assert all(item.disposition is D.REVIEW for item in plan.staged_people)
    assert all(ReviewReason.DUPLICATE_SOURCE_IDENTITY in item.review_reasons for item in plan.staged_people)


@pytest.mark.parametrize("purpose", [ImportPurpose.DATABASE, ImportPurpose.VACATION_LUNCH])
def test_bulk_new_preserves_automatic_matches_and_creates_distinct_people(purpose):
    app = QApplication.instance() or QApplication([])
    current = roster()
    suspected = observation()
    automatic = observation(name="자동연결", phone="01055556666", head="다른보호자", row=3)
    second = observation(phone="01077778888", row=4)
    stage = stage_lunch_reconciliation if purpose is ImportPurpose.VACATION_LUNCH else stage_reconciliation
    plan = stage(current, (suspected, automatic, second), effective_on=DAY)
    dialog = ImportReviewDialog(current, plan, purpose=purpose)
    try:
        assert dialog._table.rowCount() == 2
        assert dialog._table.item(0, 3).text() == "합성보호자"
        assert "세대주명" in dialog._table.item(0, 4).text()
        assert not dialog._buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
        dialog._skip_all.click()
        decisions = dialog.decisions()
        assert automatic.observation_id not in decisions
        assert all(item.resolution is IdentityResolution.CREATE_NEW for item in decisions.values())
        assert len({item.person_id for item in decisions.values()}) == 2
        assert dialog._buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
        applied = apply_reconciliation(current, plan, new_revision_id=uuid4(), decisions=decisions)
        assert len(applied.snapshot.persons) == 4
        assert applied.snapshot.person(UUID(int=1)) == current.person(UUID(int=1))
        assert applied.person_ids_by_observation[automatic.observation_id] == UUID(int=2)
        # Bulk selection remains editable before saving.
        combo = dialog._combos[suspected.observation_id]
        combo.setCurrentIndex(next(i for i in range(combo.count()) if combo.itemData(i) == UUID(int=1)))
        assert dialog.decisions()[suspected.observation_id].resolution is IdentityResolution.LINK_EXISTING
        dialog.reject()
        assert len(current.persons) == 2
    finally:
        dialog.close()


@pytest.mark.parametrize("purpose", [ImportPurpose.DATABASE, ImportPurpose.VACATION_LUNCH])
def test_bulk_new_is_saved_as_separate_identity(purpose):
    app = QApplication.instance() or QApplication([])
    current = roster()
    obs = observation()
    stage = stage_lunch_reconciliation if purpose is ImportPurpose.VACATION_LUNCH else stage_reconciliation
    plan = stage(current, (obs,), effective_on=DAY)
    profile = ImportMappingProfile(uuid4(), 1, "합성 명단", "xlsx", {
        ProfileField.NAME: "A", ProfileField.MOBILE_PHONE: "B", ProfileField.HOUSEHOLD_HEAD_NAME: "C",
    }, default_meals=(Meal.LUNCH,))
    preview = WorkbookImportPreview("synthetic.xlsx", "a" * 64, 1, "명단", "b" * 64, profile, (obs,), (), ())
    dialog = ImportReviewDialog(current, plan, purpose=purpose)
    try:
        dialog._skip_all.click()
        built = build_import_revision(current, preview, plan, decisions=dialog.decisions(),
            actor=RevisionActor("test", "sid"), recorded_at=datetime(2026, 11, 2, tzinfo=UTC),
            revision_id=uuid4(), batch_id=uuid4(), purpose=purpose,
            value_choices={} if purpose is ImportPurpose.DATABASE else None,
            lunch_value_choices={} if purpose is ImportPurpose.VACATION_LUNCH else None)
        after = built.result.snapshot
        assert len(after.persons) == 3
        assert after.person(UUID(int=1)).profile == current.person(UUID(int=1)).profile
        new_id = built.result.person_ids_by_observation[obs.observation_id]
        assert new_id not in {UUID(int=1), UUID(int=2)}
        assert after.person(new_id).profile.household_head_name == "합성보호자"
    finally:
        dialog.close()
