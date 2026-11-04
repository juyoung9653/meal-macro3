"""Build dated delivery rows grouped by household and driver identity."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from uuid import UUID

from src.domain.delivery_routing import MEAL_ORDER, RoutingState
from src.domain.roster import (
    Meal,
    RosterSnapshot,
    additional_mobile_phone_field_ordinal,
    additional_telephone_field_ordinal,
)


@dataclass(frozen=True, slots=True)
class DriverHouseholdRow:
    household_key: UUID
    driver_id: UUID | None
    driver_name: str
    member_ids: tuple[UUID, ...]
    member_names: tuple[str, ...]
    meals: tuple[Meal, ...]
    address: str
    contacts: tuple[str, ...]
    sequences: tuple[str, ...]
    # Exact member/contact/sequence projections for each meal in this row.
    # Empty for callers constructing a display row directly.
    meal_rows: tuple[DriverHouseholdRow, ...] = ()
    # Preserve missing values that are intentionally absent from display text.
    has_empty_sequence: bool = False
    lunch_periods: tuple[tuple[str, date | None, date | None], ...] = ()


def build_driver_household_rows(
    roster: RosterSnapshot,
    routing_state: RoutingState,
    on: date,
    *,
    included_meals: Mapping[UUID, tuple[Meal, ...]] | None = None,
    browse_all_meals: bool = False,
    excluded_person_ids: Collection[UUID] = (),
) -> tuple[DriverHouseholdRow, ...]:
    """Return one row per household and driver, retaining distinct field values.

    A registry UUID is the only registered-driver identity.  A legacy name with no
    UUID stays a separate name-only identity, so it cannot be merged with a
    registered driver merely because their displayed names match.
    """

    if type(on) is not date:
        raise TypeError("조회일은 날짜여야 합니다.")

    excluded = frozenset(excluded_person_ids)
    household_by_person = {
        membership.person_id: membership.household_id
        for membership in roster.current_memberships
        if membership.person_id not in excluded
    }
    groups: dict[tuple[UUID, tuple[str, UUID | str | None]], _RowParts] = {}
    meal_groups: dict[
        tuple[UUID, tuple[str, UUID | str | None], Meal], _RowParts
    ] = {}
    additional_contact_fields = _additional_contact_fields(roster)

    for person in roster.persons:
        if person.person_id in excluded:
            continue
        active_meals = roster.meals_for(person.person_id, on=on)
        available_meals = (
            _browsable_meals(roster, routing_state, person.person_id, on, active_meals)
            if browse_all_meals
            else active_meals
        )
        selected_meals = (
            available_meals
            if included_meals is None
            else tuple(
                meal
                for meal in MEAL_ORDER
                if meal in available_meals
                and meal in included_meals.get(person.person_id, ())
            )
        )
        if not selected_meals:
            continue

        resolved = routing_state.resolve(roster, person.person_id, on)
        household_key = household_by_person.get(person.person_id, person.person_id)
        address = _address(resolved.road_address, None)
        for meal in selected_meals:
            driver_id = resolved.driver_ids[meal]
            driver_name = resolved.drivers[meal]
            identity: tuple[str, UUID | str | None]
            if driver_id is not None:
                identity = ("registered", driver_id)
            elif driver_name:
                identity = ("legacy", driver_name)
            else:
                identity = ("unassigned", None)
            key = (household_key, identity)
            parts = groups.setdefault(
                key,
                _RowParts(household_key, driver_id, driver_name),
            )
            parts.add(
                person_id=person.person_id,
                name=person.profile.name,
                contacts=_contacts(person, additional_contact_fields),
                meal=meal,
                sequence=roster.resolve_assignment(person.person_id, meal).sequence,
                address=address,
                lunch_period=person.lunch_period,
            )
            meal_parts = meal_groups.setdefault(
                (*key, meal),
                _RowParts(household_key, driver_id, driver_name),
            )
            meal_parts.add(
                person_id=person.person_id,
                name=person.profile.name,
                contacts=_contacts(person, additional_contact_fields),
                meal=meal,
                sequence=roster.resolve_assignment(person.person_id, meal).sequence,
                address=address,
                lunch_period=person.lunch_period,
            )

    rows = tuple(
        replace(
            parts.row(),
            meal_rows=tuple(
                meal_groups[(*key, meal)].row()
                for meal in MEAL_ORDER
                if (*key, meal) in meal_groups
            ),
        )
        for key, parts in groups.items()
    )
    return tuple(sorted(rows, key=_row_sort_key))


def _browsable_meals(
    roster: RosterSnapshot,
    routing_state: RoutingState,
    person_id: UUID,
    on: date,
    active_meals: tuple[Meal, ...],
) -> tuple[Meal, ...]:
    meals = set(active_meals)
    meals.update(
        assignment.meal
        for assignment in roster.assignments
        if assignment.person_id == person_id and assignment.meal is not None
    )
    meals.update(
        meal
        for event in routing_state.events
        if event.person_id == person_id and event.effective_on <= on
        for meal in event.driver_ids
    )
    return tuple(meal for meal in MEAL_ORDER if meal in meals)


@dataclass(slots=True)
class _RowParts:
    household_key: UUID
    driver_id: UUID | None
    driver_name: str
    addresses: list[str] = field(default_factory=list)
    member_ids: list[UUID] = field(default_factory=list)
    member_names: list[str] = field(default_factory=list)
    meals: list[Meal] = field(default_factory=list)
    contacts: list[str] = field(default_factory=list)
    sequences: list[str] = field(default_factory=list)
    has_empty_sequence: bool = False
    lunch_periods: dict[UUID, tuple[str, date | None, date | None]] = field(default_factory=dict)

    def add(
        self,
        *,
        person_id: UUID,
        name: str,
        contacts: tuple[str, ...],
        meal: Meal,
        sequence: str | None,
        address: str,
        lunch_period=None,
    ) -> None:
        if person_id not in self.member_ids:
            self.member_ids.append(person_id)
        if meal is Meal.LUNCH:
            self.lunch_periods[person_id] = (name,
                lunch_period.starts_on if lunch_period else None,
                lunch_period.ends_on if lunch_period else None)
        if name not in self.member_names:
            self.member_names.append(name)
        for contact in contacts:
            if contact not in self.contacts:
                self.contacts.append(contact)
        if meal not in self.meals:
            self.meals.append(meal)
        if not sequence:
            self.has_empty_sequence = True
        if sequence is not None and sequence not in self.sequences:
            self.sequences.append(sequence)
        if address and address not in self.addresses:
            self.addresses.append(address)

    def row(self) -> DriverHouseholdRow:
        return DriverHouseholdRow(
            self.household_key,
            self.driver_id,
            self.driver_name,
            tuple(self.member_ids),
            tuple(self.member_names),
            tuple(meal for meal in MEAL_ORDER if meal in self.meals),
            "\n".join(self.addresses),
            tuple(self.contacts),
            tuple(sorted(self.sequences, key=_sequence_sort_key)),
            has_empty_sequence=self.has_empty_sequence,
            lunch_periods=tuple(self.lunch_periods.values()),
        )


def lunch_period_texts(row: DriverHouseholdRow) -> tuple[str, str]:
    """Keep differing family-member periods paired by name in both columns."""
    periods = row.lunch_periods
    if not periods:
        return "", ""
    def text(day):
        return "" if day is None else day.isoformat()
    if len({(start, end) for _, start, end in periods}) == 1:
        return text(periods[0][1]), text(periods[0][2])
    return tuple("\n".join(f"{item[0]}: {text(item[index]) or '-'}" for item in periods)
                 for index in (1, 2))


def _address(road_address: str | None, detail_address: str | None) -> str:
    return " ".join(
        " ".join(value.split())
        for value in (road_address, detail_address)
        if value is not None and value.strip()
    )


def _additional_contact_fields(
    roster: RosterSnapshot,
) -> tuple[tuple[UUID, int, int], ...]:
    fields: list[tuple[UUID, int, int]] = []
    for definition in roster.custom_fields:
        telephone_ordinal = additional_telephone_field_ordinal(definition.name)
        if telephone_ordinal is not None:
            fields.append((definition.definition_id, 0, telephone_ordinal))
        mobile_ordinal = additional_mobile_phone_field_ordinal(definition.name)
        if mobile_ordinal is not None:
            fields.append((definition.definition_id, 1, mobile_ordinal))
    return tuple(sorted(fields, key=lambda item: (item[1], item[2])))


def _contacts(person, additional_fields: tuple[tuple[UUID, int, int], ...]) -> tuple[str, ...]:
    values: list[str] = []
    for contact in (person.profile.telephone, person.profile.mobile_phone):
        if contact:
            values.append(contact)
    for field_id, _, _ in additional_fields:
        value = person.profile.custom_values.get(field_id)
        if isinstance(value, str) and value:
            values.append(value)
    return tuple(dict.fromkeys(values))


def _sequence_sort_key(sequence: str) -> tuple[int, int | str, str]:
    text = sequence.strip()
    if text.isdigit():
        return (0, int(text), text)
    return (1, text, text)


def _row_sort_key(row: DriverHouseholdRow) -> tuple[str, tuple[int, int | str, str], str, str]:
    return (
        row.driver_name,
        min((_sequence_sort_key(value) for value in row.sequences), default=(2, "", "")),
        " ".join(row.member_names),
        str(row.household_key),
    )


def sequence_sort_key(values):
    """Shared route ordering for display, drag saves and household regrouping."""
    return tuple(
        (0, int(value.strip()), value.strip()) if value.strip().isdigit()
        else (1, 0, value.strip().casefold())
        for value in values if value.strip()
    ) or ((2, 0, ""),)
