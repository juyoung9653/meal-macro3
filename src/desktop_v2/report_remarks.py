"""Priority columns and delivery-form names for daily-report remarks."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
)
from src.domain.daily_report_choices import ReportRemarkOptions
from .theme import application_stylesheet, register_application_fonts


class ReportRemarksDialog(QDialog):
    def __init__(self, options, columns, forms, parent=None):
        super().__init__(parent)
        self.setWindowTitle("일일식수 비고 설정")
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.resize(640, 650)
        self._labels = dict(columns)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(10)
        title = QLabel("비고에 추가할 칼럼")
        title.setObjectName("sectionLabel")
        root.addWidget(title)
        hint = QLabel("1순위가 비어 있으면 다음 순위의 값을 사용합니다. 값이 있는 첫 칼럼만 성명과 함께 표시합니다.")
        hint.setWordWrap(True)
        root.addWidget(hint)
        self.priority_list = QListWidget()
        self.priority_list.setAccessibleName("비고 칼럼 선택과 우선순위")
        self.priority_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.priority_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.priority_list.model().rowsMoved.connect(self._renumber)
        root.addWidget(self.priority_list, 1)
        for key in dict.fromkeys((*options.columns, *(key for key, _ in columns))):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if key in options.columns else Qt.CheckState.Unchecked)
            self.priority_list.addItem(item)
        self.priority_list.itemChanged.connect(self._renumber)
        self._renumber()
        actions = QHBoxLayout()
        for text, callback in (("위로", lambda: self._move(-1)), ("아래로", lambda: self._move(1))):
            button = QPushButton(text)
            button.setObjectName("quietAction")
            button.clicked.connect(callback)
            actions.addWidget(button)
        tip = QLabel("사용할 칼럼만 체크 · 이름을 드래그해 순서 변경")
        tip.setObjectName("muted")
        actions.addWidget(tip, 1)
        root.addLayout(actions)
        position_row = QHBoxLayout()
        position_row.addWidget(QLabel("추가 내용 위치"))
        self.position_combo = QComboBox()
        self.position_combo.addItem("기존 비고 위", "above")
        self.position_combo.addItem("기존 비고 아래", "below")
        self.position_combo.setCurrentIndex(self.position_combo.findData(options.position))
        position_row.addWidget(self.position_combo, 1)
        root.addLayout(position_row)
        heading = QLabel("별도로 표시할 배송 형태 (여러 개 선택 가능)")
        heading.setObjectName("sectionLabel")
        root.addWidget(heading)
        example = QLabel("예: BOX 별도\n(홍길동, 김철수)\n선택한 날짜·식종의 실제 배송 대상만 표시합니다.")
        root.addWidget(example)
        self.include_excluded = QCheckBox("제외한 배송도 표시")
        self.include_excluded.setChecked(options.include_excluded_deliveries)
        self.include_excluded.setToolTip("문서에 포함할 배송 형태에서 제외했더라도, 아래에서 선택한 형태의 실제 배송 대상은 비고에 표시합니다. 가구 수와 식수에는 더하지 않습니다.")
        root.addWidget(self.include_excluded)
        self.form_list = QListWidget()
        self.form_list.setAccessibleName("별도로 표시할 배송 형태")
        root.addWidget(self.form_list, 1)
        available = tuple(form for form in forms if form is not None)
        for form in dict.fromkeys((*available, *options.separate_forms)):
            label = form if form in available else f"{form} (현재 원본에 없음)"
            item = QListWidgetItem(label, self.form_list)
            item.setData(Qt.ItemDataRole.UserRole, form)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if form in options.separate_forms else Qt.CheckState.Unchecked)
        if not self.form_list.count():
            example.setText("등록된 별도 배송 형태가 없습니다. 배송 설정에서 형태를 입력하면 선택할 수 있습니다.")
            example.setWordWrap(True)
        self.error = QLabel()
        self.error.setObjectName("errorMessage")
        self.error.setWordWrap(True)
        self.error.hide()
        root.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("적용")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("quietAction")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _renumber(self, *_args):
        self.priority_list.blockSignals(True)
        try:
            priority = 0
            for index in range(self.priority_list.count()):
                item = self.priority_list.item(index)
                key = item.data(Qt.ItemDataRole.UserRole)
                selected = item.checkState() == Qt.CheckState.Checked
                if selected:
                    priority += 1
                prefix = f"{priority}순위 · " if selected else ""
                item.setText(prefix + self._labels.get(key, "저장된 칼럼 (현재 원본에 없음)"))
        finally:
            self.priority_list.blockSignals(False)

    def _move(self, delta):
        row = self.priority_list.currentRow()
        target = row + delta
        if row >= 0 and 0 <= target < self.priority_list.count():
            item = self.priority_list.takeItem(row)
            self.priority_list.insertItem(target, item)
            self.priority_list.setCurrentRow(target)
            self._renumber()

    def options(self):
        return ReportRemarkOptions(
            tuple(self.priority_list.item(index).data(Qt.ItemDataRole.UserRole)
                  for index in range(self.priority_list.count())
                  if self.priority_list.item(index).checkState() == Qt.CheckState.Checked),
            self.position_combo.currentData(),
            tuple(self.form_list.item(index).data(Qt.ItemDataRole.UserRole)
                  for index in range(self.form_list.count())
                  if self.form_list.item(index).checkState() == Qt.CheckState.Checked),
            self.include_excluded.isChecked(),
        )

    def _accept(self):
        if set(self.options().columns) - self._labels.keys():
            self.error.setText("현재 원본에 없는 칼럼의 체크를 해제하거나 원본 변경번호를 다시 선택해주세요.")
            self.error.show()
            return
        self.accept()
