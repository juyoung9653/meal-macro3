"""Single entry point for choosing a generated document workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.revisions import RevisionSummary
from src.domain.roster import Meal

from .theme import application_stylesheet, register_application_fonts


class DocumentAction(StrEnum):
    """The four document jobs recognizable to center staff."""

    INVOICE = "invoice"
    CONFIRMATION = "confirmation"
    DELIVERY_REPORT = "delivery_report"
    DELIVERY_SUBSTITUTE = "delivery_substitute"

    @property
    def label(self) -> str:
        return _ACTION_COPY[self][0]

    @property
    def description(self) -> str:
        return _ACTION_COPY[self][1]


_ACTION_COPY = {
    DocumentAction.INVOICE: (
        "청구서",
        "한 달 식수와 단가를 계산해 청구용 Excel을 만듭니다.",
    ),
    DocumentAction.CONFIRMATION: (
        "확인서",
        "선택한 기간의 급식 제공 내역을 확인서로 만듭니다.",
    ),
    DocumentAction.DELIVERY_REPORT: (
        "일일식수",
        "배송 대상과 담당·순서를 날짜별 Excel로 정리합니다.",
    ),
    DocumentAction.DELIVERY_SUBSTITUTE: (
        "배송대타",
        "담당자가 바뀌는 날의 인수인계용 배송표를 만듭니다.",
    ),
}


@dataclass(frozen=True, slots=True)
class DocumentSetupRequest:
    """Chosen job and sealed roster revision, ready for period inputs."""

    action: DocumentAction
    source: RevisionSummary
    anchor_day: date
    anchor_meal: Meal

    def __post_init__(self) -> None:
        if not isinstance(self.action, DocumentAction):
            raise TypeError("문서 작업은 DocumentAction이어야 합니다.")
        if not isinstance(self.source, RevisionSummary):
            raise TypeError("문서 원본은 RevisionSummary여야 합니다.")
        if isinstance(self.anchor_day, datetime) or not isinstance(
            self.anchor_day,
            date,
        ):
            raise TypeError("문서 기본 날짜는 date여야 합니다.")
        if not isinstance(self.anchor_meal, Meal):
            raise TypeError("문서 기본 식종은 Meal이어야 합니다.")


class _DocumentActionCard(QPushButton):
    def __init__(
        self,
        action: DocumentAction,
        *,
        category: str,
        stamp: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.action = action
        self.setObjectName("documentActionCard")
        self.setProperty("category", category)
        self.setAccessibleName(action.label)
        self.setAccessibleDescription(action.description)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setMinimumHeight(108)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(13, 12, 14, 12)
        layout.setSpacing(13)
        stamp_label = QLabel(stamp)
        stamp_label.setObjectName(
            "documentSettlementStamp"
            if category == "settlement"
            else "documentRouteStamp"
        )
        stamp_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stamp_label.setFixedSize(58, 58)
        stamp_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
        )
        layout.addWidget(stamp_label)

        copy = QVBoxLayout()
        copy.setSpacing(4)
        title = QLabel(action.label)
        title.setObjectName("documentCardTitle")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        description = QLabel(action.description)
        description.setObjectName("documentCardDescription")
        description.setWordWrap(True)
        description.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
        )
        copy.addStretch(1)
        copy.addWidget(title)
        copy.addWidget(description)
        copy.addStretch(1)
        layout.addLayout(copy, 1)


class DocumentHubDialog(QDialog):
    """Choose one output from a quiet settlement/route dispatch desk."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._selected_action: DocumentAction | None = None
        self._cards: dict[DocumentAction, _DocumentActionCard] = {}
        register_application_fonts()
        self.setObjectName("documentHubDialog")
        self.setWindowTitle("문서 만들기")
        self.setModal(True)
        self.resize(850, 590)
        self.setMinimumSize(760, 540)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()

    @property
    def selected_action(self) -> DocumentAction:
        if self._selected_action is None:
            raise RuntimeError("만들 문서 종류를 아직 선택하지 않았습니다.")
        return self._selected_action

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 19)
        root.setSpacing(14)
        root.addWidget(self._build_header())

        trays = QHBoxLayout()
        trays.setSpacing(14)
        trays.addWidget(
            self._build_tray(
                "정산 문서",
                "식수·금액과 제공 내역",
                (
                    (DocumentAction.INVOICE, "청구"),
                    (DocumentAction.CONFIRMATION, "확인"),
                ),
                category="settlement",
            ),
            1,
        )
        trays.addWidget(
            self._build_tray(
                "배송 문서",
                "현장 배포와 인수인계",
                (
                    (DocumentAction.DELIVERY_REPORT, "식수"),
                    (DocumentAction.DELIVERY_SUBSTITUTE, "대타"),
                ),
                category="route",
            ),
            1,
        )
        root.addLayout(trays, 1)

        hint = QLabel(
            "문서를 고르면 다음 화면에서 기간과 사용할 변경번호를 확인합니다."
        )
        hint.setObjectName("documentHubHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("닫기")
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _build_header() -> QFrame:
        frame = QFrame()
        frame.setObjectName("documentHubHeader")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 15, 18, 16)
        layout.setSpacing(4)
        eyebrow = QLabel("문서 만들기 · 현재 명단")
        eyebrow.setObjectName("editEyebrow")
        title = QLabel("어떤 문서를 만들까요?")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel(
            "업무 이름을 한 번 누르면 해당 문서의 기간과 명단 기준을 확인합니다."
        )
        subtitle.setObjectName("workspaceSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(eyebrow)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        return frame

    def _build_tray(
        self,
        title: str,
        description: str,
        actions: tuple[tuple[DocumentAction, str], ...],
        *,
        category: str,
    ) -> QFrame:
        frame = QFrame()
        frame.setObjectName("documentTray")
        frame.setProperty("category", category)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(13, 12, 13, 13)
        layout.setSpacing(9)

        heading = QLabel(title)
        heading.setObjectName("documentTrayTitle")
        copy = QLabel(description)
        copy.setObjectName("documentTrayDescription")
        layout.addWidget(heading)
        layout.addWidget(copy)
        for action, stamp in actions:
            card = _DocumentActionCard(
                action,
                category=category,
                stamp=stamp,
            )
            card.clicked.connect(
                lambda _checked=False, selected=action: self._choose(selected)
            )
            self._cards[action] = card
            layout.addWidget(card, 1)
        return frame

    def _choose(self, action: DocumentAction) -> None:
        self._selected_action = action
        self.accept()
