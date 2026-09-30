"""Cached meal-change rows for the roster inspector."""

from src.services.person_history import build_history_tables

from .ledger_reader import load_revision_chain


class InspectorMealHistory:
    """Read an ancestry once per displayed roster revision, not on each click."""

    def __init__(self, database):
        self._database = database
        self._revision_id = None
        self._rows = {}

    def rows_for(self, roster, person_id):
        if roster.revision_id != self._revision_id:
            snapshot = self._database.snapshot(roster.revision_id)
            revisions = load_revision_chain(self._database, roster.revision_id)
            headings, rows = build_history_tables(roster, snapshot, revisions)["식종 변경"]
            grouped = {}
            for row in reversed(rows):
                values = dict(zip(headings, row, strict=True))
                applied = values["적용일"]
                day = "적용일 없음" if applied is None else str(applied)
                text = (
                    f"{values['식종']} · {values['구분']}  "
                    f"{values['이전 값'] or '없음'} → {values['변경 값'] or '없음'} · {day}"
                )
                tooltip = "\n".join(
                    f"{heading}: {value}"
                    for heading, value in values.items()
                    if heading not in {"사람 UUID", "기록 시각(KST)"}
                    and value is not None and value != ""
                )
                grouped.setdefault(values["사람 UUID"], []).append((text, tooltip))
            self._rows = {key: tuple(items) for key, items in grouped.items()}
            self._revision_id = roster.revision_id
        return self._rows.get(str(person_id), ())
