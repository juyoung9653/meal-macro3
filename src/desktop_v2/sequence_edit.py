"""Edit source roster delivery order for each meal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QMessageBox, QVBoxLayout, QScrollArea, QWidget,
)

from src.domain.roster import Meal, RosterSnapshot
from src.domain.sequence_edits import build_sequence_revision, build_scoped_sequence_revision

from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts

MEALS = (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH)


class SequenceEditDialog(QDialog):
    def __init__(self, roster: RosterSnapshot, person_id, parent=None):
        super().__init__(parent)
        keys = MEALS
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
        note = QLabel("식종별 배송순서를 수정합니다. 변경 내용은 모든 날짜에 적용됩니다.")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        form.setSpacing(10)
        self.fields = {}
        for meal in keys:
            field = QLineEdit(self._initial[meal])
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
        return {meal: field.text().strip() for meal, field in self.fields.items()}

    def _save(self):
        values = self.values()
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

    def run(self, person_id, parent=None):
        try:
            roster = self._session.database.current_roster()
            if roster is None:
                raise ValueError("현재 명단이 없습니다.")
            dialog = self._dialog_type(roster, person_id, parent)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            values = dialog.values()
            if all(
                values[meal] == (roster.resolve_assignment(person_id, meal).sequence or "")
                for meal in MEALS
            ):
                return None
            prepared = build_sequence_revision(
                roster, person_id, values, actor=self._actor_provider(),
                recorded_at=datetime.now(UTC), revision_id=uuid4(),
            )
            backup = self._session.append_revision(prepared.record)
            if backup.error is not None:
                self._message_box.warning(
                    parent, "배송순서는 저장됐지만 자동 백업에 실패했습니다", str(backup.error)
                )
            return self._session.database.current_roster()
        except Exception as error:  # noqa: BLE001 - storage errors are shown at the UI boundary
            self._message_box.critical(parent, "배송순서를 저장하지 못했습니다", str(error))
            return None


    def save_value(self, targets, value: str) -> SequenceSaveResult:
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
            prepared = build_scoped_sequence_revision(
                roster, {key: normalized for key in targets}, actor=self._actor_provider(),
                recorded_at=datetime.now(UTC), revision_id=uuid4(),
            )
            backup = self._session.append_revision(prepared.record)
        except Exception as error:  # noqa: BLE001 - observable inline storage boundary
            return SequenceSaveResult(SequenceSaveStatus.FAILED, message=f"배송순서를 저장하지 못했습니다: {error}")
        # append_revision reports automatic backup failures after the committed
        # revision. Do not turn that result into a failed save or reread error.
        if backup.error is not None:
            return SequenceSaveResult(
                SequenceSaveStatus.SAVED, prepared.snapshot,
                f"배송순서는 저장됐지만 자동 백업에 실패했습니다: {backup.error}", True,
            )
        return SequenceSaveResult(SequenceSaveStatus.SAVED, prepared.snapshot, "배송순서를 저장했습니다.")
