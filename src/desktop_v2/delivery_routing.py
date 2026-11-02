"""Driver registry, dated assignment and relocation dialogs."""

from __future__ import annotations

from .user_messages import user_error

from .dialog_workflow import DialogRequest, dialog_workflow, invoke

from .startup_loading import save_revision

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from src.domain.delivery_routing import (
    MEAL_ORDER,
    Driver,
    RoutingState,
    build_batch_assignment_revision,
    build_driver_replacement_revision,
    build_driver_revision,
    build_driver_order_revision,
    build_routing_revision,
    preview_driver_replacement,
    routing_state_from_snapshot,
)
from src.domain.roster import Meal, RosterSnapshot
from src.domain.daily_driver_order import build_daily_driver_order_revision
from src.storage.encrypted_database import RevisionConflictError

from .date_edit import WheelSafeDateEdit

from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts


def _style(dialog, title):
    register_application_fonts()
    dialog.setWindowTitle(title)
    dialog.setStyleSheet(application_stylesheet())


def _date_editor(day):
    editor = WheelSafeDateEdit(QDate(day.year, day.month, day.day))
    editor.setCalendarPopup(True)
    editor.setDisplayFormat("yyyy-MM-dd")
    return editor


def _driver_combo(state, selected):
    combo = QComboBox()
    combo.addItem("미지정", "")
    for driver in state.drivers:
        label = driver.name + (f" · {driver.telephone}" if driver.telephone else "")
        combo.addItem(label, str(driver.driver_id))
    index = combo.findData("" if selected is None else str(selected))
    combo.setCurrentIndex(max(0, index))
    return combo


def _chosen_driver(combo):
    raw = combo.currentData()
    if raw == "__unchanged__":
        raise ValueError("변경할 식종의 새 기사를 선택해주세요. 해제하려면 미지정을 선택하세요.")
    return UUID(raw) if raw else None


class DriverOrderTable(QTableWidget):
    """Move complete rows without changing the UUID stored on each item."""

    orderChanged = Signal()

    def __init__(self, rows, parent=None):
        super().__init__(rows, 2, parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)
        self._drag_source_row = None

    def startDrag(self, supported_actions):
        drag = QDrag(self)
        drag.setMimeData(self.mimeData(self.selectedItems()))
        self._drag_source_row = self.currentRow()
        try:
            drag.exec(Qt.DropAction.MoveAction)
        finally:
            self._drag_source_row = None

    def dragEnterEvent(self, event):
        if event.source() is self:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        super().dragMoveEvent(event)
        if event.source() is self:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if event.source() is not self:
            event.ignore()
            return
        point = event.position().toPoint()
        destination = self.rowAt(point.y())
        if destination < 0:
            destination = self.rowCount()
        elif point.y() > self.visualItemRect(self.item(destination, next(
            (column for column in range(self.columnCount()) if not self.isColumnHidden(column)), 0,
        ))).center().y():
            destination += 1
        source = self.currentRow() if self._drag_source_row is None else self._drag_source_row
        self.move_row(source, destination)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def move_row(self, source, destination):
        """Destination is an insertion boundary, including after the last row."""
        if source < 0 or source >= self.rowCount() or destination in (source, source + 1):
            return
        self.blockSignals(True)
        items = [self.takeItem(source, column) for column in range(self.columnCount())]
        self.removeRow(source)
        if destination > source:
            destination -= 1
        self.insertRow(destination)
        for column, item in enumerate(items):
            self.setItem(destination, column, item)
        self.selectRow(destination)
        self.blockSignals(False)
        self.itemSelectionChanged.emit()
        self.orderChanged.emit()


@dataclass(frozen=True)
class DriverOrderRequest:
    ordered_driver_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class DriverReplacementLaunch:
    source_driver: UUID | None


@dataclass(frozen=True)
class DailyDriverOrderLaunch:
    pass


