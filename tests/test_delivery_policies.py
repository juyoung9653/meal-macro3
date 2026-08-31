from datetime import UTC, date, datetime
from uuid import UUID

from src.domain import (
    BREAKFAST_DELIVERY_POLICY_ID,
    BREAKFAST_DELIVERY_POLICY_KIND,
    BreakfastDeliveryPolicyChange,
    BreakfastDeliveryPolicySchedule,
    ClosedFridayMode,
    RevisionActor,
    breakfast_delivery_policy_from_state,
    breakfast_delivery_policy_state,
    build_breakfast_delivery_policy_revision,
)


CURRENT_REVISION_ID = UUID("bf000000-0000-0000-0000-000000000001")
NEXT_REVISION_ID = UUID("bf000000-0000-0000-0000-000000000002")
FIRST_CHANGE_ID = UUID("bf000000-0000-0000-0000-000000000003")
SECOND_CHANGE_ID = UUID("bf000000-0000-0000-0000-000000000004")
RECORDED_AT = datetime(2026, 8, 31, 3, 0, tzinfo=UTC)


def test_default_and_effective_dated_modes_resolve_in_ledger_order():
    first = BreakfastDeliveryPolicyChange(
        FIRST_CHANGE_ID,
        date(2026, 9, 1),
        ClosedFridayMode.SKIP_FRIDAY_WEEKEND,
        RECORDED_AT,
    )
    correction = BreakfastDeliveryPolicyChange(
        SECOND_CHANGE_ID,
        date(2026, 9, 15),
        ClosedFridayMode.THURSDAY_WEEKEND_ONLY,
        RECORDED_AT,
    )
    schedule = BreakfastDeliveryPolicySchedule((first, correction))

    assert schedule.mode_on(date(2026, 8, 31)) is (
        ClosedFridayMode.THURSDAY_WEEKEND_ONLY
    )
    assert schedule.mode_on(date(2026, 9, 14)) is (
        ClosedFridayMode.SKIP_FRIDAY_WEEKEND
    )
    assert schedule.mode_on(date(2026, 9, 15)) is (
        ClosedFridayMode.THURSDAY_WEEKEND_ONLY
    )
    assert str(FIRST_CHANGE_ID) in schedule.rule_on(date(2026, 9, 14)).version


def test_state_round_trip_and_public_manifest_are_canonical():
    schedule = BreakfastDeliveryPolicySchedule(
        (
            BreakfastDeliveryPolicyChange(
                FIRST_CHANGE_ID,
                date(2026, 9, 1),
                ClosedFridayMode.SKIP_FRIDAY_WEEKEND,
                RECORDED_AT,
            ),
        )
    )
    state = breakfast_delivery_policy_state(schedule)

    assert breakfast_delivery_policy_from_state(state) == schedule
    assert schedule.public_manifest()["change_count"] == 1
    assert len(schedule.public_manifest()["content_sha256"]) == 64


def test_builds_one_audited_policy_revision_without_erasing_history():
    prepared = build_breakfast_delivery_policy_revision(
        CURRENT_REVISION_ID,
        None,
        date(2026, 9, 1),
        ClosedFridayMode.SKIP_FRIDAY_WEEKEND,
        actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=RECORDED_AT,
        revision_id=NEXT_REVISION_ID,
        change_id=FIRST_CHANGE_ID,
    )

    assert prepared.schedule.changes == (prepared.change,)
    assert prepared.record.metadata.effective_on == date(2026, 9, 1)
    assert prepared.record.metadata.rule_versions[
        "breakfast_delivery_policy_version"
    ] == 1
    assert prepared.record.metadata.decisions[
        "breakfast_delivery_policy_change"
    ]["closed_friday_mode"] == ClosedFridayMode.SKIP_FRIDAY_WEEKEND.value
    audit = prepared.record.changes[0]
    assert audit.entity_kind == BREAKFAST_DELIVERY_POLICY_KIND
    assert audit.entity_id == BREAKFAST_DELIVERY_POLICY_ID
    assert audit.before is None

    corrected = build_breakfast_delivery_policy_revision(
        NEXT_REVISION_ID,
        audit.after,
        date(2026, 9, 15),
        ClosedFridayMode.THURSDAY_WEEKEND_ONLY,
        actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=RECORDED_AT,
        revision_id=UUID("bf000000-0000-0000-0000-000000000005"),
        change_id=SECOND_CHANGE_ID,
    )
    assert tuple(item.change_id for item in corrected.schedule.changes) == (
        FIRST_CHANGE_ID,
        SECOND_CHANGE_ID,
    )
