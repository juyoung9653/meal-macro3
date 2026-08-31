"""Effective-dated multi-meal unit price domain and ledger tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest

apsw = pytest.importorskip(
    "apsw",
    reason="install requirements-sqlite-v2.txt for encrypted DB integration tests",
)

from src.domain import (
    DEFAULT_MEAL_UNIT_PRICE,
    UNIT_PRICE_POLICY_VERSION,
    UNIT_PRICE_SCHEDULE_ID,
    UNIT_PRICE_SCHEDULE_KIND,
    AuditChange,
    ChangeOperation,
    Meal,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
    UnitPriceChange,
    UnitPriceSchedule,
    build_unit_price_revision,
    global_unit_price_note,
    unit_price_period_segments,
    unit_price_schedule_from_snapshot,
    unit_price_schedule_from_state,
    unit_price_schedule_state,
)
from src.storage import EncryptedDatabase


KEY = bytes.fromhex("79" * 32)
BASE_REVISION_ID = UUID("79000000-0000-0000-0000-000000000001")
BASE_ENTITY_ID = UUID("79000000-0000-0000-0000-000000000002")
FIRST_REVISION_ID = UUID("79000000-0000-0000-0000-000000000003")
FIRST_CHANGE_ID = UUID("79000000-0000-0000-0000-000000000004")
SECOND_REVISION_ID = UUID("79000000-0000-0000-0000-000000000005")
SECOND_CHANGE_ID = UUID("79000000-0000-0000-0000-000000000006")
ACTOR = RevisionActor("tester", "S-1-5-21-test")


def _change(
    change_id: UUID,
    meals: tuple[Meal, ...],
    unit_price: int,
    effective_on: date,
    recorded_hour: int,
) -> UnitPriceChange:
    return UnitPriceChange(
        change_id,
        meals,
        unit_price,
        effective_on,
        datetime(2026, 8, 26, recorded_hour, 0, tzinfo=UTC),
    )


def _base_revision() -> RevisionRecord:
    return RevisionRecord(
        RevisionMetadata(
            revision_id=BASE_REVISION_ID,
            parent_revision_id=None,
            recorded_at=datetime(2026, 8, 26, 1, 0, tzinfo=UTC),
            effective_on=date(2026, 8, 1),
            actor=ACTOR,
            reason="단가 기준용 명단",
            source=RevisionSource.UI,
        ),
        (
            AuditChange(
                "pricing-probe",
                BASE_ENTITY_ID,
                ChangeOperation.INSERT,
                before=None,
                after={"value": "base"},
            ),
        ),
    )


def test_default_price_is_ten_thousand_won_for_every_meal():
    schedule = UnitPriceSchedule()

    assert DEFAULT_MEAL_UNIT_PRICE == 10_000
    assert dict(schedule.prices_on(date(2026, 1, 1))) == {
        Meal.BREAKFAST: 10_000,
        Meal.LUNCH: 10_000,
        Meal.DINNER: 10_000,
    }


def test_one_change_applies_same_price_to_multiple_selected_meals_inclusively():
    schedule = UnitPriceSchedule().with_change(
        _change(
            FIRST_CHANGE_ID,
            (Meal.DINNER, Meal.BREAKFAST),
            11_000,
            date(2026, 9, 15),
            2,
        )
    )

    assert schedule.price_for(Meal.BREAKFAST, date(2026, 9, 14)) == 10_000
    assert schedule.price_for(Meal.BREAKFAST, date(2026, 9, 15)) == 11_000
    assert schedule.price_for(Meal.DINNER, date(2026, 10, 1)) == 11_000
    assert schedule.price_for(Meal.LUNCH, date(2026, 10, 1)) == 10_000
    assert schedule.changes[0].meals == (Meal.BREAKFAST, Meal.DINNER)


def test_effective_date_wins_before_record_order_and_same_day_later_change_wins():
    schedule = UnitPriceSchedule(
        changes=(
            _change(
                FIRST_CHANGE_ID,
                (Meal.BREAKFAST,),
                11_000,
                date(2026, 1, 1),
                2,
            ),
            _change(
                SECOND_CHANGE_ID,
                (Meal.BREAKFAST,),
                9_000,
                date(2025, 12, 1),
                3,
            ),
            _change(
                UUID("79000000-0000-0000-0000-000000000007"),
                (Meal.BREAKFAST,),
                12_000,
                date(2026, 1, 1),
                4,
            ),
        )
    )

    assert schedule.price_for(Meal.BREAKFAST, date(2025, 12, 15)) == 9_000
    assert schedule.price_for(Meal.BREAKFAST, date(2026, 2, 1)) == 12_000


def test_schedule_state_round_trip_preserves_raw_change_order():
    schedule = UnitPriceSchedule().with_change(
        _change(
            FIRST_CHANGE_ID,
            (Meal.BREAKFAST, Meal.LUNCH),
            10_500,
            date(2026, 9, 1),
            2,
        )
    )

    assert unit_price_schedule_from_state(unit_price_schedule_state(schedule)) == schedule


def test_price_effective_on_first_day_has_one_period_and_no_global_note():
    schedule = UnitPriceSchedule().with_change(
        _change(
            FIRST_CHANGE_ID,
            (Meal.LUNCH,),
            11_000,
            date(2026, 9, 1),
            2,
        )
    )

    segments = unit_price_period_segments(
        schedule,
        Meal.LUNCH,
        date(2026, 9, 1),
        date(2026, 9, 30),
    )
    assert len(segments) == 1
    assert segments[0].starts_on == date(2026, 9, 1)
    assert segments[0].ends_on == date(2026, 9, 30)
    assert segments[0].unit_price == 11_000
    assert global_unit_price_note(
        schedule,
        date(2026, 9, 1),
        date(2026, 9, 30),
    ) is None


def test_midmonth_multi_meal_change_produces_one_grouped_global_note():
    schedule = UnitPriceSchedule().with_change(
        _change(
            FIRST_CHANGE_ID,
            (Meal.LUNCH, Meal.BREAKFAST),
            11_000,
            date(2026, 9, 15),
            2,
        )
    )

    assert global_unit_price_note(
        schedule,
        date(2026, 9, 1),
        date(2026, 9, 30),
    ) == (
        "조식·중식 단가: 1~14일 10,000원 / 15~30일 11,000원"
    )


def test_same_effective_price_does_not_create_a_false_midmonth_note():
    schedule = UnitPriceSchedule().with_change(
        _change(
            FIRST_CHANGE_ID,
            (Meal.DINNER,),
            10_000,
            date(2026, 9, 15),
            2,
        )
    )

    assert len(
        unit_price_period_segments(
            schedule,
            Meal.DINNER,
            date(2026, 9, 1),
            date(2026, 9, 30),
        )
    ) == 1
    assert global_unit_price_note(
        schedule,
        date(2026, 9, 1),
        date(2026, 9, 30),
    ) is None


def test_builder_records_multi_meal_decision_and_ledger_replays_latest_schedule(
    tmp_path: Path,
):
    first = build_unit_price_revision(
        BASE_REVISION_ID,
        None,
        (Meal.LUNCH, Meal.BREAKFAST),
        11_000,
        date(2026, 9, 1),
        actor=ACTOR,
        recorded_at=datetime(2026, 8, 26, 2, 0, tzinfo=UTC),
        revision_id=FIRST_REVISION_ID,
        change_id=FIRST_CHANGE_ID,
    )
    assert first.record.changes[0].entity_kind == UNIT_PRICE_SCHEDULE_KIND
    assert first.record.changes[0].entity_id == UNIT_PRICE_SCHEDULE_ID
    assert first.record.changes[0].operation is ChangeOperation.INSERT
    assert first.record.metadata.rule_versions["unit_price_policy_version"] == (
        UNIT_PRICE_POLICY_VERSION
    )
    assert first.record.metadata.decisions["unit_price_change"] == {
        "change_id": str(FIRST_CHANGE_ID),
        "meals": (Meal.BREAKFAST.value, Meal.LUNCH.value),
        "unit_price": 11_000,
        "effective_on": "2026-09-01",
    }

    path = tmp_path / "operations.sqlite3"
    with EncryptedDatabase.create(path, KEY) as database:
        database.append_revision(_base_revision())
        database.append_revision(first.record)
        first_snapshot = database.snapshot(FIRST_REVISION_ID)
        stored_state = first_snapshot.entity(
            UNIT_PRICE_SCHEDULE_KIND,
            UNIT_PRICE_SCHEDULE_ID,
        )
        second = build_unit_price_revision(
            FIRST_REVISION_ID,
            stored_state,
            (Meal.DINNER,),
            12_000,
            date(2026, 10, 1),
            actor=ACTOR,
            recorded_at=datetime(2026, 8, 26, 3, 0, tzinfo=UTC),
            revision_id=SECOND_REVISION_ID,
            change_id=SECOND_CHANGE_ID,
        )
        assert second.record.changes[0].operation is ChangeOperation.UPDATE
        database.append_revision(second.record)

        replayed = unit_price_schedule_from_snapshot(
            database.snapshot(SECOND_REVISION_ID)
        )
        assert replayed.price_for(Meal.BREAKFAST, date(2026, 10, 1)) == 11_000
        assert replayed.price_for(Meal.DINNER, date(2026, 10, 1)) == 12_000
        database.check_integrity()
