"""Safe document-meal defaults and the meal-lane selection screen."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import uuid4

from .document_navigation import DOCUMENT_BACK

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
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.daily_report_choices import ReportRemarkOptions, DailyReportChoices, build_daily_report_choices_revision, daily_report_choices_from_snapshot
from src.domain.revisions import format_revision_timestamp
from src.domain.roster import Meal
from src.domain.document_templates import (
    DocumentTemplateProfile,
    document_template_setting_from_snapshot,
)
from src.domain.delivery_substitute import SubstituteOptions

from .dialog_workflow import DialogRequest, dialog_workflow, launch
from .import_controller import current_windows_actor
from .startup_loading import save_revision
from .document_hub import DocumentAction
from .document_period import DocumentPeriodSelection
from .theme import application_stylesheet, register_application_fonts


def suggest_document_meals(
    action: DocumentAction,
    anchor_meal: Meal,
    starts_on: date | None = None,
    *,
    profile: DocumentTemplateProfile | None = None,
) -> tuple[Meal, ...]:
    """Use the delivery date for delivery documents and all meals for billing."""

    if not isinstance(action, DocumentAction):
        raise TypeError("문서 작업은 DocumentAction이어야 합니다.")
    if not isinstance(anchor_meal, Meal):
        raise TypeError("문서 기본 식종은 Meal이어야 합니다.")
    if (action is DocumentAction.DELIVERY_REPORT
            and profile is DocumentTemplateProfile.HAPPY_LUNCHBOX):
        return (Meal.BREAKFAST, Meal.DINNER)
    if action in (DocumentAction.DELIVERY_SUBSTITUTE, DocumentAction.DELIVERY_REPORT):
        day = starts_on or date.today()
        if day.weekday() < 4:
            return (Meal.DINNER,)
        if day.weekday() == 4:
            return (Meal.BREAKFAST,)
        return (anchor_meal,)
    return tuple(Meal)


@dataclass(frozen=True, slots=True)
class DocumentMealSelection:
    """Validated document period plus the exact included meal lanes."""

    period: DocumentPeriodSelection
    meals: tuple[Meal, ...]
    substitute_options: SubstituteOptions | None = None
    delivery_forms: tuple[str | None, ...] | None = None
    remark_options: ReportRemarkOptions = ReportRemarkOptions()

    def __post_init__(self) -> None:
        if not isinstance(self.period, DocumentPeriodSelection):
            raise TypeError("문서 기간 선택이 올바르지 않습니다.")
        if not isinstance(self.remark_options, ReportRemarkOptions):
            raise TypeError("비고 설정이 올바르지 않습니다.")
        if self.remark_options != ReportRemarkOptions() and self.period.action is not DocumentAction.DELIVERY_REPORT:
            raise ValueError("일일식수에서만 비고 설정을 사용할 수 있습니다.")
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
            and len(canonical) != 1 and not (len(canonical) == 2 and Meal.BREAKFAST in canonical)
        ):
            raise ValueError("배송목록은 석식 또는 중식에 조식을 함께 선택할 수 있습니다.")
        if self.period.action is DocumentAction.DELIVERY_SUBSTITUTE and len(canonical) == 2:
            canonical = tuple(meal for meal in canonical if meal is not Meal.BREAKFAST) + (Meal.BREAKFAST,)
        if self.substitute_options is not None:
            if not isinstance(self.substitute_options, SubstituteOptions):
                raise TypeError("배송대타 설정이 올바르지 않습니다.")
            if self.period.action is not DocumentAction.DELIVERY_SUBSTITUTE:
                raise ValueError("다른 문서에는 배송대타 설정을 사용할 수 없습니다.")
        if self.delivery_forms is not None:
            if self.period.action is not DocumentAction.DELIVERY_REPORT:
                raise ValueError("일일식수 문서에서만 배송 형태를 선택할 수 있습니다.")
            forms = tuple(self.delivery_forms)
            if not forms:
                raise ValueError("문서에 포함할 배송 형태를 하나 이상 선택해야 합니다.")
            if len(set(forms)) != len(forms):
                raise ValueError("같은 배송 형태를 중복 선택할 수 없습니다.")
            if any(form is not None and (
                not isinstance(form, str) or not form or form != " ".join(form.split())
            ) for form in forms):
                raise ValueError("배송 형태 선택 값이 올바르지 않습니다.")
            object.__setattr__(self, "delivery_forms", forms)
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
        self._template_profile = None
        self._buttons_by_meal: dict[Meal, QAbstractButton] = {}
        self._cards: dict[Meal, QFrame] = {}
        self._exclusive_group: QButtonGroup | None = None
        self._available_delivery_forms: tuple[str | None, ...] | None = None
        self._buttons_by_form: dict[str | None, QCheckBox] = {}
        self._excluded_forms = set()
        self._remark_options = ReportRemarkOptions()
        self._remark_columns = ()
        self._defaults = suggest_document_meals(
            period.action,
            period.anchor_meal,
            period.starts_on,
        )

        register_application_fonts()
        self.setObjectName("documentMealDialog")
        self.setWindowTitle(f"{period.action.label} 식종 선택")
        self.setModal(True)
        if period.action is DocumentAction.DELIVERY_REPORT:
            self.resize(780, 750)
            self.setMinimumSize(700, 730)
        else:
            self.resize(780, 550)
            self.setMinimumSize(700, 510)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()
        self._refresh_state()

    @property
    def period(self) -> DocumentPeriodSelection:
        return self._period

    def selected_meals(self) -> tuple[Meal, ...]:
        selected = tuple(
            meal
            for meal in Meal
            if self._buttons_by_meal[meal].isChecked()
        )
        if (self._template_profile is DocumentTemplateProfile.HAPPY_LUNCHBOX
                and getattr(self, "_include_breakfast", None) is not None
                and self._include_breakfast.isChecked() and selected):
            selected += (Meal.DINNER if selected == (Meal.BREAKFAST,) else Meal.BREAKFAST,)
        return selected

    def set_substitute_profile(self, profile: DocumentTemplateProfile | None) -> None:
        if self._period.action is not DocumentAction.DELIVERY_SUBSTITUTE:
            return
        self._template_profile = profile
        happy = profile is DocumentTemplateProfile.HAPPY_LUNCHBOX
        self._include_breakfast.setVisible(happy)
        self._include_breakfast.setChecked(happy)
        self._meal_help.setText(_meal_instruction(self._period, profile=profile))
        self._refresh_state()

    def set_report_choices(
        self, choices: DailyReportChoices, *,
        profile: DocumentTemplateProfile | None = None,
    ) -> None:
        if self._period.action is not DocumentAction.DELIVERY_REPORT:
            return
        # Keep exclusions, but start each new document with its profile's defaults.
        self._defaults = suggest_document_meals(
            self._period.action, self._period.anchor_meal, self._period.starts_on,
            profile=profile,
        )
        self._meal_help.setText(_meal_instruction(self._period, profile=profile))
        self._excluded_forms = set(choices.excluded_forms)
        self._remark_options = choices.remarks
        self._refresh_remark_summary()
        for meal, button in self._buttons_by_meal.items():
            button.setChecked(meal in self._defaults)
            description = self._cards[meal].findChild(QLabel, "documentMealDescription")
            description.setText(
                "선택일의 배송·일시중지 대상자를 1명씩 셉니다. 중지·미가입은 제외합니다."
                if profile is DocumentTemplateProfile.HAPPY_LUNCHBOX else _MEAL_COPY[meal]
            )
        for form, button in self._buttons_by_form.items():
            button.setChecked(form not in self._excluded_forms)
        self._refresh_state()

    def selected_report_choices(self) -> DailyReportChoices:
        selected = self.selection()
        # Keep exclusions absent from a historical source so a temporarily
        # missing form stays excluded when it reappears.
        excluded = self._excluded_forms - set(self._buttons_by_form)
        excluded.update(form for form, button in self._buttons_by_form.items() if not button.isChecked())
        return DailyReportChoices(selected.meals, tuple(excluded), self._remark_options)

    def set_available_delivery_forms(self, forms: tuple[str | None, ...]) -> None:
        """Configure report choices from the already selected historical source."""
        if self._period.action is not DocumentAction.DELIVERY_REPORT:
            return
        values = tuple(forms)
        if not values or None not in values or len(set(values)) != len(values):
            raise ValueError("배송 형태 목록이 올바르지 않습니다.")
        if any(value is not None and (
            not isinstance(value, str) or not value or value != " ".join(value.split())
        ) for value in values):
            raise ValueError("배송 형태 목록이 올바르지 않습니다.")
        self._available_delivery_forms = values
        for button in self._buttons_by_form.values():
            self._form_layout.removeWidget(button)
            button.deleteLater()
        self._buttons_by_form.clear()
        for form in values:
            button = QCheckBox("기본" if form is None else form)
            button.setObjectName("documentFormCheck")
            button.setMinimumHeight(22)
            button.setChecked(form not in self._excluded_forms)
            button.toggled.connect(self._refresh_state)
            self._buttons_by_form[form] = button
            self._form_layout.addWidget(button)
        self._form_panel.show()
        self._refresh_state()

    def selected_delivery_forms(self) -> tuple[str | None, ...] | None:
        if self._available_delivery_forms is None:
            return None
        return tuple(
            form for form in self._available_delivery_forms
            if self._buttons_by_form[form].isChecked()
        )

    def selection(self) -> DocumentMealSelection:
        return DocumentMealSelection(
            self._period, self.selected_meals(),
            delivery_forms=self.selected_delivery_forms(),
            remark_options=self._remark_options,
        )

    def set_report_columns(self, columns):
        self._remark_columns = tuple(columns)

    def _refresh_remark_summary(self):
        if hasattr(self, "_remark_summary"):
            options = self._remark_options
            self._remark_summary.setText(
                f"우선순위 칼럼 {len(options.columns)}개 · 별도 배송 형태 {len(options.separate_forms)}개 · "
                + ("기존 비고 위" if options.position == "above" else "기존 비고 아래")
            )

    @dialog_workflow
    def _edit_remarks(self):
        from .report_remarks import ReportRemarksDialog
        dialog = ReportRemarksDialog(self._remark_options, self._remark_columns,
                                     self._available_delivery_forms or (), self)
        if (yield DialogRequest(dialog)) == QDialog.DialogCode.Accepted:
            self._remark_options = dialog.options()
            self._refresh_remark_summary()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 19)
        root.setSpacing(13)
        root.addWidget(self._build_header())
        root.addWidget(self._build_context_ticket())

        section = QFrame()
        section.setObjectName("editSection")
        if self._period.action is DocumentAction.DELIVERY_REPORT:
            section.setMinimumHeight(180)
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

        self._meal_help = QLabel(_meal_instruction(self._period))
        self._meal_help.setObjectName("muted")
        self._meal_help.setWordWrap(True)
        section_layout.addWidget(self._meal_help)

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
        if self._period.action is DocumentAction.DELIVERY_SUBSTITUTE:
            self._include_breakfast = QCheckBox("조식 함께 표시 · 받으면 1, 안 받으면 빈칸")
            self._include_breakfast.setChecked(False)
            self._include_breakfast.hide()
            self._include_breakfast.toggled.connect(self._refresh_state)
            section_layout.addWidget(self._include_breakfast)
        root.addWidget(section, 1)

        if self._period.action is DocumentAction.DELIVERY_REPORT:
            self._form_panel = QFrame()
            self._form_panel.setObjectName("editSection")
            form_panel_layout = QVBoxLayout(self._form_panel)
            form_panel_layout.setContentsMargins(15, 10, 15, 10)
            form_panel_layout.setSpacing(5)
            title = QLabel("문서에 포함할 배송 형태")
            title.setObjectName("sectionLabel")
            form_panel_layout.addWidget(title)
            detail = QLabel("선택하지 않은 형태는 가구 수와 식수에서 모두 제외합니다.")
            detail.setObjectName("muted")
            form_panel_layout.addWidget(detail)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setMinimumHeight(100)
            scroll.setMaximumHeight(130)
            container = QWidget()
            self._form_layout = QVBoxLayout(container)
            self._form_layout.setContentsMargins(0, 0, 0, 0)
            scroll.setWidget(container)
            form_panel_layout.addWidget(scroll)
            self._form_panel.hide()
            root.addWidget(self._form_panel)
            self._extra_settings_toggle = QToolButton()
            self._extra_settings_toggle.setText("추가 설정")
            self._extra_settings_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self._extra_settings_toggle.setArrowType(Qt.ArrowType.RightArrow)
            self._extra_settings_toggle.setCheckable(True)
            self._extra_settings_toggle.setObjectName("linkAction")
            root.addWidget(self._extra_settings_toggle, 0, Qt.AlignmentFlag.AlignLeft)
            self._remark_panel = QWidget()
            remark_row = QHBoxLayout(self._remark_panel)
            remark_row.setContentsMargins(0, 0, 0, 0)
            remark_button = QPushButton("비고 설정…")
            remark_button.clicked.connect(lambda: launch(self._edit_remarks))
            remark_row.addWidget(remark_button)
            self._remark_summary = QLabel()
            self._remark_summary.setWordWrap(True)
            remark_row.addWidget(self._remark_summary, 1)
            root.addWidget(self._remark_panel)
            self._remark_panel.hide()
            self._extra_settings_toggle.toggled.connect(self._remark_panel.setVisible)
            self._extra_settings_toggle.toggled.connect(lambda checked:
                self._extra_settings_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow))
            self._refresh_remark_summary()

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
        self._dialog_buttons.rejected.connect(lambda: self.done(DOCUMENT_BACK))
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
        detail_text = f"변경번호 {format_revision_timestamp(self._period.source.recorded_at)}"
        if self._period.action is not DocumentAction.DELIVERY_SUBSTITUTE:
            detail_text += f"  ·  화면 식종 {self._period.anchor_meal.value}"
        detail = QLabel(detail_text)
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
        card.setProperty("anchor", self._period.action is not DocumentAction.DELIVERY_SUBSTITUTE
                         and meal is self._period.anchor_meal)
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
        if hasattr(self, "_include_breakfast"):
            breakfast_only = self._buttons_by_meal[Meal.BREAKFAST].isChecked()
            self._include_breakfast.setText(("석식" if breakfast_only else "조식") + " 함께 표시 · 받으면 1, 안 받으면 빈칸")
        selected = self.selected_meals() if self._buttons_by_meal else ()
        for meal, card in self._cards.items():
            card.setProperty("selected", meal in selected)
            card.style().unpolish(card)
            card.style().polish(card)
        if not hasattr(self, "_summary"):
            return
        selected_forms = self.selected_delivery_forms()
        if not selected or selected_forms == ():
            self._summary.clear()
            self._error.setText(
                "문서에 포함할 식종을 하나 이상 선택해주세요."
                if not selected else "문서에 포함할 배송 형태를 하나 이상 선택해주세요."
            )
            self._error.show()
            self._save_button.setEnabled(False)
            return
        self._error.hide()
        labels = " · ".join(meal.value for meal in selected)
        if len(selected) == len(tuple(Meal)):
            labels += " 전체"
        self._summary.setText(
            f"{self._period.action.label} 포함 식종: {labels}"
            + (
                "  ·  배송 형태: "
                + " · ".join("기본" if form is None else form for form in selected_forms)
                if selected_forms is not None else ""
            )
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


def _meal_instruction(
    period: DocumentPeriodSelection, *,
    profile: DocumentTemplateProfile | None = None,
) -> str:
    if period.action is DocumentAction.DELIVERY_SUBSTITUTE:
        if profile is DocumentTemplateProfile.HAPPY_LUNCHBOX:
            return "석식·중식은 조식을, 조식은 석식을 기본으로 함께 표시합니다. 행복도시락은 배송·일시중지이면 1, 중지·미가입이면 빈칸으로 표시합니다. 함께 표시를 해제하면 한 식종만 만듭니다."
        return "배송목록을 만들 식종 하나를 선택하세요. 선택한 날짜·식종의 실제 배송 대상을 표시합니다."
    if period.action is DocumentAction.DELIVERY_REPORT:
        if profile is DocumentTemplateProfile.HAPPY_LUNCHBOX:
            return "행복도시락 일일식수는 석식·조식을 기본 선택합니다. 중식도 필요하면 체크하세요. 식종은 변경할 수 있으며, 제외한 배송 형태는 저장된 설정을 사용합니다."
        return "기간 시작일 기준 월~목은 석식, 금요일은 조식을 기본 선택합니다. 식종은 변경할 수 있으며, 제외한 배송 형태는 저장된 설정을 사용합니다."
    return "누락을 막기 위해 조식·중식·석식을 모두 선택했습니다. 필요 없는 식종만 해제하세요."


def _period_stamp(period: DocumentPeriodSelection) -> str:
    if period.starts_on.day == 1 and period.ends_on.day >= 28:
        return period.starts_on.strftime("%m월")
    return period.starts_on.strftime("%m.%d")


def _period_text(period: DocumentPeriodSelection) -> str:
    if period.starts_on == period.ends_on:
        return period.starts_on.strftime("%Y.%m.%d")
    return f"{period.starts_on:%Y.%m.%d} ~ {period.ends_on:%Y.%m.%d}"


def load_daily_report_choices(session):
    return load_daily_report_setup(session)[0]


def load_daily_report_setup(session):
    """Read choices and the current output profile from the same DB snapshot."""
    roster = session.database.current_roster()
    if roster is None:
        raise ValueError("현재 명단이 없습니다.")
    snapshot = session.database.snapshot(roster.revision_id)
    setting = document_template_setting_from_snapshot(snapshot)
    return daily_report_choices_from_snapshot(snapshot), setting.profile if setting else None


def save_daily_report_choices(session, choices, *, parent=None):
    roster = session.database.current_roster()
    if roster is None:
        raise ValueError("현재 명단이 없습니다.")
    snapshot = session.database.snapshot(roster.revision_id)
    record = build_daily_report_choices_revision(snapshot, choices, actor=current_windows_actor(),
                                                 recorded_at=datetime.now(UTC), revision_id=uuid4())
    if record is None:
        return roster, None
    backup = save_revision(session, record, parent=parent)
    return session.database.current_roster(), backup
