"""Roster column input settings and digit-only editors."""

from __future__ import annotations

from .user_messages import user_error

from datetime import UTC, datetime
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QVBoxLayout

from src.domain.column_input_rules import InputRule, build_rules_revision, load_rules, rule_for
from src.domain.roster import CustomFieldType
from .dialog_workflow import DialogRequest, dialog_workflow
from .import_controller import current_windows_actor
from .startup_loading import save_revision
from .theme import application_stylesheet, register_application_fonts


def column_key_for_profile_field(key: str) -> str:
    return {"school_name": "school", "mobile_phone": "mobile", "road_address": "address"}.get(key, key)


def format_digits(value: str, kind: str) -> str:
    digits = "".join(char for char in value if char.isascii() and char.isdigit())
    if kind == "date":
        digits = digits[:8]
        return "-".join(part for part in (digits[:4], digits[4:6], digits[6:]) if part)
    prefix = 2 if digits.startswith("02") or ("-" in value and len(value.split("-", 1)[0]) == 2) else 3
    digits = digits[:prefix + 8]
    if len(digits) <= prefix:
        return digits
    middle = 4 if len(digits) > prefix + 7 else 3
    return "-".join(part for part in (digits[:prefix], digits[prefix:prefix + middle], digits[prefix + middle:]) if part)


class RuleLineEdit(QLineEdit):
    """Format user edits while leaving loaded legacy values untouched."""

    def __init__(self, rule: InputRule, parent=None):
        super().__init__(parent)
        self._rule = rule
        if rule.format in ("phone", "date"):
            self.textEdited.connect(self._format_edited)

    def _format_edited(self, value: str) -> None:
        cursor = self.cursorPosition()
        before = sum(char.isascii() and char.isdigit() for char in value[:cursor])
        formatted = format_digits(value, self._rule.format)
        if value == formatted:
            return
        self.setText(formatted)
        if before:
            index = 0
            seen = 0
            while index < len(formatted) and seen < before:
                if formatted[index].isdigit():
                    seen += 1
                index += 1
            self.setCursorPosition(index)
        else:
            self.setCursorPosition(0)

    def keyPressEvent(self, event):
        if self._rule.format in ("phone", "date") and event.key() == Qt.Key.Key_Backspace and not self.hasSelectedText() and self.cursorPosition() > 0 and self.text()[self.cursorPosition() - 1] == "-":
            pos = self.cursorPosition()
            text = self.text()
            self.setText(format_digits(text[:pos - 2] + text[pos:], self._rule.format))
            self.setCursorPosition(pos - 2)
            return
        super().keyPressEvent(event)


def make_rule_editor(key: str, rules: dict[str, InputRule], parent=None) -> RuleLineEdit:
    rule = rule_for(rules, key)
    # Birth date remains a typed date even when its optional text rule is chosen.
    if key == "birth_date" and rule.format == "text":
        rule = InputRule("date", rule.max_length)
    return RuleLineEdit(rule, parent)


class ColumnInputRuleDialog(QDialog):
    def __init__(self, label: str, rule: InputRule, *, birth_date: bool = False, allowed_formats: frozenset[str] = frozenset({"text", "phone", "date"}), parent=None):
        super().__init__(parent)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle(f"{label} · 입력 제한 설정")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"{label} 칼럼에 새로 입력하는 값의 형식을 선택합니다."))
        self.format = QComboBox()
        self.format.addItem("제한 없음 (텍스트)", "text")
        self.format.addItem("전화번호 (앞자리 2~3자리 · 가운데 3~4자리 · 끝 4자리)", "phone")
        self.format.addItem("날짜 (YYYY-MM-DD)", "date")
        for index in range(self.format.count()):
            self.format.model().item(index).setEnabled(self.format.itemData(index) in allowed_formats)
        self.format.setCurrentIndex(self.format.findData(rule.format))
        layout.addWidget(self.format)
        if birth_date:
            note = QLabel("생년월일은 형식 설정과 관계없이 실제 날짜로 저장됩니다.")
            note.setWordWrap(True)
            layout.addWidget(note)
        self.limit = QCheckBox("최대 글자 수")
        self.limit.setChecked(rule.max_length is not None)
        self.length = QSpinBox()
        self.length.setRange(1, 100000)
        self.length.setValue(rule.max_length or 20)
        self.length.setEnabled(self.limit.isChecked())
        self.limit.toggled.connect(self.length.setEnabled)
        row = QHBoxLayout()
        row.addWidget(self.limit)
        row.addWidget(self.length)
        layout.addLayout(row)
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("취소")
        cancel.setObjectName("quietAction")
        cancel.clicked.connect(self.reject)
        save = QPushButton("저장")
        save.setObjectName("primaryAction")
        save.clicked.connect(self.accept)
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)

    def selected_rule(self) -> InputRule:
        return InputRule(self.format.currentData(), self.length.value() if self.limit.isChecked() else None)


class ColumnInputRuleController:
    def __init__(self, session, *, dialog_type=ColumnInputRuleDialog, message_box=QMessageBox, actor_provider=current_windows_actor):
        self._session = session
        self._dialog_type = dialog_type
        self._message_box = message_box
        self._actor_provider = actor_provider

    @dialog_workflow
    def run(self, key: str, label: str, parent=None):
        try:
            roster = self._session.database.current_roster()
            if roster is None:
                raise ValueError("현재 명단이 없습니다.")
            snapshot = self._session.database.snapshot(roster.revision_id)
            rules = load_rules(snapshot)
            definition = next((item for item in roster.custom_fields if f"custom:{item.definition_id}" == key), None)
            allowed = (
                frozenset({"date"}) if key in {"lunch_starts_on", "lunch_ends_on"}
                else frozenset({"text", "date"}) if key == "birth_date" or (definition is not None and definition.field_type is CustomFieldType.DATE)
                else frozenset({"text"}) if definition is not None and definition.field_type is CustomFieldType.NUMBER
                else frozenset({"text", "phone", "date"})
            )
            if self._dialog_type is ColumnInputRuleDialog:
                dialog = self._dialog_type(label, rule_for(rules, key), birth_date=key == "birth_date", allowed_formats=allowed, parent=parent)
            else:
                dialog = self._dialog_type(label, rule_for(rules, key), birth_date=key == "birth_date", parent=parent)
            if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
                return False
            selected = dialog.selected_rule()
            if selected.format not in allowed:
                raise ValueError("이 항목의 저장 형식과 맞지 않는 입력 제한입니다.")
            prepared = build_rules_revision(roster.revision_id, snapshot, key, selected, actor=self._actor_provider(), recorded_at=datetime.now(UTC), revision_id=uuid4())
            if prepared is None:
                return False
            backup = save_revision(self._session, prepared, parent=parent)
            if backup.warning_required:
                self._message_box.warning(parent, "입력 제한은 저장됐지만 자동 백업에 실패했습니다", user_error(backup.error))
            return True
        except Exception as error:
            self._message_box.critical(parent, "입력 제한을 저장하지 못했습니다", user_error(error))
            return False
