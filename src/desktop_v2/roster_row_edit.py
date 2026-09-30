"""Add and delete one roster person through the revision ledger."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtCore import QDate
from PySide6.QtWidgets import QCheckBox, QComboBox, QDateEdit, QDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget

from src.domain.delivery_routing import Driver, RoutingState, routing_state_from_snapshot
from src.domain.roster import CustomFieldDefinition, CustomFieldType, DeliveryAssignment, Household, Meal, Person, PersonProfile, RosterSnapshot
from src.domain.roster_row_changes import build_roster_row_revision
from src.domain.revisions import RevisionActor
from src.domain.status import MealStatus
from src.storage import AutomaticBackupResult, DatabaseSession

from .import_controller import current_windows_actor
from .person_edit import _FIELD_LABELS, _custom_value, _parse_optional_date
from .theme import application_stylesheet, register_application_fonts


@dataclass(frozen=True, slots=True)
class RosterRowCreateRequest:
    profile: PersonProfile
    meals: tuple[Meal, ...]
    starts_on: date
    assignments: dict[Meal, tuple[UUID | str | None, str | None]]
    household_id: UUID | None
    statuses: dict[Meal, tuple[MealStatus, date | None]]


class RosterRowCreateDialog(QDialog):
    """Collect a new person's profile and at least one enrolled meal."""

    def __init__(self, selected_meal: Meal = Meal.LUNCH, start_date: date | None = None, parent: QWidget | None = None, *, custom_fields: tuple[CustomFieldDefinition, ...] = (), drivers: tuple[Driver, ...] = (), households: tuple[Household, ...] = (), household_labels: dict[UUID, str] | None = None) -> None:
        super().__init__(parent)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("명단 행 추가")
        self.resize(760, 640)
        self.setMinimumSize(650, 580)
        self._custom_fields = custom_fields
        self._editors: dict[str, QLineEdit | QPlainTextEdit | QComboBox] = {}
        root = QVBoxLayout(self)
        title = QLabel("대상자 추가")
        title.setObjectName("workspaceTitle")
        root.addWidget(title)
        hint = QLabel("명단의 인적사항, 가입 식종, 배송담당과 순서를 한 번에 입력합니다. 배송 시작일 전에는 배송 대상에 포함되지 않습니다.")
        hint.setObjectName("workspaceSubtitle")
        hint.setWordWrap(True)
        root.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        for title_text, keys in (
            ("기본·학교", ("name", "birth_date", "school_type", "school_name", "grade", "class_name")),
            ("가구·기관", ("household_head_name", "relationship_to_child", "jurisdiction", "provider")),
            ("연락처·주소", ("mobile_phone", "telephone", "road_address", "detail_address")),
        ):
            frame = QFrame()
            frame.setObjectName("editSection")
            section = QVBoxLayout(frame)
            label = QLabel(title_text)
            label.setObjectName("sectionLabel")
            section.addWidget(label)
            grid = QGridLayout()
            for index, key in enumerate(keys):
                field_label = QLabel(_FIELD_LABELS.get(key, {"class_name": "반", "provider": "제공기관"}.get(key, key)) + (" *" if key == "name" else ""))
                editor = QLineEdit()
                editor.setObjectName(f"newPerson_{key}")
                editor.setClearButtonEnabled(True)
                if key == "birth_date":
                    editor.setPlaceholderText("YYYY-MM-DD 또는 YYYYMMDD")
                self._editors[key] = editor
                row, column = divmod(index, 2)
                grid.addWidget(field_label, row * 2, column)
                grid.addWidget(editor, row * 2 + 1, column)
            section.addLayout(grid)
            body_layout.addWidget(frame)
        note_frame = QFrame()
        note_frame.setObjectName("editSection")
        note_layout = QVBoxLayout(note_frame)
        note_layout.addWidget(QLabel("비고"))
        note = QPlainTextEdit()
        note.setObjectName("newPerson_note")
        note.setMaximumHeight(100)
        self._editors["note"] = note
        note_layout.addWidget(note)
        body_layout.addWidget(note_frame)
        if custom_fields:
            custom_frame = QFrame()
            custom_frame.setObjectName("editSection")
            custom_form = QFormLayout(custom_frame)
            custom_form.addRow(QLabel("추가 항목"))
            for definition in sorted(custom_fields, key=lambda item: item.display_order):
                key = f"custom:{definition.definition_id}"
                if definition.field_type is CustomFieldType.CHOICE:
                    editor = QComboBox()
                    editor.addItem("미입력", "")
                    for choice in definition.choices:
                        editor.addItem(choice, choice)
                else:
                    editor = QLineEdit()
                    if definition.field_type is CustomFieldType.DATE:
                        editor.setPlaceholderText("YYYY-MM-DD")
                editor.setObjectName(f"newPerson_{key}")
                self._editors[key] = editor
                custom_form.addRow(definition.name, editor)
            body_layout.addWidget(custom_frame)
        delivery_frame = QFrame()
        delivery_frame.setObjectName("editSection")
        delivery_form = QFormLayout(delivery_frame)
        delivery_form.addRow(QLabel("가입·배송"))
        self._meal_checks = {}
        meals_row = QHBoxLayout()
        for meal in (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH):
            check = QCheckBox(meal.value)
            check.setChecked(meal is selected_meal)
            self._meal_checks[meal] = check
            meals_row.addWidget(check)
        delivery_form.addRow("가입 식종 *", meals_row)
        day = start_date or date.today()
        self.start_date_edit = QDateEdit(QDate(day.year, day.month, day.day))
        self.start_date_edit.setObjectName("deliveryStartDate")
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setDisplayFormat("yyyy-MM-dd")
        delivery_form.addRow("배송 시작일 *", self.start_date_edit)
        self.household_combo = QComboBox()
        self.household_combo.setObjectName("newPerson_household")
        self.household_combo.addItem("가구 연결 없음", None)
        for household in households:
            self.household_combo.addItem((household_labels or {}).get(household.household_id, "구성원 없는 가구"), household.household_id)
        delivery_form.addRow("기존 가구", self.household_combo)
        self._driver_combos: dict[Meal, QComboBox] = {}
        self._legacy_driver_edits: dict[Meal, QLineEdit] = {}
        self._sequence_edits: dict[Meal, QLineEdit] = {}
        self._status_combos: dict[Meal, QComboBox] = {}
        self._pause_end_edits: dict[Meal, QLineEdit] = {}
        name_counts = {driver.name: sum(other.name == driver.name for other in drivers) for driver in drivers}
        for meal in (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH):
            row = QHBoxLayout()
            driver_combo = QComboBox()
            driver_combo.setObjectName(f"newPerson_driver_{meal.name}")
            driver_combo.addItem("미지정", None)
            for driver in drivers:
                detail = driver.telephone or str(driver.driver_id)[:8]
                label = f"{driver.name} · {detail}" if name_counts[driver.name] > 1 or driver.telephone else driver.name
                driver_combo.addItem(label, driver.driver_id)
            row.addWidget(driver_combo, 2)
            legacy_edit = QLineEdit()
            legacy_edit.setObjectName(f"newPerson_legacy_driver_{meal.name}")
            legacy_edit.setPlaceholderText("미등록 기사명")
            row.addWidget(legacy_edit, 1)
            sequence_edit = QLineEdit()
            sequence_edit.setObjectName(f"newPerson_sequence_{meal.name}")
            sequence_edit.setPlaceholderText("배송순서")
            row.addWidget(sequence_edit, 1)
            delivery_form.addRow(f"{meal.value} 담당·순서", row)
            self._driver_combos[meal], self._legacy_driver_edits[meal], self._sequence_edits[meal] = driver_combo, legacy_edit, sequence_edit
            status_row = QHBoxLayout()
            status_combo = QComboBox()
            status_combo.setObjectName(f"newPerson_status_{meal.name}")
            for status in MealStatus:
                status_combo.addItem(status.value, status)
            status_row.addWidget(status_combo)
            pause_end = QLineEdit()
            pause_end.setObjectName(f"newPerson_pause_end_{meal.name}")
            pause_end.setPlaceholderText("일시중지 종료일 YYYY-MM-DD")
            pause_end.setEnabled(False)
            status_combo.currentIndexChanged.connect(lambda _index, combo=status_combo, editor=pause_end, check=self._meal_checks[meal]: editor.setEnabled(check.isChecked() and combo.currentData() == MealStatus.PAUSED))
            status_row.addWidget(pause_end)
            delivery_form.addRow(f"{meal.value} 초기 상태", status_row)
            self._status_combos[meal], self._pause_end_edits[meal] = status_combo, pause_end
            controls = (driver_combo, sequence_edit, status_combo)
            self._meal_checks[meal].toggled.connect(lambda checked, widgets=controls, legacy_editor=legacy_edit, selected=driver_combo, end=pause_end, status_box=status_combo: (
                [widget.setEnabled(checked) for widget in widgets],
                legacy_editor.setEnabled(checked and selected.currentData() is None),
                end.setEnabled(checked and status_box.currentData() == MealStatus.PAUSED),
            ))
            driver_combo.currentIndexChanged.connect(lambda _index, selected=driver_combo, editor=legacy_edit, check=self._meal_checks[meal]: editor.setEnabled(selected.currentData() is None and check.isChecked()))
            for widget in (*controls, legacy_edit):
                widget.setEnabled(self._meal_checks[meal].isChecked())
        body_layout.addWidget(delivery_frame)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        self._error = QLabel()
        self._error.setObjectName("errorMessage")
        self._error.setWordWrap(True)
        self._error.hide()
        root.addWidget(self._error)
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("취소")
        cancel.setObjectName("quietAction")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        save = QPushButton("행 추가")
        save.setObjectName("primaryAction")
        save.clicked.connect(self._accept_valid)
        actions.addWidget(save)
        root.addLayout(actions)
        self._request: RosterRowCreateRequest | None = None
        self._profile: PersonProfile | None = None
        self._meals: tuple[Meal, ...] = ()
        self._start_date: date | None = None

    def _accept_valid(self) -> None:
        try:
            def value(key: str) -> str:
                editor = self._editors[key]
                return editor.toPlainText() if isinstance(editor, QPlainTextEdit) else editor.text()

            custom_values = {}
            for definition in self._custom_fields:
                editor = self._editors[f"custom:{definition.definition_id}"]
                raw = editor.currentData() if isinstance(editor, QComboBox) else editor.text().strip()
                parsed = _custom_value(definition, raw or "")
                if parsed is not None:
                    custom_values[definition.definition_id] = parsed
            profile = PersonProfile(
                name=value("name"), birth_date=_parse_optional_date(value("birth_date")),
                **{key: value(key) or None for key in ("school_type", "school_name", "grade", "class_name", "household_head_name", "relationship_to_child", "jurisdiction", "provider", "road_address", "detail_address", "telephone", "mobile_phone", "note")},
                custom_values=custom_values,
            )
            meals = tuple(meal for meal, check in self._meal_checks.items() if check.isChecked())
            if not meals:
                raise ValueError("가입 식종을 하나 이상 선택해주세요.")
            assignments = {}
            statuses = {}
            for meal in (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH):
                registered = self._driver_combos[meal].currentData()
                legacy = self._legacy_driver_edits[meal].text().strip() if registered is None else ""
                sequence = self._sequence_edits[meal].text().strip() or None
                status = MealStatus(self._status_combos[meal].currentData())
                pause_text = self._pause_end_edits[meal].text().strip()
                if meal not in meals:
                    continue
                if registered is not None or legacy or sequence:
                    assignments[meal] = (registered if registered is not None else legacy or None, sequence)
                if status is MealStatus.PAUSED:
                    if not pause_text:
                        raise ValueError(f"{meal.value} 일시중지 종료일을 입력해주세요.")
                    pause_end = _parse_optional_date(pause_text)
                    if pause_end < self.start_date_edit.date().toPython():
                        raise ValueError(f"{meal.value} 일시중지 종료일은 배송 시작일 이후여야 합니다.")
                    statuses[meal] = (status, pause_end)
                elif status is MealStatus.STOPPED:
                    statuses[meal] = (status, None)
        except (TypeError, ValueError) as error:
            self._error.setText(str(error))
            self._error.show()
            return
        selected_day = self.start_date_edit.date()
        self._profile, self._meals = profile, meals
        self._start_date = date(selected_day.year(), selected_day.month(), selected_day.day())
        self._request = RosterRowCreateRequest(profile, meals, self._start_date, assignments, self.household_combo.currentData(), statuses)
        self.accept()

    def values(self) -> tuple[PersonProfile, tuple[Meal, ...], date]:
        if self._profile is None or self._start_date is None:
            raise RuntimeError("추가 확정 전에는 값을 가져올 수 없습니다.")
        return self._profile, self._meals, self._start_date

    def request(self) -> RosterRowCreateRequest:
        if self._request is None:
            raise RuntimeError("추가 확정 전에는 값을 가져올 수 없습니다.")
        return self._request


