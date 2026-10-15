"""Explicit, revision-backed household column sharing with direct value editing."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from uuid import uuid4

from PySide6.QtCore import Qt, Signal, QRect, QSize
from PySide6.QtGui import QColor, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QComboBox, QDialog, QDialogButtonBox,
    QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QCheckBox, QStyledItemDelegate, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QMessageBox, QPlainTextEdit, QStyleOptionViewItem, QStyle,
)

from src.domain.household_shared import (
    HOUSEHOLD_SHARED_ID, HOUSEHOLD_SHARED_KIND,
    build_household_shared_revision, household_shared_components,
    household_shared_rows, shared_columns_from_state,
)
from src.domain.projections import roster_from_entity_states
from src.domain.revisions import EntityRef
from src.storage.database_session import committed_roster_after_save
from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts


class _SharedValueDelegate(QStyledItemDelegate):
    def __init__(self, components, parent):
        super().__init__(parent)
        self._components = components

    def sizeHint(self, option, index):
        width = self.parent().columnWidth(index.column())
        bounds = option.fontMetrics.boundingRect(
            QRect(0, 0, max(30, width - 18), 100000),
            Qt.TextFlag.TextWordWrap, index.data() or "",
        )
        return QSize(width, max(52, bounds.height() + 18))

    def paint(self, painter, option, index):
        # Qt's native item text layout collapses embedded newlines on some
        # Windows styles. Keep the stored lines visible and never add ellipses.
        decorated = QStyleOptionViewItem(option)
        self.initStyleOption(decorated, index)
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        decorated.text = ""
        widget = decorated.widget
        widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, decorated, painter, widget)
        selected = bool(decorated.state & QStyle.StateFlag.State_Selected)
        role = QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        painter.save()
        painter.setFont(decorated.font)
        painter.setPen(decorated.palette.color(role))
        painter.setClipRect(decorated.rect)
        painter.drawText(decorated.rect.adjusted(5, 7, -5, -7), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, text)
        painter.restore()

    def createEditor(self, parent, option, index):
        component = self._components[index.column() - 2]
        if component.choices:
            editor = QComboBox(parent)
            editor.setEditable(True)
            editor.addItems(component.choices)
        else:
            editor = QPlainTextEdit(parent)
            editor.setPlaceholderText(component.placeholder)
        editor.setAccessibleName(component.label + " 공용 값")
        return editor

    def setEditorData(self, editor, index):
        value = index.data(Qt.ItemDataRole.EditRole) or ""
        if isinstance(editor, QComboBox):
            editor.setCurrentText(value)
        else:
            editor.setPlainText(value)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText() if isinstance(editor, QComboBox) else editor.toPlainText())


class SharedValueTable(QTableWidget):
    clipboard_error = Signal(str)

    values_editable = True

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_cells()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Paste):
            self.paste_cells()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.clear_cells()
            event.accept()
            return
        super().keyPressEvent(event)

    def copy_cells(self):
        if self.currentItem() is not None:
            QApplication.clipboard().setText(self.currentItem().text())

    def paste_cells(self):
        if not self.values_editable:
            return
        if self.currentItem() is None or self.currentColumn() < 2:
            self.clipboard_error.emit("공용 값을 입력할 칸을 먼저 선택해주세요.")
            return
        item = self.currentItem()
        item.setText(QApplication.clipboard().text())
        item.setData(Qt.ItemDataRole.UserRole, True)

    def clear_cells(self):
        if not self.values_editable:
            return
        for index in self.selectedIndexes():
            if index.column() >= 2:
                item = self.item(index.row(), index.column())
                item.setText("")
                item.setData(Qt.ItemDataRole.UserRole, True)


class HouseholdSharedColumnDialog(QDialog):
    """All draft edits live in this dialog until one validated save."""

    def __init__(self, column_label, enabled, components, rows, service_day, parent=None):
        super().__init__(parent)
        self._rows = rows
        if len(components) == 1:
            components = (replace(components[0], label=column_label),)
        self._components = components
        self.validation_callback = None
        self._loading = True
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle(f"가구 공용 값 확인 · {column_label}")
        self.resize(1000, 620)
        self.setMinimumSize(720, 440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        title = QLabel(f"{column_label} · 서로 다른 가구원 값 확인")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        self.enabled = QCheckBox("같은 가구에서 이 칼럼을 공용으로 사용")
        self.enabled.setChecked(enabled)
        layout.addWidget(self.enabled)
        note = QLabel(
            f"조회일 {service_day:%Y-%m-%d}에 같은 가구원끼리 값이 다른 가구만 표시합니다. "
            "값이 이미 같은 가구는 그대로 유지합니다. 명단 검색·숨김 조건과 관계없습니다. "
            "공용 값을 직접 입력하고 복사·붙여넣기로 수정할 수 있습니다. 서로 다른 값은 직접 결정해야 합니다. "
            "저장 후 같은 가구에서 이 칼럼을 수정하면 함께 적용됩니다. 해제하면 현재 값은 유지합니다."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        controls = QHBoxLayout()
        self.copy = QPushButton("복사 (Ctrl+C)")
        self.paste = QPushButton("붙여넣기 (Ctrl+V)")
        self.clear = QPushButton("선택한 값 비우기")
        for button in (self.copy, self.paste, self.clear):
            button.setObjectName("quietAction")
            controls.addWidget(button)
        controls.addStretch()
        layout.addLayout(controls)
        self.table = SharedValueTable(len(rows), 2 + len(components))
        self.table.setAccessibleName(f"{column_label} 가구별 공용 값")
        self.table.setHorizontalHeaderLabels(["가구원", "현재 개인별 값", *("공용 · " + c.label for c in components)])
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed | QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.table.setItemDelegate(_SharedValueDelegate(components, self.table))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 160)
        self.table.setColumnWidth(1, 300)
        for column, component in enumerate(components, 2):
            self.table.setColumnWidth(column, 190)
            hint = component.placeholder
            if component.choices:
                hint = " · ".join(component.choices) + ("\n" + hint if hint else "")
            self.table.horizontalHeaderItem(column).setToolTip(hint)
        for row_index, row in enumerate(rows):
            names = QTableWidgetItem("\n".join(row.member_names))
            current = QTableWidgetItem("\n".join(
                name + ": " + " · ".join(f"{c.label}: {value or '(빈 값)'}" for c, value in zip(components, values))
                for name, values in zip(row.member_names, row.existing_values)
            ))
            for column, item in enumerate((names, current)):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setToolTip(item.text())
                self.table.setItem(row_index, column, item)
            for column, value in enumerate(row.values, 2):
                item = QTableWidgetItem(value if value is not None else "")
                item.setData(Qt.ItemDataRole.UserRole, value is not None)
                self.table.setItem(row_index, column, item)
                self._decorate_item(item)
        self.table.resizeRowsToContents()
        self.table.horizontalHeader().sectionResized.connect(lambda *_: self.table.resizeRowsToContents())
        layout.addWidget(self.table, 1)
        hint = QLabel("더블클릭·F2로 수정 · 입력 중 Ctrl+C / Ctrl+V · Tab으로 다음 칸 · 비우기/Del은 선택한 공용 값을 지웁니다.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setAccessibleName("공용 값 입력 안내")
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primaryAction")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("quietAction")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.copy.clicked.connect(self.table.copy_cells)
        self.paste.clicked.connect(self.table.paste_cells)
        self.clear.clicked.connect(self.table.clear_cells)
        self.table.clipboard_error.connect(self.error.setText)
        self.table.itemChanged.connect(self._item_changed)
        self.enabled.toggled.connect(self._update_enabled)
        self._loading = False
        self._update_enabled(enabled)

    def _decorate_item(self, item):
        resolved = bool(item.data(Qt.ItemDataRole.UserRole))
        item.setBackground(QColor("#FFFFFF" if resolved else "#FFF1DA"))
        item.setToolTip(item.text() if resolved else "개인별 값이 다릅니다. 공용 값을 직접 입력하거나 붙여넣으세요. 빈 값으로 결정하려면 '선택한 값 비우기'를 누르세요.")

    def _item_changed(self, item):
        if self._loading or item.column() < 2:
            return
        self._loading = True
        item.setData(Qt.ItemDataRole.UserRole, True)
        self._decorate_item(item)
        self._loading = False
        self.table.resizeRowToContents(item.row())
        self.error.clear()

    def _update_enabled(self, enabled):
        # Reading and scrolling remain available even while draft edits are off.
        self.table.values_editable = enabled
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed | QAbstractItemView.EditTrigger.AnyKeyPressed
            if enabled else QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._loading = True
        for row in range(self.table.rowCount()):
            for column in range(2, self.table.columnCount()):
                item = self.table.item(row, column)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable if enabled else item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._loading = False
        self.paste.setEnabled(enabled)
        self.clear.setEnabled(enabled)
        self.error.setText("" if enabled else "공용 사용을 선택해야 값을 수정할 수 있습니다. 현재 값은 스크롤·복사로 확인할 수 있습니다.")

    def selected_values(self):
        if not self.enabled.isChecked():
            return {}
        result = {}
        for row_index, row in enumerate(self._rows):
            values = []
            for column, component in enumerate(self._components, 2):
                item = self.table.item(row_index, column)
                if not item.data(Qt.ItemDataRole.UserRole):
                    self.table.setCurrentCell(row_index, column)
                    self.table.scrollToItem(item)
                    self.table.setFocus()
                    raise ValueError(f"{', '.join(row.member_names)} · {component.label}: 공용 값을 결정해주세요. 비우려면 '선택한 값 비우기'를 누르세요.")
                values.append(item.text())
            result[row.household_id] = tuple(values)
        return result

    def accept(self):
        # Moving focus commits the current Qt cell editor before collecting drafts.
        self.buttons.setFocus()
        try:
            values = self.selected_values()
            if self.validation_callback is not None:
                self.validation_callback(self.enabled.isChecked(), values)
        except (ValueError, TypeError) as error:
            self.error.setText(str(error))
            return
        super().accept()


@dataclass(frozen=True, slots=True)
class HouseholdSharedColumnOutcome:
    roster: object | None = None


class HouseholdSharedColumnController:
    def __init__(self, session, *, dialog_type=HouseholdSharedColumnDialog,
                 message_box=QMessageBox, actor_provider=current_windows_actor,
                 recorded_at_provider=lambda: datetime.now(UTC), revision_id_factory=uuid4):
        self._session = session
        self._dialog_type = dialog_type
        self._message_box = message_box
        self._actor_provider = actor_provider
        self._recorded_at_provider = recorded_at_provider
        self._revision_id_factory = revision_id_factory

    def run(self, column_key: str, column_label: str, service_day: date, parent=None):
        try:
            roster = self._session.database.current_roster()
            if roster is None:
                raise ValueError("현재 명단이 없습니다.")
            snapshot = self._session.database.snapshot(roster.revision_id)
            stored = snapshot.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
            enabled = column_key in shared_columns_from_state(stored)
            components = household_shared_components(roster, column_key, snapshot=snapshot)
            rows = household_shared_rows(roster, column_key, service_day, snapshot=snapshot) if not enabled else ()
        except Exception as error:
            self._message_box.critical(parent, "가구 공용 설정을 열지 못했습니다", str(error))
            return HouseholdSharedColumnOutcome()
        selected_enabled = not enabled
        values = {row.household_id: tuple(row.values) for row in rows}
        conflicts = tuple(row for row in rows if any(value is None for value in row.values))
        if conflicts:
            dialog = self._dialog_type(column_label, True, components, conflicts, service_day, parent)

            def validate(draft_enabled, draft_values):
                if draft_enabled:
                    build_household_shared_revision(
                        roster, stored, column_key, True, {**values, **draft_values},
                        actor=self._actor_provider(), recorded_at=self._recorded_at_provider(),
                        effective_on=service_day, revision_id=self._revision_id_factory(), snapshot=snapshot,
                    )

            dialog.validation_callback = validate
            if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.enabled.isChecked():
                return HouseholdSharedColumnOutcome()
        try:
            if conflicts:
                values.update(dialog.selected_values())
            current = self._session.database.current_roster()
            if current is None or current.revision_id != roster.revision_id:
                raise ValueError("설정창을 연 뒤 명단이 변경되었습니다. 창을 다시 열어 최신 값을 확인해주세요.")
            record = build_household_shared_revision(
                roster, stored, column_key, selected_enabled, values,
                actor=self._actor_provider(), recorded_at=self._recorded_at_provider(),
                effective_on=service_day, revision_id=self._revision_id_factory(), snapshot=snapshot,
            )
            entities = dict(snapshot.entities)
            for change in record.changes:
                reference = EntityRef(change.entity_kind, change.entity_id)
                if change.after is None:
                    entities.pop(reference, None)
                else:
                    entities[reference] = change.after
            prepared_roster = roster_from_entity_states(record.metadata.revision_id, entities)
            backup = self._session.append_revision(record)
        except Exception as error:
            self._message_box.critical(parent, "가구 공용 설정을 저장하지 못했습니다", str(error))
            return HouseholdSharedColumnOutcome()
        if backup.error is not None:
            self._message_box.warning(parent, "가구 공용 설정은 저장됐지만 자동 백업에 실패했습니다", str(backup.error))
        return HouseholdSharedColumnOutcome(committed_roster_after_save(self._session, prepared_roster))
