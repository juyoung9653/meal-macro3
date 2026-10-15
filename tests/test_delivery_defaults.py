from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from src.domain.delivery_defaults import DELIVERY_DEFAULTS_KIND, build_delivery_defaults_revision, delivery_defaults_from_state, delivery_defaults_state
from src.domain.projections import roster_audit_changes, roster_entity_states, roster_from_historical_snapshot
from src.domain.roster_row_changes import build_roster_row_revision
from src.domain.revisions import HistoricalSnapshot, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import Household, HouseholdDeliverySetting, HouseholdMembership, Meal, Person, PersonProfile, RosterSnapshot, standard_delivery_defaults
from src.domain.school_calendar import SchoolLunchCalendar
from src.services.meal_service_availability import default_service_weekdays, physical_delivery_on, regular_service_available


def test_dinner_defaults_to_four_days_and_explicit_friday_is_preserved():
    defaults = delivery_defaults_from_state(None)
    assert {item.meal: item.weekdays for item in defaults} == {
        Meal.BREAKFAST: (4, 5, 6), Meal.LUNCH: (0, 1, 2, 3, 4), Meal.DINNER: (0, 1, 2, 3),
    }
    person = Person(uuid4(), PersonProfile("기본 석식"))
    roster = RosterSnapshot(uuid4(), persons=(person,))
    assert default_service_weekdays(Meal.DINNER) == (0, 1, 2, 3)
    assert regular_service_available(roster, person, Meal.DINNER, date(2026, 10, 15), SchoolLunchCalendar())
    assert not regular_service_available(roster, person, Meal.DINNER, date(2026, 10, 16), SchoolLunchCalendar())
    explicit = tuple(replace(item, weekdays=(0, 1, 2, 3, 4)) if item.meal is Meal.DINNER else item for item in defaults)
    restored = delivery_defaults_from_state(delivery_defaults_state(explicit))
    assert restored == explicit
    configured = replace(roster, delivery_defaults=restored)
    assert default_service_weekdays(Meal.DINNER, configured) == (0, 1, 2, 3, 4)
    assert regular_service_available(configured, person, Meal.DINNER, date(2026, 10, 16), SchoolLunchCalendar())
from src.storage.database_session import DatabaseSession


def changed_defaults():
    return tuple(replace(item, weekdays=(0, 2, 4), delivery_weekdays=(6,) * 7) if item.meal is Meal.BREAKFAST else item for item in standard_delivery_defaults())


def test_defaults_roundtrip_reopen_history_and_no_person_rewrites(tmp_path):
    actor = RevisionActor("tester", "test-sid")
    first = RosterSnapshot(uuid4(), persons=(Person(uuid4(), PersonProfile("합성 아동")),))
    initial = RevisionRecord(RevisionMetadata(first.revision_id, None, datetime.now(UTC), None, actor, "initial", RevisionSource.UI), roster_audit_changes(None, first))
    path = tmp_path / "defaults.sqlite3"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    with DatabaseSession.create(path, "password", backup_directory=backup_dir) as session:
        session.append_revision(initial)
        record = build_delivery_defaults_revision(first.revision_id, None, changed_defaults(), actor=actor, recorded_at=datetime.now(UTC), revision_id=uuid4())
        backup = session.append_revision(record)
        assert backup.succeeded
        assert len(record.changes) == 1
        assert record.changes[0].entity_kind == DELIVERY_DEFAULTS_KIND
        current = session.database.current_roster()
        assert current.delivery_defaults == changed_defaults()
        assert current.persons == first.persons
        assert roster_from_historical_snapshot(session.database.snapshot(first.revision_id)).delivery_defaults == standard_delivery_defaults()
        added = Person(uuid4(), PersonProfile("신규 합성 아동"))
        prepared_add = build_roster_row_revision(current, actor=actor, recorded_at=datetime.now(UTC), effective_on=date(2026, 10, 12), revision_id=uuid4(), add_person=added, enroll_meals=(Meal.BREAKFAST,))
        assert prepared_add.snapshot.delivery_defaults == changed_defaults()
        session.append_revision(prepared_add.record)
        saved = session.database.current_roster()
        # A stale save cannot append a setting or replace the already committed head.
        from src.storage.encrypted_database import RevisionConflictError
        with pytest.raises(RevisionConflictError):
            session.append_revision(record)
        assert session.database.current_roster() == saved
    with DatabaseSession.open(path, "password") as session:
        current = session.database.current_roster()
        assert current.delivery_defaults == changed_defaults()
        assert current.delivery_settings_for(added.person_id, on=date(2026, 10, 12)) == ()
        assert physical_delivery_on(current, added, Meal.BREAKFAST, date(2026, 10, 12)) == date(2026, 10, 11)
        assert roster_from_historical_snapshot(session.database.snapshot(current.revision_id)).delivery_defaults == changed_defaults()


def test_fields_inherit_independently_and_explicit_person_household_values_survive():
    household = Household(uuid4(), (HouseholdDeliverySetting(Meal.BREAKFAST, (1,), delivery_weekdays=(0,) * 7),))
    inherited = Person(uuid4(), PersonProfile("상속"), ())
    family = Person(uuid4(), PersonProfile("가구"))
    personal = Person(uuid4(), PersonProfile("개인"), (HouseholdDeliverySetting(Meal.BREAKFAST, form="도시락"),))
    roster = RosterSnapshot(uuid4(), (inherited, family, personal), (household,), tuple(HouseholdMembership(p.person_id, household.household_id) for p in (inherited, family, personal)), delivery_defaults=changed_defaults())
    monday, tuesday = date(2026, 10, 12), date(2026, 10, 13)
    for person in (inherited, personal):
        assert regular_service_available(roster, person, Meal.BREAKFAST, monday, SchoolLunchCalendar())
        assert not regular_service_available(roster, person, Meal.BREAKFAST, tuesday, SchoolLunchCalendar())
        assert physical_delivery_on(roster, person, Meal.BREAKFAST, monday) == date(2026, 10, 11)
    assert not regular_service_available(roster, family, Meal.BREAKFAST, monday, SchoolLunchCalendar())
    assert regular_service_available(roster, family, Meal.BREAKFAST, tuesday, SchoolLunchCalendar())
    assert physical_delivery_on(roster, family, Meal.BREAKFAST, tuesday) == monday
    assert roster.delivery_settings_for(inherited.person_id, on=monday) == ()


def test_defaults_validate_roundtrip_and_unchanged_before_any_revision():
    assert delivery_defaults_from_state(delivery_defaults_state(changed_defaults())) == changed_defaults()
    with pytest.raises(ValueError, match="변경된"):
        build_delivery_defaults_revision(uuid4(), None, standard_delivery_defaults(), actor=RevisionActor("test", "sid"), recorded_at=datetime.now(UTC), revision_id=uuid4())
    with pytest.raises(ValueError):
        delivery_defaults_state(standard_delivery_defaults()[:2])
    with pytest.raises(ValueError):
        delivery_defaults_state((HouseholdDeliverySetting(Meal.BREAKFAST, (0,)), *standard_delivery_defaults()[1:]))


def test_public_roster_state_roundtrip_preserves_defaults_without_roster_rewrites():
    roster = RosterSnapshot(uuid4(), persons=(Person(uuid4(), PersonProfile("합성 아동")),), delivery_defaults=changed_defaults())
    restored = roster_from_historical_snapshot(HistoricalSnapshot(roster.revision_id, roster_entity_states(roster)))
    assert restored == roster
    assert roster_audit_changes(replace(roster, delivery_defaults=standard_delivery_defaults()), roster) == ()
