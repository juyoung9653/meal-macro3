"""Edit DB-wide recurring service and physical delivery defaults."""
from __future__ import annotations

from .dialog_workflow import DialogRequest, dialog_workflow

from .startup_loading import save_revision

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget

from src.domain.delivery_defaults import DELIVERY_DEFAULTS_ID, DELIVERY_DEFAULTS_KIND, build_delivery_defaults_revision
from src.domain.roster import HouseholdDeliverySetting, Meal, RosterSnapshot, standard_delivery_defaults, validate_delivery_defaults
from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts


class DeliveryDefaultsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._initial = validate_delivery_defaults(settings)
        self._days = {}
        self._delivery_days = {}
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("식종별 배송 기본값")
        self.resize(800, 660)
        self.setMinimumSize(680, 540)
        root = QVBoxLayout(self)
        title = QLabel("식종별 배송 기본값")
        title.setObjectName("workspaceTitle")
        root.addWidget(title)
        scope = QLabel("별도 설정이 없는 기존 대상자 전체와 앞으로 추가할 대상자에게 적용합니다. 개인·가구에 따로 지정한 항목은 유지합니다.")
        scope.setWordWrap(True)
        root.addWidget(scope)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        sections = QVBoxLayout(body)
        sections.setContentsMargins(0, 0, 0, 0)
        for meal in (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH):
            setting = next(item for item in self._initial if item.meal is meal)
            frame = QFrame()
            layout = QVBoxLayout(frame)
            heading = QHBoxLayout()
            label = QLabel(meal.value)
            label.setObjectName("sectionTitle")
            heading.addWidget(label)
            heading.addStretch(1)
            reset = QPushButton("초기화")
            reset.setObjectName("quietAction")
            reset.clicked.connect(lambda _checked=False, value=meal: self._reset(value))
            heading.addWidget(reset)
            layout.addLayout(heading)
            grid = QGridLayout()
            grid.addWidget(QLabel("제공 요일"), 0, 0)
            grid.addWidget(QLabel("배송요일"), 1, 0)
            days, delivery_days = [], []
            for index, name in enumerate(("월", "화", "수", "목", "금", "토", "일")):
                day = QCheckBox(name)
                day.setAccessibleName(f"{meal.value} {name}요일 제공")
                day.setChecked(index in setting.weekdays)
                delivery = QComboBox()
                delivery.addItems(("월", "화", "수", "목", "금", "토", "일"))
                delivery.setAccessibleName(f"{meal.value} {name}요일 제공분 배송요일")
                delivery.setCurrentIndex(setting.delivery_weekdays[index])
                delivery.setEnabled(day.isChecked())
                day.toggled.connect(delivery.setEnabled)
                day.toggled.connect(self._refresh)
                delivery.currentIndexChanged.connect(self._refresh)
                grid.addWidget(day, 0, index + 1)
                grid.addWidget(delivery, 1, index + 1)
                days.append(day)
                delivery_days.append(delivery)
            self._days[meal] = tuple(days)
            self._delivery_days[meal] = tuple(delivery_days)
            layout.addLayout(grid)
            sections.addWidget(frame)
        note = QLabel("배송요일은 제공일 당일 또는 그 이전의 가장 가까운 해당 요일입니다. 여러 제공일을 같은 날 배송해도 식수는 제공일별로 계산합니다. 중식은 학교 일정의 제공 가능일에만 제공하며, 날짜별 휴무·대체배송 지정은 유지합니다.")
        note.setWordWrap(True)
        sections.addWidget(note)
        sections.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        self._status = QLabel()
        root.addWidget(self._status)
        actions = QHBoxLayout()
        reset_all = QPushButton("전체 초기화")
        reset_all.setObjectName("quietAction")
        reset_all.setToolTip("프로그램 최초 기본값으로 복원합니다.")
        reset_all.clicked.connect(lambda: [self._reset(meal) for meal in Meal])
        actions.addWidget(reset_all)
        actions.addStretch(1)
        cancel = QPushButton("취소")
        cancel.setObjectName("quietAction")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self._save = QPushButton("저장")
        self._save.setObjectName("primaryAction")
        self._save.clicked.connect(self.accept)
        actions.addWidget(self._save)
        root.addLayout(actions)
        self._refresh()

    def settings(self):
        return validate_delivery_defaults(
            HouseholdDeliverySetting(
                meal, tuple(index for index, day in enumerate(self._days[meal]) if day.isChecked()),
                delivery_weekdays=tuple(day.currentIndex() for day in self._delivery_days[meal]),
            ) for meal in Meal
        )

    def _refresh(self, *_args):
        if not hasattr(self, "_save"):
            return
        try:
            changed = self.settings() != self._initial
        except ValueError:
            self._save.setEnabled(False)
            self._status.setText("식종마다 제공 요일을 하나 이상 선택해주세요.")
            return
        self._save.setEnabled(changed)
        self._status.setText("저장할 변경이 있습니다." if changed else "현재 기본값입니다.")

    def _reset(self, meal):
        setting = next(item for item in standard_delivery_defaults() if item.meal is meal)
        for index, day in enumerate(self._days[meal]):
            day.setChecked(index in setting.weekdays)
            self._delivery_days[meal][index].setCurrentIndex(setting.delivery_weekdays[index])
        self._refresh()


@dataclass(frozen=True, slots=True)
class DeliveryDefaultsOutcome:
    completed: bool = False
    roster: RosterSnapshot | None = None


class DeliveryDefaultsController:
    def __init__(self, session, *, dialog_type=DeliveryDefaultsDialog, message_box=QMessageBox,
                 actor_provider=current_windows_actor, recorded_at_provider=None, revision_id_factory=uuid4):
        self._session = session
        self._dialog_type = dialog_type
        self._message_box = message_box
        self._actor_provider = actor_provider
        self._recorded_at_provider = recorded_at_provider or (lambda: datetime.now(UTC))
        self._revision_id_factory = revision_id_factory

    @dialog_workflow
    def run(self, parent=None):
        roster = self._session.database.current_roster()
        if roster is None:
            return DeliveryDefaultsOutcome()
        snapshot = self._session.database.snapshot(roster.revision_id)
        dialog = self._dialog_type(roster.delivery_defaults, parent)
        if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
            return DeliveryDefaultsOutcome()
        try:
            settings = validate_delivery_defaults(dialog.settings())
            if settings == roster.delivery_defaults:
                return DeliveryDefaultsOutcome()
            record = build_delivery_defaults_revision(
                roster.revision_id, snapshot.entity(DELIVERY_DEFAULTS_KIND, DELIVERY_DEFAULTS_ID), settings,
                actor=self._actor_provider(), recorded_at=self._recorded_at_provider(), revision_id=self._revision_id_factory(),
            )
            backup = save_revision(self._session, record, parent=parent)
        except Exception as error:  # noqa: BLE001 - UI save boundary
            self._message_box.critical(parent, "배송 기본값을 저장하지 못했습니다", str(error))
            return DeliveryDefaultsOutcome()
        if backup.error is not None:
            self._message_box.warning(parent, "배송 기본값은 저장됐지만 자동 백업에 실패했습니다", str(backup.error))
        return DeliveryDefaultsOutcome(True, self._session.database.current_roster())
