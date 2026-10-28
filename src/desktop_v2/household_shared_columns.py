"""Explicit, revision-backed household column sharing with direct value editing."""

from __future__ import annotations

from .dialog_workflow import DialogRequest, dialog_workflow

from .startup_loading import save_revision

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from math import ceil
from uuid import uuid4

from PySide6.QtCore import Qt, Signal, QRect, QSize
from PySide6.QtGui import (
    QColor, QKeySequence, QPalette, QTextCharFormat, QTextCursor,
    QTextDocument, QTextOption,
)
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
        document = self._text_document(
            index.data() or "", option.font,
            option.palette.color(QPalette.ColorRole.Text), max(30, width - 10),
        )
        return QSize(width, max(52, ceil(document.size().height()) + 14))

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
        painter.setClipRect(decorated.rect)
        name_row = isinstance(self.parent(), _CurrentValuesGrid) and index.row() % 2 == 0
        padding = 4 if name_row else 7
        content = decorated.rect.adjusted(5, padding, -5, -padding)
        document = self._text_document(
            text, decorated.font, decorated.palette.color(role),
            max(30, content.width()),
        )
        painter.translate(content.topLeft())
        document.drawContents(painter, QRect(0, 0, content.width(), content.height()))
        painter.restore()

    @staticmethod
    def _text_document(text, font, color, width):
        document = QTextDocument()
        document.setDocumentMargin(0)
        document.setDefaultFont(font)
        option = document.defaultTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        document.setDefaultTextOption(option)

        cursor = QTextCursor(document)
        document.setPlainText(text)
        cursor.select(QTextCursor.SelectionType.Document)
        text_format = QTextCharFormat()
        text_format.setForeground(color)
        cursor.mergeCharFormat(text_format)
        document.setTextWidth(width)
        return document

    def createEditor(self, parent, option, index):
        component = self._components[index.column() - 1]
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
        if self.currentItem() is None or self.currentColumn() < 1:
            self.clipboard_error.emit("공용 값을 입력할 칸을 먼저 선택해주세요.")
            return
        item = self.currentItem()
        item.setText(QApplication.clipboard().text())
        item.setData(Qt.ItemDataRole.UserRole, True)

    def clear_cells(self):
        if not self.values_editable:
            return
        for index in self.selectedIndexes():
            if index.column() >= 1:
                item = self.item(index.row(), index.column())
                item.setText("")
                item.setData(Qt.ItemDataRole.UserRole, True)


