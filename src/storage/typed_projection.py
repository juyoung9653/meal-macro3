"""Typed current-state projection maintained from immutable audit changes."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date
from uuid import UUID

import apsw

from src.domain.delivery_defaults import DELIVERY_DEFAULTS_ID, DELIVERY_DEFAULTS_KIND, delivery_defaults_from_state
from src.domain.projections import (
    ASSIGNMENT_KIND,
    CUSTOM_FIELD_KIND,
    ENROLLMENT_KIND,
    HOUSEHOLD_KIND,
    MEMBERSHIP_KIND,
    PERSON_KIND,
    STATUS_EVENT_KIND,
    TYPED_ENTITY_KINDS,
    assignment_from_state,
    custom_field_from_state,
    enrollments_from_state,
    household_from_state,
    memberships_from_state,
    person_from_state,
    roster_entity_states,
    status_event_from_state,
    status_event_state,
)
from src.domain.revisions import (
    AuditChange,
    ChangeOperation,
    EntityRef,
    JsonValue,
    plain_json,
)
from src.domain.roster import (
    CustomFieldDefinition,
    DeliveryAssignment,
    Household,
    HouseholdMembership,
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    Person,
    RosterSnapshot,
)
from src.domain.status import StatusEvent


class ProjectionContractError(ValueError):
    """A typed audit state does not satisfy its canonical domain contract."""


def apply_typed_projection_changes(
    connection: apsw.Connection,
    revision_id: UUID,
    changes: tuple[AuditChange, ...],
) -> None:
    """Apply recognized audit entities inside the caller's transaction."""

    typed_changes = tuple(
        change for change in changes if change.entity_kind in TYPED_ENTITY_KINDS
    )
    if not typed_changes:
        return

    current = load_typed_entity_states(connection)
    for change in typed_changes:
        reference = EntityRef(change.entity_kind, change.entity_id)
        actual = current.get(reference)
        if change.operation is ChangeOperation.INSERT:
            if actual is not None:
                raise ProjectionContractError(
                    f"typed projection entity가 이미 있습니다: {reference}"
                )
        elif plain_json(change.before) != (
            None if actual is None else plain_json(actual)
        ):
            raise ProjectionContractError(
                f"typed projection before가 현재 상태와 다릅니다: {reference}"
            )

    connection.execute(
        "INSERT INTO projection_write_context(revision_id) VALUES (?)",
        (str(revision_id),),
    )
    deletes = sorted(
        (
            change
            for change in typed_changes
            if change.operation is ChangeOperation.DELETE
        ),
        key=lambda change: (_delete_order(change.entity_kind), str(change.entity_id)),
    )
    writes = sorted(
        (
            change
            for change in typed_changes
            if change.operation is not ChangeOperation.DELETE
        ),
        key=lambda change: (_write_order(change.entity_kind), str(change.entity_id)),
    )
    for change in (*deletes, *writes):
        _apply_change(connection, revision_id, change)
    connection.execute(
        "DELETE FROM projection_write_context WHERE revision_id = ?",
        (str(revision_id),),
    )


def load_current_roster(
    connection: apsw.Connection,
    revision_id: UUID,
) -> RosterSnapshot:
    persons = tuple(_load_persons(connection))
    households = tuple(_load_households(connection))
    memberships = tuple(_load_memberships(connection))
    enrollments = tuple(_load_enrollments(connection))
    assignments = tuple(_load_assignments(connection))
    custom_fields = tuple(_load_custom_fields(connection))
    defaults_row = connection.execute(
        """SELECT change.after_json FROM audit_change AS change
           JOIN revision ON revision.revision_id = change.revision_id
           WHERE change.entity_kind = ? AND change.entity_id = ?
           ORDER BY revision.sequence DESC, change.ordinal DESC LIMIT 1""",
        (DELIVERY_DEFAULTS_KIND, str(DELIVERY_DEFAULTS_ID)),
    ).fetchone()
    defaults = delivery_defaults_from_state(
        None if defaults_row is None or defaults_row[0] is None else json.loads(defaults_row[0])
    )
    return RosterSnapshot(
        revision_id,
        persons,
        households,
        memberships,
        enrollments,
        assignments,
        custom_fields,
        delivery_defaults=defaults,
    )


