"""Inline roster profile-cell editing through the immutable revision ledger."""

from __future__ import annotations

from .startup_loading import save_revision

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

from src.domain.household_shared import (
    build_profile_correction_revision,
    profile_with_shared_routing,
)
from src.domain.profile_edits import build_person_profile_revision
from src.domain.column_input_rules import load_rules, rule_for, validate_input
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
from .column_input_rules import make_rule_editor


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
_ROUTING_PROFILE_FIELDS = {
    "address": "road_address",
    "detail_address": "detail_address",
    "jurisdiction": "jurisdiction",
}


def is_roster_cell_editable(column_key: str) -> bool:
    return (
        column_key == "birth_date"
        or column_key in _PROFILE_FIELDS
        or column_key in _ROUTING_PROFILE_FIELDS
        or column_key.startswith("custom:")
    )


def is_roster_routing_cell(column_key: str) -> bool:
    return column_key in _ROUTING_PROFILE_FIELDS


def roster_cell_text(person: Person, column_key: str, definition: CustomFieldDefinition | None = None) -> str:
    """Return the exact stored value used to seed an inline editor."""

    profile = person.profile
    if column_key == "birth_date":
        return "" if profile.birth_date is None else profile.birth_date.isoformat()
    if column_key in _PROFILE_FIELDS:
        value = getattr(profile, _PROFILE_FIELDS[column_key])
    elif column_key in _ROUTING_PROFILE_FIELDS:
        value = getattr(profile, _ROUTING_PROFILE_FIELDS[column_key])
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
    if column_key in _ROUTING_PROFILE_FIELDS:
        return replace(
            profile,
            **{_ROUTING_PROFILE_FIELDS[column_key]: _optional_text(text)},
        )
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

    def set_input_rules(self, rules) -> None:
        self._input_rules = rules

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
            editor = make_rule_editor(column.key, getattr(self, "_input_rules", {}), parent)
            if column.key == "birth_date" or (
                column.custom_field is not None
                and column.custom_field.field_type is CustomFieldType.DATE
            ):
                editor.setPlaceholderText("YYYY-MM-DD")
        editor.setObjectName("rosterCellEditor")
        editor.setAccessibleName(column.label)
        editor._roster_person_id = person.person_id
        editor._roster_column_key = column.key
        editor._roster_original_text = (
            str(model.data(index, Qt.ItemDataRole.DisplayRole) or "")
            if column.key in _ROUTING_PROFILE_FIELDS
            else roster_cell_text(person, column.key, column.custom_field)
        )
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
        if text != editor._roster_original_text:
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
        *,
        service_day: date | None = None,
    ) -> RosterCellEditOutcome:
        try:
            roster = self._session.database.current_roster()
            if roster is None:
                raise ValueError("현재 명단이 없습니다.")
            person = roster.person(person_id)
            effective_on = self._effective_date_provider()
            snapshot = None
            editing_person = person
            forced_routing_fields = ()
            if column_key in _ROUTING_PROFILE_FIELDS:
                snapshot_reader = getattr(self._session.database, "snapshot", None)
                snapshot = (
                    snapshot_reader(roster.revision_id)
                    if snapshot_reader is not None
                    else None
                )
                if service_day is not None:
                    displayed_profile = profile_with_shared_routing(
                        roster,
                        person_id,
                        service_day,
                        snapshot,
                    )
                    field = _ROUTING_PROFILE_FIELDS[column_key]
                    if (_optional_text(text) or "") == (
                        getattr(displayed_profile, field) or ""
                    ):
                        return RosterCellEditOutcome(RosterCellEditStatus.CANCELLED)
                    forced_routing_fields = (field,)
                editing_person = replace(
                    person,
                    profile=profile_with_shared_routing(
                        roster,
                        person_id,
                        effective_on,
                        snapshot,
                    ),
                )
            updated_profile = updated_profile_for_cell(
                editing_person, column_key, text, roster.custom_fields
            )
            if updated_profile == editing_person.profile and not forced_routing_fields:
                return RosterCellEditOutcome(RosterCellEditStatus.CANCELLED)
            snapshot_reader = getattr(self._session.database, "snapshot", None)
            rules = load_rules(snapshot_reader(roster.revision_id) if snapshot_reader else None)
            value = roster_cell_text(replace(editing_person, profile=updated_profile), column_key,
                                     next((item for item in roster.custom_fields if f"custom:{item.definition_id}" == column_key), None))
            validate_input(column_key, value, rule_for(rules, column_key))
            build_revision = (
                build_profile_correction_revision
                if column_key in _ROUTING_PROFILE_FIELDS
                else build_person_profile_revision
            )
            try:
                prepared = build_revision(
                    roster,
                    person_id,
                    updated_profile,
                    **({"snapshot": snapshot} if column_key in _ROUTING_PROFILE_FIELDS else {}),
                    **(
                        {"forced_routing_fields": forced_routing_fields}
                        if forced_routing_fields
                        else {}
                    ),
                    actor=self._actor_provider(),
                    recorded_at=self._recorded_at_provider(),
                    effective_on=effective_on,
                    revision_id=self._revision_id_factory(),
                    reason="명단 셀 수정",
                )
            except ValueError as error:
                if forced_routing_fields and str(error) == "변경된 사람 정보가 없습니다.":
                    return RosterCellEditOutcome(RosterCellEditStatus.CANCELLED)
                raise
            backup_result = save_revision(self._session, prepared.record, parent=parent)
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
