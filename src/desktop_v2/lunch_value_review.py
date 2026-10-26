"""Side-by-side, editable vacation lunch import values."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHeaderView, QLabel, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QHBoxLayout, QPushButton,
)
from src.importing.lunch_values import lunch_value_rows, apply_lunch_values
from .theme import application_stylesheet, register_application_fonts


def display(value):
    return "" if value is None else str(value)


class LunchValueReviewDialog(QDialog):
    def __init__(self, current, result, plan, parent=None):
        super().__init__(parent)
        self._current, self._result, self._plan = current, result, plan
        self.rows = lunch_value_rows(current, result, plan)
        self._choices = {}
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("방학 중식 명단 · 변경 값 확인")
        self.resize(1180, 680)
        self.setMinimumSize(900, 520)
        layout = QVBoxLayout(self)
        guide = QLabel(
            "기존 값과 가져온 값을 비교하고 ‘반영할 값’을 더블클릭해 수정하세요.\n"
            "학교·학년은 학교 단계 → 학년 순으로 높은 값을 자동 선택합니다. "
            "같은 단계의 다른 학교나 판단하기 어려운 값은 직접 확인합니다.\n"
            "가져온 값이 빈칸이면 기존 값을 제안합니다. 반영할 값을 지우면 해당 값을 비웁니다."
        )
        guide.setWordWrap(True)
        layout.addWidget(guide)
        tools = QHBoxLayout()
        for title, column in (("선택 항목: 기존 값 사용", 2), ("선택 항목: 가져온 값 사용", 3)):
            button = QPushButton(title)
            button.clicked.connect(lambda _checked=False, c=column: self._use_values(c))
            tools.addWidget(button)
        tools.addStretch()
        layout.addLayout(tools)
        self.table = QTableWidget(len(self.rows), 5)
        self.table.setHorizontalHeaderLabels(("대상자 · 원본 위치", "항목", "기존 값", "가져온 값", "반영할 값 (수정 가능)"))
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(76)
        self.table.setSortingEnabled(False)
        for index, row in enumerate(self.rows):
            for column, value in enumerate((
                f"{row.name} · {row.phone}\n{row.source}",
                row.label + ("\n자동 선택" if row.automatic else ""),
                display(row.before), display(row.incoming), display(row.proposed),
            )):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column != 4 or row.automatic:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(index, column, item)
        layout.addWidget(self.table, 1)
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("확인한 값으로 가져오기")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("quietAction")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _use_values(self, column):
        for index in {i.row() for i in self.table.selectedIndexes()}:
            if not self.rows[index].automatic:
                self.table.item(index, 4).setText(self.table.item(index, column).text())

    def choices(self):
        return dict(self._choices)

    def accept(self):
        # Finish any active cell editor before collecting values.
        self.table.clearFocus()
        try:
            choices = {}
            for index, row in enumerate(self.rows):
                if row.automatic:
                    continue
                try:
                    choices[row.key] = row.parse(self.table.item(index, 4).text())
                except (ValueError, TypeError) as error:
                    raise ValueError(f"{row.name} · {row.label}: {error}") from error
            persons, _ = apply_lunch_values(self._current, self._result, self._plan, choices)
            definitions = {d.definition_id: d for d in self._result.snapshot.custom_fields}
            for person in persons:
                for key, value in person.profile.custom_values.items():
                    definitions[key].validate_value(value)
        except (ValueError, TypeError) as error:
            self.error.setText(str(error))
            return
        self._choices = choices
        super().accept()
