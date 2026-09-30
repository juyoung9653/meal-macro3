"""Canonical revision oracles for export layouts and person history."""
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from uuid import uuid4

import openpyxl
import pytest
from openpyxl.utils.cell import coordinate_from_string

from src.domain.complaints import Complaint, build_complaint_revision
from src.domain.delivery_routing import RoutingState, build_routing_revision
from src.domain.export_security import build_export_password_revision
from src.domain.intake import IntakeChannel
from src.domain.meal_status_changes import (
    build_batch_meal_status_revision,
    build_meal_status_revision,
)
from src.domain.profile_edits import build_person_profile_revision
from src.domain.projections import (
    roster_audit_changes,
    roster_entity_states,
    roster_from_historical_snapshot,
)
from src.domain.revisions import (
    HistoricalSnapshot,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
    format_revision_timestamp,
)
from src.domain.roster import (
    CustomFieldDefinition,
    CustomFieldType,
    DeliveryAssignment,
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.status import MealStatus
from src.services.database_export import build_database_workbook
from src.services.person_history import build_history_tables

DAY = date(2026, 9, 21)
STAMP = datetime(2026, 9, 21, 1, 30, tzinfo=UTC)
ACTOR = RevisionActor("접수담당", "test-sid")


@pytest.fixture
def base():
    field = CustomFieldDefinition(uuid4(), "추가 안내", CustomFieldType.TEXT, 0)
    people = (
        Person(uuid4(), PersonProfile("첫째", telephone="001234", note="=not a formula",
                                    road_address="이전 주소", detail_address="101호",
                                    custom_values={field.definition_id: "=literal"})),
        Person(uuid4(), PersonProfile("둘째")),
        Person(uuid4(), PersonProfile("미가입 아동")),
    )
    return RosterSnapshot(
        uuid4(), persons=people, custom_fields=(field,),
        enrollments=tuple(MealEnrollment(p.person_id, m) for p in people[:2]
                          for m in (Meal.BREAKFAST, Meal.DINNER)),
        assignments=tuple(DeliveryAssignment(p.person_id, m, "기사A", "001")
                          for p in people[:2] for m in (Meal.BREAKFAST, Meal.DINNER)),
    )


def initial(roster):
    return RevisionRecord(
        RevisionMetadata(roster.revision_id, None, STAMP, DAY, ACTOR, "초기 명단", RevisionSource.IMPORT),
        roster_audit_changes(None, roster),
    )


def project(base, records):
    states = dict(roster_entity_states(base))
    for record in records:
        for change in record.changes:
            from src.domain.revisions import EntityRef
            key = EntityRef(change.entity_kind, change.entity_id)
            if change.after is None:
                states.pop(key, None)
            else:
                states[key] = change.after
    snapshot = HistoricalSnapshot(records[-1].metadata.revision_id if records else base.revision_id, states)
    return roster_from_historical_snapshot(snapshot), snapshot


def rows(sheet):
    header_row = coordinate_from_string(sheet.freeze_panes)[1] - 1
    headings = [cell.value for cell in sheet[header_row]]
    return [dict(zip(headings, row, strict=True)) for row in sheet.iter_rows(min_row=header_row + 1, values_only=True)]


def test_wide_mode_actual_status_custom_columns_and_literal_roundtrip(base):
    person = base.persons[0]
    change = build_meal_status_revision(
        base, person.person_id, (Meal.BREAKFAST, Meal.DINNER), MealStatus.STOPPED,
        InclusiveDateRange(DAY, None), actor=ACTOR, recorded_at=STAMP,
        revision_id=uuid4(), event_ids={m: uuid4() for m in (Meal.BREAKFAST, Meal.DINNER)},
    )
    roster, snapshot = project(base, (change.record,))
    book = build_database_workbook(roster, snapshot, (initial(base), change.record), day=DAY)
    data = BytesIO()
    book.save(data)
    book.close()
    reopened = openpyxl.load_workbook(data)
    table = rows(reopened["명단"])
    assert len(table) == 3
    first = next(row for row in table if row["성명"] == "첫째")
    assert first["조식"] == first["석식"] == "미배송"
    assert first["중식"] == "미가입"
    assert first["전화번호"] == "001234" and first["조식 순서"] == "001"
    assert first["비고"] == "=not a formula" and first["추가 안내"] == "=literal"
    assert not any(cell.data_type == "f" for sheet in reopened for row in sheet for cell in row)
    assert "식종" not in first
    reopened.close()


@pytest.mark.parametrize("driver", ["기사B", None])
def test_one_legacy_different_or_missing_driver_splits_entire_roster(base, driver):
    assignments = tuple(
        replace(item, driver=driver) if item.person_id == base.persons[0].person_id and item.meal is Meal.DINNER
        else item for item in base.assignments
    )
    base = replace(base, assignments=assignments)
    snapshot = HistoricalSnapshot(base.revision_id, roster_entity_states(base))
    book = build_database_workbook(base, snapshot, (initial(base),), day=DAY, sections=("roster",))
    table = rows(book["명단"])
    assert len(table) == 5
    assert {row["식종"] for row in table if row["성명"] == "둘째"} == {"조식", "석식"}
    assert len([row for row in table if row["성명"] == "미가입 아동"]) == 1
    assert {row["배송담당"] for row in table if row["성명"] == "첫째"} == {"기사A", driver or ""}
    book.close()


def test_effective_relocation_changes_only_from_its_date_and_preserves_original(base):
    change = build_routing_revision(
        base, RoutingState(), person_id=base.persons[0].person_id, effective_on=DAY,
        driver_ids={}, road_address="새 주소", detail_address="202호",
        actor=ACTOR, recorded_at=STAMP, revision_id=uuid4(), event_id=uuid4(),
    )
    roster, snapshot = project(base, (change,))
    for day, expected in ((date(2026, 9, 20), "이전 주소"), (DAY, "새 주소")):
        book = build_database_workbook(roster, snapshot, (initial(base), change), day=day, sections=("roster",))
        first = next(row for row in rows(book["명단"]) if row["성명"] == "첫째")
        assert first["도로명주소"] == expected
        assert first["원본 도로명주소"] == "이전 주소"
        book.close()


def test_lunch_missing_schedule_matches_roster_dash(base):
    first = base.persons[0]
    base = replace(
        base,
        persons=(replace(first, profile=replace(first.profile, school_name="일정 없는 학교", grade="1학년")), *base.persons[1:]),
        enrollments=(*base.enrollments, MealEnrollment(first.person_id, Meal.LUNCH)),
        assignments=(*base.assignments, DeliveryAssignment(first.person_id, Meal.LUNCH, "기사A")),
    )
    snapshot = HistoricalSnapshot(base.revision_id, roster_entity_states(base))
    book = build_database_workbook(base, snapshot, (), day=DAY, sections=("roster",))
    assert rows(book["명단"])[0]["중식"] == "-"
    book.close()


def test_canonical_profile_history_per_person_with_human_labels_and_custom_values(base):
    person = base.persons[0]
    changed_at = STAMP + timedelta(seconds=1)
    new_profile = replace(person.profile, school_name="새 학교",
                          custom_values={base.custom_fields[0].definition_id: "새 안내"})
    change = build_person_profile_revision(
        base, person.person_id, new_profile, actor=ACTOR, recorded_at=changed_at,
        effective_on=DAY, revision_id=uuid4(),
    )
    roster, snapshot = project(base, (change.record,))
    records = (initial(base), change.record)
    tables = build_history_tables(roster, snapshot, records, person_id=person.person_id)
    headings, data = tables["정보 변경"]
    change_code = format_revision_timestamp(changed_at)
    assert change_code != format_revision_timestamp(STAMP)
    selected = [dict(zip(headings, row, strict=True)) for row in data
                if row[headings.index("변경번호")] == change_code]
    assert {row["항목"] for row in selected} == {"학교명", "사용자 정의: 추가 안내"}
    assert all(row["성명"] == "첫째" and row["사람 UUID"] == str(person.person_id) for row in selected)
    assert next(row for row in selected if row["항목"] == "학교명")["변경 값"] == "새 학교"
    other = build_history_tables(roster, snapshot, (change.record,), person_id=base.persons[1].person_id)
    assert not other["정보 변경"][1]


@pytest.mark.parametrize("batch,channel", [(False, None), (False, IntakeChannel.PHONE), (True, IntakeChannel.EMAIL)])
def test_status_history_has_intake_for_canonical_single_and_batch(base, batch, channel):
    person = base.persons[0]
    if batch:
        targets = tuple((p.person_id, Meal.BREAKFAST) for p in base.persons[:2])
        change = build_batch_meal_status_revision(
            base, targets, MealStatus.PAUSED, InclusiveDateRange(DAY, DAY),
            actor=ACTOR, recorded_at=STAMP, revision_id=uuid4(),
            event_ids={target: uuid4() for target in targets}, intake_channel=channel,
        )
    else:
        change = build_meal_status_revision(
            base, person.person_id, (Meal.BREAKFAST,), MealStatus.PAUSED, InclusiveDateRange(DAY, DAY),
            actor=ACTOR, recorded_at=STAMP, revision_id=uuid4(),
            event_ids={Meal.BREAKFAST: uuid4()}, intake_channel=channel,
        )
    roster, snapshot = project(base, (change.record,))
    headings, data = build_history_tables(roster, snapshot, (change.record,), person_id=person.person_id)["식종 변경"]
    assert data
    table = [dict(zip(headings, row, strict=True)) for row in data]
    assert all(row["성명"] == "첫째" for row in table)
    assert {row["접수 채널"] for row in table} == {"" if channel is None else channel.value}
    assert any(row["변경 값"] == "일시중지" for row in table)


def test_complaint_records_received_and_saved_time_and_keeps_deleted_person_name(base):
    person = base.persons[0]
    complaint = Complaint(uuid4(), person.person_id, STAMP, IntakeChannel.OTHER, "내용")
    record = build_complaint_revision(base, complaint, actor=ACTOR, recorded_at=STAMP, revision_id=uuid4())
    current, _ = project(base, (record,))
    deleted = replace(current, revision_id=uuid4(), persons=tuple(p for p in current.persons if p.person_id != person.person_id),
                      enrollments=tuple(e for e in current.enrollments if e.person_id != person.person_id),
                      assignments=tuple(a for a in current.assignments if a.person_id != person.person_id))
    deletion = RevisionRecord(
        RevisionMetadata(deleted.revision_id, current.revision_id, STAMP, DAY, ACTOR, "삭제", RevisionSource.UI),
        roster_audit_changes(current, deleted),
    )
    roster, snapshot = project(base, (record, deletion))
    headings, data = build_history_tables(roster, snapshot, (initial(base), record, deletion))["민원"]
    row = dict(zip(headings, data[0], strict=True))
    assert row["성명"] == "첫째"
    assert row["기록 시각(KST)"].hour == row["접수 시각(KST)"].hour == 10
    assert row["변경자"] == "접수담당"


def test_secret_setting_not_in_history_and_sections_are_independent(base):
    secret = "Never-export-this-password"
    record = build_export_password_revision(
        base.revision_id, None, secret, actor=ACTOR, recorded_at=STAMP, revision_id=uuid4(),
    ).record
    roster, snapshot = project(base, (record,))
    book = build_database_workbook(roster, snapshot, (initial(base), record), day=DAY, sections=("history",))
    assert "명단" not in book.sheetnames and "민원" not in book.sheetnames
    assert secret not in str([list(sheet.values) for sheet in book])
    assert all("password" not in str(row["대상 종류"]) for row in rows(book["변경 이력"]))
    book.close()
    with pytest.raises(ValueError):
        build_database_workbook(roster, snapshot, (), day=DAY, sections=())
