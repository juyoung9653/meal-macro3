"""Review ordinary roster imports before replacing existing values."""
from dataclasses import replace
from uuid import UUID

from src.domain.roster import CustomFieldType, DeliveryAssignment, Meal
from .lunch_values import LunchValueRow, lunch_value_rows


def import_value_rows(current, result, plan):
    rows = []
    for row in lunch_value_rows(current, result, plan, include_assignments=False):
        # Empty source cells preserve existing values; filling an empty field
        # does not replace a conflicting value.
        if not row.automatic and (row.before is None or row.incoming is None):
            row = replace(row, automatic=True)
        rows.append(row)
    existing = {person.person_id: person for person in current.persons}
    old_assignments = {(a.person_id, a.meal): a for a in current.assignments}
    for staged in plan.staged_people:
        obs = staged.observation
        person_id = result.person_ids_by_observation.get(obs.observation_id)
        if person_id not in existing:
            continue
        person = existing[person_id]
        source = f"{obs.source.sheet_name} {', '.join(map(str, obs.source.row_numbers))}행"
        for assignment in obs.assignments:
            old = old_assignments.get((person_id, assignment.meal))
            scope = "common" if assignment.meal is None else assignment.meal.value
            label = "공통" if assignment.meal is None else assignment.meal.value
            for field, title in (("driver", "배송담당"), ("sequence", "배송순서")):
                before = getattr(old, field, None)
                incoming = getattr(assignment, field)
                if assignment.meal is not None and incoming is not None:
                    before = getattr(current.resolve_assignment(person_id, assignment.meal), field)
                if before == incoming:
                    continue
                rows.append(LunchValueRow(
                    person_id, person.profile.name, person.profile.mobile_phone or "핸드폰 없음", source,
                    f"assignment:{scope}:{field}", f"{label} {title}", before, incoming,
                    before if incoming is None else incoming, before is None or incoming is None,
                ))
        old_meals = set(current.meals_for(person_id, on=plan.effective_on))
        new_meals = set(result.snapshot.meals_for(person_id, on=plan.effective_on))
        for meal in Meal:
            if (meal in old_meals) == (meal in new_meals):
                continue
            before = "가입" if meal in old_meals else "미가입"
            incoming = "가입" if meal in new_meals else "미가입"
            rows.append(LunchValueRow(
                person_id, person.profile.name, person.profile.mobile_phone or "핸드폰 없음", source,
                "meal:" + meal.value, meal.value + " 가입", before, incoming, incoming,
                automatic=not old_meals, field_type=CustomFieldType.CHOICE, choices=("가입", "미가입"),
            ))
    return tuple(rows)


def apply_import_values(current, result, plan, choices):
    rows = import_value_rows(current, result, plan)
    supplied = dict(choices or {})
    if set(supplied) != {row.key for row in rows if not row.automatic}:
        raise ValueError("기존 값과 다른 항목을 모두 선택한 뒤 가져와주세요.")
    persons = {person.person_id: person for person in result.snapshot.persons}
    assignments = {(a.person_id, a.meal): {"driver": a.driver, "sequence": a.sequence}
                   for a in result.snapshot.assignments}
    enrollments = list(result.snapshot.enrollments)
    definitions = {d.definition_id: d for d in result.snapshot.custom_fields}
    for row in rows:
        value = row.proposed if row.automatic else supplied[row.key]
        person = persons[row.person_id]
        if row.field.startswith("assignment:"):
            _, scope, field = row.field.split(":")
            meal = None if scope == "common" else Meal(scope)
            assignment = assignments.setdefault((row.person_id, meal), {"driver": None, "sequence": None})
            if not row.automatic and value is None and meal is not None and assignments.get((row.person_id, None), {}).get(field) is not None:
                raise ValueError(f"{row.name}의 {row.label}은 공통 배정을 사용하므로 비울 수 없습니다.")
            assignment[field] = value
            continue
        if row.field.startswith("meal:"):
            if value not in row.choices:
                raise ValueError("식종 가입 여부를 선택해주세요.")
            if value == row.before:
                meal = Meal(row.field.split(":", 1)[1])
                enrollments = [e for e in enrollments if (e.person_id, e.meal) != (row.person_id, meal)]
                enrollments.extend(e for e in current.enrollments if (e.person_id, e.meal) == (row.person_id, meal))
            continue
        if row.field.startswith("custom:"):
            key = UUID(row.field[7:])
            definitions[key].validate_value(value)
            values = dict(person.profile.custom_values)
            values[key] = value
            profile = replace(person.profile, custom_values=values)
        else:
            profile = replace(person.profile, **{row.field: value})
        persons[row.person_id] = replace(person, profile=profile)
    snapshot = replace(result.snapshot, persons=tuple(persons.values()), enrollments=tuple(enrollments),
                       assignments=tuple(DeliveryAssignment(person_id, meal, **values)
                                         for (person_id, meal), values in assignments.items()
                                         if any(value is not None for value in values.values())))
    return replace(result, snapshot=snapshot)
