"""Date-spine roster workspace for the SQLite v2 desktop application."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from PySide6.QtCore import QAbstractTableModel, QDate, QModelIndex, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDateEdit,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.roster import CustomFieldDefinition, Meal, Person, RosterSnapshot
from src.domain.status import MealStatus, StatusHistory

from .theme import (
    DELIVERY,
    MUTED,
    NOT_ENROLLED,
    PAUSED,
    STOPPED,
    application_stylesheet,
    register_application_fonts,
)


_WEEKDAYS = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")
_STATUS_COLORS = {
    MealStatus.DELIVERY.value: DELIVERY,
    MealStatus.PAUSED.value: PAUSED,
    MealStatus.STOPPED.value: STOPPED,
    "미가입": NOT_ENROLLED,
}


@dataclass(frozen=True, slots=True)
class _Column:
    key: str
    label: str
    width: int
    custom_field: CustomFieldDefinition | None = None


class RosterTableModel(QAbstractTableModel):
    """Read-only current roster projection for one service date and meal."""

    PersonIdRole = Qt.ItemDataRole.UserRole + 1
    StatusRole = Qt.ItemDataRole.UserRole + 2

    def __init__(
        self,
        roster: RosterSnapshot,
        histories: Mapping[tuple[UUID, Meal], StatusHistory] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not isinstance(roster, RosterSnapshot):
            raise TypeError("roster는 RosterSnapshot이어야 합니다.")
        self._roster = roster
        self._histories = dict(histories or {})
        self._day = date.today()
        self._meal = Meal.LUNCH
        self._search = ""
        self._active_only = True
        self._columns = self._build_columns()
        self._sort_column = next(
            index
            for index, column in enumerate(self._columns)
            if column.key == "sequence"
        )
        self._sort_order = Qt.SortOrder.AscendingOrder
        self._people: tuple[Person, ...] = ()
        self._refresh_people()

    @property
    def columns(self) -> tuple[_Column, ...]:
        return self._columns

    @property
    def service_day(self) -> date:
        return self._day

    @property
    def meal(self) -> Meal:
        return self._meal

    def set_context(self, day: date, meal: Meal) -> None:
        if not isinstance(day, date):
            raise TypeError("day는 date여야 합니다.")
        if not isinstance(meal, Meal):
            raise TypeError("meal은 Meal이어야 합니다.")
        self.beginResetModel()
        self._day = day
        self._meal = meal
        self._refresh_people()
        self.endResetModel()

    def set_search(self, text: str) -> None:
        if not isinstance(text, str):
            raise TypeError("검색어는 문자열이어야 합니다.")
        normalized = " ".join(text.split()).casefold()
        if normalized == self._search:
            return
        self.beginResetModel()
        self._search = normalized
        self._refresh_people()
        self.endResetModel()

    def set_active_only(self, active_only: bool) -> None:
        if not isinstance(active_only, bool):
            raise TypeError("active_only는 bool이어야 합니다.")
        if active_only == self._active_only:
            return
        self.beginResetModel()
        self._active_only = active_only
        self._refresh_people()
        self.endResetModel()

    def replace_roster(
        self,
        roster: RosterSnapshot,
        histories: Mapping[tuple[UUID, Meal], StatusHistory] | None = None,
    ) -> None:
        if not isinstance(roster, RosterSnapshot):
            raise TypeError("roster는 RosterSnapshot이어야 합니다.")
        self.beginResetModel()
        self._roster = roster
        self._histories = dict(histories or {})
        self._columns = self._build_columns()
        if self._sort_column >= len(self._columns):
            self._sort_column = 0
        self._refresh_people()
        self.endResetModel()

    def sort(
        self,
        column: int,
        order: Qt.SortOrder = Qt.SortOrder.AscendingOrder,
    ) -> None:
        if not 0 <= column < len(self._columns):
            return
        self.beginResetModel()
        self._sort_column = column
        self._sort_order = order
        self._refresh_people()
        self.endResetModel()

    def person_at(self, row: int) -> Person:
        return self._people[row]

    def status_for(self, person: Person) -> str:
        if self._meal not in self._roster.meals_for(person.person_id, on=self._day):
            return "미가입"
        history = self._histories.get((person.person_id, self._meal))
        if history is None:
            return MealStatus.DELIVERY.value
        return history.status_on(self._day).value

    def status_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in (*_STATUS_COLORS,)}
        for person in self._people:
            counts[self.status_for(person)] += 1
        return counts

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._people)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        person = self._people[index.row()]
        column = self._columns[index.column()]
        status = self.status_for(person)
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_value(person, column, status)
        if role == self.PersonIdRole:
            return person.person_id
        if role == self.StatusRole:
            return status
        if role == Qt.ItemDataRole.ForegroundRole and column.key == "status":
            return QColor(_STATUS_COLORS[status])
        if role == Qt.ItemDataRole.FontRole and column.key in {"status", "name"}:
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if column.key in {"status", "birth_date", "meals", "grade"}:
                return Qt.AlignmentFlag.AlignCenter
        if role == Qt.ItemDataRole.ToolTipRole:
            value = self._display_value(person, column, status)
            return None if not value else str(value)
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self._columns[section].label
        return section + 1

    def _build_columns(self) -> tuple[_Column, ...]:
        base = (
            _Column("status", "상태", 82),
            _Column("name", "성명", 92),
            _Column("birth_date", "생년월일", 96),
            _Column("meals", "이용 식종", 110),
            _Column("school", "학교", 120),
            _Column("grade", "학년", 82),
            _Column("mobile", "핸드폰", 118),
            _Column("jurisdiction", "관할동", 82),
            _Column("address", "주소", 220),
            _Column("driver", "배송담당", 92),
            _Column("sequence", "순서", 68),
        )
        custom = tuple(
            _Column(
                f"custom:{definition.definition_id}",
                definition.name,
                105,
                definition,
            )
            for definition in sorted(
                self._roster.custom_fields,
                key=lambda item: item.display_order,
            )
        )
        return (*base, *custom)

    def _refresh_people(self) -> None:
        people: list[Person] = []
        for person in self._roster.persons:
            meals = self._roster.meals_for(person.person_id, on=self._day)
            if self._active_only and self._meal not in meals:
                continue
            if self._search and self._search not in self._search_text(person):
                continue
            people.append(person)
        reverse = self._sort_order == Qt.SortOrder.DescendingOrder
        self._people = tuple(
            sorted(
                people,
                key=self._sort_key,
                reverse=reverse,
            )
        )

    def _sort_key(self, person: Person) -> tuple[object, ...]:
        column = self._columns[self._sort_column]
        value = self._display_value(person, column, self.status_for(person))
        text = "" if value is None else str(value).strip()
        if not text:
            normalized: tuple[int, object] = (2, "")
        elif text.isdigit():
            normalized = (0, int(text))
        else:
            normalized = (1, text.casefold())
        return (*normalized, person.profile.name.casefold(), str(person.person_id))

    def _search_text(self, person: Person) -> str:
        profile = person.profile
        return " ".join(
            value.casefold()
            for value in (
                profile.name,
                profile.school_name or "",
                profile.mobile_phone or "",
                profile.road_address or "",
                profile.detail_address or "",
                profile.jurisdiction or "",
            )
        )

    def _display_value(self, person: Person, column: _Column, status: str):
        profile = person.profile
        assignment = self._roster.resolve_assignment(person.person_id, self._meal)
        if column.key == "status":
            return status
        if column.key == "name":
            return profile.name
        if column.key == "birth_date":
            return "" if profile.birth_date is None else profile.birth_date.isoformat()
        if column.key == "meals":
            return " · ".join(
                meal.value
                for meal in self._roster.meals_for(person.person_id, on=self._day)
            )
        if column.key == "school":
            return profile.school_name or ""
        if column.key == "grade":
            return profile.grade or ""
        if column.key == "mobile":
            return profile.mobile_phone or ""
        if column.key == "jurisdiction":
            return profile.jurisdiction or ""
        if column.key == "address":
            return " ".join(
                value for value in (profile.road_address, profile.detail_address) if value
            )
        if column.key == "driver":
            return assignment.driver or ""
        if column.key == "sequence":
            return assignment.sequence or ""
        if column.custom_field is not None:
            value = profile.custom_values.get(column.custom_field.definition_id)
            if isinstance(value, date):
                return value.isoformat()
            return "" if value is None else str(value)
        return ""


class StatusHistoryDialog(QDialog):
    """Show calculated periods and preserved raw events side by side."""

    def __init__(
        self,
        person: Person,
        history: StatusHistory | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{person.profile.name} · 상태 이력")
        self.resize(760, 480)
        layout = QVBoxLayout(self)
        title = QLabel(f"{person.profile.name}의 {history.meal.value if history else ''} 상태 이력")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        if history is None or not history.events:
            empty = QLabel("기록된 상태 변경이 없습니다. 현재 기본 상태는 배송입니다.")
            empty.setObjectName("muted")
            layout.addWidget(empty)
            layout.addStretch(1)
            return

        layout.addWidget(_section_label("계산된 유효 기간"))
        effective = QTableView()
        effective.setModel(_SimpleRowsModel(
            ("상태", "시작일", "종료일", "근거 사건"),
            tuple(
                (
                    interval.status.value,
                    _date_text(interval.period.starts_on, "이전부터"),
                    _date_text(interval.period.ends_on, "계속"),
                    str(len(interval.source_event_ids)),
                )
                for interval in history.intervals
            ),
            effective,
        ))
        effective.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(effective, 1)

        layout.addWidget(_section_label("원문 사건"))
        raw = QTableView()
        raw.setModel(_SimpleRowsModel(
            ("기록 시각", "입력 상태", "시작일", "종료일", "원문"),
            tuple(
                (
                    event.recorded_at.strftime("%Y-%m-%d %H:%M"),
                    event.status.value,
                    _date_text(event.period.starts_on, "열림"),
                    _date_text(event.period.ends_on, "열림"),
                    event.source_text or "",
                )
                for event in history.events
            ),
            raw,
        ))
        raw.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(raw, 1)


class _SimpleRowsModel(QAbstractTableModel):
    def __init__(self, headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...], parent=None):
        super().__init__(parent)
        self._headers = headers
        self._rows = rows

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._headers)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and index.isValid():
            return self._rows[index.row()][index.column()]
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self._headers[section]
        return None


class RosterWindow(QMainWindow):
    """Operational shell; persistence is supplied by a future application controller."""

    status_action_requested = Signal(object, object, object)
    complaint_requested = Signal(object, object)
    import_review_requested = Signal()
    person_edit_requested = Signal(object)
    service_adjustment_requested = Signal()
    school_calendar_requested = Signal()
    unit_price_requested = Signal()
    documents_requested = Signal()
    document_output_selected = Signal(object)
    document_generated = Signal(object)
    settings_requested = Signal()

    def __init__(
        self,
        roster: RosterSnapshot,
        histories: Mapping[tuple[UUID, Meal], StatusHistory] | None = None,
        parent: QWidget | None = None,
        *,
        revision_display_code: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._roster = roster
        self._histories = dict(histories or {})
        self._revision_display_code = revision_display_code
        self._model = RosterTableModel(roster, self._histories, self)
        self._meal = Meal.LUNCH
        self._history_dialogs: list[StatusHistoryDialog] = []
        register_application_fonts()
        self.setWindowTitle("급식 배송 운영 · SQLite v2")
        self.resize(1440, 860)
        self.setMinimumSize(1080, 680)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()
        self._set_day(date.today())
        self._set_meal(Meal.LUNCH)

    @property
    def table_model(self) -> RosterTableModel:
        return self._model

    @property
    def roster(self) -> RosterSnapshot:
        """Current immutable roster displayed by this window."""

        return self._roster

    def replace_roster(
        self,
        roster: RosterSnapshot,
        histories: Mapping[tuple[UUID, Meal], StatusHistory] | None = None,
        *,
        revision_display_code: str | None = None,
    ) -> None:
        if not isinstance(roster, RosterSnapshot):
            raise TypeError("roster는 RosterSnapshot이어야 합니다.")
        self._roster = roster
        self._histories = dict(histories or {})
        self._revision_display_code = revision_display_code
        self._model.replace_roster(roster, self._histories)
        self._revision_label.setText(self._revision_label_text())
        for index, column in enumerate(self._model.columns):
            self._table.horizontalHeader().resizeSection(index, column.width)
        self._after_model_reset()

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._build_date_spine())
        root_layout.addWidget(self._build_workspace(), 1)
        self.setCentralWidget(root)

    def _build_date_spine(self) -> QFrame:
        spine = QFrame()
        spine.setObjectName("dateSpine")
        spine.setFixedWidth(228)
        layout = QVBoxLayout(spine)
        layout.setContentsMargins(20, 24, 20, 22)
        layout.setSpacing(10)

        eyebrow = QLabel("BUNDANG MEAL ROUTE")
        eyebrow.setObjectName("productEyebrow")
        layout.addWidget(eyebrow)
        self._date_label = QLabel()
        self._date_label.setObjectName("serviceDate")
        layout.addWidget(self._date_label)
        self._weekday_label = QLabel()
        self._weekday_label.setObjectName("serviceWeekday")
        layout.addWidget(self._weekday_label)

        date_controls = QHBoxLayout()
        previous = _date_button("‹", "이전 날짜")
        previous.clicked.connect(lambda: self._shift_day(-1))
        next_day = _date_button("›", "다음 날짜")
        next_day.clicked.connect(lambda: self._shift_day(1))
        self._date_edit = QDateEdit()
        self._date_edit.setObjectName("serviceDateEdit")
        self._date_edit.setCalendarPopup(True)
        self._date_edit.setDisplayFormat("MM.dd")
        self._date_edit.dateChanged.connect(self._on_qdate_changed)
        date_controls.addWidget(previous)
        date_controls.addWidget(self._date_edit, 1)
        date_controls.addWidget(next_day)
        layout.addLayout(date_controls)

        today = QPushButton("오늘로 이동")
        today.setObjectName("quietAction")
        today.clicked.connect(lambda: self._set_day(date.today()))
        layout.addWidget(today)
        layout.addSpacing(12)

        meal_label = QLabel("식종 LANE")
        meal_label.setObjectName("spineLabel")
        layout.addWidget(meal_label)
        self._meal_group = QButtonGroup(self)
        self._meal_group.setExclusive(True)
        self._meal_buttons: dict[Meal, QPushButton] = {}
        for meal, hint in (
            (Meal.BREAKFAST, "주말분 묶음배송"),
            (Meal.LUNCH, "학교 일정 반영"),
            (Meal.DINNER, "일별 배송 상태"),
        ):
            button = QPushButton(f"{meal.value}   {hint}")
            button.setObjectName("mealLane")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, selected=meal: self._set_meal(selected))
            self._meal_group.addButton(button)
            self._meal_buttons[meal] = button
            layout.addWidget(button)

        layout.addSpacing(14)
        count_label = QLabel("선택 명단")
        count_label.setObjectName("spineLabel")
        layout.addWidget(count_label)
        self._count_metric = QLabel()
        self._count_metric.setObjectName("spineMetric")
        layout.addWidget(self._count_metric)
        self._status_metric = QLabel()
        self._status_metric.setObjectName("serviceWeekday")
        self._status_metric.setWordWrap(True)
        layout.addWidget(self._status_metric)
        layout.addStretch(1)
        self._revision_label = QLabel(self._revision_label_text())
        self._revision_label.setObjectName("serviceWeekday")
        self._revision_label.setToolTip(
            "이 명단 상태를 나중에 다시 찾을 때 사용하는 변경번호입니다."
        )
        layout.addWidget(self._revision_label)
        return spine

    def _revision_label_text(self) -> str:
        display_code = self._revision_display_code
        if display_code is None:
            display_code = str(self._roster.revision_id)[:8]
        return f"변경번호\n{display_code}"

    def _build_workspace(self) -> QWidget:
        workspace = QWidget()
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        layout.addWidget(self._build_header())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_table_frame())
        splitter.addWidget(self._build_inspector())
        splitter.setStretchFactor(0, 1)
        splitter.setSizes([900, 310])
        layout.addWidget(splitter, 1)
        return workspace

    def _build_header(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("workspaceHeader")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 12, 14, 12)
        layout.setSpacing(9)

        heading = QHBoxLayout()
        heading.setSpacing(9)
        title_column = QVBoxLayout()
        title = QLabel("대상자 명단")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel("선택일의 가입·상태·배송 배정을 한 자리에서 확인합니다.")
        subtitle.setObjectName("workspaceSubtitle")
        title_column.addWidget(title)
        title_column.addWidget(subtitle)
        heading.addLayout(title_column)
        heading.addStretch(1)
        self._search = QLineEdit()
        self._search.setPlaceholderText("성명, 학교, 전화, 주소 검색")
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumWidth(260)
        self._search.textChanged.connect(self._on_search_changed)
        heading.addWidget(self._search)
        show_all = QPushButton("미가입 포함")
        show_all.setObjectName("quietAction")
        show_all.setCheckable(True)
        show_all.toggled.connect(lambda checked: self._set_active_only(not checked))
        heading.addWidget(show_all)
        layout.addLayout(heading)

        actions = QHBoxLayout()
        actions.setSpacing(7)
        actions.addStretch(1)
        self._school_calendar_button = QPushButton("학교 일정")
        self._school_calendar_button.setObjectName("quietAction")
        self._school_calendar_button.setToolTip(
            "DB에 저장된 학교 일정을 수정하거나 Excel에서 다시 가져옵니다"
        )
        self._school_calendar_button.clicked.connect(
            self.school_calendar_requested.emit
        )
        actions.addWidget(self._school_calendar_button)
        self._service_adjustment_button = QPushButton("휴무·공휴일")
        self._service_adjustment_button.setObjectName("quietAction")
        self._service_adjustment_button.setToolTip(
            "공휴일 운영 검토와 업체 자체 휴무, 청구 제외·대체식을 관리합니다"
        )
        self._service_adjustment_button.clicked.connect(
            self.service_adjustment_requested.emit
        )
        actions.addWidget(self._service_adjustment_button)
        self._unit_price_button = QPushButton("단가 관리")
        self._unit_price_button.setObjectName("quietAction")
        self._unit_price_button.setToolTip("조식·중식·석식 청구 단가와 적용일을 관리합니다")
        self._unit_price_button.clicked.connect(self.unit_price_requested.emit)
        actions.addWidget(self._unit_price_button)
        self._documents_button = QPushButton("문서 만들기")
        self._documents_button.setObjectName("documentHubAction")
        self._documents_button.setToolTip(
            "청구서·확인서·배송 리포트·배송대타를 한 곳에서 선택합니다"
        )
        self._documents_button.clicked.connect(self.documents_requested.emit)
        actions.addWidget(self._documents_button)
        self._settings_button = QPushButton("설정")
        self._settings_button.setObjectName("quietAction")
        self._settings_button.setToolTip(
            "생성 Excel 비밀번호와 문서 저장 폴더를 관리합니다"
        )
        self._settings_button.clicked.connect(self.settings_requested.emit)
        actions.addWidget(self._settings_button)
        import_button = QPushButton("Excel 명단 가져오기")
        import_button.setObjectName("primaryAction")
        import_button.clicked.connect(self.import_review_requested.emit)
        actions.addWidget(import_button)
        layout.addLayout(actions)
        return frame

    def _build_table_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("tableFrame")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._open_context_menu)
        self._table.selectionModel().selectionChanged.connect(self._update_inspector)
        self._table.verticalHeader().setDefaultSectionSize(35)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        for index, column in enumerate(self._model.columns):
            header.resizeSection(index, column.width)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        sequence_column = next(
            index
            for index, column in enumerate(self._model.columns)
            if column.key == "sequence"
        )
        header.setSortIndicator(sequence_column, Qt.SortOrder.AscendingOrder)
        header.setToolTip("열 이름을 누르면 해당 항목 순서로 정렬됩니다.")
        self._table.setSortingEnabled(True)
        layout.addWidget(self._table)
        return frame

    def _build_inspector(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("inspector")
        frame.setMinimumWidth(280)
        frame.setMaximumWidth(390)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(9)
        heading = QHBoxLayout()
        label = QLabel("선택 정보")
        label.setObjectName("sectionLabel")
        heading.addWidget(label)
        heading.addStretch(1)
        self._edit_person_button = QPushButton("정보 수정")
        self._edit_person_button.setObjectName("quietAction")
        self._edit_person_button.setEnabled(False)
        self._edit_person_button.setToolTip("선택한 사람의 인적사항을 직접 수정합니다")
        self._edit_person_button.clicked.connect(self._request_selected_person_edit)
        heading.addWidget(self._edit_person_button)
        layout.addLayout(heading)
        self._inspector_name = QLabel("명단에서 한 사람을 선택하세요")
        self._inspector_name.setObjectName("personName")
        self._inspector_name.setWordWrap(True)
        layout.addWidget(self._inspector_name)
        self._inspector_status = QLabel("—")
        self._inspector_status.setObjectName("statusBadge")
        self._inspector_status.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._inspector_status)
        self._inspector_detail = QLabel("가구와 배송 배정이 여기에 표시됩니다.")
        self._inspector_detail.setObjectName("muted")
        self._inspector_detail.setWordWrap(True)
        layout.addWidget(self._inspector_detail)
        layout.addSpacing(10)
        household_label = QLabel("같은 가구 · 선택일 기준")
        household_label.setObjectName("sectionLabel")
        layout.addWidget(household_label)
        self._household_list = QListWidget()
        self._household_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout.addWidget(self._household_list, 1)
        guide = QLabel("우클릭: 배송 · 일시중지 · 중지 · 이력보기 · 민원")
        guide.setObjectName("muted")
        guide.setWordWrap(True)
        layout.addWidget(guide)
        return frame

    def _create_context_menu(self, person: Person) -> QMenu:
        menu = QMenu(self)
        for label, status in (
            ("배송", MealStatus.DELIVERY),
            ("일시중지", MealStatus.PAUSED),
            ("중지", MealStatus.STOPPED),
        ):
            action = QAction(label, menu)
            action.triggered.connect(
                lambda _checked=False, selected=status: self.status_action_requested.emit(
                    person.person_id,
                    self._meal,
                    selected,
                )
            )
            menu.addAction(action)
        history = QAction("이력보기", menu)
        history.triggered.connect(lambda: self._show_history(person))
        menu.addAction(history)
        complaint = QAction("민원", menu)
        complaint.triggered.connect(
            lambda: self.complaint_requested.emit(person.person_id, self._meal)
        )
        menu.addAction(complaint)
        return menu

    def _open_context_menu(self, position) -> None:
        index = self._table.indexAt(position)
        if not index.isValid():
            return
        self._table.selectRow(index.row())
        person = self._model.person_at(index.row())
        self._create_context_menu(person).exec(self._table.viewport().mapToGlobal(position))

    def _show_history(self, person: Person) -> None:
        history = self._histories.get((person.person_id, self._meal))
        dialog = StatusHistoryDialog(person, history, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.destroyed.connect(lambda: self._history_dialogs.remove(dialog))
        self._history_dialogs.append(dialog)
        dialog.show()

    def _set_day(self, day: date) -> None:
        self._date_edit.blockSignals(True)
        self._date_edit.setDate(QDate(day.year, day.month, day.day))
        self._date_edit.blockSignals(False)
        self._date_label.setText(day.strftime("%Y.%m.%d"))
        self._weekday_label.setText(f"{_WEEKDAYS[day.weekday()]} · 서비스 기준일")
        self._model.set_context(day, self._meal)
        self._after_model_reset()

    def _set_meal(self, meal: Meal) -> None:
        self._meal = meal
        self._meal_buttons[meal].setChecked(True)
        self._model.set_context(self._model.service_day, meal)
        self._after_model_reset()

    def _shift_day(self, offset: int) -> None:
        shifted = date.fromordinal(self._model.service_day.toordinal() + offset)
        self._set_day(shifted)

    def _on_qdate_changed(self, value: QDate) -> None:
        self._set_day(date(value.year(), value.month(), value.day()))

    def _on_search_changed(self, text: str) -> None:
        self._model.set_search(text)
        self._after_model_reset()

    def _set_active_only(self, active_only: bool) -> None:
        self._model.set_active_only(active_only)
        self._after_model_reset()

    def _after_model_reset(self) -> None:
        self._table.clearSelection()
        self._clear_inspector()
        counts = self._model.status_counts()
        self._count_metric.setText(f"{self._model.rowCount()}명")
        self._status_metric.setText(
            f"배송 {counts[MealStatus.DELIVERY.value]}  ·  "
            f"일시중지 {counts[MealStatus.PAUSED.value]}  ·  "
            f"중지 {counts[MealStatus.STOPPED.value]}"
        )

    def _update_inspector(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            self._clear_inspector()
            return
        person = self._model.person_at(rows[0].row())
        self._edit_person_button.setEnabled(True)
        profile = person.profile
        status = self._model.status_for(person)
        assignment = self._roster.resolve_assignment(person.person_id, self._meal)
        self._inspector_name.setText(profile.name)
        self._inspector_status.setText(status)
        self._inspector_status.setStyleSheet(
            f"background: {_STATUS_COLORS[status]}; color: white;"
        )
        address = " ".join(
            value
            for value in (profile.road_address, profile.detail_address)
            if value
        )
        details = [
            f"{self._meal.value} 배송담당  {assignment.driver or '미지정'}",
            f"배송순서  {assignment.sequence or '미지정'}",
            f"연락처  {profile.mobile_phone or profile.telephone or '미등록'}",
            f"주소  {address or '미등록'}",
        ]
        self._inspector_detail.setText("\n".join(details))
        self._household_list.clear()
        household_ids = {
            membership.household_id
            for membership in self._roster.memberships
            if membership.person_id == person.person_id
            and membership.period.contains(self._model.service_day)
        }
        members: list[Person] = []
        for household_id in household_ids:
            members.extend(
                self._roster.household_members(
                    household_id,
                    on=self._model.service_day,
                )
            )
        unique_members = {
            member.person_id: member
            for member in members
        }
        for member in unique_members.values():
            meals = " · ".join(
                meal.value
                for meal in self._roster.meals_for(
                    member.person_id,
                    on=self._model.service_day,
                )
            )
            self._household_list.addItem(f"{member.profile.name}   {meals or '미가입'}")
        if not members:
            self._household_list.addItem("연결된 가구가 없습니다")

    def _request_selected_person_edit(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return
        person = self._model.person_at(rows[0].row())
        self.person_edit_requested.emit(person.person_id)

    def _clear_inspector(self) -> None:
        self._edit_person_button.setEnabled(False)
        self._inspector_name.setText("명단에서 한 사람을 선택하세요")
        self._inspector_status.setText("—")
        self._inspector_status.setStyleSheet(f"background: {MUTED}; color: white;")
        self._inspector_detail.setText("가구와 배송 배정이 여기에 표시됩니다.")
        self._household_list.clear()


def _date_button(text: str, tooltip: str) -> QToolButton:
    button = QToolButton()
    button.setObjectName("dateStep")
    button.setText(text)
    button.setToolTip(tooltip)
    return button


def _section_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("sectionLabel")
    return label


def _date_text(value: date | None, open_text: str) -> str:
    return open_text if value is None else value.isoformat()
