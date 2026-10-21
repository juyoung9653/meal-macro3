"""Date-spine roster workspace for the SQLite v2 desktop application."""

from __future__ import annotations

import json
from hashlib import sha256
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta, timezone
from uuid import UUID, uuid4

from PySide6.QtCore import (
    QAbstractTableModel,
    QDate,
    QModelIndex,
    QItemSelectionModel,
    QSettings,
    QSize,
    Qt,
    Signal,
    QVariantAnimation,
    QEasingCurve,
)
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QSizePolicy,
    QTabBar,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.roster import (
    CustomFieldDefinition,
    CustomFieldType,
    Meal,
    Person,
    RosterSnapshot,
    additional_mobile_phone_field_ordinal,
    additional_telephone_field_ordinal,
)
from src.domain.delivery_routing import RoutingState
from src.domain.school_calendar import (
    SchoolLunchCalendar,
    SchoolLunchScheduleMissingError,
)
from src.domain.service_adjustments import (
    ServiceAdjustmentSchedule,
    ServiceTreatment,
)
from src.domain.status import MealStatus, StatusHistory
from src.services.inactive_roster import inactive_person_ids
from src.services.meal_service_availability import default_service_weekdays, regular_service_available
from src.version import APP_VERSION

from .column_layout import RosterColumnLayout
from .column_search import (
    ALL_SEQUENCES_EMPTY,
    ColumnSearchHeader,
    ColumnValuePicker,
    SequenceValuePicker,
    sequence_exclusive_meal,
    sequence_pair_token,
    sequence_pair_value,
    sequence_present_meal,
)
from .current_sheet_export import CurrentSheetSnapshot
from .date_edit import WheelSafeDateEdit
from .inactive_roster import InactiveRosterSettings, InactiveThresholdDialog
from .inspector_history import MealHistoryRow
from .meal_history import meal_status_summary
from .panel_handle import PanelEdgeHandle
from .more_panel import MoreActionsPanel
from .dialog_workflow import DialogRequest, dialog_workflow, invoke, launch
from .roster_cell_edit import RosterCellEditDelegate, is_roster_cell_editable
from .stopped_highlight import (
    StoppedHighlightDialog,
    StoppedHighlightPreference,
    load_preference,
    save_preference,
)

from .theme import (
    DELIVERY,
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
    "-": NOT_ENROLLED,
}
_MEAL_ORDER = (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH)


def _inspector_household_members(
    roster: RosterSnapshot, person_id: UUID, on: date,
) -> tuple[Person, ...]:
    """Show initial household context before its saved start, without backdating data."""
    first = {}
    households = {}
    for membership in roster.memberships:
        previous = first.get(membership.person_id)
        if previous is None or (membership.period.starts_on or date.min) < (previous.period.starts_on or date.min):
            first[membership.person_id] = membership
        if membership.period.contains(on):
            households[membership.person_id] = membership.household_id
    for member_id, membership in first.items():
        start = membership.period.starts_on
        if member_id not in households and start is not None and on < start:
            households[member_id] = membership.household_id
    household_id = households.get(person_id)
    if household_id is None:
        return ()
    return tuple(person for person in roster.persons if households.get(person.person_id) == household_id)


@dataclass(frozen=True, slots=True)
class _Column:
    key: str
    label: str
    width: int
    custom_field: CustomFieldDefinition | None = None
    meal: Meal | None = None


@dataclass(frozen=True, slots=True)
class _PersonContext:
    """Values derived from a roster for the model's selected day and meal."""

    meals: tuple[Meal, ...]
    status: str
    meal_statuses: Mapping[Meal, str]
    stopped_starts: Mapping[Meal, date | None]
    meal_drivers: Mapping[Meal, str]
    meal_driver_ids: Mapping[Meal, UUID | None]
    jurisdiction: str | None
    road_address: str | None
    detail_address: str | None
    sequence: str
    sequence_pairs: tuple[tuple[str, str], ...]
    legacy_sequence: str
    all_sequences_empty: bool


def _additional_mobile_phone_definitions(
    definitions: tuple[CustomFieldDefinition, ...],
) -> tuple[CustomFieldDefinition, ...]:
    return _additional_phone_definitions(
        definitions,
        additional_mobile_phone_field_ordinal,
    )


def _additional_telephone_definitions(
    definitions: tuple[CustomFieldDefinition, ...],
) -> tuple[CustomFieldDefinition, ...]:
    return _additional_phone_definitions(
        definitions,
        additional_telephone_field_ordinal,
    )


def _additional_phone_definitions(
    definitions: tuple[CustomFieldDefinition, ...],
    field_ordinal: Callable[[str], int | None],
) -> tuple[CustomFieldDefinition, ...]:
    return tuple(
        sorted(
            (
                definition
                for definition in definitions
                if definition.field_type is CustomFieldType.TEXT
                and field_ordinal(definition.name) is not None
            ),
            key=lambda definition: field_ordinal(definition.name) or 0,
        )
    )


# These standard import fields were previously saved without roster columns.
_OPTIONAL_PROFILE_COLUMNS = (
    _Column("school_type", "학교 유형", 100),
    _Column("household_head_name", "세대주 성명", 110),
    _Column("relationship_to_child", "아동과의 관계", 110),
)


