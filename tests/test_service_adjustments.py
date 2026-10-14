"""Vendor closure, replacement delivery, and billing-separation contracts."""

from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from src.domain.roster import (
    InclusiveDateRange,
    Meal,
)
from src.domain.public_holidays import PublicHolidayCandidate
from src.domain.service_adjustments import (
    ReplacementDeliveryOverride,
    ServiceAdjustmentChange,
    ServiceAdjustmentOrigin,
    ServiceAdjustmentSchedule,
    ServiceTreatment,
    build_public_holiday_review_revision,
    build_service_adjustment_revision,
    previous_common_operating_day,
    service_adjustment_schedule_from_state,
    service_adjustment_schedule_state,
)
from src.domain.revisions import RevisionActor


BASE_REVISION_ID = UUID("81000000-0000-0000-0000-000000000001")
REVISION_ID = UUID("81000000-0000-0000-0000-000000000002")
REPLACEMENT_CHANGE_ID = UUID("81000000-0000-0000-0000-000000000003")
CLOSURE_CHANGE_ID = UUID("81000000-0000-0000-0000-000000000004")
RECORDED_AT = datetime(2026, 8, 26, 3, 0, tzinfo=UTC)
CHUSEOK = PublicHolidayCandidate(
    date(2026, 9, 25),
    "추석",
    "python-holidays:KR",
    "0.102",
)
CHUSEOK_EVE = PublicHolidayCandidate(
    date(2026, 9, 24),
    "추석 전날",
    "python-holidays:KR",
    "0.102",
)
BATCH_CHANGE_ID = UUID("81000000-0000-0000-0000-000000000006")


def _replacement_change() -> ServiceAdjustmentChange:
    return ServiceAdjustmentChange(
        REPLACEMENT_CHANGE_ID,
        InclusiveDateRange(date(2026, 9, 10), date(2026, 9, 12)),
        tuple(Meal),
        ServiceTreatment.REPLACEMENT,
        date(2026, 9, 9),
        (
            ReplacementDeliveryOverride(
                Meal.LUNCH,
                date(2026, 9, 11),
                date(2026, 9, 8),
            ),
        ),
        "업체 연속 휴무",
        RECORDED_AT,
    )


def test_replacement_keeps_original_service_billable_and_customizes_delivery():
    schedule = ServiceAdjustmentSchedule((_replacement_change(),))

    shared = schedule.provision_for(Meal.BREAKFAST, date(2026, 9, 12))
    customized = schedule.provision_for(Meal.LUNCH, date(2026, 9, 11))
    normal = schedule.provision_for(Meal.LUNCH, date(2026, 9, 13))

    assert shared.billable
    assert shared.service_on == date(2026, 9, 12)
    assert shared.delivery_on == date(2026, 9, 9)
    assert customized.billable
    assert customized.service_on == date(2026, 9, 11)
    assert customized.delivery_on == date(2026, 9, 8)
    assert normal.treatment is ServiceTreatment.NORMAL
    assert normal.delivery_on == normal.service_on


def test_later_not_provided_change_excludes_only_matching_meal_and_date():
    not_provided = ServiceAdjustmentChange(
        CLOSURE_CHANGE_ID,
        InclusiveDateRange(date(2026, 9, 11), date(2026, 9, 11)),
        (Meal.LUNCH,),
        ServiceTreatment.NOT_PROVIDED,
        None,
        (),
        "중식만 미제공",
        RECORDED_AT,
    )
    schedule = ServiceAdjustmentSchedule(
        (_replacement_change(), not_provided)
    )

    lunch = schedule.provision_for(Meal.LUNCH, date(2026, 9, 11))
    breakfast = schedule.provision_for(
        Meal.BREAKFAST,
        date(2026, 9, 11),
    )

    assert not lunch.billable
    assert lunch.delivery_on is None
    assert lunch.change_id == CLOSURE_CHANGE_ID
    assert breakfast.billable
    assert breakfast.delivery_on == date(2026, 9, 9)


def test_schedule_state_round_trip_preserves_shared_and_custom_delivery_dates():
    schedule = ServiceAdjustmentSchedule((_replacement_change(),))

    restored = service_adjustment_schedule_from_state(
        service_adjustment_schedule_state(schedule)
    )

    assert restored == schedule


def test_policy_v1_state_without_origin_fields_remains_manual_compatible():
    legacy_state = {
        "policy_version": 1,
        "changes": (
            {
                "change_id": str(CLOSURE_CHANGE_ID),
                "period_start": "2026-09-11",
                "period_end": "2026-09-11",
                "meals": ("중식",),
                "treatment": "not_provided",
                "default_delivery_on": None,
                "delivery_overrides": (),
                "note": "기존 업체 휴무",
                "recorded_at": RECORDED_AT.isoformat(),
            },
        ),
    }

    restored = service_adjustment_schedule_from_state(legacy_state)

    assert restored.changes[0].origin is ServiceAdjustmentOrigin.MANUAL
    assert restored.changes[0].public_holiday is None


