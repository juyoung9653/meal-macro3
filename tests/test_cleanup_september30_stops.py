from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from scripts.cleanup_september30_stops import cleanup_in_place, configured_backup
from src.services.september30_cleanup import find_targets
from src.services.revision_restore import restore_revision
from src.services.person_history import build_history_tables
from src.storage.database_credentials import credential_path_for
from src.domain.documents import DocumentKind, DocumentRun
from src.domain.projections import roster_audit_changes, status_event_audit_change, STATUS_EVENT_KIND, status_event_from_state
from src.domain.revisions import RevisionRecord, RevisionMetadata, RevisionActor, RevisionSource
from src.domain.roster import Person, PersonProfile, RosterSnapshot, Meal, MealEnrollment, InclusiveDateRange
from src.domain.status import MealStatus, StatusEvent
from src.services.revision_branches import create_branch, checkout_branch, MAIN_BRANCH_ID
from src.storage.database_session import DatabaseSession

ACTOR = RevisionActor("test", "test")


def _metadata(revision, parent, reason="합성"):
    return RevisionMetadata(revision, parent, datetime(2026, 10, 5, tzinfo=UTC), date(2026, 9, 30), ACTOR, reason, RevisionSource.UI)


def _event(session, person_id, meal, status, start):
    revision = uuid4()
    event = StatusEvent(uuid4(), revision, person_id, meal, status, InclusiveDateRange(start),
                        datetime(2026, 10, 5, tzinfo=UTC), "합성 상태")
    record = RevisionRecord(_metadata(revision, session.database.latest_revision_id(), "식종 변경"),
                            (status_event_audit_change(event),))
    session.database.append_revision(record, replay=True)
    return revision


@pytest.mark.parametrize("with_branch", [False, True])
def test_cleanup_scans_history_and_preserves_other_meals_and_source(tmp_path, with_branch):
    path = tmp_path / "operations.sqlite3"
    people = tuple(Person(uuid4(), PersonProfile(name), lunch_period=InclusiveDateRange(date(2026, 7, 1), date(2026, 10, 30)))
                   for name in ("중식대상", "과거중지후재개", "다른날중지"))
    roster = RosterSnapshot(uuid4(), people, enrollments=(
        MealEnrollment(people[0].person_id, Meal.LUNCH), MealEnrollment(people[0].person_id, Meal.DINNER),
        MealEnrollment(people[1].person_id, Meal.DINNER), MealEnrollment(people[2].person_id, Meal.LUNCH)))
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    with DatabaseSession.create(path, "password", "recovery", backup_directory=backup_dir) as session:
        session.database.append_revision(RevisionRecord(_metadata(roster.revision_id, None), roster_audit_changes(None, roster)), replay=True)
        target_stop = _event(session, people[0].person_id, Meal.LUNCH, MealStatus.STOPPED, date(2026, 9, 30))
        _event(session, people[1].person_id, Meal.DINNER, MealStatus.STOPPED, date(2025, 9, 30))
        _event(session, people[1].person_id, Meal.DINNER, MealStatus.DELIVERY, date(2026, 1, 1))
        _event(session, people[2].person_id, Meal.LUNCH, MealStatus.STOPPED, date(2026, 10, 1))
        run = DocumentRun(uuid4(), DocumentKind.DELIVERY_REPORT, target_stop,
            session.database.revision_display_code(target_stop), date(2026, 9, 1), date(2026, 9, 30),
            datetime(2026, 10, 6, tzinfo=UTC), ACTOR, {"version": 1}, {"version": 1}, "synthetic.xlsx", "a" * 64, 100)
        session.database.append_document_run(run)
        if with_branch:
            create_branch(session, target_stop, session.database.latest_revision_id(), ACTOR, name="별도 작업본")
            checkout_branch(session, MAIN_BRANCH_ID, session.database.latest_revision_id(), ACTOR)
        source_head = session.database.latest_revision_id()
        source_entities = session.database.snapshot(source_head).entities
        records = tuple(session.database.revision(item.revision_id) for item in reversed(session.database.revision_summaries()))
        targets = find_targets(records)
        assert targets == frozenset({(str(people[0].person_id), Meal.LUNCH.value), (str(people[1].person_id), Meal.DINNER.value)})
        credentials_before = credential_path_for(path).read_bytes()
        old_records = tuple(records)
        record, backup = cleanup_in_place(session, actor=ACTOR)
        assert backup.succeeded
        assert session.path == path
        assert session.backup_directory == backup_dir
        assert credential_path_for(path).read_bytes() == credentials_before
        cleaned_head = record.metadata.revision_id
        assert len(session.database.revision_summaries()) == len(records) + 1
        assert tuple(session.database.revision(r.metadata.revision_id) for r in records) == old_records
        assert session.database.snapshot(source_head).entities == source_entities
        assert cleanup_in_place(session, actor=ACTOR) == (None, None)
    with DatabaseSession.open(path, "password", backup_directory=backup_dir) as cleaned:
        cleaned.database.check_integrity()
        snapshot = cleaned.database.current_roster()
        assert all((str(item.person_id), item.meal.value) not in targets for item in snapshot.enrollments)
        assert any(item.person_id == people[0].person_id and item.meal is Meal.DINNER for item in snapshot.enrollments)
        assert snapshot.person(people[0].person_id).lunch_period is None
        assert all((str(event.person_id), event.meal.value) not in targets for event in cleaned.database.status_events())
        tables = build_history_tables(snapshot, cleaned.database.snapshot(cleaned_head), cleaned.database.branch_revision_chain(cleaned_head))
        assert all((row[3], row[5]) not in targets for row in tables["식종 변경"][1])
        assert cleaned.database.document_run(run.run_id) == run
        assert len(cleaned.database.branches()) == (2 if with_branch else 1)
        result = restore_revision(cleaned, source_head, cleaned_head, ACTOR)
        assert result.backup.succeeded
        assert cleaned.database.snapshot(result.revision_id).entities == source_entities
        restored = build_history_tables(cleaned.database.current_roster(), cleaned.database.snapshot(result.revision_id), cleaned.database.branch_revision_chain(result.revision_id))
        assert any((row[3], row[5]) in targets for row in restored["식종 변경"][1])
        cleaned.database.check_integrity()


