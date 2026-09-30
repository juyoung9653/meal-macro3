"""Safe document-meal defaults and the meal-lane selection screen."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.revisions import format_revision_timestamp
from src.domain.roster import Meal

from .document_hub import DocumentAction
from .document_period import DocumentPeriodSelection
from .theme import application_stylesheet, register_application_fonts


def suggest_document_meals(
    action: DocumentAction,
    anchor_meal: Meal,
) -> tuple[Meal, ...]:
    """Use all meals for omission-sensitive documents, one lane for substitute."""

    if not isinstance(action, DocumentAction):
        raise TypeError("문서 작업은 DocumentAction이어야 합니다.")
    if not isinstance(anchor_meal, Meal):
        raise TypeError("문서 기본 식종은 Meal이어야 합니다.")
    if action is DocumentAction.DELIVERY_SUBSTITUTE:
        return (anchor_meal,)
    return tuple(Meal)


@dataclass(frozen=True, slots=True)
class DocumentMealSelection:
    """Validated document period plus the exact included meal lanes."""

    period: DocumentPeriodSelection
    meals: tuple[Meal, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.period, DocumentPeriodSelection):
            raise TypeError("문서 기간 선택이 올바르지 않습니다.")
        meals = tuple(self.meals)
        if not meals:
            raise ValueError("문서에 포함할 식종을 하나 이상 선택해야 합니다.")
        if any(not isinstance(meal, Meal) for meal in meals):
            raise TypeError("문서 식종은 Meal이어야 합니다.")
        if len(set(meals)) != len(meals):
            raise ValueError("같은 식종을 중복 선택할 수 없습니다.")
        canonical = tuple(meal for meal in Meal if meal in set(meals))
        if (
            self.period.action is DocumentAction.DELIVERY_SUBSTITUTE
            and len(canonical) != 1
        ):
            raise ValueError("배송대타는 식종 하나만 선택할 수 있습니다.")
        object.__setattr__(self, "meals", canonical)


class DocumentMealDialog(QDialog):
    """Confirm all meal lanes or one substitute lane before generation."""

    def __init__(
        self,
        period: DocumentPeriodSelection,
        parent: QWidget | None = None,
    ) -> None:
        if not isinstance(period, DocumentPeriodSelection):
            raise TypeError("문서 기간 선택이 올바르지 않습니다.")
        super().__init__(parent)
        self._period = period
        self._buttons_by_meal: dict[Meal, QAbstractButton] = {}
        self._cards: dict[Meal, QFrame] = {}
        self._exclusive_group: QButtonGroup | None = None
        self._defaults = suggest_document_meals(
            period.action,
            period.anchor_meal,
        )

        register_application_fonts()
        self.setObjectName("documentMealDialog")
        self.setWindowTitle(f"{period.action.label} 식종 선택")
        self.setModal(True)
        self.resize(780, 550)
        self.setMinimumSize(700, 510)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()
        self._refresh_state()

    @property
    def period(self) -> DocumentPeriodSelection:
        return self._period

    def selected_meals(self) -> tuple[Meal, ...]:
        return tuple(
            meal
            for meal in Meal
            if self._buttons_by_meal[meal].isChecked()
        )

    def selection(self) -> DocumentMealSelection:
        return DocumentMealSelection(self._period, self.selected_meals())

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 19)
        root.setSpacing(13)
        root.addWidget(self._build_header())
        root.addWidget(self._build_context_ticket())

        section = QFrame()
        section.setObjectName("editSection")
        section_layout = QVBoxLayout(section)
        section_layout.setContentsMargins(15, 13, 15, 15)
        section_layout.setSpacing(10)
        heading = QHBoxLayout()
        label = QLabel("문서에 포함할 식종")
        label.setObjectName("sectionLabel")
        heading.addWidget(label)
        heading.addStretch(1)
        if self._period.action is not DocumentAction.DELIVERY_SUBSTITUTE:
            select_all = QPushButton("전체 선택")
            select_all.setObjectName("linkAction")
            select_all.clicked.connect(self._select_all)
            clear_all = QPushButton("선택 해제")
            clear_all.setObjectName("linkAction")
            clear_all.clicked.connect(self._clear_all)
            heading.addWidget(select_all)
            heading.addWidget(clear_all)
        section_layout.addLayout(heading)

        help_text = QLabel(_meal_instruction(self._period))
        help_text.setObjectName("muted")
        help_text.setWordWrap(True)
        section_layout.addWidget(help_text)

        cards = QHBoxLayout()
        cards.setSpacing(10)
        if self._period.action is DocumentAction.DELIVERY_SUBSTITUTE:
            self._exclusive_group = QButtonGroup(self)
            self._exclusive_group.setExclusive(True)
        for meal in Meal:
            card, button = self._build_meal_card(meal)
            self._cards[meal] = card
            self._buttons_by_meal[meal] = button
            cards.addWidget(card, 1)
        section_layout.addLayout(cards, 1)
        root.addWidget(section, 1)

        self._summary = QLabel()
        self._summary.setObjectName("documentMealSummary")
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        self._error = QLabel()
        self._error.setObjectName("errorMessage")
        self._error.setWordWrap(True)
        self._error.hide()
        root.addWidget(self._error)

        self._dialog_buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._save_button = self._dialog_buttons.button(
            QDialogButtonBox.StandardButton.Ok
        )
        self._save_button.setText("선택한 식종 사용")
        self._save_button.setObjectName("primaryAction")
        self._dialog_buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText("뒤로")
        self._dialog_buttons.accepted.connect(self._accept_if_valid)
        self._dialog_buttons.rejected.connect(self.reject)
        root.addWidget(self._dialog_buttons)

    def _build_header(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("documentMealHeader")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 15, 18, 16)
        layout.setSpacing(4)
        eyebrow = QLabel("식종 선택 · 생성할 문서")
        eyebrow.setObjectName("editEyebrow")
        title = QLabel(f"{self._period.action.label} 식종 선택")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel(
            "선택한 식종만 계산과 출력에 포함합니다. 문서를 만들기 전에 다시 확인할 수 있습니다."
        )
        subtitle.setObjectName("workspaceSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(eyebrow)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        return frame

    def _build_context_ticket(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("documentMealContext")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(12)
        stamp = QLabel(_period_stamp(self._period))
        stamp.setObjectName("documentMealContextStamp")
        stamp.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stamp.setFixedWidth(76)
        layout.addWidget(stamp)
        copy = QVBoxLayout()
        copy.setSpacing(3)
        title = QLabel(f"{self._period.action.label} · {_period_text(self._period)}")
        title.setObjectName("sectionLabel")
        detail = QLabel(
            f"변경번호 {format_revision_timestamp(self._period.source.recorded_at)}  ·  "
            f"화면 식종 {self._period.anchor_meal.value}"
        )
        detail.setObjectName("documentMealContextDetail")
        copy.addWidget(title)
        copy.addWidget(detail)
        layout.addLayout(copy, 1)
        return frame

    def _build_meal_card(
        self,
        meal: Meal,
    ) -> tuple[QFrame, QAbstractButton]:
        card = QFrame()
        card.setObjectName("documentMealCard")
        card.setProperty("selected", meal in self._defaults)
        card.setProperty("anchor", meal is self._period.anchor_meal)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(13, 12, 13, 12)
        layout.setSpacing(7)

        if self._period.action is DocumentAction.DELIVERY_SUBSTITUTE:
            button: QAbstractButton = QRadioButton(meal.value)
            assert self._exclusive_group is not None
            self._exclusive_group.addButton(button)
        else:
            button = QCheckBox(meal.value)
        button.setObjectName("documentMealCheck")
        button.setChecked(meal in self._defaults)
        button.toggled.connect(self._refresh_state)
        layout.addWidget(button)

        if (
            self._period.action is DocumentAction.DELIVERY_SUBSTITUTE
            and meal is self._period.anchor_meal
        ):
            badge = QLabel("현재 화면 식종")
            badge.setObjectName("documentMealAnchorBadge")
            layout.addWidget(badge)
        description = QLabel(_MEAL_COPY[meal])
        description.setObjectName("documentMealDescription")
        description.setWordWrap(True)
        layout.addWidget(description)
        layout.addStretch(1)
        return card, button

    def _select_all(self) -> None:
        for button in self._buttons_by_meal.values():
            button.setChecked(True)

    def _clear_all(self) -> None:
        for button in self._buttons_by_meal.values():
            button.setChecked(False)

    def _refresh_state(self, _checked=None) -> None:
        selected = self.selected_meals() if self._buttons_by_meal else ()
        for meal, card in self._cards.items():
            card.setProperty("selected", meal in selected)
            card.style().unpolish(card)
            card.style().polish(card)
        if not hasattr(self, "_summary"):
            return
        if not selected:
            self._summary.clear()
            self._error.setText("문서에 포함할 식종을 하나 이상 선택해주세요.")
            self._error.show()
            self._save_button.setEnabled(False)
            return
        self._error.hide()
        labels = " · ".join(meal.value for meal in selected)
        if len(selected) == len(tuple(Meal)):
            labels += " 전체"
        self._summary.setText(
            f"{self._period.action.label} 포함 식종: {labels}"
        )
        self._save_button.setEnabled(True)

    def _accept_if_valid(self) -> None:
        try:
            self.selection()
        except (TypeError, ValueError):
            self._refresh_state()
            return
        self.accept()


_MEAL_COPY = {
    Meal.BREAKFAST: "등록된 조식 대상과 주말분 제공일을 계산합니다.",
    Meal.LUNCH: "등록된 중식 대상과 학교 일정을 계산합니다.",
    Meal.DINNER: "등록된 석식 대상과 날짜별 제공 상태를 계산합니다.",
}


def _meal_instruction(period: DocumentPeriodSelection) -> str:
    if period.action is DocumentAction.DELIVERY_SUBSTITUTE:
        return "배송대타는 담당자 한 명의 배송표를 만들기 때문에 식종 하나만 선택합니다."
    return "누락을 막기 위해 조식·중식·석식을 모두 선택했습니다. 필요 없는 식종만 해제하세요."


def _period_stamp(period: DocumentPeriodSelection) -> str:
    if period.starts_on.day == 1 and period.ends_on.day >= 28:
        return period.starts_on.strftime("%m월")
    return period.starts_on.strftime("%m.%d")


def _period_text(period: DocumentPeriodSelection) -> str:
    if period.starts_on == period.ends_on:
        return period.starts_on.strftime("%Y.%m.%d")
    return f"{period.starts_on:%Y.%m.%d} ~ {period.ends_on:%Y.%m.%d}"
