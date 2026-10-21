"""Person history tabs with a focused meal-status presentation."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

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
from .meal_history import MEAL_HISTORY_HEADINGS, build_person_meal_history_rows
from .theme import application_stylesheet, register_application_fonts


_HISTORY_COLUMNS = {
    "정보 변경": (("항목", "항목"), ("이전 값", "이전 값"), ("변경 값", "변경 값"),
                  ("변경자", "변경자"), ("변경번호", "변경번호")),
    "민원": (("접수 시각(KST)", "접수일시"), ("식종", "식종"), ("채널", "접수 경로"),
             ("내용", "내용"), ("변경자", "기록자")),
}
_PRIVATE_HEADINGS = {"사람 UUID", "민원 ID", "대상 UUID", "성명"}


def _history_value(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return "" if value is None else str(value)


def _focused_rows(name, headings, rows):
    fields = _HISTORY_COLUMNS.get(name)
    ordered = tuple(reversed(rows))
    if fields is None or not all(source in headings for source, _ in fields):
        return headings, ordered, None
    indices = tuple(headings.index(source) for source, _ in fields)
    details = tuple("\n".join(
        f"{heading}: {_history_value(value)}"
        for heading, value in zip(headings, row, strict=True)
        if heading not in _PRIVATE_HEADINGS
    ) for row in ordered)
    return tuple(label for _, label in fields), tuple(
        tuple(row[index] for index in indices) for row in ordered
    ), details


def _period_text(period):
    start = period.starts_on.isoformat() if period.starts_on else "시작일 미지정"
    end = period.ends_on.isoformat() if period.ends_on else "종료일 미지정"
    return f"{start} ~ {end}"


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
        text = "" if value is None else str(value)
        if self._is_grouped_status_value(index):
            return text.replace(" · ", "\n")
        return text

    def _is_grouped_status_value(self, index: QModelIndex) -> bool:
        heading = self.headings[index.column()]
        if heading not in {"이전 값", "변경 값"} or "구분" not in self.headings:
            return False
        category = self.rows[index.row()][self.headings.index("구분")]
        return category == "상태 변경"

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
            headings, rows, detail_rows = _focused_rows(name, *tables[name])
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.addWidget(QLabel(f"{len(rows)}건" if rows else "기록된 이력이 없습니다."))
            if name == "식종 변경":
                note = QLabel("가입기간·배송배정 등 다른 변경과 상태 기록 삭제는 더보기 → 이력·민원 → 전체 변경 이력에서 확인할 수 있습니다.")
                note.setWordWrap(True)
                page_layout.addWidget(note)
            view = QTableView()
            view.setObjectName(f"history_{name}")
            view.setModel(HistoryRowsModel(headings, rows, view))
            view.model().detail_rows = detail_rows
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
            view.setWordWrap(True)
            view.setTextElideMode(Qt.TextElideMode.ElideNone)
            view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
            view.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            if name == "식종 변경":
                self._configure_meal_history_columns(view, headings)
            elif detail_rows is not None:
                widths = (130, 235, 235, 110, 145) if name == "정보 변경" else (150, 80, 100, 410, 110)
                for index, width in enumerate(widths):
                    view.setColumnWidth(index, width)
            page_layout.addWidget(view, 1)
            if name != "정보 변경":
                details = QPlainTextEdit()
                details.setObjectName(f"historyDetails_{name}")
                details.setReadOnly(True)
                details.setPlaceholderText("기록을 선택하면 전체 내용을 확인할 수 있습니다.")
                details.setMaximumHeight(170)
                page_layout.addWidget(details)
                view.selectionModel().currentRowChanged.connect(
                    lambda current, previous, table=view, target=details:
                    self._show_row_details(table, target, current)
                )
            if rows:
                view.selectRow(0)
            self.tabs.addTab(page, name)
        # Summarize resolved periods here; raw events remain in the ledger.
        periods = QTabWidget()
        for meal in (Meal.BREAKFAST, Meal.LUNCH, Meal.DINNER):
            panel = self._status_period_page(histories.get(meal), meal)
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
        details = getattr(model, "detail_rows", None)
        if details is not None:
            target.setPlainText(details[index.row()] if index.isValid() else "")
            return
        header = table.horizontalHeader()
        lines = [
            f"{model.headings[logical]}: {model.data(model.index(index.row(), logical))}"
            for visual in range(header.count())
            if not table.isColumnHidden(logical := header.logicalIndex(visual))
        ] if index.isValid() else []
        target.setPlainText(chr(10).join(lines))

    def _status_period_page(self, history, meal):
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel("저장된 상태 변경을 날짜별로 정리한 기간입니다. 가입기간 안에서 별도 변경이 없는 날짜는 기본 배송 상태를 따릅니다.")
        note.setWordWrap(True)
        layout.addWidget(note)
        intervals = tuple(reversed(history.intervals)) if history is not None else ()
        layout.addWidget(QLabel(f"{len(intervals)}개 기간" if intervals else "기록된 상태 변경이 없습니다."))
        view = QTableView()
        view.setObjectName(f"historyPeriods_{meal.name}")
        rows = tuple((_period_text(interval.period), interval.status.value) for interval in intervals)
        model = HistoryRowsModel(("기간", "상태"), rows, view)
        events = {event.event_id: event for event in history.events} if history is not None else {}
        detail_rows = []
        for interval in intervals:
            lines = [f"기간: {_period_text(interval.period)}", f"상태: {interval.status.value}", "", "관련 변경 기록"]
            for event_id in interval.source_event_ids:
                event = events[event_id]
                stamp = event.recorded_at.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")
                lines.append(f"{stamp} · {event.status.value} · {_period_text(event.period)}")
                if event.source_text:
                    lines.append(event.source_text)
            detail_rows.append("\n".join(lines))
        model.detail_rows = tuple(detail_rows)
        view.setModel(model)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        view.setAlternatingRowColors(True)
        view.setWordWrap(True)
        view.setTextElideMode(Qt.TextElideMode.ElideNone)
        view.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        view.setColumnWidth(0, 350)
        view.setColumnWidth(1, 120)
        layout.addWidget(view, 1)
        details = QPlainTextEdit()
        details.setObjectName(f"historyPeriodDetails_{meal.name}")
        details.setReadOnly(True)
        details.setMaximumHeight(170)
        details.setPlaceholderText("기간을 선택하면 관련 변경 기록을 확인할 수 있습니다.")
        layout.addWidget(details)
        view.selectionModel().currentRowChanged.connect(
            lambda current, previous: self._show_row_details(view, details, current)
        )
        if intervals:
            view.selectRow(0)
        return page

    @staticmethod
    def _configure_meal_history_columns(view, headings) -> None:
        widths = {
            "식종": 70,
            "구분": 90,
            "기간": 235,
            "변경번호": 145,
            "변경자": 110,
        }
        for heading, width in widths.items():
            if heading in headings:
                view.setColumnWidth(headings.index(heading), width)


def create_person_history_dialog(session, person, meal, parent=None):
    from .startup_loading import run_progress_task

    def load():
        roster = session.database.current_roster()
        if roster is None:
            raise ValueError("현재 명단이 없습니다.")
        current_person = roster.person(person.person_id)
        snapshot = session.database.snapshot(roster.revision_id)
        revisions = load_revision_chain(session.database, roster.revision_id)
        tables = build_history_tables(roster, snapshot, revisions, person_id=current_person.person_id)
        tables["식종 변경"] = (
            MEAL_HISTORY_HEADINGS,
            build_person_meal_history_rows(revisions, current_person.person_id),
        )
        histories = {
            item: session.database.status_history(current_person.person_id, item) for item in Meal
        }
        return current_person, tables, histories

    person, tables, histories = run_progress_task(
        load, title="개인별 이력을 불러오는 중입니다",
        message="선택한 사람의 변경 이력과 식종 상태를 읽는 중입니다.",
        parent=parent,
    )
    return PersonHistoryDialog(person, tables, histories, meal, parent)