def test_previous_common_operating_day_skips_weekend_and_recorded_closure():
    friday_closure = ServiceAdjustmentChange(
        CLOSURE_CHANGE_ID,
        InclusiveDateRange(date(2026, 9, 11), date(2026, 9, 11)),
        tuple(Meal),
        ServiceTreatment.NOT_PROVIDED,
        None,
        (),
        "금요일 업체 휴무",
        RECORDED_AT,
    )
    schedule = ServiceAdjustmentSchedule((friday_closure,))

    assert previous_common_operating_day(
        schedule,
        tuple(Meal),
        date(2026, 9, 14),
    ) == date(2026, 9, 10)


def test_previous_common_operating_day_skips_unconfirmed_holiday_candidates():
    assert previous_common_operating_day(
        ServiceAdjustmentSchedule(),
        tuple(Meal),
        CHUSEOK.day,
        excluded_days=(CHUSEOK_EVE.day,),
    ) == date(2026, 9, 23)


def test_revision_records_exact_vendor_choice_without_collapsing_dates():
    prepared = build_service_adjustment_revision(
        BASE_REVISION_ID,
        None,
        InclusiveDateRange(date(2026, 9, 10), date(2026, 9, 12)),
        tuple(Meal),
        ServiceTreatment.REPLACEMENT,
        date(2026, 9, 9),
        (
            ReplacementDeliveryOverride(
                Meal.LUNCH,
                date(2026, 9, 11),
                date(2026, 9, 8),
            ),
        ),
        "업체 연속 휴무",
        actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=RECORDED_AT,
        revision_id=REVISION_ID,
        change_id=REPLACEMENT_CHANGE_ID,
    )

    decision = prepared.record.metadata.decisions[
        "service_adjustment_change"
    ]
    assert prepared.record.metadata.effective_on == date(2026, 9, 10)
    assert decision["default_delivery_on"] == "2026-09-09"
    assert decision["delivery_overrides"] == (
        {
            "meal": "중식",
            "service_on": "2026-09-11",
            "delivery_on": "2026-09-08",
        },
    )
    assert prepared.schedule.provision_for(
        Meal.LUNCH,
        date(2026, 9, 11),
    ).billable


def test_holiday_review_tracks_each_meal_without_auto_closing_unreviewed_meals():
    reviewed = ServiceAdjustmentChange(
        CLOSURE_CHANGE_ID,
        InclusiveDateRange(CHUSEOK.day, CHUSEOK.day),
        (Meal.BREAKFAST, Meal.LUNCH),
        ServiceTreatment.NOT_PROVIDED,
        None,
        (),
        CHUSEOK.name,
        RECORDED_AT,
        ServiceAdjustmentOrigin.PUBLIC_HOLIDAY,
        CHUSEOK,
    )
    schedule = ServiceAdjustmentSchedule((reviewed,))

    assert schedule.public_holiday_treatments_for(CHUSEOK.day) == {
        Meal.BREAKFAST: ServiceTreatment.NOT_PROVIDED,
        Meal.LUNCH: ServiceTreatment.NOT_PROVIDED,
    }
    assert schedule.provision_for(
        Meal.DINNER,
        CHUSEOK.day,
    ).treatment is ServiceTreatment.NORMAL


def test_later_holiday_review_updates_only_selected_meal():
    first = ServiceAdjustmentChange(
        CLOSURE_CHANGE_ID,
        InclusiveDateRange(CHUSEOK.day, CHUSEOK.day),
        tuple(Meal),
        ServiceTreatment.NOT_PROVIDED,
        None,
        (),
        CHUSEOK.name,
        RECORDED_AT,
        ServiceAdjustmentOrigin.PUBLIC_HOLIDAY,
        CHUSEOK,
    )
    later = ServiceAdjustmentChange(
        UUID("81000000-0000-0000-0000-000000000005"),
        InclusiveDateRange(CHUSEOK.day, CHUSEOK.day),
        (Meal.LUNCH,),
        ServiceTreatment.NORMAL,
        None,
        (),
        "추석 중식 정상 운영",
        RECORDED_AT,
        ServiceAdjustmentOrigin.PUBLIC_HOLIDAY,
        CHUSEOK,
    )
    schedule = ServiceAdjustmentSchedule((first, later))

    assert schedule.public_holiday_treatments_for(CHUSEOK.day) == {
        Meal.BREAKFAST: ServiceTreatment.NOT_PROVIDED,
        Meal.LUNCH: ServiceTreatment.NORMAL,
        Meal.DINNER: ServiceTreatment.NOT_PROVIDED,
    }
    assert schedule.provision_for(
        Meal.LUNCH,
        CHUSEOK.day,
    ).treatment is ServiceTreatment.NORMAL


