"""Canonical roster Module, independent of any Excel layout."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from unicodedata import normalize
from uuid import UUID

type CustomFieldValue = str | int | float | bool | date | None


class Meal(StrEnum):
    BREAKFAST = "조식"
    LUNCH = "중식"
    DINNER = "석식"


_MEAL_ORDER = (Meal.BREAKFAST, Meal.LUNCH, Meal.DINNER)


class CustomFieldType(StrEnum):
    TEXT = "text"
    NUMBER = "number"
    DATE = "date"
    CHOICE = "choice"


@dataclass(frozen=True, slots=True)
class CustomFieldDefinition:
    definition_id: UUID
    name: str
    field_type: CustomFieldType
    display_order: int
    choices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_uuid(self.definition_id, "사용자 정의 필드 ID")
        object.__setattr__(self, "name", _required_text(self.name, "사용자 정의 필드명"))
        if not isinstance(self.field_type, CustomFieldType):
            raise TypeError("field_type은 CustomFieldType이어야 합니다.")
        if isinstance(self.display_order, bool) or not isinstance(self.display_order, int):
            raise TypeError("표시 순서는 정수여야 합니다.")
        if self.display_order < 0:
            raise ValueError("표시 순서는 음수일 수 없습니다.")
        choices = tuple(_required_text(choice, "선택값") for choice in self.choices)
        if len(set(choices)) != len(choices):
            raise ValueError("사용자 정의 필드 선택값은 중복될 수 없습니다.")
        if self.field_type is CustomFieldType.CHOICE and not choices:
            raise ValueError("선택형 사용자 정의 필드에는 선택값이 필요합니다.")
        if self.field_type is not CustomFieldType.CHOICE and choices:
            raise ValueError("선택형이 아닌 사용자 정의 필드에는 선택값을 둘 수 없습니다.")
        object.__setattr__(self, "choices", choices)

    def validate_value(self, value: CustomFieldValue) -> None:
        _validate_custom_value(value)
        if value is None:
            return
        if self.field_type is CustomFieldType.TEXT and not isinstance(value, str):
            raise TypeError(f"{self.name} 값은 문자열이어야 합니다.")
        if self.field_type is CustomFieldType.NUMBER and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise TypeError(f"{self.name} 값은 숫자여야 합니다.")
        if self.field_type is CustomFieldType.DATE and (
            isinstance(value, datetime) or not isinstance(value, date)
        ):
            raise TypeError(f"{self.name} 값은 날짜여야 합니다.")
        if self.field_type is CustomFieldType.CHOICE:
            if not isinstance(value, str) or value not in self.choices:
                raise ValueError(f"{self.name} 값이 선택 목록에 없습니다: {value!r}")


@dataclass(frozen=True, slots=True)
class InclusiveDateRange:
    """An inclusive range whose start, end, or both may be open."""

    starts_on: date | None = None
    ends_on: date | None = None

    def __post_init__(self) -> None:
        _validate_optional_date(self.starts_on, "시작일")
        _validate_optional_date(self.ends_on, "종료일")
        if (
            self.starts_on is not None
            and self.ends_on is not None
            and self.starts_on > self.ends_on
        ):
            raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")

    def contains(self, day: date) -> bool:
        _validate_date(day, "조회일")
        return (self.starts_on is None or self.starts_on <= day) and (
            self.ends_on is None or day <= self.ends_on
        )

    def overlaps(self, other: InclusiveDateRange) -> bool:
        if not isinstance(other, InclusiveDateRange):
            raise TypeError("비교 대상은 InclusiveDateRange여야 합니다.")
        latest_start = max(
            self.starts_on or date.min,
            other.starts_on or date.min,
        )
        earliest_end = min(
            self.ends_on or date.max,
            other.ends_on or date.max,
        )
        return latest_start <= earliest_end


@dataclass(frozen=True, slots=True)
class PersonProfile:
    """Operational fields retained for one person in a committed revision."""

    name: str
    birth_date: date | None = None
    school_type: str | None = None
    school_name: str | None = None
    grade: str | None = None
    class_name: str | None = None
    household_head_name: str | None = None
    relationship_to_child: str | None = None
    jurisdiction: str | None = None
    provider: str | None = None
    road_address: str | None = None
    detail_address: str | None = None
    telephone: str | None = None
    mobile_phone: str | None = None
    note: str | None = None
    custom_values: Mapping[UUID, CustomFieldValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _required_text(self.name, "성명"))
        _validate_optional_date(self.birth_date, "생년월일")
        for attribute, label in (
            ("school_type", "취학유형"),
            ("school_name", "학교명"),
            ("grade", "학년"),
            ("class_name", "반"),
            ("household_head_name", "세대주 성명"),
            ("relationship_to_child", "아동과의 관계"),
            ("jurisdiction", "관할동"),
            ("provider", "제공기관"),
            ("road_address", "도로명주소"),
            ("detail_address", "상세주소"),
            ("telephone", "전화번호"),
            ("mobile_phone", "핸드폰"),
            ("note", "비고"),
        ):
            object.__setattr__(
                self,
                attribute,
                _optional_text(getattr(self, attribute), label),
            )

        custom_values = dict(self.custom_values)
        for definition_id, value in custom_values.items():
            _validate_uuid(definition_id, "사용자 정의 필드 ID")
            _validate_custom_value(value)
        object.__setattr__(self, "custom_values", MappingProxyType(custom_values))

    @property
    def household_phone_key(self) -> str:
        """Phone key used only as a candidate when assigning a household."""

        return normalize_phone(self.mobile_phone)


@dataclass(frozen=True, slots=True)
class Person:
    person_id: UUID
    profile: PersonProfile

    def __post_init__(self) -> None:
        _validate_uuid(self.person_id, "사람 ID")
        if not isinstance(self.profile, PersonProfile):
            raise TypeError("profile은 PersonProfile이어야 합니다.")


@dataclass(frozen=True, slots=True)
class Household:
    household_id: UUID

    def __post_init__(self) -> None:
        _validate_uuid(self.household_id, "가구 ID")


@dataclass(frozen=True, slots=True)
class HouseholdMembership:
    person_id: UUID
    household_id: UUID
    period: InclusiveDateRange = field(default_factory=InclusiveDateRange)

    def __post_init__(self) -> None:
        _validate_uuid(self.person_id, "사람 ID")
        _validate_uuid(self.household_id, "가구 ID")
        if not isinstance(self.period, InclusiveDateRange):
            raise TypeError("가구 소속 기간은 InclusiveDateRange여야 합니다.")


@dataclass(frozen=True, slots=True)
class MealEnrollment:
    person_id: UUID
    meal: Meal
    period: InclusiveDateRange = field(default_factory=InclusiveDateRange)

    def __post_init__(self) -> None:
        _validate_uuid(self.person_id, "사람 ID")
        if not isinstance(self.meal, Meal):
            raise TypeError("meal은 Meal이어야 합니다.")
        if not isinstance(self.period, InclusiveDateRange):
            raise TypeError("식종 가입 기간은 InclusiveDateRange여야 합니다.")


@dataclass(frozen=True, slots=True)
class DeliveryAssignment:
    """A person-wide assignment or a meal-specific override."""

    person_id: UUID
    meal: Meal | None = None
    driver: str | None = None
    sequence: str | None = None

    def __post_init__(self) -> None:
        _validate_uuid(self.person_id, "사람 ID")
        if self.meal is not None and not isinstance(self.meal, Meal):
            raise TypeError("meal은 Meal 또는 None이어야 합니다.")
        object.__setattr__(self, "driver", _optional_text(self.driver, "배송담당"))
        object.__setattr__(
            self,
            "sequence",
            _optional_text(self.sequence, "배송순서", collapse=False),
        )
        if self.driver is None and self.sequence is None:
            raise ValueError("배송담당이나 배송순서 중 하나는 지정해야 합니다.")


@dataclass(frozen=True, slots=True)
class ResolvedDeliveryAssignment:
    driver: str | None
    sequence: str | None


@dataclass(frozen=True, slots=True)
class RosterSnapshot:
    """Validated roster state at one immutable revision."""

    revision_id: UUID
    persons: tuple[Person, ...] = ()
    households: tuple[Household, ...] = ()
    memberships: tuple[HouseholdMembership, ...] = ()
    enrollments: tuple[MealEnrollment, ...] = ()
    assignments: tuple[DeliveryAssignment, ...] = ()
    custom_fields: tuple[CustomFieldDefinition, ...] = ()
    _persons_by_id: Mapping[UUID, Person] = field(init=False, repr=False)
    _households_by_id: Mapping[UUID, Household] = field(init=False, repr=False)
    _assignments_by_key: Mapping[
        tuple[UUID, Meal | None], DeliveryAssignment
    ] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _validate_uuid(self.revision_id, "revision ID")
        persons = tuple(self.persons)
        households = tuple(self.households)
        memberships = tuple(self.memberships)
        enrollments = tuple(self.enrollments)
        assignments = tuple(self.assignments)
        custom_fields = tuple(self.custom_fields)

        definitions_by_id = _unique_by_id(
            custom_fields,
            CustomFieldDefinition,
            lambda item: item.definition_id,
            "사용자 정의 필드 ID",
        )
        normalized_names = [definition.name.casefold() for definition in custom_fields]
        if len(set(normalized_names)) != len(normalized_names):
            raise ValueError("사용자 정의 필드명은 중복될 수 없습니다.")
        display_orders = [definition.display_order for definition in custom_fields]
        if len(set(display_orders)) != len(display_orders):
            raise ValueError("사용자 정의 필드 표시 순서는 중복될 수 없습니다.")

        persons_by_id = _unique_by_id(
            persons,
            Person,
            lambda item: item.person_id,
            "사람 ID",
        )
        households_by_id = _unique_by_id(
            households,
            Household,
            lambda item: item.household_id,
            "가구 ID",
        )

        for person in persons:
            for definition_id, value in person.profile.custom_values.items():
                definition = definitions_by_id.get(definition_id)
                if definition is None:
                    raise ValueError(
                        f"정의되지 않은 사용자 정의 필드 값입니다: {definition_id}"
                    )
                definition.validate_value(value)

        for membership in memberships:
            if not isinstance(membership, HouseholdMembership):
                raise TypeError("memberships에는 HouseholdMembership만 허용됩니다.")
            _require_reference(membership.person_id, persons_by_id, "사람")
            _require_reference(membership.household_id, households_by_id, "가구")
        _reject_overlapping_periods(
            memberships,
            lambda item: item.person_id,
            "한 사람의 가구 소속 기간",
        )

        for enrollment in enrollments:
            if not isinstance(enrollment, MealEnrollment):
                raise TypeError("enrollments에는 MealEnrollment만 허용됩니다.")
            _require_reference(enrollment.person_id, persons_by_id, "사람")
        _reject_overlapping_periods(
            enrollments,
            lambda item: (item.person_id, item.meal),
            "한 사람·식종의 가입 기간",
        )

        assignments_by_key: dict[tuple[UUID, Meal | None], DeliveryAssignment] = {}
        for assignment in assignments:
            if not isinstance(assignment, DeliveryAssignment):
                raise TypeError("assignments에는 DeliveryAssignment만 허용됩니다.")
            _require_reference(assignment.person_id, persons_by_id, "사람")
            key = (assignment.person_id, assignment.meal)
            if key in assignments_by_key:
                raise ValueError("같은 사람·식종의 배송 배정이 중복되었습니다.")
            assignments_by_key[key] = assignment

        object.__setattr__(self, "persons", persons)
        object.__setattr__(self, "households", households)
        object.__setattr__(self, "memberships", memberships)
        object.__setattr__(self, "enrollments", enrollments)
        object.__setattr__(self, "assignments", assignments)
        object.__setattr__(self, "custom_fields", custom_fields)
        object.__setattr__(self, "_persons_by_id", MappingProxyType(persons_by_id))
        object.__setattr__(
            self,
            "_households_by_id",
            MappingProxyType(households_by_id),
        )
        object.__setattr__(
            self,
            "_assignments_by_key",
            MappingProxyType(assignments_by_key),
        )

    def person(self, person_id: UUID) -> Person:
        try:
            return self._persons_by_id[person_id]
        except KeyError as error:
            raise KeyError(f"snapshot에 없는 사람 ID입니다: {person_id}") from error

    def household_members(
        self,
        household_id: UUID,
        *,
        on: date,
    ) -> tuple[Person, ...]:
        _require_reference(household_id, self._households_by_id, "가구")
        _validate_date(on, "조회일")
        member_ids = {
            membership.person_id
            for membership in self.memberships
            if membership.household_id == household_id
            and membership.period.contains(on)
        }
        return tuple(
            person for person in self.persons if person.person_id in member_ids
        )

    def meals_for(self, person_id: UUID, *, on: date) -> tuple[Meal, ...]:
        self.person(person_id)
        _validate_date(on, "조회일")
        active = {
            enrollment.meal
            for enrollment in self.enrollments
            if enrollment.person_id == person_id and enrollment.period.contains(on)
        }
        return tuple(meal for meal in _MEAL_ORDER if meal in active)

    def resolve_assignment(
        self,
        person_id: UUID,
        meal: Meal,
    ) -> ResolvedDeliveryAssignment:
        self.person(person_id)
        if not isinstance(meal, Meal):
            raise TypeError("meal은 Meal이어야 합니다.")
        common = self._assignments_by_key.get((person_id, None))
        override = self._assignments_by_key.get((person_id, meal))
        return ResolvedDeliveryAssignment(
            driver=(
                override.driver
                if override is not None and override.driver is not None
                else common.driver if common is not None else None
            ),
            sequence=(
                override.sequence
                if override is not None and override.sequence is not None
                else common.sequence if common is not None else None
            ),
        )


def normalize_phone(value: str | None) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError("핸드폰은 문자열 또는 None이어야 합니다.")
    normalized = normalize("NFKC", value)
    return "".join(character for character in normalized if character in "0123456789")


def _validate_custom_value(value: object) -> None:
    if isinstance(value, datetime):
        raise TypeError("사용자 정의 필드 날짜는 datetime이 아닌 date여야 합니다.")
    if value is not None and not isinstance(value, (str, int, float, bool, date)):
        raise TypeError("사용자 정의 필드 값은 JSON scalar 또는 date여야 합니다.")
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("사용자 정의 필드 숫자는 유한해야 합니다.")


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label}은 문자열이어야 합니다.")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{label}은 비어 있을 수 없습니다.")
    return normalized


def _optional_text(
    value: object,
    label: str,
    *,
    collapse: bool = True,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{label}은 문자열 또는 None이어야 합니다.")
    normalized = " ".join(value.split()) if collapse else value.strip()
    return normalized or None


def _validate_date(value: object, label: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label}은 date여야 합니다.")


def _validate_optional_date(value: object, label: str) -> None:
    if value is not None:
        _validate_date(value, label)


def _validate_uuid(value: object, label: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{label}는 UUID여야 합니다.")


def _unique_by_id(items, item_type, id_of, label: str) -> dict[UUID, object]:
    result: dict[UUID, object] = {}
    for item in items:
        if not isinstance(item, item_type):
            raise TypeError(f"{label} 목록의 형식이 올바르지 않습니다.")
        item_id = id_of(item)
        if item_id in result:
            raise ValueError(f"{label}가 중복되었습니다: {item_id}")
        result[item_id] = item
    return result


def _require_reference(reference_id: UUID, index: Mapping[UUID, object], label: str) -> None:
    if reference_id not in index:
        raise ValueError(f"존재하지 않는 {label} ID를 참조합니다: {reference_id}")


def _reject_overlapping_periods(items, key_of, label: str) -> None:
    grouped: dict[object, list[InclusiveDateRange]] = {}
    for item in items:
        grouped.setdefault(key_of(item), []).append(item.period)
    for periods in grouped.values():
        for index, period in enumerate(periods):
            if any(period.overlaps(other) for other in periods[index + 1 :]):
                raise ValueError(f"{label}은 서로 겹칠 수 없습니다.")