class _CurrentValuesGrid(QTableWidget):
    RAW_VALUE_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, components, member_names, existing_values, outer_table):
        super().__init__(2 * len(member_names), len(components), outer_table)
        self._outer_table = outer_table
        self.setAccessibleName("가구원별 현재 값")
        self.setHorizontalHeaderLabels([component.label for component in components])
        self.horizontalHeader().setVisible(len(components) > 1)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.verticalHeader().hide()
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.setWordWrap(True)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setItemDelegate(_SharedValueDelegate((), self))
        for member_index, (name, values) in enumerate(zip(member_names, existing_values)):
            name_row = 2 * member_index
            name_item = QTableWidgetItem(name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name_item.setToolTip(name)
            name_item.setData(self.RAW_VALUE_ROLE, name)
            font = self.font()
            font.setBold(True)
            name_item.setFont(font)
            name_item.setBackground(QColor("#EDF3F5"))
            self.setItem(name_row, 0, name_item)
            if len(components) > 1:
                self.setSpan(name_row, 0, 1, len(components))
            for component_column, value in enumerate(values):
                raw_value = "" if value is None else value
                item = QTableWidgetItem(raw_value if raw_value else "(빈 값)")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setData(self.RAW_VALUE_ROLE, raw_value)
                item.setToolTip(raw_value)
                self.setItem(name_row + 1, component_column, item)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Copy):
            item = self.currentItem()
            if item is not None:
                QApplication.clipboard().setText(item.data(self.RAW_VALUE_ROLE) or "")
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event):
        if event.angleDelta().y() and not event.modifiers():
            self._outer_table.wheelEvent(event)
            return
        super().wheelEvent(event)

    def fit_to_width(self, width):
        frame_width = self.frameWidth() * 2
        available = max(1, width - 1 - frame_width)
        shared_width = max(140, available // max(1, self.columnCount()))
        for column in range(self.columnCount()):
            self.setColumnWidth(column, shared_width)
        self.resizeRowsToContents()
        for row in range(0, self.rowCount(), 2):
            item = self.item(row, 0)
            document = _SharedValueDelegate._text_document(
                item.text(), item.font(), self.palette().color(QPalette.ColorRole.Text),
                max(30, shared_width * self.columnCount() - 10),
            )
            self.setRowHeight(row, ceil(document.size().height()) + 8)
        height = (
            (self.horizontalHeader().height() if not self.horizontalHeader().isHidden() else 0)
            + sum(self.rowHeight(row) for row in range(self.rowCount()))
            + (self.horizontalScrollBar().sizeHint().height()
               if shared_width * self.columnCount() > available else 0)
            + frame_width + 1
        )
        self.setMinimumHeight(height)
        return height


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
            "왼쪽 값 셀을 클릭하면 해당 공용 입력칸이 그 내용으로 바뀝니다. 직접 입력하거나 복사·붙여넣기로 수정할 수도 있습니다. "
            "저장 후 같은 가구에서 이 칼럼을 수정하면 함께 적용됩니다. 해제하면 현재 값은 유지합니다."
        )
        self.note = note
        note.setWordWrap(True)
        layout.addWidget(note)
        controls = QHBoxLayout()
        self.clear = QPushButton("선택한 값 비우기")
        self.clear.setObjectName("quietAction")
        controls.addWidget(self.clear)
        controls.addStretch()
        layout.addLayout(controls)
        self.table = SharedValueTable(len(rows), 1 + len(components))
        self.table.setAccessibleName(f"{column_label} 가구별 공용 값")
        self.table.setHorizontalHeaderLabels(["현재 개인별 값", *("공용 · " + c.label for c in components)])
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed | QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.table.setItemDelegate(_SharedValueDelegate(components, self.table))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 420)
        self._current_value_grids = []
        for column, component in enumerate(components, 1):
            self.table.setColumnWidth(column, 190)
            hint = component.placeholder
            if component.choices:
                hint = " · ".join(component.choices) + ("\n" + hint if hint else "")
            self.table.horizontalHeaderItem(column).setToolTip(hint)
        for row_index, row in enumerate(rows):
            current_text = "\n\n".join(
                name + "\n" + "\n".join(
                    f"{component.label}: {value if value is not None else ''}"
                    for component, value in zip(components, values)
                )
                for name, values in zip(row.member_names, row.existing_values)
            )
            current = QTableWidgetItem()
            current.setToolTip(current_text)
            current.setFlags(current.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row_index, 0, current)
            current_grid = _CurrentValuesGrid(
                components, row.member_names, row.existing_values, self.table,
            )
            current_grid.cellClicked.connect(
                lambda row, column, grid=current_grid, target=row_index:
                self._use_current_value(target, grid, row, column)
            )
            self._current_value_grids.append(current_grid)
            self.table.setCellWidget(row_index, 0, current_grid)
            for column, value in enumerate(row.values, 1):
                item = QTableWidgetItem(value if value is not None else "")
                item.setData(Qt.ItemDataRole.UserRole, value is not None)
                self.table.setItem(row_index, column, item)
                self._decorate_item(item)
        self._resize_review_rows()
        self.table.horizontalHeader().sectionResized.connect(self._column_resized)
        layout.addWidget(self.table, 1)
        hint = QLabel("선택한 셀 Ctrl+C · 더블클릭·F2로 수정 · 입력 중 Ctrl+C / Ctrl+V · Tab으로 다음 칸 · 비우기/Del은 선택한 공용 값을 지웁니다.")
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
        self.clear.clicked.connect(self.table.clear_cells)
        self.table.clipboard_error.connect(self.error.setText)
        self.table.itemChanged.connect(self._item_changed)
        self.enabled.toggled.connect(self._update_enabled)
        self._loading = False
        self._update_enabled(enabled)

    def _use_current_value(self, target_row, grid, source_row, source_column):
        if not self.table.values_editable or source_row % 2 == 0:
            return
        source = grid.item(source_row, source_column)
        if source is None:
            return
        item = self.table.item(target_row, source_column + 1)
        item.setText(source.data(grid.RAW_VALUE_ROLE) or "")
        # A clicked empty cell is also an explicit decision.
        item.setData(Qt.ItemDataRole.UserRole, True)
        self.table.setCurrentItem(item)

    def _column_resized(self, column, _old_width, _new_width):
        self._resize_review_rows()

    def _resize_review_rows(self):
        width = self.table.columnWidth(0)
        for row, grid in enumerate(self._current_value_grids):
            height = grid.fit_to_width(width)
            self.table.resizeRowToContents(row)
            self.table.setRowHeight(row, max(self.table.rowHeight(row), height))

    def showEvent(self, event):
        super().showEvent(event)
        # Fonts and header metrics are final only after the dialog is polished.
        self._resize_review_rows()

    def _decorate_item(self, item):
        resolved = bool(item.data(Qt.ItemDataRole.UserRole))
        item.setBackground(QColor("#FFFFFF" if resolved else "#FFF1DA"))
        item.setToolTip(item.text() if resolved else "개인별 값이 다릅니다. 공용 값을 직접 입력하거나 붙여넣으세요. 빈 값으로 결정하려면 '선택한 값 비우기'를 누르세요.")

    def _item_changed(self, item):
        if self._loading or item.column() < 1:
            return
        self._loading = True
        item.setData(Qt.ItemDataRole.UserRole, True)
        self._decorate_item(item)
        self._loading = False
        self._resize_review_rows()
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
            for column in range(1, self.table.columnCount()):
                item = self.table.item(row, column)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable if enabled else item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._loading = False
        self.clear.setEnabled(enabled)
        self.error.setText("" if enabled else "공용 사용을 선택해야 값을 수정할 수 있습니다. 현재 값은 스크롤·복사로 확인할 수 있습니다.")

    def selected_values(self):
        if not self.enabled.isChecked():
            return {}
        result = {}
        for row_index, row in enumerate(self._rows):
            values = []
            for column, component in enumerate(self._components, 1):
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

    @dialog_workflow
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
            if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted or not dialog.enabled.isChecked():
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
            backup = save_revision(self._session, record, parent=parent)
        except Exception as error:
            self._message_box.critical(parent, "가구 공용 설정을 저장하지 못했습니다", str(error))
            return HouseholdSharedColumnOutcome()
        if backup.error is not None:
            self._message_box.warning(parent, "가구 공용 설정은 저장됐지만 자동 백업에 실패했습니다", str(backup.error))
        return HouseholdSharedColumnOutcome(committed_roster_after_save(self._session, prepared_roster))
