"""Actual ordinary import conflicts, grouped by field with per-person exceptions."""
from src.importing.values import import_value_rows, apply_import_values
from .lunch_value_review import LunchValueReviewDialog


class ImportValueReviewDialog(LunchValueReviewDialog):
    _rows_for = staticmethod(import_value_rows)

    @staticmethod
    def _apply_values(current, result, plan, choices):
        snapshot = apply_import_values(current, result, plan, choices).snapshot
        return snapshot.persons, snapshot.assignments

    def __init__(self, current, result, plan, parent=None):
        super().__init__(current, result, plan, parent)
        self.setWindowTitle("명단 불러오기 · 다른 값 처리")
