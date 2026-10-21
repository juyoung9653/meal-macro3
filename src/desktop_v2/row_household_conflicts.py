"""Keep row creation intact while resolving differing shared household fields."""

from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from src.domain.column_input_rules import load_rules
from src.domain.row_household_conflicts import resolve_row_household_conflicts
from .column_input_rules import make_rule_editor
from .theme import application_stylesheet, register_application_fonts


class RowHouseholdConflictDialog(QDialog):
    def __init__(self, snapshot, prepared, conflicts, parent=None):
        super().__init__(parent)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("같은 가구의 공용 항목 확인")
        self.resize(620, 520)
        self._snapshot, self._prepared = snapshot, prepared
        self.resolved = None
        self.editors, self.exceptions = {}, {}
        root = QVBoxLayout(self)
        note = QLabel("서로 다른 항목만 표시합니다. 가구의 값에 맞게 수정하거나, 입력값을 유지할 항목은 ‘새 대상자 예외’를 선택하세요.")
        note.setWordWrap(True)
        root.addWidget(note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        fields = QVBoxLayout(content)
        rules = load_rules(snapshot)
        for conflict in conflicts:
            form = QFormLayout()
            title = ", ".join(component.label for component in conflict.components)
            if conflict.key.startswith("status:"):
                title = {"breakfast": "조식", "lunch": "중식", "dinner": "석식"}[conflict.key[7:]] + " 상태"
            label = QLabel(title)
            label.setObjectName("workspaceTitle")
            form.addRow(label)
            previous = QLabel(" / ".join(" · ".join(value or "(빈칸)" for value in values) for values in conflict.existing))
            previous.setWordWrap(True)
            form.addRow("기존 가구 값", previous)
            editors = []
            for component, value in zip(conflict.components, conflict.entered):
                if component.choices:
                    editor = QComboBox()
                    editor.addItems(component.choices)
                    if editor.findText(value) < 0:
                        editor.addItem(value)
                    editor.setCurrentText(value)
                else:
                    editor = make_rule_editor(conflict.key, rules)
                    editor.setText(value)
                    editor.setPlaceholderText(component.placeholder)
                form.addRow(component.label, editor)
                editors.append(editor)
            exception = QCheckBox("새 대상자 예외 (입력값 유지)")
            exception.toggled.connect(lambda checked, items=editors: [item.setEnabled(not checked) for item in items])
            if conflict.key.startswith("status:"):
                for editor in editors:
                    editor.setEnabled(False)
                exception.toggled.disconnect()
                form.addRow(QLabel("식종 상태는 예외로 지정하거나 입력 화면에서 수정해주세요."))
            form.addRow(exception)
            self.editors[conflict.key] = editors
            self.exceptions[conflict.key] = exception
            fields.addLayout(form)
        fields.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll)
        self.error = QLabel()
        self.error.setWordWrap(True)
        root.addWidget(self.error)
        actions = QHBoxLayout()
        actions.addStretch()
        back = QPushButton("입력 화면으로")
        back.setObjectName("quietAction")
        back.clicked.connect(self.reject)
        register = QPushButton("등록")
        register.setObjectName("primaryAction")
        register.clicked.connect(self._accept_valid)
        actions.addWidget(back)
        actions.addWidget(register)
        root.addLayout(actions)

    def _accept_valid(self):
        exceptions = {key for key, check in self.exceptions.items() if check.isChecked()}
        corrections = {
            key: tuple(editor.currentText() if isinstance(editor, QComboBox) else editor.text() for editor in editors)
            for key, editors in self.editors.items() if key not in exceptions
        }
        try:
            self.resolved = resolve_row_household_conflicts(self._snapshot, self._prepared, corrections, exceptions)
        except (ValueError, TypeError) as error:
            self.error.setText(str(error))
            return
        self.accept()
