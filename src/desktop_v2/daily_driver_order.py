"""Edit the daily document's driver order side by side for each meal."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QMenu, QMessageBox, QPushButton, QTableWidgetItem, QVBoxLayout,
)

from src.domain.delivery_routing import MEAL_ORDER
from src.domain.daily_driver_order import daily_driver_orders, saved_daily_driver_orders
from .delivery_routing import DriverOrderTable, _style


class DailyDriverOrderDialog(QDialog):
    def __init__(self, snapshot, routing, parent=None):
        super().__init__(parent)
        _style(self, "일일식수 기사 순서")
        self.resize(980, 540)
        self.setMinimumSize(820, 440)
        self._routing = routing
        self._saved = daily_driver_orders(snapshot, routing)
        self._overrides = saved_daily_driver_orders(snapshot, routing)
        self.tables = {}
        self.notes = {}
        layout = QVBoxLayout(self)
        guidance = QLabel("일일식수 문서에 표시되는 기사 순서입니다.\n"
                          "각 식종의 기사 행을 드래그하거나 위로·아래로 버튼으로 옮긴 뒤 저장하세요.")
        guidance.setWordWrap(True)
        layout.addWidget(guidance)
        lists = QHBoxLayout()
        for meal in MEAL_ORDER:
            column = QVBoxLayout()
            title = QLabel(meal.value)
            title.setObjectName("documentCardTitle")
            column.addWidget(title)
            note = QLabel()
            self.notes[meal] = note
            column.addWidget(note)
            table = DriverOrderTable(0)
            table.setObjectName(f"dailyDriverOrder_{meal.name.lower()}")
            table.setHorizontalHeaderLabels(("이름", "전화번호"))
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.horizontalHeader().setStretchLastSection(True)
            self.tables[meal] = table
            self._fill(meal, self._saved[meal])
            table.orderChanged.connect(self._refresh)
            column.addWidget(table)
            moves = QHBoxLayout()
            for text, step in (("위로 ↑", -1), ("아래로 ↓", 1)):
                button = QPushButton(text)
                button.setObjectName("quietAction")
                button.clicked.connect(lambda _checked=False, m=meal, s=step: self._move(m, s))
                moves.addWidget(button)
            column.addLayout(moves)
            copy = QPushButton("다른 식종에 순서 복사")
            copy.setObjectName("quietAction")
            menu = QMenu(copy)
            for target in MEAL_ORDER:
                if target != meal:
                    action = menu.addAction(f"{target.value}에 복사")
                    action.triggered.connect(lambda _checked=False, s=meal, t=target: self.copy_order(s, (t,)))
            action = menu.addAction("나머지 두 식종에 복사")
            action.triggered.connect(lambda _checked=False, s=meal: self.copy_order(s, tuple(m for m in MEAL_ORDER if m != s)))
            copy.setMenu(menu)
            column.addWidget(copy)
            lists.addLayout(column, 1)
        layout.addLayout(lists, 1)
        hint = QLabel("별도 순서를 저장하지 않은 식종은 기사 관리의 기본 순서를 따릅니다.\n"
                      "새 기사는 목록 끝에 추가되며, 문서에는 해당 기간의 출력 대상 기사만 표시됩니다.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.error = QLabel("등록된 기사가 없습니다. 기사 관리에서 먼저 등록해주세요." if not routing.drivers else "")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("저장")
        self.save_button.setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh()

    def _fill(self, meal, order):
        table = self.tables[meal]
        table.setRowCount(len(order))
        for row, driver_id in enumerate(order):
            driver = self._routing._drivers_by_id[driver_id]
            item = QTableWidgetItem(driver.name)
            item.setData(Qt.ItemDataRole.UserRole, driver_id)
            table.setItem(row, 0, item)
            table.setItem(row, 1, QTableWidgetItem(driver.telephone))

    def request(self):
        return {meal: tuple(table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                            for row in range(table.rowCount()))
                for meal, table in self.tables.items()}

    def _move(self, meal, step):
        table = self.tables[meal]
        row = table.currentRow()
        if row >= 0 and 0 <= row + step < table.rowCount():
            table.move_row(row, row - 1 if step < 0 else row + 2)

    def copy_order(self, source, targets):
        order = self.request()[source]
        for target in targets:
            self._fill(target, order)
        self._refresh()

    def _refresh(self):
        current = self.request()
        self.save_button.setEnabled(current != self._saved)
        for meal in MEAL_ORDER:
            self.notes[meal].setText("변경됨 · 저장 필요" if current[meal] != self._saved[meal]
                                     else "식종별 순서 사용" if meal in self._overrides else "기본 기사 순서 사용")

    def mark_saved(self):
        self._saved = self.request()

    def reject(self):
        if self.request() != self._saved:
            choice = QMessageBox.question(
                self, "일일식수 기사 순서", "변경한 식종별 기사 순서를 저장할까요?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if choice == QMessageBox.StandardButton.Save:
                self.accept()
                return
            if choice != QMessageBox.StandardButton.Discard:
                return
        super().reject()
