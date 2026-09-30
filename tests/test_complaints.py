from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from src.domain.complaints import (
    COMPLAINT_KIND,
    Complaint,
    build_complaint_revision,
    build_complaints_revision,
    complaints_from_snapshot,
)
from src.domain.intake import IntakeChannel
from src.domain.projections import roster_audit_changes
from src.domain.revisions import (
    EntityRef,
    HistoricalSnapshot,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.domain.roster import Meal, Person, PersonProfile, RosterSnapshot
from src.storage import DatabaseSession

BASE = UUID("a1000000-0000-0000-0000-000000000001")
NEXT = UUID("a1000000-0000-0000-0000-000000000002")
PERSON = UUID("a1000000-0000-0000-0000-000000000003")
COMPLAINT = UUID("a1000000-0000-0000-0000-000000000004")
ACTOR = RevisionActor("tester", "S-1-5-21-test")
RECEIVED = datetime(2026, 9, 20, 10, 30, tzinfo=UTC)


def _roster() -> RosterSnapshot:
    return RosterSnapshot(BASE, persons=(Person(PERSON, PersonProfile("아동")),))


def _initial_record(roster: RosterSnapshot) -> RevisionRecord:
    return RevisionRecord(
        RevisionMetadata(BASE, None, RECEIVED, None, ACTOR, "initial", RevisionSource.UI),
        roster_audit_changes(RosterSnapshot(UUID("a1000000-0000-0000-0000-000000000099")), roster),
    )


def test_complaint_validates_literal_content_and_builds_one_insert_only_revision():
    complaint = Complaint(
        COMPLAINT, PERSON, RECEIVED, IntakeChannel.PHONE, "  첫 줄\n둘째 줄  ", Meal.LUNCH
    )
    record = build_complaint_revision(
        _roster(), complaint, actor=ACTOR, recorded_at=RECEIVED, revision_id=NEXT
    )

    assert complaint.content == "첫 줄\n둘째 줄"
    assert len(record.changes) == 1
    change = record.changes[0]
    assert change.entity_kind == COMPLAINT_KIND
    assert change.before is None
    assert change.after == {
        "person_id": str(PERSON), "received_at": RECEIVED.isoformat(),
        "channel": "전화", "content": "첫 줄\n둘째 줄", "meal": "중식",
    }
    with pytest.raises(ValueError, match="비어 있을 수 없습니다"):
        Complaint(COMPLAINT, PERSON, RECEIVED, IntakeChannel.EMAIL, " \n ")
    with pytest.raises(KeyError, match="없는 사람"):
        build_complaint_revision(
            _roster(), Complaint(COMPLAINT, UUID(int=9), RECEIVED, IntakeChannel.OTHER, "내용"),
            actor=ACTOR, recorded_at=RECEIVED, revision_id=NEXT,
        )


def test_complaint_persists_and_reopens_as_snapshot_projection(tmp_path):
    roster = _roster()
    path = tmp_path / "complaints.sqlite3"
    with DatabaseSession.create(path, "password", "recovery") as session:
        session.append_revision(_initial_record(roster))
        record = build_complaint_revision(
            roster, Complaint(COMPLAINT, PERSON, RECEIVED, IntakeChannel.EMAIL, "메일 민원"),
            actor=ACTOR, recorded_at=RECEIVED, revision_id=NEXT,
        )
        session.append_revision(record)
    with DatabaseSession.open(path, "password") as session:
        complaints = complaints_from_snapshot(session.database.snapshot(NEXT))
        assert complaints == (
            Complaint(COMPLAINT, PERSON, RECEIVED, IntakeChannel.EMAIL, "메일 민원"),
        )
        assert session.database.snapshot(NEXT).entity(COMPLAINT_KIND, COMPLAINT) is not None


def test_complaints_from_snapshot_rejects_malformed_ledger_state():
    snapshot = HistoricalSnapshot(BASE, {EntityRef(COMPLAINT_KIND, COMPLAINT): {"content": "missing"}})
    with pytest.raises(ValueError, match="저장된 민원"):
        complaints_from_snapshot(snapshot)


def test_multi_meal_state_reopens_and_legacy_single_state_stays_supported(tmp_path):
    roster = _roster()
    multi = Complaint(COMPLAINT, PERSON, RECEIVED, IntakeChannel.OTHER, "복수", meals=(Meal.BREAKFAST, Meal.DINNER))
    record = build_complaint_revision(roster, multi, actor=ACTOR, recorded_at=RECEIVED, revision_id=NEXT)
    assert record.changes[0].after["meals"] == ("조식", "석식")
    assert multi.meal is None
    legacy = HistoricalSnapshot(BASE, {EntityRef(COMPLAINT_KIND, COMPLAINT): {
        "person_id": str(PERSON), "received_at": RECEIVED.isoformat(), "channel": "전화", "content": "기존", "meal": "중식",
    }})
    restored = complaints_from_snapshot(legacy)[0]
    assert restored.meal is Meal.LUNCH and restored.meals == (Meal.LUNCH,)
    with DatabaseSession.create(tmp_path / "multi.sqlite3", "password", "recovery") as session:
        session.append_revision(_initial_record(roster))
        session.append_revision(record)
        assert complaints_from_snapshot(session.database.snapshot(NEXT))[0].meals == (Meal.BREAKFAST, Meal.DINNER)


def test_batch_complaints_are_one_revision_and_reject_duplicate_people():
    other = UUID("a1000000-0000-0000-0000-000000000005")
    roster = RosterSnapshot(BASE, persons=(Person(PERSON, PersonProfile("첫째")), Person(other, PersonProfile("둘째"))))
    first = Complaint(COMPLAINT, PERSON, RECEIVED, IntakeChannel.PHONE, "내용", meals=(Meal.BREAKFAST,))
    second = Complaint(UUID("a1000000-0000-0000-0000-000000000006"), other, RECEIVED, IntakeChannel.PHONE, "내용")
    record = build_complaints_revision(roster, (first, second), actor=ACTOR, recorded_at=RECEIVED, revision_id=NEXT)
    assert len(record.changes) == 2
    with pytest.raises(ValueError, match="중복 접수"):
        build_complaints_revision(roster, (first, Complaint(UUID("a1000000-0000-0000-0000-000000000007"), PERSON, RECEIVED, IntakeChannel.PHONE, "내용")), actor=ACTOR, recorded_at=RECEIVED, revision_id=NEXT)
