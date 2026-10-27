"""Plain settings entry point for DB-backed settings and shared paths."""

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
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .theme import application_stylesheet, register_application_fonts


class SettingsAction(StrEnum):
    """The settings jobs recognizable to center staff."""

    PROVIDER = "provider"
    EXPORT_PASSWORD = "export_password"
    DOCUMENT_TEMPLATE = "document_template"
    DOCUMENT_COLUMN_MAPPING = "document_column_mapping"
    INVOICE_SETTINGS = "invoice_settings"
    DELIVERY_DEFAULTS = "delivery_defaults"
    STOPPED_HIGHLIGHT = "stopped_highlight"
    PAUSE_CONVERSION = "pause_conversion"
    SUBSTITUTE_COLUMNS = "substitute_columns"
    SEQUENCE_MODE = "sequence_mode"
    INITIAL_ROSTER_DATE = "initial_roster_date"
    MISSING_HOUSEHOLD_REPAIR = "missing_household_repair"
    DOCUMENT_DIRECTORY = "document_directory"
    OUTPUT_RETENTION = "output_retention"
    DATABASE_LOCATION = "database_location"
    BACKUP_DIRECTORY = "backup_directory"
    DATABASE_PASSWORD = "database_password"
    OLD_ADDRESS = "old_address"
    JUSO_KEY = "juso_key"
    USER_VIEW_LAYOUT = "user_view_layout"

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
    SettingsAction.OUTPUT_RETENTION: ("출력 파일 보관기간", "output 폴더의 파일을 자동 삭제할 보관기간을 정합니다. 기본은 1개월입니다.", "공통 설정으로 저장"),
    SettingsAction.USER_VIEW_LAYOUT: (
        "다른 사용자 화면 설정 가져오기",
        "같은 DB를 사용하는 다른 사용자의 시트 배치와 칼럼 설정을 가져옵니다.",
        "이 PC의 화면 설정에 적용",
    ),
    SettingsAction.SUBSTITUTE_COLUMNS: (
        "배송대타 칼럼 표시", "모든 시트에서 숨긴 칼럼을 배송대타 목록에서 제외할지 정합니다.", "이 PC의 운영 파일별 설정",
    ),
    SettingsAction.PAUSE_CONVERSION: (
        "일시중지 자동변환", "같은 달에 배송을 재개하면 중지 기간을 일시중지로 표시할지 정합니다.", "변경 이력에 기록",
    ),
    SettingsAction.OLD_ADDRESS: (
        "구주소 업데이트", "신주소·구주소 칼럼과 대상 범위를 선택해 수동으로 변환합니다.", "변경 이력에 기록",
    ),
    SettingsAction.JUSO_KEY: (
        "주소 API 키", "주소 검색 승인키를 저장·확인·삭제합니다.", "이 PC의 Windows 사용자별 저장",
    ),
    SettingsAction.INVOICE_SETTINGS: (
        "식수·청구서 설정", "다음 달 식수 포함 여부와 템플릿에서 식종 이력을 넣을 칼럼을 정합니다.", "변경 이력에 기록",
    ),
    SettingsAction.MISSING_HOUSEHOLD_REPAIR: (
        "가구 소속 기록 복구",
        "과거 명단 행 추가·방학 중식 가져오기에서 빠진 가구 소속을 확인하고 복구합니다.",
        "변경 이력에 기록",
    ),
    SettingsAction.INITIAL_ROSTER_DATE: (
        "초기 명단 적용일",
        "처음 가져온 명단의 적용일을 앞당깁니다. 이후 추가·변경된 기간은 유지합니다.",
        "변경 이력에 기록",
    ),
    SettingsAction.DELIVERY_DEFAULTS: (
        "식종별 배송 기본값",
        "별도 설정이 없는 기존·신규 대상자의 제공 요일과 배송요일을 설정합니다.",
        "변경 이력에 기록",
    ),
    SettingsAction.STOPPED_HIGHLIGHT: (
        "중지 표시 기간",
        "중지 식종을 빨간색으로 표시할 기간을 조회일 기준으로 정합니다.",
        "이 PC의 운영 파일별 설정",
    ),
    SettingsAction.SEQUENCE_MODE: (
        "배송순서 입력 방식",
        "식종별로 따로 입력하거나 한 번 입력해 세 식종에 같이 적용하도록 설정합니다.",
        "변경 이력에 기록",
    ),
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
    SettingsAction.DOCUMENT_COLUMN_MAPPING: (
        "청구서·확인서 항목 설정",
        "청구서·확인서 양식에 넣을 값과 비어 있을 때 대신 넣을 값을 지정합니다.",
        "변경 이력에 기록",
    ),
    SettingsAction.DOCUMENT_DIRECTORY: (
        "문서 저장 폴더",
        "새 문서를 모아둘 기본 폴더를 공통 설정으로 기억합니다.",
        "공통 설정으로 저장",
    ),
    SettingsAction.DATABASE_LOCATION: (
        "운영 데이터 위치",
        "현재 운영 데이터와 비밀번호 정보를 새 위치로 옮기고 다음 실행부터 사용합니다.",
        "공통 설정으로 저장",
    ),
    SettingsAction.BACKUP_DIRECTORY: (
        "자동 백업 폴더",
        "새 백업을 저장할 폴더를 선택합니다. 기존 백업은 원래 폴더에 보관합니다.",
        "공통 설정으로 저장",
    ),
    SettingsAction.DATABASE_PASSWORD: (
        "운영 파일 비밀번호 변경",
        "현재 비밀번호를 확인하고 운영 파일을 열 때 사용할 비밀번호를 바꿉니다.",
        "운영 파일 잠금 변경",
    ),
    SettingsAction.DOCUMENT_TEMPLATE: (
        "문서 양식",
        "청구서·확인서·일일식수·배송대타의 급식 또는 행복도시락 양식을 선택합니다.",
        "변경 이력에 기록",
    ),
}

