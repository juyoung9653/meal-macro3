from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from src.desktop_v2.inspector_history import InspectorMealHistory
from src.domain import (
    AuditChange, ChangeOperation, HistoricalSnapshot, Person, PersonProfile,
    RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource, RosterSnapshot,
    STATUS_EVENT_KIND,
)


def test_inspector_history_keeps_all_rows_filters_people_and_refreshes_revision():
    first = Person(uuid4(), PersonProfile("첫 사람"))
    second = Person(uuid4(), PersonProfile("둘째 사람"))
    records = {}
    parent = None
    for index in range(8):
        revision_id = uuid4()
        person = second if index == 7 else first
        metadata = RevisionMetadata(
            revision_id, parent, datetime(2026, 9, 22, 1, 0, index, tzinfo=UTC),
            None, RevisionActor("tester", "sid"), f"변경 {index}", RevisionSource.UI,
        )
        before = {"person_id": str(person.person_id), "meal": "석식", "status": "배송", "ends_on": "2026-09-30"}
        after = {**before, "status": "일시중지", "ends_on": None}
        records[revision_id] = RevisionRecord(metadata, (
            AuditChange(STATUS_EVENT_KIND, uuid4(), ChangeOperation.UPDATE, before, after),
        ))
        parent = revision_id
    roster = RosterSnapshot(parent, persons=(first, second))

    class Database:
        reads = 0

        def snapshot(self, revision_id):
            self.reads += 1
            return HistoricalSnapshot(revision_id, {})

        def revision(self, revision_id):
            return records[revision_id]

    database = Database()
    history = InspectorMealHistory(database)
    rows = history.rows_for(roster, first.person_id)
    assert len(rows) == 14  # More than five must remain available to scroll.
    assert "석식" in rows[0][0] and "배송 → 일시중지" in rows[0][0]
    assert "종료일 미지정" in rows[1].text
    assert "종료일=" not in rows[1].text and "ends_on" not in rows[1].tooltip
    assert rows[0].applied == "지정 안 함"
    assert rows[0].recorded.startswith("2026-09-22")
    assert "변경 6" in rows[0][1] and "변경 0" in rows[-1][1]
    assert len(history.rows_for(roster, second.person_id)) == 2
    assert history.rows_for(roster, uuid4()) == ()
    assert database.reads == 1

    previous_id = records[parent].metadata.parent_revision_id
    earlier = replace(roster, revision_id=previous_id)
    assert history.rows_for(earlier, second.person_id) == ()
    assert database.reads == 2