class DriverRegistryDialog(QDialog):
    """Only name and phone are user-facing driver attributes."""

    def __init__(self, state: RoutingState, parent=None):
        super().__init__(parent)
        _style(self, "배송기사 관리")
        self.resize(620, 470)
        self._state = state
        self._saved_order = tuple(driver.driver_id for driver in state.drivers)
        self._request = None
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "기사를 등록하면 식종별 배송담당과 기사 필터에서 선택할 수 있습니다."
            )
        )
        guidance = QLabel("기사 행을 드래그해 기본 순서를 바꾼 뒤 ‘순서 저장’을 누르세요. 일일식수의 식종별 순서는 ‘일일식수 기사 순서’에서 정합니다.")
        guidance.setWordWrap(True)
        layout.addWidget(guidance)
        self.table = DriverOrderTable(len(state.drivers))
        self.table.setHorizontalHeaderLabels(("이름", "전화번호"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        for row, driver in enumerate(state.drivers):
            item = QTableWidgetItem(driver.name)
            item.setData(Qt.ItemDataRole.UserRole, driver.driver_id)
            self.table.setItem(row, 0, item)
            self.table.setItem(row, 1, QTableWidgetItem(driver.telephone))
        self.table.itemSelectionChanged.connect(self._select)
        layout.addWidget(self.table)
        order_row = QHBoxLayout()
        self.order_note = QLabel("저장된 기사 순서입니다.")
        self.order_button = QPushButton("순서 저장")
        self.order_button.setObjectName("primaryAction")
        self.order_button.setEnabled(False)
        self.order_button.clicked.connect(self._save_order)
        order_row.addWidget(self.order_note)
        order_row.addStretch(1)
        order_row.addWidget(self.order_button)
        layout.addLayout(order_row)
        self.table.orderChanged.connect(self._order_changed)
        self.daily_order_button = QPushButton("일일식수 기사 순서")
        self.daily_order_button.setObjectName("quietAction")
        self.daily_order_button.clicked.connect(self._daily_order)
        layout.addWidget(self.daily_order_button)
        self.name = QLineEdit()
        self.phone = QLineEdit()
        form = QFormLayout()
        form.addRow("이름", self.name)
        form.addRow("전화번호", self.phone)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        self.add_button = QPushButton("새 기사 등록")
        self.edit_button = QPushButton("선택 기사 수정")
        self.edit_button.setEnabled(False)
        self.replacement_button = QPushButton("배정 일괄 교체")
        self.replacement_button.setObjectName("quietAction")
        self.replacement_button.clicked.connect(self._replace_assignments)
        self.add_button.clicked.connect(lambda: self._save(False))
        self.edit_button.clicked.connect(lambda: self._save(True))
        close = QPushButton("닫기")
        close.clicked.connect(self.reject)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.edit_button)
        buttons.addWidget(self.replacement_button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.error = QLabel()
        layout.addWidget(self.error)

    def _daily_order(self):
        if self._order_pending():
            self.error.setText("변경한 기본 기사 순서를 먼저 저장한 뒤 일일식수 기사 순서를 열어주세요.")
            return
        self._request = DailyDriverOrderLaunch()
        self.accept()

    def _replace_assignments(self):
        if self._order_pending():
            self.error.setText("변경한 기사 순서를 먼저 저장한 뒤 일괄 교체를 열어주세요.")
            return
        row = self.table.currentRow()
        self._request = DriverReplacementLaunch(
            self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) if row >= 0 else None)
        self.accept()

    def _select(self):
        row = self.table.currentRow()
        self.edit_button.setEnabled(row >= 0)
        if row >= 0:
            driver = self._state._drivers_by_id[self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)]
            self.name.setText(driver.name)
            self.phone.setText(driver.telephone)

    def _save(self, edit):
        row = self.table.currentRow()
        if edit and row < 0:
            return
        driver_id = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) if edit else uuid4()
        try:
            driver = Driver(driver_id, self.name.text(), self.phone.text())
            if any(
                other.driver_id != driver_id
                and (other.name, other.telephone) == (driver.name, driver.telephone)
                for other in self._state.drivers
            ):
                raise ValueError(
                    "같은 이름과 전화번호의 기사가 이미 등록되어 있습니다."
                )
        except (TypeError, ValueError) as error:
            self.error.setText(user_error(error))
            return
        if edit and driver == self._state._drivers_by_id[driver_id] and not self._order_pending():
            self.error.setText("변경된 기사 정보가 없습니다.")
            return
        self._request = driver
        self.accept()

    def ordered_driver_ids(self):
        return tuple(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) for row in range(self.table.rowCount()))

    def _order_pending(self):
        return self.ordered_driver_ids() != self._saved_order

    def mark_saved(self):
        self._saved_order = self.ordered_driver_ids()
        self._order_changed()

    def _order_changed(self):
        pending = self._order_pending()
        self.order_button.setEnabled(pending)
        self.order_note.setText("변경한 순서를 저장해주세요." if pending else "저장된 기사 순서입니다.")

    def _save_order(self):
        if not self._order_pending():
            return
        self._request = DriverOrderRequest(self.ordered_driver_ids())
        self.accept()

    def reject(self):
        if self._order_pending():
            choice = QMessageBox.question(
                self, "기사 순서 저장", "변경한 기사 순서를 저장할까요?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if choice == QMessageBox.StandardButton.Save:
                self._save_order()
                return
            if choice != QMessageBox.StandardButton.Discard:
                return
        super().reject()

    def request(self):
        if self._request is None:
            raise RuntimeError("확정된 기사 정보가 없습니다.")
        return self._request


@dataclass(frozen=True)
class RoutingRequest:
    effective_on: date
    driver_ids: dict[Meal, UUID | None]
    road_address: str | None = None
    detail_address: str | None = None
    whole_household: bool = False
    jurisdiction: str | None = None


class RoutingChangeDialog(QDialog):
    def __init__(
        self,
        roster: RosterSnapshot,
        state: RoutingState,
        person_id: UUID,
        day: date,
        parent=None,
        *,
        meal: Meal | None = None,
    ):
        super().__init__(parent)
        self._meal = meal
        self._roster = roster
        self._state = state
        self._person_id = person_id
        self._display_day = day
        self._request = None
        person = roster.person(person_id)
        view = state.resolve(roster, person_id, day)
        _style(
            self,
            f"{person.profile.name} · "
            + ("이사 등록" if meal is None else "배송담당 변경"),
        )
        self.resize(540, 400 if meal is None else 230)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "적용일 당일부터 변경됩니다. 이전 날짜의 주소·관할동과 담당자는 유지됩니다."
            )
        )
        form = QFormLayout()
        self.day = _date_editor(day)
        form.addRow("적용일", self.day)
        self.road = QLineEdit(view.road_address or "")
        self.detail = QLineEdit(view.detail_address or "")
        self.jurisdiction = QLineEdit(view.jurisdiction or "")
        if meal is None:
            form.addRow("이사할 주소", self.road)
            form.addRow("상세주소", self.detail)
            form.addRow("관할동", self.jurisdiction)
        else:
            self.road.hide()
            self.detail.hide()
            self.jurisdiction.hide()
        self.drivers = {}
        self.meal_checks = {}
        for selected_meal in MEAL_ORDER:
            combo = _driver_combo(state, view.driver_ids[selected_meal])
            if view.driver_ids[selected_meal] is None and view.drivers[selected_meal]:
                combo.addItem(f"현재: {view.drivers[selected_meal]} (기존 배정)", "__unchanged__")
                combo.setCurrentIndex(combo.count() - 1)
            self.drivers[selected_meal] = combo
            if meal is None:
                form.addRow(f"{selected_meal.value} 배송담당", combo)
            else:
                check = QCheckBox(selected_meal.value)
                check.setChecked(selected_meal is meal)
                combo.setEnabled(check.isChecked())
                check.toggled.connect(combo.setEnabled)
                self.meal_checks[selected_meal] = check
                form.addRow(check, combo)
        layout.addLayout(form)
        self.whole_household = None
        self.household_targets = None
        if meal is None:
            self.whole_household = QCheckBox("같은 가구 구성원도 함께 이사")
            self.household_targets = QLabel()
            self.household_targets.setWordWrap(True)
            self.day.dateChanged.connect(self._refresh_household_targets)
            layout.addWidget(self.whole_household)
            layout.addWidget(self.household_targets)
            self._refresh_household_targets()
        layout.addWidget(QLabel("이후 날짜에 예약된 담당 변경은 해당 날짜부터 그대로 적용됩니다."))
        if not state.drivers:
            layout.addWidget(
                QLabel(
                    "등록된 기사가 없습니다. 명단의 ‘기사 관리’에서 먼저 등록해주세요."
                )
            )
        self.error = QLabel()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self):
        if self._meal is not None and not any(check.isChecked() for check in self.meal_checks.values()):
            self.error.setText("변경할 식종을 하나 이상 선택해주세요.")
            return
        road = self.road.text().strip() if self._meal is None else None
        if self._meal is None and not road:
            self.error.setText("이사할 주소를 입력해주세요.")
            return
        try:
            driver_ids = {
                selected: _chosen_driver(combo) for selected, combo in self.drivers.items()
                if (self._meal is None and combo.currentData() != "__unchanged__")
                or (self._meal is not None and self.meal_checks[selected].isChecked())
            }
        except ValueError as error:
            self.error.setText(user_error(error))
            return
        request = RoutingRequest(
            effective_on=self.day.date().toPython(),
            driver_ids=driver_ids,
            road_address=road,
            detail_address=(
                self.detail.text().strip() or None
                if self._meal is None
                else None
            ),
            whole_household=(
                self.whole_household.isChecked()
                if self.whole_household is not None
                else False
            ),
            jurisdiction=(
                self.jurisdiction.text().strip()
                if self._meal is None
                else None
            ),
        )
        if (
            request.effective_on == self._display_day
            and not self._changes_effective_values(request)
        ):
            self.error.setText("변경된 내용이 없습니다.")
            return
        self._request = request
        self.accept()

    def _changes_effective_values(self, request: RoutingRequest) -> bool:
        target_ids = (self._person_id,)
        if request.whole_household:
            membership = next(
                (
                    item
                    for item in self._roster.memberships
                    if item.person_id == self._person_id
                    and item.period.contains(request.effective_on)
                ),
                None,
            )
            if membership is not None:
                target_ids = tuple(
                    person.person_id
                    for person in self._roster.household_members(
                        membership.household_id, on=request.effective_on
                    )
                )
        for target_id in target_ids:
            view = self._state.resolve(
                self._roster, target_id, request.effective_on
            )
            if request.road_address is not None and (
                view.road_address != request.road_address
                or (view.detail_address or None) != request.detail_address
            ):
                return True
            if request.jurisdiction is not None and (
                view.jurisdiction or ""
            ) != request.jurisdiction:
                return True
            if any(
                view.driver_ids[meal] != driver_id
                or (driver_id is None and bool(view.drivers[meal]))
                for meal, driver_id in request.driver_ids.items()
            ):
                return True
        return False

    def _refresh_household_targets(self):
        if self.household_targets is None:
            return
        effective_on = self.day.date().toPython()
        membership = next(
            (
                item
                for item in self._roster.memberships
                if item.person_id == self._person_id and item.period.contains(effective_on)
            ),
            None,
        )
        if membership is None:
            self.whole_household.setEnabled(False)
            self.whole_household.setChecked(False)
            self.household_targets.setText("적용일 기준 저장된 가구 소속이 없어 본인만 이사합니다.")
            return
        members = self._roster.household_members(membership.household_id, on=effective_on)
        names = ", ".join(person.profile.name for person in members)
        self.whole_household.setEnabled(True)
        self.household_targets.setText(f"적용일 기준 함께 이사할 대상: {names}")

    def request(self):
        if self._request is None:
            raise RuntimeError("확정된 변경 내용이 없습니다.")
        return self._request


