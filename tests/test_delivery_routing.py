"""Dated moves and registered-driver assignment persistence contracts."""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from src.domain.roster import (
    DeliveryAssignment,
    Household,
    HouseholdMembership,
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.revisions import (
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.domain.delivery_routing import (
    ROUTING_EVENT_KIND,
    Driver,
    DriverReplacementTarget,
    RoutingEvent,
    RoutingState,
    build_driver_replacement_revision,
    build_driver_revision,
    build_routing_revision,
    preview_driver_replacement,
    routing_state_from_snapshot,
)
from src.domain.projections import roster_audit_changes
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.storage.database_session import DatabaseSession
from src.storage.encrypted_database import RevisionConflictError

DAY = date(2026, 10, 1)
BEFORE = date(2026, 9, 30)


def base_roster():
    person = Person(
        uuid4(),
        PersonProfile(
            "김예시",
            jurisdiction="정자동",
            road_address="이전 도로",
            detail_address="101호",
        ),
    )
    return RosterSnapshot(
        uuid4(),
        persons=(person,),
        enrollments=tuple(MealEnrollment(person.person_id, meal) for meal in Meal),
        assignments=(DeliveryAssignment(person.person_id, None, "기존 기사", "001"),),
    )


def args():
    return {
        "actor": RevisionActor("tester", "test-sid"),
        "recorded_at": datetime.now(UTC),
        "revision_id": uuid4(),
    }


def loaded(session):
    roster = session.database.current_roster()
    return roster, routing_state_from_snapshot(
        session.database.snapshot(roster.revision_id)
    )


def initialize(session, roster):
    record = RevisionRecord(
        RevisionMetadata(
            revision_id=roster.revision_id,
            parent_revision_id=None,
            recorded_at=datetime.now(UTC),
            effective_on=BEFORE,
            actor=RevisionActor("tester", "test-sid"),
            reason="테스트 명단",
            source=RevisionSource.UI,
        ),
        roster_audit_changes(RosterSnapshot(uuid4()), roster),
    )
    session.append_revision(record)


def test_effective_date_component_precedence_and_unassignment():
    roster = base_roster()
    person_id = roster.persons[0].person_id
    driver = Driver(uuid4(), "새 기사", "010-0000-0001")
    later = RoutingEvent(
        uuid4(),
        person_id,
        date(2026, 11, 1),
        1,
        {Meal.LUNCH: driver.driver_id},
        "11월 주소",
        None,
    )
    move = RoutingEvent(
        uuid4(),
        person_id,
        DAY,
        2,
        {Meal.BREAKFAST: driver.driver_id},
        "새 도로",
        "202호",
    )
    clear = RoutingEvent(uuid4(), person_id, DAY, 3, {Meal.DINNER: None})
    state = RoutingState((driver,), (later, move, clear))
    before = state.resolve(roster, person_id, BEFORE)
    assert (before.road_address, before.detail_address) == ("이전 도로", "101호")
    assert set(before.drivers.values()) == {"기존 기사"}
    active = state.resolve(roster, person_id, DAY)
    assert (active.road_address, active.detail_address) == ("새 도로", "202호")
    assert active.drivers == {
        Meal.BREAKFAST: "새 기사",
        Meal.LUNCH: "기존 기사",
        Meal.DINNER: "",
    }
    assert active.driver_ids[Meal.BREAKFAST] == driver.driver_id
    assert (
        state.resolve(roster, person_id, date(2026, 11, 1)).road_address == "11월 주소"
    )
    assert roster.person(person_id).profile.road_address == "이전 도로"


def test_jurisdiction_follows_effective_dates_retains_and_can_be_cleared():
    roster = base_roster()
    person_id = roster.persons[0].person_id
    move = RoutingEvent(
        uuid4(), person_id, DAY, 1, {}, "새 도로", "202호", "  수내동  "
    )
    future_address_only = RoutingEvent(
        uuid4(), person_id, date(2026, 11, 1), 2, {}, "다음 주소", None
    )
    clear = RoutingEvent(
        uuid4(), person_id, date(2026, 12, 1), 3, {}, jurisdiction="   "
    )
    state = RoutingState(events=(move, future_address_only, clear))

    assert state.resolve(roster, person_id, BEFORE).jurisdiction == "정자동"
    assert state.resolve(roster, person_id, DAY).jurisdiction == "수내동"
    assert (
        state.resolve(roster, person_id, date(2026, 11, 1)).jurisdiction
        == "수내동"
    )
    assert state.resolve(roster, person_id, date(2026, 12, 1)).jurisdiction == ""


def test_legacy_routing_snapshot_without_jurisdiction_retains_profile_value():
    roster = base_roster()
    person_id = roster.persons[0].person_id
    event_id = uuid4()
    snapshot = HistoricalSnapshot(
        roster.revision_id,
        {
            EntityRef(ROUTING_EVENT_KIND, event_id): {
                "person_id": str(person_id),
                "effective_on": DAY.isoformat(),
                "position": 1,
                "driver_ids": {},
                "road_address": "새 주소",
                "detail_address": None,
            }
        },
    )

    view = routing_state_from_snapshot(snapshot).resolve(roster, person_id, DAY)
    assert view.road_address == "새 주소"
    assert view.jurisdiction == "정자동"


def test_registry_legacy_matching_only_for_unique_names():
    roster = base_roster()
    first = Driver(uuid4(), "기존 기사", "010-0000-0001")
    second = Driver(uuid4(), "기존 기사", "010-0000-0002")
    person_id = roster.persons[0].person_id
    assert (
        RoutingState((first,)).resolve(roster, person_id, DAY).driver_ids[Meal.LUNCH]
        == first.driver_id
    )
    assert (
        RoutingState((first, second))
        .resolve(roster, person_id, DAY)
        .driver_ids[Meal.LUNCH]
        is None
    )


def test_routing_builder_rejects_unknown_driver_and_invalid_move():
    roster = base_roster()
    kwargs = dict(
        person_id=roster.persons[0].person_id,
        effective_on=DAY,
        event_id=uuid4(),
        **args(),
    )
    with pytest.raises(ValueError, match="등록되지 않은"):
        build_routing_revision(
            roster, RoutingState(), driver_ids={Meal.LUNCH: uuid4()}, **kwargs
        )
    with pytest.raises(ValueError, match="주소"):
        build_routing_revision(
            roster, RoutingState(), driver_ids={}, road_address="", **kwargs
        )
    with pytest.raises(ValueError, match="변경할"):
        build_routing_revision(roster, RoutingState(), driver_ids={}, **kwargs)


def test_whole_household_move_uses_effective_date_membership_and_one_revision():
    selected = Person(uuid4(), PersonProfile("선택", road_address="이전 주소"))
    current_member = Person(uuid4(), PersonProfile("현재 구성원", road_address="이전 주소"))
    former_member = Person(uuid4(), PersonProfile("과거 구성원", road_address="이전 주소"))
    household = Household(uuid4())
    roster = RosterSnapshot(
        uuid4(),
        persons=(selected, current_member, former_member),
        households=(household,),
        memberships=(
            HouseholdMembership(
                selected.person_id, household.household_id,
                InclusiveDateRange(date(2026, 1, 1), None),
            ),
            HouseholdMembership(
                current_member.person_id, household.household_id,
                InclusiveDateRange(DAY, None),
            ),
            HouseholdMembership(
                former_member.person_id, household.household_id,
                InclusiveDateRange(date(2026, 1, 1), BEFORE),
            ),
        ),
    )
    second_event_id = uuid4()
    record = build_routing_revision(
        roster,
        RoutingState(),
        person_id=selected.person_id,
        effective_on=DAY,
        driver_ids={},
        road_address="새 주소",
        jurisdiction="  야탑동 ",
        whole_household=True,
        event_id=uuid4(),
        event_id_factory=lambda: second_event_id,
        **args(),
    )

    assert record.metadata.parent_revision_id == roster.revision_id
    assert len(record.changes) == 2
    after = tuple(change.after for change in record.changes)
    assert tuple(item["person_id"] for item in after) == (
        str(selected.person_id),
        str(current_member.person_id),
    )
    assert tuple(item["position"] for item in after) == (1, 2)
    assert {item["jurisdiction"] for item in after} == {"야탑동"}


def test_whole_household_move_requires_membership_on_effective_date():
    roster = base_roster()
    with pytest.raises(ValueError, match="가구 소속"):
        build_routing_revision(
            roster,
            RoutingState(),
            person_id=roster.persons[0].person_id,
            effective_on=DAY,
            driver_ids={},
            road_address="새 주소",
            whole_household=True,
            event_id=uuid4(),
            **args(),
        )


def test_registry_move_and_meal_change_survive_reopen_and_keep_old_history(tmp_path):
    roster = base_roster()
    person_id = roster.persons[0].person_id
    path = tmp_path / "routing.sqlite3"
    first = Driver(uuid4(), "첫 기사", "010-0000-0001")
    second = Driver(uuid4(), "둘째 기사", "010-0000-0002")
    with DatabaseSession.create(
        path, "user-password", "institution-recovery"
    ) as session:
        initialize(session, roster)
        for driver in (first, second):
            current, state = loaded(session)
            session.append_revision(
                build_driver_revision(
                    current,
                    state,
                    driver_id=driver.driver_id,
                    name=driver.name,
                    telephone=driver.telephone,
                    **args(),
                )
            )
        current, state = loaded(session)
        prior_revision = current.revision_id
        move = build_routing_revision(
            current,
            state,
            person_id=person_id,
            effective_on=DAY,
            driver_ids={
                Meal.DINNER: first.driver_id,
                Meal.BREAKFAST: second.driver_id,
                Meal.LUNCH: first.driver_id,
            },
            road_address="새 주소",
            detail_address="303호",
            jurisdiction="수내동",
            event_id=uuid4(),
            **args(),
        )
        session.append_revision(move)
        stale = build_routing_revision(
            current,
            state,
            person_id=person_id,
            effective_on=DAY,
            driver_ids={Meal.BREAKFAST: None},
            event_id=uuid4(),
            **args(),
        )
        with pytest.raises(RevisionConflictError):
            session.append_revision(stale)
        current, state = loaded(session)
        # Editing just one meal on the same date supersedes that meal, not the address or other meals.
        session.append_revision(
            build_routing_revision(
                current,
                state,
                person_id=person_id,
                effective_on=DAY,
                driver_ids={Meal.LUNCH: second.driver_id},
                event_id=uuid4(),
                **args(),
            )
        )
    with DatabaseSession.open(path, "user-password") as session:
        current, state = loaded(session)
        assert [(d.name, d.telephone) for d in state.drivers] == sorted(
            [(first.name, first.telephone), (second.name, second.telephone)]
        )
        assert state.resolve(current, person_id, BEFORE).road_address == "이전 도로"
        result = state.resolve(current, person_id, DAY)
        assert (result.road_address, result.detail_address) == ("새 주소", "303호")
        assert result.jurisdiction == "수내동"
        assert result.driver_ids == {
            Meal.DINNER: first.driver_id,
            Meal.BREAKFAST: second.driver_id,
            Meal.LUNCH: second.driver_id,
        }
        prior = routing_state_from_snapshot(session.database.snapshot(prior_revision))
        assert prior.resolve(current, person_id, DAY).road_address == "이전 도로"
        assert len(state.events) == 2


def two_person_roster():
    people = tuple(
        Person(uuid4(), PersonProfile(name, road_address=address, detail_address="1호"))
        for name, address in (("첫 사람", "첫 주소"), ("둘째 사람", "둘째 주소"))
    )
    return RosterSnapshot(
        uuid4(),
        persons=people,
        enrollments=tuple(
            MealEnrollment(person.person_id, meal) for person in people for meal in Meal
        ),
        assignments=tuple(
            DeliveryAssignment(person.person_id, None, "기본 기사", "001")
            for person in people
        ),
    )


def test_batch_driver_replacement_persists_and_preserves_future_history(tmp_path):
    roster = two_person_roster()
    first_person, second_person = (person.person_id for person in roster.persons)
    path = tmp_path / "batch-routing.sqlite3"
    source = Driver(uuid4(), "기존 등록 기사")
    target = Driver(uuid4(), "새 등록 기사")
    other = Driver(uuid4(), "다른 기사")
    with DatabaseSession.create(path, "user-password", "institution-recovery") as session:
        initialize(session, roster)
        for driver in (source, target, other):
            current, state = loaded(session)
            session.append_revision(
                build_driver_revision(
                    current,
                    state,
                    driver_id=driver.driver_id,
                    name=driver.name,
                    telephone=driver.telephone,
                    **args(),
                )
            )
        for person_id, driver_ids, address in (
            (
                first_person,
                {Meal.BREAKFAST: source.driver_id, Meal.LUNCH: source.driver_id},
                "바뀌지 않을 주소",
            ),
            (
                second_person,
                {Meal.BREAKFAST: source.driver_id, Meal.LUNCH: other.driver_id},
                None,
            ),
        ):
            current, state = loaded(session)
            session.append_revision(
                build_routing_revision(
                    current,
                    state,
                    person_id=person_id,
                    effective_on=BEFORE,
                    driver_ids=driver_ids,
                    road_address=address,
                    detail_address="9호" if address else None,
                    event_id=uuid4(),
                    **args(),
                )
            )
        current, state = loaded(session)
        session.append_revision(
            build_routing_revision(
                current,
                state,
                person_id=first_person,
                effective_on=date(2026, 11, 1),
                driver_ids={Meal.LUNCH: source.driver_id},
                event_id=uuid4(),
                **args(),
            )
        )
        current, state = loaded(session)
        old_revision = current.revision_id
        record = build_driver_replacement_revision(
            current,
            state,
            source_driver=source.driver_id,
            target_driver_id=target.driver_id,
            effective_on=DAY,
            meals=(Meal.BREAKFAST, Meal.LUNCH),
            event_id_factory=uuid4,
            **args(),
        )
        assert len(record.changes) == 2
        assert record.metadata.reason == "배송기사 일괄 교체"
        session.append_revision(record)
    with DatabaseSession.open(path, "user-password") as session:
        current, state = loaded(session)
        first_now = state.resolve(current, first_person, DAY)
        second_now = state.resolve(current, second_person, DAY)
        assert first_now.driver_ids[Meal.BREAKFAST] == target.driver_id
        assert first_now.driver_ids[Meal.LUNCH] == target.driver_id
        assert second_now.driver_ids[Meal.BREAKFAST] == target.driver_id
        assert second_now.driver_ids[Meal.LUNCH] == other.driver_id
        assert first_now.road_address == "바뀌지 않을 주소"
        assert state.resolve(current, first_person, date(2026, 11, 1)).driver_ids[
            Meal.LUNCH
        ] == source.driver_id
        old_state = routing_state_from_snapshot(session.database.snapshot(old_revision))
        assert old_state.resolve(current, first_person, DAY).driver_ids[Meal.LUNCH] == source.driver_id


def test_batch_preview_handles_legacy_names_without_registered_name_matching():
    roster = base_roster()
    person_id = roster.persons[0].person_id
    same_name_one = Driver(uuid4(), "기존 기사")
    same_name_two = Driver(uuid4(), "기존 기사")
    target = Driver(uuid4(), "새 기사")
    state = RoutingState((same_name_one, same_name_two, target))
    targets = preview_driver_replacement(
        roster,
        state,
        source_driver="기존 기사",
        target_driver_id=target.driver_id,
        effective_on=DAY,
        meals=(Meal.BREAKFAST, Meal.LUNCH),
    )
    assert targets == (DriverReplacementTarget(person_id, (Meal.BREAKFAST, Meal.LUNCH)),)
    with pytest.raises(ValueError, match="교체할 배송 배정"):
        build_driver_replacement_revision(
            roster,
            state,
            source_driver=same_name_one.driver_id,
            target_driver_id=target.driver_id,
            effective_on=DAY,
            meals=(Meal.BREAKFAST,),
            **args(),
        )
    with pytest.raises(ValueError, match="등록되지 않은"):
        preview_driver_replacement(
            roster,
            state,
            source_driver="기존 기사",
            target_driver_id=uuid4(),
            effective_on=DAY,
            meals=(Meal.BREAKFAST,),
        )


def test_batch_builder_rejects_duplicate_event_ids():
    roster = two_person_roster()
    source = Driver(uuid4(), "기존 기사")
    target = Driver(uuid4(), "새 기사")
    state = RoutingState(
        (source, target),
        tuple(
            RoutingEvent(
                uuid4(), person.person_id, BEFORE, index + 1, {Meal.BREAKFAST: source.driver_id}
            )
            for index, person in enumerate(roster.persons)
        ),
    )
    duplicate = uuid4()
    with pytest.raises(ValueError, match="이미 사용한"):
        build_driver_replacement_revision(
            roster,
            state,
            source_driver=source.driver_id,
            target_driver_id=target.driver_id,
            effective_on=DAY,
            meals=(Meal.BREAKFAST,),
            event_id_factory=lambda: duplicate,
            **args(),
        )
