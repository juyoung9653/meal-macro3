"""Edit the source field used for each generated document column."""

from __future__ import annotations

from .user_messages import user_error

from .dialog_workflow import DialogRequest, dialog_workflow

from .startup_loading import save_revision

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from src.domain.revisions import RevisionActor
from src.domain.roster import RosterSnapshot
from src.domain.invoice_settings import invoice_settings_from_snapshot
from src.domain.document_column_sources import AUTO_HISTORY
from src.templates.confirmation_fields import AUTO_CONFIRMATION, placeholder_mapping
from src.services.document_column_mappings import (
    MappingEditorSpec, build_mapping_revision, load_mapping_overrides, load_mapping_fallbacks,
    mapping_editor_spec,
)
from src.storage.database_session import (
    AutomaticBackupResult,
    DatabaseSession,
)

from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts


_PROFILES = (("급식", "급식"), ("행복도시락", "행복도시락"))
_DOCUMENTS = (("invoice", "청구서"), ("confirmation", "확인서"), ("daily_report", "일일식수 · 모든 식종 공용"))
_SCOPES = tuple((profile, document) for profile, _ in _PROFILES for document, _ in _DOCUMENTS)


class DocumentColumnMappingDialog(QDialog):
    """One scope at a time; switching asks before discarding unsaved edits."""

    def __init__(
        self,
        specs: Mapping[tuple[str, str], MappingEditorSpec],
        overrides: Mapping[tuple[str, str], Mapping[str, str]],
        parent: QWidget | None = None,
        *,
        message_box=QMessageBox,
        fallbacks: Mapping[tuple[str, str], Mapping[str, str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self._specs = specs
        self._overrides = overrides
        self._fallbacks = fallbacks or {}
        self._fallback_combos: dict[str, QComboBox] = {}
        self._message_box = message_box
        self._scope = _SCOPES[0]
        self._combos: dict[str, QComboBox] = {}
        self._indicators: dict[str, QLabel] = {}
        self._switching = False
        register_application_fonts()
        self.setWindowTitle("문서 항목 설정")
        self.setObjectName("documentColumnMappingDialog")
        self.setModal(True)
        self.resize(900, 650)
        self.setMinimumSize(630, 450)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()
        self._show_scope()

    @property
    def selected_profile(self) -> str:
        return self._scope[0]

    @property
    def document_kind(self) -> str:
        return self._scope[1]

    def desired_mapping(self) -> dict[str, str]:
        return {key: combo.currentData() for key, combo in self._combos.items()}

    def _initial_mapping(self, scope: tuple[str, str]) -> dict[str, str]:
        spec = self._specs[scope]
        overrides = self._overrides[scope]
        if scope[1] == "confirmation":
            overrides = placeholder_mapping(spec.defaults, overrides)
        selectable = spec.source_options if spec.selectable_source_options is None else spec.selectable_source_options
        return {
            key: source if source in selectable else ""
            for key in spec.targets if key not in spec.locked_targets
            for source in (overrides.get(key, spec.defaults.get(key, "")),)
        }

    def desired_fallbacks(self) -> dict[str, str]:
        return {key: combo.currentData() for key, combo in self._fallback_combos.items() if combo.currentData()}

    def _initial_fallbacks(self, scope):
        spec = self._specs[scope]
        selectable = spec.source_options if spec.selectable_source_options is None else spec.selectable_source_options
        values = self._fallbacks.get(scope, {})
        if scope[1] == "confirmation":
            values = placeholder_mapping({target: "" for target in spec.targets}, values)
        return {key: source for key, source in values.items()
                if key in spec.targets and key not in spec.locked_targets and source and source in selectable}

    def _dirty(self) -> bool:
        return (self.desired_mapping() != self._initial_mapping(self._scope)
                or self.desired_fallbacks() != self._initial_fallbacks(self._scope))

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)

        header = QFrame()
        header.setObjectName("documentTemplateHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(18, 14, 18, 15)
        eyebrow = QLabel("문서 출력 · 공통 설정")
        eyebrow.setObjectName("editEyebrow")
        title = QLabel("문서 항목 설정")
        title.setObjectName("workspaceTitle")
        explanation = QLabel(
            "각 대상자의 1순위 값이 비어 있으면 2순위 값을 사용합니다. 둘 다 비어 있으면 빈칸으로 출력합니다. 저장 후 새로 만드는 문서에 적용됩니다."
        )
        explanation.setObjectName("workspaceSubtitle")
        explanation.setWordWrap(True)
        header_layout.addWidget(eyebrow)
        header_layout.addWidget(title)
        header_layout.addWidget(explanation)
        root.addWidget(header)

        selectors = QHBoxLayout()
        selectors.setSpacing(12)
        for text, options, attribute in (
            ("양식", _PROFILES, "_profile_combo"),
            ("문서", _DOCUMENTS, "_document_combo"),
        ):
            label = QLabel(text)
            combo = QComboBox()
            combo.setMinimumWidth(150)
            for value, display in options:
                combo.addItem(display, value)
            combo.currentIndexChanged.connect(self._scope_changed)
            selectors.addWidget(label)
            selectors.addWidget(combo, 1)
            setattr(self, attribute, combo)
        root.addLayout(selectors)

        headings = QHBoxLayout()
        for index, text in enumerate(("출력 칼럼", "1순위", "2순위 · 1순위가 비면", "연결 상태")):
            label = QLabel(text)
            if index == 0:
                self._target_heading = label
            label.setObjectName("editEyebrow")
            if index == 3:
                label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            headings.addWidget(label, 2 if index in (1, 2) else 1)
        root.addLayout(headings)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(self._scroll, 1)

        note = QLabel("식수·청구금액·연번은 자동 계산이 기본입니다. 다른 항목이나 비움으로 바꿀 수 있습니다.")
        self._scope_note = note
        note.setObjectName("settingsHubNote")
        note.setWordWrap(True)
        root.addWidget(note)

        actions = QHBoxLayout()
        self._reset_button = QPushButton("이 문서 초기화")
        self._reset_button.setObjectName("quietAction")
        self._reset_button.clicked.connect(self._reset_scope)
        self._cancel_button = QPushButton("취소")
        self._cancel_button.setObjectName("quietAction")
        self._cancel_button.clicked.connect(self.reject)
        self._save_button = QPushButton("저장")
        self._save_button.setObjectName("primaryAction")
        self._save_button.clicked.connect(self.accept)
        actions.addWidget(self._reset_button)
        actions.addStretch(1)
        actions.addWidget(self._cancel_button)
        actions.addWidget(self._save_button)
        root.addLayout(actions)

    def _scope_changed(self, _index: int) -> None:
        if self._switching or not hasattr(self, "_document_combo"):
            return
        requested = (self._profile_combo.currentData(), self._document_combo.currentData())
        if requested not in self._specs:
            requested = (requested[0], "invoice")
        if requested == self._scope:
            return
        if self._dirty():
            answer = self._message_box.question(
                self,
                "저장하지 않은 변경",
                "현재 문서의 연결을 저장하지 않았습니다. 변경을 버리고 다른 문서로 이동할까요?",
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Discard:
                self._switching = True
                self._profile_combo.setCurrentIndex(self._profile_combo.findData(self._scope[0]))
                self._document_combo.setCurrentIndex(self._document_combo.findData(self._scope[1]))
                self._switching = False
                return
        self._scope = requested
        self._show_scope()

    def _show_scope(self) -> None:
        self._switching = True
        self._document_combo.setCurrentIndex(self._document_combo.findData(self._scope[1]))
        for index in range(self._document_combo.count()):
            self._document_combo.model().item(index).setEnabled((self._scope[0], self._document_combo.itemData(index)) in self._specs)
        self._switching = False
        spec = self._specs[self._scope]
        self._combos = {}
        self._fallback_combos = {}
        self._indicators = {}
        self.setMinimumHeight(520 if self.document_kind == "confirmation" else 450)
        self._reset_button.setText("이 문서 초기화")
        self._target_heading.setText("플레이스홀더" if self.document_kind == "confirmation" else "출력 칼럼")
        self._scope_note.setText(
            f"{self.selected_profile}의 모든 식종에 같은 연결을 사용합니다. 비고는 식종 변경 이력이 기본이며, 개인 비고나 다른 칼럼으로 바꿀 수 있습니다."
            if self.document_kind == "daily_report" else
            "번호는 가구원 순서입니다. 일부 시트에만 있는 항목도 연결할 수 있습니다."
            if self.document_kind == "confirmation" else
            "식수·청구금액·연번은 자동 계산이 기본입니다. 계산값을 다른 출력 칼럼에 연결하거나 명단 항목·비움으로 바꿀 수 있습니다.")
        self._scope_note.setToolTip(
            "child1·phone2처럼 끝의 숫자는 가구원 순서이며, 번호 없는 명단 항목은 첫 가구원의 값을 사용합니다. "
            "주소·관할동은 가구 공통 값이어야 하며 DB 가구 묶음은 유지합니다."
            if self.document_kind == "confirmation" else "")
        container = QWidget()
        grid = QGridLayout(container)
        grid.setContentsMargins(2, 2, 4, 2)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        values = self._initial_mapping(self._scope)
        for row, target in enumerate(spec.targets):
            label = QLabel(spec.target_labels[target])
            label.setWordWrap(True)
            absent = spec.missing_sheets.get(target, ())
            if absent:
                label.setToolTip("없는 시트: " + ", ".join(absent))
            grid.addWidget(label, row, 0)
            if target in spec.locked_targets:
                if self.document_kind == "confirmation":
                    reason = "가구 묶음 기준"
                elif target == "제공기관":
                    reason = "공통 제공기관 자동 적용"
                elif "식종 변경 이력" in spec.target_labels[target]:
                    reason = "식종 변경 이력 자동 적용 · 식수·청구서 설정에서 위치 변경"
                else:
                    reason = "자동 계산"
                automatic = QLabel(f"{reason} · 수정할 수 없음")
                automatic.setObjectName("settingsHubNote")
                automatic.setWordWrap(True)
                grid.addWidget(automatic, row, 1, 1, 3)
                continue
            field = QComboBox()
            field.setMinimumWidth(140)
            field.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            field.setMinimumContentsLength(8)
            field.setAccessibleName(f"{spec.target_labels[target]} 1순위")
            field.addItem("비움", "")
            selectable = spec.source_options if spec.selectable_source_options is None else spec.selectable_source_options
            if self.document_kind == "daily_report" and target != "note":
                selectable = tuple(source for source in selectable if source != AUTO_HISTORY)
            for source in selectable:
                if source:
                    field.addItem(spec.source_labels[source], source)
            field.setCurrentIndex(field.findData(values[target]))
            field.currentIndexChanged.connect(self._refresh_indicators)
            self._combos[target] = field
            indicator = QLabel()
            indicator.setObjectName("settingsHubNote")
            if self.document_kind == "confirmation":
                indicator.setMinimumWidth(145)
            self._indicators[target] = indicator
            secondary = QComboBox()
            secondary.setMinimumWidth(140)
            secondary.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            secondary.setMinimumContentsLength(8)
            secondary.setAccessibleName(f"{spec.target_labels[target]} 2순위")
            secondary.addItem("사용 안 함", "")
            for source in selectable:
                if source and source != AUTO_CONFIRMATION:
                    secondary.addItem(spec.source_labels[source], source)
            secondary.setCurrentIndex(secondary.findData(self._initial_fallbacks(self._scope).get(target, "")))
            secondary.currentIndexChanged.connect(self._refresh_indicators)
            self._fallback_combos[target] = secondary
            grid.addWidget(field, row, 1)
            grid.addWidget(secondary, row, 2)
            grid.addWidget(indicator, row, 3,
                alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(2, 2)
        grid.setColumnStretch(3, 1)
        grid.setRowStretch(len(spec.targets), 1)
        self._scroll.setWidget(container)
        self._refresh_indicators()

    def _refresh_indicators(self, _index: int = 0) -> None:
        spec = self._specs[self._scope]
        defaults = spec.defaults
        for target, combo in self._combos.items():
            state = "별도 연결" if combo.currentData() != defaults.get(target, "") or self._fallback_combos[target].currentData() else "기본 연결"
            absent = spec.missing_sheets.get(target, ())
            warning = (f"\n전체 시트에 있지 않음 ({len(spec.template_sheets) - len(absent)}/{len(spec.template_sheets)})"
                       if absent else "")
            self._indicators[target].setText(state + warning)
            self._indicators[target].setWordWrap(True)
            self._indicators[target].setToolTip("없는 시트: " + ", ".join(absent) if absent else "")
            combo.setToolTip(f"{combo.currentText()} · {state}")
            secondary = self._fallback_combos[target]
            secondary.setToolTip(secondary.currentText())

    def _reset_scope(self) -> None:
        defaults = self._specs[self._scope].defaults
        for target, combo in self._combos.items():
            index = combo.findData(defaults.get(target, ""))
            combo.setCurrentIndex(index if index >= 0 else 0)
            self._fallback_combos[target].setCurrentIndex(0)


class DocumentColumnMappingStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DocumentColumnMappingOutcome:
    status: DocumentColumnMappingStatus
    roster: RosterSnapshot | None = None
    backup_result: AutomaticBackupResult | None = None
    changed: bool = False

    @property
    def completed(self) -> bool:
        return self.status is DocumentColumnMappingStatus.COMPLETED


class DocumentColumnMappingController:
    def __init__(
        self,
        session: DatabaseSession,
        *,
        actor_provider: Callable[[], RevisionActor] | None = None,
        dialog_type=DocumentColumnMappingDialog,
        message_box=QMessageBox,
        recorded_at_provider: Callable[[], datetime] | None = None,
        revision_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._session = session
        self._actor_provider = actor_provider or current_windows_actor
        self._dialog_type = dialog_type
        self._message_box = message_box
        self._recorded_at_provider = recorded_at_provider or (lambda: datetime.now(UTC))
        self._revision_id_factory = revision_id_factory

    @dialog_workflow
    def run(self, parent: QWidget | None = None) -> DocumentColumnMappingOutcome:
        try:
            roster = self._session.database.current_roster()
            if roster is None:
                self._message_box.critical(parent, "출력 칼럼을 열지 못했습니다", "현재 명단이 없습니다. 명단을 먼저 가져와주세요.")
                return DocumentColumnMappingOutcome(DocumentColumnMappingStatus.FAILED)
            snapshot = self._session.database.snapshot(roster.revision_id)
            model = getattr(parent, "table_model", None)
            column_labels = None if model is None else {column.key: column.label for column in model.columns}
            specs = {
                scope: mapping_editor_spec(*scope, roster=roster, column_labels=column_labels,
                    history_column=invoice_settings_from_snapshot(snapshot).history_column(scope[0]))
                for scope in _SCOPES
            }
            overrides = {
                scope: load_mapping_overrides(snapshot, *scope)
                for scope in _SCOPES
            }
            fallbacks = {scope: load_mapping_fallbacks(snapshot, *scope) for scope in _SCOPES}
        except Exception as error:
            self._message_box.critical(parent, "출력 칼럼을 읽지 못했습니다", user_error(error))
            return DocumentColumnMappingOutcome(DocumentColumnMappingStatus.FAILED)

        dialog = self._dialog_type(specs, overrides, parent, message_box=self._message_box, fallbacks=fallbacks)
        if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
            return DocumentColumnMappingOutcome(DocumentColumnMappingStatus.CANCELLED, roster)
        try:
            prepared = build_mapping_revision(
                roster.revision_id,
                snapshot,
                dialog.selected_profile,
                dialog.document_kind,
                dialog.desired_mapping(),
                desired_fallbacks=dialog.desired_fallbacks(),
                actor=self._actor_provider(),
                recorded_at=self._recorded_at_provider(),
                revision_id=self._revision_id_factory(),
                roster=roster,
            )
            if prepared is None:
                return DocumentColumnMappingOutcome(DocumentColumnMappingStatus.COMPLETED, roster)
            backup = save_revision(self._session, prepared.record, parent=parent)
            updated = self._session.database.current_roster()
            if updated is None:
                raise RuntimeError("저장 뒤 현재 명단을 다시 읽지 못했습니다.")
        except Exception as error:
            self._message_box.critical(parent, "출력 칼럼을 저장하지 못했습니다", user_error(error))
            return DocumentColumnMappingOutcome(DocumentColumnMappingStatus.FAILED)
        if backup.warning_required:
            self._message_box.warning(
                parent,
                "출력 칼럼은 저장됐지만 자동 백업에 실패했습니다",
                "변경 내용은 저장되었습니다. 백업 폴더를 확인해주세요.\n\n" + user_error(backup.error),
            )
        return DocumentColumnMappingOutcome(DocumentColumnMappingStatus.COMPLETED, updated, backup, True)