@dataclass(frozen=True)
class BatchAssignmentRequest:
    effective_on: date
    driver_id: UUID | None


class BatchRoutingChangeDialog(QDialog):
    def __init__(self, roster, state, targets, day, parent=None):
        super().__init__(parent)
        _style(self, "선택한 식종 배송담당 변경")
        self.resize(540, 360)
        self._request = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"선택한 {len({person_id for person_id, _ in targets})}명 · 식종 {len(targets)}개에 적용합니다."))
        selected = QTableWidget(len(targets), 2)
        selected.setHorizontalHeaderLabels(["성명", "식종"])
        selected.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        selected.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for row, (person_id, meal) in enumerate(targets):
            selected.setItem(row, 0, QTableWidgetItem(roster.person(person_id).profile.name))
            selected.setItem(row, 1, QTableWidgetItem(meal.value))
        layout.addWidget(selected)
        form = QFormLayout()
        self.day = _date_editor(day)
        self.driver = _driver_combo(state, None)
        self.driver.insertItem(0, "변경할 기사 선택", "__unchanged__")
        self.driver.setCurrentIndex(0)
        form.addRow("적용일", self.day)
        form.addRow("배송담당", self.driver)
        layout.addLayout(form)
        note = QLabel("선택한 식종 셀에만 같은 기사를 지정합니다. 미지정을 선택하면 배정을 해제합니다. 이전 날짜와 이후 예약된 변경은 유지됩니다.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.error = QLabel()
        self.error.setObjectName("errorMessage")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("quietAction")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self):
        try:
            driver_id = _chosen_driver(self.driver)
        except ValueError as error:
            self.error.setText(user_error(error))
            self.error.show()
            return
        self._request = BatchAssignmentRequest(self.day.date().toPython(), driver_id)
        self.accept()

    def request(self):
        if self._request is None:
            raise RuntimeError("확정된 배송담당 변경이 없습니다.")
        return self._request


