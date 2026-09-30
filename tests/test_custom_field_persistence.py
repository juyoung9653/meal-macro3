"""Persistence boundaries for user-defined columns and their values."""
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from src.domain import (RosterSnapshot, Person, PersonProfile, RevisionActor,
    RevisionMetadata, RevisionRecord, RevisionSource, CustomFieldType,
    build_person_profile_revision)
from src.domain.projections import roster_audit_changes, roster_from_historical_snapshot
from src.domain.custom_field_edits import (
    build_custom_field_create_revision, build_custom_field_delete_revision)
from src.storage import DatabaseSession, RevisionConflictError


def metadata():
    return dict(actor=RevisionActor('test', 'test-sid'),
                recorded_at=datetime.now(UTC), effective_on=date(2026, 9, 9),
                revision_id=uuid4())


def test_custom_fields_create_edit_delete_reopen_and_stale_delete_is_atomic(tmp_path):
    path = tmp_path / 'custom-fields.sqlite3'
    base = RosterSnapshot(uuid4(), persons=(
        Person(uuid4(), PersonProfile('첫째')), Person(uuid4(), PersonProfile('둘째'))))
    initial = RevisionRecord(RevisionMetadata(
        revision_id=base.revision_id, parent_revision_id=None,
        recorded_at=datetime.now(UTC), effective_on=date(2026, 9, 9),
        actor=RevisionActor('test', 'test-sid'), reason='테스트 명단', source=RevisionSource.UI),
        roster_audit_changes(RosterSnapshot(uuid4()), base))
    field_id = uuid4()
    expected_values = {person.person_id: value for person, value in zip(base.persons, (0, 1.5))}
    with DatabaseSession.create(path, 'user-password', 'institution-recovery') as session:
        session.append_revision(initial)
        created = build_custom_field_create_revision(
            base, '추가 수량', CustomFieldType.NUMBER, definition_id=field_id, **metadata())
        session.append_revision(created.record)
        current = created.snapshot
        for person, value in zip(current.persons, (0, 1.5)):
            edited = build_person_profile_revision(
                current, person.person_id,
                replace(person.profile, custom_values={field_id: value}), **metadata())
            session.append_revision(edited.record)
            current = edited.snapshot
        values_revision = current.revision_id
        assert len(session.database.current_roster().custom_fields) == 1

    with DatabaseSession.open(path, 'user-password') as session:
        current = session.database.current_roster()
        assert {p.person_id: p.profile.custom_values[field_id] for p in current.persons} == expected_values
        stale = build_custom_field_delete_revision(created.snapshot, field_id, **metadata())
        with pytest.raises(RevisionConflictError):
            session.append_revision(stale.record)
        unchanged = session.database.current_roster()
        assert unchanged.revision_id == values_revision
        assert {p.person_id: p.profile.custom_values[field_id] for p in unchanged.persons} == expected_values
        deleted = build_custom_field_delete_revision(current, field_id, **metadata())
        session.append_revision(deleted.record)
        deletion_revision = deleted.snapshot.revision_id

    with DatabaseSession.open(path, 'user-password') as session:
        current = session.database.current_roster()
        assert current.revision_id == deletion_revision
        assert current.custom_fields == ()
        assert all(field_id not in p.profile.custom_values for p in current.persons)
        historical = roster_from_historical_snapshot(session.database.snapshot(values_revision))
        assert historical.custom_fields[0].definition_id == field_id
        assert {p.person_id: p.profile.custom_values[field_id] for p in historical.persons} == expected_values
