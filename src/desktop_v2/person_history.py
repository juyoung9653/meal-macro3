"""Person history tabs backed by the same rows used in Excel exports."""
from datetime import date, datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.domain.roster import Meal
from src.services.person_history import build_history_tables

from .ledger_reader import load_revision_chain
from .theme import application_stylesheet, register_application_fonts


class HistoryRowsModel(QAbstractTableModel):
    def __init__(self, headings, rows, parent=None):
        super().__init__(parent)
        self.headings, self.rows = headings, rows

    def rowCount(self, parent=QModelIndex()):  # noqa: B008 - Qt immutable default index
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: B008 - Qt immutable default index
        return 0 if parent.isValid() else len(self.headings)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        value = self.rows[index.row()][index.column()]
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M")
        if isinstance(value, date):
            return value.isoformat()
        return "" if value is None else str(value)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            return self.headings[section] if orientation == Qt.Orientation.Horizontal else section + 1


class PersonHistoryDialog(QDialog):
    def __init__(self, person, tables, histories, selected_meal, parent=None):
        super().__init__(parent)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle(f"{person.profile.name} · 이력보기")
        self.resize(1100, 640)
        layout = QVBoxLayout(self)
        title = QLabel(f"{person.profile.name}의 변경 이력과 민원")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        layout.addWidget(QLabel("최근 기록부터 표시합니다. 기록 시각은 한국시간입니다."))
        self.tabs = QTabWidget()
        for name in ("정보 변경", "식종 변경", "민원"):
            headings, rows = tables[name]
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.addWidget(QLabel(f"{len(rows)}건" if rows else "기록된 이력이 없습니다."))
            view = QTableView()
            view.setObjectName(f"history_{name}")
            view.setModel(HistoryRowsModel(headings, tuple(reversed(rows)), view))
            view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            view.setAlternatingRowColors(True)
            view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            view.resizeColumnsToContents()
            for index in range(len(headings)):
                view.setColumnWidth(index, min(320, max(110, view.columnWidth(index))))
            hidden_headings = {"사람 UUID", "민원 ID", "대상 UUID"}
            for index, heading in enumerate(headings):
                if heading in hidden_headings:
                    view.setColumnHidden(index, True)
            page_layout.addWidget(view, 1)
            details = QPlainTextEdit()
            details.setReadOnly(True)
            details.setPlaceholderText("기록을 선택하면 전체 내용을 확인할 수 있습니다.")
            details.setMaximumHeight(145)
            page_layout.addWidget(details)
            view.selectionModel().currentRowChanged.connect(
                lambda current, previous, table=view, target=details:
                self._show_row_details(table, target, current)
            )
            if rows:
                view.selectRow(0)
            self.tabs.addTab(page, name)
        # Keep calculated status periods available alongside the raw audit tables.
        from .roster_window import StatusHistoryDialog
        periods = QTabWidget()
        for meal in (Meal.BREAKFAST, Meal.LUNCH, Meal.DINNER):
            panel = StatusHistoryDialog(person, histories.get(meal), periods)
            panel.setWindowFlags(Qt.WindowType.Widget)
            periods.addTab(panel, meal.value)
            if meal is selected_meal:
                periods.setCurrentWidget(panel)
        self.tabs.addTab(periods, "상태 적용 기간")
        self.tabs.setCurrentIndex(1)
        layout.addWidget(self.tabs)
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        layout.addWidget(close)

    @staticmethod
    def _show_row_details(table, target, index):
        model = table.model()
        lines = [
            f"{heading}: {model.data(model.index(index.row(), column))}"
            for column, heading in enumerate(model.headings)
            if not table.isColumnHidden(column)
        ] if index.isValid() else []
        target.setPlainText(chr(10).join(lines))


def create_person_history_dialog(session, person, meal, parent=None):
    roster = session.database.current_roster()
    if roster is None:
        raise ValueError("현재 명단이 없습니다.")
    person = roster.person(person.person_id)
    snapshot = session.database.snapshot(roster.revision_id)
    revisions = load_revision_chain(session.database, roster.revision_id)
    tables = build_history_tables(roster, snapshot, revisions, person_id=person.person_id)
    histories = {
        item: session.database.status_history(person.person_id, item) for item in Meal
    }
    return PersonHistoryDialog(person, tables, histories, meal, parent)