def test_cleanup_runtime_stays_open_on_error(monkeypatch, tmp_path):
    import runpy
    import sys
    from pathlib import Path
    answers = iter(["2026-09-30", str(tmp_path / "nonexistent.sqlite3"), ""])
    prompts = []
    def answer(prompt):
        prompts.append(prompt)
        return next(answers)
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "password")
    monkeypatch.setattr(sys, "argv", ["cleanup_september30_stops.py"])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/cleanup_september30_stops.py"), run_name="__main__")
    assert error.value.code == 1
    assert any("Enter" in prompt for prompt in prompts)


def test_existing_backup_settings_are_only_read(tmp_path):
    from PySide6.QtCore import QSettings
    base = tmp_path / "portable"
    (base / "data").mkdir(parents=True)
    settings_path = base / "data" / "settings.ini"
    path = base / "operations.sqlite3"
    settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    settings.setValue("storage/database_path", "operations.sqlite3")
    settings.setValue("storage/backup_directory", "backup")
    settings.sync()
    before = settings_path.read_bytes()
    assert configured_backup(path, bases=(base,)) == base / "backup"
    assert configured_backup(base / "another.sqlite3", bases=(base,)) is None
    assert settings_path.read_bytes() == before


def test_cleanup_rejects_modified_plan_atomically(tmp_path):
    from src.services.september30_cleanup import build_cleanup
    from src.domain.revisions import AuditChange, ChangeOperation
    from src.domain.projections import PERSON_KIND
    person = Person(uuid4(), PersonProfile("합성"))
    roster = RosterSnapshot(uuid4(), (person,), enrollments=(MealEnrollment(person.person_id, Meal.DINNER),))
    with DatabaseSession.create(tmp_path / "test.sqlite3", "password", "recovery") as session:
        session.database.append_revision(RevisionRecord(_metadata(roster.revision_id, None), roster_audit_changes(None, roster)))
        head = _event(session, person.person_id, Meal.DINNER, MealStatus.STOPPED, date(2026, 9, 30))
        snapshot = session.database.snapshot(head)
        plan = build_cleanup(snapshot, session.database.branch_revision_chain(head), actor=ACTOR,
            recorded_at=datetime.now(UTC), revision_id=uuid4())
        before = snapshot.entity(PERSON_KIND, person.person_id)
        bad = replace(plan, changes=(*plan.changes, AuditChange(PERSON_KIND, person.person_id,
            ChangeOperation.UPDATE, before, {**before, "name": "원치않는 변경"})))
        with pytest.raises(ValueError, match="정리 대상"):
            session.append_revision(bad)
        assert session.database.latest_revision_id() == head
        assert session.database.snapshot(head).entities == snapshot.entities
        session.database.check_integrity()
