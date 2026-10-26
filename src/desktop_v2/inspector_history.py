"""Cached personal meal-status rows for the roster inspector."""


from src.services.person_history import build_history_tables

from .ledger_reader import load_revision_chain
from .meal_history import build_meal_history_entries_by_person
from .startup_loading import run_progress_task


class MealHistoryRow(tuple):
    """Three display values with an optional immutable cancellation identity."""
    def __new__(cls, meal, category, period, target=None, *, intake_channel="-", note=""):
        instance = super().__new__(cls, (meal, category, period))
        instance.target = target
        instance.intake_channel = intake_channel
        instance.note = note
        return instance

    meal = property(lambda self: self[0])
    category = property(lambda self: self[1])
    period = property(lambda self: self[2])


class InspectorMealHistory:
    """Read an ancestry once per displayed roster revision, not on each click."""

    def __init__(self, database):
        self._database = database
        self._revision_id = None
        self._rows = {}
        self._legacy_visible_ids = set()

    def rows_for(self, roster, person_id):
        self._ensure_prepared(roster)
        return self._rows.get(person_id, ())

    def has_visible_history(self, roster, person_id):
        """Keep archive eligibility on the broader pre-existing history criterion."""
        self._ensure_prepared(roster)
        return str(person_id) in self._legacy_visible_ids

    def _ensure_prepared(self, roster):
        if roster.revision_id != self._revision_id:
            run_progress_task(
                lambda: self._prepare(roster),
                title="식종 변경 이력을 불러오는 중입니다",
                message="선택 정보에 표시할 식종 변경 이력을 읽는 중입니다.",
            )

    def _prepare(self, roster):
        if roster.revision_id != self._revision_id:
            snapshot = self._database.snapshot(roster.revision_id)
            revisions = load_revision_chain(self._database, roster.revision_id)
            projected = build_meal_history_entries_by_person(revisions)
            headings, legacy_rows = build_history_tables(roster, snapshot, revisions)["식종 변경"]
            person_index = headings.index("사람 UUID")
            legacy_visible_ids = {row[person_index] for row in legacy_rows}
            self._rows = {
                person_id: tuple(MealHistoryRow(entry.row[0], entry.row[1], entry.row[3],
                    target=entry.target, intake_channel=entry.row[2], note=entry.row[4]) for entry in reversed(rows))
                for person_id, rows in projected.items()
            }
            self._legacy_visible_ids = legacy_visible_ids
            self._revision_id = roster.revision_id
