"""Novice-facing file, mapping, and preview route for roster imports."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, TypeVar
from uuid import UUID, uuid4

from PySide6.QtCore import QDate, QEventLoop, QPointF, QSize, QTimer, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.domain.roster import Meal
from src.importing import (
    DateNormalization,
    HouseholdMatchCriterion,
    HouseholdMatchingRule,
    ImportMappingProfile,
    PhoneNormalization,
    ProfileField,
    StoredImportProfile,
    StoredHouseholdMatchingRule,
    WorkbookImportPasswordRequiredError,
    WorkbookImportPreview,
    WorkbookInspection,
    WorkbookStructureError,
    inspect_workbook,
    parse_workbook,
    proposed_profile,
)

from .theme import FOCUS, application_stylesheet, register_application_fonts


_FIELD_LABELS = {
    ProfileField.NAME: "성명 *",
    ProfileField.BIRTH_DATE: "생년월일 *",
    ProfileField.SCHOOL_TYPE: "취학유형",
    ProfileField.SCHOOL_NAME: "학교명",
    ProfileField.GRADE: "학년",
    ProfileField.HOUSEHOLD_HEAD_NAME: "세대주 성명",
    ProfileField.RELATIONSHIP_TO_CHILD: "아동과의 관계",
    ProfileField.JURISDICTION: "관할동",
    ProfileField.ROAD_ADDRESS: "주소 1순위",
    ProfileField.DETAIL_ADDRESS: "상세주소",
    ProfileField.TELEPHONE: "전화번호",
    ProfileField.MOBILE_PHONE: "핸드폰",
    ProfileField.NOTE: "비고",
}
_MAPPED_PROFILE_FIELDS = tuple(
    field
    for field in ProfileField
    if field not in (ProfileField.CLASS_NAME, ProfileField.PROVIDER)
)
_HEADER_ALIASES = {
    ProfileField.NAME: ("성명", "아동명", "이름"),
    ProfileField.BIRTH_DATE: ("생년월일", "생일"),
    ProfileField.SCHOOL_TYPE: ("취학유형", "학교유형"),
    ProfileField.SCHOOL_NAME: ("학교명", "학교"),
    ProfileField.GRADE: ("학년",),
    ProfileField.CLASS_NAME: ("반", "학급"),
    ProfileField.HOUSEHOLD_HEAD_NAME: ("세대주성명", "세대주"),
    ProfileField.RELATIONSHIP_TO_CHILD: ("아동과의관계", "관계"),
    ProfileField.JURISDICTION: ("관할동", "동"),
    ProfileField.PROVIDER: ("제공기관", "업체", "제공업체"),
    ProfileField.ROAD_ADDRESS: ("변경주소", "도로명주소", "주소"),
    ProfileField.DETAIL_ADDRESS: ("상세주소",),
    ProfileField.TELEPHONE: ("전화번호", "전화"),
    ProfileField.MOBILE_PHONE: ("핸드폰", "휴대전화", "휴대폰"),
    ProfileField.NOTE: ("비고", "메모"),
}
_ROUTE_STOPS = ("파일 선택", "열 연결 확인", "반영 전 미리보기", "최종 검토")
_BLANK_TOKEN = "(빈칸)"
_HOUSEHOLD_CRITERION_LABELS = {
    HouseholdMatchCriterion.MOBILE_PHONE: "핸드폰",
    HouseholdMatchCriterion.HOUSEHOLD_HEAD_NAME: "세대주 성명",
    HouseholdMatchCriterion.ADDRESS: "주소",
    HouseholdMatchCriterion.TELEPHONE: "전화번호",
}
_TaskResult = TypeVar("_TaskResult")


@dataclass(frozen=True, slots=True)
class ImportWizardSelection:
    preview: WorkbookImportPreview
    effective_on: date
    existing_profile: StoredImportProfile | None
    household_rule: HouseholdMatchingRule
    existing_household_rule: StoredHouseholdMatchingRule | None


class RosterImportWizard(QDialog):
    """Keep users on a guided route until a valid preview is ready for review."""

    def __init__(
        self,
        *,
        existing_profiles: tuple[StoredImportProfile, ...] = (),
        existing_household_rule: StoredHouseholdMatchingRule | None = None,
        initial_directory: str | Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if any(not isinstance(item, StoredImportProfile) for item in existing_profiles):
            raise TypeError("existing_profiles에는 StoredImportProfile만 허용됩니다.")
        if existing_household_rule is not None and not isinstance(
            existing_household_rule,
            StoredHouseholdMatchingRule,
        ):
            raise TypeError(
                "existing_household_rule은 StoredHouseholdMatchingRule 또는 None이어야 합니다."
            )
        register_application_fonts()
        self.setObjectName("databaseAccessDialog")
        self.setWindowTitle("기존 Excel 명단 가져오기")
        self.setModal(True)
        self.setStyleSheet(application_stylesheet())
        self.resize(1120, 780)
        self.setMinimumSize(920, 660)
        self._existing_profiles = tuple(existing_profiles)
        self._existing_household_rule = existing_household_rule
        self._initial_directory = (
            "" if initial_directory is None else str(Path(initial_directory))
        )
        self._inspection: WorkbookInspection | None = None
        self._preview: WorkbookImportPreview | None = None
        self._suggested_profile: StoredImportProfile | None = None
        self._selection: ImportWizardSelection | None = None
        self._profile_id = uuid4()
        self._household_rule_id = uuid4()
        self._loading_structure = False
        self._mapping_initialized = False
        self._mapping_combos: dict[ProfileField, QComboBox] = {}
        self._household_criterion_checkboxes: dict[
            HouseholdMatchCriterion,
            QPushButton,
        ] = {}
        self._route_labels: list[QLabel] = []
        self._busy = False
        self._owns_wait_cursor = False
        self._build_ui()
        self._apply_household_rule(existing_household_rule)
        self._meal_mode_changed()
        self._set_page(0)

    @property
    def inspection(self) -> WorkbookInspection | None:
        return self._inspection

    @property
    def preview(self) -> WorkbookImportPreview | None:
        return self._preview

    def selection(self) -> ImportWizardSelection:
        if self._selection is None:
            raise ValueError("가져오기 미리보기가 아직 확정되지 않았습니다.")
        return self._selection

    def _build_ui(self) -> None:
        shell = QHBoxLayout(self)
        shell.setContentsMargins(18, 18, 18, 18)
        shell.setSpacing(0)
        shell.addWidget(self._build_rail())

        card = QFrame()
        card.setObjectName("startupCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(26, 22, 26, 20)
        card_layout.setSpacing(12)
        self.page_stack = QStackedWidget()
        self.page_stack.addWidget(self._build_file_page())
        self.page_stack.addWidget(self._build_mapping_page())
        self.page_stack.addWidget(self._build_preview_page())
        card_layout.addWidget(self.page_stack, 1)
        card_layout.addWidget(self._build_busy_panel())

        self.error_label = QLabel()
        self.error_label.setObjectName("errorMessage")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        card_layout.addWidget(self.error_label)

        actions = QHBoxLayout()
        self.cancel_button = QPushButton("취소")
        self.cancel_button.setObjectName("quietAction")
        self.cancel_button.clicked.connect(self.reject)
        self.back_button = QPushButton("이전")
        self.back_button.setObjectName("quietAction")
        self.back_button.clicked.connect(self._go_back)
        self.next_button = QPushButton()
        self.next_button.setObjectName("primaryAction")
        self.next_button.setDefault(True)
        self.next_button.clicked.connect(self._go_next)
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        actions.addWidget(self.back_button)
        actions.addWidget(self.next_button)
        card_layout.addLayout(actions)
        shell.addWidget(card, 1)

    def _build_busy_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("importBusyPanel")
        panel.setVisible(False)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 10, 14, 11)
        layout.setSpacing(4)
        self.busy_title = QLabel()
        self.busy_title.setObjectName("importBusyTitle")
        self.busy_detail = QLabel()
        self.busy_detail.setObjectName("importBusyDetail")
        self.busy_detail.setWordWrap(True)
        self.busy_progress = QProgressBar()
        self.busy_progress.setObjectName("importBusyProgress")
        self.busy_progress.setRange(0, 0)
        self.busy_progress.setTextVisible(False)
        layout.addWidget(self.busy_title)
        layout.addWidget(self.busy_detail)
        layout.addWidget(self.busy_progress)
        self.busy_panel = panel
        return panel

    def _build_rail(self) -> QFrame:
        rail = QFrame()
        rail.setObjectName("startupRail")
        rail.setFixedWidth(230)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(24, 25, 22, 25)
        product = QLabel("MEAL ROUTE · IMPORT")
        product.setObjectName("startupProduct")
        layout.addWidget(product)
        layout.addSpacing(14)
        title = QLabel("기존 명단을\n안전하게 옮깁니다")
        title.setObjectName("startupRailTitle")
        layout.addWidget(title)
        copy = QLabel(
            "원본 Excel은 그대로 두고, 확인한 열의 값만 암호화 DB에 저장합니다."
        )
        copy.setObjectName("startupRailCopy")
        copy.setWordWrap(True)
        layout.addWidget(copy)
        layout.addSpacing(24)
        for index, stop in enumerate(_ROUTE_STOPS):
            row = QHBoxLayout()
            row.setSpacing(9)
            dot = QLabel("●")
            dot.setObjectName("routeDot")
            dot.setFixedWidth(17)
            row.addWidget(dot)
            label = QLabel(stop)
            label.setObjectName("routeStop")
            self._route_labels.append(label)
            row.addWidget(label, 1)
            layout.addLayout(row)
            if index != len(_ROUTE_STOPS) - 1:
                line_row = QHBoxLayout()
                line_row.setContentsMargins(7, 0, 0, 0)
                line = QFrame()
                line.setObjectName("routeLine")
                line.setFixedHeight(18)
                line_row.addWidget(line)
                line_row.addStretch(1)
                layout.addLayout(line_row)
        layout.addStretch(1)
        footer = QLabel("원본 파일 전체는 DB에 복사하지 않습니다")
        footer.setObjectName("startupRailCopy")
        footer.setWordWrap(True)
        layout.addWidget(footer)
        return rail

    def _page_heading(self, title: str, subtitle: str) -> QVBoxLayout:
        layout = QVBoxLayout()
        heading = QLabel(title)
        heading.setObjectName("startupTitle")
        layout.addWidget(heading)
        description = QLabel(subtitle)
        description.setObjectName("startupSubtitle")
        description.setWordWrap(True)
        layout.addWidget(description)
        return layout

    def _build_file_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(
            self._page_heading(
                "1. 기존 Excel 파일 선택",
                "명단이 들어 있는 시트와 헤더 위치를 먼저 확인합니다. 파일은 수정하지 않습니다.",
            )
        )
        form = QFormLayout()
        form.setVerticalSpacing(10)
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setObjectName("importPathEdit")
        self.path_edit.setPlaceholderText(".xlsx 또는 .xlsm 파일")
        self.path_edit.textChanged.connect(self._invalidate_structure)
        browse = QPushButton("파일 선택")
        browse.setObjectName("browseAction")
        browse.clicked.connect(self._browse_source)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        form.addRow("Excel 파일 *", path_row)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("파일에 암호가 있을 때만 입력")
        form.addRow("Excel 파일 비밀번호", self.password_edit)

        self.sheet_combo = QComboBox()
        self.sheet_combo.setEnabled(False)
        self.sheet_combo.currentIndexChanged.connect(self._sheet_changed)
        form.addRow("명단 시트 *", self.sheet_combo)

        row_positions = QHBoxLayout()
        self.header_row_spin = QSpinBox()
        self.header_row_spin.setRange(1, 500)
        self.header_row_spin.setValue(1)
        self.header_row_spin.valueChanged.connect(self._invalidate_layout_only)
        self.data_start_row_spin = QSpinBox()
        self.data_start_row_spin.setRange(2, 50001)
        self.data_start_row_spin.setValue(2)
        self.data_start_row_spin.valueChanged.connect(self._invalidate_layout_only)
        row_positions.addWidget(QLabel("헤더 행"))
        row_positions.addWidget(self.header_row_spin)
        row_positions.addSpacing(18)
        row_positions.addWidget(QLabel("데이터 시작 행"))
        row_positions.addWidget(self.data_start_row_spin)
        row_positions.addStretch(1)
        self.inspect_button = QPushButton("선택한 위치 미리보기")
        self.inspect_button.setObjectName("browseAction")
        self.inspect_button.clicked.connect(self._inspect_source)
        row_positions.addWidget(self.inspect_button)
        form.addRow("표 위치 *", row_positions)
        layout.addLayout(form)

        self.file_status = QLabel("파일을 선택한 뒤 미리보기를 눌러주세요.")
        self.file_status.setObjectName("muted")
        self.file_status.setWordWrap(True)
        layout.addWidget(self.file_status)
        self.raw_table = QTableWidget(0, 0)
        self.raw_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.raw_table.setAlternatingRowColors(True)
        self.raw_table.verticalHeader().setVisible(False)
        layout.addWidget(self.raw_table, 1)
        return page

    def _build_mapping_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(
            self._page_heading(
                "2. Excel 열과 명단 항목 연결",
                "왼쪽은 DB에 저장할 항목이고, 오른쪽은 선택한 Excel 열입니다. * 항목만 필수입니다.",
            )
        )
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(2, 2, 10, 2)
        identity_form = QFormLayout()
        self.profile_name_edit = QLineEdit("기존 Excel 명단")
        self.source_kind_combo = QComboBox()
        self.source_kind_combo.addItem("기존 운영명단", "roster-xlsx")
        self.source_kind_combo.addItem("정부 조사표", "survey-xlsx")
        identity_form.addRow("설정 이름 *", self.profile_name_edit)
        identity_form.addRow("원본 종류 *", self.source_kind_combo)
        body_layout.addLayout(identity_form)
        body_layout.addWidget(self._section("인적사항 열"))
        fields_form = QFormLayout()
        for field in _MAPPED_PROFILE_FIELDS:
            combo = QComboBox()
            combo.setObjectName(f"mapping_{field.value}")
            combo.currentIndexChanged.connect(self._mapping_changed)
            self._mapping_combos[field] = combo
            fields_form.addRow(_FIELD_LABELS[field], combo)
            if field is ProfileField.ROAD_ADDRESS:
                self.address_fallback_combo = QComboBox()
                self.address_fallback_combo.setObjectName("mapping_road_address_fallback")
                self.address_fallback_combo.currentIndexChanged.connect(
                    self._mapping_changed
                )
                fields_form.addRow("주소 2순위", self.address_fallback_combo)
        self.provider_edit = QLineEdit()
        self.provider_edit.setObjectName("manualProviderInput")
        self.provider_edit.setPlaceholderText("예: 분당구 급식센터")
        self.provider_edit.setClearButtonEnabled(True)
        self.provider_edit.setToolTip(
            "여기에 적은 제공기관을 이번에 가져오는 모든 사람에게 동일하게 적용합니다."
        )
        self.provider_edit.textChanged.connect(self._mapping_changed)
        fields_form.addRow("제공기관 직접 입력", self.provider_edit)
        body_layout.addLayout(fields_form)

        body_layout.addWidget(self._section("가구 묶기 기준"))
        household_copy = QLabel(
            "체크한 값이 모두 입력되어 있고 모두 같은 사람만 한 가구로 제안합니다. "
            "빈칸끼리는 같은 값으로 보지 않으며, 최종 검토에서 사람별로 분리할 수 있습니다."
        )
        household_copy.setObjectName("muted")
        household_copy.setWordWrap(True)
        body_layout.addWidget(household_copy)
        household_options = QHBoxLayout()
        household_options.setSpacing(18)
        for criterion, label in _HOUSEHOLD_CRITERION_LABELS.items():
            checkbox = QPushButton(label)
            checkbox.setCheckable(True)
            checkbox.setObjectName("householdCriterionCheck")
            checkbox.setProperty("criterion", criterion.value)
            checkbox.setIcon(_household_criterion_icon(False))
            checkbox.setIconSize(QSize(18, 18))
            checkbox.setAccessibleName(f"가구 묶기 기준 {label}")
            checkbox.setToolTip(
                f"{label} 값이 비어 있지 않고 다른 선택 기준과 함께 모두 같을 때만 묶습니다."
            )
            checkbox.toggled.connect(
                lambda checked, control=checkbox: control.setIcon(
                    _household_criterion_icon(checked)
                )
            )
            self._household_criterion_checkboxes[criterion] = checkbox
            household_options.addWidget(checkbox)
        household_options.addStretch(1)
        body_layout.addLayout(household_options)

        body_layout.addWidget(self._section("식종과 배송 정보"))
        meal_guide = QFrame()
        meal_guide.setObjectName("mealFormatGuide")
        meal_guide_layout = QVBoxLayout(meal_guide)
        meal_guide_layout.setContentsMargins(12, 9, 12, 10)
        meal_guide_layout.setSpacing(3)
        meal_guide_title = QLabel("내 Excel에서 식종이 적힌 모양을 보고 하나를 고르세요")
        meal_guide_title.setObjectName("mealFormatGuideTitle")
        meal_guide_examples = QLabel(
            "• ‘조식’, ‘중식’, ‘석식’ 열이 따로 있고 각 칸이 1 또는 0 → 개별 여부 열\n"
            "• ‘식종’ 열 하나의 셀에 ‘조식 / 중식’처럼 적힘 → 식종 열 하나"
        )
        meal_guide_examples.setObjectName("mealFormatGuideExample")
        meal_guide_examples.setWordWrap(True)
        meal_guide_layout.addWidget(meal_guide_title)
        meal_guide_layout.addWidget(meal_guide_examples)
        body_layout.addWidget(meal_guide)
        meal_form = QFormLayout()
        self.meal_mode_combo = QComboBox()
        self.meal_mode_combo.addItem(
            "조식·중식·석식 여부 열이 각각 있음 (1/0)",
            "flags",
        )
        self.meal_mode_combo.addItem(
            "‘식종’ 열 하나에 조식·중식·석식이 적혀 있음",
            "combined",
        )
        self.meal_mode_combo.currentIndexChanged.connect(self._meal_mode_changed)
        meal_form.addRow("내 Excel의 식종 표기 *", self.meal_mode_combo)
        self.breakfast_combo = QComboBox()
        self.lunch_combo = QComboBox()
        self.dinner_combo = QComboBox()
        self.combined_meal_combo = QComboBox()
        for combo in (
            self.breakfast_combo,
            self.lunch_combo,
            self.dinner_combo,
            self.combined_meal_combo,
        ):
            combo.currentIndexChanged.connect(self._mapping_changed)
        meal_form.addRow("조식 여부 열", self.breakfast_combo)
        meal_form.addRow("중식 여부 열", self.lunch_combo)
        meal_form.addRow("석식 여부 열", self.dinner_combo)
        meal_form.addRow("식종 열 하나", self.combined_meal_combo)
        self.true_values_edit = QLineEdit("1")
        self.false_values_edit = QLineEdit(f"0, {_BLANK_TOKEN}")
        meal_form.addRow("급식을 받는 값", self.true_values_edit)
        meal_form.addRow("급식을 받지 않는 값", self.false_values_edit)
        self.driver_combo = QComboBox()
        self.sequence_combo = QComboBox()
        self.source_reference_combo = QComboBox()
        for combo in (self.driver_combo, self.sequence_combo, self.source_reference_combo):
            combo.currentIndexChanged.connect(self._mapping_changed)
        meal_form.addRow("배송담당", self.driver_combo)
        meal_form.addRow("배송순서", self.sequence_combo)
        meal_form.addRow("원본 연번", self.source_reference_combo)
        self._meal_flag_fields = (
            self.breakfast_combo,
            self.lunch_combo,
            self.dinner_combo,
            self.true_values_edit,
            self.false_values_edit,
        )
        self._combined_meal_fields = (self.combined_meal_combo,)
        self._meal_form = meal_form
        body_layout.addLayout(meal_form)

        body_layout.addWidget(self._section("값 정리와 제외 열"))
        normalization_form = QFormLayout()
        self.date_normalization_combo = QComboBox()
        self.date_normalization_combo.addItem("날짜 셀·YYYYMMDD 자동", DateNormalization.AUTO)
        self.date_normalization_combo.addItem("YYYYMMDD만", DateNormalization.YYYYMMDD)
        self.phone_normalization_combo = QComboBox()
        self.phone_normalization_combo.addItem("010-1234-5678 모양", PhoneNormalization.KOREAN)
        self.phone_normalization_combo.addItem("숫자만", PhoneNormalization.DIGITS)
        self.phone_normalization_combo.addItem("원본 모양 유지", PhoneNormalization.PRESERVE)
        normalization_form.addRow("생년월일 정리", self.date_normalization_combo)
        normalization_form.addRow("전화번호 정리", self.phone_normalization_combo)
        body_layout.addLayout(normalization_form)
        ignored_copy = QLabel(
            "체크한 열은 DB에 복사하지 않습니다. 연결하지 않은 열은 기본으로 제외됩니다."
        )
        ignored_copy.setObjectName("muted")
        ignored_copy.setWordWrap(True)
        body_layout.addWidget(ignored_copy)
        self.ignored_columns_list = QListWidget()
        self.ignored_columns_list.setMinimumHeight(120)
        body_layout.addWidget(self.ignored_columns_list)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        self.mapping_status = QLabel()
        self.mapping_status.setObjectName("muted")
        self.mapping_status.setWordWrap(True)
        layout.addWidget(self.mapping_status)
        return page

    def _build_preview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(
            self._page_heading(
                "3. 명단에 넣기 전 미리보기",
                "가져올 사람·식종과 오류를 확인합니다. 다음 화면에서 행별 최종 처리를 한 번 더 확인합니다.",
            )
        )
        effective_row = QHBoxLayout()
        effective_row.addWidget(QLabel("이 명단을 적용할 날짜 (당일 포함) *"))
        self.effective_date_edit = QDateEdit(QDate.currentDate())
        self.effective_date_edit.setCalendarPopup(True)
        self.effective_date_edit.setDisplayFormat("yyyy-MM-dd")
        effective_row.addWidget(self.effective_date_edit)
        effective_row.addStretch(1)
        self.preview_summary = QLabel()
        self.preview_summary.setObjectName("sectionLabel")
        effective_row.addWidget(self.preview_summary)
        layout.addLayout(effective_row)
        self.person_preview_table = QTableWidget(0, 6)
        self.person_preview_table.setHorizontalHeaderLabels(
            ("원본 행", "성명", "생년월일", "식종", "학교", "핸드폰")
        )
        self.person_preview_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.person_preview_table.setAlternatingRowColors(True)
        self.person_preview_table.verticalHeader().setVisible(False)
        self.person_preview_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.person_preview_table, 1)
        self.issue_list = QListWidget()
        self.issue_list.setMaximumHeight(150)
        layout.addWidget(self.issue_list)
        return page

    def _section(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionLabel")
        return label

    def _run_busy_task(
        self,
        title: str,
        detail: str,
        task: Callable[[], _TaskResult],
    ) -> _TaskResult:
        """Run workbook I/O off the UI thread while keeping a clear busy state."""

        self._set_busy(title, detail)
        executor: ThreadPoolExecutor | None = None
        timer: QTimer | None = None
        try:
            executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="roster-import",
            )
            future = executor.submit(task)
            loop = QEventLoop(self)
            timer = QTimer(self)
            timer.setInterval(25)
            timer.timeout.connect(
                lambda: loop.quit() if future.done() else None
            )
            timer.start()
            loop.exec()
            return future.result()
        finally:
            if timer is not None:
                timer.stop()
                timer.deleteLater()
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
            self._clear_busy()

    def _set_busy(self, title: str, detail: str) -> None:
        self._busy = True
        self.busy_title.setText(title)
        self.busy_detail.setText(detail)
        self.busy_panel.show()
        self.page_stack.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.back_button.setEnabled(False)
        self.next_button.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self._owns_wait_cursor = True
        QApplication.processEvents()

    def _clear_busy(self) -> None:
        if self._owns_wait_cursor:
            QApplication.restoreOverrideCursor()
            self._owns_wait_cursor = False
        self._busy = False
        self.busy_panel.hide()
        self.page_stack.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self._refresh_actions()

    def _browse_source(self) -> None:
        filename, _filter = QFileDialog.getOpenFileName(
            self,
            "기존 Excel 명단 선택",
            self.path_edit.text().strip() or self._initial_directory,
            "Excel 명단 (*.xlsx *.xlsm)",
        )
        if filename:
            self.path_edit.setText(filename)
            self._inspect_source()

    def _inspect_source(self) -> bool:
        self._clear_error()
        path_text = self.path_edit.text().strip()
        if not path_text:
            self._show_error("기존 Excel 파일을 선택해주세요.")
            return False
        if self.data_start_row_spin.value() <= self.header_row_spin.value():
            self._show_error("데이터 시작 행은 헤더 행보다 뒤여야 합니다.")
            return False
        selected_sheet = self.sheet_combo.currentData() if self.sheet_combo.count() else None
        password = self.password_edit.text()
        header_row = self.header_row_spin.value()
        data_start_row = self.data_start_row_spin.value()
        try:
            inspection = self._run_busy_task(
                "Excel 파일을 여는 중입니다",
                "파일 비밀번호를 확인하고 시트와 표 위치를 읽고 있습니다. 잠시 기다려주세요.",
                lambda: inspect_workbook(
                    path_text,
                    password=password,
                    sheet_name=selected_sheet,
                    header_row=header_row,
                    data_start_row=data_start_row,
                ),
            )
        except WorkbookImportPasswordRequiredError as error:
            self.password_edit.setFocus()
            self._show_error(str(error))
            return False
        except Exception as error:
            self._show_error(str(error))
            return False
        self._inspection = inspection
        self._loading_structure = True
        try:
            current_sheet = inspection.selected_sheet
            self.sheet_combo.clear()
            for sheet in inspection.sheet_names:
                self.sheet_combo.addItem(sheet, sheet)
            self.sheet_combo.setCurrentIndex(self.sheet_combo.findData(current_sheet))
            self.sheet_combo.setEnabled(True)
        finally:
            self._loading_structure = False
        self._render_raw_preview(inspection)
        self.file_status.setText(
            f"{inspection.selected_sheet} · 헤더 {len(inspection.columns)}열 · "
            f"미리보기 {len(inspection.preview_rows)}행을 확인했습니다."
        )
        self._prepare_mapping(inspection)
        self._refresh_actions()
        return True

    def _render_raw_preview(self, inspection: WorkbookInspection) -> None:
        self.raw_table.clear()
        self.raw_table.setColumnCount(len(inspection.columns))
        self.raw_table.setRowCount(len(inspection.preview_rows))
        self.raw_table.setHorizontalHeaderLabels(
            tuple(column.label for column in inspection.columns)
        )
        for row_index, preview_row in enumerate(inspection.preview_rows):
            self.raw_table.setVerticalHeaderItem(
                row_index,
                QTableWidgetItem(str(preview_row.row_number)),
            )
            for column_index, value in enumerate(preview_row.values):
                self.raw_table.setItem(row_index, column_index, QTableWidgetItem(value))
        self.raw_table.verticalHeader().setVisible(True)
        self.raw_table.resizeColumnsToContents()

    def _prepare_mapping(self, inspection: WorkbookInspection) -> None:
        self._suggested_profile = next(
            (
                item
                for item in self._existing_profiles
                if item.profile
                == proposed_profile(inspection, self._existing_profiles)
            ),
            None,
        )
        all_combos = (
            *self._mapping_combos.values(),
            self.address_fallback_combo,
            self.breakfast_combo,
            self.lunch_combo,
            self.dinner_combo,
            self.combined_meal_combo,
            self.driver_combo,
            self.sequence_combo,
            self.source_reference_combo,
        )
        for combo in all_combos:
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("— 사용 안 함 —", None)
            for column in inspection.columns:
                combo.addItem(column.label, column.letter)
            combo.blockSignals(False)
        self.ignored_columns_list.clear()
        for column in inspection.columns:
            item = QListWidgetItem(column.label)
            item.setData(Qt.ItemDataRole.UserRole, column.letter)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.ignored_columns_list.addItem(item)

        if self._suggested_profile is not None:
            self._apply_profile(self._suggested_profile.profile)
            self.mapping_status.setText(
                f"같은 구조에서 사용한 '{self._suggested_profile.profile.name}' 설정을 제안했습니다. 열 연결을 다시 확인해주세요."
            )
        else:
            self._apply_guesses(inspection)
            self.mapping_status.setText(
                "헤더 이름으로 가능한 열을 미리 골랐습니다. 틀린 항목이 없는지 확인해주세요."
            )
        self._mapping_initialized = True
        self._mapping_changed()

    def _apply_profile(self, profile: ImportMappingProfile) -> None:
        self.profile_name_edit.setText(profile.name)
        self.provider_edit.setText(profile.provider_value or "")
        index = self.source_kind_combo.findData(profile.source_kind)
        if index >= 0:
            self.source_kind_combo.setCurrentIndex(index)
        for field, combo in self._mapping_combos.items():
            self._set_combo(combo, profile.profile_columns.get(field))
        fallbacks = profile.fallback_profile_columns.get(ProfileField.ROAD_ADDRESS, ())
        self._set_combo(self.address_fallback_combo, fallbacks[0] if fallbacks else None)
        if profile.combined_meals_column is not None:
            self.meal_mode_combo.setCurrentIndex(
                self.meal_mode_combo.findData("combined")
            )
            self._set_combo(self.combined_meal_combo, profile.combined_meals_column)
        else:
            self.meal_mode_combo.setCurrentIndex(self.meal_mode_combo.findData("flags"))
            self._set_combo(self.breakfast_combo, profile.meal_flag_columns.get(Meal.BREAKFAST))
            self._set_combo(self.lunch_combo, profile.meal_flag_columns.get(Meal.LUNCH))
            self._set_combo(self.dinner_combo, profile.meal_flag_columns.get(Meal.DINNER))
        self.true_values_edit.setText(_display_value_list(profile.meal_true_values))
        self.false_values_edit.setText(_display_value_list(profile.meal_false_values))
        self._set_combo(self.driver_combo, profile.driver_column)
        self._set_combo(self.sequence_combo, profile.sequence_column)
        self._set_combo(self.source_reference_combo, profile.source_reference_column)
        self.date_normalization_combo.setCurrentIndex(
            self.date_normalization_combo.findData(profile.date_normalization)
        )
        self.phone_normalization_combo.setCurrentIndex(
            self.phone_normalization_combo.findData(profile.phone_normalization)
        )

    def _apply_guesses(self, inspection: WorkbookInspection) -> None:
        for field, combo in self._mapping_combos.items():
            self._set_combo(combo, _guess_column(field, inspection))
        road_primary = self._mapping_combos[ProfileField.ROAD_ADDRESS].currentData()
        fallback = _find_header(inspection, ("기존주소", "이전주소"), exclude=road_primary)
        self._set_combo(self.address_fallback_combo, fallback)
        combined = _find_header(inspection, ("식종", "급식구분", "지원식종"))
        breakfast = _find_header_containing(inspection, "조식")
        lunch = _find_header_containing(inspection, "중식", exclude=("토", "공휴일"))
        dinner = _find_header_containing(inspection, "석식", exclude=("토", "공휴일"))
        if combined is not None and not any((breakfast, lunch, dinner)):
            self.meal_mode_combo.setCurrentIndex(
                self.meal_mode_combo.findData("combined")
            )
            self._set_combo(self.combined_meal_combo, combined)
        else:
            self.meal_mode_combo.setCurrentIndex(self.meal_mode_combo.findData("flags"))
            self._set_combo(self.breakfast_combo, breakfast)
            self._set_combo(self.lunch_combo, lunch)
            self._set_combo(self.dinner_combo, dinner)
        self._set_combo(
            self.driver_combo,
            _find_header(inspection, ("배송담당", "담당기사", "기사", "담당")),
        )
        self._set_combo(
            self.sequence_combo,
            _find_header(inspection, ("배송순서", "배달순서", "순서")),
        )
        self._set_combo(
            self.source_reference_combo,
            _find_header(inspection, ("원본연번", "연번", "번호")),
        )
        if _find_header(inspection, ("변경주소",)) is not None:
            self.profile_name_edit.setText("정부 조사표")
            self.source_kind_combo.setCurrentIndex(
                self.source_kind_combo.findData("survey-xlsx")
            )

    def _set_combo(self, combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _build_profile(self) -> ImportMappingProfile:
        profile_columns = {
            field: str(combo.currentData())
            for field, combo in self._mapping_combos.items()
            if combo.currentData() is not None
        }
        fallback = self.address_fallback_combo.currentData()
        fallback_columns = (
            {}
            if fallback is None
            else {ProfileField.ROAD_ADDRESS: (str(fallback),)}
        )
        meal_flags = {}
        combined = None
        if self.meal_mode_combo.currentData() == "combined":
            value = self.combined_meal_combo.currentData()
            combined = None if value is None else str(value)
            if combined is None:
                raise ValueError(
                    "‘식종’ 열을 골라주세요. 한 셀에 ‘조식 / 중식’처럼 적힌 열입니다."
                )
        else:
            meal_flags = {
                meal: str(combo.currentData())
                for meal, combo in (
                    (Meal.BREAKFAST, self.breakfast_combo),
                    (Meal.LUNCH, self.lunch_combo),
                    (Meal.DINNER, self.dinner_combo),
                )
                if combo.currentData() is not None
            }
            if not meal_flags:
                raise ValueError(
                    "조식·중식·석식 중 Excel에 실제로 있는 1/0 열을 하나 이상 골라주세요."
                )
        ignored = tuple(
            str(item.data(Qt.ItemDataRole.UserRole))
            for row in range(self.ignored_columns_list.count())
            if (item := self.ignored_columns_list.item(row)).checkState()
            == Qt.CheckState.Checked
        )
        profile_id = (
            self._profile_id
            if self._suggested_profile is None
            else self._suggested_profile.profile_id
        )
        version = 1 if self._suggested_profile is None else self._suggested_profile.version
        values = dict(
            profile_id=profile_id,
            version=version,
            name=self.profile_name_edit.text(),
            source_kind=str(self.source_kind_combo.currentData()),
            profile_columns=profile_columns,
            combined_meals_column=combined,
            meal_flag_columns=meal_flags,
            driver_column=_combo_text(self.driver_combo),
            sequence_column=_combo_text(self.sequence_combo),
            source_reference_column=_combo_text(self.source_reference_combo),
            header_row=self.header_row_spin.value(),
            data_start_row=self.data_start_row_spin.value(),
            fallback_profile_columns=fallback_columns,
            meal_true_values=_parse_value_list(self.true_values_edit.text()),
            meal_false_values=_parse_value_list(self.false_values_edit.text()),
            date_normalization=DateNormalization(
                str(self.date_normalization_combo.currentData())
            ),
            phone_normalization=PhoneNormalization(
                str(self.phone_normalization_combo.currentData())
            ),
            ignored_columns=ignored,
            provider_value=self.provider_edit.text().strip() or None,
        )
        profile = ImportMappingProfile(**values)
        if self._suggested_profile is not None and profile != self._suggested_profile.profile:
            values["version"] = self._suggested_profile.version + 1
            profile = ImportMappingProfile(**values)
        return profile

    def _apply_household_rule(
        self,
        stored: StoredHouseholdMatchingRule | None,
    ) -> None:
        selected = (
            (HouseholdMatchCriterion.MOBILE_PHONE,)
            if stored is None
            else stored.rule.criteria
        )
        for criterion, checkbox in self._household_criterion_checkboxes.items():
            checkbox.setChecked(criterion in selected)

    def _build_household_rule(self) -> HouseholdMatchingRule:
        criteria = tuple(
            criterion
            for criterion, checkbox in self._household_criterion_checkboxes.items()
            if checkbox.isChecked()
        )
        if not criteria:
            raise ValueError("가구 묶기 기준을 하나 이상 선택해주세요.")
        existing = self._existing_household_rule
        if existing is None:
            return HouseholdMatchingRule(self._household_rule_id, 1, criteria)
        candidate = HouseholdMatchingRule(
            existing.rule_id,
            existing.version,
            criteria,
        )
        if candidate == existing.rule:
            return candidate
        return HouseholdMatchingRule(
            existing.rule_id,
            existing.version + 1,
            criteria,
        )

    def _render_preview(self, preview: WorkbookImportPreview) -> None:
        self.person_preview_table.setRowCount(len(preview.observations))
        for row, observation in enumerate(preview.observations):
            values = (
                ", ".join(str(number) for number in observation.source.row_numbers),
                observation.name,
                "미입력" if observation.birth_date is None else observation.birth_date.isoformat(),
                " / ".join(meal.value for meal in observation.meals) or "없음",
                str(observation.profile_values.get(ProfileField.SCHOOL_NAME) or ""),
                str(observation.profile_values.get(ProfileField.MOBILE_PHONE) or ""),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.person_preview_table.setItem(row, column, item)
        self.person_preview_table.resizeColumnsToContents()
        self.issue_list.clear()
        for issue in preview.issues:
            item = QListWidgetItem(f"{issue.location} · {issue.message}")
            item.setForeground(Qt.GlobalColor.darkRed)
            self.issue_list.addItem(item)
        if preview.issues:
            self.issue_list.show()
            self.preview_summary.setText(
                f"가져올 사람 {len(preview.observations)}명 · 수정할 오류 {len(preview.issues)}건"
            )
        else:
            self.issue_list.hide()
            self.preview_summary.setText(f"가져올 사람 {len(preview.observations)}명 · 오류 없음")

    def _go_next(self) -> None:
        self._clear_error()
        page = self.page_stack.currentIndex()
        if page == 0:
            if self._inspection is None and not self._inspect_source():
                return
            self._set_page(1)
            return
        if page == 1:
            if self._inspection is None:
                self._set_page(0)
                return
            try:
                profile = self._build_profile()
                self._build_household_rule()
                path_text = self.path_edit.text().strip()
                selected_sheet = self._inspection.selected_sheet
                password = self.password_edit.text()
                preview = self._run_busy_task(
                    "가져올 명단을 만드는 중입니다",
                    "선택한 열을 읽고 사람·식종·배송 정보를 확인하고 있습니다. 잠시 기다려주세요.",
                    lambda: parse_workbook(
                        path_text,
                        profile,
                        sheet_name=selected_sheet,
                        password=password,
                    ),
                )
            except Exception as error:
                self._show_error(str(error))
                return
            self.password_edit.clear()
            self._preview = preview
            self._render_preview(preview)
            self._set_page(2)
            return
        if self._preview is None or not self._preview.can_continue:
            self._show_error("오류를 모두 해결한 뒤 최종 검토로 이동할 수 있습니다.")
            return
        effective_on = self.effective_date_edit.date().toPython()
        self._selection = ImportWizardSelection(
            self._preview,
            effective_on,
            self._suggested_profile,
            self._build_household_rule(),
            self._existing_household_rule,
        )
        self.accept()

    def _go_back(self) -> None:
        page = self.page_stack.currentIndex()
        if page > 0:
            self._set_page(page - 1)

    def _set_page(self, page: int) -> None:
        self.page_stack.setCurrentIndex(page)
        self.back_button.setVisible(page > 0)
        self.next_button.setText(
            ("열 연결 확인으로", "미리보기 만들기", "최종 검토로 이동")[page]
        )
        for index, label in enumerate(self._route_labels):
            if index == min(page, 2):
                label.setStyleSheet(f"color: #FFFFFF; font-weight: 800; border-bottom: 2px solid {FOCUS};")
            elif index < min(page, 2):
                label.setStyleSheet("color: #9BC7CF; font-weight: 700;")
            else:
                label.setStyleSheet("color: #BDD4D9; font-weight: 700;")
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        if self._busy:
            self.cancel_button.setEnabled(False)
            self.back_button.setEnabled(False)
            self.next_button.setEnabled(False)
            return
        page = self.page_stack.currentIndex()
        self.back_button.setEnabled(page > 0)
        if page == 0:
            self.next_button.setEnabled(self._inspection is not None)
        elif page == 2:
            self.next_button.setEnabled(
                self._preview is not None and self._preview.can_continue
            )
        else:
            self.next_button.setEnabled(True)

    def _mapping_changed(self, *_args) -> None:
        if not self._mapping_initialized:
            return
        used = {
            str(combo.currentData())
            for combo in (
                *self._mapping_combos.values(),
                self.address_fallback_combo,
                self.breakfast_combo,
                self.lunch_combo,
                self.dinner_combo,
                self.combined_meal_combo,
                self.driver_combo,
                self.sequence_combo,
                self.source_reference_combo,
            )
            if combo.currentData() is not None
        }
        for row in range(self.ignored_columns_list.count()):
            item = self.ignored_columns_list.item(row)
            column = str(item.data(Qt.ItemDataRole.UserRole))
            if column in used:
                item.setCheckState(Qt.CheckState.Unchecked)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            else:
                was_disabled = not bool(item.flags() & Qt.ItemFlag.ItemIsEnabled)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEnabled)
                if was_disabled:
                    item.setCheckState(Qt.CheckState.Checked)

    def _meal_mode_changed(self, *_args) -> None:
        combined = self.meal_mode_combo.currentData() == "combined"
        for field in self._combined_meal_fields:
            field.setVisible(combined)
            label = self._meal_form.labelForField(field)
            if label is not None:
                label.setVisible(combined)
        for field in self._meal_flag_fields:
            field.setVisible(not combined)
            label = self._meal_form.labelForField(field)
            if label is not None:
                label.setVisible(not combined)
        self._mapping_changed()

    def _sheet_changed(self, *_args) -> None:
        if self._loading_structure:
            return
        self._inspection = None
        self._inspect_source()

    def _invalidate_structure(self, *_args) -> None:
        self._inspection = None
        self._preview = None
        self._loading_structure = True
        try:
            self.sheet_combo.clear()
            self.sheet_combo.setEnabled(False)
        finally:
            self._loading_structure = False
        self._refresh_actions()

    def _invalidate_layout_only(self, *_args) -> None:
        self._inspection = None
        self._preview = None
        self._refresh_actions()

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _clear_error(self) -> None:
        self.error_label.clear()
        self.error_label.hide()


def _combo_text(combo: QComboBox) -> str | None:
    value = combo.currentData()
    return None if value is None else str(value)


def _household_criterion_icon(checked: bool) -> QIcon:
    """Draw a high-contrast checkbox icon without relying on font glyphs."""

    pixmap = QPixmap(18, 18)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor("#FFFFFF" if checked else "#49646E")
    outline = QPen(color, 2.0)
    outline.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(outline)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(2, 2, 14, 14, 2, 2)
    if checked:
        check_pen = QPen(color, 3.0)
        check_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        check_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(check_pen)
        path = QPainterPath(QPointF(4.5, 9.0))
        path.lineTo(QPointF(7.7, 12.2))
        path.lineTo(QPointF(13.8, 5.6))
        painter.drawPath(path)
    painter.end()
    return QIcon(pixmap)


def _parse_value_list(text: str) -> tuple[str, ...]:
    values = tuple(
        "" if item.strip() == _BLANK_TOKEN else item.strip()
        for item in text.split(",")
        if item.strip()
    )
    if not values:
        raise ValueError("급식을 받는 값과 받지 않는 값을 하나 이상 입력해주세요.")
    return values


def _display_value_list(values: tuple[str, ...]) -> str:
    return ", ".join(_BLANK_TOKEN if value == "" else value for value in values)


def _header_key(value: str) -> str:
    return "".join(value.split()).casefold()


def _find_header(
    inspection: WorkbookInspection,
    aliases: tuple[str, ...],
    *,
    exclude: object = None,
) -> str | None:
    excluded = {str(exclude)} if exclude is not None else set()
    keys = tuple(_header_key(alias) for alias in aliases)
    for key in keys:
        matches = tuple(
            column.letter
            for column in inspection.columns
            if column.letter not in excluded and _header_key(column.header) == key
        )
        if len(matches) == 1:
            return matches[0]
    return None


def _find_header_containing(
    inspection: WorkbookInspection,
    term: str,
    *,
    exclude: tuple[str, ...] = (),
) -> str | None:
    term_key = _header_key(term)
    exclude_keys = tuple(_header_key(value) for value in exclude)
    matches = tuple(
        column.letter
        for column in inspection.columns
        if term_key in _header_key(column.header)
        and not any(key in _header_key(column.header) for key in exclude_keys)
    )
    return matches[0] if len(matches) == 1 else None


def _guess_column(
    field: ProfileField,
    inspection: WorkbookInspection,
) -> str | None:
    aliases = _HEADER_ALIASES[field]
    exact = _find_header(inspection, aliases)
    if exact is not None:
        return exact
    if field is ProfileField.BIRTH_DATE:
        return _find_header_containing(inspection, "생년월일")
    return None