_SETTINGS_STAMPS = {
    SettingsAction.OUTPUT_RETENTION: "보관",
    SettingsAction.USER_VIEW_LAYOUT: "화면",
    SettingsAction.SUBSTITUTE_COLUMNS: "칼럼",
    SettingsAction.PAUSE_CONVERSION: "변환",
    SettingsAction.OLD_ADDRESS: "주소",
    SettingsAction.JUSO_KEY: "키",
    SettingsAction.INVOICE_SETTINGS: "식수",
    SettingsAction.MISSING_HOUSEHOLD_REPAIR: "가구",
    SettingsAction.INITIAL_ROSTER_DATE: "시작",
    SettingsAction.PROVIDER: "기관",
    SettingsAction.EXPORT_PASSWORD: "잠금",
    SettingsAction.DOCUMENT_TEMPLATE: "양식",
    SettingsAction.DOCUMENT_COLUMN_MAPPING: "연결",
    SettingsAction.DELIVERY_DEFAULTS: "배송",
    SettingsAction.STOPPED_HIGHLIGHT: "중지",
    SettingsAction.SEQUENCE_MODE: "순서",
    SettingsAction.DOCUMENT_DIRECTORY: "폴더",
    SettingsAction.DATABASE_LOCATION: "DB",
    SettingsAction.BACKUP_DIRECTORY: "백업",
    SettingsAction.DATABASE_PASSWORD: "잠금",
}

_SETTINGS_GROUPS = (
    ("문서", (
        SettingsAction.DOCUMENT_TEMPLATE, SettingsAction.DOCUMENT_COLUMN_MAPPING,
        SettingsAction.PROVIDER, SettingsAction.EXPORT_PASSWORD,
        SettingsAction.DOCUMENT_DIRECTORY, SettingsAction.OUTPUT_RETENTION,
    )),
    ("배송·식수", (
        SettingsAction.DELIVERY_DEFAULTS, SettingsAction.SEQUENCE_MODE,
        SettingsAction.INVOICE_SETTINGS, SettingsAction.STOPPED_HIGHLIGHT,
        SettingsAction.PAUSE_CONVERSION,
    )),
    ("화면", (
        SettingsAction.SUBSTITUTE_COLUMNS, SettingsAction.USER_VIEW_LAYOUT,
    )),
    ("데이터 관리", (
        SettingsAction.DATABASE_LOCATION, SettingsAction.BACKUP_DIRECTORY,
        SettingsAction.DATABASE_PASSWORD, SettingsAction.OLD_ADDRESS,
        SettingsAction.JUSO_KEY, SettingsAction.INITIAL_ROSTER_DATE,
        SettingsAction.MISSING_HOUSEHOLD_REPAIR,
    )),
)


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
        style_scope = (
            SettingsAction.DOCUMENT_DIRECTORY.value
            if action in (SettingsAction.DATABASE_LOCATION, SettingsAction.BACKUP_DIRECTORY)
            else action.value
        )
        self.setObjectName("settingsActionCard")
        self.setProperty("scope", style_scope)
        self.setAccessibleName(action.label)
        self.setAccessibleDescription(action.description)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setMinimumHeight(124)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(15, 14, 16, 14)
        layout.setSpacing(15)

        stamp_label = QLabel(stamp)
        stamp_label.setObjectName("settingsActionStamp")
        stamp_label.setProperty("scope", style_scope)
        stamp_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stamp_label.setFixedSize(62, 62)
        stamp_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(stamp_label)

        copy = QVBoxLayout()
        copy.setSpacing(4)
        scope = QLabel(action.scope_label)
        scope.setObjectName("settingsScopeLabel")
        scope.setProperty("scope", style_scope)
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
        self.setMinimumSize(800, 620)
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

        for action in SettingsAction:
            card = _SettingsActionCard(action, stamp=_SETTINGS_STAMPS[action])
            card.clicked.connect(lambda _checked=False, selected=action: self._choose(selected))
            self._cards[action] = card
        self._group_actions = dict(_SETTINGS_GROUPS)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("settingsTabs")
        self._tab_scrolls = []
        for title, actions in _SETTINGS_GROUPS:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            container = QWidget()
            layout = QVBoxLayout(container)
            layout.setContentsMargins(10, 12, 10, 10)
            cards = QGridLayout()
            cards.setHorizontalSpacing(12)
            cards.setVerticalSpacing(12)
            for index, action in enumerate(actions):
                cards.addWidget(self._cards[action], index // 2, index % 2)
            cards.setColumnStretch(0, 1)
            cards.setColumnStretch(1, 1)
            layout.addLayout(cards)
            layout.addStretch(1)
            scroll.setWidget(container)
            self._tab_scrolls.append(scroll)
            self.tabs.addTab(scroll, title)
        self.tabs.setCurrentIndex(0)
        root.addWidget(self.tabs, 1)
        note = QLabel("설정별 적용 범위는 각 항목에 표시됩니다.")
        note.setObjectName("settingsHubNote")
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
        eyebrow = QLabel("문서 · 배송·식수 · 화면 · 데이터 관리")
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