def test_holiday_source_round_trip_and_revision_decision_are_auditable():
    prepared = build_service_adjustment_revision(
        BASE_REVISION_ID,
        None,
        InclusiveDateRange(CHUSEOK.day, CHUSEOK.day),
        tuple(Meal),
        ServiceTreatment.NORMAL,
        None,
        (),
        CHUSEOK.name,
        actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=RECORDED_AT,
        revision_id=REVISION_ID,
        change_id=CLOSURE_CHANGE_ID,
        origin=ServiceAdjustmentOrigin.PUBLIC_HOLIDAY,
        public_holiday=CHUSEOK,
        reason="공휴일 운영 검토 저장",
    )

    restored = service_adjustment_schedule_from_state(
        prepared.record.changes[0].after
    )
    decision = prepared.record.metadata.decisions[
        "service_adjustment_change"
    ]
    assert restored == prepared.schedule
    assert decision["origin"] == "public_holiday"
    assert decision["public_holiday"] == {
        "candidate_key": "KR:2026-09-25",
        "day": "2026-09-25",
        "name": "추석",
        "provider_id": "python-holidays:KR",
        "provider_version": "0.102",
    }


def test_public_holiday_origin_requires_exact_single_candidate_day():
    with pytest.raises(ValueError, match="후보 날짜 하루"):
        ServiceAdjustmentChange(
            CLOSURE_CHANGE_ID,
            InclusiveDateRange(date(2026, 9, 24), date(2026, 9, 25)),
            tuple(Meal),
            ServiceTreatment.NOT_PROVIDED,
            None,
            (),
            CHUSEOK.name,
            RECORDED_AT,
            ServiceAdjustmentOrigin.PUBLIC_HOLIDAY,
            CHUSEOK,
        )


def test_batch_holiday_review_uses_one_revision_and_one_schedule_audit():
    prepared = build_public_holiday_review_revision(
        BASE_REVISION_ID,
        None,
        (CHUSEOK, CHUSEOK_EVE),
        tuple(Meal),
        ServiceTreatment.NOT_PROVIDED,
        (),
        actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=RECORDED_AT,
        revision_id=REVISION_ID,
        change_ids=(CLOSURE_CHANGE_ID, BATCH_CHANGE_ID),
    )

    assert len(prepared.record.changes) == 1
    assert len(prepared.changes) == 2
    assert tuple(
        change.public_holiday.day for change in prepared.changes
    ) == (CHUSEOK_EVE.day, CHUSEOK.day)
    assert prepared.record.metadata.effective_on == CHUSEOK_EVE.day
    assert prepared.record.metadata.reason == "공휴일 운영 일괄 검토 저장"
    decisions = prepared.record.metadata.decisions[
        "public_holiday_reviews"
    ]
    assert len(decisions) == 2
    assert not prepared.schedule.provision_for(
        Meal.DINNER,
        CHUSEOK_EVE.day,
    ).billable
    assert not prepared.schedule.provision_for(
        Meal.BREAKFAST,
        CHUSEOK.day,
    ).billable


def test_batch_replacement_preserves_each_holiday_and_meal_delivery_date():
    prepared = build_public_holiday_review_revision(
        BASE_REVISION_ID,
        None,
        (CHUSEOK_EVE, CHUSEOK),
        (Meal.BREAKFAST, Meal.LUNCH),
        ServiceTreatment.REPLACEMENT,
        (
            ReplacementDeliveryOverride(
                Meal.BREAKFAST,
                CHUSEOK_EVE.day,
                date(2026, 9, 23),
            ),
            ReplacementDeliveryOverride(
                Meal.LUNCH,
                CHUSEOK_EVE.day,
                date(2026, 9, 23),
            ),
            ReplacementDeliveryOverride(
                Meal.BREAKFAST,
                CHUSEOK.day,
                date(2026, 9, 23),
            ),
            ReplacementDeliveryOverride(
                Meal.LUNCH,
                CHUSEOK.day,
                date(2026, 9, 22),
            ),
        ),
        actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=RECORDED_AT,
        revision_id=REVISION_ID,
        change_ids=(CLOSURE_CHANGE_ID, BATCH_CHANGE_ID),
    )

    assert prepared.schedule.provision_for(
        Meal.LUNCH,
        CHUSEOK_EVE.day,
    ).delivery_on == date(2026, 9, 23)
    assert prepared.schedule.provision_for(
        Meal.LUNCH,
        CHUSEOK.day,
    ).delivery_on == date(2026, 9, 22)
    assert prepared.changes[1].delivery_overrides == (
        ReplacementDeliveryOverride(
            Meal.LUNCH,
            CHUSEOK.day,
            date(2026, 9, 22),
        ),
    )


