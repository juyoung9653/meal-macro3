"""Browse the complete revision ledger and choose historical restore or branch."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
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

from src.services.revision_history import build_revision_change_rows

from .theme import application_stylesheet, register_application_fonts

_KOREA_TIME_ZONE = ZoneInfo("Asia/Seoul")
_SUMMARY_HEADINGS = ("변경번호", "사유", "변경자")
_DETAIL_HEADINGS = ("대상", "식종", "구분", "변경 내용")


def _recorded_text(value: datetime) -> str:
    return value.astimezone(_KOREA_TIME_ZONE).strftime("%Y-%m-%d %H:%M:%S")


class RevisionSummaryModel(QAbstractTableModel):
    """Small metadata-only model; payloads never enter this model."""

    def __init__(self, summaries, parent=None):
        super().__init__(parent)
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
        values = (
            summary.display_code,
            summary.reason,
            summary.actor.username,
        )
        return values[index.column()]

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return _SUMMARY_HEADINGS[section]
        return section + 1

    def search_text(self, row: int) -> str:
        summary = self.summaries[row]
        return (
            f"{summary.display_code}\n{summary.reason}\n{summary.actor.username}"
        ).casefold()


class RevisionSummaryFilterModel(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._query = ""

    def set_query(self, value: str) -> None:
        self._query = value.casefold().strip()
        self.invalidate()

    def filterAcceptsRow(self, source_row, source_parent):
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
            or len(row) != len(_DETAIL_HEADINGS)
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
        return self.rows[index.row()][index.column()]

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
        self._change_rows_builder = change_rows_builder
        self._summary_load_failed = False
        try:
            summaries = database.revision_summaries()
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
        self._open_file_button.show()
        self._restore_button.setEnabled(self.selected_revision_id is not None)
        self._eyebrow.setText(f"운영 데이터 · {session.path.name}")
        self._eyebrow.setToolTip(str(session.path))

    def _restore_selected(self):
        if self._restore_controller is not None:
            self._restore_controller.run(self.selected_revision_id, self)

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
        root.setContentsMargins(20, 20, 20, 18)
        root.setSpacing(12)

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
        root.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("revisionHistorySplitter")
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_summary_panel())
        splitter.addWidget(self._build_detail_panel())
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 7)
        splitter.setSizes((410, 720))
        root.addWidget(splitter, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.button(QDialogButtonBox.StandardButton.Close).setObjectName(
            "quietAction"
        )
        self._restore_button = QPushButton("선택한 변경번호로 복구·분기")
        self._restore_button.setObjectName("primaryAction")
        self._restore_button.clicked.connect(self._restore_selected)
        self._restore_button.hide()
        buttons.addButton(self._restore_button, QDialogButtonBox.ButtonRole.ActionRole)
        self._open_file_button = QPushButton("다른 운영 파일 열기")
        self._open_file_button.setObjectName("quietAction")
        self._open_file_button.clicked.connect(lambda: self._restore_controller.open_existing(self))
        self._open_file_button.hide()
        buttons.addButton(self._open_file_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_summary_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("editSection")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        label = QLabel("변경번호 목록")
        label.setObjectName("sectionLabel")
        layout.addWidget(label)
        self._search = QLineEdit()
        self._search.setObjectName("revisionHistorySearch")
        self._search.setAccessibleName("전체 변경 이력 검색")
        self._search.setPlaceholderText("변경번호, 사유, 변경자 검색")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_filter)
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
        self._summary_table.setAlternatingRowColors(True)
        self._summary_table.verticalHeader().setVisible(False)
        self._summary_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self._summary_table.setColumnWidth(0, 130)
        self._summary_table.setColumnWidth(1, 145)
        self._summary_table.setColumnWidth(2, 65)
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
        label = QLabel("선택한 변경 내용")
        label.setObjectName("sectionLabel")
        layout.addWidget(label)
        self._selection_title = QLabel("변경번호를 선택하세요.")
        self._selection_title.setObjectName("revisionHistorySelectionTitle")
        self._selection_title.setWordWrap(True)
        layout.addWidget(self._selection_title)
        self._selection_meta = QLabel()
        self._selection_meta.setObjectName("revisionHistorySelectionMeta")
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
        self._detail_table.setAlternatingRowColors(True)
        self._detail_table.setWordWrap(True)
        self._detail_table.verticalHeader().setVisible(False)
        self._detail_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        header = self._detail_table.horizontalHeader()
        for column in range(3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        self._detail_table.setColumnWidth(0, 170)
        self._detail_table.setColumnWidth(1, 80)
        self._detail_table.setColumnWidth(2, 145)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._detail_table, 1)
        return panel

    def _apply_filter(self, text: str) -> None:
        self._summary_proxy.set_query(text)
        self._update_summary_state()
        if self._summary_proxy.rowCount():
            self._summary_table.selectRow(0)
        else:
            self._summary_table.clearSelection()
            self._summary_table.setCurrentIndex(QModelIndex())
            self._clear_detail("검색 조건에 맞는 변경 기록이 없습니다.")

    def _update_summary_state(self) -> None:
        visible = self._summary_proxy.rowCount()
        total = self._summary_model.rowCount()
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
        self._detail_model.set_rows(())
        if not current.isValid():
            return
        summary_index = self._summary_proxy.mapToSource(
            self._summary_proxy.index(current.row(), 0)
        )
        summary = self._summary_model.summaries[summary_index.row()]
        self._selection_title.setText(
            f"{summary.display_code} · {summary.reason}"
        )
        applied = (
            "적용일 없음"
            if summary.effective_on is None
            else f"적용일 {summary.effective_on.isoformat()}"
        )
        self._selection_meta.setText(
            f"{_recorded_text(summary.recorded_at)} · 변경자 {summary.actor.username} · {applied}"
        )
        self._detail_status.setText("변경 내용을 읽는 중입니다.")
        try:
            record = self._database.revision(summary.revision_id)
            snapshot = self._database.snapshot(summary.revision_id)
            parent_revision_id = record.metadata.parent_revision_id
            previous_snapshot = (
                None
                if parent_revision_id is None
                else self._database.snapshot(parent_revision_id)
            )
            rows = self._change_rows_builder(
                record,
                snapshot,
                previous_snapshot=previous_snapshot,
            )
            self._detail_model.set_rows(rows)
        except Exception:  # noqa: BLE001 - exception text may contain stored payloads
            self._detail_model.set_rows(())
            self._detail_status.setText(
                "선택한 변경 내용을 읽지 못했습니다. 다른 변경번호를 선택하거나 창을 다시 열어주세요."
            )
            return
        count = self._detail_model.rowCount()
        self._detail_status.setText(
            f"변경 내용 {count}건" if count else "표시할 변경 내용이 없습니다."
        )

    def _clear_detail(self, status: str) -> None:
        self._restore_button.setEnabled(False)
        self._detail_model.set_rows(())
        self._selection_title.setText("변경번호를 선택하세요.")
        self._selection_meta.clear()
        self._detail_status.setText(status)
