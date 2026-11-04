"""Reviewed school-lunch calendar facts stored in the revision ledger.

The school workbook is only an input adapter.  Billing consumes these typed,
fully-covered weekday facts so an unreviewed note can never silently affect an
invoice.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from types import MappingProxyType
from uuid import UUID, uuid5

from .entitlements import LunchDayRule, SchoolLunchMark
from .revisions import HistoricalSnapshot, JsonValue, plain_json
from .roster import InclusiveDateRange, PersonProfile


SCHOOL_LUNCH_CALENDAR_POLICY_VERSION = 1
SCHOOL_LUNCH_SCHEDULE_KIND = "school-lunch.schedule"
_SCHOOL_SCHEDULE_NAMESPACE = UUID("7358f3f4-27d6-4f69-a04c-2486923fd9cb")
_GRADE_NUMBER = re.compile(r"^(\d+)\s*(?:학년)?$")


class SchoolLunchCalendarError(ValueError):
    """Base error for invalid or incomplete reviewed school schedules."""


class SchoolLunchScheduleMissingError(SchoolLunchCalendarError):
    def __init__(self, school_name: str | None, service_on: date) -> None:
        school = "학교명 없음" if school_name is None else school_name
        super().__init__(f"{school}의 {service_on:%Y.%m.%d} 중식 일정이 없습니다.")
        self.school_name = school_name
        self.service_on = service_on


@dataclass(frozen=True, slots=True)
class SchoolLunchDay:
    service_on: date
    rule: LunchDayRule

    def __post_init__(self) -> None:
        _validate_date(self.service_on, "중식 일정일")
        if self.service_on.weekday() >= 5:
            raise ValueError("학교 중식 일정에는 평일만 저장할 수 있습니다.")
        if not isinstance(self.rule, LunchDayRule):
            raise TypeError("rule은 LunchDayRule이어야 합니다.")


@dataclass(frozen=True, slots=True)
class SchoolLunchSchoolSchedule:
    """All reviewed weekday facts for one school over one or more periods."""

    school_name: str
    covered_periods: tuple[InclusiveDateRange, ...]
    days: tuple[SchoolLunchDay, ...]

    def __post_init__(self) -> None:
        name = _required_text(self.school_name, "학교명")
        periods = tuple(self.covered_periods)
        days = tuple(self.days)
        if not periods:
            raise ValueError("학교 일정에는 검토 완료 기간이 하나 이상 필요합니다.")
        for period in periods:
            if not isinstance(period, InclusiveDateRange):
                raise TypeError("검토 완료 기간은 InclusiveDateRange여야 합니다.")
            if period.starts_on is None or period.ends_on is None:
                raise ValueError("학교 일정의 검토 완료 기간은 시작일과 종료일이 필요합니다.")
        ordered_periods = tuple(
            sorted(periods, key=lambda item: (item.starts_on, item.ends_on))
        )
        for previous, current in zip(ordered_periods, ordered_periods[1:]):
            assert previous.ends_on is not None and current.starts_on is not None
            if current.starts_on <= previous.ends_on:
                raise ValueError("학교 일정의 검토 완료 기간은 겹칠 수 없습니다.")

        ordered_days = tuple(sorted(days, key=lambda item: item.service_on))
        if any(not isinstance(item, SchoolLunchDay) for item in ordered_days):
            raise TypeError("학교 날짜 목록에는 SchoolLunchDay만 허용됩니다.")
        ordered_days = tuple(
            SchoolLunchDay(
                item.service_on,
                LunchDayRule(
                    item.rule.mark,
                    frozenset(
                        normalize_grade(grade)
                        for grade in item.rule.grades_without_school_meal
                    ),
                ),
            )
            for item in ordered_days
        )
        day_index = {item.service_on: item for item in ordered_days}
        if len(day_index) != len(ordered_days):
            raise ValueError("학교 일정 날짜가 중복되었습니다.")
        expected_days = {
            day
            for period in ordered_periods
            for day in _weekdays(period.starts_on, period.ends_on)
        }
        actual_days = set(day_index)
        if actual_days != expected_days:
            missing = sorted(expected_days - actual_days)
            extra = sorted(actual_days - expected_days)
            details = []
            if missing:
                details.append(f"누락 {missing[0]:%Y.%m.%d}")
            if extra:
                details.append(f"기간 밖 {extra[0]:%Y.%m.%d}")
            raise ValueError("학교 일정의 평일 범위가 완전하지 않습니다: " + ", ".join(details))

        object.__setattr__(self, "school_name", name)
        object.__setattr__(self, "covered_periods", ordered_periods)
        object.__setattr__(self, "days", ordered_days)

    @property
    def normalized_school_name(self) -> str:
        return normalize_school_name(self.school_name)

    @property
    def entity_id(self) -> UUID:
        return school_lunch_schedule_entity_id(self.school_name)

    def covers(self, service_on: date) -> bool:
        _validate_date(service_on, "중식 일정 조회일")
        return any(period.contains(service_on) for period in self.covered_periods)

    def rule_on(self, service_on: date) -> LunchDayRule:
        _validate_date(service_on, "중식 일정 조회일")
        if service_on.weekday() >= 5:
            raise ValueError("주말은 학교 중식 일정 조회 대상이 아닙니다.")
        if not self.covers(service_on):
            raise SchoolLunchScheduleMissingError(self.school_name, service_on)
        for item in self.days:
            if item.service_on == service_on:
                return item.rule
        raise SchoolLunchCalendarError(
            f"{self.school_name}의 {service_on:%Y.%m.%d} 검토 완료 일정이 비어 있습니다."
        )

    def manifest(self) -> Mapping[str, JsonValue]:
        state = school_lunch_school_schedule_state(self)
        encoded = json.dumps(
            plain_json(state),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return MappingProxyType(
            {
                "entity_id": str(self.entity_id),
                "school_name": self.school_name,
                "covered_periods": len(self.covered_periods),
                "weekday_facts": len(self.days),
                "content_sha256": sha256(encoded).hexdigest(),
            }
        )


@dataclass(frozen=True, slots=True)
class SchoolLunchCalendar:
    schedules: tuple[SchoolLunchSchoolSchedule, ...] = ()

    def __post_init__(self) -> None:
        schedules = tuple(self.schedules)
        if any(not isinstance(item, SchoolLunchSchoolSchedule) for item in schedules):
            raise TypeError("학교 일정에는 SchoolLunchSchoolSchedule만 허용됩니다.")
        keys = tuple(item.normalized_school_name for item in schedules)
        if len(set(keys)) != len(keys):
            raise ValueError("같은 학교의 일정이 둘 이상 저장되었습니다.")
        object.__setattr__(
            self,
            "schedules",
            tuple(sorted(schedules, key=lambda item: item.normalized_school_name)),
        )

    def schedule_for(self, school_name: str | None) -> SchoolLunchSchoolSchedule:
        if school_name is None or not str(school_name).strip():
            raise SchoolLunchScheduleMissingError(None, date.today())
        key = normalize_school_name(school_name)
        for schedule in self.schedules:
            if schedule.normalized_school_name == key:
                return schedule
        raise KeyError(str(school_name).strip())

    def rule_for(self, profile: PersonProfile, service_on: date) -> LunchDayRule:
        if not isinstance(profile, PersonProfile):
            raise TypeError("profile은 PersonProfile이어야 합니다.")
        _validate_date(service_on, "중식 일정 조회일")
        if service_on.weekday() >= 5:
            raise ValueError("주말은 학교 중식 일정 조회 대상이 아닙니다.")
        if profile.school_name is None:
            raise SchoolLunchScheduleMissingError(None, service_on)
        try:
            schedule = self.schedule_for(profile.school_name)
        except KeyError as error:
            raise SchoolLunchScheduleMissingError(
                profile.school_name,
                service_on,
            ) from error
        return schedule.rule_on(service_on)

    def vendor_service_available(
        self,
        profile: PersonProfile,
        service_on: date,
    ) -> bool:
        _validate_date(service_on, "중식 서비스일")
        if service_on.weekday() >= 5:
            return False
        rule = self.rule_for(profile, service_on)
        if rule.mark is SchoolLunchMark.VENDOR_REQUIRED:
            return True
        if profile.grade is None:
            return False
        grade = normalize_grade(profile.grade)
        return grade in {normalize_grade(item) for item in rule.grades_without_school_meal}

    def manifest(self) -> Mapping[str, JsonValue]:
        schedule_manifests = [dict(item.manifest()) for item in self.schedules]
        encoded = json.dumps(
            schedule_manifests,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return MappingProxyType(
            {
                "policy_version": SCHOOL_LUNCH_CALENDAR_POLICY_VERSION,
                "school_count": len(self.schedules),
                "weekday_fact_count": sum(len(item.days) for item in self.schedules),
                "content_sha256": sha256(encoded).hexdigest(),
            }
        )


def school_lunch_schedule_entity_id(school_name: str) -> UUID:
    return uuid5(_SCHOOL_SCHEDULE_NAMESPACE, normalize_school_name(school_name))


def normalize_school_name(value: str) -> str:
    text = _required_text(value, "학교명")
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(normalized.split())


def normalize_grade(value: str) -> str:
    text = unicodedata.normalize("NFKC", _required_text(value, "학년"))
    match = _GRADE_NUMBER.fullmatch(text)
    if match is not None:
        return f"{int(match.group(1))}학년"
    return "".join(text.split())


def school_lunch_school_schedule_state(
    schedule: SchoolLunchSchoolSchedule,
) -> Mapping[str, JsonValue]:
    if not isinstance(schedule, SchoolLunchSchoolSchedule):
        raise TypeError("schedule은 SchoolLunchSchoolSchedule이어야 합니다.")
    return {
        "policy_version": SCHOOL_LUNCH_CALENDAR_POLICY_VERSION,
        "school_name": schedule.school_name,
        "normalized_school_name": schedule.normalized_school_name,
        "covered_periods": [
            {
                "starts_on": period.starts_on.isoformat(),
                "ends_on": period.ends_on.isoformat(),
            }
            for period in schedule.covered_periods
        ],
        "days": [
            {
                "service_on": item.service_on.isoformat(),
                "mark": item.rule.mark.value,
                "grades_without_school_meal": sorted(
                    normalize_grade(grade)
                    for grade in item.rule.grades_without_school_meal
                ),
            }
            for item in schedule.days
        ],
    }


def school_lunch_school_schedule_from_state(
    state: Mapping[str, JsonValue],
) -> SchoolLunchSchoolSchedule:
    if not isinstance(state, Mapping):
        raise TypeError("학교 일정 state는 mapping이어야 합니다.")
    version = state.get("policy_version")
    if version != SCHOOL_LUNCH_CALENDAR_POLICY_VERSION:
        raise ValueError(f"지원하지 않는 학교 일정 policy version입니다: {version}")
    school_name = _required_text(state.get("school_name"), "학교명")
    if state.get("normalized_school_name") != normalize_school_name(school_name):
        raise ValueError("저장된 학교명 정규화 값이 일치하지 않습니다.")
    raw_periods = state.get("covered_periods")
    raw_days = state.get("days")
    if not isinstance(raw_periods, (list, tuple)):
        raise TypeError("학교 일정 covered_periods는 배열이어야 합니다.")
    if not isinstance(raw_days, (list, tuple)):
        raise TypeError("학교 일정 days는 배열이어야 합니다.")
    periods = tuple(
        InclusiveDateRange(
            date.fromisoformat(_mapping_text(item, "starts_on")),
            date.fromisoformat(_mapping_text(item, "ends_on")),
        )
        for item in raw_periods
    )
    days = []
    for raw in raw_days:
        if not isinstance(raw, Mapping):
            raise TypeError("학교 날짜 state는 mapping이어야 합니다.")
        grades = raw.get("grades_without_school_meal", ())
        if not isinstance(grades, (list, tuple)):
            raise TypeError("학교 미급식 학년은 배열이어야 합니다.")
        days.append(
            SchoolLunchDay(
                date.fromisoformat(_mapping_text(raw, "service_on")),
                LunchDayRule(
                    SchoolLunchMark(_mapping_text(raw, "mark")),
                    frozenset(normalize_grade(str(item)) for item in grades),
                ),
            )
        )
    return SchoolLunchSchoolSchedule(school_name, periods, tuple(days))


def school_lunch_calendar_from_snapshot(
    snapshot: HistoricalSnapshot,
) -> SchoolLunchCalendar:
    if not isinstance(snapshot, HistoricalSnapshot):
        raise TypeError("snapshot은 HistoricalSnapshot이어야 합니다.")
    schedules = []
    for reference, state in snapshot.entities.items():
        if reference.entity_kind != SCHOOL_LUNCH_SCHEDULE_KIND:
            continue
        schedule = school_lunch_school_schedule_from_state(state)
        if reference.entity_id != schedule.entity_id:
            raise ValueError("학교 일정 entity ID가 학교명과 일치하지 않습니다.")
        schedules.append(schedule)
    return SchoolLunchCalendar(tuple(schedules))


def merge_school_lunch_schedule(
    existing: SchoolLunchSchoolSchedule | None,
    replacement: SchoolLunchSchoolSchedule,
) -> SchoolLunchSchoolSchedule:
    """Replace covered days from one import while preserving other seasons."""

    if existing is not None and not isinstance(existing, SchoolLunchSchoolSchedule):
        raise TypeError("existing은 SchoolLunchSchoolSchedule 또는 None이어야 합니다.")
    if not isinstance(replacement, SchoolLunchSchoolSchedule):
        raise TypeError("replacement는 SchoolLunchSchoolSchedule이어야 합니다.")
    if existing is None:
        return replacement
    if existing.normalized_school_name != replacement.normalized_school_name:
        raise ValueError("서로 다른 학교 일정은 합칠 수 없습니다.")
    replace_days = {
        day
        for period in replacement.covered_periods
        for day in _days(period.starts_on, period.ends_on)
    }
    merged_days = {
        item.service_on: item
        for item in existing.days
        if item.service_on not in replace_days
    }
    merged_days.update({item.service_on: item for item in replacement.days})
    periods = _merge_periods((*existing.covered_periods, *replacement.covered_periods))
    return SchoolLunchSchoolSchedule(
        replacement.school_name,
        periods,
        tuple(merged_days.values()),
    )


def merge_school_lunch_import(existing, replacement, explicit_periods):
    """Keep prior days outside the imported school's actual vacation periods.

    Monthly coverage still supplies school-meal defaults for previously unknown
    days. Those defaults must not erase another vacation in the same month.
    """
    merged = merge_school_lunch_schedule(existing, replacement)
    if existing is None:
        return merged
    days = {item.service_on: item for item in merged.days}
    for item in existing.days:
        if not any(period.contains(item.service_on) for period in explicit_periods):
            days[item.service_on] = item
    return SchoolLunchSchoolSchedule(merged.school_name, merged.covered_periods, tuple(days.values()))


def _merge_periods(
    periods: Iterable[InclusiveDateRange],
) -> tuple[InclusiveDateRange, ...]:
    concrete = sorted(
        tuple(periods),
        key=lambda item: (item.starts_on or date.min, item.ends_on or date.max),
    )
    merged: list[InclusiveDateRange] = []
    for period in concrete:
        if period.starts_on is None or period.ends_on is None:
            raise ValueError("학교 일정 기간은 시작일과 종료일이 필요합니다.")
        if not merged:
            merged.append(period)
            continue
        previous = merged[-1]
        assert previous.ends_on is not None
        if period.starts_on <= previous.ends_on + timedelta(days=1):
            merged[-1] = InclusiveDateRange(
                previous.starts_on,
                max(previous.ends_on, period.ends_on),
            )
        else:
            merged.append(period)
    return tuple(merged)


def _weekdays(starts_on: date, ends_on: date):
    return (day for day in _days(starts_on, ends_on) if day.weekday() < 5)


def _days(starts_on: date | None, ends_on: date | None):
    if starts_on is None or ends_on is None:
        raise ValueError("날짜 범위에는 시작일과 종료일이 필요합니다.")
    current = starts_on
    while current <= ends_on:
        yield current
        current += timedelta(days=1)


def _mapping_text(value: object, key: str) -> str:
    if not isinstance(value, Mapping):
        raise TypeError("학교 일정 state 항목은 mapping이어야 합니다.")
    return _required_text(value.get(key), key)


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}은 비어 있을 수 없습니다.")
    return value.strip()


def _validate_date(value: object, label: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label}은 date여야 합니다.")