class RosterTableModel(QAbstractTableModel):
    """Current roster projection for one service date and meal."""

    PersonIdRole = Qt.ItemDataRole.UserRole + 1
    StatusRole = Qt.ItemDataRole.UserRole + 2

    def __init__(
        self,
        roster: RosterSnapshot,
        histories: Mapping[tuple[UUID, Meal], StatusHistory] | None = None,
        parent: QWidget | None = None,
        *,
        routing_state: RoutingState | None = None,
        school_calendar: SchoolLunchCalendar | None = None,
        column_aliases: Mapping[str, str] | None = None,
        imported_profile_fields: frozenset[str] = frozenset(),
        stopped_highlight: StoppedHighlightPreference | None = None,
    ) -> None:
        super().__init__(parent)
        if not isinstance(roster, RosterSnapshot):
            raise TypeError("roster는 RosterSnapshot이어야 합니다.")
        self._roster = roster
        self._histories = dict(histories or {})
        self._stopped_highlight = stopped_highlight or StoppedHighlightPreference()
        self._routing_state = RoutingState() if routing_state is None else routing_state
        self._school_calendar = (
            SchoolLunchCalendar() if school_calendar is None else school_calendar
        )
        self._column_aliases = dict(column_aliases or {})
        self._imported_profile_fields = frozenset(imported_profile_fields)
        self._household_shared_columns: frozenset[str] = frozenset()
        self._household_shared_exceptions: frozenset[tuple[UUID, str]] = frozenset()
        self._shared_column_icon = None
        self._day = date.today()
        self._meal = Meal.LUNCH
        self._search = ""
        self._include_hidden_in_search = False
        self._column_searches: dict[str, str] = {}
        self._column_values: dict[str, frozenset[str]] = {}
        self._exception_only_columns: set[str] = set()
        self._active_only = False
        self._driver_filter: UUID | None = None
        self._birth_month: int | None = None
        self._special_only = False
        # None preserves the historical all-person model for standalone users.
        self._archive_mode: bool | None = None
        self._archive_ids: frozenset[UUID] = frozenset()
        self._columns = self._build_columns()
        # Keep the model's established sequence ordering for non-visual consumers.
        # The window clears this default before connecting its header clicks.
        self._sort_criteria: tuple[tuple[str, Qt.SortOrder], ...] = (
            ("sequence", Qt.SortOrder.AscendingOrder),
        )
        self._people: tuple[Person, ...] = ()
        self._contexts: dict[UUID, _PersonContext] = {}
        self._rebuild_context_projection()
        self._refresh_people()

    @property
    def columns(self) -> tuple[_Column, ...]:
        return self._columns

    @property
    def sort_criteria(self) -> tuple[tuple[str, Qt.SortOrder], ...]:
        """Active column keys, highest priority first."""
        return self._sort_criteria

    @property
    def _sort_column(self) -> int | None:
        return next(
            (index for index, column in enumerate(self._columns)
             if self._sort_criteria and column.key == self._sort_criteria[0][0]),
            None,
        )

    @property
    def _sort_order(self) -> Qt.SortOrder:
        return self._sort_criteria[0][1] if self._sort_criteria else Qt.SortOrder.AscendingOrder

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
        self._rebuild_context_projection()
        self._refresh_people()
        self.endResetModel()

    def set_stopped_highlight(self, preference: StoppedHighlightPreference) -> None:
        if preference == self._stopped_highlight:
            return
        self._stopped_highlight = preference
        if self.rowCount() == 0:
            return
        for column_index, column in enumerate(self._columns):
            if column.meal is not None:
                self.dataChanged.emit(
                    self.index(0, column_index),
                    self.index(self.rowCount() - 1, column_index),
                    [Qt.ItemDataRole.ForegroundRole],
                )

    def set_routing_state(self, state: RoutingState) -> None:
        if not isinstance(state, RoutingState):
            raise TypeError("state는 RoutingState여야 합니다.")
        self.beginResetModel()
        self._routing_state = state
        self._rebuild_context_projection()
        self._refresh_people()
        self.endResetModel()

    def set_school_calendar(self, calendar: SchoolLunchCalendar) -> None:
        if not isinstance(calendar, SchoolLunchCalendar):
            raise TypeError("calendar는 SchoolLunchCalendar여야 합니다.")
        self.beginResetModel()
        self._school_calendar = calendar
        self._rebuild_context_projection()
        self._refresh_people()
        self.endResetModel()

    def set_driver_filter(self, driver_id: UUID | None) -> None:
        if driver_id is not None and not isinstance(driver_id, UUID):
            raise TypeError("driver_id는 UUID 또는 None이어야 합니다.")
        if driver_id == self._driver_filter:
            return
        self.beginResetModel()
        self._driver_filter = driver_id
        self._refresh_people()
        self.endResetModel()

    def set_birth_month(self, month: int | None) -> None:
        """Filter by birth month; zero means the current calendar month."""
        if month is not None and (type(month) is not int or not 0 <= month <= 12):
            raise ValueError("생일 월은 전체(None), 이번 달(0), 1~12월이어야 합니다.")
        if month == self._birth_month:
            return
        self.beginResetModel()
        self._birth_month = month
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

    def set_include_hidden_in_search(self, enabled: bool) -> None:
        if enabled == self._include_hidden_in_search:
            return
        self.beginResetModel()
        self._include_hidden_in_search = enabled
        self._refresh_people()
        self.endResetModel()

    def set_column_search(self, key: str, text: str) -> None:
        """Combine literal, case-insensitive column searches with existing filters."""
        if key not in {column.key for column in self._columns}:
            raise ValueError(f"Unknown column: {key}")
        normalized = " ".join(text.split()).casefold()
        if self._column_searches.get(key, "") == normalized:
            return
        self.beginResetModel()
        if normalized:
            self._column_searches[key] = normalized
        else:
            self._column_searches.pop(key, None)
        self._refresh_people()
        self.endResetModel()

    def column_filter_values(self, key: str) -> tuple[str, ...]:
        """Offer all current values, including values hidden by active filters."""
        column = next(column for column in self._columns if column.key == key)
        if key == "sequence":
            return tuple(sorted({
                sequence_pair_token(meal.value, "") for meal in Meal
            } | {
                sequence_pair_token(*pair)
                for person in self._roster.persons if self._matches_archive_filter(person)
                for pair in self._contexts[person.person_id].sequence_pairs
            }))
        values = {
            str(self._display_value(person, column, self._contexts[person.person_id]))
            for person in self._roster.persons
            if self._matches_archive_filter(person)
        }
        return tuple(sorted(values, key=str.casefold))

    def selected_column_values(self, key: str) -> frozenset[str] | None:
        return self._column_values.get(key)

    def selected_column_search(self, key: str) -> str:
        return self._column_searches.get(key, "")

    def set_column_values(self, key: str, values: frozenset[str] | None) -> None:
        if key not in {column.key for column in self._columns}:
            raise ValueError(f"Unknown column: {key}")
        selected = None if values is None else frozenset(values)
        if self._column_values.get(key) == selected:
            return
        self.beginResetModel()
        if selected is None:
            self._column_values.pop(key, None)
        else:
            self._column_values[key] = selected
        self._refresh_people()
        self.endResetModel()

    def exception_filter_state(self, key: str) -> bool | None:
        if key not in self._household_shared_columns:
            return None
        return key in self._exception_only_columns

    def set_exception_filter(self, key: str, enabled: bool) -> None:
        if key not in {column.key for column in self._columns}:
            raise ValueError(f"Unknown column: {key}")
        enabled = bool(enabled) and key in self._household_shared_columns
        if (key in self._exception_only_columns) == enabled:
            return
        self.beginResetModel()
        if enabled:
            self._exception_only_columns.add(key)
        else:
            self._exception_only_columns.discard(key)
        self._refresh_people()
        self.endResetModel()

    def clear_column_filters(self) -> None:
        if not self._column_searches and not self._column_values and not self._exception_only_columns:
            return
        self.beginResetModel()
        self._column_searches.clear()
        self._column_values.clear()
        self._exception_only_columns.clear()
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

    def set_archive_filter(self, person_ids: frozenset[UUID], visible: bool) -> None:
        """Show one archive category without changing other filter or sort state."""
        ids = frozenset(person_ids)
        if ids == self._archive_ids and visible == self._archive_mode:
            return
        self.beginResetModel()
        self._archive_ids = ids
        self._archive_mode = visible
        self._refresh_people()
        self.endResetModel()

    def set_special_only(self, enabled: bool) -> None:
        if enabled == self._special_only:
            return
        self.beginResetModel()
        self._special_only = enabled
        self._columns = self._build_columns()
        if not enabled:
            self._column_searches.pop("delivery_settings", None)
            self._column_values.pop("delivery_settings", None)
            self._exception_only_columns.discard("delivery_settings")
        self._prune_sort_criteria()
        self._refresh_people()
        self.endResetModel()

    def replace_roster(
        self,
        roster: RosterSnapshot,
        histories: Mapping[tuple[UUID, Meal], StatusHistory] | None = None,
        *,
        imported_profile_fields: frozenset[str] | None = None,
    ) -> None:
        if not isinstance(roster, RosterSnapshot):
            raise TypeError("roster는 RosterSnapshot이어야 합니다.")
        self.beginResetModel()
        self._roster = roster
        self._histories = dict(histories or {})
        if imported_profile_fields is not None:
            self._imported_profile_fields = frozenset(imported_profile_fields)
        self._columns = self._build_columns()
        keys = {column.key for column in self._columns}
        self._exception_only_columns.intersection_update(keys)
        self._column_searches = {
            key: value for key, value in self._column_searches.items() if key in keys
        }
        self._column_values = {
            key: value for key, value in self._column_values.items() if key in keys
        }
        self._prune_sort_criteria()
        self._rebuild_context_projection()
        self._refresh_people()
        self.endResetModel()

    def sort(
        self,
        column: int,
        order: Qt.SortOrder = Qt.SortOrder.AscendingOrder,
    ) -> None:
        """Retain Qt's single-column API; -1 restores source roster order."""
        if column != -1 and not 0 <= column < len(self._columns):
            return
        self.beginResetModel()
        self._sort_criteria = () if column == -1 else ((self._columns[column].key, order),)
        self._refresh_people()
        self.endResetModel()

    def cycle_sort(self, column: int) -> None:
        """Cycle one column independently without changing other priorities."""
        if not 0 <= column < len(self._columns):
            return
        key = self._columns[column].key
        criteria = list(self._sort_criteria)
        for index, (active_key, order) in enumerate(criteria):
            if active_key == key:
                if order == Qt.SortOrder.AscendingOrder:
                    criteria[index] = (key, Qt.SortOrder.DescendingOrder)
                else:
                    criteria.pop(index)
                break
        else:
            criteria.append((key, Qt.SortOrder.AscendingOrder))
        self.beginResetModel()
        self._sort_criteria = tuple(criteria)
        self._refresh_people()
        self.endResetModel()

    def _prune_sort_criteria(self) -> None:
        keys = {column.key for column in self._columns}
        self._sort_criteria = tuple(item for item in self._sort_criteria if item[0] in keys)

    def person_at(self, row: int) -> Person:
        return self._people[row]

    def status_for(self, person: Person) -> str:
        return self._contexts[person.person_id].status

    def person_delivery_view(self, person: Person) -> _PersonContext:
        return self._contexts[person.person_id]

    def status_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in (*_STATUS_COLORS,)}
        for person in self._people:
            counts[self._contexts[person.person_id].status] += 1
        return counts

    def meal_status_counts(self) -> dict[Meal, dict[str, int]]:
        """Count each service status for every meal in the visible roster.

        A dash means that the person is not a target for that meal, so it is
        deliberately omitted from the operational counts.
        """
        statuses = (
            MealStatus.DELIVERY.value,
            MealStatus.PAUSED.value,
            MealStatus.STOPPED.value,
        )
        counts = {meal: {status: 0 for status in statuses} for meal in _MEAL_ORDER}
        for person in self._people:
            meal_statuses = self._contexts[person.person_id].meal_statuses
            for meal in _MEAL_ORDER:
                status = meal_statuses[meal]
                if status in counts[meal]:
                    counts[meal][status] += 1
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
        context = self._contexts[person.person_id]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_value(person, column, context)
        if role == self.PersonIdRole:
            return person.person_id
        if role == self.StatusRole:
            return self._status_for_column(column, context)
        if role == Qt.ItemDataRole.ForegroundRole and column.meal is not None:
            status = self._status_for_column(column, context)
            if status == MealStatus.STOPPED.value:
                start = context.stopped_starts[column.meal]
                if not self._stopped_highlight.is_red(start, self._day):
                    return QColor(NOT_ENROLLED)
            return QColor(_STATUS_COLORS[status])
        if role == Qt.ItemDataRole.FontRole and (column.meal is not None or column.key == "name"):
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if (
                column.meal is not None
                or column.key in {"birth_date", "grade", "sequence"}
                or (
                    column.custom_field is not None
                    and column.custom_field.field_type in {
                        CustomFieldType.NUMBER, CustomFieldType.DATE,
                    }
                )
            ):
                return Qt.AlignmentFlag.AlignCenter
        if role == Qt.ItemDataRole.ToolTipRole:
            value = self._display_value(person, column, context)
            if column.key == "sequence":
                return f"{value or '순서 미지정'}\n더블클릭 → 순서 수정"
            if column.key in {"address", "detail_address", "jurisdiction"}:
                return f"{value or '미등록'}\n더블클릭하여 수정 · 우클릭 → 이사 등록"
            if column.key == "delivery_settings":
                return f"{value or '기본값 사용'}\n더블클릭 → 배송 설정"
            if is_roster_cell_editable(column.key):
                return f"{value}\n더블클릭하여 수정" if value else "더블클릭하여 수정"
            return None if not value else str(value)
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if index.isValid() and is_roster_cell_editable(self._columns[index.column()].key):
            return flags | Qt.ItemFlag.ItemIsEditable
        return flags & ~Qt.ItemFlag.ItemIsEditable

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self._columns):
            key = self._columns[section].key
            shared = key in self._household_shared_columns
            if role == Qt.ItemDataRole.ToolTipRole and shared:
                return self._columns[section].label + " · 가구 공용: 같은 가구의 수정값을 함께 적용합니다."
            if role == Qt.ItemDataRole.DecorationRole and shared:
                if self._shared_column_icon is None:
                    pixmap = QPixmap(20, 20)
                    pixmap.fill(Qt.GlobalColor.transparent)
                    painter = QPainter(pixmap)
                    painter.setPen(QColor("#16788A"))
                    painter.setFont(QFont("맑은 고딕", 9, QFont.Weight.Bold))
                    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "공")
                    painter.end()
                    self._shared_column_icon = QIcon(pixmap)
                return self._shared_column_icon
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self._columns[section].label
        return section + 1

    def _build_columns(self) -> tuple[_Column, ...]:
        optional_columns = tuple(
            column for column in _OPTIONAL_PROFILE_COLUMNS
            if column.key in self._imported_profile_fields
            or any(getattr(person.profile, column.key) is not None for person in self._roster.persons)
        )
        additional_telephone_definitions = _additional_telephone_definitions(
            self._roster.custom_fields
        )
        additional_mobile_definitions = _additional_mobile_phone_definitions(
            self._roster.custom_fields
        )
        base_before_contacts = (
            _Column("name", "성명", 92),
            _Column("birth_date", "생년월일", 96),
            _Column("status:dinner", "석식", 112, meal=Meal.DINNER),
            _Column("status:breakfast", "조식", 112, meal=Meal.BREAKFAST),
            _Column("status:lunch", "중식", 112, meal=Meal.LUNCH),
            *(column for column in optional_columns if column.key == "school_type"),
            _Column("school", "학교", 120),
            _Column("grade", "학년", 82),
        )
        contact_columns = (
            _Column(
                "telephone",
                "전화번호1" if additional_telephone_definitions else "전화번호",
                118,
            ),
            *(
                _Column(
                    f"custom:{definition.definition_id}",
                    definition.name,
                    118,
                    definition,
                )
                for definition in additional_telephone_definitions
            ),
            _Column(
                "mobile",
                "핸드폰1" if additional_mobile_definitions else "핸드폰",
                118,
            ),
            *(
                _Column(
                    f"custom:{definition.definition_id}",
                    definition.name,
                    118,
                    definition,
                )
                for definition in additional_mobile_definitions
            ),
        )
        base_after_contacts = (
            *(column for column in optional_columns if column.key != "school_type"),
            _Column("jurisdiction", "관할동", 82),
            _Column("address", "도로명주소", 220),
            _Column("detail_address", "상세주소", 150),
            _Column("sequence", "순서", 104),
            _Column("note", "비고", 200),
        )
        additional_contact_ids = {
            definition.definition_id
            for definition in (
                *additional_telephone_definitions,
                *additional_mobile_definitions,
            )
        }
        custom = tuple(
            _Column(
                f"custom:{definition.definition_id}",
                definition.name,
                105,
                definition,
            )
            for definition in sorted(
                (
                    definition
                    for definition in self._roster.custom_fields
                    if definition.definition_id not in additional_contact_ids
                ),
                key=lambda item: item.display_order,
            )
        )
        columns = (
            *base_before_contacts[:1],
            *((_Column("delivery_settings", "배송 설정", 240),) if self._special_only else ()),
            *base_before_contacts[1:],
            *contact_columns,
            *base_after_contacts,
            *custom,
        )
        return tuple(
            replace(column, label=self._column_aliases.get(column.key, column.label))
            if column.custom_field is None else column
            for column in columns
        )

    def set_column_alias(self, key: str, label: str) -> bool:
        """Change one built-in display label without changing its stable data key."""
        column_index = next(
            (index for index, column in enumerate(self._columns) if column.key == key),
            None,
        )
        if column_index is None:
            raise ValueError("현재 명단에 없는 칼럼입니다.")
        if self._columns[column_index].custom_field is not None:
            raise ValueError("사용자 항목 이름은 사용자 항목 관리에서 변경해 주세요.")
        normalized = label.strip()
        if not normalized:
            raise ValueError("칼럼 이름을 입력해 주세요.")
        if len(normalized) > 40:
            raise ValueError("칼럼 이름은 40자 이하로 입력해 주세요.")
        if self._columns[column_index].label == normalized:
            return False
        self._column_aliases[key] = normalized
        self._columns = tuple(
            replace(column, label=normalized) if column.key == key else column
            for column in self._columns
        )
        self.headerDataChanged.emit(
            Qt.Orientation.Horizontal, column_index, column_index
        )
        return True

    def _matches_archive_filter(self, person: Person) -> bool:
        return (
            self._archive_mode is None
            or (self._archive_mode is False and self._include_hidden_in_search)
            or (person.person_id in self._archive_ids) == self._archive_mode
        )

    def _refresh_people(self) -> None:
        people: list[Person] = []
        filters = [(column, self._column_searches[column.key])
                   for column in self._columns if column.key in self._column_searches]
        value_filters = [
            (
                column,
                self._column_values[column.key],
                self._compile_sequence_values(self._column_values[column.key])
                if column.key == "sequence" else None,
            )
            for column in self._columns if column.key in self._column_values
        ]
        birth_month = date.today().month if self._birth_month == 0 else self._birth_month
        for person in self._roster.persons:
            if any((person.person_id, key) not in self._household_shared_exceptions
                   for key in self._exception_only_columns):
                continue
            if self._special_only and not self._delivery_setting_text(person):
                continue
            if not self._matches_archive_filter(person):
                continue
            if birth_month is not None and (
                person.profile.birth_date is None
                or person.profile.birth_date.month != birth_month
            ):
                continue
            context = self._contexts[person.person_id]
            if self._active_only and self._meal not in context.meals:
                continue
            if (
                self._driver_filter is not None
                and self._driver_filter not in context.meal_driver_ids.values()
            ):
                continue
            if self._search and self._search not in self._search_text(person):
                continue
            if any(
                query not in " ".join(
                    str(self._display_value(person, column, context)).split()
                ).casefold()
                for column, query in filters
            ):
                continue
            if any(
                not self._matches_column_values(person, column, context, selected, compiled)
                for column, selected, compiled in value_filters
            ):
                continue
            people.append(person)
        columns = {column.key: column for column in self._columns}
        # Stable passes preserve secondary criteria and original roster order on ties.
        for key, order in reversed(self._sort_criteria):
            people.sort(
                key=lambda person: self._sort_value(person, columns[key]),
                reverse=order == Qt.SortOrder.DescendingOrder,
            )
        self._people = tuple(people)

    def _matches_column_values(self, person, column, context, selected, compiled=None):
        if column.key == "sequence":
            return self._matches_sequence_values(context, selected, compiled)
        return str(self._display_value(person, column, context)) in selected

    @staticmethod
    def _compile_sequence_values(selected):
        meal_labels = {meal.value for meal in Meal}
        allowed_by_meal = {}
        present_meals = set()
        exclusive_meals = set()
        special_by_label = {}
        legacy_values = set()
        structured = False
        require_all_empty = False
        for token in selected:
            if pair := sequence_pair_value(token):
                structured = True
                if pair[0] in meal_labels:
                    allowed_by_meal.setdefault(pair[0], set()).add(pair[1])
                else:
                    special_by_label.setdefault(pair[0], set()).add(pair[1])
            elif meal := sequence_present_meal(token):
                structured = True
                present_meals.add(meal)
            elif meal := sequence_exclusive_meal(token):
                structured = True
                present_meals.add(meal)
                exclusive_meals.add(meal)
            elif token == ALL_SEQUENCES_EMPTY:
                structured = True
                require_all_empty = True
            else:
                legacy_values.add(token)
        return (
            {label: frozenset(values) for label, values in allowed_by_meal.items()},
            frozenset(present_meals),
            frozenset(exclusive_meals),
            {label: frozenset(values) for label, values in special_by_label.items()},
            frozenset(legacy_values),
            structured,
            require_all_empty,
        )

    @staticmethod
    def _matches_sequence_values(context, selected, compiled=None):
        if not selected:
            return context.all_sequences_empty

        meal_labels = {meal.value for meal in Meal}
        values_by_meal = {label: "" for label in meal_labels}
        context_pairs = set(context.sequence_pairs)
        unmapped_nonempty = set()
        for label, value in context.sequence_pairs:
            if label in meal_labels:
                values_by_meal[label] = value
            elif value.strip():
                unmapped_nonempty.add((label, value))

        if compiled is None:
            compiled = RosterTableModel._compile_sequence_values(selected)
        (
            allowed_by_meal,
            present_meals,
            exclusive_meals,
            special_by_label,
            legacy_values,
            structured,
            require_all_empty,
        ) = compiled

        if legacy_values and context.legacy_sequence not in legacy_values:
            return False
        if not structured:
            return bool(legacy_values)

        active_meals = set(allowed_by_meal) | present_meals
        for label, value in values_by_meal.items():
            if label in allowed_by_meal:
                if value not in allowed_by_meal[label]:
                    return False
            elif label not in active_meals and value.strip():
                return False
        if any(not values_by_meal[label].strip() for label in present_meals):
            return False
        if any(
            values_by_meal[label].strip()
            for exclusive in exclusive_meals
            for label in meal_labels
            if label != exclusive
        ):
            return False
        if require_all_empty and not context.all_sequences_empty:
            return False
        if any(
            not any((label, value) in context_pairs for value in allowed)
            for label, allowed in special_by_label.items()
        ):
            return False
        allowed_special = {
            (label, value)
            for label, allowed in special_by_label.items()
            for value in allowed
        }
        if unmapped_nonempty and not unmapped_nonempty <= allowed_special:
            return False
        return not (
            not any(value.strip() for value in values_by_meal.values())
            and not context.all_sequences_empty
            and not (unmapped_nonempty & allowed_special)
        )

    def _rebuild_context_projection(self) -> None:
        """Resolve day-and-meal lookups once, outside the table paint path."""

        contexts: dict[UUID, _PersonContext] = {}
        explicit_meals = {}
        common_sequences = {}
        people_with_sequence = {item.person_id for item in self._roster.assignments if item.sequence}
        for assignment in self._roster.assignments:
            if assignment.meal is not None:
                explicit_meals.setdefault(assignment.person_id, set()).add(assignment.meal)
            else:
                common_sequences[assignment.person_id] = assignment.sequence
        for person in self._roster.persons:
            person_id = person.person_id
            meals = self._roster.meals_for(person_id, on=self._day)
            meal_statuses = {
                meal: self._resolve_status(person_id, meal, meals)
                for meal in Meal
            }
            stopped_starts = {}
            for meal, status in meal_statuses.items():
                start = None
                if status == MealStatus.STOPPED.value:
                    history = self._histories.get((person_id, meal))
                    if history is not None:
                        start = next(
                            (interval.period.starts_on for interval in history.intervals
                             if interval.status is MealStatus.STOPPED
                             and interval.period.contains(self._day)),
                            None,
                        )
                stopped_starts[meal] = start
            delivery_view = self._routing_state.resolve(self._roster, person_id, self._day)
            sequences = {
                meal: self._roster.resolve_assignment(person_id, meal).sequence or ""
                for meal in _MEAL_ORDER
            }
            nonempty_sequences = {
                sequence for sequence in sequences.values() if sequence
            }
            if len(nonempty_sequences) <= 1:
                legacy_sequence = next(iter(nonempty_sequences), "")
            else:
                legacy_sequence = "\n".join(
                    f"{meal.value}: {sequences[meal]}"
                    for meal in _MEAL_ORDER
                    if sequences[meal]
                )
            relevant = set(meals) | explicit_meals.get(person_id, set())
            pairs = tuple((meal.value, sequences[meal]) for meal in _MEAL_ORDER
                          if meal in relevant)
            if not pairs:
                common = common_sequences.get(person_id)
                pairs = (("공통/식종 미지정", common),) if common else (("(빈 값)", ""),)
            sequence = "\n".join(f"{label}: {value}" for label, value in pairs if value)
            contexts[person_id] = _PersonContext(
                meals=meals,
                status=meal_statuses[self._meal],
                meal_statuses=meal_statuses,
                stopped_starts=stopped_starts,
                meal_drivers=delivery_view.drivers,
                meal_driver_ids=delivery_view.driver_ids,
                jurisdiction=delivery_view.jurisdiction,
                road_address=delivery_view.road_address,
                detail_address=delivery_view.detail_address,
                sequence=sequence,
                sequence_pairs=pairs,
                legacy_sequence=legacy_sequence,
                all_sequences_empty=person_id not in people_with_sequence,
            )
        self._contexts = contexts

    def _sort_value(self, person: Person, column: _Column) -> tuple[object, ...]:
        context = self._contexts[person.person_id]
        if column.meal is not None:
            status = self._status_for_column(column, context)
            driver = context.meal_drivers[column.meal].casefold()
            return (status.casefold(), driver)
        value = self._display_value(
            person,
            column,
            context,
        )
        text = "" if value is None else str(value).strip()
        if column.key == "sequence":
            raw = {value for _, value in context.sequence_pairs if value}
            text = next(iter(raw)) if len(raw) == 1 else (
                self._roster.resolve_assignment(person.person_id, self._meal).sequence or ""
            )
        if not text:
            normalized: tuple[int, object] = (2, "")
        elif text.isdigit():
            normalized = (0, int(text))
        else:
            normalized = (1, text.casefold())
        return normalized

    def _resolve_status(
        self,
        person_id: UUID,
        meal: Meal,
        meals: tuple[Meal, ...],
    ) -> str:
        status = self._effective_status_on(person_id, meal, self._day, meals)
        return "-" if status is None else status.value

    def effective_status_on(self, person_id: UUID, meal: Meal, day: date) -> MealStatus | None:
        """Return the status shown for a meal on an arbitrary roster day."""
        return self._effective_status_on(
            person_id, meal, day, self._roster.meals_for(person_id, on=day)
        )

    def _effective_status_on(
        self,
        person_id: UUID,
        meal: Meal,
        day: date,
        meals: tuple[Meal, ...],
    ) -> MealStatus | None:
        if meal not in meals:
            return None
        if meal is Meal.LUNCH:
            try:
                if not self._school_calendar.vendor_service_available(
                    self._roster.person(person_id).profile,
                    day,
                ):
                    return None
            except SchoolLunchScheduleMissingError:
                return None
        history = self._histories.get((person_id, meal))
        return (
            MealStatus.DELIVERY
            if history is None
            else history.status_on(day)
        )

    def scheduled_status_on(
        self, person_id: UUID, meal: Meal, day: date
    ) -> MealStatus | None:
        """Return status only when the meal is actually provided on ``day``."""
        status = self.effective_status_on(person_id, meal, day)
        if status is None:
            return None
        try:
            if not regular_service_available(
                self._roster,
                self._roster.person(person_id),
                meal,
                day,
                self._school_calendar,
            ):
                return None
        except SchoolLunchScheduleMissingError:
            return None
        return status

    @staticmethod
    def _status_for_column(column: _Column, context: _PersonContext) -> str:
        return context.status if column.meal is None else context.meal_statuses[column.meal]

    def _search_text(self, person: Person) -> str:
        profile = person.profile
        context = self._contexts[person.person_id]
        return " ".join(
            str(value).casefold()
            for value in (
                profile.name,
                profile.school_name or "",
                *(getattr(profile, column.key) or "" for column in _OPTIONAL_PROFILE_COLUMNS),
                profile.telephone or "",
                profile.mobile_phone or "",
                context.road_address or "",
                context.detail_address or "",
                context.jurisdiction or "",
                profile.note or "",
                *(
                    value
                    for value in profile.custom_values.values()
                    if value is not None
                ),
            )
        )

    def _display_value(
        self,
        person: Person,
        column: _Column,
        context: _PersonContext,
    ):
        profile = person.profile
        if column.key == "delivery_settings":
            return self._delivery_setting_text(person)
        if column.meal is not None:
            status = self._status_for_column(column, context)
            display_status = "-" if status == MealStatus.STOPPED.value else status
            return f"{context.meal_drivers[column.meal] or '미지정'}\n{display_status}"
        if column.key == "name":
            return profile.name
        if column.key == "birth_date":
            return "" if profile.birth_date is None else profile.birth_date.isoformat()
        if column.key in {item.key for item in _OPTIONAL_PROFILE_COLUMNS}:
            return getattr(profile, column.key) or ""
        if column.key == "school":
            return profile.school_name or ""
        if column.key == "grade":
            return profile.grade or ""
        if column.key == "telephone":
            return profile.telephone or ""
        if column.key == "mobile":
            return profile.mobile_phone or ""
        if column.key == "jurisdiction":
            return context.jurisdiction or ""
        if column.key == "address":
            return context.road_address or ""
        if column.key == "detail_address":
            return context.detail_address or ""
        if column.key == "sequence":
            return context.sequence
        if column.key == "note":
            return profile.note or ""
        if column.custom_field is not None:
            value = profile.custom_values.get(column.custom_field.definition_id)
            if isinstance(value, date):
                return value.isoformat()
            return "" if value is None else str(value)
        return ""

    def _delivery_setting_text(self, person: Person) -> str:
        settings = self._roster.delivery_settings_for(person.person_id, on=self._day)
        lines = []
        for setting in sorted(settings, key=lambda item: _MEAL_ORDER.index(item.meal)):
            parts = []
            if setting.weekdays is not None:
                parts.append("".join("월화수목금토일"[day] for day in setting.weekdays) + "요일")
            if setting.delivery_weekdays is not None:
                service_days = setting.weekdays
                if service_days is None:
                    service_days = default_service_weekdays(setting.meal, self._roster)
                grouped_days: dict[int, list[str]] = {}
                for day in service_days:
                    grouped_days.setdefault(setting.delivery_weekdays[day], []).append("월화수목금토일"[day])
                parts.extend(
                    f"{'·'.join(days)} → {'월화수목금토일'[delivery_day]} 배송"
                    for delivery_day, days in grouped_days.items()
                )
            if setting.form is not None:
                parts.append(setting.form)
            lines.append(f"{setting.meal.value}: {' · '.join(parts)}")
        return "\n".join(lines)