@dataclass(frozen=True)
class DriverReplacementRequest:
    source_driver: UUID | str
    target_driver_id: UUID
    effective_on: date
    meals: tuple[Meal, ...]


class DriverReplacementDialog(QDialog):
    def __init__(self, roster, state, day, parent=None):
        super().__init__(parent)
        _style(self, "배송기사 일괄 교체")
        self.resize(740, 540)
        self._roster, self._state = roster, state
        self._request = None
        layout = QVBoxLayout(self)
        note = QLabel(
            "전체 명단에서 적용일 기준 기존 기사가 맡은 배정을 교체합니다. "
            "미가입 식종의 배정도 포함합니다. 이전 날짜와 이후 날짜의 변경 예약은 유지됩니다."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.day = _date_editor(day)
        self.source = QComboBox()
        self.target = _driver_combo(state, None)
        self.target.setItemText(0, "새 기사 선택")
        form.addRow("적용 시작일", self.day)
        form.addRow("기존 기사", self.source)
        form.addRow("새 기사", self.target)
        meal_row = QHBoxLayout()
        self.meal_checks = {}
        for meal in Meal:
            check = QCheckBox(meal.value)
            check.setChecked(True)
            self.meal_checks[meal] = check
            meal_row.addWidget(check)
            check.toggled.connect(self._refresh)
        form.addRow("변경할 식종", meal_row)
        layout.addLayout(form)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("성명", "식종", "기존 기사", "새 기사"))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("일괄 교체 저장")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.source.currentIndexChanged.connect(self._refresh)
        self.target.currentIndexChanged.connect(self._refresh)
        self.day.dateChanged.connect(self._refresh_sources)
        self._refresh_sources()

    def _refresh_sources(self):
        previous = self.source.currentData()
        self.source.blockSignals(True)
        self.source.clear()
        self.source.addItem("기존 기사 선택", None)
        for driver in self._state.drivers:
            label = driver.name + (f" · {driver.telephone}" if driver.telephone else "")
            self.source.addItem(label, driver.driver_id)
        legacy = set()
        for person in self._roster.persons:
            view = self._state.resolve(self._roster, person.person_id, self.day.date().toPython())
            legacy.update(view.drivers[meal] for meal in Meal
                          if view.driver_ids[meal] is None and view.drivers[meal])
        for name in sorted(legacy):
            self.source.addItem(f"{name} (명단에 입력된 이름)", name)
        index = self.source.findData(previous)
        self.source.setCurrentIndex(max(index, 0))
        self.source.blockSignals(False)
        self._refresh()

    def _selection(self):
        return DriverReplacementRequest(
            self.source.currentData(), _chosen_driver(self.target),
            self.day.date().toPython(),
            tuple(meal for meal, check in self.meal_checks.items() if check.isChecked()),
        )

    def _preview(self, request):
        return preview_driver_replacement(
            self._roster, self._state, source_driver=request.source_driver,
            target_driver_id=request.target_driver_id, effective_on=request.effective_on,
            meals=request.meals,
        )

    def _refresh(self):
        self.table.setRowCount(0)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
        request = self._selection()
        if request.source_driver is None or request.target_driver_id is None:
            self.summary.setText("기존 기사와 새 기사를 선택하면 변경 대상을 표시합니다.")
            return
        try:
            targets = self._preview(request)
        except (TypeError, ValueError) as error:
            self.summary.setText(user_error(error))
            return
        count = sum(len(target.meals) for target in targets)
        self.summary.setText(f"변경 대상 {len(targets):,}명 · 식종 배정 {count:,}건")
        self.table.setRowCount(len(targets))
        for row, target in enumerate(targets):
            values = (self._roster.person(target.person_id).profile.name,
                      ", ".join(meal.value for meal in target.meals),
                      self.source.currentText(), self.target.currentText())
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, column, item)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(bool(targets))

    def _save(self):
        request = self._selection()
        try:
            if not self._preview(request):
                raise ValueError("변경할 배정이 없습니다.")
        except (TypeError, ValueError) as error:
            self.summary.setText(user_error(error))
            return
        self._request = request
        self.accept()

    def request(self):
        if self._request is None:
            raise RuntimeError("확정된 기사 교체 내용이 없습니다.")
        return self._request


