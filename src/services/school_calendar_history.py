"""Stored school-workbook rows for browsing accumulated vacation imports."""
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from src.domain.entitlements import SchoolLunchMark
from src.domain.roster import InclusiveDateRange
from src.domain.school_calendar import normalize_school_name
from src.importing.school_calendar import SchoolVacationRow


@dataclass(frozen=True)
class SchoolCalendarImportEntry:
    revision_id: UUID
    display_code: str
    recorded_at: datetime
    filename: str
    rows: tuple[SchoolVacationRow, ...]

    @property
    def period(self):
        return InclusiveDateRange(min(row.starts_on for row in self.rows),
                                  max(row.ends_on for row in self.rows))

    @property
    def label(self):
        period = self.period
        return f"{period.starts_on:%Y.%m.%d} ~ {period.ends_on:%Y.%m.%d} · {self.filename} · {self.display_code}"

    def school_rows(self, school_name):
        key = normalize_school_name(school_name)
        return tuple(row for row in self.rows if normalize_school_name(row.school_name) == key)


def school_calendar_import_history(database, revision_id):
    from types import SimpleNamespace
    from src.domain.revision_visibility import effective_history_revisions
    from .school_calendar_delete import deleted_import_ids
    active = frozenset(record.metadata.revision_id for record in
                       effective_history_revisions(tuple(SimpleNamespace(metadata=database.revision_metadata(item))
                           for item in database.branch_revision_ids(revision_id))))
    deleted = deleted_import_ids(database.snapshot(revision_id))
    entries = []
    for batch in database.import_batches(source_kind="school-calendar-xlsx"):
        if batch.revision_id not in active or str(batch.revision_id) in deleted:
            continue
        rows = tuple(SchoolVacationRow(
            int(item["source_row"]), str(item["school_name"]),
            date.fromisoformat(str(item["starts_on"])), SchoolLunchMark(item["starts_on_mark"]),
            date.fromisoformat(str(item["ends_on"])), SchoolLunchMark(item["ends_on_mark"]),
            item.get("note"),
        ) for item in batch.decisions.get("school_rows", ()))
        if rows:
            entries.append(SchoolCalendarImportEntry(
                batch.revision_id, database.revision_display_code(batch.revision_id),
                database.revision_metadata(batch.revision_id).recorded_at,
                batch.source_filename, rows,
            ))
    return tuple(entries)
