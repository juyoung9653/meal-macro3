from datetime import UTC, date, datetime
from dataclasses import replace
from uuid import UUID

import pytest

from src.domain.custom_field_edits import (
    build_custom_field_create_revision,
    build_custom_field_delete_revision,
)
from src.domain.roster import (
    CustomFieldDefinition,
    CustomFieldType,
    HouseholdDeliverySetting,
    Meal,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.revisions import RevisionActor


def _roster() -> RosterSnapshot:
    field_id = UUID("85000000-0000-0000-0000-000000000005")
    return RosterSnapshot(
        UUID("85000000-0000-0000-0000-000000000001"),
        persons=(
            Person(
                UUID("85000000-0000-0000-0000-000000000003"),
                PersonProfile("홍길동", custom_values={field_id: "A구역"}),
            ),
        ),
        custom_fields=(
            CustomFieldDefinition(field_id, "배송 구역", CustomFieldType.TEXT, 0),
        ),
    )


def _arguments():
    return {
        "actor": RevisionActor("tester", "S-1-5-21-test"),
        "recorded_at": datetime(2026, 8, 26, 12, 30, tzinfo=UTC),
        "effective_on": date(2026, 8, 26),
        "revision_id": UUID("85000000-0000-0000-0000-000000000099"),
    }


def test_create_and_delete_field_scrubs_values_but_preserves_source_snapshot():
    roster = _roster()
    delivery_setting = HouseholdDeliverySetting(Meal.BREAKFAST, form="개인 도시락")
    roster = replace(roster, persons=(replace(roster.persons[0], delivery_settings=(delivery_setting,)),))
    field_id = roster.custom_fields[0].definition_id

    deleted = build_custom_field_delete_revision(roster, field_id, **_arguments())

    assert deleted.removed_value_count == 1
    assert deleted.snapshot.custom_fields == ()
    assert deleted.snapshot.persons[0].profile.custom_values == {}
    assert deleted.snapshot.persons[0].delivery_settings == (delivery_setting,)
    assert field_id in roster.persons[0].profile.custom_values
    assert len(deleted.record.changes) == 2

    created = build_custom_field_create_revision(
        roster,
        "선호 시간",
        CustomFieldType.TEXT,
        definition_id=UUID("85000000-0000-0000-0000-000000000098"),
        **_arguments(),
    )
    assert created.definition is not None
    assert created.snapshot.custom_fields[-1].name == "선호 시간"


@pytest.mark.parametrize("name", ("", "성명", "조식", "배송 구역"))
def test_create_rejects_blank_duplicate_and_reserved_labels(name):
    with pytest.raises(ValueError):
        build_custom_field_create_revision(
            _roster(),
            name,
            CustomFieldType.TEXT,
            definition_id=UUID("85000000-0000-0000-0000-000000000098"),
            **_arguments(),
        )
