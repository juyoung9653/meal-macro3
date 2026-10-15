"""Inline roster profile-cell editing through the immutable revision ledger."""

from __future__ import annotations

from src.storage.database_session import committed_roster_after_save

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QApplication,
    QComboBox,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QStyledItemDelegate,
    QWidget,
)

from src.domain.profile_edits import build_person_profile_revision
from src.domain.revisions import RevisionActor
from src.domain.roster import (
    CustomFieldDefinition,
    CustomFieldType,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.storage.database_session import (
    AutomaticBackupResult,
    DatabaseSession,
)

from .import_controller import current_windows_actor
from .person_edit import _custom_value, _optional_text, _parse_optional_date


_PROFILE_FIELDS = {
    "name": "name",
    "school": "school_name",
    "school_type": "school_type",
    "household_head_name": "household_head_name",
    "relationship_to_child": "relationship_to_child",
    "grade": "grade",
    "telephone": "telephone",
    "mobile": "mobile_phone",
    "note": "note",
}


def is_roster_cell_editable(column_key: str) -> bool:
    return column_key == "birth_date" or column_key in _PROFILE_FIELDS or column_key.startswith("custom:")


def roster_cell_text(person: Person, column_key: str, definition: CustomFieldDefinition | None = None) -> str:
    """Return the exact stored value used to seed an inline editor."""

    profile = person.profile
    if column_key == "birth_date":
        return "" if profile.birth_date is None else profile.birth_date.isoformat()
    if column_key in _PROFILE_FIELDS:
        value = getattr(profile, _PROFILE_FIELDS[column_key])
    elif column_key.startswith("custom:") and definition is not None:
        value = profile.custom_values.get(definition.definition_id)
    else:
        raise ValueError("이 칼럼은 셀에서 직접 수정할 수 없습니다.")
    if isinstance(value, date):
        return value.isoformat()
    return "" if value is None else str(value)


def updated_profile_for_cell(
    person: Person,
    column_key: str,
    text: str,
    definitions: tuple[CustomFieldDefinition, ...],
) -> PersonProfile:
    """Parse one editor value while preserving every other profile field."""

    profile = person.profile
    if column_key == "birth_date":
        return replace(profile, birth_date=_parse_optional_date(text))
    if column_key in _PROFILE_FIELDS:
        field = _PROFILE_FIELDS[column_key]
        value = text if field == "name" else _optional_text(text)
        return replace(profile, **{field: value})
    if not column_key.startswith("custom:"):
        raise ValueError("이 칼럼은 셀에서 직접 수정할 수 없습니다.")
    try:
        definition_id = UUID(column_key.removeprefix("custom:"))
        definition = next(item for item in definitions if item.definition_id == definition_id)
    except (ValueError, StopIteration) as error:
        raise ValueError("현재 명단에 없는 사용자 항목입니다.") from error
    value = _custom_value(definition, text.strip())
    custom_values = dict(profile.custom_values)
    if value is None:
        custom_values.pop(definition_id, None)
    else:
        custom_values[definition_id] = value
    return replace(profile, custom_values=custom_values)


class RosterCellEditDelegate(QStyledItemDelegate):
    """Create one-field editors without writing uncommitted text to the model."""

    save_requested = Signal(object, str, str)
    editing_cancelled = Signal()

    def createEditor(self, parent, option, index):
        model = index.model()
        column = model.columns[index.column()]
        if not is_roster_cell_editable(column.key):
            return None
        person = model.person_at(index.row())
        if column.custom_field is not None and column.custom_field.field_type is CustomFieldType.CHOICE:
            editor = QComboBox(parent)
            editor.addItem("미입력", "")
            for choice in column.custom_field.choices:
                editor.addItem(choice, choice)
        elif column.key == "note":
            editor = QPlainTextEdit(parent)
            editor.setTabChangesFocus(True)
        else:
            editor = QLineEdit(parent)
            if column.key == "birth_date" or (
                column.custom_field is not None
                and column.custom_field.field_type is CustomFieldType.DATE
            ):
                editor.setPlaceholderText("YYYY-MM-DD")
        editor.setObjectName("rosterCellEditor")
        editor.setAccessibleName(column.label)
        editor._roster_person_id = person.person_id
        editor._roster_column_key = column.key
        editor._roster_original_text = roster_cell_text(person, column.key, column.custom_field)
        editor._roster_finished = False
        return editor

    def setEditorData(self, editor, index):
        value = editor._roster_original_text
        if isinstance(editor, QComboBox):
            selected = editor.findData(value)
            editor.setCurrentIndex(max(0, selected))
        elif isinstance(editor, QPlainTextEdit):
            editor.setPlainText(value)
            editor.selectAll()
        else:
            editor.setText(value)
            editor.selectAll()

    def setModelData(self, editor, model, index):
        # The save callback owns persistence and the eventual model refresh.
        pass

    def eventFilter(self, editor, event):
        if not hasattr(editor, "_roster_finished"):
            return super().eventFilter(editor, event)
        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._commit(editor)
                return True
            if key == Qt.Key.Key_Escape:
                self._cancel(editor)
                return True
            if key == Qt.Key.Key_Tab:
                self._commit(editor, QAbstractItemDelegate.EndEditHint.EditNextItem)
                return True
            if key == Qt.Key.Key_Backtab:
                self._commit(editor, QAbstractItemDelegate.EndEditHint.EditPreviousItem)
                return True
        if event.type() == QEvent.Type.FocusOut:
            if isinstance(editor, QComboBox):
                popup = editor.view().window()
                focused = QApplication.focusWidget()
                if editor.view().isVisible() or (
                    focused is not None
                    and (focused is popup or popup.isAncestorOf(focused))
                ):
                    return False
            self._commit(editor)
            return True
        return super().eventFilter(editor, event)

    @staticmethod
    def _text(editor) -> str:
        if isinstance(editor, QComboBox):
            return str(editor.currentData())
        if isinstance(editor, QPlainTextEdit):
            return editor.toPlainText()
        return editor.text()

    def _commit(
        self,
        editor,
        hint: QAbstractItemDelegate.EndEditHint = QAbstractItemDelegate.EndEditHint.NoHint,
    ) -> None:
        if editor._roster_finished:
            return
        editor._roster_finished = True
        person_id = editor._roster_person_id
        column_key = editor._roster_column_key
        text = self._text(editor)
        self.closeEditor.emit(editor, hint)
        self.save_requested.emit(person_id, column_key, text)

    def _cancel(self, editor) -> None:
        if editor._roster_finished:
            return
        editor._roster_finished = True
        self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.RevertModelCache)
        self.editing_cancelled.emit()


class RosterCellEditStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RosterCellEditOutcome:
    status: RosterCellEditStatus
    roster: RosterSnapshot | None = None
    backup_result: AutomaticBackupResult | None = None

    @property
    def completed(self) -> bool:
        return self.status is RosterCellEditStatus.COMPLETED


class RosterCellEditController:
    def __init__(
        self,
        session: DatabaseSession,
        *,
        message_box=QMessageBox,
        actor_provider: Callable[[], RevisionActor] = current_windows_actor,
        recorded_at_provider: Callable[[], datetime] = lambda: datetime.now(UTC),
        effective_date_provider: Callable[[], date] = date.today,
        revision_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._session = session
        self._message_box = message_box
        self._actor_provider = actor_provider
        self._recorded_at_provider = recorded_at_provider
        self._effective_date_provider = effective_date_provider
        self._revision_id_factory = revision_id_factory

    def save(
        self,
        person_id: UUID,
        column_key: str,
        text: str,
        parent: QWidget | None = None,
    ) -> RosterCellEditOutcome:
        try:
            roster = self._session.database.current_roster()
            if roster is None:
                raise ValueError("현재 명단이 없습니다.")
            person = roster.person(person_id)
            updated_profile = updated_profile_for_cell(
                person, column_key, text, roster.custom_fields
            )
            if updated_profile == person.profile:
                return RosterCellEditOutcome(RosterCellEditStatus.CANCELLED)
            prepared = build_person_profile_revision(
                roster,
                person_id,
                updated_profile,
                actor=self._actor_provider(),
                recorded_at=self._recorded_at_provider(),
                effective_on=self._effective_date_provider(),
                revision_id=self._revision_id_factory(),
                reason="명단 셀 수정",
            )
            backup_result = self._session.append_revision(prepared.record)
        except Exception as error:
            self._message_box.critical(
                parent,
                "명단 셀을 저장하지 못했습니다",
                "운영 데이터에는 반영하지 않았습니다. 내용을 확인한 뒤 다시 시도해주세요.\n\n"
                + str(error),
            )
            return RosterCellEditOutcome(RosterCellEditStatus.FAILED)
        if backup_result.error is not None:
            self._message_box.warning(
                parent,
                "명단 셀은 저장됐지만 자동 백업에 실패했습니다",
                "변경 내용과 이력은 운영 데이터에 정상 저장되었습니다. "
                "백업 폴더 연결을 확인한 뒤 백업을 다시 실행해주세요.\n\n"
                + str(backup_result.error),
            )
        return RosterCellEditOutcome(
            RosterCellEditStatus.COMPLETED,
            committed_roster_after_save(self._session, prepared.snapshot),
            backup_result,
        )
