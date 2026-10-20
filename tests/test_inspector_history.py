from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from src.desktop_v2.inspector_history import InspectorMealHistory
from src.domain.projections import STATUS_EVENT_KIND
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
    Person,
    PersonProfile,
    RosterSnapshot,
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
    assert len(rows) == 7  # One row per status event; all remain available to scroll.
    assert "석식" in rows[0].text
    assert rows[0].text == "석식 · 배송 → 일시중지"
    assert "종료일=" not in rows[0].text and "ends_on" not in rows[0].tooltip
    assert rows[0].applied == "시작일·종료일 미지정"
    assert rows[0].reason == "변경 6"
    assert rows[0].recorded.startswith("2026-09-22")
    assert "변경 6" in rows[0][1] and "변경 0" in rows[-1][1]
    assert len(history.rows_for(roster, second.person_id)) == 1
    assert history.rows_for(roster, uuid4()) == ()
    assert database.reads == 1

    previous_id = records[parent].metadata.parent_revision_id
    earlier = replace(roster, revision_id=previous_id)
    assert history.rows_for(earlier, second.person_id) == ()
    assert database.reads == 2


def test_inspector_cache_load_is_responsive_and_failed_read_can_retry(monkeypatch):
    import os
    from threading import Event, get_ident
    from types import SimpleNamespace
    import pytest
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from src.desktop_v2 import inspector_history as module
    from src.desktop_v2.startup_loading import StartupLoadingDialog
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
    monkeypatch.setattr(module, "build_history_tables", lambda *args: {"식종 변경": ((), ())})
    def tick():
        if entered.is_set() and not released.is_set():
            ticks.append(isinstance(app.activeModalWidget(), StartupLoadingDialog))
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
    assert ticks == [True]
