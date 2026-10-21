"""Edit source roster delivery order for each meal."""

from __future__ import annotations

from .dialog_workflow import DialogRequest, dialog_workflow

from .startup_loading import save_revision

from src.storage.database_session import committed_roster_after_save

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from inspect import Parameter, signature
from uuid import uuid4

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QMessageBox, QVBoxLayout, QScrollArea, QWidget,
)

from src.domain.roster import Meal, RosterSnapshot
from src.domain.sequence_edits import build_sequence_revision, build_scoped_sequence_revision
from src.domain.sequence_settings import SequenceMode, sequence_mode_from_snapshot
from src.domain.column_input_rules import InputRule, load_rules, rule_for, validate_input

from .import_controller import current_windows_actor
from .column_input_rules import make_rule_editor
from .theme import application_stylesheet, register_application_fonts

MEALS = (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH)


class SequenceEditDialog(QDialog):
    def __init__(
        self,
        roster: RosterSnapshot,
        person_id,
        parent=None,
        *,
        mode: SequenceMode = SequenceMode.SEPARATE,
        input_rules: dict[str, InputRule] | None = None,
    ):
        super().__init__(parent)
        keys = MEALS
        self._mode = mode
        self._input_rules = input_rules or {}
        self._initial = {meal: roster.resolve_assignment(person_id, meal).sequence or "" for meal in keys}
        self._labels = {meal: meal.value for meal in keys}
        name = roster.person(person_id).profile.name
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle(f"{name} · 배송순서 수정")
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        title = QLabel(f"{name} · 배송순서")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        note = QLabel(
            "배송순서 하나를 입력하면 석식·조식·중식 모두에 적용합니다. 변경 내용은 모든 날짜에 적용됩니다."
            if mode is SequenceMode.UNIFIED
            else "식종별 배송순서를 수정합니다. 변경 내용은 모든 날짜에 적용됩니다."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        form.setSpacing(10)
        self.fields = {}
        self._unified_mixed = False
        if mode is SequenceMode.UNIFIED:
            distinct = set(self._initial.values())
            self._unified_mixed = len(distinct) > 1
            if self._unified_mixed:
                current = QLabel(
                    "현재 값이 서로 다릅니다.\n"
                    + "\n".join(
                        f"{self._labels[meal]}: {self._initial[meal] or '미지정'}"
                        for meal in keys
                    )
                )
                current.setObjectName("sequenceMixedValues")
                current.setWordWrap(True)
                form.addRow("현재 순서", current)
            field = make_rule_editor("sequence", self._input_rules)
            field.setText("" if self._unified_mixed else self._initial[keys[0]])
            field.setObjectName("unifiedSequence")
            field.setAccessibleName("모든 식종 배송순서")
            field.setToolTip("새 값을 입력해 저장한 경우에만 세 식종에 함께 적용합니다")
            field.setPlaceholderText(
                "새 순서를 입력하면 모든 식종에 적용"
                if self._unified_mixed else "모든 식종 배송순서"
            )
            self.fields = {meal: field for meal in keys}
            form.addRow("모든 식종", field)
        else:
            for meal in keys:
                field = make_rule_editor("sequence", self._input_rules)
                field.setText(self._initial[meal])
                field.setAccessibleName(f"{self._labels[meal]} 배송순서")
                field.setToolTip(f"{self._labels[meal]} 배송순서")
                self.fields[meal] = field
                form.addRow(self._labels[meal], field)
        form_widget = QWidget()
        form_widget.setLayout(form)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(form_widget)
        layout.addWidget(scroll)
        self.resize(460, min(600, 240 + len(keys) * 42))
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #B42318;")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("quietAction")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        if self._mode is SequenceMode.UNIFIED:
            value = self.fields[MEALS[0]].text().strip()
            if self._unified_mixed and not value:
                return {}
            return {meal: value for meal in MEALS}
        return {meal: field.text().strip() for meal, field in self.fields.items()}

    def _save(self):
        values = self.values()
        if not values:
            self.accept()
            return
        for meal, value in values.items():
            if value == self._initial[meal]:
                continue
            if not value:
                self.error.setText(
                    f"{self._labels[meal]} 순서를 비울 수 없습니다. 기존 순서는 그대로 두거나 새 순서를 입력해주세요."
                )
                self.fields[meal].setFocus()
                return
            if "\n" in value or "\r" in value:
                self.error.setText(f"{self._labels[meal]} 순서는 한 줄로 입력해주세요.")
                self.fields[meal].setFocus()
                return
            try:
                validate_input("sequence", value, rule_for(self._input_rules, "sequence"))
            except ValueError as error:
                self.error.setText(str(error))
                self.fields[meal].setFocus()
                return
        self.accept()


class SequenceSaveStatus(Enum):
    SAVED = "saved"
    UNCHANGED = "unchanged"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SequenceSaveResult:
    status: SequenceSaveStatus
    roster: RosterSnapshot | None = None
    message: str = ""
    backup_warning: bool = False


class SequenceEditController:
    def __init__(self, session, *, dialog_type=SequenceEditDialog,
                 message_box=QMessageBox, actor_provider=current_windows_actor):
        self._session = session
        self._dialog_type = dialog_type
        self._message_box = message_box
        self._actor_provider = actor_provider

    @dialog_workflow
    def run(self, person_id, parent=None):
        try:
            roster = self._session.database.current_roster()
            if roster is None:
                raise ValueError("현재 명단이 없습니다.")
            snapshot = self._session.database.snapshot(roster.revision_id)
            mode = sequence_mode_from_snapshot(snapshot)
            parameters = signature(self._dialog_type).parameters.values()
            accepts_mode = any(
                parameter.name == "mode"
                or parameter.kind is Parameter.VAR_KEYWORD
                for parameter in parameters
            )
            if accepts_mode:
                if self._dialog_type is SequenceEditDialog:
                    dialog = self._dialog_type(roster, person_id, parent, mode=mode, input_rules=load_rules(snapshot))
                else:
                    dialog = self._dialog_type(roster, person_id, parent, mode=mode)
            else:
                # Keep older injected test/dialog factories compatible.
                dialog = self._dialog_type(roster, person_id, parent)
            if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
                return None
            values = dialog.values()
            if not values:
                return None
            if all(
                value == (roster.resolve_assignment(person_id, meal).sequence or "")
                for meal, value in values.items()
            ):
                return None
            for meal, value in values.items():
                if value != (roster.resolve_assignment(person_id, meal).sequence or ""):
                    validate_input("sequence", value, rule_for(load_rules(snapshot), "sequence"))
            prepared = build_sequence_revision(
                roster, person_id, values, actor=self._actor_provider(),
                recorded_at=datetime.now(UTC), revision_id=uuid4(),
            )
            backup = save_revision(self._session, prepared.record, parent=parent)
            if backup.error is not None:
                self._message_box.warning(
                    parent, "배송순서는 저장됐지만 자동 백업에 실패했습니다", str(backup.error)
                )
            return self._session.database.current_roster()
        except Exception as error:  # noqa: BLE001 - storage errors are shown at the UI boundary
            self._message_box.critical(parent, "배송순서를 저장하지 못했습니다", str(error))
            return None


    def save_value(self, targets, value: str, parent=None) -> SequenceSaveResult:
        """Apply one value to exact manifest pairs with an explicit commit outcome."""
        try:
            roster = self._session.database.current_roster()
            if roster is None:
                raise ValueError("현재 명단이 없습니다.")
            targets = tuple(dict.fromkeys(targets))
            if not targets:
                raise ValueError("수정할 식종이 없습니다.")
            normalized = value.strip()
            if not normalized:
                raise ValueError("배송순서를 비울 수 없습니다. 순서를 입력해주세요.")
            if "\n" in normalized or "\r" in normalized:
                raise ValueError("배송순서는 한 줄로 입력해주세요.")
            if all(normalized == (roster.resolve_assignment(*key).sequence or "") for key in targets):
                return SequenceSaveResult(SequenceSaveStatus.UNCHANGED, roster, "변경된 배송순서가 없습니다.")
            snapshot = self._session.database.snapshot(roster.revision_id)
            validate_input("sequence", normalized, rule_for(load_rules(snapshot), "sequence"))
            prepared = build_scoped_sequence_revision(
                roster, {key: normalized for key in targets}, actor=self._actor_provider(),
                recorded_at=datetime.now(UTC), revision_id=uuid4(),
            )
            backup = save_revision(self._session, prepared.record, parent=parent)
        except Exception as error:  # noqa: BLE001 - observable inline storage boundary
            return SequenceSaveResult(SequenceSaveStatus.FAILED, message=f"배송순서를 저장하지 못했습니다: {error}")
        # append_revision reports automatic backup failures after the committed
        # revision. Do not turn that result into a failed save or reread error.
        if backup.error is not None:
            return SequenceSaveResult(
                SequenceSaveStatus.SAVED, committed_roster_after_save(self._session, prepared.snapshot),
                f"배송순서는 저장됐지만 자동 백업에 실패했습니다: {backup.error}", True,
            )
        return SequenceSaveResult(SequenceSaveStatus.SAVED, committed_roster_after_save(self._session, prepared.snapshot), "배송순서를 저장했습니다.")
