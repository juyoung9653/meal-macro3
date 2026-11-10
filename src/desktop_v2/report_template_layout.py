"""Select daily-report rows from the actual Excel template."""
from openpyxl import load_workbook
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from src.excel.report_template_layout import ReportTemplateLayout
from .theme import application_stylesheet
from .user_messages import user_error


class ReportTemplateLayoutDialog(QDialog):
    def __init__(self, path, selected=ReportTemplateLayout(), parent=None):
        super().__init__(parent)
        self._path = path
        self._selection = selected
        self.setWindowTitle("일일식수 본문·복사 행 설정")
        self.resize(980, 680)
        self.setStyleSheet(application_stylesheet())
        root = QVBoxLayout(self)
        note = QLabel("현재 양식의 aaa 시트입니다. 본문과 합계 위치를 직접 지정하세요.\n"
                      "표에서 행을 선택한 뒤 ‘선택 행 사용’을 누르거나 행 번호를 입력합니다. "
                      "기사가 본문 칸보다 많으면 복사 기준 행 바로 아래에 행을 삽입하고 이후 행을 아래로 밉니다.\n"
                      "A~I열의 항목 순서와 1~2행 날짜·식종 제목은 유지합니다.")
        note.setWordWrap(True)
        root.addWidget(note)
        workbook = load_workbook(path)
        try:
            sheet = workbook["aaa"]
            self.table = QTableWidget(sheet.max_row, 9)
            self.table.setHorizontalHeaderLabels(tuple("ABCDEFGHI"))
            self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            for row in sheet.iter_rows(max_col=9):
                for cell in row:
                    item = QTableWidgetItem("" if cell.value is None else str(cell.value))
                    item.setToolTip(item.text())
                    if cell.fill.fgColor.type == "rgb" and cell.fill.fill_type == "solid":
                        item.setBackground(QColor("#" + cell.fill.fgColor.rgb[-6:]))
                    self.table.setItem(cell.row - 1, cell.column - 1, item)
            self.table.resizeColumnsToContents()
            self.table.setColumnWidth(0, 160)
            for column in range(1, 7):
                self.table.setColumnWidth(column, 66)
            self.table.setColumnWidth(7, 220)
            self.table.setColumnWidth(8, 100)
        finally:
            workbook.close()
        root.addWidget(self.table, 1)
        form = QFormLayout()
        self.rows = {}
        for key, label in (("body_start", "본문 시작 행"),
                           ("body_end", "본문 끝 행"), ("copy_row", "복사 기준 행"), ("total_row", "합계 행")):
            spin = QSpinBox()
            spin.setRange(3, max(self.table.rowCount(), selected.total_row))
            spin.setSuffix("행")
            spin.setValue(getattr(selected, key))
            spin.setAccessibleName(label)
            self.rows[key] = spin
            line = QHBoxLayout()
            line.addWidget(spin)
            use = QPushButton("선택 행 사용")
            use.setObjectName("quietAction")
            use.clicked.connect(lambda _checked=False, spin=spin: self._use_row(spin))
            line.addWidget(use)
            line.addStretch()
            form.addRow(label, line)
        root.addLayout(form)
        self.error = QLabel()
        self.error.setWordWrap(True)
        root.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("행 설정 적용")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.table.selectRow(min(selected.copy_row, self.table.rowCount()) - 1)

    def _use_row(self, spin):
        row = self.table.currentRow() + 1
        if row >= 3:
            spin.setValue(row)

    def selection(self):
        return self._selection

    def accept(self):
        try:
            selection = ReportTemplateLayout(**{key: spin.value() for key, spin in self.rows.items()})
            workbook = load_workbook(self._path)
            try:
                selection.validate(workbook["aaa"])
            finally:
                workbook.close()
        except Exception as error:
            self.error.setText(user_error(error))
            return
        self._selection = selection
        super().accept()