def test_batch_replacement_requires_every_selected_holiday_meal_cell():
    with pytest.raises(ValueError, match="모든 공휴일·식종"):
        build_public_holiday_review_revision(
            BASE_REVISION_ID,
            None,
            (CHUSEOK_EVE, CHUSEOK),
            (Meal.BREAKFAST,),
            ServiceTreatment.REPLACEMENT,
            (
                ReplacementDeliveryOverride(
                    Meal.BREAKFAST,
                    CHUSEOK_EVE.day,
                    date(2026, 9, 23),
                ),
            ),
            actor=RevisionActor("tester", "S-1-5-21-test"),
            recorded_at=RECORDED_AT,
            revision_id=REVISION_ID,
            change_ids=(CLOSURE_CHANGE_ID, BATCH_CHANGE_ID),
        )


def test_batch_replacement_only_requires_explicit_scheduled_targets():
    targets = (
        (CHUSEOK_EVE.day, Meal.BREAKFAST),
        (CHUSEOK_EVE.day, Meal.LUNCH),
        (CHUSEOK.day, Meal.LUNCH),
    )
    deliveries = (
        ReplacementDeliveryOverride(
            Meal.BREAKFAST,
            CHUSEOK_EVE.day,
            date(2026, 9, 23),
        ),
        ReplacementDeliveryOverride(
            Meal.LUNCH,
            CHUSEOK_EVE.day,
            date(2026, 9, 23),
        ),
        ReplacementDeliveryOverride(
            Meal.LUNCH,
            CHUSEOK.day,
            date(2026, 9, 22),
        ),
    )

    prepared = build_public_holiday_review_revision(
        BASE_REVISION_ID,
        None,
        (CHUSEOK_EVE, CHUSEOK),
        (Meal.BREAKFAST, Meal.LUNCH),
        ServiceTreatment.REPLACEMENT,
        deliveries,
        actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=RECORDED_AT,
        revision_id=REVISION_ID,
        change_ids=(CLOSURE_CHANGE_ID, BATCH_CHANGE_ID),
        replacement_targets=targets,
    )

    assert prepared.changes[0].meals == (Meal.BREAKFAST, Meal.LUNCH)
    assert prepared.changes[1].meals == (Meal.LUNCH,)
    assert prepared.schedule.provision_for(
        Meal.BREAKFAST,
        CHUSEOK.day,
    ).treatment is ServiceTreatment.NORMAL
    assert prepared.schedule.provision_for(
        Meal.LUNCH,
        CHUSEOK.day,
    ).delivery_on == date(2026, 9, 22)

    with pytest.raises(ValueError, match="모든 공휴일·식종"):
        build_public_holiday_review_revision(
            BASE_REVISION_ID,
            None,
            (CHUSEOK_EVE, CHUSEOK),
            (Meal.BREAKFAST, Meal.LUNCH),
            ServiceTreatment.REPLACEMENT,
            deliveries[:-1],
            actor=RevisionActor("tester", "S-1-5-21-test"),
            recorded_at=RECORDED_AT,
            revision_id=REVISION_ID,
            change_ids=(CLOSURE_CHANGE_ID, BATCH_CHANGE_ID),
            replacement_targets=targets,
        )


def test_batch_replacement_audit_omits_holiday_without_scheduled_targets():
    prepared = build_public_holiday_review_revision(
        BASE_REVISION_ID,
        None,
        (CHUSEOK_EVE, CHUSEOK),
        (Meal.BREAKFAST, Meal.LUNCH),
        ServiceTreatment.REPLACEMENT,
        (
            ReplacementDeliveryOverride(
                Meal.LUNCH,
                CHUSEOK.day,
                date(2026, 9, 22),
            ),
        ),
        actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=RECORDED_AT,
        revision_id=REVISION_ID,
        change_ids=(CLOSURE_CHANGE_ID, BATCH_CHANGE_ID),
        replacement_targets=((CHUSEOK.day, Meal.LUNCH),),
    )

    assert len(prepared.changes) == 1
    assert prepared.changes[0].change_id == BATCH_CHANGE_ID
    assert prepared.changes[0].public_holiday == CHUSEOK
    assert prepared.record.metadata.effective_on == CHUSEOK.day
    assert len(
        prepared.record.metadata.decisions["public_holiday_reviews"]
    ) == 1
    assert prepared.schedule.provision_for(
        Meal.LUNCH,
        CHUSEOK_EVE.day,
    ).treatment is ServiceTreatment.NORMAL
