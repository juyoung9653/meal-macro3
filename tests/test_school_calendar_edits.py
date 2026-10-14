from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from src.domain.revisions import (
    EntityRef,
    HistoricalSnapshot,
    RevisionActor,
    RevisionSource,
)
from src.domain.roster import InclusiveDateRange
from src.domain.entitlements import (
    LunchDayRule,
    SchoolLunchMark,
)
from src.domain.school_calendar import (
    SCHOOL_LUNCH_SCHEDULE_KIND,
    SchoolLunchDay,
    SchoolLunchSchoolSchedule,
    school_lunch_calendar_from_snapshot,
    school_lunch_school_schedule_state,
)
from src.domain.school_calendar_edits import (
    apply_school_lunch_rule_period,
    build_school_calendar_edit_revision,
    replace_school_lunch_rule_segment,
    school_lunch_rule_segments,
)


BASE_REVISION_ID = UUID("92000000-0000-0000-0000-000000000001")
EDIT_REVISION_ID = UUID("92000000-0000-0000-0000-000000000002")


def _schedule() -> SchoolLunchSchoolSchedule:
    coverage = InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 28))
    rules = {}
    current = coverage.starts_on
    while current <= coverage.ends_on:
        if current.weekday() < 5:
            mark = (
                SchoolLunchMark.VENDOR_REQUIRED
                if date(2026, 8, 19) <= current <= date(2026, 8, 25)
                else SchoolLunchMark.SCHOOL_PROVIDES
            )
            rules[current] = LunchDayRule(mark)
        current = date.fromordinal(current.toordinal() + 1)
    return SchoolLunchSchoolSchedule(
        "분당초",
        (coverage,),
        tuple(SchoolLunchDay(day, rule) for day, rule in rules.items()),
    )


def _snapshot(schedule: SchoolLunchSchoolSchedule) -> HistoricalSnapshot:
    return HistoricalSnapshot(
        BASE_REVISION_ID,
        {
            EntityRef(SCHOOL_LUNCH_SCHEDULE_KIND, schedule.entity_id): (
                school_lunch_school_schedule_state(schedule)
            )
        },
    )


def test_segments_losslessly_group_equal_rules_across_weekend():
    segments = school_lunch_rule_segments(_schedule())

    assert tuple(
        (item.starts_on, item.ends_on, item.rule.mark, item.weekday_count)
        for item in segments
    ) == (
        (
            date(2026, 8, 17),
            date(2026, 8, 18),
            SchoolLunchMark.SCHOOL_PROVIDES,
            2,
        ),
        (
            date(2026, 8, 19),
            date(2026, 8, 25),
            SchoolLunchMark.VENDOR_REQUIRED,
            5,
        ),
        (
            date(2026, 8, 26),
            date(2026, 8, 28),
            SchoolLunchMark.SCHOOL_PROVIDES,
            3,
        ),
    )


def test_resize_segment_lets_neighbours_fill_removed_days_and_extends_new_rule():
    schedule = _schedule()
    vacation = school_lunch_rule_segments(schedule)[1]

    updated = replace_school_lunch_rule_segment(
        schedule,
        vacation,
        InclusiveDateRange(date(2026, 8, 20), date(2026, 8, 26)),
        LunchDayRule(SchoolLunchMark.VENDOR_REQUIRED),
    )

    assert updated.rule_on(date(2026, 8, 19)).mark is SchoolLunchMark.SCHOOL_PROVIDES
    assert updated.rule_on(date(2026, 8, 20)).mark is SchoolLunchMark.VENDOR_REQUIRED
    assert updated.rule_on(date(2026, 8, 26)).mark is SchoolLunchMark.VENDOR_REQUIRED
    assert updated.rule_on(date(2026, 8, 27)).mark is SchoolLunchMark.SCHOOL_PROVIDES


def test_new_period_overlay_supports_grade_only_vendor_delivery():
    schedule = _schedule()
    updated = apply_school_lunch_rule_period(
        schedule,
        InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 18)),
        LunchDayRule(
            SchoolLunchMark.SCHOOL_PROVIDES,
            frozenset({"6"}),
        ),
    )

    assert updated.rule_on(date(2026, 8, 17)).grades_without_school_meal == {
        "6학년"
    }
    assert updated.rule_on(date(2026, 8, 19)) == schedule.rule_on(date(2026, 8, 19))


def test_edit_revision_records_exact_ui_diff_without_import_batch():
    schedule = _schedule()
    updated = apply_school_lunch_rule_period(
        schedule,
        InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 18)),
        LunchDayRule(SchoolLunchMark.VENDOR_REQUIRED),
    )

    prepared = build_school_calendar_edit_revision(
        _snapshot(schedule),
        (updated,),
        actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=datetime(2026, 8, 31, 2, 0, tzinfo=UTC),
        revision_id=EDIT_REVISION_ID,
    )

    assert prepared.changed_school_names == ("분당초",)
    assert prepared.changed_weekday_count == 2
    assert prepared.record.import_batch is None
    assert prepared.record.metadata.source is RevisionSource.UI
    assert prepared.record.metadata.effective_on == date(2026, 8, 17)
    decision = prepared.record.metadata.decisions["school_calendar_edit"]["schools"][0]
    assert decision["changed_periods"][0] == {
        "starts_on": "2026-08-17",
        "ends_on": "2026-08-18",
        "weekday_count": 2,
        "before": {"mark": "O", "grades_without_school_meal": ()},
        "after": {"mark": "X", "grades_without_school_meal": ()},
    }

    restored = school_lunch_calendar_from_snapshot(
        HistoricalSnapshot(
            EDIT_REVISION_ID,
            {
                EntityRef(
                    SCHOOL_LUNCH_SCHEDULE_KIND,
                    prepared.record.changes[0].entity_id,
                ): prepared.record.changes[0].after
            },
        )
    )
    assert restored.schedule_for("분당초") == updated


def test_edit_revision_rejects_a_noop():
    schedule = _schedule()

    with pytest.raises(ValueError, match="달라지는 내용"):
        build_school_calendar_edit_revision(
            _snapshot(schedule),
            (schedule,),
            actor=RevisionActor("tester", "S-1-5-21-test"),
            recorded_at=datetime(2026, 8, 31, 2, 0, tzinfo=UTC),
            revision_id=EDIT_REVISION_ID,
        )
