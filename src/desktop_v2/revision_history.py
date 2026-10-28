"""Browse the complete revision ledger and choose historical restore or branch."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping

from .dialog_workflow import launch

from datetime import datetime
from functools import partial
from uuid import UUID
from zoneinfo import ZoneInfo

from PySide6.QtCore import (
    QAbstractTableModel,
    QItemSelectionModel,
    QModelIndex,
    QSignalBlocker,
    QSortFilterProxyModel,
    Qt,
    QTimer,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QComboBox,
    QHBoxLayout,
    QFrame,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.services.revision_history import build_revision_change_rows, history_reason
from src.services.revision_branches import BRANCH_EVENT_KEY, list_branches
from src.services.revision_restore import RESTORE_TARGET_KEY

from .startup_loading import run_progress_task
from .theme import application_stylesheet, register_application_fonts

_KOREA_TIME_ZONE = ZoneInfo("Asia/Seoul")
_SUMMARY_HEADINGS = ("저장한 내용 · 변경번호",)
_DETAIL_HEADINGS = ("대상", "항목", "변경 내용")
_DETAIL_CACHE_MAX_ENTRIES = 8
_DETAIL_CACHE_MAX_TEXT_SIZE = 250_000


def _recorded_text(value: datetime) -> str:
    return value.astimezone(_KOREA_TIME_ZONE).strftime("%Y-%m-%d %H:%M:%S")


class RevisionSummaryModel(QAbstractTableModel):
    """Small metadata-only model; payloads never enter this model."""

    def __init__(self, summaries, parent=None):
        super().__init__(parent)
        self.branch_labels: dict[UUID, str] = {}
        self.summaries = tuple(
            sorted(summaries, key=lambda item: item.sequence, reverse=True)
        )

    def rowCount(self, parent=QModelIndex()):  # noqa: B008 - Qt immutable index
        return 0 if parent.isValid() else len(self.summaries)

    def columnCount(self, parent=QModelIndex()):  # noqa: B008 - Qt immutable index
        return 0 if parent.isValid() else len(_SUMMARY_HEADINGS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        summary = self.summaries[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return summary.revision_id
        if role not in (
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.ToolTipRole,
        ):
            return None
        label = self.branch_labels.get(summary.revision_id, "")
        prefix = f"{label} · " if label else ""
        return (
            f"{prefix}{history_reason(summary.reason)}\n"
            f"{_recorded_text(summary.recorded_at)} · {summary.actor.username}\n"
            f"변경번호 {summary.display_code}"
        )

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return _SUMMARY_HEADINGS[section]
        return section + 1

    def search_text(self, row: int) -> str:
        summary = self.summaries[row]
        return (
            f"{summary.display_code}\n{history_reason(summary.reason)}\n{summary.actor.username}\n"
            f"{_recorded_text(summary.recorded_at)}\n"
            f"변경번호 {summary.display_code}\n변경자 {summary.actor.username}"
        ).casefold()


class RevisionSummaryFilterModel(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._query = ""
        self._revision_ids: frozenset[UUID] | None = None

    def set_query(self, value: str) -> None:
        self._query = value.casefold().strip()
        self.invalidate()

    def set_revision_ids(self, revision_ids) -> None:
        self._revision_ids = None if revision_ids is None else frozenset(revision_ids)
        self.invalidate()

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        if self._revision_ids is not None and model.summaries[source_row].revision_id not in self._revision_ids:
            return False
        if not self._query:
            return True
        model = self.sourceModel()
        return self._query in model.search_text(source_row)


class RevisionChangeRowsModel(QAbstractTableModel):
    """Strict display-only rows returned by the safe history service."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: tuple[tuple[str, str, str, str], ...] = ()

    def set_rows(self, rows) -> None:
        values = tuple(rows)
        if any(
            not isinstance(row, tuple)
            or len(row) != 4
            or any(not isinstance(value, str) for value in row)
            for row in values
        ):
            raise TypeError("변경 이력 표시 행 형식이 올바르지 않습니다.")
        self.beginResetModel()
        self.rows = values
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):  # noqa: B008 - Qt immutable index
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: B008 - Qt immutable index
        return 0 if parent.isValid() else len(_DETAIL_HEADINGS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role not in (
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.ToolTipRole,
        ):
            return None
        target, _meal, column_label, change = self.rows[index.row()]
        return (target, column_label, change)[index.column()]

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return _DETAIL_HEADINGS[section]
        return section + 1


class GlobalRevisionHistoryDialog(QDialog):
    """Browse all revisions without mutating or exposing stored payloads."""

    def __init__(
        self,
        database,
        parent: QWidget | None = None,
        *,
        change_rows_builder=build_revision_change_rows,
    ) -> None:
        super().__init__(parent)
        self._database = database
        self._restore_controller = None
        self._branches = ()
        self._branch_read_failed = False
        columns = getattr(getattr(parent, "table_model", None), "columns", ())
        column_labels = {column.key: column.label for column in columns}
        self._change_rows_builder = (
            partial(build_revision_change_rows, column_labels=column_labels)
            if change_rows_builder is build_revision_change_rows
            else change_rows_builder
        )
        self._summary_load_failed = False
        self._loading_detail = False
        self._closed = False
        self._displayed_revision_id: UUID | None = None
        self._detail_cache: OrderedDict[
            UUID, tuple[tuple[str, str, str, str], ...]
        ] = OrderedDict()
        self._detail_cache_text_size = 0
        try:
            summaries = run_progress_task(
                database.revision_summaries,
                message="저장 기록 목록을 읽고 있습니다.",
                parent=self,
                title="변경 이력을 불러오는 중입니다",
            )
        except Exception:  # noqa: BLE001 - display a generic, payload-safe read error
            summaries = ()
            self._summary_load_failed = True

        register_application_fonts()
        self.setObjectName("globalRevisionHistoryDialog")
        self.setWindowTitle("전체 변경 이력")
        self.setModal(False)
        self.resize(1240, 760)
        self.setMinimumSize(1080, 680)
        self.setStyleSheet(application_stylesheet())

        self._summary_model = RevisionSummaryModel(summaries, self)
        self._summary_proxy = RevisionSummaryFilterModel(self)
        self._summary_proxy.setSourceModel(self._summary_model)
        self._detail_model = RevisionChangeRowsModel(self)
        self._build_ui()
        self._load_branches()
        self._update_summary_state()
        if self._summary_proxy.rowCount():
            self._summary_table.selectRow(0)
        else:
            self._clear_detail("선택할 변경 기록이 없습니다.")
        self._search.setFocus()

    def configure_actions(self, session, settings, on_restart) -> None:
        from .revision_restore import RevisionRestoreController
        self._restore_controller = RevisionRestoreController(session, settings, on_restart)
        self._restore_button.show()
        self._update_checkout_action()
        self._restore_button.setEnabled(self.selected_revision_id is not None)
        self._eyebrow.setText(f"운영 데이터 · {session.path.name}")
        self._eyebrow.setToolTip(str(session.path))

    def _restore_selected(self):
        if self._restore_controller is not None:
            launch(self._restore_controller.run, self.selected_revision_id, self)

    def _load_branches(self) -> None:
        # Older read-only adapters have no branch API; retain their audit view.
        if not hasattr(self._database, "branch_revision_ids"):
            return
        try:
            self._branches = run_progress_task(
                lambda: list_branches(self._database),
                message="분기 목록을 읽고 있습니다.", parent=self,
                title="분기를 불러오는 중입니다",
            )
        except Exception:
            self._branch_read_failed = True
            self._current_branch.setText("현재 분기를 읽지 못했습니다. 창을 다시 열어주세요.")
            self._current_branch.show()
            return
        active_index = 0
        with QSignalBlocker(self._branch_selector):
            for branch in self._branches:
                self._branch_selector.addItem(
                    branch.name + (" · 현재 사용 중" if branch.is_active else ""), branch.branch_id,
                )
                if branch.is_active:
                    active_index = self._branch_selector.count() - 1
                    self._current_branch.setText(f"현재 사용 중인 분기: {branch.name}")
            self._branch_selector.setCurrentIndex(active_index)
        self._branch_selector.setVisible(bool(self._branches))
        self._current_branch.setVisible(True)
        self._branch_changed()

    def _selected_branch(self):
        branch_id = self._branch_selector.currentData()
        return next((branch for branch in self._branches if branch.branch_id == branch_id), None)

    def _branch_changed(self, *_args) -> None:
        branch = self._selected_branch()
        labels = {}
        try:
            if branch is None:
                ids = None
            else:
                def read_branch_history():
                    ids = self._database.branch_revision_ids(branch.tip_revision_id)
                    source = branch.source_revision_id
                    before = () if source is None else self._database.branch_revision_ids(source)
                    return ids, source, before
                ids, source, before = run_progress_task(
                    read_branch_history, message="선택한 분기의 변경 기록을 읽고 있습니다.",
                    parent=self, title="분기 이력을 불러오는 중입니다",
                )
                labels = {revision_id: "분기 전 기록" for revision_id in before}
                if source is not None:
                    labels[source] = "분기 시작점"
        except Exception:
            self._current_branch.setText("분기의 변경 이력을 읽지 못했습니다. 창을 다시 열어주세요.")
            ids = ()
        self._summary_model.branch_labels = labels
        with QSignalBlocker(self._summary_table.selectionModel()):
            self._summary_proxy.set_revision_ids(ids)
        self._branch_note.setText(
            "모든 분기의 저장·전환 기록을 함께 표시합니다."
            if branch is None else
            f"‘{branch.name}’의 변경 기록입니다. 분기 시작점과 그 이전 기록도 선택할 수 있습니다. "
            "목록 선택만으로 사용 중인 분기는 바뀌지 않습니다."
        )
        self._update_checkout_action()
        self._apply_filter(self._search.text())
        self._summary_table.resizeRowsToContents()

    def _update_checkout_action(self) -> None:
        branch = self._selected_branch()
        self._checkout_button.setVisible(self._restore_controller is not None and bool(self._branches))
        self._checkout_button.setEnabled(branch is not None and not branch.is_active)

    def _checkout_selected_branch(self) -> None:
        branch = self._selected_branch()
        if self._restore_controller is not None and branch is not None and not branch.is_active:
            launch(self._restore_controller.checkout, branch.branch_id, branch.name, self)

    @property
    def selected_revision_id(self) -> UUID | None:
        index = self._summary_table.currentIndex()
        if not index.isValid():
            return None
        value = self._summary_proxy.data(
            self._summary_proxy.index(index.row(), 0),
            Qt.ItemDataRole.UserRole,
        )
        return value if isinstance(value, UUID) else None

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(10)

        header = QFrame()
        header.setObjectName("editSection")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(16, 13, 16, 14)
        header_layout.setSpacing(4)
        eyebrow = QLabel("운영 데이터 · 읽기 전용")
        eyebrow.setObjectName("editEyebrow")
        self._eyebrow = eyebrow
        title = QLabel("전체 변경 이력")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel(
            "최근 변경번호부터 확인합니다. 변경번호·사유·변경자로 검색할 수 있습니다."
        )
        subtitle.setObjectName("workspaceSubtitle")
        subtitle.setWordWrap(True)
        header_layout.addWidget(eyebrow)
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        self._current_branch = QLabel()
        self._current_branch.setObjectName("revisionHistoryCurrentBranch")
        self._current_branch.setTextFormat(Qt.TextFormat.PlainText)
        self._current_branch.setWordWrap(True)
        self._current_branch.hide()
        header_layout.addWidget(self._current_branch)
        root.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("revisionHistorySplitter")
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_summary_panel())
        splitter.addWidget(self._build_detail_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 8)
        splitter.setSizes((350, 820))
        root.addWidget(splitter, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.button(QDialogButtonBox.StandardButton.Close).setObjectName(
            "quietAction"
        )
        self._restore_button = QPushButton("선택한 변경번호로 복구·분기")
        self._restore_button.setObjectName("primaryAction")
        self._restore_button.clicked.connect(self._restore_selected)
        buttons.addButton(self._restore_button, QDialogButtonBox.ButtonRole.ActionRole)
        self._restore_button.hide()
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_summary_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("editSection")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        label = QLabel("저장 기록")
        label.setObjectName("sectionLabel")
        layout.addWidget(label)
        branch_row = QHBoxLayout()
        self._branch_selector = QComboBox()
        self._branch_selector.setObjectName("revisionHistoryBranchSelector")
        self._branch_selector.setAccessibleName("표시할 분기")
        self._branch_selector.addItem("전체 분기 · 감사 기록", None)
        self._branch_selector.hide()
        self._branch_selector.currentIndexChanged.connect(self._branch_changed)
        branch_row.addWidget(self._branch_selector, 1)
        self._checkout_button = QPushButton("이 분기로 전환")
        self._checkout_button.setObjectName("quietAction")
        self._checkout_button.clicked.connect(self._checkout_selected_branch)
        self._checkout_button.hide()
        branch_row.addWidget(self._checkout_button)
        layout.addLayout(branch_row)
        self._branch_note = QLabel()
        self._branch_note.setObjectName("revisionHistoryBranchNote")
        self._branch_note.setWordWrap(True)
        layout.addWidget(self._branch_note)
        self._search = QLineEdit()
        self._search.setObjectName("revisionHistorySearch")
        self._search.setAccessibleName("전체 변경 이력 검색")
        self._search.setPlaceholderText("변경번호, 사유, 변경자 검색")
        self._search.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(500)
        self._search_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._search_timer.timeout.connect(self._apply_pending_filter)
        self._search.textChanged.connect(self._schedule_filter)
        layout.addWidget(self._search)
        self._summary_status = QLabel()
        self._summary_status.setObjectName("revisionHistorySummaryStatus")
        self._summary_status.setWordWrap(True)
        layout.addWidget(self._summary_status)
        self._summary_table = QTableView()
        self._summary_table.setObjectName("revisionHistorySummaryTable")
        self._summary_table.setAccessibleName("전체 변경번호 목록")
        self._summary_table.setModel(self._summary_proxy)
        self._summary_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._summary_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._summary_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._summary_table.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self._summary_table.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self._summary_table.setAlternatingRowColors(True)
        self._summary_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self._summary_table.verticalHeader().setVisible(False)
        self._summary_table.setWordWrap(True)
        self._summary_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._summary_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._summary_table.selectionModel().currentRowChanged.connect(
            self._load_selected_revision
        )
        layout.addWidget(self._summary_table, 1)
        return panel

    def _build_detail_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("editSection")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        label = QLabel("변경 상세")
        label.setObjectName("sectionLabel")
        layout.addWidget(label)
        self._selection_title = QLabel("변경번호를 선택하세요.")
        self._selection_title.setObjectName("revisionHistorySelectionTitle")
        self._selection_title.setTextFormat(Qt.TextFormat.PlainText)
        self._selection_title.setWordWrap(True)
        layout.addWidget(self._selection_title)
        self._selection_meta = QLabel()
        self._selection_meta.setObjectName("revisionHistorySelectionMeta")
        self._selection_meta.setTextFormat(Qt.TextFormat.PlainText)
        self._selection_meta.setWordWrap(True)
        layout.addWidget(self._selection_meta)
        self._detail_status = QLabel()
        self._detail_status.setObjectName("revisionHistoryDetailStatus")
        self._detail_status.setWordWrap(True)
        layout.addWidget(self._detail_status)
        self._detail_table = QTableView()
        self._detail_table.setObjectName("revisionHistoryDetailTable")
        self._detail_table.setAccessibleName("선택한 변경번호의 변경 내용")
        self._detail_table.setModel(self._detail_model)
        self._detail_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._detail_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._detail_table.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self._detail_table.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self._detail_table.setAlternatingRowColors(True)
        self._detail_table.setWordWrap(True)
        self._detail_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self._detail_table.verticalHeader().setVisible(False)
        self._detail_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        header = self._detail_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self._detail_table.setColumnWidth(0, 130)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self._detail_table.setColumnWidth(1, 140)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._detail_table, 1)
        return panel

    def _schedule_filter(self, _text: str) -> None:
        self._search_timer.start()

    def _apply_pending_filter(self) -> None:
        if self._loading_detail:
            self._search_timer.start()
            return
        self._apply_filter(self._search.text())

    def done(self, result: int) -> None:
        self._closed = True
        self._search_timer.stop()
        self._dispose_detail_cache()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._closed = True
        self._search_timer.stop()
        self._dispose_detail_cache()
        super().closeEvent(event)

    def _dispose_detail_cache(self) -> None:
        self._detail_cache.clear()
        self._detail_cache_text_size = 0

    def _apply_filter(self, text: str) -> None:
        selected_revision_id = self.selected_revision_id
        selection_model = self._summary_table.selectionModel()
        with QSignalBlocker(selection_model):
            self._summary_proxy.set_query(text)
            self._update_summary_state()
            selected_row = self._visible_row(selected_revision_id)
            if selected_row is None and self._summary_proxy.rowCount():
                selected_row = 0
            if selected_row is None:
                selection_model.clearSelection()
                selection_model.setCurrentIndex(
                    QModelIndex(), QItemSelectionModel.SelectionFlag.NoUpdate
                )
                current = QModelIndex()
            else:
                current = self._summary_proxy.index(selected_row, 0)
                selection_model.setCurrentIndex(
                    current,
                    QItemSelectionModel.SelectionFlag.ClearAndSelect
                    | QItemSelectionModel.SelectionFlag.Rows,
                )
        if current.isValid():
            self._load_selected_revision(current, QModelIndex())
        else:
            self._clear_detail("검색 조건에 맞는 변경 기록이 없습니다.")

    def _visible_row(self, revision_id: UUID | None) -> int | None:
        if revision_id is None:
            return None
        for row in range(self._summary_proxy.rowCount()):
            value = self._summary_proxy.data(
                self._summary_proxy.index(row, 0), Qt.ItemDataRole.UserRole
            )
            if value == revision_id:
                return row
        return None

    def _update_summary_state(self) -> None:
        visible = self._summary_proxy.rowCount()
        total = self._summary_model.rowCount()
        if self._summary_proxy._revision_ids is not None:
            total = sum(summary.revision_id in self._summary_proxy._revision_ids
                        for summary in self._summary_model.summaries)
        if self._summary_load_failed:
            text = "변경 이력 목록을 읽지 못했습니다. 창을 닫고 다시 시도해주세요."
        elif not total:
            text = "저장된 변경 이력이 없습니다."
        elif self._search.text().strip() and not visible:
            text = "검색 결과가 없습니다."
        elif visible == total:
            text = f"{total}건 · 최근 저장 순"
        else:
            text = f"검색 결과 {visible}건 · 전체 {total}건"
        self._summary_status.setText(text)

    def _load_selected_revision(self, current, _previous) -> None:
        self._restore_button.setEnabled(current.isValid())
        if not current.isValid():
            return
        summary_index = self._summary_proxy.mapToSource(
            self._summary_proxy.index(current.row(), 0)
        )
        summary = self._summary_model.summaries[summary_index.row()]
        if summary.revision_id == self._displayed_revision_id:
            return
        self._displayed_revision_id = summary.revision_id
        self._detail_model.set_rows(())
        self._selection_title.setText(
            f"{summary.display_code} · {history_reason(summary.reason)}"
        )
        applied = (
            "적용일 없음"
            if summary.effective_on is None
            else f"적용일 {summary.effective_on.isoformat()}"
        )
        self._selection_meta.setText(
            f"{_recorded_text(summary.recorded_at)} · 변경자 {summary.actor.username} · {applied}"
        )
        cached_rows = self._detail_cache.get(summary.revision_id)
        if cached_rows is not None:
            self._detail_cache.move_to_end(summary.revision_id)
            self._detail_model.set_rows(cached_rows)
            self._update_detail_status()
            return

        self._detail_status.setText("변경 내용을 읽는 중입니다.")
        search_had_focus = self._search.hasFocus()
        active_window = QApplication.activeWindow()
        cursor_position = self._search.cursorPosition()
        selection_start = self._search.selectionStart()
        selected_text_length = len(self._search.selectedText())
        search_text = self._search.text()
        self._loading_detail = True
        rows_loaded = False
        load_failed = False
        try:
            def load_rows():
                record = self._database.revision(summary.revision_id)
                decisions = getattr(record.metadata, "decisions", {})
                event = decisions.get(BRANCH_EVENT_KEY)
                if isinstance(event, Mapping) and event.get("operation") in {"create", "checkout"}:
                    branch = next((item for item in self._branches
                                   if str(item.branch_id) == event.get("branch_id")), None)
                    name = branch.name if branch is not None else event.get("name", "선택한 분기")
                    target = UUID(decisions[RESTORE_TARGET_KEY])
                    code = self._database.revision_display_code(target)
                    operation = "분기 생성" if event["operation"] == "create" else "분기 전환"
                    target_label = "시작 변경번호" if event["operation"] == "create" else "전환 변경번호"
                    return (("운영 데이터", "", operation, f"분기: {name}\n{target_label}: {code}"),)
                snapshot = self._database.snapshot(summary.revision_id)
                parent_revision_id = record.metadata.parent_revision_id
                previous_snapshot = (
                    None
                    if parent_revision_id is None
                    else self._database.snapshot(parent_revision_id)
                )
                return self._change_rows_builder(
                    record,
                    snapshot,
                    previous_snapshot=previous_snapshot,
                )

            rows = run_progress_task(
                load_rows,
                message="선택한 기록의 변경 내용을 읽고 있습니다.",
                parent=self,
                title="변경 내용을 불러오는 중입니다",
            )
            if self._closed:
                return
            self._detail_model.set_rows(rows)
            rows_loaded = True
        except Exception:  # noqa: BLE001 - exception text may contain stored payloads
            load_failed = True
        finally:
            self._loading_detail = False
        if self._closed:
            return
        if load_failed:
            self._detail_model.set_rows(())
            self._detail_status.setText(
                "선택한 변경 내용을 읽지 못했습니다. 다른 변경번호를 선택하거나 창을 다시 열어주세요."
            )
        elif rows_loaded:
            self._remember_detail_rows(summary.revision_id, self._detail_model.rows)
            self._update_detail_status()
        current_active_window = QApplication.activeWindow()
        progress_window_just_closed = (
            current_active_window is not None
            and not current_active_window.isVisible()
            and current_active_window.parentWidget() is self.window()
        )
        progress_left_no_active_window = (
            current_active_window is None
            and QApplication.applicationState()
            is Qt.ApplicationState.ApplicationActive
        )
        if (
            search_had_focus
            and active_window is self.window()
            and (
                current_active_window is self.window()
                or progress_window_just_closed
                or progress_left_no_active_window
            )
        ):
            state = (
                cursor_position,
                selection_start,
                selected_text_length,
                search_text,
            )
            if current_active_window is self.window():
                self._restore_search_state(*state)
            else:
                self._activate_history_for_search_restore(*state)

    def _activate_history_for_search_restore(
        self,
        cursor_position: int,
        selection_start: int,
        selected_text_length: int,
        expected_text: str,
    ) -> None:
        if (
            self._closed
            or not self.isVisible()
            or self._search.text() != expected_text
            or QApplication.applicationState()
            is not Qt.ApplicationState.ApplicationActive
        ):
            return
        active_window = QApplication.activeWindow()
        if active_window is not None and not (
            not active_window.isVisible()
            and active_window.parentWidget() is self.window()
        ):
            return
        self.activateWindow()
        QTimer.singleShot(
            0,
            partial(
                self._restore_search_state,
                cursor_position,
                selection_start,
                selected_text_length,
                expected_text,
            ),
        )

    def _restore_search_state(
        self,
        cursor_position: int,
        selection_start: int,
        selected_text_length: int,
        expected_text: str,
    ) -> None:
        if self._closed or self._search.text() != expected_text:
            return
        active_window = QApplication.activeWindow()
        if active_window is not self.window():
            return
        self._search.setFocus(Qt.FocusReason.OtherFocusReason)
        if selection_start >= 0:
            if cursor_position == selection_start:
                self._search.setSelection(
                    selection_start + selected_text_length,
                    -selected_text_length,
                )
            else:
                self._search.setSelection(selection_start, selected_text_length)
        else:
            self._search.setCursorPosition(cursor_position)

    def _remember_detail_rows(
        self,
        revision_id: UUID,
        rows: tuple[tuple[str, str, str, str], ...],
    ) -> None:
        text_size = sum(len(value) for row in rows for value in row)
        if text_size > _DETAIL_CACHE_MAX_TEXT_SIZE:
            return
        previous = self._detail_cache.pop(revision_id, None)
        if previous is not None:
            self._detail_cache_text_size -= sum(
                len(value) for row in previous for value in row
            )
        while self._detail_cache and (
            len(self._detail_cache) >= _DETAIL_CACHE_MAX_ENTRIES
            or self._detail_cache_text_size + text_size > _DETAIL_CACHE_MAX_TEXT_SIZE
        ):
            _, evicted = self._detail_cache.popitem(last=False)
            self._detail_cache_text_size -= sum(
                len(value) for row in evicted for value in row
            )
        self._detail_cache[revision_id] = rows
        self._detail_cache_text_size += text_size

    def _update_detail_status(self) -> None:
        count = self._detail_model.rowCount()
        self._detail_status.setText(
            f"변경 내용 {count}건" if count else "표시할 변경 내용이 없습니다."
        )

    def _clear_detail(self, status: str) -> None:
        self._displayed_revision_id = None
        self._restore_button.setEnabled(False)
        self._detail_model.set_rows(())
        self._selection_title.setText("변경번호를 선택하세요.")
        self._selection_meta.clear()
        self._detail_status.setText(status)
