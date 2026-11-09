"""Novice-facing file, mapping, and preview route for roster imports."""

from __future__ import annotations

from .user_messages import user_error

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, TypeVar
from uuid import UUID, uuid4, uuid5

from PySide6.QtCore import QDate, QEvent, QEventLoop, QSize, QTimer, Qt
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QProgressBar,
    QSpinBox,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.domain.roster import Meal
from src.importing.workbook import (
    AutomaticSourceResolution,
    ImportIssue,
    WorkbookImportPasswordRequiredError,
    WorkbookImportPasswordError,
    WorkbookImportPreview,
    WorkbookInspection,
    WorkbookStructureError,
    WorkbookTable,
    inspect_workbook,
    parse_workbook,
    proposed_profile,
)
from src.importing.contracts import (
    DateNormalization,
    ImportMappingProfile,
    ImportRowFilter,
    PhoneNormalization,
    ProfileField,
)
from src.importing.households import (
    HouseholdMatchCriterion,
    HouseholdMatchingRule,
    StoredHouseholdMatchingRule,
)
from src.importing.revision import ImportPurpose
from src.importing.vacation_lunch import LunchDuplicatePolicy
from src.services.school_calendar_history import SchoolCalendarImportEntry
from src.importing.profile_history import StoredImportProfile

from .import_columns import ImportColumnMappingDialog, ImportColumnsTable
from .date_edit import WheelSafeDateEdit
from .theme import FOCUS, application_stylesheet, register_application_fonts
from src.importing.workbook import custom_column_names, source_column_letter


