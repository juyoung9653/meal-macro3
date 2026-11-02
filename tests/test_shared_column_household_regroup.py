"""Turning on shared values also reconciles legacy separated route rows."""
from dataclasses import replace
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.household_shared_columns import HouseholdSharedColumnController, HouseholdSharedColumnDialog
from src.domain.delivery_routing import RoutingState
from src.domain.household_grouping import build_grouping_revision
from src.domain.household_shared import HOUSEHOLD_SHARED_ID, HOUSEHOLD_SHARED_KIND
from src.domain.projections import roster_audit_changes
from src.domain.revisions import RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import DeliveryAssignment, Meal, MealEnrollment
from src.services.driver_households import build_driver_household_rows
from src.storage.database_session import DatabaseSession
from tests.test_mobile_household_merge import setup, household, ACTOR, DAY, NOW, TARGET, SOURCE


def add_delivery_facts(session, roster):
    current = replace(roster, revision_id=uuid4(),
        enrollments=tuple(MealEnrollment(p.person_id, Meal.DINNER) for p in roster.persons),
        assignments=tuple(DeliveryAssignment(p.person_id, Meal.DINNER, driver='합성 기사', sequence=str(i + 1))
                          for i, p in enumerate(roster.persons)))
    session.append_revision(RevisionRecord(
        RevisionMetadata(current.revision_id, roster.revision_id, NOW, DAY, ACTOR, '배송 정보', RevisionSource.UI),
        roster_audit_changes(roster, current)))
    return session.database.current_roster()


@pytest.mark.parametrize('accept', [False, True])
def test_shared_note_reviews_and_rejoins_separated_delivery_rows_atomically(tmp_path, monkeypatch, accept):
    import src.desktop_v2.household_shared_columns as shared_ui
    app = QApplication.instance() or QApplication([])
    reviews = []

    class Review(HouseholdSharedColumnDialog):
        def exec(self):
            reviews.append(self.windowTitle())
            assert len(self._rows) == 1
            assert len(self._rows[0].member_names) == 2
            return QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected

        def selected_values(self):
            return {TARGET: ('같은 비고',)}

    monkeypatch.setattr(shared_ui, 'HouseholdSharedColumnDialog', Review)
    with DatabaseSession.create(tmp_path / 'notes.db', 'password') as session:
        before = add_delivery_facts(session, setup(session, keys=(), same=True))
        assert len(build_driver_household_rows(before, RoutingState(), DAY)) == 2
        records_before = len(session.database.revision_summaries())
        outcome = HouseholdSharedColumnController(session, actor_provider=lambda: ACTOR,
            recorded_at_provider=lambda: NOW).run('note', '비고', DAY)
        assert len(reviews) == 1 and '비고' in reviews[0]
        if not accept:
            assert outcome.roster is None
            assert session.database.current_roster() == before
            assert len(session.database.revision_summaries()) == records_before
            return
        after = outcome.roster
        assert after == session.database.current_roster()
        assert household(after, 10) == household(after, 11) == TARGET
        assert {p.profile.note for p in after.persons} == {'같은 비고'}
        rows = build_driver_household_rows(after, RoutingState(), DAY)
        assert len(rows) == 1 and len(rows[0].member_ids) == 2
        assert rows[0].sequences == ('1',)
        assert all(after.resolve_assignment(p.person_id, Meal.DINNER).sequence == '1' for p in after.persons)
        assert all(after.resolve_assignment(p.person_id, Meal.DINNER).driver == '합성 기사' for p in after.persons)
        assert after.enrollments == before.enrollments
        assert len(session.database.revision_summaries()) == records_before + 1
        assert household(session.database.roster_snapshot(before.revision_id), 11) == SOURCE
        session.database.check_integrity()


def test_shared_note_does_not_override_selected_grouping_criteria(tmp_path):
    app = QApplication.instance() or QApplication([])
    with DatabaseSession.create(tmp_path / 'criteria.db', 'password') as session:
        before = setup(session, keys=(), same=True)
        snapshot = session.database.snapshot(before.revision_id)
        session.append_revision(build_grouping_revision(snapshot, before, ('mobile', 'note'), DAY,
            actor=ACTOR, recorded_at=NOW, revision_id=uuid4()))
        outcome = HouseholdSharedColumnController(session, actor_provider=lambda: ACTOR,
            recorded_at_provider=lambda: NOW).run('note', '비고', DAY)
        assert outcome.roster is not None
        assert household(outcome.roster, 10) != household(outcome.roster, 11)
        assert {p.profile.note for p in outcome.roster.persons} == {'첫째', '둘째'}
        state = session.database.snapshot(outcome.roster.revision_id).entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
        assert 'note' in state['columns']
