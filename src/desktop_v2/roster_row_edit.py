"""Add and delete one roster person through the revision ledger."""

from __future__ import annotations

from .user_messages import user_error

from .dialog_workflow import DialogRequest, dialog_workflow, invoke

from .startup_loading import save_revision

from src.storage.database_session import committed_roster_after_save

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtCore import QDate
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget

from src.domain.delivery_routing import RoutingState, routing_state_from_snapshot
from src.domain.delivery_dates import (
    actual_delivery_weekdays,
    next_actual_delivery_date,
    validate_actual_delivery_date,
    weekday_text,
)
from src.domain.column_input_rules import InputRule, load_rules, validate_new_profile
from src.domain.roster import CustomFieldDefinition, CustomFieldType, DeliveryAssignment, HouseholdDeliverySetting, Meal, Person, PersonProfile, RosterSnapshot, standard_delivery_defaults
from src.domain.roster_row_changes import build_roster_row_revision
from src.domain.row_household_conflicts import row_household_conflicts
from src.domain.revisions import RevisionActor
from src.domain.status import MealStatus
from src.domain.intake import IntakeChannel
from src.importing.households import match_existing_household_id
from src.storage.database_session import (
    AutomaticBackupResult,
    DatabaseSession,
)

from .date_edit import WheelSafeDateEdit
from .row_household_conflicts import RowHouseholdConflictDialog
from .column_input_rules import make_rule_editor
from .import_controller import current_windows_actor
from .person_edit import _custom_value, _parse_optional_date
from .theme import application_stylesheet, register_application_fonts


_PROFILE_COLUMNS = {
    "name": ("name", "성명"), "birth_date": ("birth_date", "생년월일"),
    "school": ("school_name", "학교"), "grade": ("grade", "학년"),
    "telephone": ("telephone", "전화번호"), "mobile": ("mobile_phone", "핸드폰"),
    "jurisdiction": ("jurisdiction", "관할동"), "address": ("address", "도로명주소"),
    "detail_address": ("detail_address", "상세주소"),
    "note": ("note", "비고"),
    "school_type": ("school_type", "학교 유형"),
    "household_head_name": ("household_head_name", "세대주 성명"),
    "relationship_to_child": ("relationship_to_child", "아동과의 관계"),
}


def _date_value(editor: WheelSafeDateEdit) -> date:
    selected = editor.date()
    return date(selected.year(), selected.month(), selected.day())


def _compact_weekday_text(weekdays: tuple[int, ...]) -> str:
    text = weekday_text(weekdays)
    return text if text == "없음" else text.replace("요일", "") + "요일"


@dataclass(frozen=True, slots=True)
class RosterRowCreateRequest:
    profile: PersonProfile
    meals: tuple[Meal, ...]
    starts_on: date
    assignments: dict[Meal, tuple[UUID | str | None, str | None]]
    household_id: UUID | None
    statuses: dict[Meal, tuple[MealStatus, date | None]]
    meal_starts_on: dict[Meal, date] = field(default_factory=dict)
    intake_channel: IntakeChannel | None = None