class RosterRowDeleteDialog(QDialog):
    """Require an explicit delete action for one person across all meals."""

    def __init__(self, person: Person, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("명단 행 삭제 확인")
        self.setMinimumWidth(430)
        root = QVBoxLayout(self)
        title = QLabel(f"{person.profile.name} 대상자를 삭제할까요?")
        title.setObjectName("workspaceTitle")
        root.addWidget(title)
        explanation = QLabel("현재 표시된 식종을 포함해 모든 식종의 가입, 가구 연결, 배송 배정이 현재 명단에서 제거됩니다. 이전 이력은 보존됩니다.")
        explanation.setWordWrap(True)
        root.addWidget(explanation)
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("취소")
        cancel.setObjectName("quietAction")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        delete = QPushButton("삭제")
        delete.setObjectName("primaryAction")
        delete.clicked.connect(self.accept)
        actions.addWidget(delete)
        root.addLayout(actions)


class RosterRowEditStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RosterRowEditOutcome:
    status: RosterRowEditStatus
    roster: RosterSnapshot | None = None
    backup_result: AutomaticBackupResult | None = None

    @property
    def completed(self) -> bool:
        return self.status is RosterRowEditStatus.COMPLETED


class RosterRowEditController:
    def __init__(self, session: DatabaseSession, *, dialog_type=RosterRowCreateDialog, delete_dialog_type=RosterRowDeleteDialog, message_box=QMessageBox, actor_provider: Callable[[], RevisionActor] = current_windows_actor, recorded_at_provider: Callable[[], datetime] = lambda: datetime.now(UTC), effective_date_provider: Callable[[], date] = date.today, revision_id_factory: Callable[[], UUID] = uuid4, person_id_factory: Callable[[], UUID] = uuid4) -> None:
        self._session = session
        self._dialog_type = dialog_type
        self._delete_dialog_type = delete_dialog_type
        self._message_box = message_box
        self._actor_provider = actor_provider
        self._recorded_at_provider = recorded_at_provider
        self._effective_date_provider = effective_date_provider
        self._revision_id_factory = revision_id_factory
        self._person_id_factory = person_id_factory

    def add(self, selected_meal: Meal, parent: QWidget | None = None, *, service_day: date | None = None) -> RosterRowEditOutcome:
        roster = self._session.database.current_roster()
        if roster is None:
            return self._failure(parent, ValueError("현재 명단이 없습니다."))
        state = (
            routing_state_from_snapshot(self._session.database.snapshot(roster.revision_id))
            if hasattr(self._session.database, "snapshot") else RoutingState()
        )
        if self._dialog_type is RosterRowCreateDialog:
            on = service_day or date.today()
            household_labels = {}
            for household in roster.households:
                members = roster.household_members(household.household_id, on=on)
                if members:
                    names = ", ".join(person.profile.name for person in members)
                    address = next((person.profile.road_address for person in members if person.profile.road_address), None)
                    household_labels[household.household_id] = names + (f" · {address}" if address else "")
            dialog = self._dialog_type(selected_meal, on, parent, custom_fields=roster.custom_fields, drivers=state.drivers, households=roster.households, household_labels=household_labels)
        else:
            dialog = self._dialog_type(selected_meal, service_day or date.today(), parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return RosterRowEditOutcome(RosterRowEditStatus.CANCELLED)
        person_id = self._person_id_factory()
        if not hasattr(dialog, "request"):
            profile, meals, starts_on = dialog.values()
            return self._commit(parent, add_person=Person(person_id, profile), enroll_meals=meals, delivery_starts_on=starts_on)
        request = dialog.request()
        meal_assignments = []
        driver_ids = {}
        for meal, (driver, sequence) in request.assignments.items():
            if isinstance(driver, UUID):
                driver_ids[meal] = driver
            if isinstance(driver, str) or sequence:
                meal_assignments.append(DeliveryAssignment(person_id, meal, driver if isinstance(driver, str) else None, sequence))
        return self._commit(
            parent, add_person=Person(person_id, request.profile), enroll_meals=request.meals,
            delivery_starts_on=request.starts_on, meal_assignments=tuple(meal_assignments),
            household_id=request.household_id, driver_ids=driver_ids, routing_state=state,
            routing_event_id=uuid4() if driver_ids else None, initial_statuses=request.statuses,
            status_event_ids={meal: uuid4() for meal in request.statuses},
        )

    def delete(self, person_id: UUID, parent: QWidget | None = None) -> RosterRowEditOutcome:
        roster = self._session.database.current_roster()
        if roster is None:
            return self._failure(parent, ValueError("현재 명단이 없습니다."))
        try:
            person = roster.person(person_id)
        except (TypeError, KeyError) as error:
            return self._failure(parent, error)
        dialog = self._delete_dialog_type(person, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return RosterRowEditOutcome(RosterRowEditStatus.CANCELLED)
        return self._commit(parent, delete_person_id=person_id)

    def _commit(self, parent: QWidget | None, **change) -> RosterRowEditOutcome:
        try:
            roster = self._session.database.current_roster()
            if roster is None:
                raise ValueError("현재 명단이 없습니다.")
            prepared = build_roster_row_revision(
                roster, actor=self._actor_provider(), recorded_at=self._recorded_at_provider(),
                effective_on=self._effective_date_provider(), revision_id=self._revision_id_factory(), **change,
            )
            backup = self._session.append_revision(prepared.record)
        except Exception as error:
            return self._failure(parent, error)
        if backup.error is not None:
            self._message_box.warning(parent, "명단은 저장됐지만 자동 백업에 실패했습니다", str(backup.error))
        return RosterRowEditOutcome(RosterRowEditStatus.COMPLETED, prepared.snapshot, backup)

    def _failure(self, parent: QWidget | None, error: Exception) -> RosterRowEditOutcome:
        self._message_box.critical(parent, "명단 행을 저장하지 못했습니다", "운영 데이터에는 반영하지 않았습니다.\n\n" + str(error))
        return RosterRowEditOutcome(RosterRowEditStatus.FAILED)
