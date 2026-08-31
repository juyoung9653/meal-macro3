"""Reviewed person-profile editor and its revision/backup controller."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtWidgets import (
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
from src.storage import AutomaticBackupResult, DatabaseSession

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
    "provider": "제공기관",
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
            "provider",
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
    ) -> None:
        if not isinstance(person, Person):
            raise TypeError("person은 Person이어야 합니다.")
        super().__init__(parent)
        self._person = person
        self._original = person.profile
        self._message_box = message_box
        self._accepted_profile: PersonProfile | None = None
        self._editors: dict[str, QLineEdit | QPlainTextEdit] = {}
        register_application_fonts()
        self.setObjectName("personProfileEditDialog")
        self.setWindowTitle(f"{person.profile.name} · 사람 정보 수정")
        self.resize(760, 760)
        self.setMinimumSize(650, 620)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()
        self._populate()
        self._connect_changes()
        self._refresh_form_state()

    def profile(self) -> PersonProfile:
        if self._accepted_profile is None:
            raise RuntimeError("저장 확정 전에는 수정 profile을 가져올 수 없습니다.")
        return self._accepted_profile

    def cleared_fields(self) -> tuple[str, ...]:
        return tuple(
            key
            for key in _FIELD_LABELS
            if key != "name"
            and getattr(self._original, key) is not None
            and not self._editor_text(key).strip()
        )

    def _build_ui(self) -> None:
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
        scroll_layout.addStretch(1)
        scroll.setWidget(scroll_body)
        root.addWidget(scroll, 1)

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

    def _populate(self) -> None:
        for key, editor in self._editors.items():
            value = getattr(self._original, key)
            text = value.isoformat() if isinstance(value, date) else (value or "")
            if isinstance(editor, QPlainTextEdit):
                editor.setPlainText(text)
            else:
                editor.setText(text)

    def _connect_changes(self) -> None:
        for editor in self._editors.values():
            editor.textChanged.connect(self._refresh_form_state)

    def _editor_text(self, key: str) -> str:
        editor = self._editors[key]
        return editor.toPlainText() if isinstance(editor, QPlainTextEdit) else editor.text()

    def _candidate_profile(self) -> PersonProfile:
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
            provider=_optional_text(self._editor_text("provider")),
            road_address=_optional_text(self._editor_text("road_address")),
            detail_address=_optional_text(self._editor_text("detail_address")),
            telephone=_optional_text(self._editor_text("telephone")),
            mobile_phone=_optional_text(self._editor_text("mobile_phone")),
            note=_optional_text(self._editor_text("note")),
            custom_values=self._original.custom_values,
        )

    def _refresh_form_state(self, *_args) -> None:
        cleared = self.cleared_fields()
        if cleared:
            labels = " · ".join(_FIELD_LABELS[key] for key in cleared)
            self._clear_notice.setText(f"삭제 예정  {labels}")
            self._clear_notice.show()
        else:
            self._clear_notice.hide()

        try:
            candidate = self._candidate_profile()
        except (TypeError, ValueError) as error:
            self._form_error.setText(str(error))
            self._form_error.show()
            self._save_button.setEnabled(False)
            return
        self._form_error.hide()
        self._save_button.setEnabled(candidate != self._original)

    def _save(self) -> None:
        try:
            candidate = self._candidate_profile()
        except (TypeError, ValueError) as error:
            self._form_error.setText(str(error))
            self._form_error.show()
            return
        if candidate == self._original:
            self._form_error.setText("변경된 내용이 없습니다.")
            self._form_error.show()
            return
        cleared = self.cleared_fields()
        if cleared:
            field_lines = "\n".join(f"• {_FIELD_LABELS[key]}" for key in cleared)
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
        dialog = self._dialog_type(
            person,
            parent,
            message_box=self._message_box,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return PersonProfileEditOutcome(PersonProfileEditStatus.CANCELLED)
        try:
            prepared = build_person_profile_revision(
                roster,
                person_id,
                dialog.profile(),
                actor=self._actor_provider(),
                recorded_at=self._recorded_at_provider(),
                effective_on=self._effective_date_provider(),
                revision_id=self._revision_id_factory(),
            )
            backup_result = self._session.append_revision(prepared.record)
        except Exception as error:
            self._message_box.critical(
                parent,
                "사람 정보를 저장하지 못했습니다",
                "DB에는 반영하지 않았습니다. 내용을 확인한 뒤 다시 시도해주세요.\n\n"
                + str(error),
            )
            return PersonProfileEditOutcome(PersonProfileEditStatus.FAILED)
        if backup_result.error is not None:
            self._message_box.warning(
                parent,
                "사람 정보는 저장됐지만 자동 백업에 실패했습니다",
                "변경 내용과 이력은 DB에 정상 저장되었습니다. "
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


def _utc_now() -> datetime:
    return datetime.now(UTC)
