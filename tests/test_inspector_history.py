from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

from src.desktop_v2.inspector_history import InspectorMealHistory
from src.desktop_v2.meal_history import build_person_meal_history_rows
from src.domain.projections import ENROLLMENT_KIND, status_event_audit_change
from src.domain.revisions import (
    AuditChange,
    ChangeOperation,
    HistoricalSnapshot,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.domain.roster import (
    InclusiveDateRange,
    Meal,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.status import MealStatus, StatusEvent


def test_inspector_history_matches_person_dialog_status_rows_and_refreshes_revision():
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
        event = StatusEvent(
            uuid4(), revision_id, person.person_id, Meal.DINNER, MealStatus.PAUSED,
            InclusiveDateRange(date(2026, 9, 22), date(2026, 9, 30)),
            metadata.recorded_at,
        )
        records[revision_id] = RevisionRecord(metadata, (
            status_event_audit_change(event),
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
    assert len(rows) == 7  # One row per status event; all remain available to scroll.
    expected = build_person_meal_history_rows(tuple(records.values()), first.person_id)
    assert tuple(tuple(row) for row in rows) == tuple((row[0], row[1], row[3]) for row in reversed(expected))
    assert rows[0].meal == "석식"
    assert rows[0].category == "일시중지"
    assert rows[0].period == "2026-09-22 ~ 2026-09-30"
    assert len(rows[0]) == 3
    assert len(history.rows_for(roster, second.person_id)) == 1
    assert history.rows_for(roster, uuid4()) == ()
    assert database.reads == 1

    previous_id = records[parent].metadata.parent_revision_id
    earlier = replace(roster, revision_id=previous_id)
    assert history.rows_for(earlier, second.person_id) == ()
    assert database.reads == 2


def test_nonstatus_history_stays_available_for_archive_without_inspector_card():
    person = Person(uuid4(), PersonProfile("가입 이력 대상"))
    revision_id = uuid4()
    roster = RosterSnapshot(revision_id, persons=(person,))
    record = RevisionRecord(
        RevisionMetadata(
            revision_id, None, datetime(2026, 9, 22, tzinfo=UTC), None,
            RevisionActor("tester", "sid"), "가입 수정", RevisionSource.UI,
        ),
        (AuditChange(
            ENROLLMENT_KIND, uuid4(), ChangeOperation.INSERT, None,
            {"person_id": str(person.person_id), "meal": "중식", "periods": (
                {"starts_on": "2026-09-22", "ends_on": None},
            )},
        ),),
    )

    class Database:
        def snapshot(self, revision):
            return HistoricalSnapshot(revision, {})

        def revision(self, revision):
            return record

    history = InspectorMealHistory(Database())
    assert history.rows_for(roster, person.person_id) == ()
    assert history.has_visible_history(roster, person.person_id)


def test_inspector_cache_load_is_responsive_and_failed_read_can_retry(monkeypatch):
    import os
    from threading import Event, get_ident
    from types import SimpleNamespace
    import pytest
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from src.desktop_v2 import inspector_history as module
    app = QApplication.instance() or QApplication([])
    entered, released = Event(), Event()
    gui = get_ident()
    reads, ticks = [], []
    fail = [True]
    roster = RosterSnapshot(uuid4())

    def snapshot(revision):
        reads.append(get_ident())
        entered.set()
        assert released.wait(5)
        if fail[0]:
            raise OSError("history unavailable")
        return HistoricalSnapshot(revision, {})

    history = InspectorMealHistory(SimpleNamespace(snapshot=snapshot))
    monkeypatch.setattr(module, "load_revision_chain", lambda *args: ())
    monkeypatch.setattr(module, "build_history_tables", lambda *args: {"식종 변경": (("사람 UUID",), ())})
    def tick():
        if entered.is_set() and not released.is_set():
            ticks.append(get_ident() == gui)
            released.set()
    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(10)
    try:
        with pytest.raises(OSError, match="history unavailable"):
            history.rows_for(roster, uuid4())
        fail[0] = False
        assert history.rows_for(roster, uuid4()) == ()
        assert history.rows_for(roster, uuid4()) == ()
    finally:
        timer.stop()
        released.set()
    assert len(reads) == 2 and all(thread != gui for thread in reads)
    assert ticks == [True]  # The GUI processed a timer while the database read waited.
