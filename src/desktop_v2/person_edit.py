"""Reviewed person-profile editor and its revision/backup controller."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.domain import (
    Person,
    PersonProfile,
    RevisionActor,
    RosterSnapshot,
    build_person_profile_revision,
)
from src.domain.profile_edits import profile_household_members
from src.domain.roster import CustomFieldDefinition, CustomFieldType, HouseholdDeliverySetting, Meal
from src.storage import AutomaticBackupResult, DatabaseSession
from src.services.meal_service_availability import default_delivery_weekdays, default_service_weekdays

from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts

_FIELD_LABELS = {
    "name": "성명",
    "birth_date": "생년월일",
    "school_type": "취학유형",
    "school_name": "학교명",
    "grade": "학년",
    "household_head_name": "세대주 성명",
    "relationship_to_child": "아동과의 관계",
    "jurisdiction": "관할동",
    "road_address": "도로명주소",
    "detail_address": "상세주소",
    "telephone": "전화번호",
    "mobile_phone": "핸드폰",
    "note": "비고",
}
_SECTIONS = (
    (
        "기본·학교",
        ("name", "birth_date", "school_type", "school_name", "grade"),
    ),
    (
        "가구·기관",
        (
            "household_head_name",
            "relationship_to_child",
            "jurisdiction",
        ),
    ),
    (
        "연락처·주소",
        ("mobile_phone", "telephone", "road_address", "detail_address"),
    ),
)
_PLACEHOLDERS = {
    "birth_date": "예: 2015-03-02",
    "mobile_phone": "예: 010-1234-5678",
    "telephone": "예: 031-123-4567",
    "road_address": "도로명주소",
    "detail_address": "동·호수 등 상세주소",
}


class PersonProfileEditDialog(QDialog):
    """Edit canonical profile values; only explicit UI blanks can clear data."""

    def __init__(
        self,
        person: Person,
        parent: QWidget | None = None,
        *,
        message_box=QMessageBox,
        custom_fields: tuple[CustomFieldDefinition, ...] = (),
        household_members: tuple[Person, ...] = (),
        household_delivery_settings: tuple[HouseholdDeliverySetting, ...] | None = None,
        effective_delivery_settings: tuple[HouseholdDeliverySetting, ...] | None = None,
        member_delivery_settings: dict[UUID, tuple[HouseholdDeliverySetting, ...]] | None = None,
        delivery_only: bool = False,
        defaults_roster: RosterSnapshot | None = None,
    ) -> None:
        if not isinstance(person, Person):
            raise TypeError("person은 Person이어야 합니다.")
        super().__init__(parent)
        self._person = person
        self._original = person.profile
        self._message_box = message_box
        self._custom_fields = custom_fields
        self._household_members = household_members or (person,)
        self._original_delivery_settings = person.delivery_settings
        self._effective_delivery_settings = (
            effective_delivery_settings if effective_delivery_settings is not None
            else household_delivery_settings if household_delivery_settings is not None
            else person.delivery_settings or ()
        )
        self._member_delivery_settings = member_delivery_settings or {}
        self._delivery_only = delivery_only
        self._defaults_roster = defaults_roster
        self._delivery_days: dict[Meal, tuple[QCheckBox, ...]] = {}
        self._physical_delivery_days: dict[Meal, tuple[QComboBox, ...]] = {}
        self._delivery_forms: dict[Meal, QLineEdit] = {}
        self._delivery_status: dict[Meal, QLabel] = {}
        self._initial_delivery_values: dict[Meal, tuple[tuple[int, ...], tuple[int, ...], str]] = {}
        self._force_reset_meals: set[Meal] = set()
        self._accepted_profile: PersonProfile | None = None
        self._editors: dict[str, QLineEdit | QPlainTextEdit] = {}
        register_application_fonts()
        self.setObjectName("personProfileEditDialog")
        self.setWindowTitle(f"{person.profile.name} 배송 설정" if delivery_only else f"{person.profile.name} · 사람 정보 수정")
        self.resize(760, 680 if delivery_only else 760)
        self.setMinimumSize(650, 560 if delivery_only else 620)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()
        self._populate()
        self._connect_changes()
        self._refresh_form_state()

    def profile(self) -> PersonProfile:
        if self._accepted_profile is None:
            raise RuntimeError("저장 확정 전에는 수정 profile을 가져올 수 없습니다.")
        return self._accepted_profile

    def delivery_settings(self) -> tuple[HouseholdDeliverySetting, ...] | None:
        return self._candidate_delivery_settings()

    def cleared_fields(self) -> tuple[str, ...]:
        if self._delivery_only:
            return ()
        canonical = tuple(
            key
            for key in _FIELD_LABELS
            if key != "name"
            and getattr(self._original, key) is not None
            and not self._editor_text(key).strip()
        )
        custom = tuple(
            definition.name
            for definition in self._custom_fields
            if definition.definition_id in self._original.custom_values
            and not self._editor_text(f"custom:{definition.definition_id}").strip()
        )
        return (*canonical, *custom)

    def _build_ui(self) -> None:
        if self._delivery_only:
            self._build_delivery_only_ui()
            return
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 16)
        root.setSpacing(12)

        header = QFrame()
        header.setObjectName("editHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(16, 14, 16, 14)
        header_layout.setSpacing(3)
        eyebrow = QLabel("명단 기록 · 직접 수정")
        eyebrow.setObjectName("editEyebrow")
        header_layout.addWidget(eyebrow)
        title = QLabel(f"{self._original.name} 정보 수정")
        title.setObjectName("workspaceTitle")
        header_layout.addWidget(title)
        help_text = QLabel(
            "이 화면에서 비운 값은 저장된 명단에서도 삭제됩니다. "
            "Excel 가져오기의 빈칸은 기존 값을 유지합니다."
        )
        help_text.setObjectName("workspaceSubtitle")
        help_text.setWordWrap(True)
        header_layout.addWidget(help_text)
        root.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll_body = QWidget()
        scroll_layout = QVBoxLayout(scroll_body)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(10)
        for title_text, field_keys in _SECTIONS:
            scroll_layout.addWidget(self._build_section(title_text, field_keys))
        scroll_layout.addWidget(self._build_note_section())
        scroll_layout.addWidget(self._build_delivery_section())
        if self._custom_fields:
            scroll_layout.addWidget(self._build_custom_fields_section())
        scroll_layout.addStretch(1)
        scroll.setWidget(scroll_body)
        root.addWidget(scroll, 1)

        self.household_mobile = QCheckBox("핸드폰을 가구 전체에 적용")
        self.household_mobile.setEnabled(len(self._household_members) > 1)
        self.household_mobile.toggled.connect(self._refresh_form_state)
        root.addWidget(self.household_mobile)
        targets = QLabel("같은 가구: " + ", ".join(member.profile.name for member in self._household_members)
                         + "\n핸드폰 공유는 위 체크로, 배송 설정 공유는 배송 설정 안의 체크로 각각 선택합니다.")
        targets.setWordWrap(True)
        root.addWidget(targets)

        self._clear_notice = QLabel()
        self._clear_notice.setObjectName("clearNotice")
        self._clear_notice.setWordWrap(True)
        root.addWidget(self._clear_notice)

        self._form_error = QLabel()
        self._form_error.setObjectName("errorMessage")
        self._form_error.setWordWrap(True)
        root.addWidget(self._form_error)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("취소")
        cancel.setObjectName("quietAction")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self._save_button = QPushButton("변경 저장")
        self._save_button.setObjectName("primaryAction")
        self._save_button.clicked.connect(self._save)
        actions.addWidget(self._save_button)
        root.addLayout(actions)

    def _build_delivery_only_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 16)
        root.setSpacing(12)
        title = QLabel(f"{self._original.name} 배송 설정")
        title.setObjectName("workspaceTitle")
        root.addWidget(title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self._build_delivery_section())
        root.addWidget(scroll, 1)
        self._clear_notice = QLabel()
        self._clear_notice.hide()
        self._form_error = QLabel()
        self._form_error.setObjectName("errorMessage")
        self._form_error.setWordWrap(True)
        root.addWidget(self._form_error)
        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("취소")
        cancel.setObjectName("quietAction")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self._save_button = QPushButton("변경 저장")
        self._save_button.setObjectName("primaryAction")
        self._save_button.clicked.connect(self._save)
        actions.addWidget(self._save_button)
        root.addLayout(actions)

    def _build_section(self, title: str, field_keys: tuple[str, ...]) -> QFrame:
        frame = QFrame()
        frame.setObjectName("editSection")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(8)
        label = QLabel(title)
        label.setObjectName("sectionLabel")
        layout.addWidget(label)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(5)
        for index, key in enumerate(field_keys):
            column = index % 2
            row = (index // 2) * 2
            field_label = QLabel(_FIELD_LABELS[key] + (" *" if key == "name" else ""))
            field_label.setObjectName("fieldLabel")
            editor = QLineEdit()
            editor.setPlaceholderText(_PLACEHOLDERS.get(key, ""))
            editor.setClearButtonEnabled(True)
            editor.setObjectName(f"profileField_{key}")
            self._editors[key] = editor
            grid.addWidget(field_label, row, column)
            grid.addWidget(editor, row + 1, column)
        layout.addLayout(grid)
        return frame

    def _build_note_section(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("editSection")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 14)
        label = QLabel("비고")
        label.setObjectName("sectionLabel")
        layout.addWidget(label)
        editor = QPlainTextEdit()
        editor.setObjectName("profileField_note")
        editor.setPlaceholderText("업무상 필요한 내용만 입력하세요.")
        editor.setMinimumHeight(78)
        editor.setMaximumHeight(120)
        self._editors["note"] = editor
        layout.addWidget(editor)
        return frame

    def _build_delivery_section(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("editSection")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 14)
        if not self._delivery_only:
            self._delivery_toggle = QPushButton("배송 설정 — 기본값 (+)")
            self._delivery_toggle.setObjectName("quietAction")
            self._delivery_toggle.setCheckable(True)
            layout.addWidget(self._delivery_toggle)
        self._delivery_body = QWidget()
        body = QVBoxLayout(self._delivery_body)
        body.setContentsMargins(0, 8, 0, 0)
        body.setSpacing(10)
        existing = {item.meal: item for item in self._effective_delivery_settings}
        self.household_delivery = QCheckBox("같은 가구 전체에 배송 설정 적용")
        self.household_delivery.setEnabled(len(self._household_members) > 1)
        self.household_delivery.toggled.connect(self._refresh_delivery_scope)
        body.addWidget(self.household_delivery)
        self._delivery_scope = QLabel()
        self._delivery_scope.setObjectName("workspaceSubtitle")
        self._delivery_scope.setWordWrap(True)
        body.addWidget(self._delivery_scope)
        self._refresh_delivery_scope()
        guidance = QLabel("제공 요일은 식사를 제공하는 날, 배송 요일은 실제로 받는 날입니다. 조식 기본값은 금·토·일 제공분을 금요일에 함께 배송합니다. 기본적으로 선택한 사람에게만 적용됩니다.")
        guidance.setWordWrap(True)
        body.addWidget(guidance)
        for meal in (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH):
            setting = existing.get(meal)
            meal_frame = QFrame()
            meal_frame.setObjectName("editSection")
            meal_layout = QVBoxLayout(meal_frame)
            meal_layout.setContentsMargins(10, 8, 10, 8)
            heading = QHBoxLayout()
            meal_name = QLabel(meal.value)
            meal_name.setObjectName("sectionLabel")
            heading.addWidget(meal_name)
            status = QLabel()
            status.setObjectName("workspaceSubtitle")
            heading.addWidget(status)
            heading.addStretch(1)
            reset = QPushButton("초기화")
            reset.setObjectName("quietAction")
            reset.clicked.connect(lambda _checked=False, current=meal: self._reset_delivery_meal(current))
            heading.addWidget(reset)
            meal_layout.addLayout(heading)
            weekday_grid = QGridLayout()
            weekday_grid.addWidget(QLabel("제공 요일"), 0, 0)
            weekday_grid.addWidget(QLabel("배송 요일"), 1, 0)
            weekday_grid.setHorizontalSpacing(6)
            days = []
            for index, label in enumerate(("월", "화", "수", "목", "금", "토", "일")):
                day = QCheckBox(label)
                default_days = default_service_weekdays(meal, self._defaults_roster)
                day.setChecked(index in (setting.weekdays if setting and setting.weekdays is not None else default_days))
                day.toggled.connect(self._refresh_form_state)
                weekday_grid.addWidget(day, 0, index + 1)
                days.append(day)
            delivery_days = []
            mapping = setting.delivery_weekdays if setting and setting.delivery_weekdays is not None else default_delivery_weekdays(meal, self._defaults_roster)
            for index, day in enumerate(days):
                delivery_day = QComboBox()
                delivery_day.setAccessibleName(f"{meal.value} {day.text()} 제공분 실제 배송 요일")
                delivery_day.addItems(("월", "화", "수", "목", "금", "토", "일"))
                delivery_day.setCurrentIndex(mapping[index])
                delivery_day.setEnabled(day.isChecked())
                day.toggled.connect(delivery_day.setEnabled)
                delivery_day.currentIndexChanged.connect(self._refresh_form_state)
                weekday_grid.addWidget(delivery_day, 1, index + 1)
                delivery_days.append(delivery_day)
            meal_layout.addLayout(weekday_grid)
            form_row = QHBoxLayout()
            form_edit = QLineEdit()
            form_edit.setPlaceholderText("비워두면 기본 형태 · 예: 도시락, 식재료 박스")
            form_edit.setText(setting.form if setting and setting.form is not None else "")
            form_edit.textChanged.connect(self._refresh_form_state)
            form_row.addWidget(QLabel("배송 형태"))
            form_row.addWidget(form_edit, 1)
            meal_layout.addLayout(form_row)
            body.addWidget(meal_frame)
            self._delivery_days[meal] = tuple(days)
            self._physical_delivery_days[meal] = tuple(delivery_days)
            self._delivery_forms[meal] = form_edit
            self._delivery_status[meal] = status
            self._initial_delivery_values[meal] = self._delivery_values(meal)
        reset = QPushButton("전체 초기화")
        reset.setObjectName("quietAction")
        reset.clicked.connect(self._reset_delivery_settings)
        body.addWidget(reset)
        if not self._delivery_only:
            self._delivery_body.hide()
        layout.addWidget(self._delivery_body)
        if not self._delivery_only:
            self._delivery_toggle.toggled.connect(self._toggle_delivery_section)
        self._update_delivery_heading()
        return frame

    def _toggle_delivery_section(self, expanded: bool) -> None:
        self._delivery_body.setVisible(expanded)
        self._update_delivery_heading()

    def _reset_delivery_settings(self) -> None:
        for meal in self._delivery_days:
            self._reset_delivery_meal(meal)
        self._refresh_form_state()

    def _reset_delivery_meal(self, meal: Meal) -> None:
        self._force_reset_meals.add(meal)
        self._show_inherited_weekdays(meal)
        self._show_inherited_delivery_weekdays(meal)
        self._delivery_forms[meal].clear()
        self._refresh_form_state()

    def _show_inherited_weekdays(self, meal: Meal) -> None:
        for index, day in enumerate(self._delivery_days[meal]):
            day.setChecked(index in default_service_weekdays(meal, self._defaults_roster))

    def _show_inherited_delivery_weekdays(self, meal: Meal) -> None:
        for index, day in enumerate(self._physical_delivery_days[meal]):
            day.setCurrentIndex(default_delivery_weekdays(meal, self._defaults_roster)[index])

    def _update_delivery_heading(self) -> None:
        for meal, label in self._delivery_status.items():
            values = self._delivery_values(meal)
            baseline = (
                default_service_weekdays(meal, self._defaults_roster), default_delivery_weekdays(meal, self._defaults_roster), ""
            )
            summary = "별도 설정" if values != baseline else "기본값"
            if meal in self._force_reset_meals or values != self._initial_delivery_values[meal]:
                original = next((item for item in self._effective_delivery_settings if item.meal is meal), None)
                if self._setting_from_values(meal, values) != original:
                    summary += " · 저장할 변경"
            label.setText(summary)
        if not self._delivery_only:
            special = any("별도 설정" in label.text() for label in self._delivery_status.values())
            summary = "별도 설정" if special else "기본값"
            self._delivery_toggle.setText(f"배송 설정 — {summary} {'(-)' if self._delivery_toggle.isChecked() else '(+)'}")

    def _delivery_values(self, meal: Meal) -> tuple[tuple[int, ...], tuple[int, ...], str]:
        return (
            tuple(index for index, day in enumerate(self._delivery_days[meal]) if day.isChecked()),
            tuple(day.currentIndex() for day in self._physical_delivery_days[meal]),
            " ".join(self._delivery_forms[meal].text().split()),
        )

    def _setting_from_values(self, meal: Meal, values: tuple[tuple[int, ...], tuple[int, ...], str]) -> HouseholdDeliverySetting | None:
        days, mapping, form = values
        if not days:
            raise ValueError(f"{meal.value} 제공 요일을 하나 이상 선택해주세요.")
        weekdays = None if days == default_service_weekdays(meal, self._defaults_roster) else days
        delivery_weekdays = None if mapping == default_delivery_weekdays(meal, self._defaults_roster) else mapping
        if weekdays is None and delivery_weekdays is None and not form:
            return None
        return HouseholdDeliverySetting(meal, weekdays, form or None, delivery_weekdays)

    def _candidate_delivery_settings(self) -> tuple[HouseholdDeliverySetting, ...] | None:
        if not self._force_reset_meals and all(
            self._delivery_values(meal) == self._initial_delivery_values[meal]
            for meal in self._delivery_days
        ):
            return self._original_delivery_settings
        settings = []
        for meal in (Meal.BREAKFAST, Meal.LUNCH, Meal.DINNER):
            values = self._delivery_values(meal)
            baseline = (default_service_weekdays(meal, self._defaults_roster), default_delivery_weekdays(meal, self._defaults_roster), "")
            preserve_original = values == self._initial_delivery_values[meal] and (
                meal not in self._force_reset_meals or values != baseline
            )
            if preserve_original:
                setting = next((item for item in self._effective_delivery_settings if item.meal is meal), None)
            else:
                setting = self._setting_from_values(meal, values)
            if setting is not None:
                settings.append(setting)
        return tuple(settings)

    def _refresh_delivery_scope(self, *_args) -> None:
        members = self._household_members if self.household_delivery.isChecked() else (self._person,)
        names = ", ".join(member.profile.name for member in members)
        self._delivery_scope.setText(f"적용 대상 {len(members)}명: {names}")
        if hasattr(self, "_save_button"):
            self._refresh_form_state()

    def apply_delivery_to_household(self) -> bool:
        return self.household_delivery.isEnabled() and self.household_delivery.isChecked()

    def _delivery_copy_changed(self, candidate: tuple[HouseholdDeliverySetting, ...] | None) -> bool:
        if not self.apply_delivery_to_household():
            return False
        effective = self._effective_delivery_settings if candidate is None else candidate
        return any(
            self._member_delivery_settings.get(member.person_id, effective) != effective
            for member in self._household_members
            if member.person_id != self._person.person_id
        )

    def _build_custom_fields_section(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("editSection")
        layout = QGridLayout(frame)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.addWidget(QLabel("추가 항목"), 0, 0, 1, 2)
        for row, definition in enumerate(self._custom_fields, 1):
            key = f"custom:{definition.definition_id}"
            layout.addWidget(QLabel(definition.name), row, 0)
            editor = QLineEdit()
            editor.setObjectName(f"profileField_{key}")
            self._editors[key] = editor
            layout.addWidget(editor, row, 1)
        return frame

    def _populate(self) -> None:
        for key, editor in self._editors.items():
            value = (
                self._original.custom_values.get(UUID(key[7:]))
                if key.startswith("custom:")
                else getattr(self._original, key)
            )
            text = (
                value.isoformat()
                if isinstance(value, date)
                else ("" if value is None else str(value))
            )
            if isinstance(editor, QPlainTextEdit):
                editor.setPlainText(text)
            else:
                editor.setText(text)

    def _connect_changes(self) -> None:
        for editor in self._editors.values():
            editor.textChanged.connect(self._refresh_form_state)

    def _editor_text(self, key: str) -> str:
        editor = self._editors[key]
        return (
            editor.toPlainText()
            if isinstance(editor, QPlainTextEdit)
            else editor.text()
        )

    def _candidate_profile(self) -> PersonProfile:
        if self._delivery_only:
            return self._original
        custom_values = dict(self._original.custom_values)
        for definition in self._custom_fields:
            text = self._editor_text(f"custom:{definition.definition_id}").strip()
            value = _custom_value(definition, text)
            if value is None:
                custom_values.pop(definition.definition_id, None)
            else:
                custom_values[definition.definition_id] = value
        return PersonProfile(
            name=self._editor_text("name"),
            birth_date=_parse_optional_date(self._editor_text("birth_date")),
            school_type=_optional_text(self._editor_text("school_type")),
            school_name=_optional_text(self._editor_text("school_name")),
            grade=_optional_text(self._editor_text("grade")),
            # 이전 DB에 반 값이 있더라도 숨겨진 값은 수정 과정에서 보존한다.
            class_name=self._original.class_name,
            household_head_name=_optional_text(
                self._editor_text("household_head_name")
            ),
            relationship_to_child=_optional_text(
                self._editor_text("relationship_to_child")
            ),
            jurisdiction=_optional_text(self._editor_text("jurisdiction")),
            provider=self._original.provider,
            road_address=_optional_text(self._editor_text("road_address")),
            detail_address=_optional_text(self._editor_text("detail_address")),
            telephone=_optional_text(self._editor_text("telephone")),
            mobile_phone=_optional_text(self._editor_text("mobile_phone")),
            note=_optional_text(self._editor_text("note")),
            custom_values=custom_values,
        )

    def _refresh_form_state(self, *_args) -> None:
        cleared = self.cleared_fields()
        if cleared:
            labels = " · ".join(_FIELD_LABELS.get(key, key) for key in cleared)
            self._clear_notice.setText(f"삭제 예정  {labels}")
            self._clear_notice.show()
        else:
            self._clear_notice.hide()

        try:
            candidate = self._candidate_profile()
            delivery_settings = self._candidate_delivery_settings()
        except (TypeError, ValueError) as error:
            self._form_error.setText(str(error))
            self._form_error.show()
            self._save_button.setEnabled(False)
            return
        self._form_error.hide()
        self._update_delivery_heading()
        self._save_button.setEnabled(
            candidate != self._original
            or self._household_phone_changed(candidate)
            or delivery_settings != self._original_delivery_settings
            or self._delivery_copy_changed(delivery_settings)
        )

    def apply_mobile_to_household(self) -> bool:
        return not self._delivery_only and self.household_mobile.isChecked()

    def _household_phone_changed(self, candidate: PersonProfile) -> bool:
        return self.apply_mobile_to_household() and any(
            member.profile.mobile_phone != candidate.mobile_phone for member in self._household_members
        )

    def _save(self) -> None:
        try:
            candidate = self._candidate_profile()
            delivery_settings = self._candidate_delivery_settings()
        except (TypeError, ValueError) as error:
            self._form_error.setText(str(error))
            self._form_error.show()
            return
        if (candidate == self._original and not self._household_phone_changed(candidate)
                and delivery_settings == self._original_delivery_settings
                and not self._delivery_copy_changed(delivery_settings)):
            self._form_error.setText("변경된 내용이 없습니다.")
            self._form_error.show()
            return
        cleared = self.cleared_fields()
        if cleared:
            field_lines = "\n".join(
                f"• {_FIELD_LABELS.get(key, key)}" for key in cleared
            )
            answer = self._message_box.question(
                self,
                "기존 값을 비울까요?",
                "다음 기존 값이 저장된 명단에서 삭제됩니다.\n\n"
                f"{field_lines}\n\n"
                "계속하려면 '예'를 누르세요.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if self._household_phone_changed(candidate):
            answer = self._message_box.question(
                self, "가구 전체 핸드폰 변경",
                "다음 가구원의 기본 핸드폰을 같은 값으로 변경합니다.\n\n"
                + ", ".join(member.profile.name for member in self._household_members)
                + "\n\n새 핸드폰: " + (candidate.mobile_phone or "(빈칸으로 삭제)"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._accepted_profile = candidate
        self.accept()


class PersonProfileEditStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PersonProfileEditOutcome:
    status: PersonProfileEditStatus
    roster: RosterSnapshot | None = None
    backup_result: AutomaticBackupResult | None = None

    @property
    def completed(self) -> bool:
        return self.status is PersonProfileEditStatus.COMPLETED


class PersonProfileEditController:
    """Commit one accepted profile edit and immediately attempt a backup."""

    def __init__(
        self,
        session: DatabaseSession,
        *,
        actor_provider: Callable[[], RevisionActor] | None = None,
        dialog_type=PersonProfileEditDialog,
        message_box=QMessageBox,
        recorded_at_provider: Callable[[], datetime] | None = None,
        effective_date_provider: Callable[[], date] | None = None,
        revision_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if not isinstance(session, DatabaseSession):
            raise TypeError("session은 DatabaseSession이어야 합니다.")
        self._session = session
        self._actor_provider = actor_provider or current_windows_actor
        self._dialog_type = dialog_type
        self._message_box = message_box
        self._recorded_at_provider = recorded_at_provider or _utc_now
        self._effective_date_provider = effective_date_provider or date.today
        self._revision_id_factory = revision_id_factory

    def run(
        self,
        person_id: UUID,
        parent: QWidget | None = None,
    ) -> PersonProfileEditOutcome:
        return self._run(person_id, parent, delivery_only=False)

    def run_delivery_settings(
        self,
        person_id: UUID,
        parent: QWidget | None = None,
        *,
        service_day: date | None = None,
    ) -> PersonProfileEditOutcome:
        return self._run(person_id, parent, delivery_only=True, service_day=service_day)

    def _run(
        self,
        person_id: UUID,
        parent: QWidget | None,
        *,
        delivery_only: bool,
        service_day: date | None = None,
    ) -> PersonProfileEditOutcome:
        roster = self._session.database.current_roster()
        if roster is None:
            self._message_box.critical(
                parent,
                "사람 정보를 수정하지 못했습니다",
                "현재 명단이 없습니다. 기존 명단 가져오기를 먼저 완료해주세요.",
            )
            return PersonProfileEditOutcome(PersonProfileEditStatus.FAILED)
        try:
            person = roster.person(person_id)
        except (KeyError, TypeError) as error:
            self._message_box.critical(
                parent,
                "사람 정보를 수정하지 못했습니다",
                str(error),
            )
            return PersonProfileEditOutcome(PersonProfileEditStatus.FAILED)
        effective_on = service_day if delivery_only and service_day is not None else self._effective_date_provider()
        household_members = profile_household_members(roster, person_id, effective_on)
        effective_delivery_settings = roster.delivery_settings_for(person_id, on=effective_on)
        if self._dialog_type is PersonProfileEditDialog:
            dialog = self._dialog_type(
                person,
                parent,
                message_box=self._message_box,
                custom_fields=roster.custom_fields,
                household_members=household_members,
                effective_delivery_settings=effective_delivery_settings,
                member_delivery_settings={
                    member.person_id: roster.delivery_settings_for(member.person_id, on=effective_on)
                    for member in household_members
                },
                delivery_only=delivery_only,
                defaults_roster=roster,
            )
        else:
            dialog = self._dialog_type(person, parent, message_box=self._message_box)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return PersonProfileEditOutcome(PersonProfileEditStatus.CANCELLED)
        try:
            updated_profile = dialog.profile()
            updated_delivery_settings = getattr(dialog, "delivery_settings", lambda: None)()
            copy_delivery = getattr(dialog, "apply_delivery_to_household", lambda: False)()
            if copy_delivery and updated_delivery_settings is None:
                updated_delivery_settings = effective_delivery_settings
            if (
                updated_profile == person.profile
                and not getattr(dialog, "apply_mobile_to_household", lambda: False)()
                and updated_delivery_settings == person.delivery_settings
                and not copy_delivery
            ):
                return PersonProfileEditOutcome(PersonProfileEditStatus.CANCELLED)
            prepared = build_person_profile_revision(
                roster,
                person_id,
                updated_profile,
                actor=self._actor_provider(),
                recorded_at=self._recorded_at_provider(),
                effective_on=effective_on,
                household_mobile_phone=getattr(dialog, "apply_mobile_to_household", lambda: False)(),
                person_delivery_settings=updated_delivery_settings,
                delivery_settings_to_household=copy_delivery,
                revision_id=self._revision_id_factory(),
            )
            backup_result = self._session.append_revision(prepared.record)
        except Exception as error:
            self._message_box.critical(
                parent,
                "사람 정보를 저장하지 못했습니다",
                "운영 데이터에는 반영하지 않았습니다. 내용을 확인한 뒤 다시 시도해주세요.\n\n"
                + str(error),
            )
            return PersonProfileEditOutcome(PersonProfileEditStatus.FAILED)
        if backup_result.error is not None:
            self._message_box.warning(
                parent,
                "사람 정보는 저장됐지만 자동 백업에 실패했습니다",
                "변경 내용과 이력은 운영 데이터에 정상 저장되었습니다. "
                "백업 폴더 연결을 확인한 뒤 백업을 다시 실행해주세요.\n\n"
                + str(backup_result.error),
            )
        return PersonProfileEditOutcome(
            PersonProfileEditStatus.COMPLETED,
            prepared.snapshot,
            backup_result,
        )


def _parse_optional_date(text: str) -> date | None:
    value = text.strip()
    if not value:
        return None
    if len(value) == 8 and value.isdigit():
        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(
            "생년월일은 2015-03-02 또는 20150302 형식으로 입력해주세요."
        ) from error


def _optional_text(text: str) -> str | None:
    return text if text.strip() else None


def _custom_value(definition: CustomFieldDefinition, text: str):
    if not text:
        return None
    if definition.field_type is CustomFieldType.TEXT:
        return text
    if definition.field_type is CustomFieldType.NUMBER:
        try:
            return float(text) if "." in text else int(text)
        except ValueError as error:
            raise ValueError(f"{definition.name} 값은 숫자여야 합니다.") from error
    if definition.field_type is CustomFieldType.DATE:
        try:
            return date.fromisoformat(text)
        except ValueError as error:
            raise ValueError(
                f"{definition.name} 값은 YYYY-MM-DD 형식의 날짜여야 합니다."
            ) from error
    if text not in definition.choices:
        raise ValueError(f"{definition.name} 값이 선택 목록에 없습니다.")
    return text


def _utc_now() -> datetime:
    return datetime.now(UTC)
