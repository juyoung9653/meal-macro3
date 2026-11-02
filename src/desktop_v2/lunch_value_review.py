"""Review actual value conflicts by field, showing all conflicting people in the selected field."""

from .user_messages import user_error
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHeaderView, QLabel, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QHBoxLayout, QPushButton, QWidget,
)
from src.importing.lunch_values import lunch_value_rows, apply_lunch_values
from src.importing.values import conflicting_value_rows
from .theme import application_stylesheet, register_application_fonts


def display(value):
    return "" if value is None else str(value)


class LunchValueReviewDialog(QDialog):
    export_requested = Signal(object)
    _rows_for = staticmethod(lunch_value_rows)
    _apply_values = staticmethod(apply_lunch_values)

    def __init__(self, current, result, plan, parent=None):
        super().__init__(parent)
        self._current, self._result, self._plan = current, result, plan
        self._all_rows = self._rows_for(current, result, plan)
        self.rows = conflicting_value_rows(self._all_rows)
        self._choices, self._groups, self._group_combos = {}, {}, {}
        self._confirmed = set()
        self._exception_rows = set()
        self._group_defaults = {}
        self._detail_field = None
        self._batch = False
        for index, row in enumerate(self.rows):
            self._groups.setdefault(row.field, []).append(index)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("방학 중식 명단 · 다른 값 처리")
        screen = self.screen().availableGeometry()
        self.setMinimumSize(min(640, screen.width()), min(440, screen.height()))
        self.resize(min(1120, screen.width() - 32), min(760, screen.height() - 48))
        layout = QVBoxLayout(self)
        self.guide = QLabel(
            "DB와 다른 값만 항목별로 모았습니다. 항목별 반영 방향을 정하고 예외인 사람만 따로 수정하세요.\n"
            "파일이 빈칸이면 기존 값을 유지하고, DB가 빈칸이면 파일 값으로 채웁니다. 학교·학년은 높은 학교 단계·학년을 우선하고, 같은 학교의 약칭과 정식 명칭 중 정식 명칭을 씁니다.")
        self.guide.setWordWrap(True)
        layout.addWidget(self.guide)
        tools = QHBoxLayout()
        for title, column in (("모두 가져온 값 사용", 3), ("모두 기존 값 유지", 2)):
            button = QPushButton(title)
            button.setAutoDefault(False)
            button.clicked.connect(lambda _checked=False, c=column: self._use_all_values(c))
            tools.addWidget(button)
        tools.addStretch()
        self.export_button = QPushButton("불일치 Excel 내보내기")
        self.export_button.setAutoDefault(False)
        self.export_button.setEnabled(bool(self.rows))
        self.export_button.setToolTip("모든 불일치를 DB 값과 원본 명단 값으로 비교해 저장합니다. 현재 선택과 DB는 바뀌지 않습니다.")
        self.export_button.clicked.connect(self._export_conflicts)
        tools.addWidget(self.export_button)
        layout.addLayout(tools)
        self.group_table = QTableWidget(len(self._groups), 4)
        self.group_table.setHorizontalHeaderLabels(("다른 항목", "대상", "반영 방향", "예외 수정"))
        self.group_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.group_table.verticalHeader().hide()
        self.group_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            self.group_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for group_index, (field, indexes) in enumerate(self._groups.items()):
            row = self.rows[indexes[0]]
            self.group_table.setItem(group_index, 0, QTableWidgetItem(row.label))
            self.group_table.setItem(group_index, 1, QTableWidgetItem())
            combo = QComboBox()
            combo.setAccessibleName(row.label + " 일괄 반영 방향")
            combo.addItem("반영 방향 선택", None)
            combo.addItem("가져온 값 사용", "incoming")
            combo.addItem("기존 값 유지", "before")
            combo.addItem("예외 설정", "custom")
            self._group_combos[field] = combo
            self.group_table.setCellWidget(group_index, 2, combo)
            button = QPushButton("사람별 예외 보기")
            button.setAutoDefault(False)
            button.clicked.connect(lambda _checked=False, key=field: self._show_group_details(key))
            self.group_table.setCellWidget(group_index, 3, button)
            combo.currentIndexChanged.connect(lambda _index, key=field: self._group_changed(key))
            combo.ensurePolished()
            button.ensurePolished()
            self.group_table.setRowHeight(group_index, max(combo.sizeHint().height(), button.sizeHint().height()) + 2)
        layout.addWidget(self.group_table, 1)

        self.detail_panel = QWidget()
        detail_layout = QVBoxLayout(self.detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        self.exception_note = QLabel("이 항목에서 값이 다른 사람을 모두 표시합니다. 예외인 사람만 반영할 값을 수정하세요.")
        self.exception_note.setWordWrap(True)
        detail_tools = QHBoxLayout()
        self.detail_title = QLabel()
        self.detail_title.setWordWrap(True)
        detail_layout.addWidget(self.detail_title)
        detail_layout.addWidget(self.exception_note)
        detail_tools.addStretch()
        for title, column in (("선택한 사람: 기존 값", 2), ("선택한 사람: 가져온 값", 3)):
            button = QPushButton(title)
            button.setAutoDefault(False)
            button.clicked.connect(lambda _checked=False, c=column: self._use_values(c))
            detail_tools.addWidget(button)
        remove_button = QPushButton("예외 해제")
        remove_button.setAutoDefault(False)
        remove_button.clicked.connect(self._remove_exceptions)
        detail_tools.addWidget(remove_button)
        hide_button = QPushButton("접기")
        hide_button.setAutoDefault(False)
        hide_button.clicked.connect(self.detail_panel.hide)
        detail_tools.addWidget(hide_button)
        detail_layout.addLayout(detail_tools)
        self.table = QTableWidget(len(self.rows), 5)
        self.table.setHorizontalHeaderLabels(("대상자", "항목", "기존 값", "가져온 값", "반영할 값"))
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(66)
        self.table.verticalHeader().hide()
        self.table.setSortingEnabled(False)
        for index, row in enumerate(self.rows):
            for column, value in enumerate((f"{row.name} · {row.phone}", row.label + "\n선택 필요",
                                             display(row.before), display(row.incoming), display(row.proposed))):
                item = QTableWidgetItem(value)
                item.setToolTip(value + ("\n" + row.source if column == 0 else ""))
                if column != 4:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(index, column, item)
        self.table.cellClicked.connect(self._choose_cell)
        self.table.itemChanged.connect(self._confirm_edit)
        detail_layout.addWidget(self.table, 1)
        layout.addWidget(self.detail_panel, 2)
        self.detail_panel.hide()
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("선택한 값으로 불러오기")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("quietAction")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_groups()

    def _export_conflicts(self):
        from .import_conflict_export import conflict_export_snapshot
        try:
            snapshot = conflict_export_snapshot(self._result, self._plan, self.rows)
            self.export_requested.emit(snapshot)
        except Exception as error:
            self.error.setText(user_error(error))

    def _refresh_groups(self):
        for group_index, (field, indexes) in enumerate(self._groups.items()):
            people = len({self.rows[index].person_id for index in indexes})
            pending = sum(self.rows[index].key not in self._confirmed for index in indexes)
            self.group_table.item(group_index, 1).setText(f"{people}명" + (f" · 미선택 {pending}" if pending else " · 선택 완료"))

    def _group_changed(self, field):
        choice = self._group_combos[field].currentData()
        if choice == "custom":
            self._show_group_details(field)
        elif choice is None:
            self._group_defaults.pop(field, None)
            for index in self._groups[field]:
                self._confirmed.discard(self.rows[index].key)
                self.table.item(index, 1).setText(self.rows[index].label + "\n선택 필요")
            self._refresh_groups()
        else:
            self._set_group_values(field, 3 if choice == "incoming" else 2)

    def _set_group_values(self, field, column):
        self._group_defaults[field] = column
        self._exception_rows.difference_update(self._groups[field])
        self._batch = True
        try:
            for index in self._groups[field]:
                self._choose_cell(index, column)
            combo = self._group_combos[field]
            combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData("incoming" if column == 3 else "before"))
            combo.blockSignals(False)
        finally:
            self._batch = False
        self._refresh_groups()
        self._refresh_exception_rows()

    def _use_all_values(self, column):
        for field in self._groups:
            self._set_group_values(field, column)

    def _show_group_details(self, field):
        self._detail_field = field
        self.detail_title.setText(self.rows[self._groups[field][0]].label + " · 예외인 사람만 수정")
        self.table.clearSelection()
        self._refresh_exception_rows()
        self.detail_panel.show()

    def _refresh_exception_rows(self):
        for index, row in enumerate(self.rows):
            self.table.setRowHidden(index, row.field != self._detail_field)

    def _remove_exceptions(self):
        selected = {item.row() for item in self.table.selectedIndexes() if not self.table.isRowHidden(item.row())}
        self._batch = True
        try:
            for index in selected:
                row = self.rows[index]
                column = self._group_defaults.get(row.field)
                self._choose_cell(index, column or 3)
                if column is None:
                    self._confirmed.discard(row.key)
                    self.table.item(index, 1).setText(row.label + "\n선택 필요")
                self._exception_rows.discard(index)
        finally:
            self._batch = False
        for field in {self.rows[index].field for index in selected}:
            if not self._exception_rows.intersection(self._groups[field]):
                combo = self._group_combos[field]
                column = self._group_defaults.get(field)
                combo.blockSignals(True)
                combo.setCurrentIndex(combo.findData(None if column is None else "incoming" if column == 3 else "before"))
                combo.blockSignals(False)
        self._refresh_groups()
        self._refresh_exception_rows()

    def _choose_cell(self, index, column):
        if column in (2, 3):
            self.table.item(index, 4).setText(self.table.item(index, column).text())
            self._confirm(index)

    def _confirm_edit(self, item):
        if item.column() == 4:
            self._confirm(item.row())

    def _confirm(self, index):
        row = self.rows[index]
        self._confirmed.add(row.key)
        self.table.item(index, 1).setText(row.label + "\n선택 완료")
        if not self._batch:
            self._exception_rows.add(index)
            self._refresh_exception_rows()
            combo = self._group_combos[row.field]
            combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData("custom"))
            combo.blockSignals(False)
            self._refresh_groups()

    def _use_values(self, column):
        for index in {item.row() for item in self.table.selectedIndexes() if not self.table.isRowHidden(item.row())}:
            self._choose_cell(index, column)

    def choices(self):
        return dict(self._choices)

    def accept(self):
        self.table.clearFocus()
        missing = [index for index, row in enumerate(self.rows) if row.key not in self._confirmed]
        if missing:
            self.error.setText(f"아직 선택하지 않은 항목이 {len(missing)}개 있습니다. 항목별 반영 방향을 선택해주세요.")
            return
        try:
            choices = {row.key: row.proposed for row in self._all_rows if not row.automatic}
            for index, row in enumerate(self.rows):
                try:
                    choices[row.key] = row.parse(self.table.item(index, 4).text())
                except (ValueError, TypeError) as error:
                    self._exception_rows.add(index)
                    self._show_group_details(row.field)
                    self.table.setCurrentCell(index, 4)
                    raise ValueError(f"{row.name} · {row.label}: {user_error(error)}") from error
            persons, _ = self._apply_values(self._current, self._result, self._plan, choices)
            definitions = {d.definition_id: d for d in self._result.snapshot.custom_fields}
            for person in persons:
                for key, value in person.profile.custom_values.items():
                    definitions[key].validate_value(value)
        except (ValueError, TypeError) as error:
            self.error.setText(user_error(error))
            return
        self._choices = choices
        super().accept()
