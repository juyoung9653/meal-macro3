"""School progression and explicit value review for vacation lunch imports."""
from dataclasses import dataclass, replace
from datetime import date
import re
from uuid import UUID

from src.domain.roster import CustomFieldType, Meal, DeliveryAssignment, normalize_phone
from .contracts import ProfileField

SCHOOL_FIELDS = ("school_type", "school_name", "grade")
FIELD_LABELS = {
    "name": "성명", "birth_date": "생년월일", "school_type": "학교 유형",
    "school_name": "학교", "grade": "학년", "class_name": "반",
    "household_head_name": "세대주 성명", "relationship_to_child": "아동과의 관계",
    "jurisdiction": "관할동", "provider": "제공기관", "road_address": "도로명주소",
    "detail_address": "상세주소", "telephone": "전화번호", "mobile_phone": "핸드폰",
    "note": "비고", "driver": "중식 배송담당", "sequence": "중식 배송순서",
}


def school_stage(school_type, school_name):
    def stage(text):
        text = re.sub(r"\s+", "", text or "")
        for words, rank in ((('대학교', '대학'), 5), (('고등',), 4),
                            (('중학교', '중등'), 3), (('초등',), 2),
                            (('유치', '어린이'), 1), (('미취학',), 0)):
            if any(word in text for word in words):
                return rank
        return {"초": 2, "중": 3, "고": 4}.get(text)
    # A full school name is stronger evidence than an old type label.
    named = stage(school_name)
    return named if named is not None else stage(school_type)


def grade_rank(value):
    text = re.sub(r"\s+", "", value or "")
    match = re.fullmatch(r"([1-9])(?:학년)?", text)
    return int(match[1]) if match else None


def preferred_school(before, incoming):
    """Return old/new, or None when the pair needs a person's decision."""
    old_stage = school_stage(before.school_type, before.school_name)
    new_stage = school_stage(incoming.school_type, incoming.school_name)
    type_stage = school_stage(incoming.school_type, None)
    named_stage = school_stage(None, incoming.school_name)
    if type_stage is not None and named_stage is not None and type_stage != named_stage:
        return None
    if old_stage is None or new_stage is None:
        return None
    if incoming.grade and grade_rank(incoming.grade) is None:
        return None
    if old_stage != new_stage:
        return "new" if new_stage > old_stage else "old"
    if before.school_name and incoming.school_name and (
        re.sub(r"\s+", "", before.school_name) != re.sub(r"\s+", "", incoming.school_name)
    ):
        return None
    old_grade, new_grade = grade_rank(before.grade), grade_rank(incoming.grade)
    if old_grade is None or new_grade is None:
        return None
    return "new" if new_grade > old_grade else "old"


@dataclass(frozen=True)
class LunchValueRow:
    person_id: UUID
    name: str
    phone: str
    source: str
    field: str
    label: str
    before: object
    incoming: object
    proposed: object
    automatic: bool = False
    field_type: CustomFieldType = CustomFieldType.TEXT
    choices: tuple[str, ...] = ()

    @property
    def key(self):
        return self.person_id, self.field

    def parse(self, text):
        text = text.strip()
        if not text:
            if self.field == "name":
                raise ValueError("성명은 비울 수 없습니다.")
            return None
        if self.field_type is CustomFieldType.DATE:
            return date.fromisoformat(text)
        if self.field_type is CustomFieldType.NUMBER:
            value = float(text)
            from math import isfinite
            if not isfinite(value):
                raise ValueError("유한한 숫자를 입력해주세요.")
            return int(value) if value.is_integer() else value
        if self.field_type is CustomFieldType.CHOICE and text not in self.choices:
            raise ValueError("다음 값 중에서 선택해주세요: " + ", ".join(self.choices))
        return text


