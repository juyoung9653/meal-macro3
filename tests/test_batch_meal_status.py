"""Batch changes are previewed by exact cells and committed atomically."""

from datetime import UTC, date, datetime
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.meal_status_edit import (
    MealStatusChangeController,
    MealStatusChangeDialog,
)
from src.domain.roster import (
    Household,
    HouseholdMembership,
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.status import MealStatus
from src.domain.intake import IntakeChannel
from src.domain.revisions import RevisionActor
from src.domain.meal_status_changes import (
    build_batch_meal_status_revision,
    build_meal_status_revision,
)
from src.domain.projections import roster_audit_changes
from src.domain.revisions import RevisionMetadata, RevisionRecord, RevisionSource
from src.storage.database_session import DatabaseSession

DAY = date(2026, 9, 9)
ACTOR = RevisionActor("담당자", "batch-test-sid")
NOW = datetime(2026, 9, 9, tzinfo=UTC)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def roster():
    persons = tuple(
        Person(uuid4(), PersonProfile(name)) for name in ("첫째", "둘째", "다른 가구")
    )
    households = (Household(uuid4()), Household(uuid4()))
    return RosterSnapshot(
        uuid4(),
        persons=persons,
        households=households,
        memberships=(
            HouseholdMembership(persons[0].person_id, households[0].household_id),
            HouseholdMembership(
                persons[1].person_id,
                households[0].household_id,
                InclusiveDateRange(None, DAY),
            ),
            HouseholdMembership(
                persons[1].person_id,
                households[1].household_id,
                InclusiveDateRange(date(2026, 9, 10), None),
            ),
            HouseholdMembership(persons[2].person_id, households[1].household_id),
        ),
        enrollments=tuple(MealEnrollment(p.person_id, Meal.DINNER) for p in persons),
    )


def dialog(roster, **kwargs):
    return MealStatusChangeDialog(
        roster.persons[0], roster, {}, DAY, Meal.DINNER, MealStatus.PAUSED, **kwargs
    )


def test_household_scope_counts_targets_and_changes_with_start_date(app, roster):
    view = dialog(roster)
    try:
        assert len(view.selected_targets()) == 1
        view._same_household.setChecked(True)
        assert set(view.selected_targets()) == {
            (p.person_id, Meal.DINNER) for p in roster.persons[:2]
        }
        assert "총 2개 적용" in view._summary_text.text()
        view._starts_on.setDate(QDate(2026, 9, 10))
        view._ends_on.setDate(QDate(2026, 9, 10))
        assert view.selected_targets() == ((roster.persons[0].person_id, Meal.DINNER),)
        view._status_buttons[MealStatus.DELIVERY].click()
        view._meal_checks[Meal.BREAKFAST].setChecked(True)
        assert len(view.selected_targets()) == 2
        assert "식종 추가" in view._summary_text.text()
    finally:
        view.close()


def test_exact_cells_do_not_become_person_meal_cross_product_and_show_exclusions(
    app, roster
):
    first, second = roster.persons[:2]
    targets = ((first.person_id, Meal.DINNER), (second.person_id, Meal.BREAKFAST))
    view = dialog(roster, targets=targets)
    try:
        assert view.selected_targets() == ((first.person_id, Meal.DINNER),)
        assert "둘째 · 조식 · 이용 중이 아니어서 제외" in view._summary_text.text()
        view._status_buttons[MealStatus.DELIVERY].click()
        assert set(view.selected_targets()) == set(targets)
        assert not view._meal_checks[Meal.LUNCH].isEnabled()
        view._meal_checks[Meal.DINNER].setChecked(False)
        assert view.selected_targets() == ((second.person_id, Meal.BREAKFAST),)
        view._status_buttons[MealStatus.PAUSED].click()
        assert view.selected_targets() == ()
        assert not view._save_button.isEnabled()
    finally:
        view.close()


def prepare(roster, targets, status=MealStatus.DELIVERY, **kwargs):
    return build_batch_meal_status_revision(
        roster,
        targets,
        status,
        InclusiveDateRange(DAY, DAY),
        actor=ACTOR,
        recorded_at=NOW,
        revision_id=uuid4(),
        event_ids=kwargs.get("event_ids", {target: uuid4() for target in targets}),
    )


def test_batch_deduplicates_enrollment_diffs_and_rejects_invalid_target_atomically(
    roster,
):
    targets = tuple(
        (p.person_id, meal)
        for p in roster.persons[:2]
        for meal in (Meal.DINNER, Meal.BREAKFAST)
    )
    result = prepare(roster, targets)
    assert len(result.events) == 4
    assert len({event.event_id for event in result.events}) == 4
    assert {event.revision_id for event in result.events} == {
        result.snapshot.revision_id
    }
    assert result.record.metadata.parent_revision_id == roster.revision_id
    assert len(result.auto_enrolled_targets) == 2
    assert len(
        {(change.entity_kind, change.entity_id) for change in result.record.changes}
    ) == len(result.record.changes)
    assert Meal.BREAKFAST not in roster.meals_for(roster.persons[0].person_id, on=DAY)
    with pytest.raises(ValueError):
        prepare(roster, targets, MealStatus.PAUSED)
    duplicate = uuid4()
    with pytest.raises(ValueError, match="중복"):
        prepare(roster, targets, event_ids={target: duplicate for target in targets})


def test_batch_change_records_selected_intake_channel_in_revision_metadata(roster):
    targets = ((roster.persons[0].person_id, Meal.DINNER),)

    result = build_batch_meal_status_revision(
        roster,
        targets,
        MealStatus.STOPPED,
        InclusiveDateRange(DAY, None),
        actor=ACTOR,
        recorded_at=NOW,
        revision_id=uuid4(),
        event_ids={targets[0]: uuid4()},
        intake_channel=IntakeChannel.EMAIL,
    )

    assert result.record.metadata.decisions["batch_meal_status_change"]["intake_channel"] == "메일"


@pytest.mark.parametrize(
    ("status", "targets", "period"),
    [
        (
            MealStatus.DELIVERY,
            (Meal.DINNER, Meal.BREAKFAST),
            InclusiveDateRange(DAY, date(2026, 9, 10)),
        ),
        (MealStatus.PAUSED, (Meal.DINNER,), InclusiveDateRange(DAY, DAY)),
        (MealStatus.STOPPED, (Meal.DINNER,), InclusiveDateRange(DAY, None)),
    ],
)
def test_batch_matches_single_change_semantics_and_diffs_once(
    monkeypatch, roster, status, targets, period
):
    first, second = roster.persons[:2]
    if status is MealStatus.DELIVERY:
        roster = replace(
            roster,
            enrollments=(
                *roster.enrollments,
                MealEnrollment(
                    first.person_id,
                    Meal.BREAKFAST,
                    InclusiveDateRange(date(2026, 9, 11), None),
                ),
            ),
        )
    selected = tuple(
        (person.person_id, meal)
        for meal in targets
        for person in (second, first)
    )
    ids = {target: uuid4() for target in selected}
    revision_id = uuid4()
    expected_roster = roster
    expected_events = []
    expected_enrolled = []
    for person in (second, first):
        meals = tuple(meal for pid, meal in selected if pid == person.person_id)
        single = build_meal_status_revision(
            replace(expected_roster, revision_id=roster.revision_id),
            person.person_id,
            meals,
            status,
            period,
            actor=ACTOR,
            recorded_at=NOW,
            revision_id=revision_id,
            event_ids={meal: ids[(person.person_id, meal)] for meal in meals},
        )
        expected_roster = single.snapshot
        expected_events.extend(single.events)
        expected_enrolled.extend(
            (person.person_id, meal) for meal in single.auto_enrolled_meals
        )

    import src.domain.meal_status_changes as changes_module

    original_diff = changes_module.roster_audit_changes
    calls = []

    def counted_diff(before, after):
        calls.append((before, after))
        return original_diff(before, after)

    monkeypatch.setattr(changes_module, "roster_audit_changes", counted_diff)
    result = build_batch_meal_status_revision(
        roster,
        selected,
        status,
        period,
        actor=ACTOR,
        recorded_at=NOW,
        revision_id=revision_id,
        event_ids=ids,
    )

    assert result.snapshot == expected_roster
    assert result.events == tuple(expected_events)
    assert result.auto_enrolled_targets == tuple(expected_enrolled)
    assert len(calls) == 1
    assert calls[0] == (roster, result.snapshot)
    assert result.record.changes == (
        *original_diff(roster, expected_roster),
        *(changes_module.status_event_audit_change(event) for event in expected_events),
    )


def test_batch_invalid_late_target_leaves_roster_unchanged(monkeypatch, roster):
    import src.domain.meal_status_changes as changes_module

    before = roster.enrollments
    valid = (roster.persons[0].person_id, Meal.BREAKFAST)
    invalid = (uuid4(), Meal.BREAKFAST)
    monkeypatch.setattr(
        changes_module,
        "roster_audit_changes",
        lambda *_args: pytest.fail("invalid batch must not build a diff"),
    )

    with pytest.raises(KeyError, match="snapshot에 없는 사람"):
        prepare(roster, (valid, invalid))
    assert roster.enrollments == before


@pytest.mark.parametrize("cancel", [False, True])
def test_controller_persists_each_target_in_one_revision_and_reopens(
    tmp_path, roster, cancel
):
    initial = RevisionRecord(
        RevisionMetadata(
            roster.revision_id,
            None,
            NOW,
            None,
            ACTOR,
            "명단 가져오기",
            RevisionSource.IMPORT,
        ),
        roster_audit_changes(RosterSnapshot(uuid4()), roster),
    )
    targets = (
        (roster.persons[0].person_id, Meal.DINNER),
        (roster.persons[1].person_id, Meal.BREAKFAST),
    )

    class Reviewed:
        def __init__(self, *args, **kwargs):
            assert kwargs["targets"] == targets

        def exec(self):
            return (
                QDialog.DialogCode.Rejected if cancel else QDialog.DialogCode.Accepted
            )

        def selected_meals(self):
            return (Meal.BREAKFAST, Meal.DINNER)

        def selected_targets(self):
            return targets

        def selected_status(self):
            return MealStatus.DELIVERY

        def selected_period(self):
            return InclusiveDateRange(DAY, None)

        def selected_intake_channel(self):
            return IntakeChannel.EMAIL

    messages = SimpleNamespace(
        critical=lambda *args: pytest.fail(str(args)),
        warning=lambda *args: pytest.fail(str(args)),
    )
    path = tmp_path / "batch.sqlite3"
    with DatabaseSession.create(path, "password", "recovery") as session:
        session.append_revision(initial)
        outcome = MealStatusChangeController(
            session,
            dialog_type=Reviewed,
            message_box=messages,
            actor_provider=lambda: ACTOR,
            recorded_at_provider=lambda: NOW,
        ).run_batch(targets, MealStatus.DELIVERY, DAY)
        assert outcome.completed is not cancel
        saved_id = session.database.current_roster().revision_id
        assert (saved_id == roster.revision_id) is cancel
    with DatabaseSession.open(path, "password") as session:
        assert session.database.current_roster().revision_id == saved_id
        if not cancel:
            record = session.database.latest_revision()
            assert record.metadata.parent_revision_id == roster.revision_id
            assert record.metadata.actor == ACTOR
            assert record.metadata.decisions["batch_meal_status_change"][
                "intake_channel"
            ] == IntakeChannel.EMAIL.value
            for pid, meal in targets:
                history = session.database.status_history(pid, meal)
                assert len(history.events) == 1
                assert history.events[0].revision_id == saved_id
            assert not session.database.status_history(
                roster.persons[2].person_id, Meal.DINNER
            ).events
            old = session.database.snapshot(roster.revision_id)
            assert all(ref.entity_kind != "status.event" for ref in old.entities)


def test_application_routes_exact_batch_and_refreshes_after_save(monkeypatch):
    from src.desktop_v2.application import DesktopStartupController

    calls = []
    targets = ((uuid4(), Meal.DINNER), (uuid4(), Meal.BREAKFAST))
    updated = RosterSnapshot(uuid4())
    session = object()
    window = SimpleNamespace(table_model=SimpleNamespace(service_day=DAY))

    class Controller:
        def __init__(self, actual):
            assert actual is session

        def run_batch(self, *args):
            calls.append(args)
            return SimpleNamespace(completed=True, roster=updated)

    controller = DesktopStartupController(settings=SimpleNamespace(), meal_status_controller_type=Controller)
    refreshed = []
    monkeypatch.setattr(
        controller, "_replace_roster_if_changed", lambda *args: refreshed.append(args)
    )
    controller._run_batch_meal_status_change(
        session, window, targets, MealStatus.PAUSED
    )
    assert calls == [(targets, MealStatus.PAUSED, DAY, window)]
    assert refreshed == [(session, window, updated)]
