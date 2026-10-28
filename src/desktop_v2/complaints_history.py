"""Read-only complaints across everyone in the current operating database."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re

from PySide6.QtCore import QSortFilterProxyModel, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView, QLabel,
    QFileDialog, QHBoxLayout, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QSplitter, QTableView, QVBoxLayout, QWidget,
)

from src.services.person_history import build_history_tables
from src.services.database_export import build_complaints_workbook
from src.domain.export_security import require_export_password_setting
from src.domain.revisions import format_revision_timestamp
from src.excel.publication import publish_workbook
from .column_search import ColumnSearchHeader

from .ledger_reader import load_revision_chain
from .person_history import HistoryRowsModel
from .startup_loading import run_progress_task
from .theme import application_stylesheet, register_application_fonts


@dataclass(frozen=True)
class _Column:
    key: str
    label: str


_COLUMNS = tuple(_Column(key, label) for key, label in (
    ("received", "접수일"), ("name", "성명"), ("meal", "식종"),
    ("channel", "채널"), ("content", "내용"), ("actor", "변경자"),
    ("revision", "변경번호"), ("recorded", "기록 시각(KST)"),
))


class ComplaintFilterModel(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.searches = {}
        self.values = {}
        self.criteria = ()

    def lessThan(self, left, right):
        model = self.sourceModel()
        for key, order in self.criteria:
            column = next(i for i, item in enumerate(_COLUMNS) if item.key == key)
            a, b = (model.data(model.index(index.row(), column)) for index in (left, right))
            if key == "revision":
                def natural(value):
                    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
                                 for part in re.split(r"(\d+)", value))
                a, b = natural(a), natural(b)
            else:
                a, b = a.casefold(), b.casefold()
            if a != b:
                return a < b if order == Qt.SortOrder.AscendingOrder else a > b
        return left.row() < right.row()

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
        model = self.sourceModel()
        if query and not any(
            query in model.data(model.index(source_row, column)).casefold()
            for column in range(7)
        ):
            return False
        for column, definition in enumerate(_COLUMNS):
            value = model.data(model.index(source_row, column))
            if self.searches.get(definition.key, "") not in " ".join(value.split()).casefold():
                return False
            selected = self.values.get(definition.key)
            if selected is not None and value not in selected:
                return False
        return True


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
            codes = {
                str(change.entity_id): record.metadata.display_code or format_revision_timestamp(record.metadata.recorded_at)
                for record in revisions for change in record.changes if change.entity_kind == "complaint"
            }
            result = []
            for row in reversed(rows):
                values = dict(zip(headings, row))
                result.append((values["접수 시각(KST)"].date(), values["성명"], values["식종"],
                               values["채널"], values["내용"], values["변경자"],
                               codes.get(values["민원 ID"], ""), values["기록 시각(KST)"]))
            return tuple(column.label for column in _COLUMNS), tuple(result)

        headings, rows = run_progress_task(
            load, title="전체 민원을 불러오는 중입니다",
            message="현재 운영 파일에 저장된 모든 대상자의 민원을 읽는 중입니다.",
            parent=parent,
        )
        super().__init__(parent)
        self._database = database
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("전체 민원")
        self.resize(1180, 760)
        self.setMinimumSize(900, 560)
        layout = QVBoxLayout(self)
        title = QLabel("전체 민원")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        note = QLabel("현재 운영 파일의 모든 민원입니다. 명단의 검색·필터·식종과 관계없이 최근 접수부터 표시합니다. 접수일은 한국시간 기준입니다. 열 제목은 정렬, ▾는 필터입니다.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.search = QLineEdit()
        self.search.setPlaceholderText("접수일, 성명, 식종, 채널, 내용, 변경자, 변경번호 검색")
        self.search.setClearButtonEnabled(True)
        search_bar = QHBoxLayout()
        search_bar.addWidget(self.search, 1)
        self.clear_filters = QPushButton("전체 필터 해제")
        self.clear_filters.setObjectName("quietAction")
        self.clear_filters.clicked.connect(self._clear_filters)
        search_bar.addWidget(self.clear_filters)
        self.export_button = QPushButton("Excel 내보내기")
        self.export_button.setObjectName("primaryAction")
        self.export_button.setToolTip("현재 검색·필터·정렬 결과를 Excel로 저장합니다.")
        self.export_button.clicked.connect(self._export)
        search_bar.addWidget(self.export_button)
        layout.addLayout(search_bar)
        self.count_label = QLabel()
        layout.addWidget(self.count_label)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableView()
        self.table.setObjectName("complaintsHistoryTable")
        self.model = HistoryRowsModel(headings, rows, self)
        self.proxy = ComplaintFilterModel(self)
        self.proxy.setSourceModel(self.model)
        self.table.setModel(self.proxy)
        self.header = ColumnSearchHeader(
            self.table, values_provider=self.column_filter_values,
            selection_provider=lambda key: self.proxy.values.get(key),
            search_provider=lambda key: self.proxy.searches.get(key, ""),
        )
        self.table.setHorizontalHeader(self.header)
        self.header.replace_columns(_COLUMNS)
        self.header.search_changed.connect(self._set_column_search)
        self.header.values_changed.connect(self._set_column_values)
        self.header.sectionClicked.connect(self._cycle_sort)
        self.table.horizontalScrollBar().valueChanged.connect(self.header.update_positions)
        self.header.setToolTip("열 제목 클릭: 오름차순 → 내림차순 → 해제 · 먼저 선택한 열부터 우선 정렬 · ▾: 검색과 값 필터")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((120, 110, 90, 85, 260, 115, 190)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        # Recording metadata belongs in the complete selected-record details below.
        self.table.setColumnHidden(7, True)
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
        self.header.refresh_buttons()
        active = bool(text.strip() or self.proxy.searches or self.proxy.values)
        self.clear_filters.setEnabled(active)
        count, total = self.proxy.rowCount(), self.model.rowCount()
        self.export_button.setEnabled(bool(count))
        self.count_label.setText(
            "등록된 민원이 없습니다." if not total else
            "검색 결과가 없습니다. 검색어와 칼럼 필터를 해제하면 전체 민원을 볼 수 있습니다." if not count else
            f"전체 {total}건 · 검색 결과 {count}건" if active else f"전체 {total}건"
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


    def column_filter_values(self, key):
        column = next(i for i, item in enumerate(_COLUMNS) if item.key == key)
        return tuple(sorted({self.model.data(self.model.index(row, column))
                             for row in range(self.model.rowCount())}, key=str.casefold))

    def _set_column_search(self, key, text):
        query = " ".join(text.split()).casefold()
        if query:
            self.proxy.searches[key] = query
        else:
            self.proxy.searches.pop(key, None)
        self._filter(self.search.text())

    def _set_column_values(self, key, values):
        if values is None:
            self.proxy.values.pop(key, None)
        else:
            self.proxy.values[key] = frozenset(values)
        self._filter(self.search.text())

    def _clear_filters(self):
        self.proxy.searches.clear()
        self.proxy.values.clear()
        self.search.clear()
        self._filter("")

    def _cycle_sort(self, column):
        key = _COLUMNS[column].key
        criteria = list(self.proxy.criteria)
        found = next((i for i, (name, _) in enumerate(criteria) if name == key), None)
        if found is None:
            criteria.append((key, Qt.SortOrder.AscendingOrder))
        elif criteria[found][1] == Qt.SortOrder.AscendingOrder:
            criteria[found] = (key, Qt.SortOrder.DescendingOrder)
        else:
            criteria.pop(found)
        self.proxy.criteria = tuple(criteria)
        self.proxy.invalidate()
        self.proxy.sort(0 if criteria else -1)
        self.header.set_sort_criteria(criteria)

    def export_rows(self):
        return tuple(self.model.rows[self.proxy.mapToSource(self.proxy.index(row, 0)).row()][:7]
                     for row in range(self.proxy.rowCount()))

    def _export(self):
        rows = self.export_rows()
        if not rows:
            return
        chosen, _ = QFileDialog.getSaveFileName(
            self, "전체 민원 Excel 내보내기", str(Path.home() / f"민원_{date.today():%Y%m%d}.xlsx"),
            "Excel 통합 문서 (*.xlsx)", options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not chosen:
            return
        target = Path(chosen).with_suffix(".xlsx")
        try:
            exists = run_progress_task(target.exists, parent=self, title="저장 위치 확인", message="같은 이름의 파일을 확인합니다.")
            if exists and QMessageBox.question(self, "파일 덮어쓰기", "같은 이름의 파일이 있습니다. 덮어쓸까요?",
                                              QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                              QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            def save():
                roster = self._database.current_roster()
                snapshot = self._database.snapshot(roster.revision_id)
                password = require_export_password_setting(snapshot).password
                workbook = build_complaints_workbook(tuple(c.label for c in _COLUMNS[:7]), rows)
                try:
                    return publish_workbook(workbook, target, password=password, overwrite=exists)
                finally:
                    workbook.close()
            self.export_button.setEnabled(False)
            run_progress_task(save, parent=self, title="민원을 Excel로 내보내는 중입니다",
                              message="현재 조회 결과를 공통 Excel 비밀번호로 저장합니다.")
            QMessageBox.information(self, "Excel 저장 완료", f"{len(rows)}건의 민원을 저장했습니다.\n{target}")
        except Exception as error:
            QMessageBox.critical(self, "민원을 내보내지 못했습니다", str(error))
        finally:
            self.export_button.setEnabled(bool(self.proxy.rowCount()))
