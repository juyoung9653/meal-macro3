"""Revision-backed per-person exclusions from household column sharing."""

from .user_messages import user_error

from .dialog_workflow import DialogRequest, dialog_workflow

from .startup_loading import run_progress_task, save_revision
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QVBoxLayout)

from src.domain.household_shared import (
    HOUSEHOLD_SHARED_ID, HOUSEHOLD_SHARED_KIND, available_shared_column_keys,
    household_shared_components, shared_columns_from_state, shared_exceptions_from_state,
    build_household_shared_exceptions_revision,
)
from src.storage.database_session import committed_roster_after_save
from .household_shared_columns import HouseholdSharedColumnOutcome
from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts


class HouseholdSharedExceptionsDialog(QDialog):
    def __init__(self, roster, stored_state, service_day, labels=None, inactive_ids=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle("가구 공용 예외")
        self.resize(820, 620)
        self.setMinimumSize(620, 440)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self._draft = set(shared_exceptions_from_state(stored_state))
        self._person_id = None
        self._loading = False
        layout = QVBoxLayout(self)
        hint = QLabel("표시된 칼럼은 이 대상자가 개별 관리합니다. 체크를 해제하면 이후 공용 수정부터 다시 함께 적용하며 현재 값은 바꾸지 않습니다.\n새 예외는 명단의 공용 셀을 우클릭해 지정합니다.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.search = QLineEdit()
        self.search.setPlaceholderText("이름·생년월일·학교·연락처·대상자 ID 검색")
        self.search.setAccessibleName("대상자 검색")
        layout.addWidget(self.search)
        self.empty = QLabel("예외가 지정된 대상자가 없습니다. 명단의 공용 셀을 우클릭해 공용 예외를 지정하세요.")
        self.empty.setObjectName("muted")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        lists = QHBoxLayout()
        self.people = QListWidget()
        self.people.setAccessibleName("예외 대상자")
        self.people.setWordWrap(True)
        self.people.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.columns = QListWidget()
        self.columns.setAccessibleName("개별 관리할 칼럼")
        lists.addWidget(self.people, 1)
        lists.addWidget(self.columns, 1)
        layout.addLayout(lists, 1)
        labels = labels or {}
        enabled = shared_columns_from_state(stored_state)
        keys = available_shared_column_keys(roster)
        keys = tuple(key for key in keys if not key.startswith("status:")) + ("status:dinner", "status:breakfast", "status:lunch")
        for key in keys:
            components = household_shared_components(roster, key)
            fallback = {"address": "도로명주소", "detail_address": "상세주소", "sequence": "순서 (식종별)",
                        "delivery_settings": "배송 설정", "status:breakfast": "조식", "status:lunch": "중식", "status:dinner": "석식"}.get(key, components[0].label)
            label = labels.get(key, fallback)
            item = QListWidgetItem(label + (" · 공용 사용 중" if key in enabled else " · 공용 미사용"))
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.columns.addItem(item)
        identity_counts = {}
        for person in roster.persons:
            identity = (person.profile.name, person.profile.birth_date, person.profile.school_name, person.profile.mobile_phone)
            identity_counts[identity] = identity_counts.get(identity, 0) + 1
        for person in sorted(roster.persons, key=lambda p: (p.profile.name, str(p.person_id))):
            profile = person.profile
            identity = " · ".join(str(v) for v in (profile.birth_date, profile.school_name, profile.mobile_phone) if v)
            active = bool(roster.meals_for(person.person_id, on=service_day))
            duplicate = identity_counts[(profile.name, profile.birth_date, profile.school_name, profile.mobile_phone)] > 1
            suffix = f" · 식별 {str(person.person_id)[:8]}" if duplicate else ""
            item = QListWidgetItem(f"{profile.name} · {identity or '추가 정보 없음'}" + suffix + (" · 숨긴 명단" if person.person_id in inactive_ids else "" if active else " · 현재 미가입"))
            item.setToolTip(item.text() + f"\n대상자 ID: {person.person_id}")
            item.setData(Qt.ItemDataRole.UserRole, person.person_id)
            self.people.addItem(item)
        self.people.currentItemChanged.connect(self._select_person)
        self.columns.itemChanged.connect(self._change_column)
        self.search.textChanged.connect(self._filter)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        for button, text, style in ((QDialogButtonBox.StandardButton.Save, "저장", "primaryAction"), (QDialogButtonBox.StandardButton.Cancel, "취소", "quietAction")):
            self.buttons.button(button).setText(text)
            self.buttons.button(button).setObjectName(style)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.columns.setEnabled(False)
        self._filter("")

    def _select_person(self, item, _previous):
        self._person_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.columns.setEnabled(item is not None)
        self._loading = True
        for index in range(self.columns.count()):
            column = self.columns.item(index)
            checked = (self._person_id, column.data(Qt.ItemDataRole.UserRole)) in self._draft
            column.setHidden(not checked)
            flags = column.flags()
            if checked:
                flags |= Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            else:
                flags &= ~Qt.ItemFlag.ItemIsEnabled
                flags &= ~Qt.ItemFlag.ItemIsUserCheckable
            column.setFlags(flags)
            column.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self._loading = False

    def _change_column(self, item):
        if self._loading or self._person_id is None:
            return
        pair = (self._person_id, item.data(Qt.ItemDataRole.UserRole))
        if pair in self._draft and item.checkState() == Qt.CheckState.Unchecked:
            self._draft.discard(pair)
        elif pair not in self._draft:
            self._loading = True
            item.setCheckState(Qt.CheckState.Unchecked)
            self._loading = False
        self._filter(self.search.text())
        current = self.people.currentItem()
        if current is not None and current.data(Qt.ItemDataRole.UserRole) == self._person_id:
            self._select_person(current, current)

    def _filter(self, text):
        visible = []
        for index in range(self.people.count()):
            item = self.people.item(index)
            person_id = item.data(Qt.ItemDataRole.UserRole)
            has_exception = any(pid == person_id for pid, _key in self._draft)
            item.setHidden(
                not has_exception
                or text.casefold() not in item.toolTip().casefold()
            )
            if not item.isHidden():
                visible.append(item)
        self.empty.setText(
            "검색 조건에 맞는 예외 대상자가 없습니다."
            if self._draft and text else
            "예외가 지정된 대상자가 없습니다. 명단의 공용 셀을 우클릭해 공용 예외를 지정하세요."
        )
        self.empty.setVisible(not visible)
        current = self.people.currentItem()
        if current is None or current.isHidden():
            self.people.setCurrentItem(visible[0] if visible else None)
        if not visible:
            self._select_person(None, current)

    def selected_exceptions(self):
        return frozenset(self._draft)


class HouseholdSharedExceptionsController:
    def __init__(self, session, *, dialog_type=HouseholdSharedExceptionsDialog,
                 message_box=QMessageBox, actor_provider=current_windows_actor,
                 recorded_at_provider=lambda: datetime.now(UTC), revision_id_factory=uuid4):
        self._session = session
        self._dialog_type = dialog_type
        self._message_box = message_box
        self._actor_provider = actor_provider
        self._recorded_at_provider = recorded_at_provider
        self._revision_id_factory = revision_id_factory

    def set_exception(
        self, person_id, column_key, enabled, service_day, *, parent=None,
    ):
        def save():
            roster = self._session.database.current_roster()
            if roster is None:
                raise ValueError("현재 명단이 없습니다.")
            roster.person(person_id)
            snapshot = self._session.database.snapshot(roster.revision_id)
            stored = snapshot.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
            if column_key not in shared_columns_from_state(stored):
                raise ValueError("현재 가구 공용으로 사용하는 칼럼에서만 예외를 지정할 수 있습니다.")
            exceptions = set(shared_exceptions_from_state(stored))
            pair = (person_id, column_key)
            if enabled:
                exceptions.add(pair)
            else:
                exceptions.discard(pair)
            record = build_household_shared_exceptions_revision(
                roster, stored, exceptions, actor=self._actor_provider(),
                recorded_at=self._recorded_at_provider(), effective_on=service_day,
                revision_id=self._revision_id_factory(),
            )
            if record is None:
                return None, None
            backup = self._session.append_revision(record)
            return committed_roster_after_save(
                self._session, replace(roster, revision_id=record.metadata.revision_id)), backup

        try:
            updated_roster, backup = run_progress_task(
                save, parent=parent, title="공용 예외를 저장하는 중입니다",
                message="공용 예외 설정을 확인하고 변경 내용과 자동 백업을 저장하는 중입니다.",
            )
        except Exception as error:
            self._message_box.critical(
                parent, "가구 공용 예외를 저장하지 못했습니다", user_error(error),
            )
            return HouseholdSharedColumnOutcome()
        if backup is None:
            return HouseholdSharedColumnOutcome()
        if backup.warning_required:
            self._message_box.warning(
                parent, "가구 공용 예외는 저장됐지만 자동 백업에 실패했습니다",
                user_error(backup.error),
            )
        return HouseholdSharedColumnOutcome(updated_roster)

    @dialog_workflow
    def run(self, service_day, *, labels=None, inactive_ids=(), parent=None):
        try:
            roster = self._session.database.current_roster()
            if roster is None:
                raise ValueError("현재 명단이 없습니다.")
            snapshot = self._session.database.snapshot(roster.revision_id)
            stored = snapshot.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
            dialog = self._dialog_type(roster, stored, service_day, labels=labels, inactive_ids=inactive_ids, parent=parent)
            if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
                return HouseholdSharedColumnOutcome()
            current = self._session.database.current_roster()
            if current is None or current.revision_id != roster.revision_id:
                raise ValueError("설정창을 연 뒤 명단이 변경되었습니다. 창을 다시 열어주세요.")
            record = build_household_shared_exceptions_revision(
                roster, stored, dialog.selected_exceptions(), actor=self._actor_provider(),
                recorded_at=self._recorded_at_provider(), effective_on=service_day,
                revision_id=self._revision_id_factory(),
            )
            if record is None:
                return HouseholdSharedColumnOutcome()
            backup = save_revision(self._session, record, parent=parent)
        except Exception as error:
            self._message_box.critical(parent, "가구 공용 예외를 저장하지 못했습니다", user_error(error))
            return HouseholdSharedColumnOutcome()
        if backup.warning_required:
            self._message_box.warning(parent, "가구 공용 예외는 저장됐지만 자동 백업에 실패했습니다", user_error(backup.error))
        return HouseholdSharedColumnOutcome(committed_roster_after_save(
            self._session, replace(roster, revision_id=record.metadata.revision_id)))