class _MealHistoryList(QListWidget):
    """Size wrapped inspector rows to the available viewport width."""

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.update_row_heights()

    def update_row_heights(self) -> None:
        width = max(80, self.viewport().width() - 16)
        for index in range(self.count()):
            item = self.item(index)
            entry = self.itemWidget(item)
            if entry is None:
                continue
            labels = entry.findChildren(QLabel)
            height = 4 + sum(label.heightForWidth(width) for label in labels) + max(0, len(labels) - 1)
            item.setSizeHint(QSize(0, max(54, height)))


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

        layout.addWidget(_section_label("날짜별 적용 상태"))
        effective = QTableView()
        effective.setModel(_SimpleRowsModel(
            ("상태", "시작일", "종료일", "관련 기록 수"),
            tuple(
                (
                    _status_display(interval.status),
                    _date_text(interval.period.starts_on, "지정 안 함"),
                    _date_text(interval.period.ends_on, "계속"),
                    str(len(interval.source_event_ids)),
                )
                for interval in history.intervals
            ),
            effective,
        ))
        effective.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(effective, 1)

        layout.addWidget(_section_label("등록된 변경 이력"))
        raw = QTableView()
        raw.setModel(_SimpleRowsModel(
            ("기록 시각", "등록한 상태", "시작일", "종료일", "변경 내용"),
            tuple(
                (
                    event.recorded_at.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M"),
                    _status_display(event.status),
                    _date_text(event.period.starts_on, "지정 안 함"),
                    _date_text(event.period.ends_on, "계속"),
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


class _RosterSheetTabBar(QTabBar):
    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(0, hint.height())


class RosterWindow(QMainWindow):
    """Operational shell; persistence is supplied by a future application controller."""

    status_action_requested = Signal(object, object, object)
    batch_status_action_requested = Signal(object, object)
    drivers_requested = Signal()
    driver_assignment_requested = Signal(object, object)
    batch_driver_assignment_requested = Signal(object)
    sequence_edit_requested = Signal(object)
    household_sequence_save_requested = Signal(object, str, object)
    driver_replacement_requested = Signal()
    relocation_requested = Signal(object)
    complaint_requested = Signal(object, object)
    database_export_requested = Signal()
    current_sheet_export_requested = Signal(object)
    driver_households_export_requested = Signal(object, object)
    meal_history_cancel_requested = Signal(object, object)
    revision_history_requested = Signal()
    complaint_history_requested = Signal()
    import_review_requested = Signal()
    person_edit_requested = Signal(object)
    cell_edit_requested = Signal(object, str, str)
    delivery_settings_requested = Signal(object)
    person_add_requested = Signal(object)
    person_delete_requested = Signal(object)
    service_adjustment_requested = Signal()
    school_calendar_requested = Signal()
    unit_price_requested = Signal()
    documents_requested = Signal()
    document_output_selected = Signal(object)
    document_generated = Signal(object)
    settings_requested = Signal()
    custom_fields_requested = Signal()
    household_shared_column_requested = Signal(str)
    household_shared_exception_requested = Signal(object, str, bool)
    column_input_rule_requested = Signal(str)
    monthly_statistics_requested = Signal()
    monthly_meal_history_requested = Signal()

    def __init__(
        self,
        roster: RosterSnapshot,
        histories: Mapping[tuple[UUID, Meal], StatusHistory] | None = None,
        parent: QWidget | None = None,
        *,
        revision_display_code: str | None = None,
        column_settings=None,
        view_settings_scope: str | None = None,
        inactive_settings: QSettings | None = None,
        routing_state: RoutingState | None = None,
        school_calendar: SchoolLunchCalendar | None = None,
        service_adjustment_schedule: ServiceAdjustmentSchedule | None = None,
        cell_edit_callback: Callable[[UUID, str, str], object] | None = None,
        imported_profile_fields: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(parent)
        self._column_settings = column_settings
        self._search_settings = (
            column_settings if column_settings is not None
            else QSettings("분당구", "급식 배송 운영 SQLite v2")
        )
        # Standalone windows without an injected store/scope are common in tests.
        # The real startup passes a stable database ID for per-file preferences.
        self._remember_view = view_settings_scope is not None or column_settings is not None
        self._search_settings_key = (
            self._SEARCH_SETTINGS_KEY
            if view_settings_scope is None
            else f"{self._SEARCH_SETTINGS_KEY}/{sha256(view_settings_scope.encode('utf-8')).hexdigest()}"
        )
        self._stopped_highlight_key = (
            self._search_settings_key.replace(self._SEARCH_SETTINGS_KEY, "roster/stopped_highlight_v1", 1)
            if self._remember_view else None
        )
        self._stopped_highlight = load_preference(
            self._search_settings if self._remember_view else None,
            self._stopped_highlight_key,
        )
        self._driver_household_search_settings_key = self._search_settings_key.replace(
            self._SEARCH_SETTINGS_KEY, "driver_households/search_v1", 1
        )
        self._sheet_settings_key = (
            "roster/sheets_v1" if view_settings_scope is None
            else f"roster/sheets_v1/{sha256(view_settings_scope.encode('utf-8')).hexdigest()}"
        )
        self._column_alias_settings_key = f"{self._sheet_settings_key}/column_aliases"
        self._column_aliases = self._load_column_aliases()
        self._sheets, self._active_sheet_id = self._load_sheets()
        self._restoring_search = False
        self._pending_exception_filters: set[str] = set()
        self._inactive_settings = inactive_settings
        self._archive_settings = InactiveRosterSettings(inactive_settings)
        self._roster = roster
        self._histories = dict(histories or {})
        self._routing_state = RoutingState() if routing_state is None else routing_state
        self._school_calendar = (
            SchoolLunchCalendar() if school_calendar is None else school_calendar
        )
        if (
            service_adjustment_schedule is not None
            and not isinstance(service_adjustment_schedule, ServiceAdjustmentSchedule)
        ):
            raise TypeError(
                "service_adjustment_schedule은 ServiceAdjustmentSchedule이어야 합니다."
            )
        self._service_adjustment_schedule = (
            ServiceAdjustmentSchedule()
            if service_adjustment_schedule is None
            else service_adjustment_schedule
        )
        self._revision_display_code = revision_display_code
        if cell_edit_callback is not None:
            self.cell_edit_requested.connect(cell_edit_callback)
        remembered_width = 240
        if self._remember_view:
            try:
                remembered_width = int(self._search_settings.value("roster/delivery_settings_width", 240))
            except (TypeError, ValueError):
                pass
        self._delivery_settings_width = remembered_width if remembered_width > 0 else 240
        self._model = RosterTableModel(
            roster,
            self._histories,
            self,
            routing_state=self._routing_state,
            school_calendar=self._school_calendar,
            column_aliases=self._column_aliases,
            imported_profile_fields=imported_profile_fields,
            stopped_highlight=self._stopped_highlight,
        )
        self._meal = Meal.LUNCH
        self._history_dialogs: list[QDialog] = []
        self._history_provider = None
        self._meal_change_history_provider: (
            Callable[[Person], tuple[MealHistoryRow | tuple[str, str], ...]] | None
        ) = None
        self._meal_history_presence_provider: Callable[[UUID], bool] | None = None
        self._meal_change_history_rows: dict[UUID, tuple] = {}
        register_application_fonts()
        self.setWindowTitle(f"급식 배송 운영 · {APP_VERSION}")
        self.resize(1440, 860)
        self.setMinimumSize(1080, 680)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()
        self._restore_search_filters()
        self._refresh_archive()
        self._set_day(date.today())
        self._set_meal(Meal.LUNCH)

    @property
    def table_model(self) -> RosterTableModel:
        return self._model

    @property
    def roster(self) -> RosterSnapshot:
        """Current immutable roster displayed by this window."""

        return self._roster

    @dialog_workflow
    def open_stopped_highlight_settings(self) -> None:
        dialog = StoppedHighlightDialog(self._stopped_highlight, parent=self)
        if (yield DialogRequest(dialog)) != QDialog.DialogCode.Accepted:
            return
        selected = dialog.selected_preference()
        if selected == self._stopped_highlight:
            return
        if self._stopped_highlight_key is not None:
            try:
                save_preference(self._search_settings, self._stopped_highlight_key, selected)
            except OSError as error:
                QMessageBox.warning(self, "중지 표시 기간을 저장하지 못했습니다", str(error))
                return
        self._stopped_highlight = selected
        self._model.set_stopped_highlight(selected)

    def current_sheet_snapshot(self) -> CurrentSheetSnapshot:
        """Freeze the active table exactly as displayed, on the UI thread."""

        header = self._table.horizontalHeader()
        logical_columns = tuple(
            logical
            for visual in range(header.count())
            if (logical := header.logicalIndex(visual)) >= 0
            and not self._table.isColumnHidden(logical)
        )
        if not logical_columns:
            raise ValueError("현재 시트에 표시된 칼럼이 없어 내보낼 수 없습니다.")
        headers = tuple(
            str(
                self._model.headerData(
                    logical,
                    Qt.Orientation.Horizontal,
                    Qt.ItemDataRole.DisplayRole,
                )
                or ""
            )
            for logical in logical_columns
        )
        rows = tuple(
            tuple(
                "" if (value := self._model.data(
                    self._model.index(row, logical), Qt.ItemDataRole.DisplayRole
                )) is None else str(value)
                for logical in logical_columns
            )
            for row in range(self._model.rowCount())
        )
        sheet_name = self._sheets[self._sheet_index(self._active_sheet_id)][1]
        return CurrentSheetSnapshot(sheet_name, headers, rows)

    def _request_current_sheet_export(self) -> None:
        try:
            snapshot = self.current_sheet_snapshot()
        except ValueError as error:
            QMessageBox.warning(self, "현재 시트를 내보낼 수 없습니다", str(error))
            return
        self.current_sheet_export_requested.emit(snapshot)

    def replace_roster(
        self,
        roster: RosterSnapshot,
        histories: Mapping[tuple[UUID, Meal], StatusHistory] | None = None,
        *,
        revision_display_code: str | None = None,
        imported_profile_fields: frozenset[str] | None = None,
    ) -> None:
        if not isinstance(roster, RosterSnapshot):
            raise TypeError("roster는 RosterSnapshot이어야 합니다.")
        self._roster = roster
        self._histories = dict(histories or {})
        self._meal_change_history_rows.clear()
        self._revision_display_code = revision_display_code
        self._column_layout.save()
        self._model.replace_roster(roster, self._histories, imported_profile_fields=imported_profile_fields)
        self._refresh_archive()
        self._revision_label.setText(self._revision_label_text())
        self._column_layout.replace_columns(self._model.columns)
        self._column_search_header.replace_columns(self._model.columns)
        self._after_model_reset()

    def set_imported_profile_fields(self, fields: frozenset[str]) -> None:
        if fields == self._model._imported_profile_fields:
            return
        self.replace_roster(
            self._roster, self._histories,
            revision_display_code=self._revision_display_code,
            imported_profile_fields=fields,
        )

    def set_household_shared_columns(self, columns: frozenset[str]) -> None:
        self.set_household_shared_state(columns, self._model._household_shared_exceptions)

    def set_household_shared_state(
        self,
        columns: frozenset[str],
        exceptions: frozenset[tuple[UUID, str]],
    ) -> None:
        columns, exceptions = frozenset(columns), frozenset(exceptions)
        if (columns == self._model._household_shared_columns
                and exceptions == self._model._household_shared_exceptions
                and not self._pending_exception_filters):
            return
        selected = self.selected_person_cell()
        self._model.beginResetModel()
        self._model._household_shared_columns = columns
        self._model._household_shared_exceptions = exceptions
        self._model._exception_only_columns.update(self._pending_exception_filters)
        self._pending_exception_filters.clear()
        self._model._exception_only_columns.intersection_update(
            columns, {c.key for c in self._model.columns})
        self._model._refresh_people()
        self._model.endResetModel()
        self._column_search_header.refresh_buttons()
        self._after_model_reset()
        self._save_search_filters()
        if selected is not None:
            self.restore_person_cell(selected)
        if self._model.columnCount():
            self._model.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, self._model.columnCount() - 1)

    def set_routing_state(self, state: RoutingState) -> None:
        if not isinstance(state, RoutingState):
            raise TypeError("state는 RoutingState여야 합니다.")
        selected_cell = self.selected_person_cell()
        self._routing_state = state
        self._model.set_routing_state(state)
        self._after_model_reset()
        if selected_cell is not None:
            self.restore_person_cell(selected_cell)

    def set_school_calendar(self, calendar: SchoolLunchCalendar) -> None:
        if not isinstance(calendar, SchoolLunchCalendar):
            raise TypeError("calendar는 SchoolLunchCalendar여야 합니다.")
        self._school_calendar = calendar
        self._model.set_school_calendar(calendar)
        self._refresh_archive()

    def set_service_adjustment_schedule(
        self,
        schedule: ServiceAdjustmentSchedule,
    ) -> None:
        if not isinstance(schedule, ServiceAdjustmentSchedule):
            raise TypeError("schedule은 ServiceAdjustmentSchedule이어야 합니다.")
        self._service_adjustment_schedule = schedule
        self._after_model_reset()

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self._date_spine = self._build_date_spine()
        root_layout.addWidget(self._date_spine)
        self._date_panel_edge = self._panel_rail("datePanelRail")
        root_layout.addWidget(self._date_panel_edge)
        self._date_spine_toggle = self._panel_toggle(root, self._date_panel_edge, "날짜·식종", False)
        self._date_spine_toggle.toggled.connect(self._set_date_spine_visible)
        self._date_width_animation = self._panel_animation(self._apply_date_spine_width)
        self._date_width_animation.finished.connect(self._finish_date_spine_animation)
        root_layout.addWidget(self._build_workspace(), 1)
        self.setCentralWidget(root)

    def _build_date_spine(self) -> QFrame:
        spine = QFrame()
        spine.setObjectName("dateSpine")
        spine.setFixedWidth(228)
        layout = QVBoxLayout(spine)
        layout.setContentsMargins(20, 24, 20, 22)
        layout.setSpacing(10)

        eyebrow = QLabel("급식 배송 관리")
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
        self._date_edit = WheelSafeDateEdit()
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

        meal_summary_label = QLabel("식종별 현황")
        meal_summary_label.setObjectName("spineLabel")
        layout.addWidget(meal_summary_label)
        self._meal_summary_widgets: dict[Meal, QLabel] = {}
        for meal in _MEAL_ORDER:
            summary = QLabel()
            summary.setObjectName("serviceWeekday")
            summary.setWordWrap(True)
            summary.setToolTip("현재 표시된 명단 기준입니다. '-' 비대상은 집계에서 제외합니다.")
            self._meal_summary_widgets[meal] = summary
            layout.addWidget(summary)

        layout.addSpacing(14)
        birthday_label = QLabel("생일 월")
        birthday_label.setObjectName("spineLabel")
        layout.addWidget(birthday_label)
        self._birth_month_filter = QComboBox()
        self._birth_month_filter.setAccessibleName("생일 월")
        self._birth_month_filter.addItem("전체", None)
        self._birth_month_filter.addItem("이번 달", 0)
        for month in range(1, 13):
            self._birth_month_filter.addItem(f"{month}월", month)
        self._birth_month_filter.setToolTip(
            "생년월일의 월로 명단을 좁힙니다. 이번 달은 오늘 날짜 기준입니다."
        )
        self._birth_month_filter.currentIndexChanged.connect(self._on_birth_month_changed)
        layout.addWidget(self._birth_month_filter)

        layout.addSpacing(14)
        count_label = QLabel("조회 명단")
        count_label.setObjectName("spineLabel")
        layout.addWidget(count_label)
        self._count_metric = QLabel()
        self._count_metric.setObjectName("spineMetric")
        layout.addWidget(self._count_metric)
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

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(1)
        self._splitter.addWidget(self._build_table_frame())
        self._inspector_container = QWidget()
        inspector_layout = QHBoxLayout(self._inspector_container)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        inspector_layout.setSpacing(0)
        self._inspector_panel_edge = self._panel_rail("inspectorPanelRail")
        inspector_layout.addWidget(self._inspector_panel_edge)
        self._inspector = self._build_inspector()
        inspector_layout.addWidget(self._inspector)
        self._inspector_container.setMinimumWidth(282)
        self._inspector_container.setMaximumWidth(392)
        self._splitter.addWidget(self._inspector_container)
        self._inspector_toggle = self._panel_toggle(
            workspace, self._inspector_container, "상세정보", True
        )
        self._inspector_toggle.toggled.connect(self._set_inspector_visible)
        self._inspector_width_animation = self._panel_animation(self._apply_inspector_width)
        self._inspector_width_animation.finished.connect(self._finish_inspector_animation)
        self._splitter.setStretchFactor(0, 1)
        self._inspector_width = 334
        self._splitter.setSizes([900, self._inspector_width])
        self._splitter.splitterMoved.connect(self._remember_inspector_width)
        layout.addWidget(self._splitter, 1)
        return workspace

    def _build_header(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("workspaceHeader")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 12, 14, 12)
        layout.setSpacing(12)

        heading = QHBoxLayout()
        heading.setSpacing(9)
        title_column = QVBoxLayout()
        title_column.setSpacing(3)
        title = QLabel("대상자 명단")
        self._roster_title = title
        title.setObjectName("workspaceTitle")
        subtitle = QLabel("선택일의 가입·상태·배송 배정을 확인합니다.")
        subtitle.setObjectName("workspaceSubtitle")
        title_column.addWidget(title)
        title_column.addWidget(subtitle)
        heading.addLayout(title_column, 1)
        self._documents_button = QPushButton("문서 만들기")
        self._documents_button.setObjectName("documentHubAction")
        self._documents_button.setToolTip(
            "청구서·확인서·일일식수·배송대타를 한 곳에서 선택합니다"
        )
        self._documents_button.clicked.connect(self.documents_requested.emit)
        heading.addWidget(self._documents_button)
        self._more_button = QPushButton("더보기")
        self._more_button.setObjectName("quietAction")
        self._more_button.setToolTip("명단 관리, 이력·민원, 통계, 내보내기, 기사 관리, 운영, 화면·설정")
        self._more_menu = self._build_more_menu()
        self._more_button.setCheckable(True)
        self._more_panel = MoreActionsPanel(self._more_menu, self)
        self._more_button.toggled.connect(self._toggle_more_panel)
        self._more_panel.action_selected.connect(lambda: self._more_button.setChecked(False))
        heading.addWidget(self._more_button)
        layout.addLayout(heading)

        filters = QHBoxLayout()
        filters.setSpacing(9)
        self._search_input = QLineEdit(self)
        self._search_input.setPlaceholderText("성명, 학교, 전화, 주소 검색")
        self._search_input.setAccessibleName("전체 명단 검색")
        self._search_input.textChanged.connect(self._on_search_changed)
        filters.addWidget(self._search_input, 1)
        self._include_hidden_filter = QCheckBox("숨긴 행 포함")
        self._include_hidden_filter.setToolTip("일반 명단에 장기 미배송으로 숨긴 행도 표시하고 검색합니다. 다른 필터는 유지합니다.")
        self._include_hidden_filter.toggled.connect(self._on_include_hidden_changed)
        filters.addWidget(self._include_hidden_filter)
        self._special_only_filter = QCheckBox("별도 설정만")
        self._special_only_filter.setToolTip("개인별 적용 배송 설정이 기본값과 다른 대상자만 표시합니다")
        self._special_only_filter.toggled.connect(self._on_special_only_changed)
        self._special_only_filter.toggled.connect(self._special_only_action.setChecked)
        self._special_only_action.toggled.connect(self._special_only_filter.setChecked)
        self._driver_households_button = QPushButton("기사별 가구")
        self._driver_households_button.setObjectName("quietAction")
        self._driver_households_button.setToolTip(
            "선택일에 배송 상태인 모든 식종의 담당 가구를 기사별로 봅니다"
        )
        self._driver_households_button.clicked.connect(lambda: launch(self._show_driver_households))
        filters.addWidget(self._driver_households_button)
        self._add_person_button = QPushButton("행 추가")
        self._add_person_button.setObjectName("quietAction")
        self._add_person_button.setToolTip("현재 식종에 가입한 대상자를 명단에 추가합니다")
        self._add_person_button.clicked.connect(lambda: self.person_add_requested.emit(self._model.meal))
        filters.addWidget(self._add_person_button)
        layout.addLayout(filters)
        self._column_filter_strip = QWidget()
        filter_notice = QHBoxLayout(self._column_filter_strip)
        filter_notice.setContentsMargins(0, 0, 0, 0)
        filter_notice.setSpacing(9)
        self._column_filter_label = QLabel()
        self._column_filter_label.setObjectName("workspaceSubtitle")
        self._column_filter_label.setWordWrap(True)
        filter_notice.addWidget(self._column_filter_label, 1)
        self._clear_column_filters_button = QPushButton("칼럼 필터 해제")
        self._clear_column_filters_button.setObjectName("quietAction")
        self._clear_column_filters_button.clicked.connect(self._clear_column_filters)
        filter_notice.addWidget(self._clear_column_filters_button)
        self._archive_threshold_button = QPushButton("보관 기준 설정")
        self._archive_threshold_button.setObjectName("quietAction")
        self._archive_threshold_button.setToolTip("장기 미배송 보관 기준 일수를 변경합니다")
        self._archive_threshold_button.clicked.connect(lambda: launch(self._show_archive_threshold))
        self._archive_threshold_button.hide()
        filter_notice.addWidget(self._archive_threshold_button)
        self._archive_exit_button = QPushButton("나가기")
        self._archive_exit_button.setObjectName("quietAction")
        self._archive_exit_button.setToolTip("보관함을 나가 일반 명단으로 돌아갑니다")
        self._archive_exit_button.clicked.connect(lambda: self._archive_action.setChecked(False))
        self._archive_exit_button.hide()
        filter_notice.addWidget(self._archive_exit_button)
        layout.addWidget(self._column_filter_strip)
        self._column_filter_strip.hide()
        return frame

    def _position_more_panel(self) -> None:
        anchor = self._more_button.mapTo(self, self._more_button.rect().bottomRight())
        width = min(360, self.width() - 24)
        height = min(340, self.height() - anchor.y() - 18)
        self._more_panel.setGeometry(max(12, anchor.x() - width + 1), anchor.y() + 6, width, height)

    def _toggle_more_panel(self, expanded: bool) -> None:
        if expanded:
            self._position_more_panel()
            self._more_panel.show()
            self._more_panel.raise_()
            self._more_panel.group_buttons[0].setFocus()
        else:
            self._more_panel.hide()
            self._more_button.setFocus()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_more_panel") and self._more_panel.isVisible():
            self._position_more_panel()

    def _archive_ids_on(self, day: date, *, days: int | None = None):
        has_visible_history: Callable[[UUID], bool] | None = None
        if self._meal_change_history_provider is not None:
            has_visible_history = self._meal_history_presence_provider
            if has_visible_history is None:
                def has_visible_history(person_id: UUID) -> bool:
                    return bool(self._meal_history_rows_for(self._roster.person(person_id)))

        return inactive_person_ids(
            self._roster, self._histories,
            days=self._archive_settings.days if days is None else days,
            as_of=day,
            status_on=self._model.effective_status_on,
            has_visible_history=has_visible_history,
        )

    def _refresh_archive(self, *, days: int | None = None) -> None:
        ids = self._archive_ids_on(self._model.service_day, days=days)
        self._model.set_archive_filter(ids, self._archive_action.isChecked())
        self._roster_title.setText(
            f"장기 미배송 보관함 ({len(ids)}명)" if self._archive_action.isChecked()
            else "대상자 명단"
        )
        self._after_model_reset()

    @dialog_workflow
    def _show_archive_threshold(self) -> None:
        dialog = InactiveThresholdDialog(self._archive_settings.days, self)
        dialog.days.valueChanged.connect(lambda days: self._refresh_archive(days=days))
        if (yield DialogRequest(dialog)) == QDialog.DialogCode.Accepted:
            self._archive_settings.save_days(dialog.days.value())
        self._refresh_archive()
        dialog.deleteLater()

    def _panel_toggle(
        self, host: QWidget, boundary: QWidget, label: str, points_right: bool
    ) -> PanelEdgeHandle:
        button = PanelEdgeHandle(host, boundary, points_right=points_right)
        button.setAccessibleName(f"{label} 패널 접기")
        button.setToolTip(f"{label} 패널 접기")
        return button

    def _panel_rail(self, name: str) -> QFrame:
        rail = QFrame()
        rail.setObjectName(name)
        rail.setFixedWidth(2)
        return rail

    def _panel_animation(self, on_value: Callable[[int], None]) -> QVariantAnimation:
        animation = QVariantAnimation(self)
        animation.setDuration(150)
        animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        animation.valueChanged.connect(lambda value: on_value(int(value)))
        return animation

    def _panel_duration(self) -> int:
        preference = getattr(QGuiApplication.styleHints(), "reducedMotion", None)
        reduced = preference() if callable(preference) else bool(preference)
        return 0 if reduced else 150

    def _start_panel_animation(self, animation: QVariantAnimation, start: int, end: int) -> None:
        animation.stop()
        animation.setDuration(self._panel_duration())
        animation.setStartValue(start)
        animation.setEndValue(end)
        if animation.duration() == 0:
            animation.start()
            animation.setCurrentTime(0)
        else:
            animation.start()

    def _apply_date_spine_width(self, width: int) -> None:
        self._date_spine.setFixedWidth(max(0, width))
        self._date_spine_toggle.reposition()

    def _finish_date_spine_animation(self) -> None:
        if not self._date_spine_toggle.isChecked():
            self._date_spine.hide()
        self._date_spine_toggle.reposition()

    def _set_date_spine_visible(self, visible: bool) -> None:
        start = self._date_spine.width() if self._date_spine.isVisible() else 0
        if visible:
            self._date_spine.show()
        self._start_panel_animation(self._date_width_animation, start, 228 if visible else 0)
        self._date_spine_toggle.set_points_right(not visible)
        action = f"날짜·식종 패널 {'접기' if visible else '펼치기'}"
        self._date_spine_toggle.setToolTip(action)
        self._date_spine_toggle.setAccessibleName(action)

    def _remember_inspector_width(self, _position: int, _index: int) -> None:
        if self._inspector_toggle.isChecked() and self._inspector_width_animation.state() == QVariantAnimation.State.Stopped:
            self._inspector_width = max(282, self._splitter.sizes()[1])

    def _apply_inspector_width(self, width: int) -> None:
        width = max(2, width)
        self._inspector_container.setFixedWidth(width)
        self._splitter.setSizes([max(1, self._splitter.width() - width - 1), width])
        self._inspector_toggle.reposition()

    def _finish_inspector_animation(self) -> None:
        if self._inspector_toggle.isChecked():
            self._inspector_container.setMinimumWidth(282)
            self._inspector_container.setMaximumWidth(392)
        else:
            self._inspector.hide()
        self._inspector_toggle.reposition()

    def _set_inspector_visible(self, visible: bool) -> None:
        start = self._inspector_container.width()
        if not visible:
            if self._inspector_width_animation.state() == QVariantAnimation.State.Stopped:
                self._inspector_width = max(282, start)
        if visible:
            self._inspector.show()
        self._start_panel_animation(
            self._inspector_width_animation, start, self._inspector_width if visible else 2
        )
        self._inspector_toggle.set_points_right(visible)
        action = f"상세정보 패널 {'접기' if visible else '펼치기'}"
        self._inspector_toggle.setToolTip(action)
        self._inspector_toggle.setAccessibleName(action)

    def _build_more_menu(self) -> QMenu:
        menu = QMenu(self)
        # Keep both Qt wrappers alive when callers inspect temporary action lists.
        self._more_submenus = []
        self._more_group_actions = []
        groups = (
            ("명단 관리", (
                ("_import_action", "Excel 명단 가져오기", self.import_review_requested.emit,
                 "신규 명단을 가져오거나 기존 명단을 업데이트합니다"),
            )),
            ("이력·민원", (
                ("_revision_history_action", "전체 변경 이력", self.revision_history_requested.emit,
                 "저장된 모든 변경번호와 변경 내용을 읽기 전용으로 확인합니다"),
                ("_complaint_history_action", "전체 민원", self.complaint_history_requested.emit,
                 "명단 검색·필터와 관계없이 모든 대상자의 민원을 확인합니다"),
                ("_monthly_meal_history_action", "월별 식종 변경 이력", self.monthly_meal_history_requested.emit,
                 "선택한 달과 관련된 전체 명단의 배송·일시중지·중지 이력을 조회합니다"),
            )),
            ("통계", (
                ("_monthly_statistics_action", "월별 식수", self.monthly_statistics_requested.emit,
                 "실제 배송일 기준 전체 명단의 월별 식수를 조회합니다"),
            )),
            ("내보내기", (
                ("_current_sheet_export_action", "현재 시트 Excel 내보내기", self._request_current_sheet_export,
                 "현재 시트에 보이는 칼럼과 행을 표시 순서 그대로 Excel로 저장합니다"),
                ("_database_export_action", "DB 전체 Excel로 내보내기", self.database_export_requested.emit,
                 "전체 명단·민원·정보 및 식종 변경 이력을 Excel로 저장합니다"),
            )),
            ("기사 관리", (
                ("_drivers_action", "기사 관리", self.drivers_requested.emit,
                 "배송기사를 등록하고 연락처를 관리합니다"),
                ("_driver_replacement_action", "기사 일괄 교체", self.driver_replacement_requested.emit,
                 "기존 기사의 배정을 선택한 날짜부터 새 기사에게 일괄 변경합니다"),
            )),
            ("운영", (
                ("_school_calendar_action", "학교 일정", self.school_calendar_requested.emit,
                 "학교 일정을 수정하거나 Excel에서 다시 가져옵니다"),
                ("_service_adjustment_action", "휴무·공휴일", self.service_adjustment_requested.emit,
                 "공휴일 운영과 업체 자체 휴무, 청구 제외·대체식을 관리합니다"),
                ("_unit_price_action", "단가 관리", self.unit_price_requested.emit,
                 "조식·중식·석식 청구 단가와 적용일을 관리합니다"),
            )),
            ("화면·설정", (
                ("_special_only_action", "별도 설정만", lambda checked=False: self._special_only_filter.setChecked(checked),
                 "개인별 배송 설정이 다른 대상자만 표시합니다"),
                ("_columns_action", "칼럼 설정", self._show_column_menu,
                 "칼럼 숨기기 해제, 기본 배치 복원, 사용자 항목 추가·삭제"),
                ("_settings_action", "설정", self.settings_requested.emit,
                 "생성 Excel 비밀번호와 문서 저장 폴더를 관리합니다"),
            )),
        )
        for label, entries in groups:
            submenu = menu.addMenu(label)
            self._more_submenus.append(submenu)
            self._more_group_actions.append(submenu.menuAction())
            for attribute, text, callback, tooltip in entries:
                action = submenu.addAction(text)
                action.setToolTip(tooltip)
                if attribute == "_special_only_action":
                    action.setCheckable(True)
                action.triggered.connect(callback)
                setattr(self, attribute, action)
            if label == "명단 관리":
                self._archive_action = submenu.addAction("장기 미배송 보관함")
                self._archive_action.setCheckable(True)
                self._archive_action.toggled.connect(lambda _checked: self._refresh_archive())
        return menu

    def driver_household_rows(self, *, include_hidden: bool = False, service_day: date | None = None):
        from src.services.driver_households import build_driver_household_rows

        day = self._model.service_day if service_day is None else service_day
        included_meals = None if include_hidden else {
            person.person_id: tuple(
                meal for meal in Meal
                if self._model.effective_status_on(person.person_id, meal, day)
                in (MealStatus.DELIVERY, MealStatus.PAUSED)
            )
            for person in self._roster.persons
        }
        return build_driver_household_rows(
            self._roster, self._routing_state, day,
            included_meals=included_meals,
            browse_all_meals=True,
            excluded_person_ids=() if include_hidden else (
                self._model._archive_ids if day == self._model.service_day
                else self._archive_ids_on(day)
            ),
        )

    @dialog_workflow
    def _show_driver_households(self) -> None:
        from .driver_households import DriverHouseholdsDialog

        service_day = self._model.service_day
        dialog = DriverHouseholdsDialog(
            self.driver_household_rows(),
            service_day=service_day,
            drivers=self._routing_state.drivers,
            parent=self,
            rows_provider=lambda include_hidden: self.driver_household_rows(
                include_hidden=include_hidden, service_day=service_day
            ),
            settings=self._search_settings if self._remember_view else None,
            settings_key=self._driver_household_search_settings_key,
        )
        dialog.sequence_save_requested.connect(
            lambda targets, value: self.household_sequence_save_requested.emit(targets, value, dialog)
        )
        export_signal = getattr(dialog, "export_requested", None)
        if export_signal is not None and hasattr(export_signal, "connect"):
            export_signal.connect(
                lambda snapshot: self.driver_households_export_requested.emit(snapshot, dialog)
            )
        yield DialogRequest(dialog)
        dialog.deleteLater()

    def _build_table_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("tableFrame")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        self._table = QTableView()
        self._table.setModel(self._model)
        self._column_search_header = ColumnSearchHeader(
            self._table,
            values_provider=self._model.column_filter_values,
            selection_provider=self._model.selected_column_values,
            search_provider=self._model.selected_column_search,
            picker_provider=lambda key: SequenceValuePicker if key == "sequence" else ColumnValuePicker,
            exception_provider=self._model.exception_filter_state,
        )
        self._table.setHorizontalHeader(self._column_search_header)
        self._column_search_header.replace_columns(self._model.columns)
        self._column_search_header.search_changed.connect(self._on_column_search_changed)
        self._column_search_header.values_changed.connect(self._on_column_values_changed)
        self._column_search_header.exceptions_changed.connect(self._on_exception_filter_changed)
        self._table.horizontalScrollBar().valueChanged.connect(
            self._column_search_header.update_positions
        )
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self._cell_edit_delegate = RosterCellEditDelegate(self._table)
        self._table.setItemDelegate(self._cell_edit_delegate)
        self._cell_edit_delegate.save_requested.connect(self.cell_edit_requested.emit)
        self._table.doubleClicked.connect(self._on_table_double_clicked)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._open_context_menu)
        self._table.selectionModel().selectionChanged.connect(self._update_inspector)
        self._table.verticalHeader().setDefaultSectionSize(76)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        for index, column in enumerate(self._model.columns):
            header.resizeSection(index, column.width)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._table.setSortingEnabled(False)
        self._model.sort(-1)
        header.setSortIndicatorShown(False)
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        header.sectionClicked.connect(self._model.cycle_sort)
        self._model.modelReset.connect(
            lambda: header.set_sort_criteria(self._model.sort_criteria)
        )
        header.setToolTip(
            "열 이름 클릭: 오름차순 → 내림차순 → 이 열 정렬 해제 · 먼저 선택한 열부터 우선 정렬 · 숫자: 우선순위 · ▾: 검색과 값 필터 · 드래그: 칼럼 이동 · 우클릭: 이름 변경·숨기기"
        )
        self._column_layout = RosterColumnLayout(
            self._table, self._model.columns, settings=self._search_settings, parent=self,
            settings_key=self._sheet_layout_key(self._active_sheet_id),
            mandatory_keys=frozenset(),
            migrate_legacy=len(self._sheets) == 1 and self._sheets[0][0] == "main",
            preserve_temporary_keys=frozenset({"delivery_settings"}),
            hide_new_custom_columns=self._active_sheet_id != self._default_sheet_id(),
        )
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._show_header_column_menu)
        layout.addWidget(self._table)
        sheet_row = QHBoxLayout()
        sheet_row.setContentsMargins(10, 0, 10, 6)
        sheet_row.setSpacing(6)
        self._sheet_tabs = _RosterSheetTabBar(frame)
        self._sheet_tabs.setObjectName("rosterSheetTabs")
        self._sheet_tabs.setMovable(True)
        self._sheet_tabs.setExpanding(False)
        self._sheet_tabs.setUsesScrollButtons(True)
        self._sheet_tabs.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self._sheet_tabs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        for sheet_id, name in self._sheets:
            self._sheet_tabs.addTab(name)
        self._sheet_tabs.setCurrentIndex(self._sheet_index(self._active_sheet_id))
        self._sheet_tabs.currentChanged.connect(self._switch_sheet)
        self._sheet_tabs.tabMoved.connect(self._move_sheet)
        self._sheet_tabs.customContextMenuRequested.connect(lambda position: launch(self._show_sheet_menu, position))
        sheet_row.addWidget(self._sheet_tabs)
        add_sheet = QToolButton(frame)
        add_sheet.setObjectName("addRosterSheet")
        add_sheet.setText("+")
        add_sheet.setToolTip("명단 시트 추가")
        add_sheet.setAccessibleName("명단 시트 추가")
        add_sheet.setStyleSheet(
            "QToolButton#addRosterSheet { background: #FFFFFF; border: 1px solid #CAD8DD; "
            "border-radius: 4px; color: #16788A; font-weight: 700; min-width: 26px; "
            "min-height: 26px; } QToolButton#addRosterSheet:hover { background: #DCEFF2; }"
        )
        add_sheet.clicked.connect(lambda: launch(self._prompt_add_sheet))
        sheet_row.addWidget(add_sheet)
        sheet_row.addStretch(1)
        layout.addLayout(sheet_row)
        return frame

    def _show_header_column_menu(self, position) -> None:
        header = self._table.horizontalHeader()
        logical_index = header.logicalIndexAt(position)
        if logical_index < 0:
            return
        column_key = self._model.columns[logical_index].key
        self._show_column_menu(header.mapToGlobal(position), column_key=column_key)

    def _show_column_menu(self, position=None, *, column_key: str | None = None) -> None:
        column = next(
            (column for column in self._model.columns if column.key == column_key),
            None,
        )
        rename_callback = (
            (lambda key: launch(self._prompt_column_alias, key))
            if column is not None and column.custom_field is None else None
        )
        menu = (
            self._column_layout.create_menu(self)
            if column_key is None
            else self._column_layout.create_menu(
                self,
                column_key=column_key,
                rename_callback=rename_callback,
                include_reset=False,
            )
        )
        if column is None:
            menu.addSeparator()
            self._add_household_shared_column_actions(menu, column)
            manage = menu.addAction("사용자 항목 추가·삭제")
            manage.triggered.connect(self.custom_fields_requested.emit)
        elif column.meal is None:
            menu.addSeparator()
            input_action = menu.addAction("입력 제한 설정…")
            if column.key == "delivery_settings" or (column.custom_field is not None and column.custom_field.field_type is CustomFieldType.CHOICE):
                input_action.setEnabled(False)
                input_action.setToolTip("직접 입력하는 칼럼에서 설정할 수 있습니다." if column.key == "delivery_settings" else "선택 목록형 항목은 사용자 항목의 선택지를 사용합니다.")
            else:
                input_action.triggered.connect(lambda checked=False, key=column.key: self.column_input_rule_requested.emit(key))
        if position is None or isinstance(position, bool):
            position = self._more_button.mapToGlobal(self._more_button.rect().bottomLeft())
        menu.exec(position)

    def _add_household_shared_column_actions(self, menu, column=None) -> None:
        if column is not None:
            key = column.key
            action = menu.addAction("가구 공용 설정")
            action.setCheckable(True)
            action.setChecked(key in self._model._household_shared_columns)
            action.setToolTip("체크하면 가구 공용으로 사용하고, 체크를 해제하면 현재 값을 유지하며 공용 사용을 끝냅니다.")
            action.triggered.connect(lambda checked=False, key=key, target=action: self._request_household_shared_toggle(key, target))
            return
        shared_menu = menu.addMenu("가구 공용 설정")
        menu._household_shared_submenu = shared_menu
        menu._household_shared_action = shared_menu.menuAction()
        columns = list(self._model.columns)
        if not any(item.key == "delivery_settings" for item in columns):
            columns.append(_Column("delivery_settings", self._column_aliases.get("delivery_settings", "배송 설정"), 240))
        for item in columns:
            action = shared_menu.addAction(item.label)
            action.setCheckable(True)
            action.setChecked(item.key in self._model._household_shared_columns)
            action.setToolTip("체크하면 가구 공용으로 사용하고, 체크를 해제하면 현재 값을 유지하며 공용 사용을 끝냅니다.")
            action.triggered.connect(lambda checked=False, key=item.key, target=action: self._request_household_shared_toggle(key, target))

    def _request_household_shared_toggle(self, key, action) -> None:
        self.household_shared_column_requested.emit(key)
        # A cancelled conflict review must not leave a checked menu item behind.
        action.setChecked(key in self._model._household_shared_columns)

    def _add_household_shared_exception_action(
        self, menu: QMenu, person: Person, column_key: str | None,
    ) -> None:
        if column_key not in self._model._household_shared_columns:
            return
        pair = (person.person_id, column_key)
        action = menu.addAction("공용 예외")
        action.setCheckable(True)
        action.setChecked(pair in self._model._household_shared_exceptions)
        action.setToolTip("체크하면 이 대상자의 선택한 칼럼만 개별 관리합니다.")
        action.triggered.connect(
            lambda checked=False, target=action, person_id=person.person_id, key=column_key:
            self._request_household_shared_exception(person_id, key, checked, target)
        )

    def _request_household_shared_exception(
        self, person_id: UUID, column_key: str, enabled: bool, action: QAction,
    ) -> None:
        self.household_shared_exception_requested.emit(person_id, column_key, enabled)
        action.setChecked(
            (person_id, column_key) in self._model._household_shared_exceptions
        )

    def _load_column_aliases(self) -> dict[str, str]:
        if not self._remember_view or not self._search_settings.contains(
            self._column_alias_settings_key
        ):
            return {}
        raw = self._search_settings.value(self._column_alias_settings_key)
        if not isinstance(raw, str):
            return {}
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(value, dict):
            return {}
        return {
            key: label.strip()
            for key, label in value.items()
            if isinstance(key, str)
            and not key.startswith("custom:")
            and isinstance(label, str)
            and label.strip()
            and len(label.strip()) <= 40
        }

    def _save_column_aliases(self) -> None:
        if not self._remember_view:
            return
        self._search_settings.setValue(
            self._column_alias_settings_key,
            json.dumps(self._column_aliases, ensure_ascii=False),
        )
        self._search_settings.sync()

    def _rename_column(self, key: str, label: str) -> bool:
        changed = self._model.set_column_alias(key, label)
        if not changed:
            return False
        self._column_aliases[key] = self._model.columns[
            next(index for index, column in enumerate(self._model.columns) if column.key == key)
        ].label
        self._save_column_aliases()
        self._column_layout.replace_columns(self._model.columns)
        self._column_search_header.replace_columns(self._model.columns)
        self._refresh_column_filter_strip()
        return True

    @dialog_workflow
    def _prompt_column_alias(self, key: str) -> None:
        column = next(column for column in self._model.columns if column.key == key)
        dialog = QDialog(self)
        dialog.setWindowTitle("칼럼 이름 바꾸기")
        dialog.setStyleSheet(application_stylesheet())
        content = QVBoxLayout(dialog)
        content.addWidget(QLabel("화면에 표시할 칼럼 이름"))
        entry = QLineEdit(column.label, dialog)
        entry.setMaxLength(40)
        entry.setAccessibleName("칼럼 이름")
        content.addWidget(entry)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog,
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("quietAction")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        content.addWidget(buttons)
        entry.selectAll()
        entry.setFocus()
        if (yield DialogRequest(dialog)) != QDialog.DialogCode.Accepted:
            return
        try:
            self._rename_column(key, entry.text())
        except ValueError as error:
            QMessageBox.warning(self, "칼럼 이름", str(error))

    def _sheet_layout_key(self, sheet_id: str) -> str:
        return f"{self._sheet_settings_key}/layout/{sheet_id}"

    def _sheet_index(self, sheet_id: str) -> int:
        return next(index for index, item in enumerate(self._sheets) if item[0] == sheet_id)

    def _default_sheet_id(self) -> str:
        return "main" if any(sheet_id == "main" for sheet_id, _ in self._sheets) else self._sheets[0][0]

    def _load_sheets(self) -> tuple[list[tuple[str, str]], str]:
        if self._remember_view:
            raw = self._search_settings.value(self._sheet_settings_key)
            try:
                state = json.loads(raw) if isinstance(raw, str) else None
            except ValueError:
                state = None
            if isinstance(state, dict) and isinstance(state.get("sheets"), list):
                sheets: list[tuple[str, str]] = []
                for item in state["sheets"]:
                    if not isinstance(item, dict):
                        break
                    sheet_id, name = item.get("id"), item.get("name")
                    if not isinstance(sheet_id, str) or not isinstance(name, str):
                        break
                    if sheet_id != "main":
                        try:
                            UUID(sheet_id)
                        except ValueError:
                            break
                    name = name.strip()
                    if not name or len(name) > 40 or any(
                        old_id == sheet_id or old_name.casefold() == name.casefold()
                        for old_id, old_name in sheets
                    ):
                        break
                    sheets.append((sheet_id, name))
                else:
                    active = state.get("active")
                    if isinstance(active, str) and sheets and active in {sheet_id for sheet_id, _ in sheets}:
                        return sheets, active
        return [("main", "명단")], "main"

    def _save_sheets(self) -> None:
        if not self._remember_view:
            return
        state = {
            "sheets": [{"id": sheet_id, "name": name} for sheet_id, name in self._sheets],
            "active": self._active_sheet_id,
        }
        self._search_settings.setValue(
            self._sheet_settings_key, json.dumps(state, ensure_ascii=False)
        )
        self._search_settings.sync()

    def _switch_sheet(self, index: int) -> None:
        if not 0 <= index < len(self._sheets):
            return
        sheet_id = self._sheets[index][0]
        if sheet_id == self._active_sheet_id:
            return
        selected = self._selected_index()
        selected_row = selected.row() if selected.isValid() else None
        self._table.clearSelection()
        self._active_sheet_id = sheet_id
        self._column_layout.switch_layout(
            self._sheet_layout_key(sheet_id),
            hide_new_custom_columns=sheet_id != self._default_sheet_id(),
        )
        if selected_row is not None and selected_row < self._model.rowCount():
            name_column = next(
                i for i, column in enumerate(self._model.columns) if column.key == "name"
            )
            self._table.selectionModel().setCurrentIndex(
                self._model.index(selected_row, name_column),
                QItemSelectionModel.SelectionFlag.ClearAndSelect,
            )
        self._save_sheets()

    def _move_sheet(self, old: int, new: int) -> None:
        self._sheets.insert(new, self._sheets.pop(old))
        self._column_layout.set_hide_new_custom_columns(
            self._active_sheet_id != self._default_sheet_id()
        )
        self._save_sheets()

    def _check_sheet_name(self, name: str, except_index: int | None = None) -> str:
        name = name.strip()
        if not name or len(name) > 40:
            raise ValueError("시트 이름은 1~40자로 입력해 주세요.")
        if any(
            index != except_index and existing.casefold() == name.casefold()
            for index, (_, existing) in enumerate(self._sheets)
        ):
            raise ValueError("같은 이름의 시트가 이미 있습니다.")
        return name

    def _add_sheet(self, name: str, visible_keys: set[str]) -> int:
        name = self._check_sheet_name(name)
        valid_keys = {column.key for column in self._model.columns}
        if not visible_keys <= valid_keys:
            raise ValueError("현재 명단에 없는 열은 시트에 추가할 수 없습니다.")
        sheet_id = str(uuid4())
        self._sheets.append((sheet_id, name))
        self._sheet_tabs.addTab(name)
        index = len(self._sheets) - 1
        self._sheet_tabs.setCurrentIndex(index)
        self._column_layout.set_visible_keys(set(visible_keys))
        self._save_sheets()
        return index

    def _rename_sheet(self, index: int, name: str) -> None:
        if not 0 <= index < len(self._sheets):
            raise IndexError(index)
        name = self._check_sheet_name(name, index)
        sheet_id, _ = self._sheets[index]
        self._sheets[index] = (sheet_id, name)
        self._sheet_tabs.setTabText(index, name)
        self._save_sheets()

    def _delete_sheet(self, index: int) -> None:
        if not 0 <= index < len(self._sheets):
            raise IndexError(index)
        if len(self._sheets) == 1:
            raise ValueError("마지막 명단 시트는 삭제할 수 없습니다.")
        sheet_id = self._sheets[index][0]
        if sheet_id == self._active_sheet_id:
            self._sheet_tabs.setCurrentIndex(0 if index != 0 else 1)
        self._sheets.pop(index)
        self._sheet_tabs.blockSignals(True)
        self._sheet_tabs.removeTab(index)
        self._sheet_tabs.blockSignals(False)
        self._search_settings.remove(self._sheet_layout_key(sheet_id))
        self._column_layout.set_hide_new_custom_columns(
            self._active_sheet_id != self._default_sheet_id()
        )
        self._save_sheets()

    @dialog_workflow
    def _prompt_sheet_name(self, title: str, current: str = "") -> tuple[str, bool]:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setStyleSheet(application_stylesheet())
        content = QVBoxLayout(dialog)
        content.addWidget(QLabel("시트 이름"))
        entry = QLineEdit(current, dialog)
        entry.setMaxLength(40)
        entry.setAccessibleName("시트 이름")
        content.addWidget(entry)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("quietAction")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        content.addWidget(buttons)
        entry.selectAll()
        entry.setFocus()
        accepted = (yield DialogRequest(dialog)) == QDialog.DialogCode.Accepted
        return entry.text(), accepted

    @dialog_workflow
    def _prompt_add_sheet(self) -> None:
        name, accepted = yield from invoke(self._prompt_sheet_name, "시트 추가")
        if not accepted:
            return
        try:
            name = self._check_sheet_name(name)
        except ValueError as error:
            QMessageBox.warning(self, "시트 이름", str(error))
            return
        self._add_sheet(name, {column.key for column in self._model.columns})

    @dialog_workflow
    def _show_sheet_menu(self, position) -> None:
        index = self._sheet_tabs.tabAt(position)
        if index < 0:
            return
        menu = QMenu(self)
        rename_action = menu.addAction("이름 바꾸기")
        delete_action = menu.addAction("시트 삭제")
        delete_action.setEnabled(len(self._sheets) > 1)
        chosen = menu.exec(self._sheet_tabs.mapToGlobal(position))
        if chosen == rename_action:
            sheet_id, old_name = self._sheets[index]
            name, accepted = yield from invoke(self._prompt_sheet_name, "시트 이름 바꾸기", old_name)
            if accepted:
                try:
                    index = self._sheet_index(sheet_id)
                    self._rename_sheet(index, name)
                except StopIteration:
                    QMessageBox.warning(self, "시트 이름", "다른 창에서 삭제한 시트입니다.")
                except ValueError as error:
                    QMessageBox.warning(self, "시트 이름", str(error))
        elif chosen == delete_action:
            self._delete_sheet(index)

    def _build_inspector(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("inspector")
        frame.setMinimumWidth(280)
        frame.setMaximumWidth(390)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(7)
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
        self._delivery_settings_button = QToolButton()
        self._delivery_settings_button.setText("배송 설정")
        self._delivery_settings_button.setToolTip("선택한 사람의 배송 설정 · 필요하면 같은 가구 전체에 적용")
        self._delivery_settings_button.setEnabled(False)
        self._delivery_settings_button.clicked.connect(self._request_delivery_settings)
        heading.addWidget(self._delivery_settings_button)
        layout.addLayout(heading)
        self._inspector_name = QLabel("명단에서 한 사람을 선택하세요")
        self._inspector_name.setObjectName("personName")
        self._inspector_name.setWordWrap(True)
        layout.addWidget(self._inspector_name)
        layout.addSpacing(10)
        household_label = QLabel("같은 가구")
        household_label.setObjectName("sectionLabel")
        layout.addWidget(household_label)
        self._household_list = QListWidget()
        self._household_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._household_list.setFixedHeight(32)
        layout.addWidget(self._household_list)
        history_heading = QHBoxLayout()
        history_label = QLabel("식종 변경 이력")
        history_label.setObjectName("sectionLabel")
        history_heading.addWidget(history_label)
        history_heading.addStretch(1)
        self._meal_history_all_button = QToolButton()
        self._meal_history_all_button.setText("전체 보기")
        self._meal_history_all_button.setToolTip("선택한 사람의 전체 이력 보기")
        self._meal_history_all_button.setEnabled(False)
        self._meal_history_all_button.clicked.connect(self._show_selected_person_history)
        history_heading.addWidget(self._meal_history_all_button)
        layout.addLayout(history_heading)
        self._meal_history_list = _MealHistoryList()
        self._meal_history_list.setObjectName("mealHistory")
        self._meal_history_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._meal_history_list.customContextMenuRequested.connect(self._open_meal_history_context_menu)
        self._meal_history_list.setToolTip("식종 변경을 우클릭하면 선택한 변경 한 건을 취소할 수 있습니다.")
        self._meal_history_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._meal_history_list.setUniformItemSizes(False)
        self._meal_history_list.setSpacing(2)
        self._meal_history_list.setWordWrap(True)
        self._meal_history_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self._meal_history_list.setMinimumHeight(168)
        layout.addWidget(self._meal_history_list, 1)
        guide = QLabel("우클릭: 이력보기·민원·상태 변경")
        guide.setObjectName("muted")
        guide.setWordWrap(True)
        guide.setToolTip("모든 셀 우클릭: 이력보기·민원\n식종 셀 우클릭: 배송 상태·배송담당 변경\nCtrl·Shift로 여러 식종 셀 선택 후 우클릭하면 일괄 변경할 수 있습니다.")
        layout.addWidget(guide)
        return frame

    def _create_context_menu(
        self,
        person: Person,
        meal: Meal | None = None,
        column_key: str | None = None,
        batch_targets: tuple[tuple[UUID, Meal], ...] = (),
    ) -> QMenu:
        menu = QMenu(self)
        if meal is None:
            if column_key == "sequence":
                edit = QAction("순서 수정", menu)
                edit.triggered.connect(lambda: self.sequence_edit_requested.emit(person.person_id))
            else:
                edit = QAction("정보 수정", menu)
                edit.triggered.connect(lambda: self.person_edit_requested.emit(person.person_id))
            menu.addAction(edit)
            if column_key in {"address", "detail_address", "jurisdiction"}:
                relocation = QAction("이사 등록", menu)
                relocation.triggered.connect(lambda: self.relocation_requested.emit(person.person_id))
                menu.addAction(relocation)
            self._add_household_shared_exception_action(menu, person, column_key)
            self._add_person_context_actions(menu, person, None)
            return menu
        for label, status in (
            ("배송", MealStatus.DELIVERY),
            ("일시중지", MealStatus.PAUSED),
            ("중지", MealStatus.STOPPED),
        ):
            if len(batch_targets) > 1:
                action = menu.addAction(f"선택한 식종 {len(batch_targets)}개 · {label}")
                action.triggered.connect(
                    lambda _checked=False, selected=status:
                    self.batch_status_action_requested.emit(batch_targets, selected)
                )
                continue
            action = QAction(f"{meal.value} {label}", menu)
            action.triggered.connect(
                lambda _checked=False, selected=status: self.status_action_requested.emit(
                    person.person_id,
                    meal,
                    selected,
                )
            )
            menu.addAction(action)
        assignment = QAction(
            f"선택한 식종 {len(batch_targets)}개 · 배송담당 변경"
            if len(batch_targets) > 1 else "배송담당 변경", menu
        )
        if len(batch_targets) > 1:
            assignment.triggered.connect(lambda: self.batch_driver_assignment_requested.emit(batch_targets))
        else:
            assignment.triggered.connect(
                lambda: self.driver_assignment_requested.emit(person.person_id, meal)
            )
        menu.addAction(assignment)
        self._add_household_shared_exception_action(menu, person, column_key)
        self._add_person_context_actions(menu, person, meal)
        return menu

    def set_operating_database(self, path):
        self.setWindowTitle(f"급식 배송 운영 · {APP_VERSION} · {path.name}")

    def _add_person_context_actions(
        self, menu: QMenu, person: Person, meal: Meal | None
    ) -> None:
        """Keep history and complaint actions available on every person cell."""
        history = QAction("이력보기", menu)
        history.triggered.connect(lambda: launch(self._show_history, person, meal))
        menu.addAction(history)
        complaint = QAction("민원", menu)
        complaint.triggered.connect(
            lambda: self.complaint_requested.emit(person.person_id, meal)
        )
        menu.addAction(complaint)
        menu.addSeparator()
        delete = QAction("명단 행 삭제", menu)
        delete.triggered.connect(lambda: self.person_delete_requested.emit(person.person_id))
        menu.addAction(delete)

    def selected_meal_targets(self) -> tuple[tuple[UUID, Meal], ...]:
        selected = self._table.selectionModel().selectedIndexes()
        targets: dict[tuple[UUID, Meal], None] = {}
        for index in sorted(selected, key=lambda item: (item.row(), item.column())):
            if self._table.isRowHidden(index.row()) or self._table.isColumnHidden(index.column()):
                continue
            column = self._model.columns[index.column()]
            if column.meal is None:
                continue
            person = self._model.person_at(index.row())
            targets[(person.person_id, column.meal)] = None
        return tuple(targets)

    def _open_context_menu(self, position) -> None:
        index = self._table.indexAt(position)
        if not index.isValid():
            return
        self._select_for_context_menu(index)
        person = self._model.person_at(index.row())
        column = self._model.columns[index.column()]
        targets = self.selected_meal_targets() if column.meal is not None else ()
        self._create_context_menu(person, column.meal, column.key, targets).exec(
            self._table.viewport().mapToGlobal(position)
        )

    def _on_table_double_clicked(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        person = self._model.person_at(index.row())
        column = self._model.columns[index.column()]
        if column.meal is not None or is_roster_cell_editable(column.key):
            return
        if column.key == "sequence":
            self.sequence_edit_requested.emit(person.person_id)
        elif column.key == "delivery_settings":
            self.delivery_settings_requested.emit(person.person_id)

    def _select_for_context_menu(self, index: QModelIndex) -> None:
        selection_model = self._table.selectionModel()
        if not selection_model.isSelected(index):
            selection_model.setCurrentIndex(
                index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect,
            )

    def set_history_provider(self, provider) -> None:
        self._history_provider = provider

    def set_meal_change_history_provider(
        self,
        provider: Callable[[Person], tuple[MealHistoryRow | tuple[str, str], ...]] | None,
        *,
        history_presence_provider: Callable[[UUID], bool] | None = None,
    ) -> None:
        index = self._selected_index()
        selected_person_id = (
            self._model.person_at(index.row()).person_id if index.isValid() else None
        )
        self._meal_change_history_provider = provider
        self._meal_history_presence_provider = history_presence_provider
        self._meal_change_history_rows.clear()
        self._refresh_archive()
        if selected_person_id is not None:
            for row in range(self._model.rowCount()):
                if self._model.person_at(row).person_id == selected_person_id:
                    self._table.selectRow(row)
                    break
        self._update_inspector()

    @dialog_workflow
    def _show_history(self, person: Person, meal: Meal | None = None) -> None:
        selected_meal = self._meal if meal is None else meal
        history = self._histories.get((person.person_id, selected_meal))
        dialog = (
            self._history_provider(person, meal, self)
            if self._history_provider is not None
            else StatusHistoryDialog(person, history, self)
        )
        if dialog is None:
            return
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.destroyed.connect(lambda: self._history_dialogs.remove(dialog))
        self._history_dialogs.append(dialog)
        yield DialogRequest(dialog)

    def _set_day(self, day: date) -> None:
        self._date_edit.blockSignals(True)
        self._date_edit.setDate(QDate(day.year, day.month, day.day))
        self._date_edit.blockSignals(False)
        self._date_label.setText(day.strftime("%Y.%m.%d"))
        self._weekday_label.setText(f"{_WEEKDAYS[day.weekday()]} · 조회 기준일")
        self._model.set_context(day, self._meal)
        self._refresh_archive()

    def _set_meal(self, meal: Meal) -> None:
        self._meal = meal
        self._model.set_context(self._model.service_day, meal)
        self._after_model_reset()

    def _on_birth_month_changed(self) -> None:
        self._model.set_birth_month(self._birth_month_filter.currentData())
        self._after_model_reset()

    def _on_special_only_changed(self, enabled: bool) -> None:
        prior_layout = self._column_layout.layout_snapshot() if enabled else None
        self._model.set_special_only(enabled)
        self._column_layout.replace_columns(self._model.columns)
        self._column_search_header.replace_columns(self._model.columns)
        if enabled and self._active_sheet_id == "main" and "delivery_settings" not in prior_layout["widths"]:
            delivery = next(i for i, column in enumerate(self._model.columns) if column.key == "delivery_settings")
            self._table.horizontalHeader().resizeSection(delivery, self._delivery_settings_width)
        self._after_model_reset()
        self._save_search_filters()

    def _shift_day(self, offset: int) -> None:
        shifted = date.fromordinal(self._model.service_day.toordinal() + offset)
        self._set_day(shifted)

    def _on_qdate_changed(self, value: QDate) -> None:
        self._set_day(date(value.year(), value.month(), value.day()))

    def _on_exception_filter_changed(self, key: str, enabled: bool) -> None:
        self._model.set_exception_filter(key, enabled)
        self._column_search_header.refresh_buttons()
        self._after_model_reset()
        self._save_search_filters()

    def _on_column_values_changed(self, key: str, values) -> None:
        self._model.set_column_values(key, values)
        self._after_model_reset()
        self._save_search_filters()

    def _on_column_search_changed(self, key: str, text: str) -> None:
        self._model.set_column_search(key, text)
        self._after_model_reset()
        self._save_search_filters()

    def _clear_column_filters(self) -> None:
        self._model.clear_column_filters()
        self._pending_exception_filters.clear()
        self._column_search_header.refresh_buttons()
        self._after_model_reset()
        self._save_search_filters()

    def _on_search_changed(self, text: str) -> None:
        self._model.set_search(text)
        self._after_model_reset()
        self._save_search_filters()

    def _on_include_hidden_changed(self, enabled: bool) -> None:
        self._model.set_include_hidden_in_search(enabled)
        self._after_model_reset()
        self._save_search_filters()

    _SEARCH_SETTINGS_KEY = "roster/search_v1"

    def _save_search_filters(self) -> None:
        if self._restoring_search or not self._remember_view:
            return
        state = {
            "address_columns_version": 2,
            "global": self._search_input.text(),
            "include_hidden": self._include_hidden_filter.isChecked(),
            "special_only": self._special_only_filter.isChecked(),
            "columns": dict(self._model._column_searches),
            "exception_only": sorted(self._model._exception_only_columns | self._pending_exception_filters),
            # Omission means all values; an empty list means explicitly none.
            "values": {
                key: sorted(values) for key, values in self._model._column_values.items()
            },
        }
        if not any((state["global"], state["include_hidden"], state["special_only"], state["columns"], state["values"], state["exception_only"])):
            self._search_settings.remove(self._search_settings_key)
        else:
            self._search_settings.setValue(
                self._search_settings_key, json.dumps(state, ensure_ascii=False)
            )
        self._search_settings.sync()

    def _restore_search_filters(self) -> None:
        if not self._remember_view:
            return
        raw = self._search_settings.value(self._search_settings_key)
        if not isinstance(raw, str):
            return
        try:
            state = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(state, dict):
            return
        keys = {column.key for column in self._model.columns}
        self._restoring_search = True
        try:
            if type(state.get("special_only")) is bool:
                self._special_only_filter.setChecked(state["special_only"])
                keys = {column.key for column in self._model.columns}
            # Old address filters matched the joined road/detail string. They
            # cannot safely be interpreted as a road-only filter.
            if state.get("address_columns_version") != 2:
                keys.discard("address")
            global_search = state.get("global")
            if isinstance(global_search, str):
                self._search_input.setText(global_search)
            if type(state.get("include_hidden")) is bool:
                self._include_hidden_filter.setChecked(state["include_hidden"])
            searches = state.get("columns")
            if isinstance(searches, dict):
                for key, search in searches.items():
                    if key in keys and isinstance(search, str):
                        self._model.set_column_search(key, search)
            exception_only = state.get("exception_only")
            if isinstance(exception_only, list):
                self._pending_exception_filters = {key for key in exception_only
                                                   if isinstance(key, str) and key in keys}
            selections = state.get("values")
            if isinstance(selections, dict):
                for key, values in selections.items():
                    if key in keys and isinstance(values, list) and all(
                        isinstance(value, str) for value in values
                    ):
                        self._model.set_column_values(key, frozenset(values))
        finally:
            self._restoring_search = False
        self._column_search_header.refresh_buttons()
        self._after_model_reset()
        self._save_search_filters()

    def _set_active_only(self, active_only: bool) -> None:
        self._model.set_active_only(active_only)
        self._after_model_reset()

    def _after_model_reset(self) -> None:
        self._table.clearSelection()
        self._clear_inspector()
        self._count_metric.setText(f"{self._model.rowCount()}명")
        self._refresh_column_filter_strip()
        for meal, counts in self._model.meal_status_counts().items():
            summary = self._meal_summary_widgets[meal]
            defaults = next(
                setting for setting in self._roster.delivery_defaults
                if setting.meal is meal
            )
            delivery_weekdays = {
                defaults.delivery_weekdays[weekday] for weekday in defaults.weekdays
            }
            summary.setText(
                f"{meal.value}  배송 {counts[MealStatus.DELIVERY.value]} · "
                f"일시중지 {counts[MealStatus.PAUSED.value]}"
            )
            summary.setVisible(
                self._model.service_day.weekday() in delivery_weekdays
                and self._service_adjustment_schedule.provision_for(
                    meal, self._model.service_day
                ).treatment is not ServiceTreatment.NOT_PROVIDED
                and (
                    counts[MealStatus.DELIVERY.value] > 0
                    or counts[MealStatus.PAUSED.value] > 0
                )
            )

    def _refresh_column_filter_strip(self) -> None:
        active_columns = [
            column.label for column in self._model.columns
            if column.key in self._model._column_searches
            or column.key in self._model._column_values
            or column.key in self._model._exception_only_columns
        ]
        archive_visible = self._archive_action.isChecked()
        self._column_filter_strip.setVisible(bool(active_columns) or archive_visible)
        self._clear_column_filters_button.setVisible(bool(active_columns))
        self._archive_threshold_button.setVisible(archive_visible)
        self._archive_exit_button.setVisible(archive_visible)
        self._column_filter_label.clear()
        if active_columns:
            label = f"칼럼 필터 적용 중: {', '.join(active_columns)}"
            if self._model.rowCount() == 0:
                label += " · 표시 0명"
            self._column_filter_label.setText(label)

    def _update_inspector(self) -> None:
        index = self._selected_index()
        if not index.isValid():
            self._clear_inspector()
            return
        person = self._model.person_at(index.row())
        self._edit_person_button.setEnabled(True)
        profile = person.profile
        self._inspector_name.setText(profile.name)
        self._household_list.clear()
        members = _inspector_household_members(
            self._roster, person.person_id, self._model.service_day,
        )
        for member in members:
            meals = " · ".join(
                meal.value
                for meal in Meal
                if self._model.effective_status_on(
                    member.person_id,
                    meal,
                    self._model.service_day,
                )
                is MealStatus.DELIVERY
            )
            self._household_list.addItem(f"{member.profile.name}   {meals or '-'}")
        self._delivery_settings_button.setEnabled(True)
        if not members:
            self._household_list.addItem("연결된 가구가 없습니다")
        # Fit complete rows at the current font size while leaving room for history.
        self._household_list.doItemsLayout()
        visible_rows = min(self._household_list.count(), 5)
        self._household_list.setFixedHeight(
            sum(self._household_list.sizeHintForRow(row) for row in range(visible_rows))
            + 2 * self._household_list.frameWidth()
        )
        self._household_list.scrollToTop()
        self._populate_meal_history(person)

    def _request_selected_person_edit(self) -> None:
        index = self._selected_index()
        if not index.isValid():
            return
        person = self._model.person_at(index.row())
        self.person_edit_requested.emit(person.person_id)

    def _request_delivery_settings(self) -> None:
        index = self._selected_index()
        if not index.isValid():
            return
        person = self._model.person_at(index.row())
        self.delivery_settings_requested.emit(person.person_id)

    def _show_selected_person_history(self) -> None:
        index = self._selected_index()
        if index.isValid():
            launch(self._show_history, self._model.person_at(index.row()), None)

    def _selected_index(self) -> QModelIndex:
        selection_model = self._table.selectionModel()
        current = selection_model.currentIndex()
        if current.isValid() and selection_model.isSelected(current):
            return current
        selected = selection_model.selectedIndexes()
        return selected[0] if selected else QModelIndex()

    def selected_person_cell(self) -> tuple[UUID, str] | None:
        index = self._selected_index()
        if not index.isValid():
            return None
        return self._model.person_at(index.row()).person_id, self._model.columns[index.column()].key

    def restore_person_cell(self, selected_cell: tuple[UUID, str]) -> None:
        person_id, column_key = selected_cell
        for row in range(self._model.rowCount()):
            if self._model.person_at(row).person_id != person_id:
                continue
            column = next(
                (index for index, item in enumerate(self._model.columns) if item.key == column_key),
                0,
            )
            self._table.selectionModel().setCurrentIndex(
                self._model.index(row, column),
                QItemSelectionModel.SelectionFlag.ClearAndSelect,
            )
            self._update_inspector()
            return
        self._table.clearSelection()
        self._clear_inspector()

    def _clear_inspector(self) -> None:
        self._edit_person_button.setEnabled(False)
        self._delivery_settings_button.setEnabled(False)
        self._meal_history_all_button.setEnabled(False)
        self._inspector_name.setText("명단에서 한 사람을 선택하세요")
        self._household_list.clear()
        self._household_list.setFixedHeight(32)
        self._meal_history_list.clear()

    def _populate_meal_history(self, person: Person) -> None:
        self._meal_history_list.clear()
        self._meal_history_all_button.setEnabled(True)
        rows = (
            self._meal_history_rows_for(person)
            if self._meal_change_history_provider is not None
            else self._fallback_meal_history_rows(person)
        )
        for row in rows:
            if isinstance(row, MealHistoryRow):
                title = f"{row.meal} · {row.category}"
                period = f"기간 {row.period}"
                tooltip = f"{title}\n{period}"
            else:
                title, tooltip = row
            item = QListWidgetItem("" if isinstance(row, MealHistoryRow) else title)
            item.setSizeHint(QSize(0, 54 if isinstance(row, MealHistoryRow) else 34))
            item.setToolTip(tooltip + ("\n우클릭: 선택한 변경 취소" if getattr(row, "target", None) else ""))
            item.setData(Qt.ItemDataRole.UserRole, getattr(row, "target", None))
            self._meal_history_list.addItem(item)
            if isinstance(row, MealHistoryRow):
                entry = QWidget()
                entry_layout = QVBoxLayout(entry)
                entry_layout.setContentsMargins(5, 2, 5, 2)
                entry_layout.setSpacing(1)
                title_label = QLabel(title)
                title_label.setWordWrap(True)
                title_label.setStyleSheet("font-weight: 700;")
                entry_layout.addWidget(title_label)
                period_label = QLabel(period)
                period_label.setWordWrap(True)
                period_label.setStyleSheet("font-size: 12px;")
                entry_layout.addWidget(period_label)
                entry.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                entry.setToolTip(tooltip)
                entry.setAccessibleName(f"{title}, {period}")
                self._meal_history_list.setItemWidget(item, entry)
        if not rows:
            item = QListWidgetItem("기록된 식종 변경 이력이 없습니다")
            item.setSizeHint(QSize(0, 34))
            self._meal_history_list.addItem(item)
        self._meal_history_list.update_row_heights()

    def _open_meal_history_context_menu(self, position) -> None:
        item = self._meal_history_list.itemAt(position)
        target = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if target is None:
            return
        menu = QMenu(self)
        action = menu.addAction("선택한 변경 취소…")
        revision_id = self._roster.revision_id
        action.triggered.connect(lambda: self.meal_history_cancel_requested.emit(target, revision_id))
        menu.exec(self._meal_history_list.viewport().mapToGlobal(position))

    def _meal_history_rows_for(self, person: Person) -> tuple:
        rows = self._meal_change_history_rows.get(person.person_id)
        if rows is None:
            rows = self._meal_change_history_provider(person)
            self._meal_change_history_rows[person.person_id] = rows
        return rows

    def _fallback_meal_history_rows(self, person: Person) -> tuple[MealHistoryRow, ...]:
        history_events = [
            (meal, event)
            for meal in Meal
            for history in (self._histories.get((person.person_id, meal)),)
            for event in (history.events if history is not None else ())
        ]
        return tuple(MealHistoryRow(*meal_status_summary(event)) for _, event in sorted(
            history_events,
            key=lambda item: item[1].recorded_at,
            reverse=True,
        ))


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


def _status_display(status: MealStatus) -> str:
    return "중지" if status is MealStatus.STOPPED else status.value
