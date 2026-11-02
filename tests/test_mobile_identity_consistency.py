"""Phone grouping and exact identity matching across later additions/imports."""
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from src.domain.roster import Person, PersonProfile, RosterSnapshot, Household, HouseholdMembership, Meal, MealEnrollment
from src.domain.revisions import RevisionActor
from src.importing.application import IdentityResolution, ReconciliationDecision
from src.importing.contracts import ImportMappingProfile, PersonObservation, ProfileField, SourceReference
from src.importing.reconciliation import stage_reconciliation, stage_lunch_reconciliation, ReconciliationDisposition
from src.importing.revision import ImportPurpose, build_import_revision
from src.importing.households import HouseholdMatchingRule, stage_household_grouping
from src.importing.workbook import WorkbookImportPreview

DAY = date(2026, 10, 29)
PHONE = "010-1234-5678"


def roster():
    people = (Person(UUID(int=10), PersonProfile("홍길동", mobile_phone=PHONE, birth_date=date(2015, 1, 1))),
              Person(UUID(int=11), PersonProfile("홍길동", mobile_phone="010-9999-0000", birth_date=date(2016, 1, 1))),
              Person(UUID(int=12), PersonProfile("가족", mobile_phone="01012345678")))
    households = tuple(Household(UUID(int=i+1)) for i in range(3))
    return RosterSnapshot(uuid4(), persons=people, households=households,
        memberships=tuple(HouseholdMembership(p.person_id, h.household_id) for p,h in zip(people,households)),
        enrollments=tuple(MealEnrollment(p.person_id, Meal.LUNCH) for p in people))


def observation(name="홍길동", phone="01012345678", birth=None):
    values={ProfileField.NAME:name, ProfileField.MOBILE_PHONE:phone}
    if birth is not None:
        values[ProfileField.BIRTH_DATE]=birth
    return PersonObservation(uuid4(), SourceReference("synthetic.xlsx", "명단", (2,)), values, (Meal.LUNCH,))


@pytest.mark.parametrize("stage", [stage_reconciliation, stage_lunch_reconciliation])
@pytest.mark.parametrize("birth", [None, date(2016,1,1)])
def test_exact_name_phone_wins_over_other_namesake_and_birth(stage, birth):
    current=roster()
    plan=stage(current, (observation(birth=birth),), effective_on=DAY)
    person=plan.staged_people[0]
    assert person.disposition is ReconciliationDisposition.MATCHED
    assert person.matched_person_id == UUID(int=10)
    assert person.candidate_person_ids == ()


@pytest.mark.parametrize("stage", [stage_reconciliation, stage_lunch_reconciliation])
def test_duplicate_exact_database_identity_still_needs_review(stage):
    current=roster()
    duplicate=Person(UUID(int=99), current.persons[0].profile)
    current=replace(current, persons=(*current.persons,duplicate))
    person=stage(current,(observation(),),effective_on=DAY).staged_people[0]
    assert person.disposition is ReconciliationDisposition.REVIEW
    assert set(person.candidate_person_ids) == {UUID(int=10),UUID(int=99)}


@pytest.mark.parametrize("purpose", [ImportPurpose.DATABASE, ImportPurpose.VACATION_LUNCH])
@pytest.mark.parametrize("new_person", [False, True])
def test_later_import_merges_existing_and_new_households(purpose, new_person):
    current=roster()
    obs=observation(name="새 가족" if new_person else "홍길동")
    profile=ImportMappingProfile(uuid4(),1,"합성","xlsx",{ProfileField.NAME:"A",ProfileField.MOBILE_PHONE:"B"},default_meals=(Meal.LUNCH,))
    preview=WorkbookImportPreview("synthetic.xlsx","a"*64,1,"명단","b"*64,profile,(obs,),(),())
    stage=stage_lunch_reconciliation if purpose is ImportPurpose.VACATION_LUNCH else stage_reconciliation
    plan=stage(current,(obs,),effective_on=DAY)
    decisions={obs.observation_id:ReconciliationDecision(obs.observation_id,IdentityResolution.CREATE_NEW,UUID(int=99))} if new_person else None
    grouping=None if purpose is ImportPurpose.VACATION_LUNCH else stage_household_grouping((obs,),HouseholdMatchingRule(uuid4(),1),effective_on=DAY,roster=current)
    built=build_import_revision(current,preview,plan,decisions=decisions,actor=RevisionActor("test","sid"),
        recorded_at=datetime(2026,10,29,tzinfo=UTC),revision_id=uuid4(),batch_id=uuid4(),purpose=purpose,
        household_plan=grouping, household_decisions=None if grouping is None else grouping.default_decisions(),
        value_choices={} if purpose is ImportPurpose.DATABASE else None,
        lunch_value_choices={} if purpose is ImportPurpose.VACATION_LUNCH else None)
    after=built.result.snapshot
    target=after.household_for(UUID(int=10),on=DAY).household_id
    assert after.household_for(UUID(int=12),on=DAY).household_id == target
    assert after.household_for(UUID(int=11),on=DAY).household_id != target
    if new_person:
        assert after.household_for(UUID(int=99),on=DAY).household_id == target
    assert current.household_for(UUID(int=12),on=DAY).household_id != target


@pytest.mark.parametrize("cancel", [False, True])
def test_import_merges_old_households_after_shared_value_review(tmp_path, cancel):
    from PySide6.QtWidgets import QApplication, QDialog
    from src.desktop_v2.import_controller import GuidedRosterImportController
    from src.desktop_v2.import_wizard import ImportWizardSelection
    from src.desktop_v2.household_shared_columns import HouseholdSharedColumnDialog
    from src.storage.database_session import DatabaseSession
    from tests.test_mobile_household_merge import setup, DAY as MERGE_DAY
    from tests.test_desktop_v2_application import _wizard_type, _Messages
    app=QApplication.instance() or QApplication([])
    _Messages.reset()
    with DatabaseSession.create(tmp_path / "conflict.sqlite3", "password") as session:
        before=setup(session,keys=("address",),same=True,conflict=True)
        first=before.persons[0]
        obs=observation(name=first.profile.name, phone=first.profile.mobile_phone)
        profile=ImportMappingProfile(uuid4(),1,"합성","xlsx",{ProfileField.NAME:"A",ProfileField.MOBILE_PHONE:"B"},default_meals=(Meal.LUNCH,))
        preview=WorkbookImportPreview("synthetic.xlsx","a"*64,1,"명단","b"*64,profile,(obs,),(),())
        selection=ImportWizardSelection(preview,MERGE_DAY,None,HouseholdMatchingRule(uuid4(),1),None)
        opened=[]
        class Review(HouseholdSharedColumnDialog):
            def exec(self):
                opened.append(True)
                assert session.database.current_roster() == before
                if cancel:
                    return QDialog.DialogCode.Rejected
                for row in range(self.table.rowCount()):
                    self.table.item(row,1).setText("선택 주소")
                self.accept()
                assert self.result() == QDialog.DialogCode.Accepted, self.error.text()
                return self.result()
        outcome=GuidedRosterImportController(session,wizard_type=_wizard_type(selection),
            shared_review_type=Review,actor_provider=lambda: RevisionActor("test","sid"),message_box=_Messages).run()
        assert opened and not _Messages.critical_calls
        after=session.database.current_roster()
        assert len(session.database.revision_summaries()) == (1 if cancel else 2)
        if cancel:
            assert after == before
        else:
            assert outcome.completed
            assert len({after.household_for(p.person_id,on=MERGE_DAY).household_id for p in after.persons}) == 1