def load_status_events(
    connection: apsw.Connection,
    *,
    person_id: UUID | None = None,
    meal: Meal | None = None,
) -> tuple[StatusEvent, ...]:
    if person_id is not None and not isinstance(person_id, UUID):
        raise TypeError("사람 ID는 UUID 또는 None이어야 합니다.")
    if meal is not None and not isinstance(meal, Meal):
        raise TypeError("meal은 Meal 또는 None이어야 합니다.")

    clauses: list[str] = []
    parameters: list[str] = []
    if person_id is not None:
        clauses.append("person_id = ?")
        parameters.append(str(person_id))
    if meal is not None:
        clauses.append("meal = ?")
        parameters.append(meal.value)
    where = "" if not clauses else " WHERE " + " AND ".join(clauses)
    rows = connection.execute(
        """
        SELECT event_id, created_revision_id, person_id, meal, status,
               starts_on, ends_on, recorded_at, source_text
        FROM status_event_projection
        """
        + where
        + " ORDER BY recorded_at, event_id",
        parameters,
    )
    return tuple(_status_event_from_row(row) for row in rows)


def load_typed_entity_states(
    connection: apsw.Connection,
) -> dict[EntityRef, Mapping[str, JsonValue]]:
    latest = connection.execute(
        "SELECT revision_id FROM revision ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    states: dict[EntityRef, Mapping[str, JsonValue]] = {}
    if latest is not None:
        roster = load_current_roster(connection, UUID(str(latest[0])))
        states.update(
            (reference, state)
            for reference, state in roster_entity_states(roster).items()
            if reference.entity_kind in TYPED_ENTITY_KINDS
        )
    for event in load_status_events(connection):
        states[EntityRef(STATUS_EVENT_KIND, event.event_id)] = status_event_state(event)
    return states


def validate_typed_projections(connection: apsw.Connection) -> None:
    """Compare every typed current row with a replay of the audit ledger."""

    expected: dict[EntityRef, Mapping[str, JsonValue]] = {}
    for row in connection.execute(
        """
        SELECT change.entity_kind, change.entity_id, change.operation,
               change.after_json
        FROM audit_change AS change
        JOIN revision ON revision.revision_id = change.revision_id
        WHERE change.entity_kind IN (?, ?, ?, ?, ?, ?, ?)
        ORDER BY revision.sequence, change.ordinal
        """,
        tuple(sorted(TYPED_ENTITY_KINDS)),
    ):
        reference = EntityRef(str(row[0]), UUID(str(row[1])))
        operation = ChangeOperation(str(row[2]))
        if operation is ChangeOperation.DELETE:
            expected.pop(reference, None)
        else:
            expected[reference] = json.loads(str(row[3]))

    try:
        actual = load_typed_entity_states(connection)
    except (TypeError, ValueError) as error:
        raise ProjectionContractError(
            f"typed projection을 domain 객체로 복원할 수 없습니다: {error}"
        ) from error
    if _plain_states(actual) != _plain_states(expected):
        raise ProjectionContractError(
            "typed projection이 revision ledger의 최신 상태와 다릅니다."
        )


def _apply_change(
    connection: apsw.Connection,
    revision_id: UUID,
    change: AuditChange,
) -> None:
    state = change.after
    if change.entity_kind == PERSON_KIND:
        _apply_person(connection, revision_id, change, state)
    elif change.entity_kind == HOUSEHOLD_KIND:
        _apply_household(connection, revision_id, change, state)
    elif change.entity_kind == MEMBERSHIP_KIND:
        _apply_memberships(connection, revision_id, change, state)
    elif change.entity_kind == ENROLLMENT_KIND:
        _apply_enrollments(connection, revision_id, change, state)
    elif change.entity_kind == ASSIGNMENT_KIND:
        _apply_assignment(connection, revision_id, change, state)
    elif change.entity_kind == STATUS_EVENT_KIND:
        _apply_status_event(connection, revision_id, change, state)
    elif change.entity_kind == CUSTOM_FIELD_KIND:
        _apply_custom_field(connection, revision_id, change, state)


def _apply_custom_field(
    connection: apsw.Connection,
    revision_id: UUID,
    change: AuditChange,
    state: Mapping[str, JsonValue] | None,
) -> None:
    if change.operation is ChangeOperation.DELETE:
        _delete_one(
            connection,
            "custom_field_definition_projection",
            "definition_id",
            change.entity_id,
        )
        return
    assert state is not None
    definition = custom_field_from_state(change.entity_id, state)
    values = (
        str(definition.definition_id),
        str(revision_id),
        definition.name,
        definition.field_type.value,
        definition.display_order,
        json.dumps(
            definition.choices,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ),
    )
    if change.operation is ChangeOperation.INSERT:
        connection.execute(
            """
            INSERT INTO custom_field_definition_projection(
                definition_id, last_revision_id, name, field_type,
                display_order, choices_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        return
    connection.execute(
        """
        UPDATE custom_field_definition_projection
        SET last_revision_id = ?, name = ?, field_type = ?,
            display_order = ?, choices_json = ?
        WHERE definition_id = ?
        """,
        (*values[1:], values[0]),
    )
    _require_one_changed(connection, CUSTOM_FIELD_KIND, change.entity_id)


def _apply_person(
    connection: apsw.Connection,
    revision_id: UUID,
    change: AuditChange,
    state: Mapping[str, JsonValue] | None,
) -> None:
    if change.operation is ChangeOperation.DELETE:
        _delete_one(connection, "person_projection", "person_id", change.entity_id)
        return
    assert state is not None
    person = person_from_state(change.entity_id, state)
    values = _person_values(person, revision_id)
    if change.operation is ChangeOperation.INSERT:
        connection.execute(
            """
            INSERT INTO person_projection(
                person_id, last_revision_id, name, birth_date, school_type,
                school_name, grade, class_name, household_head_name,
                relationship_to_child, jurisdiction, provider, road_address,
                detail_address, telephone, mobile_phone, note,
                custom_values_json, delivery_settings_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        return
    connection.execute(
        """
        UPDATE person_projection
        SET last_revision_id = ?, name = ?, birth_date = ?, school_type = ?,
            school_name = ?, grade = ?, class_name = ?,
            household_head_name = ?, relationship_to_child = ?,
            jurisdiction = ?, provider = ?, road_address = ?, detail_address = ?,
            telephone = ?, mobile_phone = ?, note = ?, custom_values_json = ?, delivery_settings_json = ?
        WHERE person_id = ?
        """,
        (*values[1:], values[0]),
    )
    _require_one_changed(connection, PERSON_KIND, change.entity_id)


def _apply_household(
    connection: apsw.Connection,
    revision_id: UUID,
    change: AuditChange,
    state: Mapping[str, JsonValue] | None,
) -> None:
    if change.operation is ChangeOperation.DELETE:
        _delete_one(
            connection,
            "household_projection",
            "household_id",
            change.entity_id,
        )
        return
    assert state is not None
    household = household_from_state(change.entity_id, state)
    settings_json = json.dumps([
        {"meal": item.meal.value, "weekdays": None if item.weekdays is None else list(item.weekdays), "form": item.form,
         **({"delivery_weekdays": list(item.delivery_weekdays)} if item.delivery_weekdays is not None else {})}
        for item in household.delivery_settings
    ], ensure_ascii=False)
    if change.operation is ChangeOperation.INSERT:
        connection.execute(
            """
            INSERT INTO household_projection(household_id, last_revision_id, delivery_settings_json)
            VALUES (?, ?, ?)
            """,
            (str(household.household_id), str(revision_id), settings_json),
        )
        return
    connection.execute(
        """
        UPDATE household_projection
        SET last_revision_id = ?, delivery_settings_json = ?
        WHERE household_id = ?
        """,
        (str(revision_id), settings_json, str(change.entity_id)),
    )
    _require_one_changed(connection, HOUSEHOLD_KIND, change.entity_id)


def _apply_memberships(
    connection: apsw.Connection,
    revision_id: UUID,
    change: AuditChange,
    state: Mapping[str, JsonValue] | None,
) -> None:
    if change.operation is ChangeOperation.DELETE:
        connection.execute(
            "DELETE FROM household_membership_projection WHERE person_id = ?",
            (str(change.entity_id),),
        )
        return
    assert state is not None
    memberships = memberships_from_state(change.entity_id, state)
    connection.execute(
        "DELETE FROM household_membership_projection WHERE person_id = ?",
        (str(change.entity_id),),
    )
    for ordinal, membership in enumerate(memberships, start=1):
        connection.execute(
            """
            INSERT INTO household_membership_projection(
                person_id, ordinal, household_id, starts_on, ends_on,
                last_revision_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(membership.person_id),
                ordinal,
                str(membership.household_id),
                _date_text(membership.period.starts_on),
                _date_text(membership.period.ends_on),
                str(revision_id),
            ),
        )


def _apply_enrollments(
    connection: apsw.Connection,
    revision_id: UUID,
    change: AuditChange,
    state: Mapping[str, JsonValue] | None,
) -> None:
    current_state = state if state is not None else change.before
    assert current_state is not None
    enrollments = enrollments_from_state(change.entity_id, current_state)
    person_id = UUID(str(current_state["person_id"]))
    meal = Meal(str(current_state["meal"]))
    connection.execute(
        """
        DELETE FROM meal_enrollment_projection
        WHERE person_id = ? AND meal = ?
        """,
        (str(person_id), meal.value),
    )
    if change.operation is ChangeOperation.DELETE:
        return
    for ordinal, enrollment in enumerate(enrollments, start=1):
        connection.execute(
            """
            INSERT INTO meal_enrollment_projection(
                person_id, meal, ordinal, starts_on, ends_on, last_revision_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(enrollment.person_id),
                enrollment.meal.value,
                ordinal,
                _date_text(enrollment.period.starts_on),
                _date_text(enrollment.period.ends_on),
                str(revision_id),
            ),
        )


def _apply_assignment(
    connection: apsw.Connection,
    revision_id: UUID,
    change: AuditChange,
    state: Mapping[str, JsonValue] | None,
) -> None:
    current_state = state if state is not None else change.before
    assert current_state is not None
    assignment = assignment_from_state(change.entity_id, current_state)
    person_id = UUID(str(current_state["person_id"]))
    meal_value = current_state["meal"]
    meal_text = "" if meal_value is None else str(meal_value)
    connection.execute(
        """
        DELETE FROM delivery_assignment_projection
        WHERE person_id = ? AND meal = ?
        """,
        (str(person_id), meal_text),
    )
    if change.operation is ChangeOperation.DELETE or assignment is None:
        return
    connection.execute(
        """
        INSERT INTO delivery_assignment_projection(
            person_id, meal, driver, sequence, last_revision_id
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            str(assignment.person_id),
            meal_text,
            assignment.driver,
            assignment.sequence,
            str(revision_id),
        ),
    )


def _apply_status_event(
    connection: apsw.Connection,
    revision_id: UUID,
    change: AuditChange,
    state: Mapping[str, JsonValue] | None,
) -> None:
    if change.operation is not ChangeOperation.INSERT or state is None:
        raise ProjectionContractError("원본 상태 사건은 append-only insert여야 합니다.")
    event = status_event_from_state(change.entity_id, state)
    if event.revision_id != revision_id:
        raise ProjectionContractError(
            "상태 사건 revision ID가 현재 revision과 다릅니다."
        )
    if connection.execute(
        "SELECT 1 FROM person_projection WHERE person_id = ?",
        (str(event.person_id),),
    ).fetchone() is None:
        raise ProjectionContractError("상태 사건의 사람이 현재 명단에 없습니다.")
    connection.execute(
        """
        INSERT INTO status_event_projection(
            event_id, created_revision_id, person_id, meal, status,
            starts_on, ends_on, recorded_at, source_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(event.event_id),
            str(event.revision_id),
            str(event.person_id),
            event.meal.value,
            event.status.value,
            _date_text(event.period.starts_on),
            _date_text(event.period.ends_on),
            event.recorded_at.isoformat(),
            event.source_text,
        ),
    )


def _load_persons(connection: apsw.Connection) -> Iterable[Person]:
    for row in connection.execute(
        """
        SELECT person_id, name, birth_date, school_type, school_name, grade,
               class_name, household_head_name, relationship_to_child,
               jurisdiction, provider, road_address, detail_address, telephone,
               mobile_phone, note, custom_values_json, delivery_settings_json
        FROM person_projection
        ORDER BY person_id
        """
    ):
        state = {
            "name": row[1],
            "birth_date": row[2],
            "school_type": row[3],
            "school_name": row[4],
            "grade": row[5],
            "class_name": row[6],
            "household_head_name": row[7],
            "relationship_to_child": row[8],
            "jurisdiction": row[9],
            "provider": row[10],
            "road_address": row[11],
            "detail_address": row[12],
            "telephone": row[13],
            "mobile_phone": row[14],
            "note": row[15],
            "custom_values": json.loads(str(row[16])),
            **({"delivery_settings": json.loads(str(row[17]))} if row[17] is not None else {}),
        }
        yield person_from_state(UUID(str(row[0])), state)


def _load_custom_fields(
    connection: apsw.Connection,
) -> Iterable[CustomFieldDefinition]:
    for row in connection.execute(
        """
        SELECT definition_id, name, field_type, display_order, choices_json
        FROM custom_field_definition_projection
        ORDER BY display_order, definition_id
        """
    ):
        yield custom_field_from_state(
            UUID(str(row[0])),
            {
                "name": row[1],
                "field_type": row[2],
                "display_order": row[3],
                "choices": json.loads(str(row[4])),
            },
        )


def _load_households(connection: apsw.Connection) -> Iterable[Household]:
    for row in connection.execute(
        "SELECT household_id, delivery_settings_json FROM household_projection ORDER BY household_id"
    ):
        settings = json.loads(str(row[1]))
        yield household_from_state(UUID(str(row[0])), {"delivery_settings": settings} if settings else {})


def _load_memberships(connection: apsw.Connection) -> Iterable[HouseholdMembership]:
    for row in connection.execute(
        """
        SELECT person_id, household_id, starts_on, ends_on
        FROM household_membership_projection
        ORDER BY person_id, ordinal
        """
    ):
        yield HouseholdMembership(
            UUID(str(row[0])),
            UUID(str(row[1])),
            InclusiveDateRange(_optional_date(row[2]), _optional_date(row[3])),
        )


def _load_enrollments(connection: apsw.Connection) -> Iterable[MealEnrollment]:
    for row in connection.execute(
        """
        SELECT person_id, meal, starts_on, ends_on
        FROM meal_enrollment_projection
        ORDER BY person_id, meal, ordinal
        """
    ):
        yield MealEnrollment(
            UUID(str(row[0])),
            Meal(str(row[1])),
            InclusiveDateRange(_optional_date(row[2]), _optional_date(row[3])),
        )


def _load_assignments(connection: apsw.Connection) -> Iterable[DeliveryAssignment]:
    for row in connection.execute(
        """
        SELECT person_id, meal, driver, sequence
        FROM delivery_assignment_projection
        ORDER BY person_id, meal
        """
    ):
        yield DeliveryAssignment(
            UUID(str(row[0])),
            None if row[1] == "" else Meal(str(row[1])),
            None if row[2] is None else str(row[2]),
            None if row[3] is None else str(row[3]),
        )


def _status_event_from_row(row) -> StatusEvent:
    state = {
        "revision_id": row[1],
        "person_id": row[2],
        "meal": row[3],
        "status": row[4],
        "starts_on": row[5],
        "ends_on": row[6],
        "recorded_at": row[7],
        "source_text": row[8],
    }
    return status_event_from_state(UUID(str(row[0])), state)


def _person_values(person: Person, revision_id: UUID) -> tuple:
    profile = person.profile
    custom_values = {
        str(definition_id): (
            {"kind": "date", "value": value.isoformat()}
            if isinstance(value, date)
            else {"kind": "scalar", "value": value}
        )
        for definition_id, value in profile.custom_values.items()
    }
    return (
        str(person.person_id),
        str(revision_id),
        profile.name,
        _date_text(profile.birth_date),
        profile.school_type,
        profile.school_name,
        profile.grade,
        profile.class_name,
        profile.household_head_name,
        profile.relationship_to_child,
        profile.jurisdiction,
        profile.provider,
        profile.road_address,
        profile.detail_address,
        profile.telephone,
        profile.mobile_phone,
        profile.note,
        json.dumps(
            custom_values,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        None if person.delivery_settings is None else json.dumps([
            {"meal": item.meal.value,
             "weekdays": None if item.weekdays is None else list(item.weekdays),
             "form": item.form,
             **({"delivery_weekdays": list(item.delivery_weekdays)} if item.delivery_weekdays is not None else {})}
            for item in person.delivery_settings
        ], ensure_ascii=False),
    )


def _delete_one(
    connection: apsw.Connection,
    table: str,
    id_column: str,
    entity_id: UUID,
) -> None:
    connection.execute(
        f"DELETE FROM {table} WHERE {id_column} = ?",
        (str(entity_id),),
    )
    _require_one_changed(connection, table, entity_id)


def _require_one_changed(
    connection: apsw.Connection,
    entity_kind: str,
    entity_id: UUID,
) -> None:
    if connection.changes() != 1:
        raise ProjectionContractError(
            f"typed projection 대상이 하나가 아닙니다: {entity_kind}/{entity_id}"
        )


def _plain_states(
    states: Mapping[EntityRef, Mapping[str, JsonValue]],
) -> dict[EntityRef, object]:
    return {reference: plain_json(state) for reference, state in states.items()}


def _date_text(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _optional_date(value: object) -> date | None:
    return None if value is None else date.fromisoformat(str(value))


def _delete_order(entity_kind: str) -> int:
    return {
        STATUS_EVENT_KIND: 0,
        ASSIGNMENT_KIND: 1,
        ENROLLMENT_KIND: 2,
        MEMBERSHIP_KIND: 3,
        PERSON_KIND: 4,
        CUSTOM_FIELD_KIND: 5,
        HOUSEHOLD_KIND: 6,
    }[entity_kind]


def _write_order(entity_kind: str) -> int:
    return {
        HOUSEHOLD_KIND: 0,
        CUSTOM_FIELD_KIND: 1,
        PERSON_KIND: 2,
        MEMBERSHIP_KIND: 3,
        ENROLLMENT_KIND: 4,
        ASSIGNMENT_KIND: 5,
        STATUS_EVENT_KIND: 6,
    }[entity_kind]