_FIELD_LABELS = {
    ProfileField.NAME: "성명 *",
    ProfileField.BIRTH_DATE: "생년월일",
    ProfileField.SCHOOL_TYPE: "학교 유형",
    ProfileField.SCHOOL_NAME: "학교명",
    ProfileField.GRADE: "학년",
    ProfileField.HOUSEHOLD_HEAD_NAME: "세대주 성명",
    ProfileField.RELATIONSHIP_TO_CHILD: "아동과의 관계",
    ProfileField.JURISDICTION: "관할동",
    ProfileField.ROAD_ADDRESS: "도로명주소",
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
_ROUTE_STOPS = ("파일 선택", "열 연결 · 적용일", "오류가 있을 때만 수정")
_BLANK_TOKEN = "(빈칸)"
_TaskResult = TypeVar("_TaskResult")


@dataclass(frozen=True, slots=True)
class ImportWizardSelection:
    preview: WorkbookImportPreview
    effective_on: date
    existing_profile: StoredImportProfile | None
    household_rule: HouseholdMatchingRule
    existing_household_rule: StoredHouseholdMatchingRule | None
    purpose: ImportPurpose = ImportPurpose.DATABASE
    lunch_calendar_entry: SchoolCalendarImportEntry | None = None
    lunch_duplicate_policy: LunchDuplicatePolicy = LunchDuplicatePolicy.UPDATE


class RosterImportWizard(QDialog):
    """Keep users on a guided route until a valid preview is ready for review."""

    def __init__(
        self,
        *,
        existing_profiles: tuple[StoredImportProfile, ...] = (),
        existing_household_rule: StoredHouseholdMatchingRule | None = None,
        initial_directory: str | Path | None = None,
        allow_vacation_lunch: bool = False,
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
        self.setWindowTitle("명단 불러오기")
        self.setModal(True)
        self.setStyleSheet(application_stylesheet())
        _fit_dialog_to_available_screen(
            self,
            desired_size=QSize(1120, 780),
            minimum_size=QSize(760, 520),
        )
        self._existing_custom_fields = ()
        self._database_columns = None
        self._column_labels = {}
        self._custom_destinations = {}
        self._new_destination_ids = {}
        self._syncing_destinations = False
        self._existing_profiles = tuple(existing_profiles)
        self._allow_vacation_lunch = allow_vacation_lunch
        self._existing_household_rule = existing_household_rule
        self._initial_directory = (
            "" if initial_directory is None else str(Path(initial_directory))
        )
        self._inspection: WorkbookInspection | None = None
        self._preview: WorkbookImportPreview | None = None
        self._suggested_profile: StoredImportProfile | None = None
        self._selection: ImportWizardSelection | None = None
        self._profile_id = uuid4()
        self._source_kind = "roster-xlsx"
        self._household_rule_id = uuid4()
        self._loading_structure = False
        self._mapping_initialized = False
        self._confirmed_mapping_key = None
        self._mapping_summary_text = ""
        self._mapping_combos: dict[ProfileField, QComboBox] = {}
        self._route_labels: list[QLabel] = []
        self._busy = False
        self._owns_wait_cursor = False
        self._password_retry_pending = False
        self._source_password: str | None = None
        self._meal_mapping_expanded = False
        self._cell_overrides: dict[tuple[int, str], object] = {}
        self._excluded_rows: set[int] = set()
        self._pending_excluded_rows: set[int] = set()
        self._correction_editors: dict[
            tuple[int, str],
            QLineEdit | QComboBox,
        ] = {}
        self._correction_issues: dict[tuple[int, str], ImportIssue] = {}
        self._build_ui()
        self.purpose_combo.currentIndexChanged.connect(self._meal_mode_changed)
        self._sync_navigation_rail()
        self._meal_mode_changed()
        self._set_page(0)

    def set_initial_import_defaults(self) -> None:
        """Called once by the controller only when no current roster exists."""
        if getattr(self, "_initial_import_defaults_applied", False):
            return
        self._initial_import_defaults_applied = True
        today = QDate.currentDate()
        self.effective_date_edit.setDate(QDate(today.year(), today.month(), 1))
        self.initial_date_note.show()
        self.effective_date_edit.setToolTip(self.initial_date_note.text())

    @property
    def inspection(self) -> WorkbookInspection | None:
        return self._inspection

    @property
    def preview(self) -> WorkbookImportPreview | None:
        return self._preview

    def selection(self) -> ImportWizardSelection:
        if self._selection is None:
            raise ValueError("불러오기 미리보기가 아직 확정되지 않았습니다.")
        return self._selection

    def done(self, result: int) -> None:
        if hasattr(self, "correction_dialog"):
            self.correction_dialog.reject()
        self.password_edit.clear()
        self._source_password = None
        self._cell_overrides.clear()
        self._excluded_rows.clear()
        self._pending_excluded_rows.clear()
        self._correction_editors.clear()
        self._correction_issues.clear()
        super().done(result)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._sync_navigation_rail()

    def _sync_navigation_rail(self) -> None:
        if hasattr(self, "navigation_rail"):
            self.navigation_rail.setVisible(self.width() >= 1000)

    def _build_ui(self) -> None:
        shell = QHBoxLayout(self)
        shell.setContentsMargins(18, 18, 18, 18)
        shell.setSpacing(0)
        self.navigation_rail = self._build_rail()
        shell.addWidget(self.navigation_rail)

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
        product = QLabel("명단 불러오기")
        product.setObjectName("startupProduct")
        layout.addWidget(product)
        layout.addSpacing(14)
        title = QLabel("기존 명단을\n안전하게 옮깁니다")
        title.setObjectName("startupRailTitle")
        layout.addWidget(title)
        copy = QLabel(
            "원본 Excel은 그대로 두고, 확인한 열의 값만 암호화된 데이터 파일에 저장합니다."
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
        footer = QLabel("원본 파일 전체는 운영 데이터에 복사하지 않습니다")
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
                "Excel 표 또는 2줄 헤더가 있는 시트를 선택합니다. 파일은 수정하지 않습니다.",
            )
        )
        form = QFormLayout()
        form.setVerticalSpacing(10)
        self.purpose_combo = QComboBox()
        self.purpose_combo.addItem("일반 명단 반영", ImportPurpose.DATABASE)
        self.purpose_note = QLabel(page)
        self.purpose_note.setObjectName("muted")
        self.purpose_note.setStyleSheet("font-size: 12px;")
        self.purpose_note.setTextFormat(Qt.TextFormat.RichText)
        self.purpose_note.setWordWrap(True)
        self.purpose_note.setAccessibleName("불러오기 방식 설명")
        self.purpose_note.setVisible(self._allow_vacation_lunch)
        if self._allow_vacation_lunch:
            self.purpose_combo.addItem("방학 중식 대상자 갱신", ImportPurpose.VACATION_LUNCH)
            purpose_fields = QVBoxLayout()
            purpose_fields.setSpacing(4)
            purpose_fields.addWidget(self.purpose_combo)
            purpose_fields.addWidget(self.purpose_note)
            form.addRow("불러오기 방식 *", purpose_fields)
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
        self.password_edit.installEventFilter(self)
        self.password_edit.returnPressed.connect(self._retry_password_if_needed)
        self.password_edit.editingFinished.connect(self._retry_password_if_needed)
        form.addRow("Excel 파일 비밀번호", self.password_edit)

        self.source_layout_combo = QComboBox()
        self.source_layout_combo.addItem("자동 · Excel 표 우선", "auto")
        self.source_layout_combo.addItem("2줄 헤더", "two-row")
        self.source_layout_combo.currentIndexChanged.connect(self._source_layout_changed)
        form.addRow("헤더 읽기", self.source_layout_combo)
        self.header_row_spin = QSpinBox()
        self.header_row_spin.setRange(1, 1_048_575)
        self.header_row_spin.setValue(1)
        self.header_row_spin.setEnabled(False)
        self.header_row_spin.setToolTip("2줄 헤더의 첫 번째 행입니다. 데이터는 두 번째 헤더 다음 행부터 읽습니다.")
        self.header_row_spin.valueChanged.connect(self._header_row_changed)
        form.addRow("헤더 시작 행", self.header_row_spin)
        self.table_combo = QComboBox()
        self.table_combo.setEnabled(False)
        self.table_combo.currentIndexChanged.connect(self._table_changed)
        form.addRow("명단 표 / 시트 *", self.table_combo)

        self.inspect_button = QPushButton("표 미리보기")
        self.inspect_button.setObjectName("browseAction")
        self.inspect_button.clicked.connect(self._inspect_source)
        form.addRow("", self.inspect_button)
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
                "각 Excel 열을 현재 DB의 칼럼에 연결하세요. * 항목만 필수입니다.",
            )
        )
        self.lunch_mode_note = QLabel("이름·핸드폰으로 연결하고 선택한 방학의 학교·학년별 중식 기간에 등록합니다. 식종 열은 필요하지 않습니다.")
        self.lunch_mode_note.setWordWrap(True)
        self.lunch_mode_note.setObjectName("sectionLabel")
        self.lunch_mode_note.setWordWrap(True)
        layout.addWidget(self.lunch_mode_note)
        self.lunch_filter_panel = QWidget()
        filter_form = QFormLayout(self.lunch_filter_panel)
        filter_form.setContentsMargins(0, 0, 0, 0)
        self.lunch_calendar_combo = QComboBox()
        self.lunch_calendar_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.lunch_calendar_combo.setMinimumContentsLength(32)
        self.lunch_calendar_combo.addItem("학교 일정에서 방학 파일을 먼저 불러와주세요", None)
        filter_form.addRow("적용할 방학 기록 *", self.lunch_calendar_combo)
        self.lunch_duplicate_combo = QComboBox()
        self.lunch_duplicate_combo.addItem("기존 유지 · 중복 명단 무시", LunchDuplicatePolicy.KEEP)
        self.lunch_duplicate_combo.addItem("업데이트 · 가져온 정보 반영", LunchDuplicatePolicy.UPDATE)
        filter_form.addRow("기존 대상자 처리", self.lunch_duplicate_combo)
        duplicate_note = QLabel("기존 유지는 기존 정보·등록 기간을 보존합니다. 업데이트는 기존 값과 다른 항목을 확인합니다. 해당 방학에 미등록이면 두 방식 모두 추가합니다.")
        duplicate_note.setWordWrap(True)
        duplicate_note.setObjectName("muted")
        filter_form.addRow(duplicate_note)
        self.lunch_calendar_combo.currentIndexChanged.connect(self._lunch_calendar_changed)
        self.lunch_filter_mode = QComboBox()
        self.lunch_filter_mode.addItem("전체 명단", "all")
        self.lunch_filter_mode.addItem("선택한 칼럼에 값이 있는 행", "nonempty")
        self.lunch_filter_mode.addItem("선택한 칼럼이 특정 값인 행", "equals")
        self.lunch_filter_column = QComboBox()
        self.lunch_filter_column.addItem("— 칼럼 선택 —", None)
        self.lunch_filter_value = QLineEdit()
        self.lunch_filter_value.setPlaceholderText("예: 1, 대상, O (앞뒤 공백을 제외하고 정확히 일치)")
        filter_form.addRow("가져올 중식 명단", self.lunch_filter_mode)
        filter_form.addRow("조건 칼럼", self.lunch_filter_column)
        filter_form.addRow("일치할 값", self.lunch_filter_value)
        filter_note = QLabel("조건에 맞는 행만 선택한 방학에 등록합니다. 이번 파일에 없거나 조건에서 빠진 기존 대상자는 그대로 유지합니다.")
        filter_note.setWordWrap(True)
        filter_form.addRow(filter_note)
        self.lunch_filter_mode.currentIndexChanged.connect(self._lunch_filter_changed)
        self.lunch_filter_column.currentIndexChanged.connect(self._mapping_changed)
        self.lunch_filter_value.textChanged.connect(self._mapping_changed)
        layout.addWidget(self.lunch_filter_panel)
        effective_row = QHBoxLayout()
        self.effective_date_label = QLabel("이 명단을 적용할 날짜 (당일 포함) *")
        effective_row.addWidget(self.effective_date_label)
        self.effective_date_edit = WheelSafeDateEdit(QDate.currentDate())
        self.effective_date_edit.setCalendarPopup(True)
        self.effective_date_edit.setDisplayFormat("yyyy-MM-dd")
        effective_row.addWidget(self.effective_date_edit)
        effective_row.addStretch(1)
        layout.addLayout(effective_row)
        self.initial_date_note = QLabel("처음 가져오는 명단은 이번 달 1일부터 적용합니다. 실제 시작일이 다르면 날짜를 바꿔주세요.")
        self.initial_date_note.setWordWrap(True)
        self.initial_date_note.setObjectName("muted")
        self.initial_date_note.hide()
        layout.addWidget(self.initial_date_note)
        import_note = QLabel("열 연결 확인 후 불러오기를 누르면 오류와 모호한 사람 연결을 확인합니다. DB와 충돌하는 값은 해당 항목만 선택합니다.")
        import_note.setObjectName("muted")
        import_note.setWordWrap(True)
        layout.addWidget(import_note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(2, 2, 10, 2)
        self._profile_name = "기존 Excel 명단"
        body_layout.addWidget(self._section("엑셀 열 → DB 칼럼 연결"))
        self.destination_table = ImportColumnsTable()
        self.destination_table.destination_changed.connect(self._destination_changed)
        self.mapping_dialog = ImportColumnMappingDialog(self.destination_table, self)
        self.mapping_dialog.setStyleSheet(application_stylesheet())
        self.finished.connect(self.mapping_dialog.close)
        self.mapping_dialog.finished.connect(self._mapping_dialog_closed)
        mapping_row = QHBoxLayout()
        self.mapping_summary = QLabel("파일을 선택하면 자동으로 열을 연결합니다.")
        self.mapping_summary.setWordWrap(True)
        self.mapping_summary.setObjectName("muted")
        self.destination_table.summary_changed.connect(self._set_mapping_summary)
        mapping_row.addWidget(self.mapping_summary, 1)
        self.mapping_open_button = QPushButton("열 연결 확인하기")
        self.mapping_open_button.setObjectName("primaryAction")
        self.mapping_open_button.setAutoDefault(False)
        self.mapping_open_button.clicked.connect(self._open_mapping_dialog)
        mapping_row.addWidget(self.mapping_open_button)
        body_layout.addLayout(mapping_row)
        self._profile_mapping_state = QWidget(body)
        self._profile_mapping_state.hide()
        fields_form = QFormLayout(self._profile_mapping_state)
        for field in _MAPPED_PROFILE_FIELDS:
            combo = QComboBox()
            combo.setObjectName(f"mapping_{field.value}")
            combo.currentIndexChanged.connect(self._mapping_changed)
            self._mapping_combos[field] = combo
            fields_form.addRow(_FIELD_LABELS[field], combo)
        provider_note = QLabel("제공기관은 명단을 가져온 뒤 ‘설정 → 공통 제공기관’에서 설정합니다.")
        provider_note.setWordWrap(True)
        fields_form.addRow(provider_note)

        household_note = QLabel("가구는 설정의 ‘가구 묶기 기준’으로 자동으로 묶습니다. 기본은 핸드폰이며, 선택한 모든 값이 같고 빈칸이 없을 때 묶습니다.")
        household_note.setObjectName("muted")
        household_note.setWordWrap(True)
        body_layout.addWidget(household_note)

        self.meal_section = self._section("식종과 배송 정보")
        body_layout.addWidget(self.meal_section)
        meal_guide = self.meal_guide = QFrame()
        meal_guide.setObjectName("mealFormatGuide")
        meal_guide_layout = QVBoxLayout(meal_guide)
        meal_guide_layout.setContentsMargins(12, 9, 12, 10)
        meal_guide_layout.setSpacing(3)
        meal_guide_title = QLabel("앱이 열 이름을 보고 식종 형태를 자동으로 고릅니다")
        meal_guide_title.setObjectName("mealFormatGuideTitle")
        meal_guide_examples = QLabel(
            "• ‘조식’, ‘중식’, ‘석식’ 열이 따로 있고 각 칸이 1 또는 0 → 식종별 열\n"
            "• ‘식종’ 열 하나의 셀에 ‘조식 / 중식’처럼 적힘 → 식종 열 하나"
        )
        meal_guide_examples.setObjectName("mealFormatGuideExample")
        meal_guide_examples.setWordWrap(True)
        meal_guide_layout.addWidget(meal_guide_title)
        meal_guide_layout.addWidget(meal_guide_examples)
        body_layout.addWidget(meal_guide)

        meal_detection = self.meal_detection_panel = QFrame()
        meal_detection.setObjectName("mealDetectionPanel")
        meal_detection_layout = QHBoxLayout(meal_detection)
        meal_detection_layout.setContentsMargins(12, 8, 10, 8)
        meal_detection_layout.setSpacing(10)
        self.meal_detection_status = QLabel()
        self.meal_detection_status.setObjectName("mealDetectionStatus")
        self.meal_detection_status.setWordWrap(True)
        meal_detection_layout.addWidget(self.meal_detection_status, 1)
        self.meal_mapping_toggle = QPushButton("열 연결 바꾸기")
        self.meal_mapping_toggle.setObjectName("linkAction")
        self.meal_mapping_toggle.clicked.connect(self._toggle_meal_mapping_details)
        meal_detection_layout.addWidget(self.meal_mapping_toggle)
        body_layout.addWidget(meal_detection)

        meal_form = QFormLayout()
        self.meal_mode_combo = QComboBox()
        self.meal_mode_combo.addItem(
            "조식·중식·석식 열이 각각 있음",
            "flags",
        )
        self.meal_mode_combo.addItem(
            "‘식종’ 열 하나에 조식·중식·석식이 적혀 있음",
            "combined",
        )
        self.meal_mode_combo.currentIndexChanged.connect(self._meal_mode_changed)
        meal_form.addRow("식종 열 형태 (자동 선택)", self.meal_mode_combo)
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
        meal_form.addRow("조식 열", self.breakfast_combo)
        meal_form.addRow("중식 열", self.lunch_combo)
        meal_form.addRow("석식 열", self.dinner_combo)
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
        for combo in (self.driver_combo, self.sequence_combo, self.source_reference_combo):
            combo.setParent(self._profile_mapping_state)
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

        normalization_note = QLabel("생년월일과 전화번호는 자동으로 정리합니다. 처리할 수 없는 값은 오류 정정 창에서 확인합니다.")
        normalization_note.setObjectName("muted")
        normalization_note.setWordWrap(True)
        body_layout.addWidget(normalization_note)
        self.ignored_columns_list = QListWidget(body)
        self.ignored_columns_list.hide()
        self.ignored_columns_list.itemChanged.connect(self._mapping_changed)
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
                "불러오기 오류 수정",
                "오류를 수정하거나 해당 행을 제외한 뒤 가져옵니다. 선택 항목의 빈칸은 그대로 저장됩니다.",
            )
        )
        effective_row = QHBoxLayout()
        effective_row.addStretch(1)
        self.preview_summary = QLabel()
        self.preview_summary.setObjectName("sectionLabel")
        effective_row.addWidget(self.preview_summary)
        layout.addLayout(effective_row)

        self.correction_launch_panel = QFrame()
        self.correction_launch_panel.setObjectName("importCorrectionLaunchPanel")
        launch_layout = QHBoxLayout(self.correction_launch_panel)
        launch_layout.setContentsMargins(13, 10, 11, 10)
        launch_layout.setSpacing(12)
        launch_stamp = QLabel("수정 필요")
        launch_stamp.setObjectName("importCorrectionStamp")
        launch_layout.addWidget(launch_stamp)
        launch_copy = QVBoxLayout()
        launch_copy.setSpacing(2)
        self.correction_launch_title = QLabel()
        self.correction_launch_title.setObjectName("importCorrectionTitle")
        launch_help = QLabel(
            "오류 수정은 잘리지 않는 별도 창에서 한꺼번에 처리합니다."
        )
        launch_help.setObjectName("importCorrectionHelp")
        launch_copy.addWidget(self.correction_launch_title)
        launch_copy.addWidget(launch_help)
        launch_layout.addLayout(launch_copy, 1)
        self.correction_launch_button = QPushButton("오류 수정 창 열기")
        self.correction_launch_button.setObjectName("primaryAction")
        self.correction_launch_button.clicked.connect(
            self._open_correction_dialog
        )
        launch_layout.addWidget(self.correction_launch_button)
        self.correction_launch_panel.hide()
        layout.addWidget(self.correction_launch_panel)

        self._build_correction_dialog()

        self.correction_applied_panel = QFrame()
        self.correction_applied_panel.setObjectName("importCorrectionApplied")
        applied_layout = QHBoxLayout(self.correction_applied_panel)
        applied_layout.setContentsMargins(11, 7, 9, 7)
        self.correction_applied_label = QLabel()
        self.correction_applied_label.setObjectName("importCorrectionAppliedText")
        applied_layout.addWidget(self.correction_applied_label, 1)
        self.correction_reset_button = QPushButton("수정·제외 모두 취소")
        self.correction_reset_button.setObjectName("quietAction")
        self.correction_reset_button.clicked.connect(
            self._reset_error_corrections
        )
        applied_layout.addWidget(self.correction_reset_button)
        self.correction_applied_panel.hide()
        layout.addWidget(self.correction_applied_panel)

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
        self.notice_label = QLabel("빈칸·제외 안내")
        self.notice_label.setObjectName("sectionLabel")
        self.issue_list = QListWidget()
        self.issue_list.setMaximumHeight(120)
        layout.addWidget(self.notice_label)
        layout.addWidget(self.issue_list)
        return page

    def _build_correction_dialog(self) -> None:
        self.correction_dialog = QDialog(self)
        self.finished.connect(self.correction_dialog.close)
        self.correction_dialog.setObjectName("importCorrectionDialog")
        self.correction_dialog.setWindowTitle("명단 오류 한꺼번에 수정")
        self.correction_dialog.setModal(False)
        self.correction_dialog.setSizeGripEnabled(True)
        self.correction_dialog.setStyleSheet(application_stylesheet())
        _fit_dialog_to_available_screen(
            self.correction_dialog,
            desired_size=QSize(1100, 700),
            minimum_size=QSize(720, 460),
        )

        dialog_layout = QVBoxLayout(self.correction_dialog)
        dialog_layout.setContentsMargins(16, 16, 16, 16)
        dialog_layout.setSpacing(10)

        self.correction_panel = QFrame()
        self.correction_panel.setObjectName("importCorrectionPanel")
        correction_layout = QVBoxLayout(self.correction_panel)
        correction_layout.setContentsMargins(15, 13, 15, 14)
        correction_layout.setSpacing(10)
        correction_heading = QHBoxLayout()
        correction_stamp = QLabel("오류 정정표")
        correction_stamp.setObjectName("importCorrectionStamp")
        correction_heading.addWidget(correction_stamp)
        correction_copy = QVBoxLayout()
        self.correction_title = QLabel()
        self.correction_title.setObjectName("importCorrectionTitle")
        self.correction_help = QLabel(
            "값을 고치거나 왼쪽 체크로 원본 행 전체를 제외한 뒤 가져오세요. "
            "원본 Excel 파일은 바뀌지 않습니다."
        )
        self.correction_help.setObjectName("importCorrectionHelp")
        self.correction_help.setWordWrap(True)
        correction_copy.addWidget(self.correction_title)
        correction_copy.addWidget(self.correction_help)
        correction_heading.addLayout(correction_copy, 1)
        correction_layout.addLayout(correction_heading)

        self.correction_table = QTableWidget(0, 6)
        self.correction_table.setObjectName("importCorrectionTable")
        self.correction_table.setHorizontalHeaderLabels(
            ("행 제외", "원본 위치", "항목", "현재 값", "문제", "바꿀 값")
        )
        self.correction_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.correction_table.setAlternatingRowColors(True)
        self.correction_table.setWordWrap(False)
        self.correction_table.verticalHeader().setVisible(False)
        correction_header = self.correction_table.horizontalHeader()
        for column in (0, 1):
            correction_header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.ResizeToContents,
            )
        for column in (2, 3, 5):
            correction_header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.Interactive,
            )
        correction_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.correction_table.setColumnWidth(2, 120)
        self.correction_table.setColumnWidth(3, 140)
        self.correction_table.setColumnWidth(5, 190)
        self.correction_table.itemChanged.connect(self._exclude_error_row_changed)
        correction_layout.addWidget(self.correction_table, 1)

        correction_actions = QHBoxLayout()
        self.correction_close_button = QPushButton("미리보기로 돌아가기")
        self.correction_close_button.setObjectName("quietAction")
        self.correction_close_button.clicked.connect(
            self.correction_dialog.reject
        )
        correction_actions.addWidget(self.correction_close_button)
        self.correction_mapping_button = QPushButton("열 연결 다시 보기")
        self.correction_mapping_button.setObjectName("quietAction")
        self.correction_mapping_button.clicked.connect(
            self._return_to_mapping_from_correction
        )
        correction_actions.addWidget(self.correction_mapping_button)
        correction_actions.addStretch(1)
        self.correction_apply_button = QPushButton("수정·제외 적용 후 불러오기")
        self.correction_apply_button.setObjectName("primaryAction")
        self.correction_apply_button.clicked.connect(
            self._apply_error_corrections
        )
        correction_actions.addWidget(self.correction_apply_button)
        correction_layout.addLayout(correction_actions)
        dialog_layout.addWidget(self.correction_panel, 1)

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
        self.mapping_dialog.close()
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
        selected_source = self.table_combo.currentData() if self.table_combo.count() else None
        two_rows = self.source_layout_combo.currentData() == "two-row"
        header_row = self.header_row_spin.value()
        selected_table = None if two_rows else selected_source
        self._inspection = None
        self._preview = None
        self._cell_overrides.clear()
        self._excluded_rows.clear()
        self._pending_excluded_rows.clear()
        self.raw_table.clear()
        self.raw_table.setRowCount(0)
        self.raw_table.setColumnCount(0)
        self.file_status.setText("선택한 Excel 표를 확인해주세요.")
        self._refresh_actions()
        password = self.password_edit.text() or self._source_password or ""
        try:
            inspection = self._run_busy_task(
                "Excel 파일을 여는 중입니다",
                "파일 비밀번호를 확인하고 Excel 표를 읽고 있습니다. 잠시 기다려주세요.",
                lambda: inspect_workbook(
                    path_text,
                    password=password,
                    require_table=not two_rows,
                    table_name=selected_table,
                    sheet_name=selected_source if two_rows else None,
                    header_row=header_row,
                    header_row_count=2 if two_rows else 1,
                    allow_two_row_headers=True,
                ),
            )
        except (WorkbookImportPasswordRequiredError, WorkbookImportPasswordError) as error:
            self._password_retry_pending = True
            self.inspect_button.setText("비밀번호 확인하고 불러오기")
            self.file_status.setText(
                "파일 비밀번호를 입력한 뒤 Enter를 누르거나 다른 곳을 클릭하면 바로 읽습니다."
            )
            if isinstance(error, WorkbookImportPasswordError):
                self.password_edit.selectAll()
            self.password_edit.setFocus()
            self._show_error(user_error(error))
            return False
        except Exception as error:
            self._password_retry_pending = False
            self.inspect_button.setText("표 미리보기")
            if isinstance(error, WorkbookStructureError):
                self._populate_tables(error.tables, selected_table)
            self._show_error(user_error(error))
            return False
        self._password_retry_pending = False
        self._source_password = password
        self.inspect_button.setText("표 미리보기")
        self._inspection = inspection
        if inspection.selected_table is None:
            self.source_layout_combo.blockSignals(True)
            self.source_layout_combo.setCurrentIndex(self.source_layout_combo.findData("two-row"))
            self.source_layout_combo.blockSignals(False)
            self.header_row_spin.setEnabled(True)
            self._populate_sheets(inspection)
        else:
            self._populate_tables(inspection.tables, inspection.selected_table)
        self._render_raw_preview(inspection)
        source_name = inspection.selected_table or inspection.selected_sheet
        header_note = " · 2줄 헤더는 세분화된 제목 우선" if inspection.header_row_count == 2 else ""
        self.file_status.setText(
            f"{source_name} · 헤더 {len(inspection.columns)}열{header_note} · "
            f"미리보기 {len(inspection.preview_rows)}행을 확인했습니다."
        )
        try:
            self._prepare_mapping(inspection)
        except WorkbookStructureError as error:
            self._inspection = None
            self._show_error(user_error(error))
            self._refresh_actions()
            return False
        self._refresh_actions()
        return True

    def _populate_tables(
        self, tables: tuple[WorkbookTable, ...], selected_name: str | None
    ) -> None:
        if selected_name is None and tables:
            selected_name = next(
                (table.name for table in tables if table.name == "명단"),
                tables[0].name,
            )
        self._loading_structure = True
        try:
            self.table_combo.clear()
            for table in tables:
                self.table_combo.addItem(table.name, table.name)
            if selected_name is not None:
                self.table_combo.setCurrentIndex(self.table_combo.findData(selected_name))
            self.table_combo.setEnabled(bool(tables))
        finally:
            self._loading_structure = False

    def _populate_sheets(self, inspection: WorkbookInspection) -> None:
        self._loading_structure = True
        try:
            self.table_combo.clear()
            for name in inspection.sheet_names:
                self.table_combo.addItem(name, name)
            self.table_combo.setCurrentIndex(self.table_combo.findData(inspection.selected_sheet))
            self.table_combo.setEnabled(True)
        finally:
            self._loading_structure = False

    def _source_layout_changed(self, *_args) -> None:
        if self._loading_structure:
            return
        self.header_row_spin.setEnabled(self.source_layout_combo.currentData() == "two-row")
        self._invalidate_structure()

    def _header_row_changed(self, *_args) -> None:
        if self._loading_structure:
            return
        self._inspection = None
        self._preview = None
        self._cell_overrides.clear()
        self._excluded_rows.clear()
        self._pending_excluded_rows.clear()
        self.raw_table.clear()
        self.raw_table.setRowCount(0)
        self.raw_table.setColumnCount(0)
        self._refresh_actions()

    def _retry_password_if_needed(self) -> None:
        if (
            not self._password_retry_pending
            or self._busy
            or not self.password_edit.text()
        ):
            return
        self._inspect_source()

    def eventFilter(self, watched, event) -> bool:
        if (
            watched is self.password_edit
            and self._password_retry_pending
            and event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        ):
            # A disabled dialog default makes Qt fall back to the first
            # auto-default button. On this page that is the file picker, so the
            # same Enter intended to submit the password reopened Explorer.
            # Consume the key after retrying the already selected path.
            self._retry_password_if_needed()
            return True
        return super().eventFilter(watched, event)

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

    def _open_mapping_dialog(self):
        if self._inspection is None or self._busy:
            return
        from .dialog_workflow import _show_work_dialog
        self.mapping_dialog.setWindowModality(Qt.WindowModality.NonModal)
        self.mapping_dialog.setWindowState(
            (self.mapping_dialog.windowState() & ~Qt.WindowState.WindowMinimized)
            | Qt.WindowState.WindowMaximized)
        _show_work_dialog(self.mapping_dialog)
        self.mapping_dialog.raise_()
        self.mapping_dialog.activateWindow()

    def _mapping_review_key(self):
        if self._inspection is None:
            return None
        return (id(self._inspection), self.purpose_combo.currentData(),
                tuple(sorted(self._destination_selection().items())))

    def _mapping_review_complete(self):
        return self._confirmed_mapping_key is not None and self._confirmed_mapping_key == self._mapping_review_key()

    def _set_mapping_summary(self, text):
        self._mapping_summary_text = text
        self._refresh_mapping_review()

    def _refresh_mapping_review(self):
        complete = self._mapping_review_complete()
        self.mapping_summary.setText(("확인 완료 · " if complete else "필수 확인 · ") + self._mapping_summary_text)
        self.mapping_open_button.setText("열 연결 다시 보기" if complete else "열 연결 확인하기")
        if self.page_stack.currentIndex() == 1:
            self.next_button.setText("불러오기" if complete else "열 연결 확인")

    def _mapping_dialog_closed(self, result):
        if result == QDialog.DialogCode.Accepted:
            self._confirmed_mapping_key = self._mapping_review_key()
        self._refresh_mapping_review()
        if self.isVisible() and self.page_stack.currentIndex() == 1:
            self.raise_()
            self.activateWindow()
            self.mapping_open_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_database_columns(self, roster, labels=None):
        """Use the opened roster's stable custom IDs and current display names."""
        self._database_columns = roster
        self._existing_custom_fields = () if roster is None else roster.custom_fields
        self._column_labels = dict(labels or {})

    def set_document_targets(self, targets, unavailable=()):
        self.destination_table.set_document_targets(targets)
        if unavailable:
            self.destination_table.priority_note.setText(
                "·".join(unavailable) + " 양식을 읽을 수 없어 저장된 연결만 앞쪽에 표시합니다.")
            self.destination_table.priority_note.show()

    def _profile_label(self, field):
        key = {ProfileField.SCHOOL_NAME: "school", ProfileField.MOBILE_PHONE: "mobile",
               ProfileField.ROAD_ADDRESS: "address"}.get(field, field.value)
        default = "도로명주소" if field is ProfileField.ROAD_ADDRESS else _FIELD_LABELS[field].removesuffix(" *")
        return self._column_labels.get(key, default)

    def _custom_label(self, definition):
        return self._column_labels.get("custom:" + str(definition.definition_id), definition.name)

    def _destination_bindings(self, *, all_meals=False):
        bindings = {"profile:" + field.value: combo for field, combo in self._mapping_combos.items()}
        bindings.update({"driver": self.driver_combo,
                         "sequence": self.sequence_combo, "reference": self.source_reference_combo})
        if all_meals or self.purpose_combo.currentData() != ImportPurpose.VACATION_LUNCH:
            if all_meals or self.meal_mode_combo.currentData() == "combined":
                bindings["meal:combined"] = self.combined_meal_combo
            if all_meals or self.meal_mode_combo.currentData() == "flags":
                bindings.update({"meal:breakfast": self.breakfast_combo, "meal:lunch": self.lunch_combo,
                                 "meal:dinner": self.dinner_combo})
        return bindings

    def _destination_options(self):
        options = [("ignore", "가져오지 않음"), ("new", "새 칼럼 추가 (문자)")]
        options.extend(("profile:" + field.value, self._profile_label(field) + (" *" if field is ProfileField.NAME else ""))
                       for field in self._mapping_combos)
        options.extend((("driver", "배송담당"), ("sequence", self._column_labels.get("sequence", "배송순서")),
                        ("reference", "원본 연번")))
        if self.purpose_combo.currentData() != ImportPurpose.VACATION_LUNCH:
            options.extend((("meal:combined", "식종 (조식 / 중식 / 석식)"),
                        ("meal:breakfast", self._column_labels.get("status:breakfast", "조식") + " · 가입 여부"),
                        ("meal:lunch", self._column_labels.get("status:lunch", "중식") + " · 가입 여부"),
                        ("meal:dinner", self._column_labels.get("status:dinner", "석식") + " · 가입 여부")))
        type_labels = {"text": "문자", "number": "숫자", "date": "날짜", "choice": "선택"}
        options.extend(("custom:" + str(item.definition_id), f"{self._custom_label(item)} · 사용자 칼럼 ({type_labels[item.field_type.value]})")
                       for item in self._existing_custom_fields)
        if self._database_columns is None and self._suggested_profile is not None:
            known = {key for key, _ in options}
            options.extend(("custom:" + str(key), self._suggested_profile.profile.custom_field_names.get(key, "저장된 추가 칼럼") + " · 사용자 칼럼 (문자)")
                           for key in self._suggested_profile.profile.custom_field_columns if "custom:" + str(key) not in known)
        return options

    def _destination_selection(self):
        selected = {}
        for row in range(self.ignored_columns_list.count()):
            item = self.ignored_columns_list.item(row)
            source = item.data(Qt.ItemDataRole.UserRole)
            selected[source] = ("ignore" if item.checkState() == Qt.CheckState.Checked else
                                self._custom_destinations.get(source, "new"))
        for target, combo in self._destination_bindings().items():
            if combo.currentData() is not None:
                selected[str(combo.currentData())] = target
        return selected

    def _destination_changed(self, source, target):
        if self.purpose_combo.currentData() == ImportPurpose.VACATION_LUNCH and target.startswith("meal:"):
            return
        self._syncing_destinations = True
        try:
            bindings = self._destination_bindings(all_meals=True)
            for combo in bindings.values():
                if combo.currentData() == source:
                    self._set_combo(combo, None)
            self._custom_destinations.pop(source, None)
            if target.startswith("meal:"):
                if target == "meal:combined":
                    for combo in (self.breakfast_combo, self.lunch_combo, self.dinner_combo):
                        self._set_combo(combo, None)
                    self.meal_mode_combo.setCurrentIndex(self.meal_mode_combo.findData("combined"))
                else:
                    self._set_combo(self.combined_meal_combo, None)
                    self.meal_mode_combo.setCurrentIndex(self.meal_mode_combo.findData("flags"))
            if target in bindings:
                self._set_combo(bindings[target], source)
            elif target != "ignore":
                self._custom_destinations[source] = target
            for row in range(self.ignored_columns_list.count()):
                item = self.ignored_columns_list.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == source:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEnabled)
                    item.setCheckState(Qt.CheckState.Checked if target == "ignore" else Qt.CheckState.Unchecked)
                    break
        finally:
            self._syncing_destinations = False
        self._mapping_changed()

    def _suggest_existing_destinations(self, inspection):
        if self._suggested_profile is not None:
            return
        normalize = lambda text: "".join(text.split()).casefold()
        names = {}
        for field in self._mapping_combos:
            names.setdefault(normalize(self._profile_label(field)), []).append("profile:" + field.value)
        for definition in self._existing_custom_fields:
            for name in {normalize(definition.name), normalize(self._custom_label(definition))}:
                names.setdefault(name, []).append("custom:" + str(definition.definition_id))
        for column in inspection.columns:
            matches = names.get(normalize(column.header), ())
            if len(matches) > 1:
                self._destination_changed(column.letter, "ignore")
            elif len(matches) == 1:
                selected = self._destination_selection()
                if not any(source != column.letter and target == matches[0] for source, target in selected.items()):
                    self._destination_changed(column.letter, matches[0])

    def _prepare_mapping(self, inspection: WorkbookInspection) -> None:
        self._profile_name = Path(inspection.source_filename).stem or "기존 Excel 명단"
        self._mapping_initialized = False
        self._custom_destinations = {}
        self._new_destination_ids = {}
        self._source_kind = "roster-xlsx"
        self._meal_mapping_expanded = False
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
        self.lunch_filter_column.blockSignals(True)
        self.lunch_filter_column.clear()
        self.lunch_filter_column.addItem("— 칼럼 선택 —", None)
        for column in inspection.columns:
            self.lunch_filter_column.addItem(column.label, column.letter)
        self.lunch_filter_column.blockSignals(False)
        self.lunch_filter_mode.setCurrentIndex(0)
        self.lunch_filter_value.clear()
        self.ignored_columns_list.clear()
        for column in inspection.columns:
            item = QListWidgetItem(column.label)
            item.setData(Qt.ItemDataRole.UserRole, column.letter)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.ignored_columns_list.addItem(item)

        if self._database_columns is not None:
            # Keep the saved profile identity for history/no-change detection,
            # but leave every source column unconnected for manual review.
            if self._suggested_profile is not None:
                self._profile_name = self._suggested_profile.profile.name
            self.mapping_status.setText(
                "가져올 Excel 칼럼을 DB 칼럼에 직접 연결해주세요. 연결하지 않은 칼럼은 가져오지 않습니다."
            )
        elif self._suggested_profile is not None:
            self._apply_profile(self._suggested_profile.profile)
            self.mapping_status.setText(
                f"같은 구조에서 사용한 '{self._suggested_profile.profile.name}' 설정을 제안했습니다. 열 연결을 다시 확인해주세요."
            )
        else:
            self._apply_guesses(inspection)
            self.mapping_status.setText(
                "헤더 이름으로 열을 자동 연결했습니다. 자동 연결이 틀린 항목만 바꿔주세요."
            )
        if self._database_columns is None:
            self._suggest_existing_destinations(inspection)
        self._mapping_initialized = True
        self.destination_table.populate(inspection, self._destination_options(), self._destination_selection())
        self._mapping_changed()

    def _apply_profile(self, profile: ImportMappingProfile) -> None:
        self._profile_name = profile.name
        if profile.row_filter is not None:
            self.lunch_filter_mode.setCurrentIndex(self.lunch_filter_mode.findData(profile.row_filter.mode))
            self._set_combo(self.lunch_filter_column, profile.row_filter.column)
            self.lunch_filter_value.setText(profile.row_filter.value)
        self._source_kind = profile.source_kind
        for field, combo in self._mapping_combos.items():
            self._set_combo(combo, profile.profile_columns.get(field))
        if profile.default_meals:
            self.meal_mode_combo.setCurrentIndex(self.meal_mode_combo.findData("combined"))
        elif profile.combined_meals_column is not None:
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
        custom_columns = {
            source_column_letter(token, self._inspection.columns)
            for token in profile.custom_field_columns.values()
        }
        existing_ids = {item.definition_id for item in self._existing_custom_fields}
        self._custom_destinations = {
            source_column_letter(token, self._inspection.columns):
                ("custom:" if definition_id in existing_ids or self._database_columns is None else "missing:") + str(definition_id)
            for definition_id, token in profile.custom_field_columns.items()
        }
        for row in range(self.ignored_columns_list.count()):
            item = self.ignored_columns_list.item(row)
            column = str(item.data(Qt.ItemDataRole.UserRole))
            item.setCheckState(
                Qt.CheckState.Unchecked if column in custom_columns else Qt.CheckState.Checked
            )

    def _apply_guesses(self, inspection: WorkbookInspection) -> None:
        for field, combo in self._mapping_combos.items():
            self._set_combo(combo, _guess_column(field, inspection))
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
            self._source_kind = "survey-xlsx"

    def _set_combo(self, combo: QComboBox, value: object) -> None:
        if isinstance(value, str) and self._inspection is not None:
            value = source_column_letter(value, self._inspection.columns)
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _build_profile(self) -> ImportMappingProfile:
        profile_columns = {
            field: str(combo.currentData())
            for field, combo in self._mapping_combos.items()
            if combo.currentData() is not None
        }
        meal_flags = {}
        combined = None
        vacation_lunch = self.purpose_combo.currentData() == ImportPurpose.VACATION_LUNCH
        if vacation_lunch:
            pass
        elif self.meal_mode_combo.currentData() == "combined":
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
                    "식종별 열이 연결되지 않았습니다. 조식·중식·석식 열을 연결해주세요."
                )
        used = self._used_columns()
        ignored = tuple(
            str(item.data(Qt.ItemDataRole.UserRole))
            for row in range(self.ignored_columns_list.count())
            if (item := self.ignored_columns_list.item(row)).checkState()
            == Qt.CheckState.Checked
            and str(item.data(Qt.ItemDataRole.UserRole)) not in used
        )
        profile_id = (
            self._profile_id
            if self._suggested_profile is None
            else self._suggested_profile.profile_id
        )
        version = 1 if self._suggested_profile is None else self._suggested_profile.version
        previous = None if self._suggested_profile is None else self._suggested_profile.profile
        previous_ids = {} if previous is None else {
            source_column_letter(column, self._inspection.columns): definition_id
            for definition_id, column in previous.custom_field_columns.items()
        }
        custom_columns = {}
        for column in self._inspection.columns:
            if column.letter in used or column.letter in ignored:
                continue
            target = self._custom_destinations.get(column.letter)
            if target and target.startswith("missing:"):
                raise ValueError(f"{column.label}: 연결했던 DB 칼럼이 삭제되었습니다. 연결 대상을 다시 선택해주세요.")
            definition_id = (UUID(target.split(":", 1)[1]) if target and target.startswith("custom:") else
                             previous_ids.get(column.letter, uuid5(profile_id, f"custom-column:{column.letter}")))
            if target == "new":
                definition_id = self._new_destination_ids.setdefault(column.letter, uuid4())
            if definition_id in custom_columns:
                raise ValueError("같은 DB 칼럼에 여러 엑셀 열을 연결할 수 없습니다.")
            custom_columns[definition_id] = column.letter
        names = custom_column_names(self._inspection.columns)
        custom_names = {
            definition_id: (
                previous.custom_field_names.get(definition_id, names[column])
                if previous is not None else names[column]
            )
            for definition_id, column in custom_columns.items()
        }
        current_names = {item.definition_id: item.name for item in self._existing_custom_fields}
        custom_names.update({key: current_names[key] for key in custom_columns if key in current_names})
        values = dict(
            profile_id=profile_id,
            version=version,
            name=self._profile_name,
            source_kind=self._source_kind,
            profile_columns=profile_columns,
            combined_meals_column=combined,
            meal_flag_columns=meal_flags,
            default_meals=(Meal.LUNCH,) if vacation_lunch else (),
            driver_column=_combo_text(self.driver_combo),
            sequence_column=_combo_text(self.sequence_combo),
            source_reference_column=_combo_text(self.source_reference_combo),
            header_row=self._inspection.header_row,
            header_row_count=self._inspection.header_row_count,
            data_start_row=(
                next(table.data_start_row for table in self._inspection.tables
                     if table.name == self._inspection.selected_table)
                if self._inspection.selected_table is not None
                else self._inspection.header_row + self._inspection.header_row_count
            ),
            fallback_profile_columns={},
            meal_true_values=_parse_value_list(self.true_values_edit.text()),
            meal_false_values=_parse_value_list(self.false_values_edit.text()),
            date_normalization=DateNormalization.AUTO,
            phone_normalization=PhoneNormalization.KOREAN,
            ignored_columns=ignored,
            custom_field_columns=custom_columns,
            custom_field_names=custom_names,
            provider_value=None,
            row_filter=self._selected_lunch_filter() if vacation_lunch else None,
        )
        profile = ImportMappingProfile(**values)
        if self._suggested_profile is not None and profile != self._suggested_profile.profile:
            values["version"] = self._suggested_profile.version + 1
            profile = ImportMappingProfile(**values)
        return profile

    def _build_household_rule(self) -> HouseholdMatchingRule:
        criteria = (HouseholdMatchCriterion.MOBILE_PHONE,)
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
        self._render_correction_panel(preview)
        self.issue_list.clear()
        for issue in preview.notices:
            if issue.row_number is not None and issue.column is not None:
                continue
            item = QListWidgetItem(
                f"제외 · {issue.location} · {user_error(issue.message)}"
            )
            item.setForeground(QColor("#9A5B00"))
            self.issue_list.addItem(item)
        for title, detail in _preview_automatic_resolution_groups(preview):
            item = QListWidgetItem(title)
            item.setForeground(QColor("#1F6F78"))
            item.setToolTip(detail)
            self.issue_list.addItem(item)
        missing_groups = _preview_missing_groups(preview)
        for label, count, detail in missing_groups:
            item = QListWidgetItem(f"미입력 · {label} {count}명")
            item.setForeground(QColor("#49646E"))
            item.setToolTip(detail)
            self.issue_list.addItem(item)
        blocking_count = len(preview.blocking_issues)
        excluded_count = len(preview.notices)
        missing_count = sum(count for _label, count, _detail in missing_groups)
        has_notices = self.issue_list.count() > 0
        self.notice_label.setVisible(has_notices)
        self.issue_list.setVisible(has_notices)
        if has_notices or preview.issues:
            parts = [f"가져올 사람 {len(preview.observations)}명"]
            if missing_count:
                parts.append(f"미입력 {missing_count}건")
            if excluded_count:
                parts.append(f"제외 {excluded_count}행")
            if blocking_count:
                parts.append(f"수정할 오류 {blocking_count}건")
            self.preview_summary.setText(" · ".join(parts))
        else:
            self.preview_summary.setText(f"가져올 사람 {len(preview.observations)}명 · 오류 없음")

        if preview.excluded_rows:
            self.preview_summary.setText(self.preview_summary.text() + f" · 직접 제외 {len(preview.excluded_rows)}행")
        if preview.profile.row_filter is not None:
            self.preview_summary.setText(self.preview_summary.text() + f" · 조건 제외 {len(preview.filtered_rows)}행")
            self.issue_list.addItem("조건에서 빠진 기존 대상자는 그대로 유지됩니다. 가져올 명단을 확인한 뒤 불러오기를 눌러주세요.")
            self.notice_label.show()
            self.issue_list.show()

    def _render_correction_panel(
        self,
        preview: WorkbookImportPreview,
    ) -> None:
        self._correction_editors.clear()
        self._correction_issues.clear()
        self._pending_excluded_rows = set(self._excluded_rows)
        self.correction_help.setText(
            "값을 고치거나 왼쪽 체크로 원본 행 전체를 제외한 뒤 가져오세요. 원본 Excel은 바뀌지 않습니다."
            + (" 제외한 행에 연결된 기존 대상자는 그대로 유지됩니다." if self.purpose_combo.currentData() == ImportPurpose.VACATION_LUNCH else ""))
        display_issues: list[ImportIssue] = []
        seen: set[tuple[object, ...]] = set()
        for issue in preview.issues:
            editable = issue.row_number is not None and issue.column is not None
            if not issue.blocking and not editable:
                continue
            marker = (
                (issue.row_number, issue.column)
                if editable
                else (issue.row_number, issue.column, issue.message)
            )
            if marker in seen:
                continue
            seen.add(marker)
            display_issues.append(issue)

        self.correction_table.blockSignals(True)
        self.correction_table.clearContents()
        self.correction_table.setRowCount(len(display_issues))
        uneditable = False
        for row, issue in enumerate(display_issues):
            blocking = issue.blocking or self.purpose_combo.currentData() == ImportPurpose.VACATION_LUNCH
            status = "오류" if blocking else "제외"
            status_item = QTableWidgetItem(status)
            status_item.setForeground(
                Qt.GlobalColor.darkRed
                if blocking
                else QColor("#9A5B00")
            )
            if issue.row_number is not None and issue.column is not None:
                status_item.setFlags(status_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                status_item.setCheckState(Qt.CheckState.Unchecked)
                status_item.setData(Qt.ItemDataRole.UserRole, issue.row_number)
                status_item.setToolTip(f"체크하면 원본 {issue.row_number}행 전체를 가져오지 않습니다.")
            self.correction_table.setItem(row, 0, status_item)
            self.correction_table.setItem(
                row,
                1,
                QTableWidgetItem(issue.location),
            )
            self.correction_table.setItem(
                row,
                2,
                QTableWidgetItem(self._source_column_label(issue.column)),
            )
            current_text = issue.current_value or ""
            current_item = QTableWidgetItem(current_text or "(빈칸)")
            current_item.setToolTip(current_text)
            self.correction_table.setItem(row, 3, current_item)
            message_item = QTableWidgetItem(user_error(issue.message))
            message_item.setToolTip(user_error(issue.message))
            self.correction_table.setItem(row, 4, message_item)

            if issue.row_number is None or issue.column is None:
                uneditable = True
                action_item = QTableWidgetItem("열 연결 또는 원본 범위 확인")
                action_item.setForeground(QColor("#637A83"))
                self.correction_table.setItem(row, 5, action_item)
                continue
            key = (issue.row_number, issue.column)
            editor = self._build_correction_editor(preview.profile, issue)
            self._correction_editors[key] = editor
            self._correction_issues[key] = issue
            self.correction_table.setCellWidget(row, 5, editor)

        self.correction_table.blockSignals(False)
        blocking_count = sum(issue.blocking for issue in display_issues)
        excluded_count = len(display_issues) - blocking_count
        title_parts = []
        if blocking_count:
            title_parts.append(f"수정할 오류 {blocking_count}건")
        if excluded_count:
            title_parts.append(f"포함할 수 있는 제외 행 {excluded_count}건")
        title = " · ".join(title_parts)
        self.correction_title.setText(title)
        self.correction_launch_title.setText(title)
        self.correction_launch_button.setText(
            f"{len(display_issues)}건 큰 창에서 수정"
        )
        self.correction_apply_button.setEnabled(bool(self._correction_editors))
        self.correction_mapping_button.setVisible(uneditable)
        self.correction_launch_panel.setVisible(bool(display_issues))
        self._resize_correction_columns()
        if not display_issues and self.correction_dialog.isVisible():
            self.correction_dialog.accept()

        correction_count = len(preview.corrected_cells)
        excluded_count = len(preview.excluded_rows)
        if correction_count or excluded_count:
            outcome = " · 오류 검사 통과" if preview.can_continue else ""
            self.correction_applied_label.setText(
                f"고친 값 {correction_count}개 · 직접 제외 {excluded_count}행 적용 중{outcome}. "
                "원본 Excel은 그대로입니다."
            )
        self.correction_applied_panel.setVisible(correction_count > 0 or excluded_count > 0)

    def _exclude_error_row_changed(self, item):
        if item.column() != 0:
            return
        source_row = item.data(Qt.ItemDataRole.UserRole)
        if source_row is None:
            return
        excluded = item.checkState() == Qt.CheckState.Checked
        if excluded:
            self._pending_excluded_rows.add(source_row)
        else:
            self._pending_excluded_rows.discard(source_row)
        self.correction_table.blockSignals(True)
        try:
            for row in range(self.correction_table.rowCount()):
                other = self.correction_table.item(row, 0)
                if other.data(Qt.ItemDataRole.UserRole) == source_row:
                    other.setCheckState(item.checkState())
                    editor = self.correction_table.cellWidget(row, 5)
                    if editor is not None:
                        editor.setEnabled(not excluded)
        finally:
            self.correction_table.blockSignals(False)

    def _open_correction_dialog(self) -> None:
        if self.correction_table.rowCount() == 0 or self._selection is not None or self.page_stack.currentIndex() != 2:
            return
        self.correction_table.scrollToTop()
        self.correction_table.horizontalScrollBar().setValue(0)
        self.correction_dialog.setWindowModality(Qt.WindowModality.NonModal)
        from .dialog_workflow import _show_work_dialog
        _show_work_dialog(self.correction_dialog)
        self.correction_dialog.raise_()
        self.correction_dialog.activateWindow()
        QTimer.singleShot(0, self._resize_correction_columns)
        QTimer.singleShot(0, self._focus_first_correction_editor)

    def _focus_first_correction_editor(self) -> None:
        for editor in self._correction_editors.values():
            editor.setFocus(Qt.FocusReason.OtherFocusReason)
            break

    def _return_to_mapping_from_correction(self) -> None:
        self.correction_dialog.reject()
        self._set_page(1)

    def _resize_correction_columns(self) -> None:
        table = self.correction_table
        if table.columnCount() != 6:
            return
        metrics = QFontMetrics(table.font())
        dialog_width = max(640, self.correction_dialog.width() - 64)

        table.resizeColumnToContents(0)
        table.resizeColumnToContents(1)
        table.resizeColumnToContents(2)
        table.setColumnWidth(2, min(190, max(110, table.columnWidth(2))))

        current_width = 120
        for issue in self._correction_issues.values():
            current_width = max(
                current_width,
                metrics.horizontalAdvance(issue.current_value or "(빈칸)") + 32,
            )
        table.setColumnWidth(3, min(int(dialog_width * 0.28), current_width))

        editor_width = 190
        for editor in self._correction_editors.values():
            if isinstance(editor, QComboBox):
                texts = tuple(
                    editor.itemText(index) for index in range(editor.count())
                ) + (editor.currentText(),)
                padding = 54
            else:
                texts = (editor.text(), editor.placeholderText())
                padding = 36
            editor_width = max(
                editor_width,
                *(metrics.horizontalAdvance(text) + padding for text in texts),
            )
        table.setColumnWidth(5, min(int(dialog_width * 0.38), editor_width))

        # Styled editors can be taller than Qt's default 30px table rows.
        # Fit every row to the same height so index widgets never overlap.
        editor_height = max(32, metrics.height() + 18)
        for editor in self._correction_editors.values():
            editor.ensurePolished()
            editor_height = max(editor_height, editor.minimumSizeHint().height(), editor.sizeHint().height())
        row_height = editor_height + 1  # one pixel for the table grid line
        table.verticalHeader().setMinimumSectionSize(row_height)
        table.verticalHeader().setDefaultSectionSize(row_height)
        for row in range(table.rowCount()):
            table.setRowHeight(row, row_height)

    def _source_column_label(self, column: str | None) -> str:
        if column is None:
            return "파일 전체"
        if self._inspection is not None:
            source = next(
                (
                    item
                    for item in self._inspection.columns
                    if item.letter == column
                ),
                None,
            )
            if source is not None:
                return f"{source.header or '이름 없는 열'} ({column}열)"
        return f"{column}열"

    def _build_correction_editor(
        self,
        profile: ImportMappingProfile,
        issue: ImportIssue,
    ) -> QLineEdit | QComboBox:
        current = issue.current_value or ""
        column = issue.column
        combined_column = self._source_letter(profile.combined_meals_column)
        flag_columns = {
            self._source_letter(token)
            for token in profile.meal_flag_columns.values()
        }
        if column == combined_column:
            choices = (
                "",
                "조식",
                "중식",
                "석식",
                "조식 / 중식",
                "조식 / 석식",
                "중식 / 석식",
                "조식 / 중식 / 석식",
            )
        elif column in flag_columns:
            choices = tuple(
                dict.fromkeys(
                    (*profile.meal_true_values, *profile.meal_false_values)
                )
            )
        else:
            choices = ()

        custom = next((item for item in self._existing_custom_fields
                       if column is not None and self._source_letter(profile.custom_field_columns.get(item.definition_id)) == column), None)
        if custom is not None and custom.choices:
            choices = ("", *custom.choices)
        if choices:
            editor = QComboBox()
            editor.setObjectName("importCorrectionEditor")
            editor.setEditable(True)
            editor.addItems(choices)
            editor.setCurrentText(current)
            editor.setMinimumWidth(120)
            line_edit = editor.lineEdit()
            if line_edit is not None:
                line_edit.setPlaceholderText(
                    self._correction_placeholder(profile, column)
                )
                line_edit.returnPressed.connect(self._apply_error_corrections)
            return editor

        editor = QLineEdit(current)
        editor.setObjectName("importCorrectionEditor")
        editor.setPlaceholderText(self._correction_placeholder(profile, column))
        editor.setMinimumWidth(120)
        editor.returnPressed.connect(self._apply_error_corrections)
        return editor

    def _correction_placeholder(
        self,
        profile: ImportMappingProfile,
        column: str | None,
    ) -> str:
        field = next(
            (
                field
                for field, token in profile.profile_columns.items()
                if self._source_letter(token) == column
            ),
            None,
        )
        if field is ProfileField.NAME:
            return "성명 입력"
        if field is ProfileField.BIRTH_DATE:
            return "예: 20150302"
        if field in (ProfileField.TELEPHONE, ProfileField.MOBILE_PHONE):
            return "예: 010-1234-5678"
        return "바꿀 값 입력"

    def _source_letter(self, token: str | None) -> str | None:
        if token is None or self._inspection is None:
            return None
        if len(token) <= 3 and token.isalpha():
            letter = token.upper()
            if any(item.letter == letter for item in self._inspection.columns):
                return letter
        matches = tuple(
            item.letter
            for item in self._inspection.columns
            if _header_key(item.header) == _header_key(token)
        )
        return matches[0] if len(matches) == 1 else None

    def _apply_error_corrections(self) -> None:
        if self._preview is None or self._busy or self._selection is not None:
            return
        self._clear_error()
        updates: dict[tuple[int, str], object] = {}
        for key, editor in self._correction_editors.items():
            if key[0] in self._pending_excluded_rows:
                continue
            issue = self._correction_issues[key]
            value = (
                editor.currentText()
                if isinstance(editor, QComboBox)
                else editor.text()
            ).strip()
            if value == (issue.current_value or "").strip():
                continue
            updates[key] = value or None
        if not updates and self._pending_excluded_rows == self._excluded_rows:
            self._show_error("바꿀 값을 입력하거나 제외할 행을 체크해주세요.")
            return
        self._cell_overrides.update(updates)
        self._excluded_rows = set(self._pending_excluded_rows)
        self.correction_dialog.setEnabled(False)
        try:
            loaded = self._load_preview(
                self._preview.profile,
                title="수정·제외 결과를 확인하는 중입니다",
                detail="수정값과 제외할 행을 적용해 나머지 명단을 다시 검사하고 있습니다.",
            )
        finally:
            self.correction_dialog.setEnabled(True)
        if loaded:
            self._finish_import_if_valid()

    def _reset_error_corrections(self) -> None:
        if self._preview is None or not (self._cell_overrides or self._excluded_rows) or self._busy:
            return
        profile = self._preview.profile
        self._cell_overrides.clear()
        self._excluded_rows.clear()
        self._pending_excluded_rows.clear()
        loaded = self._load_preview(
            profile,
            title="수정·제외를 취소하는 중입니다",
            detail="원본 Excel 값으로 명단을 다시 검사하고 있습니다.",
        )
        if loaded and self._preview is not None and self._preview.issues:
            self._open_correction_dialog()

    def _go_next(self) -> None:
        if self._busy or self._selection is not None:
            return
        self._clear_error()
        page = self.page_stack.currentIndex()
        if page == 0:
            if self._inspection is None and not self._inspect_source():
                return
            self._set_page(1)
            return
        if page == 1:
            if not self._mapping_review_complete():
                self._open_mapping_dialog()
                return
            if self._inspection is None:
                self._set_page(0)
                return
            try:
                profile = self._build_profile()
                self._build_household_rule()
            except Exception as error:
                self._show_error(user_error(error))
                return
            if self._preview is None or self._preview.profile != profile:
                self._cell_overrides.clear()
                self._excluded_rows.clear()
                self._pending_excluded_rows.clear()
            if not self._load_preview(
                profile,
                title="가져올 명단을 만드는 중입니다",
                detail=(
                    "선택한 열을 읽고 사람·식종·배송 정보를 확인하고 있습니다. "
                    "잠시 기다려주세요."
                ),
            ):
                return
            if self._preview is not None and self._preview.issues:
                self._render_preview(self._preview)
                self._set_page(2)
                self._open_correction_dialog()
                return
            if profile.row_filter is not None:
                self._set_page(2)
                if self._preview is not None and self._preview.blocking_issues:
                    QTimer.singleShot(0, self._open_correction_dialog)
                return
            if self._finish_import_if_valid():
                return
            self._set_page(2)
            if self._preview is not None and self._preview.blocking_issues:
                QTimer.singleShot(0, self._open_correction_dialog)
            return
        if not self._finish_import_if_valid():
            self._open_correction_dialog()

    def _can_import(self) -> bool:
        return bool(
            self._preview is not None
            and self._preview.can_continue
            and not (
                self.purpose_combo.currentData() == ImportPurpose.VACATION_LUNCH
                and self._preview.notices
            )
        )

    def _finish_import_if_valid(self) -> bool:
        lunch = self.purpose_combo.currentData() == ImportPurpose.VACATION_LUNCH
        entry = self.lunch_calendar_combo.currentData() if lunch else None
        if lunch and entry is None:
            self._show_error(user_error(ValueError("적용할 방학 기록이 없습니다. 운영 일정 → 학교 일정에서 방학 파일을 불러온 뒤 명단 불러오기를 다시 열어주세요.")))
            return False
        if not self._mapping_review_complete():
            self._set_page(1)
            self._open_mapping_dialog()
            return False
        if not self._can_import():
            if self._preview is not None and self._preview.notices and self.purpose_combo.currentData() == ImportPurpose.VACATION_LUNCH:
                self._show_error("방학 중식 명단의 오류 행을 수정하거나 행 제외를 체크한 뒤 가져와주세요.")
            return False
        assert self._preview is not None
        effective_on = entry.period.starts_on if entry is not None else self.effective_date_edit.date().toPython()
        self._selection = ImportWizardSelection(
            self._preview,
            effective_on,
            self._suggested_profile,
            self._build_household_rule(),
            self._existing_household_rule,
            ImportPurpose(self.purpose_combo.currentData()),
            entry,
            LunchDuplicatePolicy(self.lunch_duplicate_combo.currentData()),
        )
        self.accept()
        return True

    def _load_preview(
        self,
        profile: ImportMappingProfile,
        *,
        title: str,
        detail: str,
    ) -> bool:
        if self._inspection is None:
            self._show_error("Excel 파일 위치를 먼저 확인해주세요.")
            return False
        path_text = self.path_edit.text().strip()
        selected_table = self._inspection.selected_table
        selected_sheet = self._inspection.selected_sheet
        password = self.password_edit.text() or self._source_password or ""
        try:
            preview = self._run_busy_task(
                title,
                detail,
                lambda: parse_workbook(
                    path_text,
                    profile,
                    table_name=selected_table,
                    sheet_name=selected_sheet,
                    password=password,
                    cell_overrides=self._cell_overrides,
                    excluded_rows=tuple(sorted(self._excluded_rows)),
                    identity_by_mobile=self.purpose_combo.currentData() == ImportPurpose.VACATION_LUNCH,
                    existing_custom_fields=self._existing_custom_fields,
                ),
            )
        except Exception as error:
            self._show_error(user_error(error))
            return False
        self.password_edit.clear()
        self._source_password = password
        self._preview = preview
        if profile.row_filter is not None or not self._can_import() or self.page_stack.currentIndex() == 2:
            self._render_preview(preview)
        self._refresh_actions()
        return True

    def _go_back(self) -> None:
        page = self.page_stack.currentIndex()
        if page > 0:
            self._set_page(page - 1)

    def _set_page(self, page: int) -> None:
        self.page_stack.setCurrentIndex(page)
        if page != 1:
            self.mapping_dialog.close()
        if page != 2:
            self.correction_dialog.close()
        self.back_button.setVisible(page > 0)
        self.next_button.setText(
            ("열 연결 확인으로", "불러오기", "불러오기")[page]
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
        if page == 1:
            self._refresh_mapping_review()
        self.back_button.setEnabled(page > 0)
        if page == 0:
            self.next_button.setEnabled(self._inspection is not None)
        elif page == 2:
            self.next_button.setEnabled(
                self._can_import()
            )
        else:
            self.next_button.setEnabled(True)

    def _used_columns(self) -> set[str]:
        meal_combos = ()
        if self.purpose_combo.currentData() != ImportPurpose.VACATION_LUNCH:
            meal_combos = (
                (self.combined_meal_combo,)
                if self.meal_mode_combo.currentData() == "combined"
                else (self.breakfast_combo, self.lunch_combo, self.dinner_combo)
            )
        return {
            str(combo.currentData())
            for combo in (
                *self._mapping_combos.values(),
                *meal_combos,
                self.driver_combo,
                self.sequence_combo,
                self.source_reference_combo,
            )
            if combo.currentData() is not None
        }

    def _mapping_changed(self, *_args) -> None:
        if not self._mapping_initialized or self._syncing_destinations:
            return
        self._syncing_destinations = True
        used = self._used_columns()
        for row in range(self.ignored_columns_list.count()):
            item = self.ignored_columns_list.item(row)
            column = str(item.data(Qt.ItemDataRole.UserRole))
            if column in used:
                item.setCheckState(Qt.CheckState.Unchecked)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            else:
                was_disabled = not bool(item.flags() & Qt.ItemFlag.ItemIsEnabled)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEnabled)
                if was_disabled and column not in self._custom_destinations:
                    item.setCheckState(Qt.CheckState.Checked)
        self._syncing_destinations = False
        options = self._destination_options()
        if self.destination_table.targets != options:
            self.destination_table.populate(self._inspection, options, self._destination_selection())
        else:
            self.destination_table.sync(self._destination_selection())
        self.destination_table.set_meal_targets_enabled(self.purpose_combo.currentData() != ImportPurpose.VACATION_LUNCH)
        self._sync_meal_mapping_visibility()

    def _selected_lunch_filter(self):
        mode = self.lunch_filter_mode.currentData()
        if mode == "all":
            return None
        column = self.lunch_filter_column.currentData()
        if column is None:
            raise ValueError("가져올 중식 명단의 조건 칼럼을 선택해주세요.")
        return ImportRowFilter(column, mode, self.lunch_filter_value.text() if mode == "equals" else "")

    def _lunch_filter_changed(self, *_args):
        mode = self.lunch_filter_mode.currentData()
        self.lunch_filter_column.setEnabled(mode != "all")
        self.lunch_filter_value.setEnabled(mode == "equals")
        self._mapping_changed()

    def _meal_mode_changed(self, *_args) -> None:
        self.lunch_filter_panel.setVisible(self.purpose_combo.currentData() == ImportPurpose.VACATION_LUNCH)
        self._lunch_filter_changed()
        self.purpose_note.setText(
            "불러온 방학 기록을 골라 <b>그 기간에만 중식을 등록</b>합니다. "
            "1차·2차 명단을 누적할 수 있으며 이번 파일에 없는 기존 대상자는 유지합니다."
            if self.purpose_combo.currentData() == ImportPurpose.VACATION_LUNCH else
            "학교·학년은 높은 쪽을 유지하고 빈칸은 기존 값을 유지합니다. 충돌하는 값만 항목별로 선택합니다."
        )
        self.lunch_mode_note.setVisible(self.purpose_combo.currentData() == ImportPurpose.VACATION_LUNCH)
        lunch = self.purpose_combo.currentData() == ImportPurpose.VACATION_LUNCH
        self.effective_date_edit.setVisible(not lunch)
        self.effective_date_label.setVisible(not lunch)
        self._sync_meal_mapping_visibility()
        self._mapping_changed()

    def set_lunch_calendar_entries(self, entries):
        self.lunch_calendar_combo.clear()
        for entry in sorted(entries, key=lambda item: item.recorded_at, reverse=True):
            self.lunch_calendar_combo.addItem(entry.label, entry)
        if not entries:
            self.lunch_calendar_combo.addItem("학교 일정에서 방학 파일을 먼저 불러와주세요", None)
        self._lunch_calendar_changed()

    def _lunch_calendar_changed(self, *_args):
        self.lunch_calendar_combo.setToolTip(self.lunch_calendar_combo.currentText())

    def _toggle_meal_mapping_details(self) -> None:
        self._meal_mapping_expanded = not self._meal_mapping_expanded
        self._sync_meal_mapping_visibility()

    def _sync_meal_mapping_visibility(self) -> None:
        vacation_lunch = self.purpose_combo.currentData() == ImportPurpose.VACATION_LUNCH
        connection_label = "자동 연결됨" if self._database_columns is None else "연결됨"
        mode_label = self._meal_form.labelForField(self.meal_mode_combo)
        if mode_label is not None:
            mode_label.setText("식종 열 형태 (자동 선택)" if self._database_columns is None else "식종 열 형태")
        self.meal_section.setVisible(not vacation_lunch)
        self.meal_detection_panel.setVisible(not vacation_lunch)
        combined = self.meal_mode_combo.currentData() == "combined"
        if vacation_lunch:
            selected = True
            summary = "방학 중식 명단 · 선택한 불러오기 조건으로 중식 배송 대상 결정"
            detail = "식종 열 없이 명단의 포함 여부로 중식 배송을 갱신합니다."
        elif combined:
            selected_column = self.combined_meal_combo.currentData()
            selected = selected_column is not None
            summary = (
                f"{connection_label} · 식종 {selected_column}열"
                if selected
                else "식종 열 하나를 연결해주세요."
            )
            detail = (
                f"식종: {self.combined_meal_combo.currentText()}"
                if selected
                else ""
            )
        else:
            selected_meals = tuple(
                (meal.value, str(combo.currentData()), combo.currentText())
                for meal, combo in (
                    (Meal.BREAKFAST, self.breakfast_combo),
                    (Meal.LUNCH, self.lunch_combo),
                    (Meal.DINNER, self.dinner_combo),
                )
                if combo.currentData() is not None
            )
            selected = bool(selected_meals)
            summary = (
                connection_label + " · "
                + " / ".join(
                    f"{meal} {column}열" for meal, column, _text in selected_meals
                )
                if selected
                else "조식·중식·석식 열을 연결해주세요."
            )
            detail = " / ".join(
                f"{meal}: {text}" for meal, _column, text in selected_meals
            )

        show_details = not vacation_lunch and (self._meal_mapping_expanded or not selected)
        self.meal_mode_combo.setVisible(show_details)
        mode_label = self._meal_form.labelForField(self.meal_mode_combo)
        if mode_label is not None:
            mode_label.setVisible(show_details)
        for field in self._combined_meal_fields:
            visible = combined and show_details
            field.setVisible(visible)
            label = self._meal_form.labelForField(field)
            if label is not None:
                label.setVisible(visible)
        for field in self._meal_flag_fields:
            visible = not combined and show_details
            field.setVisible(visible)
            label = self._meal_form.labelForField(field)
            if label is not None:
                label.setVisible(visible)
        self.meal_detection_status.setText(summary)
        self.meal_detection_status.setToolTip(detail)
        self.meal_guide.setVisible(not vacation_lunch and self._database_columns is None)
        self.meal_mapping_toggle.setVisible(not vacation_lunch and selected)
        self.meal_mapping_toggle.setText(
            "연결 요약만 보기" if show_details else "열 연결 바꾸기"
        )

    def _table_changed(self, *_args) -> None:
        if self._loading_structure:
            return
        self._inspection = None
        self._inspect_source()

    def _invalidate_structure(self, *_args) -> None:
        if self.sender() is self.path_edit and hasattr(self, "source_layout_combo"):
            self.source_layout_combo.blockSignals(True)
            self.source_layout_combo.setCurrentIndex(0)
            self.source_layout_combo.blockSignals(False)
            self.header_row_spin.blockSignals(True)
            self.header_row_spin.setValue(1)
            self.header_row_spin.blockSignals(False)
            self.header_row_spin.setEnabled(False)
        self._inspection = None
        self._preview = None
        self._cell_overrides.clear()
        self._excluded_rows.clear()
        self._pending_excluded_rows.clear()
        if hasattr(self, "raw_table"):
            self.raw_table.clear()
            self.raw_table.setRowCount(0)
            self.raw_table.setColumnCount(0)
            self.file_status.setText("파일을 선택한 뒤 표를 확인해주세요.")
        self._source_password = None
        self._password_retry_pending = False
        if hasattr(self, "inspect_button"):
            self.inspect_button.setText("표 미리보기")
        self._loading_structure = True
        try:
            self.table_combo.clear()
            self.table_combo.setEnabled(False)
        finally:
            self._loading_structure = False
        self._refresh_actions()

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _clear_error(self) -> None:
        self.error_label.clear()
        self.error_label.hide()


def _fit_dialog_to_available_screen(
    dialog: QDialog,
    *,
    desired_size: QSize,
    minimum_size: QSize,
) -> None:
    screen = dialog.screen() or QApplication.primaryScreen()
    if screen is None:
        dialog.setMinimumSize(minimum_size)
        dialog.resize(desired_size)
        return
    available = screen.availableGeometry()
    usable_width = max(1, available.width() - 48)
    usable_height = max(1, available.height() - 48)
    dialog.setMinimumSize(
        min(minimum_size.width(), usable_width),
        min(minimum_size.height(), usable_height),
    )
    dialog.resize(
        min(desired_size.width(), usable_width),
        min(desired_size.height(), usable_height),
    )


def _preview_automatic_resolution_groups(
    preview: WorkbookImportPreview,
) -> tuple[tuple[str, str], ...]:
    names = {
        observation.observation_id: observation.name
        for observation in preview.observations
    }
    grouped: dict[
        tuple[UUID, int, str],
        list[AutomaticSourceResolution],
    ] = {}
    for resolution in preview.automatic_source_resolutions:
        key = (
            resolution.observation_id,
            resolution.selected_row,
            resolution.selection_rule,
        )
        grouped.setdefault(key, []).append(resolution)

    result: list[tuple[str, str]] = []
    for (observation_id, selected_row, rule), resolutions in grouped.items():
        name = names.get(observation_id, "이름 미확인")
        labels = " / ".join(
            dict.fromkeys(resolution.field_label for resolution in resolutions)
        )
        other_rows = tuple(
            sorted(
                {
                    row
                    for resolution in resolutions
                    for row, _value in resolution.superseded_row_values
                }
            )
        )
        other_rows_text = ", ".join(f"{row}행" for row in other_rows)
        if rule == "later_excel_row":
            title = f"자동 정리 · {name} · {selected_row}행을 현재값으로 사용"
            detail = (
                f"{labels}: 아래쪽 Excel 행을 우선했습니다. "
                f"{other_rows_text} 값은 가져온 이전 배송정보로 기록하며, "
                "원본에 날짜가 없어 적용일은 미상으로 보존합니다."
            )
        else:
            title = f"자동 정리 · {name} · 더 완전한 주소 표기를 사용"
            detail = (
                f"{labels}: {selected_row}행 주소가 다른 행 주소를 포함해 "
                f"더 완전한 표기로 판단했습니다. {other_rows_text} 표기도 "
                "원본 기록에 함께 보존합니다."
            )
        result.append((title, detail))
    return tuple(result)


def _preview_missing_groups(
    preview: WorkbookImportPreview,
) -> tuple[tuple[str, int, str], ...]:
    fields = tuple(
        dict.fromkeys(
            (
                ProfileField.BIRTH_DATE,
                *(
                    field
                    for field in preview.profile.profile_columns
                    if field is not ProfileField.NAME
                ),
            )
        )
    )
    groups: list[tuple[str, int, str]] = []
    for field in fields:
        missing = tuple(
            observation
            for observation in preview.observations
            if observation.profile_values.get(field) is None
        )
        if not missing:
            continue
        label = _FIELD_LABELS[field].removesuffix(" *")
        detail = ", ".join(
            f"{observation.name} ({'/'.join(map(str, observation.source.row_numbers))}행)"
            for observation in missing
        )
        groups.append((label, len(missing), detail))
    missing_meals = tuple(
        observation for observation in preview.observations if not observation.meals
    )
    if missing_meals:
        detail = ", ".join(
            f"{observation.name} ({'/'.join(map(str, observation.source.row_numbers))}행)"
            for observation in missing_meals
        )
        groups.append(("식종", len(missing_meals), detail))
    return tuple(groups)


def _combo_text(combo: QComboBox) -> str | None:
    value = combo.currentData()
    return None if value is None else str(value)


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
