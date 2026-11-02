"""Select household criteria and apply them to the existing roster atomically."""
from datetime import UTC, date, datetime
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QMessageBox, QScrollArea, QVBoxLayout, QWidget

from src.domain.household_grouping import build_grouping_revision, grouping_columns, grouping_options
from src.domain.mobile_household_merge import merge_mobile_households, split_households_by_criteria
from .dialog_workflow import DialogRequest, dialog_workflow, invoke
from .import_controller import current_windows_actor
from .mobile_household_merge import review_mobile_household_merge
from .startup_loading import save_revision
from .theme import application_stylesheet, register_application_fonts
from .user_messages import user_error


class HouseholdGroupingDialog(QDialog):
    def __init__(self, roster, snapshot, on, parent=None):
        super().__init__(parent)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("가구 묶기 기준")
        self.resize(600, 620)
        self._roster, self._snapshot, self._recorded_on = roster, snapshot, on
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        title = QLabel("같은 가구로 묶을 칼럼")
        title.setObjectName("workspaceTitle")
        root.addWidget(title)
        note = QLabel("선택한 칼럼 값이 모두 같으면 같은 가구로 묶습니다. 선택한 값 중 빈칸이 있으면 묶지 않습니다. 핸드폰·전화번호의 하이픈과 공백은 무시합니다.")
        note.setWordWrap(True)
        root.addWidget(note)
        current = set(grouping_columns(snapshot))
        aliases = {column.key: column.label for column in getattr(getattr(parent, "table_model", None), "columns", ())}
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        fields = QVBoxLayout(content)
        self.checks = {}
        for key, label in grouping_options(roster):
            check = QCheckBox(aliases.get(key, label))
            check.setChecked(key in current)
            self.checks[key] = check
            fields.addWidget(check)
            check.toggled.connect(self._refresh)
        # Preserve a missing stored selection visibly until the user removes it.
        for key in current - self.checks.keys():
            check = QCheckBox("삭제된 칼럼 · 선택을 해제해주세요")
            check.setChecked(True)
            self.checks[key] = check
            fields.addWidget(check)
            check.toggled.connect(self._refresh)
        fields.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.summary)
        note = QLabel("저장하면 전체 명단에서 기준값이 같은 사람을 합치고 다른 사람은 별도 가구로 나눕니다. 가구 연결일과 관계없이 즉시 적용하며, 이전 구성은 변경 이력에 보존합니다. 이후 불러오기·행 추가·정보 수정에도 이 기준을 사용합니다. 가구 공용 값이 다르면 저장 전에 확인합니다. 기사·식종별 가구 순서를 1부터 다시 매겨 명단 배송순서에도 함께 저장합니다.")
        note.setWordWrap(True)
        root.addWidget(note)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장·기존 명단에 적용")
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primaryAction")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)
        self._refresh()

    def selected_columns(self):
        return tuple(key for key, check in self.checks.items() if check.isChecked())

    def selected_date(self):
        return self._recorded_on

    def _refresh(self):
        if not hasattr(self, "buttons"):
            return
        selected = self.selected_columns()
        valid = bool(selected) and set(selected) <= {key for key, _ in grouping_options(self._roster)}
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(valid)
        if not valid:
            self.summary.setText("사용할 수 있는 칼럼을 하나 이상 선택해주세요.")
            return
        split, separated = split_households_by_criteria(self._roster, self.selected_date(), columns=selected, snapshot=self._snapshot)
        _, groups = merge_mobile_households(split, self.selected_date(), columns=selected, snapshot=self._snapshot)
        self.summary.setText((f"별도 가구로 나눌 대상: {len(separated)}명 · " if separated else "") + (f"합칠 대상: 기존 {sum(len(members) for members in groups.values())}가구 → {len(groups)}가구" if groups else "현재 기준으로 새로 합칠 가구가 없습니다."))


class HouseholdGroupingController:
    def __init__(self, session, *, dialog_type=HouseholdGroupingDialog, message_box=QMessageBox,
                 actor_provider=current_windows_actor, recorded_at_provider=None):
        self.session, self.dialog_type, self.messages = session, dialog_type, message_box
        self.actor_provider = actor_provider
        self.recorded_at_provider = recorded_at_provider or (lambda: datetime.now(UTC))

    @dialog_workflow
    def run(self, parent=None):
        try:
            roster = self.session.database.current_roster()
            snapshot = self.session.database.snapshot(roster.revision_id)
            on = getattr(getattr(parent, "table_model", None), "service_day", None) or date.today()
            dialog = self.dialog_type(roster, snapshot, on, parent)
            if (yield DialogRequest(dialog, self.session)) != QDialog.DialogCode.Accepted:
                return None
            record = build_grouping_revision(snapshot, roster, dialog.selected_columns(), dialog.selected_date(),
                actor=self.actor_provider(), recorded_at=self.recorded_at_provider(), revision_id=uuid4())
            record = yield from invoke(review_mobile_household_merge, self.session, record, parent)
            if record is None:
                return None
            if all(change.before == change.after for change in record.changes):
                self.messages.information(parent, "변경 없음", "가구 묶기 기준·가구 소속·배송순서가 현재와 같습니다.")
                return None
            backup = save_revision(self.session, record, parent=parent)
        except Exception as error:
            self.messages.critical(parent, "가구 묶기 기준을 저장하지 못했습니다", user_error(error))
            return None
        if backup.warning_required:
            self.messages.warning(parent, "저장 완료 · 자동 백업 실패", user_error(backup.error))
        return self.session.database.current_roster()
