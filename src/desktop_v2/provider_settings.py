"""Dialog and controller for the DB-wide provider name."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.projections import roster_from_historical_snapshot
from src.domain.provider_settings import (
    PROVIDER_SETTING_ID,
    PROVIDER_SETTING_KIND,
    build_provider_revision,
    provider_from_snapshot,
)
from src.domain.revisions import RevisionActor
from src.domain.roster import RosterSnapshot
from src.storage import DatabaseSession

from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts


class ProviderSettingsDialog(QDialog):
    def __init__(
        self, provider: str, legacy_mixed: bool, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("공통 제공기관")
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "이 운영 데이터의 모든 명단과 생성 문서에 같은 제공기관을 사용합니다."
            )
        )
        if legacy_mixed:
            layout.addWidget(
                QLabel(
                    "기존 개인별 제공기관 값이 서로 달랐습니다. 사용할 공통 값을 입력해주세요."
                )
            )
        self._provider = QLineEdit(provider)
        self._provider.setObjectName("commonProvider")
        layout.addWidget(self._provider)
        actions = QHBoxLayout()
        cancel = QPushButton("취소")
        save = QPushButton("저장")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept)
        actions.addStretch(1)
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)

    def provider(self) -> str:
        return self._provider.text().strip()


class ProviderSettingsStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProviderSettingsOutcome:
    status: ProviderSettingsStatus
    roster: RosterSnapshot | None = None

    @property
    def completed(self) -> bool:
        return self.status is ProviderSettingsStatus.COMPLETED


class ProviderSettingsController:
    def __init__(
        self,
        session: DatabaseSession,
        *,
        actor_provider: Callable[[], RevisionActor] | None = None,
        dialog_type=ProviderSettingsDialog,
        message_box=QMessageBox,
        recorded_at_provider: Callable[[], datetime] | None = None,
        revision_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._session = session
        self._actor_provider = actor_provider or current_windows_actor
        self._dialog_type = dialog_type
        self._message_box = message_box
        self._recorded_at_provider = recorded_at_provider or (lambda: datetime.now(UTC))
        self._revision_id_factory = revision_id_factory

    def run(self, parent: QWidget | None = None) -> ProviderSettingsOutcome:
        roster = self._session.database.current_roster()
        if roster is None:
            return ProviderSettingsOutcome(ProviderSettingsStatus.FAILED)
        snapshot = self._session.database.snapshot(roster.revision_id)
        current = provider_from_snapshot(snapshot)
        stored_state = snapshot.entity(PROVIDER_SETTING_KIND, PROVIDER_SETTING_ID)
        legacy_values = {
            person.profile.provider
            for person in roster_from_historical_snapshot(snapshot).persons
            if person.profile.provider
        }
        dialog = self._dialog_type(
            current,
            stored_state is None and len(legacy_values) > 1,
            parent,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return ProviderSettingsOutcome(ProviderSettingsStatus.CANCELLED)
        provider = dialog.provider()
        if stored_state is not None and stored_state.get("provider") == provider:
            return ProviderSettingsOutcome(ProviderSettingsStatus.CANCELLED)
        try:
            prepared = build_provider_revision(
                roster.revision_id,
                stored_state,
                provider,
                actor=self._actor_provider(),
                recorded_at=self._recorded_at_provider(),
                revision_id=self._revision_id_factory(),
            )
            backup = self._session.append_revision(prepared.record)
        except Exception as error:  # noqa: BLE001 - report save failures at the UI boundary
            self._message_box.critical(
                parent, "공통 제공기관을 저장하지 못했습니다", str(error)
            )
            return ProviderSettingsOutcome(ProviderSettingsStatus.FAILED)
        if backup.error is not None:
            self._message_box.warning(
                parent,
                "공통 제공기관은 저장됐지만 자동 백업에 실패했습니다",
                str(backup.error),
            )
        return ProviderSettingsOutcome(
            ProviderSettingsStatus.COMPLETED,
            self._session.database.current_roster() or roster,
        )
