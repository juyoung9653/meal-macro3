"""Lossless UI edits for reviewed school-lunch calendars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4

from .entitlements import LunchDayRule
from .revisions import (
    AuditChange,
    ChangeOperation,
    HistoricalSnapshot,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from .roster import InclusiveDateRange
from .school_calendar import (
    SCHOOL_LUNCH_CALENDAR_POLICY_VERSION,
    SCHOOL_LUNCH_SCHEDULE_KIND,
    SchoolLunchCalendar,
    SchoolLunchDay,
    SchoolLunchSchoolSchedule,
    normalize_grade,
    school_lunch_calendar_from_snapshot,
    school_lunch_school_schedule_state,
)


SCHOOL_LUNCH_CALENDAR_EDIT_POLICY_VERSION = 1


@dataclass(frozen=True, slots=True)
class SchoolLunchRuleSegment:
    """One contiguous run of reviewed weekdays sharing exactly one rule."""

    starts_on: date
    ends_on: date
    rule: LunchDayRule
    weekday_count: int

    def __post_init__(self) -> None:
        _validate_date(self.starts_on, "구간 시작일")
        _validate_date(self.ends_on, "구간 종료일")
        if self.ends_on < self.starts_on:
            raise ValueError("학교 일정 구간 종료일은 시작일보다 빠를 수 없습니다.")
        if not isinstance(self.rule, LunchDayRule):
            raise TypeError("학교 일정 구간 rule은 LunchDayRule이어야 합니다.")
        if isinstance(self.weekday_count, bool) or not isinstance(
            self.weekday_count, int
        ):
            raise TypeError("학교 일정 구간 평일 수는 정수여야 합니다.")
        if self.weekday_count < 1:
            raise ValueError("학교 일정 구간에는 평일이 하나 이상 필요합니다.")


@dataclass(frozen=True, slots=True)
class PreparedSchoolCalendarEditRevision:
    calendar: SchoolLunchCalendar
    record: RevisionRecord
    changed_school_names: tuple[str, ...]
    changed_weekday_count: int


def school_lunch_rule_segments(
    schedule: SchoolLunchSchoolSchedule,
) -> tuple[SchoolLunchRuleSegment, ...]:
    """Collapse canonical weekday facts into lossless, human-sized periods."""

    if not isinstance(schedule, SchoolLunchSchoolSchedule):
        raise TypeError("schedule은 SchoolLunchSchoolSchedule이어야 합니다.")
    by_day = {item.service_on: item.rule for item in schedule.days}
    segments: list[SchoolLunchRuleSegment] = []
    for coverage in schedule.covered_periods:
        assert coverage.starts_on is not None and coverage.ends_on is not None
        reviewed_days = tuple(_weekdays(coverage.starts_on, coverage.ends_on))
        if not reviewed_days:
            continue
        segment_start = reviewed_days[0]
        segment_end = reviewed_days[0]
        segment_rule = by_day[reviewed_days[0]]
        count = 1
        for service_on in reviewed_days[1:]:
            rule = by_day[service_on]
            if rule == segment_rule:
                segment_end = service_on
                count += 1
                continue
            segments.append(
                SchoolLunchRuleSegment(
                    segment_start,
                    segment_end,
                    segment_rule,
                    count,
                )
            )
            segment_start = service_on
            segment_end = service_on
            segment_rule = rule
            count = 1
        segments.append(
            SchoolLunchRuleSegment(
                segment_start,
                segment_end,
                segment_rule,
                count,
            )
        )
    return tuple(segments)


def apply_school_lunch_rule_period(
    schedule: SchoolLunchSchoolSchedule,
    period: InclusiveDateRange,
    rule: LunchDayRule,
) -> SchoolLunchSchoolSchedule:
    """Overlay one reviewed rule on a period within one stored coverage block."""

    coverage, service_days = _validated_edit_period(schedule, period)
    del coverage
    if not isinstance(rule, LunchDayRule):
        raise TypeError("적용할 학교 일정 rule은 LunchDayRule이어야 합니다.")
    by_day = {item.service_on: item.rule for item in schedule.days}
    for service_on in service_days:
        by_day[service_on] = rule
    return _schedule_with_rules(schedule, by_day)


def replace_school_lunch_rule_segment(
    schedule: SchoolLunchSchoolSchedule,
    original: SchoolLunchRuleSegment,
    period: InclusiveDateRange,
    rule: LunchDayRule,
) -> SchoolLunchSchoolSchedule:
    """Move/resize one visible segment while letting its neighbours fill gaps."""

    if not isinstance(schedule, SchoolLunchSchoolSchedule):
        raise TypeError("schedule은 SchoolLunchSchoolSchedule이어야 합니다.")
    if not isinstance(original, SchoolLunchRuleSegment):
        raise TypeError("원래 구간은 SchoolLunchRuleSegment여야 합니다.")
    segments = school_lunch_rule_segments(schedule)
    try:
        segment_index = segments.index(original)
    except ValueError as error:
        raise ValueError("선택한 구간이 현재 학교 일정과 일치하지 않습니다.") from error
    coverage, new_days = _validated_edit_period(schedule, period)
    if not isinstance(rule, LunchDayRule):
        raise TypeError("적용할 학교 일정 rule은 LunchDayRule이어야 합니다.")
    if not (coverage.contains(original.starts_on) and coverage.contains(original.ends_on)):
        raise ValueError("수정 날짜는 선택한 구간과 같은 검토 범위 안이어야 합니다.")

    original_days = tuple(_weekdays(original.starts_on, original.ends_on))
    if set(original_days).isdisjoint(new_days):
        raise ValueError("수정한 기간은 원래 선택한 구간과 하루 이상 겹쳐야 합니다.")
    by_day = {item.service_on: item.rule for item in schedule.days}

    removed_before = tuple(day for day in original_days if day < new_days[0])
    if removed_before:
        previous = _adjacent_segment(
            schedule,
            segments,
            segment_index - 1,
            coverage,
        )
        if previous is None:
            raise ValueError(
                "이 구간의 시작일을 늦추려면 앞에 이어받을 저장 구간이 필요합니다. "
                "필요한 날짜만 새 기간으로 지정해주세요."
            )
        for service_on in removed_before:
            by_day[service_on] = previous.rule

    removed_after = tuple(day for day in original_days if day > new_days[-1])
    if removed_after:
        following = _adjacent_segment(
            schedule,
            segments,
            segment_index + 1,
            coverage,
        )
        if following is None:
            raise ValueError(
                "이 구간의 종료일을 앞당기려면 뒤에 이어받을 저장 구간이 필요합니다. "
                "필요한 날짜만 새 기간으로 지정해주세요."
            )
        for service_on in removed_after:
            by_day[service_on] = following.rule

    for service_on in new_days:
        by_day[service_on] = rule
    return _schedule_with_rules(schedule, by_day)


def build_school_calendar_edit_revision(
    current_snapshot: HistoricalSnapshot,
    replacements: tuple[SchoolLunchSchoolSchedule, ...],
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID | None = None,
    reason: str = "저장된 학교 중식 일정 수정",
) -> PreparedSchoolCalendarEditRevision:
    """Commit all changed schools from one editor session as one UI revision."""

    if not isinstance(current_snapshot, HistoricalSnapshot):
        raise TypeError("current_snapshot은 HistoricalSnapshot이어야 합니다.")
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor이어야 합니다.")
    if not isinstance(recorded_at, datetime) or recorded_at.tzinfo is None:
        raise ValueError("recorded_at에는 시간대가 있어야 합니다.")
    revision_id = uuid4() if revision_id is None else revision_id
    if not isinstance(revision_id, UUID):
        raise TypeError("revision ID는 UUID여야 합니다.")
    if revision_id == current_snapshot.revision_id:
        raise ValueError("새 revision ID는 현재 revision과 달라야 합니다.")

    requested = tuple(replacements)
    if any(not isinstance(item, SchoolLunchSchoolSchedule) for item in requested):
        raise TypeError("수정 일정에는 SchoolLunchSchoolSchedule만 허용됩니다.")
    keys = tuple(item.normalized_school_name for item in requested)
    if len(set(keys)) != len(keys):
        raise ValueError("같은 학교를 한 번의 수정에서 두 번 저장할 수 없습니다.")

    existing_calendar = school_lunch_calendar_from_snapshot(current_snapshot)
    existing_by_key = {
        schedule.normalized_school_name: schedule
        for schedule in existing_calendar.schedules
    }
    updated_by_key = dict(existing_by_key)
    audit_changes: list[AuditChange] = []
    decisions = []
    changed_school_names = []
    changed_days_total = 0
    earliest: date | None = None

    for replacement in requested:
        existing = existing_by_key.get(replacement.normalized_school_name)
        if existing is None:
            raise ValueError(
                f"저장된 일정에 없는 학교는 이 화면에서 추가할 수 없습니다: {replacement.school_name}"
            )
        if replacement.school_name != existing.school_name:
            raise ValueError("저장 일정 수정에서는 학교명을 바꿀 수 없습니다.")
        if replacement.covered_periods != existing.covered_periods:
            raise ValueError("저장 일정 수정에서는 검토 완료 범위를 바꿀 수 없습니다.")
        if replacement == existing:
            continue

        changed = _changed_days(existing, replacement)
        if not changed:
            continue
        earliest = changed[0] if earliest is None else min(earliest, changed[0])
        changed_days_total += len(changed)
        changed_school_names.append(existing.school_name)
        decisions.append(
            {
                "school_name": existing.school_name,
                "changed_weekdays": len(changed),
                "first_changed_on": changed[0].isoformat(),
                "last_changed_on": changed[-1].isoformat(),
                "changed_periods": _changed_period_manifest(existing, replacement),
            }
        )
        audit_changes.append(
            AuditChange(
                SCHOOL_LUNCH_SCHEDULE_KIND,
                existing.entity_id,
                ChangeOperation.UPDATE,
                before=school_lunch_school_schedule_state(existing),
                after=school_lunch_school_schedule_state(replacement),
            )
        )
        updated_by_key[replacement.normalized_school_name] = replacement

    if not audit_changes or earliest is None:
        raise ValueError("저장된 학교 일정에서 달라지는 내용이 없습니다.")

    metadata = RevisionMetadata(
        revision_id=revision_id,
        parent_revision_id=current_snapshot.revision_id,
        recorded_at=recorded_at,
        effective_on=earliest,
        actor=actor,
        reason=reason,
        source=RevisionSource.UI,
        decisions={"school_calendar_edit": {"schools": decisions}},
        rule_versions={
            "school_calendar_policy_version": SCHOOL_LUNCH_CALENDAR_POLICY_VERSION,
            "school_calendar_edit_policy_version": (
                SCHOOL_LUNCH_CALENDAR_EDIT_POLICY_VERSION
            ),
        },
    )
    return PreparedSchoolCalendarEditRevision(
        SchoolLunchCalendar(tuple(updated_by_key.values())),
        RevisionRecord(metadata, tuple(audit_changes)),
        tuple(changed_school_names),
        changed_days_total,
    )


def _validated_edit_period(
    schedule: SchoolLunchSchoolSchedule,
    period: InclusiveDateRange,
) -> tuple[InclusiveDateRange, tuple[date, ...]]:
    if not isinstance(schedule, SchoolLunchSchoolSchedule):
        raise TypeError("schedule은 SchoolLunchSchoolSchedule이어야 합니다.")
    if not isinstance(period, InclusiveDateRange):
        raise TypeError("수정 기간은 InclusiveDateRange여야 합니다.")
    if period.starts_on is None or period.ends_on is None:
        raise ValueError("수정 기간에는 시작일과 종료일이 필요합니다.")
    coverage = next(
        (
            item
            for item in schedule.covered_periods
            if item.contains(period.starts_on) and item.contains(period.ends_on)
        ),
        None,
    )
    if coverage is None:
        raise ValueError("수정 기간은 한 개의 저장된 검토 범위 안이어야 합니다.")
    service_days = tuple(_weekdays(period.starts_on, period.ends_on))
    if not service_days:
        raise ValueError("수정 기간에는 평일이 하나 이상 있어야 합니다.")
    return coverage, service_days


def _adjacent_segment(
    schedule: SchoolLunchSchoolSchedule,
    segments: tuple[SchoolLunchRuleSegment, ...],
    index: int,
    coverage: InclusiveDateRange,
) -> SchoolLunchRuleSegment | None:
    if index < 0 or index >= len(segments):
        return None
    candidate = segments[index]
    if not (
        coverage.contains(candidate.starts_on)
        and coverage.contains(candidate.ends_on)
    ):
        return None
    del schedule
    return candidate


def _schedule_with_rules(
    schedule: SchoolLunchSchoolSchedule,
    rules: dict[date, LunchDayRule],
) -> SchoolLunchSchoolSchedule:
    return SchoolLunchSchoolSchedule(
        schedule.school_name,
        schedule.covered_periods,
        tuple(
            SchoolLunchDay(service_on, rule)
            for service_on, rule in sorted(rules.items())
        ),
    )


def _changed_days(
    before: SchoolLunchSchoolSchedule,
    after: SchoolLunchSchoolSchedule,
) -> tuple[date, ...]:
    before_rules = {item.service_on: item.rule for item in before.days}
    after_rules = {item.service_on: item.rule for item in after.days}
    if set(before_rules) != set(after_rules):
        raise ValueError("수정 전후 학교 일정의 검토 평일 범위가 다릅니다.")
    return tuple(
        service_on
        for service_on in sorted(before_rules)
        if before_rules[service_on] != after_rules[service_on]
    )


def _changed_period_manifest(
    before: SchoolLunchSchoolSchedule,
    after: SchoolLunchSchoolSchedule,
) -> list[dict[str, object]]:
    before_rules = {item.service_on: item.rule for item in before.days}
    after_rules = {item.service_on: item.rule for item in after.days}
    changed = _changed_days(before, after)
    groups: list[list[date]] = []
    for service_on in changed:
        if not groups:
            groups.append([service_on])
            continue
        previous = groups[-1][-1]
        same_transition = (
            before_rules[previous] == before_rules[service_on]
            and after_rules[previous] == after_rules[service_on]
        )
        if service_on == _next_weekday(previous) and same_transition:
            groups[-1].append(service_on)
        else:
            groups.append([service_on])
    return [
        {
            "starts_on": group[0].isoformat(),
            "ends_on": group[-1].isoformat(),
            "weekday_count": len(group),
            "before": _rule_manifest(before_rules[group[0]]),
            "after": _rule_manifest(after_rules[group[0]]),
        }
        for group in groups
    ]


def _rule_manifest(rule: LunchDayRule) -> dict[str, object]:
    return {
        "mark": rule.mark.value,
        "grades_without_school_meal": sorted(
            normalize_grade(grade) for grade in rule.grades_without_school_meal
        ),
    }


def _next_weekday(value: date) -> date:
    candidate = value + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _weekdays(starts_on: date, ends_on: date):
    current = starts_on
    while current <= ends_on:
        if current.weekday() < 5:
            yield current
        current += timedelta(days=1)


def _validate_date(value: object, label: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label}은 date여야 합니다.")
