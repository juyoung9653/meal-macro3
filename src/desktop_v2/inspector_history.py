"""Cached meal-change rows for the roster inspector."""

from typing import NamedTuple

from src.services.person_history import build_history_tables

from .ledger_reader import load_revision_chain


class MealHistoryRow(NamedTuple):
    text: str
    tooltip: str
    applied: str
    recorded: str
    reason: str = ""


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
                day = "지정 안 함" if applied is None else str(applied)
                before, after = values['이전 값'], values['변경 값']
                reason = ""
                if values['구분'] == '상태 변경':
                    old_parts = dict(line.split(': ', 1) for line in str(before).splitlines() if ': ' in line)
                    new_parts = dict(line.split(': ', 1) for line in str(after).splitlines() if ': ' in line)
                    old_status, new_status = old_parts.get('상태', ''), new_parts.get('상태', '')
                    status = new_status or f"{old_status} 기록 삭제"
                    if old_status and new_status and old_status != new_status:
                        status = f"{old_status} → {new_status}"
                    text = f"{values['식종']} · {status}"
                    day = (new_parts or old_parts).get('기간', day)
                    source_reason = str(after or before).partition('\n사유: ')[2]
                    reasons = [str(values.get('사유') or ''), source_reason]
                    reason = '\n'.join(dict.fromkeys(item for item in reasons if item and item != '배송 상태 변경'))
                else:
                    change = f"{before} → {after}" if before and after else str(after or before or '')
                    text = f"{values['식종']} · {values['구분']}  {change}"
                recorded = str(values.get("기록 시각(KST)") or "")
                tooltip = "\n".join(
                    f"{heading}: {value}"
                    for heading, value in values.items()
                    if heading != "사람 UUID"
                    and value is not None and value != ""
                )
                grouped.setdefault(values["사람 UUID"], []).append(
                    MealHistoryRow(text, tooltip, day, recorded, reason)
                )
            self._rows = {key: tuple(items) for key, items in grouped.items()}
            self._revision_id = roster.revision_id
        return self._rows.get(str(person_id), ())