def lunch_value_rows(current, result, plan):
    rows = []
    existing = {p.person_id: p for p in current.persons}
    definitions = {d.definition_id: d for d in result.snapshot.custom_fields}
    for staged in plan.staged_people:
        obs = staged.observation
        person_id = result.person_ids_by_observation.get(obs.observation_id)
        if person_id not in existing:
            continue
        old = existing[person_id].profile
        merged = result.snapshot.person(person_id).profile
        raw = obs.merged_profile(None)
        if not any(field in obs.profile_values for field in (ProfileField.SCHOOL_NAME, ProfileField.SCHOOL_TYPE)):
            raw = replace(raw, school_name=old.school_name, school_type=old.school_type)
        school_choice = preferred_school(old, raw)
        source = f"{obs.source.sheet_name} {', '.join(map(str, obs.source.row_numbers))}행"

        def add(field, before, incoming, proposed, automatic=False, definition=None):
            if before == incoming and before == proposed:
                return
            rows.append(LunchValueRow(
                person_id, old.name, old.mobile_phone or "핸드폰 없음", source,
                field, definition.name if definition else FIELD_LABELS[field],
                before, incoming, proposed, automatic,
                definition.field_type if definition else (
                    CustomFieldType.DATE if field == "birth_date" else CustomFieldType.TEXT
                ), definition.choices if definition else (),
            ))

        for field in ProfileField:
            key = field.value
            if field not in obs.profile_values:
                continue
            before, incoming = getattr(old, key), obs.profile_values[field]
            if key in ("mobile_phone", "telephone") and normalize_phone(before) == normalize_phone(incoming):
                continue
            proposed = getattr(merged, key)
            automatic = key in SCHOOL_FIELDS and school_choice is not None
            if automatic:
                proposed = before if school_choice == "old" else incoming
                if incoming is None and school_stage(old.school_type, old.school_name) == school_stage(raw.school_type, raw.school_name):
                    proposed = before
            add(key, before, incoming, proposed, automatic)
        # Move school and grade as a pair; do not retain an elementary grade
        # when the incoming school is secondary but the grade is absent.
        if school_choice == "new" and school_stage(old.school_type, old.school_name) != school_stage(raw.school_type, raw.school_name):
            for key in SCHOOL_FIELDS:
                if ProfileField(key) not in obs.profile_values:
                    add(key, getattr(old, key), None, getattr(raw, key), True)
        for definition_id in set(merged.custom_values) | set(obs.custom_values):
            before = old.custom_values.get(definition_id)
            incoming = obs.custom_values.get(definition_id, merged.custom_values.get(definition_id))
            add("custom:" + str(definition_id), before, incoming,
                merged.custom_values.get(definition_id), definition=definitions[definition_id])
        old_assignment = current.resolve_assignment(person_id, Meal.LUNCH)
        observed = {assignment.meal: assignment for assignment in obs.assignments}
        for key in ("driver", "sequence"):
            incoming = next((
                getattr(observed[meal], key) for meal in (Meal.LUNCH, None)
                if meal in observed and getattr(observed[meal], key) is not None
            ), getattr(old_assignment, key))
            add(key, getattr(old_assignment, key), incoming, incoming)
    return tuple(rows)


def apply_lunch_values(current, result, plan, choices):
    rows = lunch_value_rows(current, result, plan)
    manual = {row.key for row in rows if not row.automatic}
    supplied = dict(choices or {})
    if set(supplied) != manual:
        raise ValueError("중식 명단의 변경 값을 모두 확인한 뒤 가져와주세요.")
    existing = {p.person_id: p for p in current.persons}
    persons = {p.person_id: existing.get(p.person_id, p) for p in result.snapshot.persons}
    assignments = {(a.person_id, a.meal): a for a in current.assignments}
    assignment_values = {}
    for row in rows:
        value = row.proposed if row.automatic else supplied[row.key]
        person = persons[row.person_id]
        if row.field.startswith("custom:"):
            values = dict(person.profile.custom_values)
            values[UUID(row.field[7:])] = value
            profile = replace(person.profile, custom_values=values)
        elif row.field in ("driver", "sequence"):
            values = assignment_values.setdefault(row.person_id, {
                "driver": current.resolve_assignment(row.person_id, Meal.LUNCH).driver,
                "sequence": current.resolve_assignment(row.person_id, Meal.LUNCH).sequence,
            })
            # Empty meal-specific values inherit a common assignment in this
            # domain model, so never silently promise to erase inherited data.
            common = assignments.get((row.person_id, None))
            if value is None and common is not None and getattr(common, row.field) is not None:
                raise ValueError(f"{row.name}의 {row.label}은 이 화면에서 비울 수 없습니다. 배송 배정에서 수정해주세요.")
            values[row.field] = value
            continue
        else:
            profile = replace(person.profile, **{row.field: value})
        persons[row.person_id] = replace(person, profile=profile)
    for person_id, values in assignment_values.items():
        if all(value is None for value in values.values()):
            assignments.pop((person_id, Meal.LUNCH), None)
        else:
            assignments[person_id, Meal.LUNCH] = DeliveryAssignment(person_id, Meal.LUNCH, **values)
    return tuple(persons.values()), tuple(assignments.values())
