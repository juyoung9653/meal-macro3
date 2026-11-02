"""Regrouping persists source sequences, including legacy already-merged rows."""
from dataclasses import replace
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from src.desktop_v2.driver_households import DriverHouseholdsDialog
from src.domain.delivery_routing import RoutingState
from src.domain.household_grouping import build_grouping_revision
from src.domain.mobile_household_merge import prepare_mobile_household_revision
from src.domain.profile_edits import build_person_profile_revision
from src.domain.projections import roster_audit_changes, status_event_audit_change
from src.domain.revisions import RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import DeliveryAssignment, HouseholdMembership, InclusiveDateRange, Meal
from src.domain.status import MealStatus, StatusEvent
from src.services.driver_households import build_driver_household_rows
from src.storage.database_session import DatabaseSession
from tests.test_driver_households import person, roster, uid
from tests.test_mobile_household_merge import ACTOR, DAY, NOW


def seed(session, *, merged=False):
    people = tuple(person(i + 1, name, phone=phone) for i, (name, phone) in enumerate((
        ("first", "01011111111"), ("second", "01011111111" if merged else "01022222222"),
        ("middle", "01033333333"), ("other driver", "01044444444"), ("stopped", "01055555555"),
    )))
    initial = replace(roster(*people,
        memberships=tuple(HouseholdMembership(p.person_id, uid(50 if merged and i == 1 else 50+i))
                          for i, p in enumerate(people)),
        meals={p.person_id: (Meal.DINNER, Meal.BREAKFAST) if i < 2 else (Meal.DINNER,)
               for i, p in enumerate(people)},
        assignments=tuple(DeliveryAssignment(p.person_id, Meal.DINNER,
            "other" if i == 3 else "driver", value)
            for i, (p, value) in enumerate(zip(people, ("3", "8", "4", "22", "9"))))
            + tuple(DeliveryAssignment(p.person_id, Meal.BREAKFAST, "morning", str(31+i))
                    for i, p in enumerate(people[:2]))), revision_id=uuid4())
    stopped = StatusEvent(uuid4(), initial.revision_id, people[4].person_id, Meal.DINNER,
                          MealStatus.STOPPED, InclusiveDateRange(DAY), NOW)
    session.append_revision(RevisionRecord(
        RevisionMetadata(initial.revision_id, None, NOW, DAY, ACTOR, "seed", RevisionSource.UI),
        (*roster_audit_changes(None, initial), status_event_audit_change(stopped))))
    return session.database.current_roster()


def sequences(current, meal=Meal.DINNER):
    return tuple(current.resolve_assignment(uid(i), meal).sequence for i in range(1, 6))


def grouping(session):
    current = session.database.current_roster()
    return build_grouping_revision(session.database.snapshot(current.revision_id), current,
        ("mobile",), DAY, actor=ACTOR, recorded_at=NOW, revision_id=uuid4())


def test_merge_reorders_source_per_driver_meal_atomically_and_survives_reopen(tmp_path):
    path = tmp_path / "routes.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    with DatabaseSession.create(path, "password", backup_directory=backup_dir) as session:
        before = seed(session)
        edited = replace(before.person(uid(2)).profile, mobile_phone="01011111111")
        record = build_person_profile_revision(before, uid(2), edited, actor=ACTOR,
            recorded_at=NOW, effective_on=DAY, revision_id=uuid4()).record
        backup = session.append_revision(record)
        assert backup.succeeded, backup.error
        after = session.database.current_roster()
        assert sequences(after) == ("1", "1", "2", "22", "9")
        assert sequences(after, Meal.BREAKFAST)[:2] == ("1", "1")
        assert after.enrollments == before.enrollments
        assert [a.driver for a in after.assignments] == [a.driver for a in before.assignments]
        assert sequences(session.database.roster_snapshot(before.revision_id)) == ("3", "8", "4", "22", "9")
        assert len(session.database.revision_summaries()) == 2
        session.database.check_integrity()
    with DatabaseSession.open(path, "password") as session:
        assert sequences(session.database.current_roster()) == ("1", "1", "2", "22", "9")
    with DatabaseSession.restore_and_open_with_user_password(
        backup.record.path, tmp_path / "restored.db", "password",
    ) as restored:
        assert restored.database.current_roster() == after
        restored.database.check_integrity()


def test_reapply_repairs_already_merged_sequences_and_second_application_is_empty(tmp_path):
    with DatabaseSession.create(tmp_path / "legacy.db", "password") as session:
        before = seed(session, merged=True)
        session.append_revision(grouping(session))
        after = session.database.current_roster()
        assert sequences(after) == ("1", "1", "2", "1", "9")
        assert after.memberships == before.memberships
        prepared = prepare_mobile_household_revision(session.database.snapshot(after.revision_id), grouping(session))
        assert all(change.before == change.after for change in prepared.changes)
        session.database.check_integrity()


def test_split_renumbers_remaining_and_separated_households(tmp_path):
    with DatabaseSession.create(tmp_path / "split.db", "password") as session:
        seed(session, merged=True)
        session.append_revision(grouping(session))
        before = session.database.current_roster()
        edited = replace(before.person(uid(2)).profile, mobile_phone="01022222222")
        record = build_person_profile_revision(before, uid(2), edited, actor=ACTOR,
            recorded_at=NOW, effective_on=DAY, revision_id=uuid4()).record
        session.append_revision(record)
        after = session.database.current_roster()
        assert sequences(after) == ("1", "2", "3", "1", "9")
        assert after.current_membership(uid(1)).household_id != after.current_membership(uid(2)).household_id
        session.database.check_integrity()


def test_route_headers_display_source_values_without_renumbering_for_view_filters(tmp_path):
    app = QApplication.instance() or QApplication([])
    with DatabaseSession.create(tmp_path / "display.db", "password") as session:
        current = seed(session, merged=True)
        rows = build_driver_household_rows(current, RoutingState(), DAY)
        dialog = DriverHouseholdsDialog(rows, service_day=DAY, drivers=())
        try:
            dialog.driver_combo.setCurrentIndex(next(i for i in range(dialog.driver_combo.count())
                if tuple(dialog.driver_combo.itemData(i)) == (None, "driver")))
            assert dialog.table.rowCount() > 0
            for index in range(dialog.table.rowCount()):
                row = dialog.table.item(index, 3).data(Qt.ItemDataRole.UserRole)
                expected = (", ".join(row.sequences) if len(row.sequences) <= 1 else
                            "\n".join(f"{p.meals[0].value}: {', '.join(p.sequences)}" for p in (row.meal_rows or (row,))))
                assert dialog.table.verticalHeaderItem(index).text() == expected
            dialog._set_column_search("members", "middle")
            assert dialog.table.rowCount() == 1
            assert dialog.table.verticalHeaderItem(0).text() == "4"
        finally:
            dialog.close()
