"""Plain settings entry point for DB-backed and computer-local settings."""

from __future__ import annotations

from enum import StrEnum

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .theme import application_stylesheet, register_application_fonts


class SettingsAction(StrEnum):
    """The settings jobs recognizable to center staff."""

    PROVIDER = "provider"
    EXPORT_PASSWORD = "export_password"
    DOCUMENT_TEMPLATE = "document_template"
    DOCUMENT_DIRECTORY = "document_directory"

    @property
    def label(self) -> str:
        return _SETTINGS_COPY[self][0]

    @property
    def description(self) -> str:
        return _SETTINGS_COPY[self][1]

    @property
    def scope_label(self) -> str:
        return _SETTINGS_COPY[self][2]


_SETTINGS_COPY = {
    SettingsAction.PROVIDER: (
        "공통 제공기관",
        "모든 대상자와 생성 문서에 사용할 제공기관을 한 번만 설정합니다.",
        "변경 이력에 기록",
    ),
    SettingsAction.EXPORT_PASSWORD: (
        "생성 Excel 비밀번호",
        "청구서·확인서 등 외부로 보내는 Excel의 공통 비밀번호를 관리합니다.",
        "변경 이력에 기록",
    ),
    SettingsAction.DOCUMENT_DIRECTORY: (
        "문서 저장 폴더",
        "새 문서를 모아둘 기본 폴더를 이 컴퓨터에 기억합니다.",
        "이 컴퓨터에만 저장",
    ),
    SettingsAction.DOCUMENT_TEMPLATE: (
        "청구서·확인서 양식",
        "문서를 만들 때 사용할 급식 또는 행복도시락 Excel 양식을 선택합니다.",
        "변경 이력에 기록",
    ),
}


class _SettingsActionCard(QPushButton):
    def __init__(
        self,
        action: SettingsAction,
        *,
        stamp: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.action = action
        self.setObjectName("settingsActionCard")
        self.setProperty("scope", action.value)
        self.setAccessibleName(action.label)
        self.setAccessibleDescription(action.description)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setMinimumHeight(112)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(15, 14, 16, 14)
        layout.setSpacing(15)

        stamp_label = QLabel(stamp)
        stamp_label.setObjectName("settingsActionStamp")
        stamp_label.setProperty("scope", action.value)
        stamp_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stamp_label.setFixedSize(62, 62)
        stamp_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(stamp_label)

        copy = QVBoxLayout()
        copy.setSpacing(4)
        scope = QLabel(action.scope_label)
        scope.setObjectName("settingsScopeLabel")
        scope.setProperty("scope", action.value)
        title = QLabel(action.label)
        title.setObjectName("settingsCardTitle")
        description = QLabel(action.description)
        description.setObjectName("settingsCardDescription")
        description.setWordWrap(True)
        for label in (scope, title, description):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        copy.addWidget(scope)
        copy.addWidget(title)
        copy.addWidget(description)
        layout.addLayout(copy, 1)


class SettingsHubDialog(QDialog):
    """Choose one settings job without mixing local paths and DB revisions."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._selected_action: SettingsAction | None = None
        self._cards: dict[SettingsAction, _SettingsActionCard] = {}
        register_application_fonts()
        self.setObjectName("settingsHubDialog")
        self.setWindowTitle("설정")
        self.setModal(True)
        self.resize(900, 760)
        self.setMinimumSize(800, 700)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()

    @property
    def selected_action(self) -> SettingsAction:
        if self._selected_action is None:
            raise RuntimeError("열 설정 항목을 아직 선택하지 않았습니다.")
        return self._selected_action

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 19)
        root.setSpacing(14)
        root.addWidget(self._build_header())

        cards = QGridLayout()
        cards.setSpacing(12)
        for index, (action, stamp) in enumerate((
            (SettingsAction.PROVIDER, "기관"),
            (SettingsAction.EXPORT_PASSWORD, "잠금"),
            (SettingsAction.DOCUMENT_TEMPLATE, "양식"),
            (SettingsAction.DOCUMENT_DIRECTORY, "폴더"),
        )):
            card = _SettingsActionCard(action, stamp=stamp)
            card.clicked.connect(
                lambda _checked=False, selected=action: self._choose(selected)
            )
            self._cards[action] = card
            cards.addWidget(card, index // 2, index % 2)
        cards.setColumnStretch(0, 1)
        cards.setColumnStretch(1, 1)
        root.addLayout(cards, 1)

        note = QLabel(
            "제공기관·비밀번호·문서 양식은 변경 이력에 남습니다. 문서 저장 폴더는 이 컴퓨터에만 저장합니다."
        )
        note.setObjectName("settingsHubNote")
        note.setWordWrap(True)
        note.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Maximum,
        )
        root.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("닫기")
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_header(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("settingsHubHeader")
        frame.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Maximum,
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 15, 18, 16)
        layout.setSpacing(4)
        eyebrow = QLabel("공통 설정 · 보관과 잠금")
        eyebrow.setObjectName("editEyebrow")
        title = QLabel("설정")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel("바꾸려는 항목을 고르면 해당 설정만 엽니다.")
        subtitle.setObjectName("workspaceSubtitle")
        layout.addWidget(eyebrow)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        return frame

    def _choose(self, action: SettingsAction) -> None:
        self._selected_action = action
        self.accept()
