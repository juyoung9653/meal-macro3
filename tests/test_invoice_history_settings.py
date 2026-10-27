"""Persist counting choices and write history into a chosen template column."""

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import openpyxl
import pytest

from src.domain.invoice_settings import InvoiceSettings, build_invoice_settings_revision, invoice_settings_from_snapshot
from src.domain.projections import roster_entity_states, roster_from_historical_snapshot, status_event_state
from src.domain.revisions import AuditChange, ChangeOperation, EntityRef, HistoricalSnapshot, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import InclusiveDateRange, Meal
from src.domain.status import MealStatus, StatusEvent
from src.services.sqlite_document_service import prepare_monthly_document_data
from src.services.document_column_mappings import mapping_editor_spec
from tests.test_sqlite_delivery_report_service import _snapshot


def history_source(status, starts_on, ends_on):
    source, people = _snapshot(meals=(Meal.DINNER,))
    roster = roster_from_historical_snapshot(source)
    roster = replace(roster, persons=(replace(roster.persons[0], profile=replace(roster.persons[0].profile, note="명단의 일반 메모")),))
    entities = dict(source.entities)
    entities.update(roster_entity_states(roster))
    metadata = lambda revision, parent: RevisionMetadata(revision, parent, datetime(2026, 10, 1, tzinfo=UTC), None, RevisionActor("test", "sid"), "합성 검증", RevisionSource.UI)
    original = RevisionRecord(metadata(source.revision_id, None), tuple(
        AuditChange(ref.entity_kind, ref.entity_id, ChangeOperation.INSERT, None, value) for ref, value in entities.items()
    ))
    revision = uuid4()
    event = StatusEvent(uuid4(), revision, people[0], Meal.DINNER, status, InclusiveDateRange(starts_on, ends_on), datetime(2026, 10, 2, tzinfo=UTC))
    state = status_event_state(event)
    entities[EntityRef("status.event", event.event_id)] = state
    record = RevisionRecord(metadata(revision, source.revision_id), (AuditChange("status.event", event.event_id, ChangeOperation.INSERT, None, state),))
    return HistoricalSnapshot(revision, entities), (original, record)


@pytest.mark.parametrize("status, start, end, text", [
    (MealStatus.DELIVERY, date(2026, 10, 23), None, "석식: 10/23~ 배송"),
    (MealStatus.STOPPED, date(2026, 10, 29), None, "석식: 10/29~ 중지"),
    (MealStatus.PAUSED, date(2026, 10, 23), date(2026, 10, 29), "석식: 10/23~10/29 일시중지"),
])
def test_invoice_note_uses_history_not_roster_memo(status, start, end, text):
    source, records = history_source(status, start, end)
    prepared = prepare_monthly_document_data(source, date(2026, 10, 1), date(2026, 10, 31), (Meal.DINNER,), revisions=records)
    assert prepared.batch.deliveries[0].note == text
    assert "명단의 일반 메모" not in prepared.batch.deliveries[0].note
    # Removing a cancelled event from the source must remove its note, too.
    cancelled = HistoricalSnapshot(source.revision_id, {ref: value for ref, value in source.entities.items() if ref.entity_kind != "status.event"})
    assert prepare_monthly_document_data(cancelled, date(2026, 10, 1), date(2026, 10, 31), (Meal.DINNER,), revisions=records).batch.deliveries[0].note == ""


def test_history_destination_is_an_editable_default(tmp_path):
    from src.core.settings import get_settings
    from src.generators.invoice_generator import make_invoice_publication
    from src.templates.config import get_template_data_start_row, get_template_sheet_name, read_template_columns
    source, records = history_source(MealStatus.STOPPED, date(2026, 10, 29), None)
    prepared = prepare_monthly_document_data(source, date(2026, 10, 1), date(2026, 10, 31), (Meal.DINNER,), revisions=records)
    spec = mapping_editor_spec("급식", "invoice", history_column="비고")
    assert "비고" not in spec.locked_targets
    assert "비고" in spec.selectable_source_options
    assert spec.defaults["비고"] == "__auto_history"
    template = get_settings().get_invoice_template_path("급식")
    path = tmp_path / "history.xlsx"
    make_invoice_publication(prepared.batch, {}, str(path), template, history_note_column="비고")
    sheet = get_template_sheet_name(False, "급식")
    columns = read_template_columns(template, sheet_name=sheet)
    book = openpyxl.load_workbook(path)
    try:
        assert book[sheet].cell(get_template_data_start_row(), columns.index("비고") + 1).value == "석식: 10/29~ 중지"
    finally:
        book.close()
    changed = tmp_path / "changed-history.xlsx"
    make_invoice_publication(prepared.batch, {"조식": "__auto_history", "비고": "비고"}, str(changed), template, history_note_column="비고")
    book = openpyxl.load_workbook(changed)
    try:
        assert book[sheet].cell(get_template_data_start_row(), columns.index("조식") + 1).value == "석식: 10/29~ 중지"
        assert book[sheet].cell(get_template_data_start_row(), columns.index("비고") + 1).value == "명단의 일반 메모"
    finally:
        book.close()


def test_invoice_settings_reopen_noop_and_historical_source(tmp_path):
    from src.storage.database_session import DatabaseSession
    from tests.test_delivery_routing import initialize
    source, _ = _snapshot()
    path = tmp_path / "settings.sqlite3"
    selected = InvoiceSettings(False, "비고", "")
    with DatabaseSession.create(path, "password") as session:
        initialize(session, roster_from_historical_snapshot(source))
        old = session.database.snapshot(source.revision_id)
        record = build_invoice_settings_revision(old, selected, actor=RevisionActor("test", "sid"), recorded_at=datetime.now(UTC), revision_id=uuid4())
        session.append_revision(record)
        current = session.database.snapshot(record.metadata.revision_id)
        assert invoice_settings_from_snapshot(current) == selected
        assert invoice_settings_from_snapshot(old).include_next_month
        assert build_invoice_settings_revision(current, selected, actor=RevisionActor("test", "sid"), recorded_at=datetime.now(UTC), revision_id=uuid4()) is None
    with DatabaseSession.open(path, "password") as session:
        assert invoice_settings_from_snapshot(session.database.snapshot(record.metadata.revision_id)) == selected