@dataclass(frozen=True)
class DeliveryRoutingOutcome:
    roster: RosterSnapshot | None = None
    routing_state: RoutingState | None = None


class DeliveryRoutingController:
    def __init__(
        self,
        session,
        *,
        registry_dialog_type=DriverRegistryDialog,
        change_dialog_type=RoutingChangeDialog,
        batch_change_dialog_type=BatchRoutingChangeDialog,
        replacement_dialog_type=DriverReplacementDialog,
        daily_order_dialog_type=None,
        message_box=QMessageBox,
        actor_provider=current_windows_actor,
    ):
        self._session = session
        self._registry_dialog_type = registry_dialog_type
        self._change_dialog_type = change_dialog_type
        self._batch_change_dialog_type = batch_change_dialog_type
        self._replacement_dialog_type = replacement_dialog_type
        if daily_order_dialog_type is None:
            from .daily_driver_order import DailyDriverOrderDialog
            daily_order_dialog_type = DailyDriverOrderDialog
        self._daily_order_dialog_type = daily_order_dialog_type
        self._message_box = message_box
        self._actor_provider = actor_provider

    def _load(self):
        roster = self._session.database.current_roster()
        if roster is None:
            raise ValueError("현재 명단이 없습니다.")
        state = routing_state_from_snapshot(
            self._session.database.snapshot(roster.revision_id)
        )
        return roster, state

    def _commit(self, record, parent):
        backup = save_revision(self._session, record, parent=parent)
        if backup.warning_required:
            self._message_box.warning(
                parent,
                "변경 내용은 저장됐지만 자동 백업에 실패했습니다",
                user_error(backup.error),
            )
        roster, state = self._load()
        return DeliveryRoutingOutcome(roster, state)

    @dialog_workflow
    def run_drivers(self, parent=None, *, on_saved=None, service_day=None):
        outcome = DeliveryRoutingOutcome()
        dialog = None
        while True:
            try:
                if dialog is None:
                    roster, state = self._load()
                    dialog = self._registry_dialog_type(state, parent)
                if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
                    return DeliveryRoutingOutcome(*self._load()) if outcome.roster is not None else outcome
                request = dialog.request()
                if isinstance(request, DailyDriverOrderLaunch):
                    result = yield from invoke(self.run_daily_order, parent=parent)
                    if result.roster is not None:
                        outcome = result
                        if on_saved is not None:
                            on_saved(outcome)
                    dialog = None
                    continue
                if isinstance(request, DriverReplacementLaunch):
                    replacement = yield from invoke(self.run_replacement, service_day or date.today(),
                                                    parent=parent, source_driver=request.source_driver)
                    if replacement.roster is not None:
                        outcome = replacement
                        if on_saved is not None:
                            on_saved(outcome)
                    dialog = None
                    continue
                metadata = dict(actor=self._actor_provider(), recorded_at=datetime.now(UTC), revision_id=uuid4())
                if isinstance(request, DriverOrderRequest):
                    record = build_driver_order_revision(roster, state, ordered_driver_ids=request.ordered_driver_ids, **metadata)
                else:
                    order = getattr(dialog, "ordered_driver_ids", lambda: None)()
                    if order is not None:
                        order = tuple(order)
                        if request.driver_id not in order:
                            order += (request.driver_id,)
                        if order == tuple(driver.driver_id for driver in state.drivers):
                            order = None
                    record = build_driver_revision(
                        roster, state, driver_id=request.driver_id, name=request.name,
                        telephone=request.telephone, ordered_driver_ids=order, **metadata,
                    )
                if record is None:
                    dialog = None
                    continue
                outcome = self._commit(record, parent)
                if isinstance(dialog, DriverRegistryDialog):
                    dialog.mark_saved()
                dialog = None
                if on_saved is not None:
                    on_saved(outcome)
            except Exception as error:  # noqa: BLE001 - report storage failures at the UI boundary
                message = (
                    "다른 창에서 운영 자료가 변경되어 저장하지 않았습니다. "
                    "입력한 내용과 순서는 이 창에 남아 있습니다. 필요한 내용을 확인한 뒤 "
                    "창을 닫고 변경 내용을 버린 다음, 기사 관리를 다시 열어 최신 자료에서 수정해주세요."
                    if isinstance(error, RevisionConflictError) else user_error(error)
                )
                self._message_box.critical(
                    parent, "배송기사를 저장하지 못했습니다", message
                )
                if isinstance(dialog, DriverRegistryDialog):
                    dialog.error.setText(f"저장하지 못했습니다: {message}")
                    dialog.error.setWordWrap(True)
                    dialog.setResult(QDialog.DialogCode.Rejected)
                    continue
                return outcome

    @dialog_workflow
    def run_daily_order(self, parent=None):
        dialog = None
        while True:
            try:
                if dialog is None:
                    roster, routing = self._load()
                    snapshot = self._session.database.snapshot(roster.revision_id)
                    dialog = self._daily_order_dialog_type(snapshot, routing, parent)
                if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
                    return DeliveryRoutingOutcome()
                record = build_daily_driver_order_revision(
                    roster, snapshot, routing, orders=dialog.request(),
                    actor=self._actor_provider(), recorded_at=datetime.now(UTC), revision_id=uuid4(),
                )
                if record is None:
                    return DeliveryRoutingOutcome()
                result = self._commit(record, parent)
                if hasattr(dialog, "mark_saved"):
                    dialog.mark_saved()
                return result
            except Exception as error:
                message = (
                    "다른 창에서 운영 자료가 변경되어 저장하지 않았습니다. "
                    "변경한 순서는 이 창에 남아 있습니다. 창을 닫고 변경 내용을 버린 뒤 다시 열어주세요."
                    if isinstance(error, RevisionConflictError) else user_error(error)
                )
                self._message_box.critical(parent, "일일식수 기사 순서를 저장하지 못했습니다", message)
                if dialog is None or not hasattr(dialog, "error"):
                    return DeliveryRoutingOutcome()
                dialog.error.setText(f"저장하지 못했습니다: {message}")
                dialog.setResult(QDialog.DialogCode.Rejected)

    @dialog_workflow
    def run_replacement(self, day, parent=None, *, source_driver=None):
        try:
            roster, state = self._load()
            dialog = self._replacement_dialog_type(roster, state, day, parent)
            if source_driver is not None and isinstance(dialog, DriverReplacementDialog):
                dialog.source.setCurrentIndex(max(0, dialog.source.findData(source_driver)))
            if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
                return DeliveryRoutingOutcome()
            request = dialog.request()
            record = build_driver_replacement_revision(
                roster, state, source_driver=request.source_driver,
                target_driver_id=request.target_driver_id, effective_on=request.effective_on,
                meals=request.meals, actor=self._actor_provider(),
                recorded_at=datetime.now(UTC), revision_id=uuid4(),
            )
            return self._commit(record, parent)
        except Exception as error:  # noqa: BLE001 - report storage failures at the UI boundary
            self._message_box.critical(parent, "배송기사를 교체하지 못했습니다", user_error(error))
            return DeliveryRoutingOutcome()

    @dialog_workflow
    def run_assignment(self, person_id, meal, day, parent=None):
        return (yield from invoke(self._run_change, person_id, day, parent, meal=meal))

    @dialog_workflow
    def run_batch_assignment(self, targets, day, parent=None):
        try:
            roster, state = self._load()
            targets = tuple(dict.fromkeys(targets))
            if not targets:
                raise ValueError("배송담당을 변경할 식종 셀을 선택해주세요.")
            dialog = self._batch_change_dialog_type(roster, state, targets, day, parent)
            if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
                return DeliveryRoutingOutcome()
            request = dialog.request()
            record = build_batch_assignment_revision(
                roster, state, targets=targets, driver_id=request.driver_id,
                effective_on=request.effective_on, actor=self._actor_provider(),
                recorded_at=datetime.now(UTC), revision_id=uuid4(),
            )
            return self._commit(record, parent) if record is not None else DeliveryRoutingOutcome()
        except Exception as error:
            self._message_box.critical(parent, "배송담당을 저장하지 못했습니다", user_error(error))
            return DeliveryRoutingOutcome()

    @dialog_workflow
    def run_relocation(self, person_id, day, parent=None):
        return (yield from invoke(self._run_change, person_id, day, parent))

    @dialog_workflow
    def _run_change(self, person_id, day, parent, *, meal=None):
        try:
            roster, state = self._load()
            dialog = self._change_dialog_type(
                roster, state, person_id, day, parent, meal=meal
            )
            if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
                return DeliveryRoutingOutcome()
            request = dialog.request()
            record = build_routing_revision(
                roster,
                state,
                person_id=person_id,
                effective_on=request.effective_on,
                driver_ids=request.driver_ids,
                road_address=request.road_address,
                detail_address=request.detail_address,
                jurisdiction=request.jurisdiction,
                whole_household=request.whole_household,
                actor=self._actor_provider(),
                recorded_at=datetime.now(UTC),
                revision_id=uuid4(),
                event_id=uuid4(),
            )
            return self._commit(record, parent)
        except Exception as error:  # noqa: BLE001 - report storage failures at the UI boundary
            self._message_box.critical(
                parent, "주소·배송담당을 저장하지 못했습니다", user_error(error)
            )
            return DeliveryRoutingOutcome()
