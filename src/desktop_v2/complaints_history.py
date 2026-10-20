"""Read-only complaints across everyone in the current operating database."""

from PySide6.QtCore import QSortFilterProxyModel, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView, QLabel,
    QLineEdit, QPlainTextEdit, QSplitter, QTableView, QVBoxLayout, QWidget,
)

from src.services.person_history import build_history_tables

from .ledger_reader import load_revision_chain
from .person_history import HistoryRowsModel
from .startup_loading import run_progress_task
from .theme import application_stylesheet, register_application_fonts


class ComplaintFilterModel(QSortFilterProxyModel):
    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        value = super().data(index, role)
        if role == Qt.ItemDataRole.DisplayRole and index.column() == 4 and isinstance(value, str):
            # Qt elides multi-line text even in an ElideNone single-line cell.
            # Keep the table compact; details and tooltips retain the literal text.
            return value.replace("\r", " ").replace("\n", " ")
        return value

    def set_query(self, text):
        self._query = text.strip().casefold()
        self.invalidate()

    def filterAcceptsRow(self, source_row, source_parent):
        query = getattr(self, "_query", "")
        if not query:
            return True
        model = self.sourceModel()
        return any(
            query in model.data(model.index(source_row, column)).casefold()
            for column in (1, 2, 3, 4)
        )


class ComplaintsHistoryDialog(QDialog):
    def __init__(self, database, parent=None):
        # Only immutable data is prepared on the worker; widgets stay on the UI thread.
        def load():
            roster = database.current_roster()
            if roster is None:
                raise ValueError("현재 명단이 없습니다.")
            snapshot = database.snapshot(roster.revision_id)
            revisions = load_revision_chain(database, roster.revision_id)
            headings, rows = build_history_tables(
                roster, snapshot, revisions, person_id=None,
            )["민원"]
            columns = tuple(headings.index(heading) for heading in (
                "접수 시각(KST)", "성명", "식종", "채널", "내용", "기록 시각(KST)", "변경자",
            ))
            return tuple(headings[column] for column in columns), tuple(
                tuple(row[column] for column in columns) for row in reversed(rows)
            )

        headings, rows = run_progress_task(
            load, title="전체 민원을 불러오는 중입니다",
            message="현재 운영 파일에 저장된 모든 대상자의 민원을 읽는 중입니다.",
            parent=parent,
        )
        super().__init__(parent)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("전체 민원")
        self.resize(1180, 760)
        self.setMinimumSize(900, 560)
        layout = QVBoxLayout(self)
        title = QLabel("전체 민원")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        note = QLabel("현재 운영 파일의 모든 민원입니다. 명단의 검색·필터·식종과 관계없이 최근 접수부터 표시합니다. 시각은 한국시간입니다.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.search = QLineEdit()
        self.search.setPlaceholderText("성명, 식종, 채널, 내용 검색")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        self.count_label = QLabel()
        layout.addWidget(self.count_label)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableView()
        self.table.setObjectName("complaintsHistoryTable")
        self.model = HistoryRowsModel(headings, rows, self)
        self.proxy = ComplaintFilterModel(self)
        self.proxy.setSourceModel(self.model)
        self.table.setModel(self.proxy)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((165, 110, 100, 75)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        # Recording metadata belongs in the complete selected-record details below.
        self.table.setColumnHidden(5, True)
        self.table.setColumnHidden(6, True)
        splitter.addWidget(self.table)
        details_panel = QWidget()
        details_layout = QVBoxLayout(details_panel)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.addWidget(QLabel("선택한 민원 전체 내용"))
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("민원을 선택하면 전체 내용을 확인하고 복사할 수 있습니다.")
        details_layout.addWidget(self.details)
        splitter.addWidget(details_panel)
        splitter.setSizes([390, 210])
        layout.addWidget(splitter, 1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        close.button(QDialogButtonBox.StandardButton.Close).setObjectName("quietAction")
        layout.addWidget(close)
        self.table.selectionModel().currentRowChanged.connect(self._show_details)
        self.search.textChanged.connect(self._filter)
        self._filter("")

    def _filter(self, text):
        self.proxy.set_query(text)
        count, total = self.proxy.rowCount(), self.model.rowCount()
        self.count_label.setText(
            "등록된 민원이 없습니다." if not total else
            "검색 결과가 없습니다. 검색어를 지우면 전체 민원을 볼 수 있습니다." if not count else
            f"전체 {total}건 · 검색 결과 {count}건" if text.strip() else f"전체 {total}건"
        )
        self.table.clearSelection()
        self.table.setCurrentIndex(self.proxy.index(-1, -1))
        self.details.clear()
        if count:
            self.table.selectRow(0)

    def _show_details(self, current, _previous):
        if not current.isValid():
            self.details.clear()
            return
        source = self.proxy.mapToSource(current)
        self.details.setPlainText("\n".join(
            f"{heading}: {self.model.data(self.model.index(source.row(), column))}"
            for column, heading in enumerate(self.model.headings)
        ))
