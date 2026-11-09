"""Remove one workbook import, replaying remaining calendar contributions."""
from dataclasses import dataclass
from datetime import date

from src.domain.revision_visibility import effective_history_revisions
from src.domain.revisions import AuditChange, ChangeOperation, RevisionMetadata, RevisionRecord, RevisionSource, plain_json
from src.domain.roster import InclusiveDateRange
from src.domain.projections import PERSON_KIND
from src.domain.school_calendar import (
    SCHOOL_LUNCH_SCHEDULE_KIND, SchoolLunchCalendar, SchoolLunchSchoolSchedule,
    normalize_school_name, school_lunch_calendar_from_snapshot,
    school_lunch_school_schedule_state, school_lunch_school_schedule_from_state,
    merge_school_lunch_schedule, merge_school_lunch_import,
)

DELETION_KIND = "school-lunch.import-deletion"
DELETION_DECISION = "school_calendar_import_delete"


def deleted_import_ids(snapshot):
    return frozenset(str(state["import_revision_id"]) for ref, state in snapshot.entities.items()
                     if ref.entity_kind == DELETION_KIND)


def _period(raw):
    return InclusiveDateRange(date.fromisoformat(raw["starts_on"]), date.fromisoformat(raw["ends_on"]))


def rebuild_calendar(records, excluded):
    schedules = {}
    for record in effective_history_revisions(records):
        if DELETION_DECISION in record.metadata.decisions or "snapshot_restore_target" in record.metadata.decisions:
            continue
        imported = record.metadata.decisions.get("school_calendar_import")
        if imported is not None:
            if str(record.metadata.revision_id) in excluded:
                continue
            # New imports retain their original contribution, even when only
            # person dates changed. Legacy imports are clipped to their own month.
            sources = imported.get("source_schedules")
            if sources is None:
                coverage = _period(imported["coverage"])
                sources = []
                for change in record.changes:
                    if change.entity_kind == SCHOOL_LUNCH_SCHEDULE_KIND and change.after:
                        schedule = school_lunch_school_schedule_from_state(change.after)
                        sources.append(school_lunch_school_schedule_state(SchoolLunchSchoolSchedule(
                            schedule.school_name, (coverage,), tuple(day for day in schedule.days if coverage.contains(day.service_on)))))
            for state in sources:
                source = school_lunch_school_schedule_from_state(state)
                key = source.normalized_school_name
                if imported.get("date_overlay_version"):
                    periods = [_period(row) for row in imported["school_rows"] if normalize_school_name(row["school_name"]) == key]
                    for review in imported.get("note_reviews", ()):
                        if normalize_school_name(review["school_name"]) == key and review.get("period"):
                            periods.append(_period(review["period"]))
                    schedules[key] = merge_school_lunch_import(schedules.get(key), source, periods)
                else:
                    schedules[key] = merge_school_lunch_schedule(schedules.get(key), source)
            continue
        for change in record.changes:
            if change.entity_kind != SCHOOL_LUNCH_SCHEDULE_KIND:
                continue
            state = change.after or change.before
            key = state["normalized_school_name"]
            if change.after is None:
                schedules.pop(key, None)
            elif change.before is None:
                schedules[key] = school_lunch_school_schedule_from_state(change.after)
            elif key in schedules:
                # Keep subsequent explicit manual edits only on coverage that
                # still belongs to a retained import. Do not resurrect a removed school.
                before = {day.service_on: day for day in school_lunch_school_schedule_from_state(change.before).days}
                after = school_lunch_school_schedule_from_state(change.after)
                changed = {day.service_on: day for day in after.days if before.get(day.service_on) != day}
                existing = schedules[key]
                schedules[key] = SchoolLunchSchoolSchedule(existing.school_name, existing.covered_periods,
                    tuple(changed.get(day.service_on, day) for day in existing.days))
    return SchoolLunchCalendar(tuple(schedules.values()))


@dataclass(frozen=True)
class PreparedSchoolCalendarDeletion:
    calendar: SchoolLunchCalendar
    record: RevisionRecord


def build_import_deletion(snapshot, records, import_revision_id, *, actor, recorded_at, revision_id):
    if not records or records[-1].metadata.revision_id != snapshot.revision_id:
        raise ValueError("명단이 변경되었습니다. 학교 일정 창을 다시 열어주세요.")
    effective = effective_history_revisions(records)
    source = next((item for item in effective if item.metadata.revision_id == import_revision_id
                   and "school_calendar_import" in item.metadata.decisions), None)
    excluded = deleted_import_ids(snapshot)
    if source is None or str(import_revision_id) in excluded:
        raise ValueError("선택한 불러오기 기록이 현재 일정에 없습니다. 학교 일정 창을 다시 열어주세요.")
    calendar = rebuild_calendar(records, excluded | {str(import_revision_id)})
    old = {item.entity_id: item for item in school_lunch_calendar_from_snapshot(snapshot).schedules}
    new = {item.entity_id: item for item in calendar.schedules}
    changes = []
    for entity_id in sorted(old.keys() | new.keys(), key=str):
        before = school_lunch_school_schedule_state(old[entity_id]) if entity_id in old else None
        after = school_lunch_school_schedule_state(new[entity_id]) if entity_id in new else None
        if before != after:
            operation = ChangeOperation.INSERT if before is None else ChangeOperation.DELETE if after is None else ChangeOperation.UPDATE
            changes.append(AuditChange(SCHOOL_LUNCH_SCHEDULE_KIND, entity_id, operation, before, after))
    later_date_edits = set()
    after_source = False
    for record in effective:
        if record.metadata.revision_id == import_revision_id:
            after_source = True
            continue
        if after_source:
            for change in record.changes:
                if (change.entity_kind == PERSON_KIND and change.before and change.after
                        and change.before.get("lunch_period") != change.after.get("lunch_period")):
                    later_date_edits.add(change.entity_id)
    # Legacy imports only filled display dates. New imports reset recipients;
    # deleting a calendar must not revive those cleared registrations or dates.
    reset_import = "lunch_reset" in source.metadata.decisions["school_calendar_import"]
    for change in (() if reset_import else source.changes):
        if change.entity_kind != PERSON_KIND or change.before is None or change.after is None:
            continue
        if change.entity_id in later_date_edits:
            continue
        current = snapshot.entity(PERSON_KIND, change.entity_id)
        if current is None or current.get("lunch_period") != change.after.get("lunch_period"):
            continue
        if change.before.get("lunch_period") == change.after.get("lunch_period"):
            continue
        after = plain_json(current)
        if "lunch_period" in change.before:
            after["lunch_period"] = plain_json(change.before["lunch_period"])
        else:
            after.pop("lunch_period", None)
        changes.append(AuditChange(PERSON_KIND, change.entity_id, ChangeOperation.UPDATE, current, after))
    changes.append(AuditChange(DELETION_KIND, revision_id, ChangeOperation.INSERT, None,
        {"import_revision_id": str(import_revision_id)}))
    record = RevisionRecord(RevisionMetadata(revision_id, snapshot.revision_id, recorded_at, None, actor,
        f"학교 일정 불러오기 삭제 · {source.import_batch.source_filename if source.import_batch else '선택 기록'}",
        RevisionSource.UI, decisions={DELETION_DECISION: str(import_revision_id)}), tuple(changes))
    return PreparedSchoolCalendarDeletion(calendar, record)
