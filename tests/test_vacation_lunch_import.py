"""Vacation lunch imports update lunch status without rewriting existing profiles."""

from datetime import UTC, date, datetime
from uuid import UUID

import openpyxl
import pytest
from PySide6.QtWidgets import QDialog

from src.domain.roster import (
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.revisions import RevisionActor
from src.domain.roster import InclusiveDateRange
from src.domain.status import MealStatus, StatusEvent
from src.domain.projections import status_events_from_historical_snapshot
from src.domain.status import resolve_status_history
from src.importing.households import HouseholdMatchingRule
from src.importing.application import (
    IdentityResolution,
    ReconciliationDecision,
)
from src.importing.contracts import (
    ImportMappingProfile,
    PersonObservation,
    ProfileField,
    SourceReference,
)
from src.importing.revision import (
    ImportPurpose,
    build_import_revision,
)
from src.importing.workbook import (
    WorkbookImportPreview,
    parse_workbook,
)
from src.importing.reconciliation import stage_reconciliation
from src.importing.profile_history import import_profiles_from_snapshot
from src.storage.database_session import DatabaseSession
from src.desktop_v2.import_controller import GuidedImportStatus, GuidedRosterImportController
from src.desktop_v2.import_wizard import ImportWizardSelection


START = date(2026, 8, 1)
REVISION = UUID(int=101)
NEW_REVISION = UUID(int=102)
KEEP = UUID(int=103)
OMIT = UUID(int=104)
RESTORE = UUID(int=105)


def _preview(*names: str) -> WorkbookImportPreview:
    profile = ImportMappingProfile(
        UUID(int=106), 1, "방학 중식", "xlsx",
        {ProfileField.NAME: "A", ProfileField.BIRTH_DATE: "B"},
        combined_meals_column="C",
    )
    observations = tuple(
        PersonObservation(
            UUID(int=200 + index), SourceReference("synthetic.xlsx", "명단", (index + 2,)),
            {ProfileField.NAME: name, ProfileField.BIRTH_DATE: date(2015, 1, {"유지": 1, "제외": 2, "재개": 3, "신규": 4}[name]), ProfileField.SCHOOL_NAME: "새 학교"},
            (Meal.LUNCH,),
        )
        for index, name in enumerate(names)
    )
    return WorkbookImportPreview(
        "synthetic.xlsx", "a" * 64, 1, "명단", "b" * 64, profile,
        observations, (), (),
    )


def _roster() -> RosterSnapshot:
    persons = tuple(
        Person(person_id, PersonProfile(name, birth_date=date(2015, 1, index + 1), school_name="기존 학교"))
        for index, (person_id, name) in enumerate(((KEEP, "유지"), (OMIT, "제외"), (RESTORE, "재개")))
    )
    return RosterSnapshot(
        REVISION, persons=persons,
        enrollments=(
            *(MealEnrollment(person_id, Meal.LUNCH) for person_id in (KEEP, OMIT, RESTORE)),
            MealEnrollment(OMIT, Meal.BREAKFAST),
        ),
    )


def _stopped(person_id: UUID, starts_on: date = date(2026, 7, 1)) -> StatusEvent:
    return StatusEvent(
        UUID(int=300 + person_id.int), REVISION, person_id, Meal.LUNCH,
        MealStatus.STOPPED, InclusiveDateRange(starts_on, None),
        datetime(2026, 7, 1, tzinfo=UTC),
    )


def _build(preview: WorkbookImportPreview, *, events: tuple[StatusEvent, ...] = ()):
    roster = _roster()
    plan = stage_reconciliation(roster, preview.observations, effective_on=START)
    return build_import_revision(
        roster, preview, plan, decisions={},
        actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=datetime(2026, 8, 2, tzinfo=UTC),
        revision_id=NEW_REVISION, batch_id=UUID(int=107),
        purpose=ImportPurpose.VACATION_LUNCH,
        lunch_status_events=events,
    )


def test_vacation_lunch_retains_present_restores_stopped_and_stops_only_omitted_lunch():
    prepared = _build(_preview("유지", "재개"), events=(_stopped(RESTORE),))
    snapshot = prepared.result.snapshot
    assert snapshot.persons == _roster().persons
    assert Meal.BREAKFAST in snapshot.meals_for(OMIT, on=START)
    assert prepared.record.metadata.decisions["vacation_lunch"] == {
        "present": (str(KEEP), str(RESTORE)),
        "stopped": (str(OMIT),),
    }
    status_changes = [change for change in prepared.record.changes if change.entity_kind == "status.event"]
    assert {(change.after["person_id"], change.after["status"]) for change in status_changes} == {
        (str(KEEP), MealStatus.DELIVERY.value),
        (str(RESTORE), MealStatus.DELIVERY.value),
        (str(OMIT), MealStatus.STOPPED.value),
    }
    assert all(change.after["starts_on"] == START.isoformat() and change.after["ends_on"] is None for change in status_changes)


def test_vacation_lunch_new_person_gets_standalone_household():
    roster = _roster()
    preview = _preview("신규")
    plan = stage_reconciliation(roster, preview.observations, effective_on=START)
    new_id = UUID(int=109)
    prepared = build_import_revision(
        roster, preview, plan,
        decisions={preview.observations[0].observation_id: ReconciliationDecision(
            preview.observations[0].observation_id, IdentityResolution.CREATE_NEW, new_id,
        )},
        actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=datetime(2026, 8, 2, tzinfo=UTC),
        revision_id=NEW_REVISION, batch_id=UUID(int=107),
        purpose=ImportPurpose.VACATION_LUNCH,
    )
    result = prepared.result.snapshot
    members = [item for item in result.memberships if item.person_id == new_id]
    assert len(members) == 1
    assert members[0].period.starts_on == START
    assert members[0].household_id in {item.household_id for item in result.households}
    assert members[0].household_id not in {item.household_id for item in roster.households}
    assert result.meals_for(new_id, on=START) == (Meal.LUNCH,)


def test_vacation_lunch_rejects_future_status_and_skipped_rows():
    preview = _preview("유지")
    with pytest.raises(ValueError, match="예약된 중식"):
        _build(preview, events=(_stopped(OMIT, date(2026, 9, 1)),))


def test_empty_lunch_roster_cannot_stop_everyone():
    base = _preview("유지")
    preview = WorkbookImportPreview(
        base.source_filename, base.sha256, base.byte_size, base.sheet_name,
        base.header_fingerprint, base.profile, (), (), (),
    )
    with pytest.raises(ValueError, match="데이터가 없는 Excel"):
        _build(preview)


def test_vacation_lunch_does_not_stop_people_already_stopped():
    prepared = _build(_preview("유지"), events=(_stopped(RESTORE),))
    assert prepared.record.metadata.decisions["vacation_lunch"]["stopped"] == (str(OMIT),)


def test_lunch_source_without_meal_column_treats_every_valid_row_as_lunch(tmp_path):
    path = tmp_path / "vacation-lunch.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.append(("성명", "생년월일"))
    workbook.active.append(("유지", "20150101"))
    workbook.save(path)
    workbook.close()
    profile = ImportMappingProfile(
        UUID(int=400), 1, "방학 중식", "xlsx",
        {ProfileField.NAME: "A", ProfileField.BIRTH_DATE: "B"},
        default_meals=(Meal.LUNCH,),
    )
    preview = parse_workbook(path, profile)
    assert preview.can_continue
    assert preview.observations[0].meals == (Meal.LUNCH,)
    prepared = _build(preview)
    assert prepared.record.metadata.decisions["vacation_lunch"]["stopped"] == (str(OMIT), str(RESTORE))


def test_lunch_source_adds_new_person_only_to_lunch():
    roster = _roster()
    observation = PersonObservation(
        UUID(int=500), SourceReference("synthetic.xlsx", "명단", (2,)),
        {ProfileField.NAME: "신규", ProfileField.BIRTH_DATE: date(2015, 1, 4)},
        (Meal.BREAKFAST, Meal.DINNER),
    )
    base = _preview("유지")
    preview = WorkbookImportPreview(
        base.source_filename, base.sha256, base.byte_size, base.sheet_name,
        base.header_fingerprint, base.profile, (observation,), (), (),
    )
    plan = stage_reconciliation(roster, preview.observations, effective_on=START)
    person_id = UUID(int=501)
    prepared = build_import_revision(
        roster, preview, plan,
        decisions={observation.observation_id: ReconciliationDecision(
            observation.observation_id, IdentityResolution.CREATE_NEW, person_id,
        )},
        actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=datetime(2026, 8, 2, tzinfo=UTC),
        revision_id=NEW_REVISION, batch_id=UUID(int=502),
        purpose=ImportPurpose.VACATION_LUNCH,
    )
    assert prepared.result.snapshot.meals_for(person_id, on=START) == (Meal.LUNCH,)
    assert prepared.record.metadata.decisions["vacation_lunch"]["stopped"] == (str(KEEP), str(OMIT), str(RESTORE))


def test_consecutive_lunch_imports_persist_stop_and_restart_history(tmp_path):
    path = tmp_path / "operation.sqlite3"
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    actor = RevisionActor("tester", "S-1-5-21-test")
    initial_preview = _preview("유지", "제외", "재개")
    initial_plan = stage_reconciliation(None, initial_preview.observations, effective_on=date(2026, 7, 1))
    initial_ids = (KEEP, OMIT, RESTORE)
    initial = build_import_revision(
        None, initial_preview, initial_plan,
        decisions={observation.observation_id: ReconciliationDecision(
            observation.observation_id, IdentityResolution.CREATE_NEW, person_id,
        ) for observation, person_id in zip(initial_preview.observations, initial_ids)},
        actor=actor, recorded_at=datetime(2026, 7, 1, tzinfo=UTC),
        revision_id=REVISION, batch_id=UUID(int=600),
    )
    with DatabaseSession.create(path, "test-password", "test-recovery", backup_directory=backup_directory) as session:
        session.append_revision(initial.record)
        for day, names, revision_id in (
            (START, ("유지",), UUID(int=601)),
            (date(2026, 9, 1), ("유지", "제외"), UUID(int=602)),
        ):
            roster = session.database.current_roster()
            historical = session.database.snapshot(roster.revision_id)
            events = status_events_from_historical_snapshot(historical, meal=Meal.LUNCH)
            preview = _preview(*names)
            plan = stage_reconciliation(roster, preview.observations, effective_on=day)
            profile = import_profiles_from_snapshot(historical)[0]
            prepared = build_import_revision(
                roster, preview, plan, decisions={}, actor=actor,
                recorded_at=datetime(day.year, day.month, day.day, tzinfo=UTC),
                revision_id=revision_id, batch_id=UUID(int=revision_id.int + 100),
                existing_profile=profile,
                purpose=ImportPurpose.VACATION_LUNCH,
                lunch_status_events=events,
            )
            assert session.append_revision(prepared.record).succeeded
    with DatabaseSession.open(path, "test-password") as reopened:
        roster = reopened.database.current_roster()
        historical = reopened.database.snapshot(roster.revision_id)
        events = status_events_from_historical_snapshot(historical, person_id=OMIT, meal=Meal.LUNCH)
        history = resolve_status_history(OMIT, Meal.LUNCH, events)
        assert history.status_on(date(2026, 7, 31)) is MealStatus.DELIVERY
        assert history.status_on(date(2026, 8, 1)) is MealStatus.PAUSED
        assert history.status_on(date(2026, 8, 31)) is MealStatus.PAUSED
        assert history.status_on(date(2026, 9, 1)) is MealStatus.DELIVERY
        assert len(events) == 2


def test_lunch_controller_skips_review_for_exact_match_and_preserves_cancelled_ambiguity(tmp_path):
    path = tmp_path / "operation.sqlite3"
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    actor = RevisionActor("tester", "S-1-5-21-test")
    initial_preview = _preview("유지")
    initial_plan = stage_reconciliation(None, initial_preview.observations, effective_on=date(2026, 7, 1))
    initial = build_import_revision(
        None, initial_preview, initial_plan,
        decisions={initial_preview.observations[0].observation_id: ReconciliationDecision(
            initial_preview.observations[0].observation_id, IdentityResolution.CREATE_NEW, KEEP,
        )}, actor=actor, recorded_at=datetime(2026, 7, 1, tzinfo=UTC),
        revision_id=REVISION, batch_id=UUID(int=700),
    )

    class Messages:
        summaries = []
        @staticmethod
        def information(_parent, _title, message):
            Messages.summaries.append(message)
        @staticmethod
        def warning(*_args):
            raise AssertionError("unexpected warning")
        @staticmethod
        def critical(*_args):
            raise AssertionError("unexpected failure")

    class NoReview:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("exact lunch identities should not open review")

    def wizard_type(preview):
        class Wizard:
            def __init__(self, **_kwargs):
                pass
            def exec(self):
                return QDialog.DialogCode.Accepted
            def selection(self):
                return ImportWizardSelection(
                    preview, START, initial.stored_profile,
                    HouseholdMatchingRule(UUID(int=701), 1), None,
                    ImportPurpose.VACATION_LUNCH,
                )
        return Wizard

    with DatabaseSession.create(path, "test-password", "test-recovery", backup_directory=backup_directory) as session:
        session.append_revision(initial.record)
        outcome = GuidedRosterImportController(
            session, wizard_type=wizard_type(_preview("유지")),
            review_type=NoReview, actor_provider=lambda: actor,
            message_box=Messages,
        ).run()
        assert outcome.completed
        assert "기존 배송 유지 1명" in Messages.summaries[-1]
        committed_id = session.database.latest_revision().metadata.revision_id

        ambiguous = PersonObservation(
            UUID(int=702), SourceReference("synthetic.xlsx", "명단", (2,)),
            {ProfileField.NAME: "유지", ProfileField.BIRTH_DATE: None}, (Meal.LUNCH,),
        )
        base = _preview("유지")
        preview = WorkbookImportPreview(
            base.source_filename, base.sha256, base.byte_size, base.sheet_name,
            base.header_fingerprint, base.profile, (ambiguous,), (), (),
        )
        class RejectReview:
            def __init__(self, *_args, **_kwargs):
                pass
            def exec(self):
                return QDialog.DialogCode.Rejected
        cancelled = GuidedRosterImportController(
            session, wizard_type=wizard_type(preview),
            review_type=RejectReview, actor_provider=lambda: actor,
            message_box=Messages,
        ).run()
        assert cancelled.status is GuidedImportStatus.CANCELLED
        assert session.database.latest_revision().metadata.revision_id == committed_id
