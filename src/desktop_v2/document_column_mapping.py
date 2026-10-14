"""Edit the source field used for each generated document column."""

from __future__ import annotations

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
from src.services.document_column_mappings import (
    MappingEditorSpec, build_mapping_revision, load_mapping_overrides,
    mapping_editor_spec,
)
from src.storage.database_session import (
    AutomaticBackupResult,
    DatabaseSession,
)

from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts


_PROFILES = (("급식", "급식"), ("행복도시락", "행복도시락"))
_DOCUMENTS = (("invoice", "청구서"), ("confirmation", "확인서"))
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
    ) -> None:
        super().__init__(parent)
        self._specs = specs
        self._overrides = overrides
        self._message_box = message_box
        self._scope = _SCOPES[0]
        self._combos: dict[str, QComboBox] = {}
        self._indicators: dict[str, QLabel] = {}
        self._switching = False
        register_application_fonts()
        self.setWindowTitle("출력 칼럼 연결")
        self.setObjectName("documentColumnMappingDialog")
        self.setModal(True)
        self.resize(800, 650)
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
        return {
            key: overrides.get(key, spec.defaults.get(key, ""))
            for key in spec.targets if key not in spec.locked_targets
        }

    def _dirty(self) -> bool:
        return self.desired_mapping() != self._initial_mapping(self._scope)

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
        title = QLabel("출력 칼럼 연결")
        title.setObjectName("workspaceTitle")
        explanation = QLabel(
            "템플릿의 각 칼럼에 넣을 명단 항목을 선택하세요. 저장 후 새로 생성하는 문서에 적용되며, 가져온 명단은 바뀌지 않습니다."
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
        for text in ("템플릿 칼럼", "넣을 명단 항목"):
            label = QLabel(text)
            label.setObjectName("editEyebrow")
            headings.addWidget(label, 1)
        root.addLayout(headings)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(self._scroll, 1)

        note = QLabel("식수·청구금액·연번 등 계산 칼럼은 자동으로 채워지며 수정할 수 없습니다.")
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
        spec = self._specs[self._scope]
        self._combos = {}
        self._indicators = {}
        container = QWidget()
        grid = QGridLayout(container)
        grid.setContentsMargins(2, 2, 4, 2)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(8)
        values = self._initial_mapping(self._scope)
        for row, target in enumerate(spec.targets):
            label = QLabel(spec.target_labels[target])
            label.setWordWrap(True)
            grid.addWidget(label, row, 0)
            if target in spec.locked_targets:
                if self.document_kind == "confirmation":
                    reason = "가구 묶음 기준"
                elif target == "제공기관":
                    reason = "공통 제공기관 자동 적용"
                else:
                    reason = "자동 계산"
                automatic = QLabel(f"{reason} · 수정할 수 없음")
                automatic.setObjectName("settingsHubNote")
                grid.addWidget(automatic, row, 1)
                continue
            field = QComboBox()
            field.setMinimumWidth(220)
            field.addItem("비움", "")
            for source in spec.source_options:
                if source:
                    field.addItem(spec.source_labels[source], source)
            index = field.findData(values[target])
            if index < 0:
                # Preserve a historical custom field even if it is unavailable now.
                field.addItem(spec.source_labels.get(values[target], "사용할 수 없는 이전 항목"), values[target])
                index = field.count() - 1
            field.setCurrentIndex(index)
            field.currentIndexChanged.connect(self._refresh_indicators)
            self._combos[target] = field
            indicator = QLabel()
            indicator.setObjectName("settingsHubNote")
            self._indicators[target] = indicator
            right = QHBoxLayout()
            right.addWidget(field, 1)
            right.addWidget(indicator)
            grid.addLayout(right, row, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 2)
        grid.setRowStretch(len(spec.targets), 1)
        self._scroll.setWidget(container)
        self._refresh_indicators()

    def _refresh_indicators(self, _index: int = 0) -> None:
        defaults = self._specs[self._scope].defaults
        for target, combo in self._combos.items():
            self._indicators[target].setText(
                "별도 연결" if combo.currentData() != defaults.get(target, "") else "기본 연결"
            )

    def _reset_scope(self) -> None:
        defaults = self._specs[self._scope].defaults
        for target, combo in self._combos.items():
            index = combo.findData(defaults.get(target, ""))
            combo.setCurrentIndex(index if index >= 0 else 0)


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

    def run(self, parent: QWidget | None = None) -> DocumentColumnMappingOutcome:
        try:
            roster = self._session.database.current_roster()
            if roster is None:
                self._message_box.critical(parent, "출력 칼럼을 열지 못했습니다", "현재 명단이 없습니다. 명단을 먼저 가져와주세요.")
                return DocumentColumnMappingOutcome(DocumentColumnMappingStatus.FAILED)
            snapshot = self._session.database.snapshot(roster.revision_id)
            specs = {
                scope: mapping_editor_spec(*scope, roster=roster)
                for scope in _SCOPES
            }
            overrides = {
                scope: load_mapping_overrides(snapshot, *scope)
                for scope in _SCOPES
            }
        except Exception as error:
            self._message_box.critical(parent, "출력 칼럼을 읽지 못했습니다", str(error))
            return DocumentColumnMappingOutcome(DocumentColumnMappingStatus.FAILED)

        dialog = self._dialog_type(specs, overrides, parent, message_box=self._message_box)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return DocumentColumnMappingOutcome(DocumentColumnMappingStatus.CANCELLED, roster)
        try:
            prepared = build_mapping_revision(
                roster.revision_id,
                snapshot,
                dialog.selected_profile,
                dialog.document_kind,
                dialog.desired_mapping(),
                actor=self._actor_provider(),
                recorded_at=self._recorded_at_provider(),
                revision_id=self._revision_id_factory(),
                roster=roster,
            )
            if prepared is None:
                return DocumentColumnMappingOutcome(DocumentColumnMappingStatus.COMPLETED, roster)
            backup = self._session.append_revision(prepared.record)
            updated = self._session.database.current_roster()
            if updated is None:
                raise RuntimeError("저장 뒤 현재 명단을 다시 읽지 못했습니다.")
        except Exception as error:
            self._message_box.critical(parent, "출력 칼럼을 저장하지 못했습니다", str(error))
            return DocumentColumnMappingOutcome(DocumentColumnMappingStatus.FAILED)
        if backup.error is not None:
            self._message_box.warning(
                parent,
                "출력 칼럼은 저장됐지만 자동 백업에 실패했습니다",
                "변경 내용은 저장되었습니다. 백업 폴더를 확인해주세요.\n\n" + str(backup.error),
            )
        return DocumentColumnMappingOutcome(DocumentColumnMappingStatus.COMPLETED, updated, backup, True)
