"""Personal history displays user values rather than storage representations."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from src.domain.delivery_routing import ROUTING_EVENT_KIND, RoutingEvent, _routing_event_state
from src.domain.projections import roster_audit_changes
from src.domain.revisions import (
    AuditChange, ChangeOperation, HistoricalSnapshot, ImportBatch, RevisionActor,
    RevisionMetadata, RevisionRecord, RevisionSource,
)
from src.domain.roster import CustomFieldDefinition, CustomFieldType, Person, PersonProfile, RosterSnapshot
from src.services.person_history import build_history_tables


STAMP = datetime(2026, 10, 21, tzinfo=UTC)


def _record(changes, number=0, **metadata):
    return RevisionRecord(RevisionMetadata(
        uuid4(), None, STAMP + timedelta(seconds=number), date(2026, 10, 21),
        RevisionActor("담당자", "test"), "수정", RevisionSource.UI, **metadata,
    ), tuple(changes))


@pytest.mark.parametrize("field_type,old,new,expected", [
    (CustomFieldType.TEXT, "종8080", "종9090", ("종8080", "종9090")),
    (CustomFieldType.TEXT, None, "종9090", ("", "종9090")),
    (CustomFieldType.TEXT, "종9090", None, ("종9090", "")),
    (CustomFieldType.NUMBER, 0, 3.5, ("0", "3.5")),
    (CustomFieldType.DATE, date(2026, 1, 1), date(2026, 2, 2), ("2026-01-01", "2026-02-02")),
])
def test_custom_history_unwraps_values_without_altering_record(field_type, old, new, expected):
    field = CustomFieldDefinition(uuid4(), "사용자 칼럼", field_type, 0)
    person = Person(uuid4(), PersonProfile("대상", custom_values={} if old is None else {field.definition_id: old}))
    before = RosterSnapshot(uuid4(), persons=(person,), custom_fields=(field,))
    after = replace(before, revision_id=uuid4(), persons=(replace(person, profile=replace(
        person.profile, custom_values={} if new is None else {field.definition_id: new},
    )),))
    record = _record(roster_audit_changes(before, after))
    original = repr(record)
    headings, rows = build_history_tables(after, HistoricalSnapshot(record.metadata.revision_id, {}), (record,))["정보 변경"]
    assert len(rows) == 1
    assert rows[0][headings.index("항목")] == "사용자 정의: 사용자 칼럼"
    assert (rows[0][headings.index("이전 값")], rows[0][headings.index("변경 값")]) == expected
    assert repr(record) == original


def test_distinct_jurisdiction_records_keep_rows_and_resolve_real_previous_value():
    person = Person(uuid4(), PersonProfile("대상", jurisdiction="기존동"))
    roster = RosterSnapshot(uuid4(), persons=(person,))
    initial = _record(roster_audit_changes(None, roster))
    batch = ImportBatch(uuid4(), initial.metadata.revision_id, "synthetic.xlsx", "0" * 64,
                        0, "명단", "synthetic", {}, date(2026, 10, 1))
    initial = replace(initial, metadata=replace(initial.metadata, source=RevisionSource.IMPORT,
                      decisions={"import_purpose": "database"}), import_batch=batch)
    records = [initial]
    for number, day, value in ((1, date(2026, 10, 15), "위례동"),
                               (2, date(2026, 11, 1), "예약동"),
                               (3, date(2026, 10, 21), "위례동")):
        event = RoutingEvent(uuid4(), person.person_id, day, number, {}, jurisdiction=value)
        records.append(_record((AuditChange(ROUTING_EVENT_KIND, event.event_id, ChangeOperation.INSERT,
                                           None, _routing_event_state(event)),), number))
    snapshot = HistoricalSnapshot(records[-1].metadata.revision_id, {})
    headings, rows = build_history_tables(roster, snapshot, tuple(records), person_id=person.person_id)["정보 변경"]
    assert len(rows) == 3
    assert [(row[headings.index("이전 값")], row[headings.index("변경 값")]) for row in rows] == [
        ("기존동", "위례동"), ("위례동", "예약동"), ("위례동", "위례동"),
    ]
    assert len({row[headings.index("변경번호")] for row in rows}) == 3
