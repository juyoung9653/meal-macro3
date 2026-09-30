"""Date-spine roster workspace for the SQLite v2 desktop application."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta, timezone
from uuid import UUID

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
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPushButton,
    QSplitter,
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
from src.domain.status import MealStatus, StatusHistory
from src.services.inactive_roster import inactive_person_ids

from .column_layout import RosterColumnLayout
from .column_search import ColumnSearchHeader
from .inactive_roster import InactiveRosterSettings, InactiveThresholdDialog
from .panel_handle import PanelEdgeHandle

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
    "-": NOT_ENROLLED,
}
_MEAL_ORDER = (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH)


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
    meal_drivers: Mapping[Meal, str]
    meal_driver_ids: Mapping[Meal, UUID | None]
    road_address: str | None
    detail_address: str | None
    sequence: str


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


def _mobile_phone_display(roster: RosterSnapshot, person: Person) -> str:
    profile = person.profile
    mobile_phones = _phone_values(
        profile.mobile_phone,
        profile.custom_values,
        _additional_mobile_phone_definitions(roster.custom_fields),
    )
    telephones = _phone_values(
        profile.telephone,
        profile.custom_values,
        _additional_telephone_definitions(roster.custom_fields),
    )
    contacts = (
        *((f"핸드폰 {' / '.join(mobile_phones)}",) if mobile_phones else ()),
        *((f"전화 {' / '.join(telephones)}",) if telephones else ()),
    )
    return " · ".join(contacts) or "미등록"


def _inspector_contact_lines(roster: RosterSnapshot, person: Person) -> tuple[str, ...]:
    profile = person.profile
    mobile_phones = _phone_values(
        profile.mobile_phone,
        profile.custom_values,
        _additional_mobile_phone_definitions(roster.custom_fields),
    )
    telephones = _phone_values(
        profile.telephone,
        profile.custom_values,
        _additional_telephone_definitions(roster.custom_fields),
    )
    return (
        *(f"핸드폰 {number}" for number in mobile_phones),
        *(f"전화 {number}" for number in telephones),
    ) or ("미등록",)


def _phone_values(
    primary_phone: str | None,
    custom_values: Mapping[UUID, object],
    definitions: tuple[CustomFieldDefinition, ...],
) -> tuple[str, ...]:
    phones = [primary_phone] if primary_phone else []
    phones.extend(
        str(value)
        for definition in definitions
        if (value := custom_values.get(definition.definition_id)) is not None
        and str(value).strip()
    )
    return tuple(phones)


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
    ) -> None:
        super().__init__(parent)
        if not isinstance(roster, RosterSnapshot):
            raise TypeError("roster는 RosterSnapshot이어야 합니다.")
        self._roster = roster
        self._histories = dict(histories or {})
        self._routing_state = RoutingState() if routing_state is None else routing_state
        self._school_calendar = (
            SchoolLunchCalendar() if school_calendar is None else school_calendar
        )
        self._day = date.today()
        self._meal = Meal.LUNCH
        self._search = ""
        self._column_searches: dict[str, str] = {}
        self._column_values: dict[str, frozenset[str]] = {}
        self._active_only = False
        self._driver_filter: UUID | None = None
        self._birth_month: int | None = None
        # None preserves the historical all-person model for standalone users.
        self._archive_mode: bool | None = None
        self._archive_ids: frozenset[UUID] = frozenset()
        self._columns = self._build_columns()
        # Keep the model's established sequence ordering for non-visual consumers.
        # The table clears it while enabling header sorting, so its first click is
        # still ascending and the clearable indicator can restore roster order.
        self._sort_column: int | None = next(
            index
            for index, column in enumerate(self._columns)
            if column.key == "sequence"
        )
        self._sort_order = Qt.SortOrder.AscendingOrder
        self._people: tuple[Person, ...] = ()
        self._contexts: dict[UUID, _PersonContext] = {}
        self._rebuild_context_projection()
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
        self._rebuild_context_projection()
        self._refresh_people()
        self.endResetModel()

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
        values = {
            str(self._display_value(person, column, self._contexts[person.person_id]))
            for person in self._roster.persons
            if self._archive_mode is None
            or (person.person_id in self._archive_ids) == self._archive_mode
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

    def replace_roster(
        self,
        roster: RosterSnapshot,
        histories: Mapping[tuple[UUID, Meal], StatusHistory] | None = None,
    ) -> None:
        if not isinstance(roster, RosterSnapshot):
            raise TypeError("roster는 RosterSnapshot이어야 합니다.")
        self.beginResetModel()
        sort_key = (
            self._columns[self._sort_column].key
            if self._sort_column is not None
            else None
        )
        self._roster = roster
        self._histories = dict(histories or {})
        self._columns = self._build_columns()
        keys = {column.key for column in self._columns}
        self._column_searches = {
            key: value for key, value in self._column_searches.items() if key in keys
        }
        self._column_values = {
            key: value for key, value in self._column_values.items() if key in keys
        }
        if sort_key is not None:
            self._sort_column = next(
                (
                    index
                    for index, column in enumerate(self._columns)
                    if column.key == sort_key
                ),
                None,
            )
        self._rebuild_context_projection()
        self._refresh_people()
        self.endResetModel()

    def sort(
        self,
        column: int,
        order: Qt.SortOrder = Qt.SortOrder.AscendingOrder,
    ) -> None:
        if column == -1:
            self.beginResetModel()
            self._sort_column = None
            self._refresh_people()
            self.endResetModel()
            return
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
            return QColor(_STATUS_COLORS[self._status_for_column(column, context)])
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
                return f"{value or '순서 미지정'}\n우클릭 → 순서 수정"
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
            _Column("jurisdiction", "관할동", 82),
            _Column("address", "주소", 220),
            _Column("sequence", "순서", 104),
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
        return (
            *base_before_contacts,
            *contact_columns,
            *base_after_contacts,
            *custom,
        )

    def _refresh_people(self) -> None:
        people: list[Person] = []
        filters = [(column, self._column_searches[column.key])
                   for column in self._columns if column.key in self._column_searches]
        value_filters = [(column, self._column_values[column.key])
                         for column in self._columns if column.key in self._column_values]
        birth_month = date.today().month if self._birth_month == 0 else self._birth_month
        for person in self._roster.persons:
            if self._archive_mode is not None and (
                (person.person_id in self._archive_ids) != self._archive_mode
            ):
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
                str(self._display_value(person, column, context)) not in selected
                for column, selected in value_filters
            ):
                continue
            people.append(person)
        if self._sort_column is None:
            self._people = tuple(people)
            return
        reverse = self._sort_order == Qt.SortOrder.DescendingOrder
        self._people = tuple(sorted(people, key=self._sort_key, reverse=reverse))

    def _rebuild_context_projection(self) -> None:
        """Resolve day-and-meal lookups once, outside the table paint path."""

        contexts: dict[UUID, _PersonContext] = {}
        for person in self._roster.persons:
            person_id = person.person_id
            meals = self._roster.meals_for(person_id, on=self._day)
            meal_statuses = {
                meal: self._resolve_status(person_id, meal, meals)
                for meal in Meal
            }
            delivery_view = self._routing_state.resolve(self._roster, person_id, self._day)
            sequences = {
                meal: self._roster.resolve_assignment(person_id, meal).sequence or ""
                for meal in _MEAL_ORDER
            }
            nonempty_sequences = {
                sequence for sequence in sequences.values() if sequence
            }
            if len(nonempty_sequences) <= 1:
                sequence = next(iter(nonempty_sequences), "")
            else:
                sequence = "\n".join(
                    f"{meal.value}: {sequences[meal]}"
                    for meal in _MEAL_ORDER
                    if sequences[meal]
                )
            contexts[person_id] = _PersonContext(
                meals=meals,
                status=meal_statuses[self._meal],
                meal_statuses=meal_statuses,
                meal_drivers=delivery_view.drivers,
                meal_driver_ids=delivery_view.driver_ids,
                road_address=delivery_view.road_address,
                detail_address=delivery_view.detail_address,
                sequence=sequence,
            )
        self._contexts = contexts

    def _sort_key(self, person: Person) -> tuple[object, ...]:
        assert self._sort_column is not None
        column = self._columns[self._sort_column]
        context = self._contexts[person.person_id]
        if column.meal is not None:
            status = self._status_for_column(column, context)
            driver = context.meal_drivers[column.meal].casefold()
            return (status.casefold(), driver, person.profile.name.casefold(), str(person.person_id))
        value = self._display_value(
            person,
            column,
            context,
        )
        text = "" if value is None else str(value).strip()
        if column.key == "sequence" and "\n" in text:
            text = self._roster.resolve_assignment(person.person_id, self._meal).sequence or ""
        if not text:
            normalized: tuple[int, object] = (2, "")
        elif text.isdigit():
            normalized = (0, int(text))
        else:
            normalized = (1, text.casefold())
        return (*normalized, person.profile.name.casefold(), str(person.person_id))

    def _resolve_status(
        self,
        person_id: UUID,
        meal: Meal,
        meals: tuple[Meal, ...],
    ) -> str:
        if meal not in meals:
            return "-"
        if meal is Meal.LUNCH:
            try:
                if not self._school_calendar.vendor_service_available(
                    self._roster.person(person_id).profile,
                    self._day,
                ):
                    return "-"
            except SchoolLunchScheduleMissingError:
                return "-"
        history = self._histories.get((person_id, meal))
        return (
            MealStatus.DELIVERY.value
            if history is None
            else history.status_on(self._day).value
        )

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
                profile.telephone or "",
                profile.mobile_phone or "",
                context.road_address or "",
                context.detail_address or "",
                profile.jurisdiction or "",
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
        if column.meal is not None:
            status = self._status_for_column(column, context)
            display_status = "미배송" if status == MealStatus.STOPPED.value else status
            return f"{context.meal_drivers[column.meal] or '미지정'}\n{display_status}"
        if column.key == "name":
            return profile.name
        if column.key == "birth_date":
            return "" if profile.birth_date is None else profile.birth_date.isoformat()
        if column.key == "school":
            return profile.school_name or ""
        if column.key == "grade":
            return profile.grade or ""
        if column.key == "telephone":
            return profile.telephone or ""
        if column.key == "mobile":
            return profile.mobile_phone or ""
        if column.key == "jurisdiction":
            return profile.jurisdiction or ""
        if column.key == "address":
            return " ".join(
                value for value in (context.road_address, context.detail_address) if value
            )
        if column.key == "sequence":
            return context.sequence
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


class RosterWindow(QMainWindow):
    """Operational shell; persistence is supplied by a future application controller."""

    status_action_requested = Signal(object, object, object)
    batch_status_action_requested = Signal(object, object)
    drivers_requested = Signal()
    driver_assignment_requested = Signal(object, object)
    sequence_edit_requested = Signal(object)
    driver_replacement_requested = Signal()
    relocation_requested = Signal(object)
    complaint_requested = Signal(object, object)
    database_export_requested = Signal()
    import_review_requested = Signal()
    person_edit_requested = Signal(object)
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

    def __init__(
        self,
        roster: RosterSnapshot,
        histories: Mapping[tuple[UUID, Meal], StatusHistory] | None = None,
        parent: QWidget | None = None,
        *,
        revision_display_code: str | None = None,
        column_settings=None,
        inactive_settings: QSettings | None = None,
        routing_state: RoutingState | None = None,
        school_calendar: SchoolLunchCalendar | None = None,
    ) -> None:
        super().__init__(parent)
        self._column_settings = column_settings
        self._inactive_settings = inactive_settings
        self._archive_settings = InactiveRosterSettings(inactive_settings)
        self._roster = roster
        self._histories = dict(histories or {})
        self._routing_state = RoutingState() if routing_state is None else routing_state
        self._school_calendar = (
            SchoolLunchCalendar() if school_calendar is None else school_calendar
        )
        self._revision_display_code = revision_display_code
        self._model = RosterTableModel(
            roster,
            self._histories,
            self,
            routing_state=self._routing_state,
            school_calendar=self._school_calendar,
        )
        self._meal = Meal.LUNCH
        self._history_dialogs: list[QDialog] = []
        self._history_provider = None
        self._meal_change_history_provider: (
            Callable[[Person], tuple[tuple[str, str], ...]] | None
        ) = None
        register_application_fonts()
        self.setWindowTitle("급식 배송 운영")
        self.resize(1440, 860)
        self.setMinimumSize(1080, 680)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()
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
        self._column_layout.save()
        self._model.replace_roster(roster, self._histories)
        self._refresh_archive()
        self._revision_label.setText(self._revision_label_text())
        self._column_layout.replace_columns(self._model.columns)
        self._column_search_header.replace_columns(self._model.columns)
        self._after_model_reset()

    def set_routing_state(self, state: RoutingState) -> None:
        if not isinstance(state, RoutingState):
            raise TypeError("state는 RoutingState여야 합니다.")
        self._routing_state = state
        selected_driver_id = self._driver_filter.currentData()
        self._model.set_routing_state(state)
        self._refresh_driver_filter(selected_driver_id)
        self._after_model_reset()

    def set_school_calendar(self, calendar: SchoolLunchCalendar) -> None:
        if not isinstance(calendar, SchoolLunchCalendar):
            raise TypeError("calendar는 SchoolLunchCalendar여야 합니다.")
        self._school_calendar = calendar
        self._model.set_school_calendar(calendar)
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
        count_label = QLabel("선택 명단")
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
            "청구서·확인서·배송 리포트·배송대타를 한 곳에서 선택합니다"
        )
        self._documents_button.clicked.connect(self.documents_requested.emit)
        heading.addWidget(self._documents_button)
        self._more_button = QPushButton("더보기")
        self._more_button.setObjectName("quietAction")
        self._more_button.setToolTip("명단 가져오기·내보내기, 기사 관리, 운영 설정")
        self._more_menu = self._build_more_menu()
        self._more_button.setMenu(self._more_menu)
        heading.addWidget(self._more_button)
        layout.addLayout(heading)

        filters = QHBoxLayout()
        filters.setSpacing(9)
        self._search_input = QLineEdit(self)
        self._search_input.setPlaceholderText("성명, 학교, 전화, 주소 검색")
        self._search_input.setAccessibleName("전체 명단 검색")
        self._search_input.textChanged.connect(self._on_search_changed)
        filters.addWidget(self._search_input, 1)
        filters.addStretch(1)
        self._driver_filter = QComboBox()
        self._driver_filter.setMinimumWidth(140)
        self._driver_filter.setMaximumWidth(230)
        self._driver_filter.setToolTip("전체 식종 중 선택한 기사가 담당하는 대상자를 표시합니다")
        self._driver_filter.currentIndexChanged.connect(self._on_driver_filter_changed)
        filters.addWidget(self._driver_filter)
        self._refresh_driver_filter()
        self._driver_households_button = QPushButton("기사별 가구")
        self._driver_households_button.setObjectName("quietAction")
        self._driver_households_button.setToolTip(
            "선택일에 배송 상태인 모든 식종의 담당 가구를 기사별로 봅니다"
        )
        self._driver_households_button.clicked.connect(self._show_driver_households)
        filters.addWidget(self._driver_households_button)
        self._add_person_button = QPushButton("행 추가")
        self._add_person_button.setObjectName("quietAction")
        self._add_person_button.setToolTip("현재 식종에 가입한 대상자를 명단에 추가합니다")
        self._add_person_button.clicked.connect(lambda: self.person_add_requested.emit(self._model.meal))
        filters.addWidget(self._add_person_button)
        layout.addLayout(filters)
        return frame

    def _refresh_archive(self) -> None:
        ids = inactive_person_ids(
            self._roster, self._histories,
            days=self._archive_settings.days,
        )
        self._model.set_archive_filter(ids, self._archive_action.isChecked())
        self._roster_title.setText(
            f"장기 미배송 보관함 ({len(ids)}명)" if self._archive_action.isChecked()
            else "대상자 명단"
        )
        self._after_model_reset()

    def _show_archive_threshold(self) -> None:
        dialog = InactiveThresholdDialog(self._archive_settings.days, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
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
        groups = (
            ("명단", (
                ("_import_action", "Excel 명단 가져오기", self.import_review_requested.emit,
                 "신규 명단을 가져오거나 기존 명단을 업데이트합니다"),
                ("_add_person_action", "명단 행 추가", lambda: self.person_add_requested.emit(self._model.meal),
                 "새 대상자와 가입 식종을 명단에 추가합니다"),
                ("_database_export_action", "Excel 내보내기", self.database_export_requested.emit,
                 "전체 명단·민원·정보 및 식종 변경 이력을 Excel로 저장합니다"),
            )),
            ("기사", (
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
                ("_columns_action", "칼럼 설정…", self._show_column_menu,
                 "칼럼 표시·숨기기, 기본 배치 복원, 사용자 항목 추가·삭제"),
                ("_settings_action", "설정", self.settings_requested.emit,
                 "생성 Excel 비밀번호와 문서 저장 폴더를 관리합니다"),
            )),
        )
        for label, entries in groups:
            menu.addSection(label)
            for attribute, text, callback, tooltip in entries:
                action = menu.addAction(text)
                action.setToolTip(tooltip)
                action.triggered.connect(callback)
                setattr(self, attribute, action)
            if label == "화면·설정":
                self._archive_action = menu.addAction("장기 미배송 보관함")
                self._archive_action.setCheckable(True)
                self._archive_action.toggled.connect(self._refresh_archive)
                self._archive_threshold_action = menu.addAction("보관 기준 설정…")
                self._archive_threshold_action.triggered.connect(self._show_archive_threshold)
        return menu

    def _show_driver_households(self) -> None:
        from src.services.driver_households import build_driver_household_rows
        from .driver_households import DriverHouseholdsDialog

        included_meals = {
            person.person_id: tuple(
                meal for meal, status in self._model.person_delivery_view(person).meal_statuses.items()
                if status == MealStatus.DELIVERY.value
            )
            for person in self._roster.persons
        }
        rows = build_driver_household_rows(
            self._roster, self._routing_state, self._model.service_day,
            included_meals=included_meals,
        )
        dialog = DriverHouseholdsDialog(
            rows,
            service_day=self._model.service_day,
            drivers=self._routing_state.drivers,
            selected_driver_id=self._driver_filter.currentData(),
            parent=self,
        )
        dialog.exec()
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
        )
        self._table.setHorizontalHeader(self._column_search_header)
        self._column_search_header.replace_columns(self._model.columns)
        self._column_search_header.search_changed.connect(self._on_column_search_changed)
        self._column_search_header.values_changed.connect(self._on_column_values_changed)
        self._table.horizontalScrollBar().valueChanged.connect(
            self._column_search_header.update_positions
        )
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
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
        header.setSortIndicatorClearable(True)
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        header.setToolTip(
            "열 이름 클릭: 오름차순 · 내림차순 · 정렬 해제 · ▾: 검색과 값 필터 · 드래그: 칼럼 이동 · 우클릭: 칼럼 설정"
        )
        self._table.setSortingEnabled(True)
        self._column_layout = RosterColumnLayout(
            self._table, self._model.columns, settings=self._column_settings, parent=self
        )
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(
            lambda position: self._show_column_menu(header.mapToGlobal(position))
        )
        layout.addWidget(self._table)
        return frame

    def _show_column_menu(self, position=None) -> None:
        menu = self._column_layout.create_menu(self)
        menu.addSeparator()
        manage = menu.addAction("사용자 항목 추가·삭제…")
        manage.triggered.connect(self.custom_fields_requested.emit)
        if position is None or isinstance(position, bool):
            position = self._more_button.mapToGlobal(self._more_button.rect().bottomLeft())
        menu.exec(position)

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
        self._inspector_detail = QLabel("연락처와 주소, 변경 이력이 여기에 표시됩니다.")
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
        history_label = QLabel("식종 변경 이력")
        history_label.setObjectName("sectionLabel")
        layout.addWidget(history_label)
        self._meal_history_list = QListWidget()
        self._meal_history_list.setObjectName("mealHistory")
        self._meal_history_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._meal_history_list.setUniformItemSizes(True)
        self._meal_history_list.setSpacing(4)
        self._meal_history_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._meal_history_list.setStyleSheet("QListWidget::item { height: 20px; }")
        self._meal_history_list.setFixedHeight(102)
        layout.addWidget(self._meal_history_list)
        guide = QLabel("모든 셀 우클릭: 이력보기·민원\n식종 셀 우클릭: 배송 상태 변경\nCtrl·Shift로 여러 식종 셀 선택 후 우클릭하면 일괄 변경할 수 있습니다.")
        guide.setObjectName("muted")
        guide.setWordWrap(True)
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
            if column_key == "address":
                relocation = QAction("이사 등록", menu)
                relocation.triggered.connect(lambda: self.relocation_requested.emit(person.person_id))
                menu.addAction(relocation)
            self._add_person_context_actions(menu, person, None)
            return menu
        for label, status in (
            ("배송", MealStatus.DELIVERY),
            ("일시중지", MealStatus.PAUSED),
            ("미배송", MealStatus.STOPPED),
        ):
            action = QAction(f"{meal.value} {label}", menu)
            action.triggered.connect(
                lambda _checked=False, selected=status: self.status_action_requested.emit(
                    person.person_id,
                    meal,
                    selected,
                )
            )
            menu.addAction(action)
        assignment = QAction("배송담당 변경", menu)
        assignment.triggered.connect(
            lambda: self.driver_assignment_requested.emit(person.person_id, meal)
        )
        menu.addAction(assignment)
        self._add_person_context_actions(menu, person, meal)
        if len(batch_targets) > 1:
            batch = QMenu(f"선택한 식종 {len(batch_targets)}개 일괄 변경", menu)
            menu._batch_status_menu = batch
            menu.addMenu(batch)
            for label, status in (
                ("배송", MealStatus.DELIVERY),
                ("일시중지", MealStatus.PAUSED),
                ("미배송", MealStatus.STOPPED),
            ):
                action = batch.addAction(label)
                action.triggered.connect(
                    lambda _checked=False, selected=status, selected_targets=batch_targets:
                    self.batch_status_action_requested.emit(selected_targets, selected)
                )
        return menu

    def _add_person_context_actions(
        self, menu: QMenu, person: Person, meal: Meal | None
    ) -> None:
        """Keep history and complaint actions available on every person cell."""
        history = QAction("이력보기", menu)
        history.triggered.connect(lambda: self._show_history(person, meal))
        menu.addAction(history)
        complaint = QAction("민원", menu)
        complaint.triggered.connect(
            lambda: self.complaint_requested.emit(person.person_id, meal)
        )
        menu.addAction(complaint)
        menu.addSeparator()
        delete = QAction("명단 행 삭제…", menu)
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
        provider: Callable[[Person], tuple[tuple[str, str], ...]] | None,
    ) -> None:
        self._meal_change_history_provider = provider
        self._update_inspector()

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
        dialog.show()

    def _set_day(self, day: date) -> None:
        self._date_edit.blockSignals(True)
        self._date_edit.setDate(QDate(day.year, day.month, day.day))
        self._date_edit.blockSignals(False)
        self._date_label.setText(day.strftime("%Y.%m.%d"))
        self._weekday_label.setText(f"{_WEEKDAYS[day.weekday()]} · 조회 기준일")
        self._model.set_context(day, self._meal)
        self._after_model_reset()

    def _set_meal(self, meal: Meal) -> None:
        self._meal = meal
        self._model.set_context(self._model.service_day, meal)
        self._after_model_reset()

    def _on_birth_month_changed(self) -> None:
        self._model.set_birth_month(self._birth_month_filter.currentData())
        self._after_model_reset()

    def _shift_day(self, offset: int) -> None:
        shifted = date.fromordinal(self._model.service_day.toordinal() + offset)
        self._set_day(shifted)

    def _on_qdate_changed(self, value: QDate) -> None:
        self._set_day(date(value.year(), value.month(), value.day()))

    def _on_column_values_changed(self, key: str, values) -> None:
        self._model.set_column_values(key, values)
        self._after_model_reset()

    def _on_column_search_changed(self, key: str, text: str) -> None:
        self._model.set_column_search(key, text)
        self._after_model_reset()

    def _on_search_changed(self, text: str) -> None:
        self._model.set_search(text)
        self._after_model_reset()

    def _refresh_driver_filter(self, selected_driver_id: UUID | None = None) -> None:
        if selected_driver_id is None:
            selected_driver_id = self._driver_filter.currentData()
        self._driver_filter.blockSignals(True)
        self._driver_filter.clear()
        self._driver_filter.addItem("전체 기사", None)
        for driver in self._routing_state.drivers:
            detail = f"{driver.name} · {driver.telephone}" if driver.telephone else driver.name
            self._driver_filter.addItem(detail, driver.driver_id)
        selected_index = self._driver_filter.findData(selected_driver_id)
        self._driver_filter.setCurrentIndex(max(selected_index, 0))
        self._driver_filter.blockSignals(False)
        self._model.set_driver_filter(self._driver_filter.currentData())

    def _on_driver_filter_changed(self) -> None:
        self._model.set_driver_filter(self._driver_filter.currentData())
        self._after_model_reset()

    def _set_active_only(self, active_only: bool) -> None:
        self._model.set_active_only(active_only)
        self._after_model_reset()

    def _after_model_reset(self) -> None:
        self._table.clearSelection()
        self._clear_inspector()
        self._count_metric.setText(f"{self._model.rowCount()}명")
        for meal, counts in self._model.meal_status_counts().items():
            self._meal_summary_widgets[meal].setText(
                f"{meal.value}  배송 {counts[MealStatus.DELIVERY.value]} · "
                f"일시중지 {counts[MealStatus.PAUSED.value]}"
            )

    def _update_inspector(self) -> None:
        index = self._selected_index()
        if not index.isValid():
            self._clear_inspector()
            return
        person = self._model.person_at(index.row())
        self._edit_person_button.setEnabled(True)
        profile = person.profile
        delivery_view = self._model.person_delivery_view(person)
        self._inspector_name.setText(profile.name)
        address = " ".join(
            value
            for value in (delivery_view.road_address, delivery_view.detail_address)
            if value
        )
        details = [
            "[연락처]",
            *_inspector_contact_lines(self._roster, person),
            "[주소]",
            address or "미등록",
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
            self._household_list.addItem(f"{member.profile.name}   {meals or '-'}")
        if not members:
            self._household_list.addItem("연결된 가구가 없습니다")
        self._populate_meal_history(person)

    def _request_selected_person_edit(self) -> None:
        index = self._selected_index()
        if not index.isValid():
            return
        person = self._model.person_at(index.row())
        self.person_edit_requested.emit(person.person_id)

    def _selected_index(self) -> QModelIndex:
        selection_model = self._table.selectionModel()
        current = selection_model.currentIndex()
        if current.isValid() and selection_model.isSelected(current):
            return current
        selected = selection_model.selectedIndexes()
        return selected[0] if selected else QModelIndex()

    def _clear_inspector(self) -> None:
        self._edit_person_button.setEnabled(False)
        self._inspector_name.setText("명단에서 한 사람을 선택하세요")
        self._inspector_detail.setText("연락처와 주소, 변경 이력이 여기에 표시됩니다.")
        self._household_list.clear()
        self._meal_history_list.clear()

    def _populate_meal_history(self, person: Person) -> None:
        self._meal_history_list.clear()
        rows = (
            self._meal_change_history_provider(person)
            if self._meal_change_history_provider is not None
            else self._fallback_meal_history_rows(person)
        )
        for text, tooltip in rows:
            item = QListWidgetItem(text)
            item.setSizeHint(QSize(0, 20))
            item.setToolTip(tooltip)
            self._meal_history_list.addItem(item)
        if not rows:
            item = QListWidgetItem("기록된 식종 변경 이력이 없습니다")
            item.setSizeHint(QSize(0, 20))
            self._meal_history_list.addItem(item)
        self._meal_history_list.doItemsLayout()
        last_visible_row = min(4, self._meal_history_list.count() - 1)
        visible_rect = self._meal_history_list.visualItemRect(
            self._meal_history_list.item(last_visible_row),
        )
        self._meal_history_list.setFixedHeight(
            visible_rect.bottom() + 1 + 2 * self._meal_history_list.frameWidth(),
        )

    def _fallback_meal_history_rows(self, person: Person) -> tuple[tuple[str, str], ...]:
        history_events = [
            (meal, event)
            for meal in Meal
            for history in (self._histories.get((person.person_id, meal)),)
            for event in (history.events if history is not None else ())
        ]
        rows = []
        for meal, event in sorted(
            history_events,
            key=lambda item: item[1].recorded_at,
            reverse=True,
        ):
            recorded_at = event.recorded_at.astimezone(
                timezone(timedelta(hours=9)),
            )
            description = event.source_text or event.status.value
            text = f"{recorded_at:%Y-%m-%d %H:%M}  {meal.value} · {description}"
            rows.append((text, text))
        return tuple(rows)


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
    return "미배송" if status is MealStatus.STOPPED else status.value
