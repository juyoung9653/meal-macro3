"""DB-backed management dialog for user-defined roster fields."""

from __future__ import annotations

from .user_messages import user_error

from .dialog_workflow import DialogRequest, dialog_workflow

from .startup_loading import save_revision

from src.storage.database_session import committed_roster_after_save

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.custom_field_edits import (
    PreparedCustomFieldRevision,
    build_custom_field_create_revision,
    build_custom_field_delete_revision,
)
from src.domain.revisions import RevisionActor
from src.domain.roster import CustomFieldDefinition, CustomFieldType, RosterSnapshot
from src.storage.database_session import DatabaseSession

from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts


_TYPE_LABELS = {
    CustomFieldType.TEXT: "글자",
    CustomFieldType.NUMBER: "숫자",
    CustomFieldType.DATE: "날짜",
    CustomFieldType.CHOICE: "선택",
}


class CustomFieldAction(StrEnum):
    CREATE = "create"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class CustomFieldRequest:
    action: CustomFieldAction
    name: str | None = None
    field_type: CustomFieldType | None = None
    definition_id: UUID | None = None
    choices: tuple[str, ...] = ()


class CustomFieldsDialog(QDialog):
    def __init__(
        self,
        definitions: tuple[CustomFieldDefinition, ...],
        parent: QWidget | None = None,
        *,
        message_box=QMessageBox,
        affected_value_counts: dict[UUID, int] | None = None,
    ) -> None:
        super().__init__(parent)
        self._definitions = definitions
        self._message_box = message_box
        self._affected_value_counts = affected_value_counts or {}
        self._request: CustomFieldRequest | None = None
        register_application_fonts()
        self.setWindowTitle("사용자 정의 항목")
        self.setStyleSheet(application_stylesheet())
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("명단에 표시할 추가 항목을 관리합니다."))
        self._list = QListWidget()
        for definition in definitions:
            self._list.addItem(
                f"{definition.name}  ·  {_TYPE_LABELS[definition.field_type]}"
            )
        layout.addWidget(self._list)
        row = QHBoxLayout()
        self._name = QLineEdit()
        self._name.setPlaceholderText("새 항목 이름")
        self._type = QComboBox()
        for field_type in CustomFieldType:
            self._type.addItem(_TYPE_LABELS[field_type], field_type)
        self._choices = QLineEdit()
        self._choices.setPlaceholderText("선택형 값(쉼표 구분)")
        self._choices.setEnabled(False)
        self._type.currentIndexChanged.connect(self._update_choice_editor)
        add = QPushButton("추가")
        add.clicked.connect(self._add)
        row.addWidget(self._name)
        row.addWidget(self._type)
        row.addWidget(self._choices)
        row.addWidget(add)
        layout.addLayout(row)
        actions = QHBoxLayout()
        delete = QPushButton("선택 삭제")
        delete.clicked.connect(self._delete)
        close = QPushButton("닫기")
        close.clicked.connect(self.reject)
        actions.addWidget(delete)
        actions.addStretch(1)
        actions.addWidget(close)
        layout.addLayout(actions)

    def request(self) -> CustomFieldRequest:
        if self._request is None:
            raise RuntimeError("변경 요청이 없습니다.")
        return self._request

    def _add(self) -> None:
        choices = tuple(
            value.strip() for value in self._choices.text().split(",") if value.strip()
        )
        self._request = CustomFieldRequest(
            CustomFieldAction.CREATE,
            self._name.text(),
            CustomFieldType(self._type.currentData()),
            choices=choices,
        )
        self.accept()

    def _update_choice_editor(self, _index: int) -> None:
        choice = CustomFieldType(self._type.currentData()) is CustomFieldType.CHOICE
        self._choices.setEnabled(choice)
        if not choice:
            self._choices.clear()

    def _delete(self) -> None:
        row = self._list.currentRow()
        if row < 0:
            return
        definition = self._definitions[row]
        affected = self._affected_value_counts.get(definition.definition_id, 0)
        answer = self._message_box.question(
            self,
            "항목을 삭제할까요?",
            f"‘{definition.name}’ 항목과 입력된 값 {affected}건이 삭제됩니다. 계속할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._request = CustomFieldRequest(
                CustomFieldAction.DELETE, definition_id=definition.definition_id
            )
            self.accept()


class CustomFieldStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CustomFieldOutcome:
    status: CustomFieldStatus
    roster: RosterSnapshot | None = None

    @property
    def completed(self) -> bool:
        return self.status is CustomFieldStatus.COMPLETED


class CustomFieldController:
    def __init__(
        self,
        session: DatabaseSession,
        *,
        actor_provider: Callable[[], RevisionActor] | None = None,
        dialog_type=CustomFieldsDialog,
        message_box=QMessageBox,
        recorded_at_provider: Callable[[], datetime] | None = None,
        effective_date_provider: Callable[[], date] | None = None,
        revision_id_factory: Callable[[], UUID] = uuid4,
        definition_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._session = session
        self._actor_provider = actor_provider or current_windows_actor
        self._dialog_type = dialog_type
        self._message_box = message_box
        self._recorded_at_provider = recorded_at_provider or (lambda: datetime.now(UTC))
        self._effective_date_provider = effective_date_provider or date.today
        self._revision_id_factory = revision_id_factory
        self._definition_id_factory = definition_id_factory

    @dialog_workflow
    def run(self, parent: QWidget | None = None, *, on_saved=None) -> CustomFieldOutcome:
        roster = self._session.database.current_roster()
        if roster is None:
            self._message_box.critical(
                parent, "사용자 정의 항목", "현재 명단이 없습니다."
            )
            return CustomFieldOutcome(CustomFieldStatus.FAILED)
        changed = False
        while True:
            counts = {
                definition.definition_id: sum(
                    definition.definition_id in person.profile.custom_values
                    for person in roster.persons
                )
                for definition in roster.custom_fields
            }
            dialog = self._dialog_type(
                roster.custom_fields,
                parent,
                message_box=self._message_box,
                affected_value_counts=counts,
            )
            if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
                return CustomFieldOutcome(
                    CustomFieldStatus.COMPLETED
                    if changed
                    else CustomFieldStatus.CANCELLED,
                    committed_roster_after_save(self._session, roster) if changed else None,
                )
            try:
                request = dialog.request()
                prepared: PreparedCustomFieldRevision
                if request.action is CustomFieldAction.CREATE:
                    prepared = build_custom_field_create_revision(
                        roster,
                        request.name or "",
                        request.field_type,
                        actor=self._actor_provider(),
                        recorded_at=self._recorded_at_provider(),
                        effective_on=self._effective_date_provider(),
                        revision_id=self._revision_id_factory(),
                        definition_id=self._definition_id_factory(),
                        choices=request.choices,
                    )
                else:
                    prepared = build_custom_field_delete_revision(
                        roster,
                        request.definition_id,
                        actor=self._actor_provider(),
                        recorded_at=self._recorded_at_provider(),
                        effective_on=self._effective_date_provider(),
                        revision_id=self._revision_id_factory(),
                    )
                backup_result = save_revision(self._session, prepared.record, parent=parent)
                roster = committed_roster_after_save(self._session, prepared.snapshot)
                changed = True
                if on_saved is not None:
                    on_saved(roster)
                if backup_result.warning_required:
                    self._message_box.warning(
                        parent,
                        "사용자 정의 항목은 저장됐지만 자동 백업에 실패했습니다",
                        "변경 내용과 이력은 운영 데이터에 정상 저장되었습니다.\n\n"
                        + user_error(backup_result.error),
                    )
            except Exception as error:
                self._message_box.critical(
                    parent, "사용자 정의 항목을 저장하지 못했습니다", user_error(error)
                )
