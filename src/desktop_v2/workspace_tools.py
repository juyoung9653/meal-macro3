"""Compact entry points for related roster work."""
from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QListWidget, QMenu,
    QListWidgetItem, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from .theme import application_stylesheet, register_application_fonts
from .monthly_statistics import MonthlyStatisticsDialog
from .monthly_meal_history import MonthlyMealHistoryDialog
from src.domain.household_shared import available_shared_column_keys


def _shell(dialog, title):
    register_application_fonts()
    dialog.setWindowTitle(title)
    dialog.setStyleSheet(application_stylesheet())
    root = QVBoxLayout(dialog)
    root.setContentsMargins(20, 18, 20, 18)
    root.setSpacing(12)
    label = QLabel(title)
    label.setObjectName("workspaceTitle")
    root.addWidget(label)
    return root


def _button(text, callback, primary=False):
    button = QPushButton(text)
    button.setObjectName("primaryAction" if primary else "quietAction")
    button.clicked.connect(callback)
    return button


class ExportChoiceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.choice = None
        self.resize(520, 270)
        root = _shell(self, "Excel 내보내기")
        for key, title, detail in (
            ("sheet", "현재 시트", "지금 보이는 행과 칼럼을 현재 순서대로 저장합니다."),
            ("database", "전체 데이터", "전체 명단·민원·정보 및 식종 변경 이력을 저장합니다."),
        ):
            root.addWidget(_button(title, lambda _checked=False, key=key: self._choose(key), True))
            note = QLabel(detail)
            note.setWordWrap(True)
            root.addWidget(note)
        root.addWidget(_button("취소", self.reject))

    def _choose(self, key):
        self.choice = key
        self.accept()


class MonthlyOverviewDialog(QDialog):
    def __init__(self, selected_day, load_statistics, load_history, parent=None):
        super().__init__(parent)
        self.resize(940, 710)
        self.setMinimumSize(780, 520)
        root = _shell(self, "월별 조회")
        period = QHBoxLayout()
        period.addWidget(_button("이전 달", lambda: self._shift(-1)))
        self.year = QSpinBox()
        self.year.setRange(1900, 9999)
        self.year.setSuffix("년")
        self.year.setValue(selected_day.year)
        self.month = QComboBox()
        for month in range(1, 13):
            self.month.addItem(f"{month}월", month)
        self.month.setCurrentIndex(selected_day.month - 1)
        period.addWidget(self.year)
        period.addWidget(self.month)
        period.addWidget(_button("다음 달", lambda: self._shift(1)))
        period.addWidget(_button("새로고침", self.refresh))
        period.addStretch()
        root.addLayout(period)
        self.tabs = QTabWidget()
        self.statistics = MonthlyStatisticsDialog(selected_day, load_statistics, self, embedded=True)
        self.history = MonthlyMealHistoryDialog(selected_day, load_history, self, embedded=True)
        self.tabs.addTab(self.statistics, "식수")
        self.tabs.addTab(self.history, "식종 변경")
        root.addWidget(self.tabs, 1)
        root.addWidget(_button("닫기", self.accept))
        self.year.valueChanged.connect(self.refresh)
        self.month.currentIndexChanged.connect(self.refresh)
        self.tabs.currentChanged.connect(self.refresh)
        self.refresh()

    def _shift(self, offset):
        year, month = divmod(self.year.value() * 12 + self.month.currentData() - 1 + offset, 12)
        if 1900 <= year <= 9999:
            with QSignalBlocker(self.year), QSignalBlocker(self.month):
                self.year.setValue(year)
                self.month.setCurrentIndex(month)
            self.refresh()

    def refresh(self, *_):
        page = self.tabs.currentWidget()
        with QSignalBlocker(page.year), QSignalBlocker(page.month):
            page.year.setValue(self.year.value())
            page.month.setCurrentIndex(self.month.currentIndex())
        page.refresh()


class OperatingScheduleDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.choice = None
        self.resize(600, 320)
        root = _shell(self, "운영 일정")
        self.tabs = QTabWidget()
        for title, description in (
            ("학교 일정", "학교별 중식 일정을 수정하거나 Excel에서 가져옵니다.\n학교 급식일과 학년별 미급식 기간을 관리합니다."),
            ("휴무·공휴일", "공휴일 운영과 업체 휴무를 관리합니다.\n미제공·정상 운영·대체식 제공을 지정합니다."),
        ):
            page = QWidget()
            layout = QVBoxLayout(page)
            label = QLabel(description)
            label.setWordWrap(True)
            layout.addWidget(label)
            layout.addStretch()
            self.tabs.addTab(page, title)
        root.addWidget(self.tabs, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(_button("닫기", self.reject))
        self.open_button = _button("학교 일정 관리", self._choose, True)
        actions.addWidget(self.open_button)
        root.addLayout(actions)
        self.tabs.currentChanged.connect(lambda i: self.open_button.setText(self.tabs.tabText(i) + " 관리"))

    def _choose(self):
        self.choice = "school" if self.tabs.currentIndex() == 0 else "service"
        self.accept()


class ColumnManagerDialog(QDialog):
    """Edit local visibility immediately; keep domain field edits in their controller."""
    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self.resize(540, 560)
        self.setMinimumSize(460, 400)
        root = _shell(self, "칼럼 관리")
        note = QLabel("체크한 칼럼을 현재 시트에 표시합니다. 변경은 바로 적용됩니다.")
        note.setWordWrap(True)
        root.addWidget(note)
        self.columns = QListWidget()
        root.addWidget(self.columns, 1)
        self._reload()
        self.columns.itemChanged.connect(self._toggle)
        actions = QHBoxLayout()
        actions.addWidget(_button("기본 배치 복원", self._reset))
        actions.addWidget(_button("사용자 항목 추가·삭제", self._custom_fields))
        actions.addStretch()
        root.addLayout(actions)
        self.shared_button = _button("칼럼 가구 공용 설정", self._shared)
        self._shared_menu = QMenu(self)
        root.addWidget(self.shared_button)
        root.addWidget(_button("닫기", self.accept))
        window.table_model.modelReset.connect(self._reload)

    def _reload(self):
        with QSignalBlocker(self.columns):
            self.columns.clear()
            model, table = self._window.table_model, self._window._table
            header = table.horizontalHeader()
            for visual in range(header.count()):
                logical = header.logicalIndex(visual)
                column = model.columns[logical]
                item = QListWidgetItem(column.label)
                item.setData(Qt.ItemDataRole.UserRole, column.key)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                if column.key in self._window._column_layout._mandatory_keys:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked if table.isColumnHidden(logical) else Qt.CheckState.Checked)
                self.columns.addItem(item)

    def _toggle(self, item):
        self._window._column_layout._set_hidden(item.data(Qt.ItemDataRole.UserRole), item.checkState() != Qt.CheckState.Checked)

    def _reset(self):
        self._window._column_layout.reset()
        self._reload()

    def _custom_fields(self):
        self.accept()
        self._window.custom_fields_requested.emit()

    def _shared(self):
        self._shared_menu.clear()
        allowed = available_shared_column_keys(self._window.roster)
        shared = self._window.table_model._household_shared_columns
        for index in range(self.columns.count()):
            item = self.columns.item(index)
            key = item.data(Qt.ItemDataRole.UserRole)
            if key not in allowed:
                continue
            action = self._shared_menu.addAction(item.text())
            action.setData(key)
            action.setCheckable(True)
            action.setChecked(key in shared)
            action.setToolTip("체크하면 가구 공용으로 사용하고, 해제하면 현재 값을 유지하며 공용 사용을 끝냅니다.")
            action.triggered.connect(lambda checked=False, key=key: self._select_shared(key))
        self._shared_menu.popup(self.shared_button.mapToGlobal(self.shared_button.rect().bottomLeft()))

    def _select_shared(self, key):
        self.accept()
        self._window.household_shared_column_requested.emit(key)
