"""Explicit choices for ordinary import conflicts."""
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
        self.setWindowTitle("명단 가져오기 · 다른 값 선택")
        self.guide.setText(
            "기존 값 또는 가져온 값 셀을 눌러 반영할 값을 선택하세요. 여러 항목을 선택한 뒤 위 버튼으로 한꺼번에 고를 수도 있습니다.\n"
            "반영할 값은 직접 수정할 수 있습니다. 다른 값은 모두 선택해야 저장됩니다.\n"
            "학교·학년은 높은 쪽을 자동 선택하고, 원본 빈칸은 기존 값을 유지합니다. 판단하기 어려운 학교는 직접 선택하세요."
        )
        self._confirmed = set()
        for index, row in enumerate(self.rows):
            if not row.automatic:
                self.table.item(index, 1).setText(row.label + "\n선택 필요")
        self.table.cellClicked.connect(self._choose_cell)
        self.table.itemChanged.connect(self._confirm_edit)

    def _choose_cell(self, index, column):
        if column in (2, 3) and not self.rows[index].automatic:
            self.table.item(index, 4).setText(self.table.item(index, column).text())
            self._confirm(index)

    def _confirm_edit(self, item):
        if item.column() == 4 and not self.rows[item.row()].automatic:
            self._confirm(item.row())

    def _confirm(self, index):
        self._confirmed.add(self.rows[index].key)
        self.table.item(index, 1).setText(self.rows[index].label + "\n선택 완료")

    def _use_values(self, column):
        selected = {item.row() for item in self.table.selectedIndexes()}
        super()._use_values(column)
        for index in selected:
            if not self.rows[index].automatic:
                self._confirm(index)

    def accept(self):
        self.table.clearFocus()
        missing = [index for index, row in enumerate(self.rows)
                   if not row.automatic and row.key not in self._confirmed]
        if missing:
            self.error.setText(f"아직 선택하지 않은 항목이 {len(missing)}개 있습니다. 기존 값 또는 가져온 값을 선택해주세요.")
            self.table.setCurrentCell(missing[0], 4)
            return
        super().accept()