class RosterRowCreateDialog(QDialog):
    """Collect a new person's profile and at least one enrolled meal."""

    def __init__(self, selected_meal: Meal = Meal.LUNCH, start_date: date | None = None, parent: QWidget | None = None, *, custom_fields: tuple[CustomFieldDefinition, ...] = (), household_matcher: Callable[[PersonProfile, date], UUID | None] | None = None, column_labels: Mapping[str, str] | None = None, routing_state: RoutingState | None = None, input_rules: dict[str, InputRule] | None = None, delivery_defaults: tuple[HouseholdDeliverySetting, ...] | None = None) -> None:
        super().__init__(parent)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("명단 행 추가")
        self.resize(760, 640)
        self.setMinimumSize(650, 580)
        if column_labels is None:
            column_labels = {
                key: label for key, (_, label) in _PROFILE_COLUMNS.items()
                if key not in {"school_type", "household_head_name", "relationship_to_child"}
            }
            column_labels.update({f"custom:{field.definition_id}": field.name for field in sorted(custom_fields, key=lambda item: item.display_order)})
        self._custom_fields = tuple(field for field in custom_fields if f"custom:{field.definition_id}" in column_labels)
        custom_by_key = {f"custom:{field.definition_id}": field for field in self._custom_fields}
        self._household_matcher = household_matcher
        self._input_rules = input_rules or {}
        self._delivery_defaults = delivery_defaults or standard_delivery_defaults()
        self._editors: dict[str, QLineEdit | QPlainTextEdit | QComboBox] = {}
        root = QVBoxLayout(self)
        title = QLabel("대상자 추가")
        title.setObjectName("workspaceTitle")
        root.addWidget(title)
        hint = QLabel("명단의 인적사항과 가입 식종을 입력합니다. 배송 시작일 전에는 배송 대상에 포함되지 않습니다.")
        hint.setObjectName("workspaceSubtitle")
        hint.setWordWrap(True)
        root.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        frame = QFrame()
        frame.setObjectName("editSection")
        grid = QGridLayout(frame)
        for column_key, label in column_labels.items():
            definition = custom_by_key.get(column_key)
            if column_key not in _PROFILE_COLUMNS and definition is None:
                continue
            key = _PROFILE_COLUMNS[column_key][0] if definition is None else column_key
            if key == "note":
                editor = QPlainTextEdit()
                editor.setMaximumHeight(90)
            elif definition is not None and definition.field_type is CustomFieldType.CHOICE:
                editor = QComboBox()
                editor.addItem("미입력", "")
                for choice in definition.choices:
                    editor.addItem(choice, choice)
            else:
                editor = make_rule_editor(column_key, self._input_rules)
                editor.setClearButtonEnabled(True)
                if key == "birth_date" or (definition is not None and definition.field_type is CustomFieldType.DATE):
                    editor.setPlaceholderText("YYYY-MM-DD 또는 YYYYMMDD")
            editor.setObjectName(f"newPerson_{key}")
            row, column = divmod(len(self._editors), 2)
            field_label = QLabel(label + (" *" if key == "name" else ""))
            field_label.setWordWrap(True)
            grid.addWidget(field_label, row * 2, column)
            grid.addWidget(editor, row * 2 + 1, column)
            self._editors[key] = editor
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        body_layout.addWidget(frame)
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
        self._intake_channel = QComboBox()
        self._intake_channel.setObjectName("intakeChannel")
        self._intake_channel.addItem("-", None)
        for channel in IntakeChannel:
            self._intake_channel.addItem(channel.value, channel)
        delivery_form.addRow("접수 경로", self._intake_channel)
        day = start_date or date.today()
        self._start_date_edits: dict[Meal, WheelSafeDateEdit] = {}
        for meal, check in self._meal_checks.items():
            default_day = next_actual_delivery_date(self._delivery_defaults, meal, day)
            editor = WheelSafeDateEdit(
                QDate(default_day.year, default_day.month, default_day.day)
            )
            editor.setObjectName(f"deliveryStartDate_{meal.name}")
            editor.setCalendarPopup(True)
            editor.setDisplayFormat("yyyy-MM-dd")
            editor.setEnabled(check.isChecked())
            check.toggled.connect(editor.setEnabled)
            allowed = QLabel(
                "실제 배송: "
                + _compact_weekday_text(
                    actual_delivery_weekdays(self._delivery_defaults, meal)
                )
            )
            allowed.setObjectName("muted")
            row = QHBoxLayout()
            row.addWidget(editor)
            row.addWidget(allowed)
            row.addStretch()
            delivery_form.addRow(f"{meal.value} 배송 시작일 *", row)
            self._start_date_edits[meal] = editor
        # Compatibility for callers that still inspect the initially selected date.
        self.start_date_edit = self._start_date_edits[selected_meal]
        self._driver_combos: dict[Meal, QComboBox] = {}
        state = routing_state or RoutingState()
        for meal, check in self._meal_checks.items():
            combo = QComboBox()
            combo.setObjectName(f"newPerson_driver_{meal.name}")
            combo.addItem("미지정", None)
            for driver in state.drivers:
                label = driver.name + (f" · {driver.telephone}" if driver.telephone else "")
                combo.addItem(label, driver.driver_id)
            combo.setEnabled(check.isChecked())
            check.toggled.connect(combo.setEnabled)
            self._driver_combos[meal] = combo
            delivery_form.addRow(f"{meal.value} 배송 담당", combo)
        delivery_hint = QLabel("선택한 식종의 배송 시작일부터 담당 기사를 적용합니다." if state.drivers else "등록된 기사가 없습니다. 미지정으로 추가한 뒤 배송기사 관리에서 기사를 등록할 수 있습니다.")
        delivery_hint.setWordWrap(True)
        delivery_form.addRow(delivery_hint)
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
                editor = self._editors.get(key)
                if editor is None:
                    return ""
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
                **{key: value(key) or None for key in (
                    "school_name", "school_type", "grade", "household_head_name",
                    "relationship_to_child", "jurisdiction", "telephone", "mobile_phone", "note",
                )},
                road_address=value("address") or None,
                detail_address=value("detail_address") or None,
                custom_values=custom_values,
            )
            validate_new_profile(profile, self._input_rules)
            meals = tuple(meal for meal, check in self._meal_checks.items() if check.isChecked())
            if not meals:
                raise ValueError("가입 식종을 하나 이상 선택해주세요.")
            starts_by_meal = {
                meal: _date_value(self._start_date_edits[meal]) for meal in meals
            }
            for meal, starts_on in starts_by_meal.items():
                validate_actual_delivery_date(
                    self._delivery_defaults,
                    meal,
                    starts_on,
                    field_name="배송 시작일",
                )
            starts_on = min(starts_by_meal.values())
            household_id = (
                self._household_matcher(profile, starts_on)
                if self._household_matcher is not None else None
            )
        except (TypeError, ValueError, KeyError) as error:
            self._error.setText(user_error(error))
            self._error.show()
            return
        self._profile, self._meals = profile, meals
        self._start_date = starts_on
        assignments = {meal: (self._driver_combos[meal].currentData(), None) for meal in meals if self._driver_combos[meal].currentData() is not None}
        self._request = RosterRowCreateRequest(
            profile, meals, starts_on, assignments, household_id, {}, starts_by_meal,
            intake_channel=(IntakeChannel(self._intake_channel.currentData())
                            if self._intake_channel.currentData() is not None else None),
        )
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
    def __init__(self, session: DatabaseSession, *, dialog_type=RosterRowCreateDialog, delete_dialog_type=RosterRowDeleteDialog, conflict_dialog_type=RowHouseholdConflictDialog, message_box=QMessageBox, actor_provider: Callable[[], RevisionActor] = current_windows_actor, recorded_at_provider: Callable[[], datetime] = lambda: datetime.now(UTC), effective_date_provider: Callable[[], date] = date.today, revision_id_factory: Callable[[], UUID] = uuid4, person_id_factory: Callable[[], UUID] = uuid4) -> None:
        self._session = session
        self._dialog_type = dialog_type
        self._delete_dialog_type = delete_dialog_type
        self._conflict_dialog_type = conflict_dialog_type
        self._message_box = message_box
        self._actor_provider = actor_provider
        self._recorded_at_provider = recorded_at_provider
        self._effective_date_provider = effective_date_provider
        self._revision_id_factory = revision_id_factory
        self._person_id_factory = person_id_factory

    @dialog_workflow
    def add(self, selected_meal: Meal, parent: QWidget | None = None, *, service_day: date | None = None) -> RosterRowEditOutcome:
        roster = self._session.database.current_roster()
        if roster is None:
            return self._failure(parent, ValueError("현재 명단이 없습니다."))
        snapshot = None
        if self._dialog_type is RosterRowCreateDialog:
            on = service_day or date.today()
            try:
                snapshot = (
                    self._session.database.snapshot(roster.revision_id)
                    if hasattr(self._session.database, "snapshot") else None
                )
            except Exception as error:
                return self._failure(parent, error)
            dialog = self._dialog_type(
                selected_meal,
                on,
                parent,
                custom_fields=roster.custom_fields,
                column_labels=(
                    {column.key: column.label for column in parent.table_model.columns}
                    if getattr(parent, "table_model", None) is not None else None
                ),
                routing_state=routing_state_from_snapshot(snapshot) if snapshot is not None else RoutingState(),
                input_rules=load_rules(snapshot),
                delivery_defaults=roster.delivery_defaults,
                household_matcher=lambda profile, starts_on: match_existing_household_id(
                    profile, None, roster, effective_on=starts_on,
                ),
            )
        else:
            dialog = self._dialog_type(selected_meal, service_day or date.today(), parent)
        person_id = self._person_id_factory()
        while True:
            if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
                return RosterRowEditOutcome(RosterRowEditStatus.CANCELLED)
            if not hasattr(dialog, "request"):
                profile, meals, starts_on = dialog.values()
                return self._commit(parent, add_person=Person(person_id, profile), enroll_meals=meals, delivery_starts_on=starts_on)
            request = dialog.request()
            try:
                if snapshot is None and hasattr(self._session.database, "snapshot"):
                    snapshot = self._session.database.snapshot(roster.revision_id)
                if self._session.database.current_roster().revision_id != roster.revision_id:
                    raise ValueError("명단이 변경되었습니다. 입력값을 복사한 뒤 창을 다시 열어주세요.")
                validate_new_profile(request.profile, load_rules(snapshot))
                starts_by_meal = request.meal_starts_on or {
                    meal: request.starts_on for meal in request.meals
                }
                from src.domain.household_grouping import GROUPING_KIND, GROUPING_ID
                explicit_grouping = snapshot is not None and snapshot.entity(GROUPING_KIND, GROUPING_ID) is not None
                # Explicit criteria are applied after the full draft has been built.
                household_id = None if explicit_grouping else match_existing_household_id(
                    request.profile, None, roster, effective_on=min(starts_by_meal.values()),
                )
                state = routing_state_from_snapshot(snapshot) if snapshot is not None and request.assignments else RoutingState()
                meal_assignments = []
                driver_ids = {}
                for meal, (driver, sequence) in request.assignments.items():
                    if isinstance(driver, UUID):
                        driver_ids[meal] = driver
                    if isinstance(driver, str) or sequence:
                        meal_assignments.append(DeliveryAssignment(person_id, meal, driver if isinstance(driver, str) else None, sequence))
                prepared = build_roster_row_revision(
                    roster, actor=self._actor_provider(), recorded_at=self._recorded_at_provider(),
                    effective_on=self._effective_date_provider(), revision_id=self._revision_id_factory(),
                    add_person=Person(person_id, request.profile), enroll_meals=request.meals,
                    delivery_starts_on=request.starts_on,
                    delivery_starts_by_meal=starts_by_meal,
                    meal_assignments=tuple(meal_assignments),
                    household_id=household_id, driver_ids=driver_ids, routing_state=state,
                    routing_event_ids={
                        starts_on: uuid4()
                        for starts_on in {
                            starts_by_meal[meal] for meal in driver_ids
                        }
                    },
                    initial_statuses=request.statuses,
                    status_event_ids={meal: uuid4() for meal in request.statuses},
                    intake_channel=request.intake_channel,
                )
                conflicts = row_household_conflicts(snapshot, prepared)
                if conflicts:
                    conflict_dialog = self._conflict_dialog_type(snapshot, prepared, conflicts, parent)
                    while True:
                        if (yield DialogRequest(conflict_dialog, self._session)) != QDialog.DialogCode.Accepted:
                            break
                        outcome = yield from invoke(self._save_prepared, parent, conflict_dialog.resolved)
                        if outcome.completed:
                            return outcome
                    continue
                outcome = yield from invoke(self._save_prepared, parent, prepared)
                if outcome.completed or not isinstance(dialog, RosterRowCreateDialog):
                    return outcome
            except Exception as error:
                outcome = self._failure(parent, error)
                if not isinstance(dialog, RosterRowCreateDialog):
                    return outcome

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
            if change.get("add_person") is not None:
                reader = getattr(self._session.database, "snapshot", None)
                validate_new_profile(change["add_person"].profile, load_rules(reader(roster.revision_id) if reader else None))
            prepared = build_roster_row_revision(
                roster, actor=self._actor_provider(), recorded_at=self._recorded_at_provider(),
                effective_on=self._effective_date_provider(), revision_id=self._revision_id_factory(), **change,
            )
        except Exception as error:
            return self._failure(parent, error)
        return self._save_prepared(parent, prepared)

    @dialog_workflow
    def _save_prepared(self, parent, prepared):
        from .mobile_household_merge import review_mobile_household_merge
        try:
            record = yield from invoke(review_mobile_household_merge, self._session, prepared.record, parent)
            if record is None:
                return RosterRowEditOutcome(RosterRowEditStatus.CANCELLED)
            backup = save_revision(self._session, record, parent=parent)
        except Exception as error:
            return self._failure(parent, error)
        if backup.warning_required:
            self._message_box.warning(parent, "명단은 저장됐지만 자동 백업에 실패했습니다", user_error(backup.error))
        return RosterRowEditOutcome(RosterRowEditStatus.COMPLETED, committed_roster_after_save(self._session, prepared.snapshot), backup)

    def _failure(self, parent: QWidget | None, error: Exception) -> RosterRowEditOutcome:
        self._message_box.critical(parent, "명단 행을 저장하지 못했습니다", "운영 데이터에는 반영하지 않았습니다.\n\n" + user_error(error))
        return RosterRowEditOutcome(RosterRowEditStatus.FAILED)
