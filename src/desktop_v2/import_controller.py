"""Application controller for the mandatory first roster import."""

from __future__ import annotations

import csv
import getpass
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

from src.domain.revisions import RevisionActor
from src.domain.roster import Meal, RosterSnapshot
from src.domain.projections import status_events_from_historical_snapshot
from src.domain.status import MealStatus, resolve_status_history
from src.importing.application import (
    IdentityResolution,
    ReconciliationDecision,
)
from src.importing.revision import (
    ImportPurpose,
    build_import_revision,
)
from src.importing.reconciliation import (
    ReconciliationDisposition,
    stage_reconciliation,
)
from src.importing.households import (
    StoredHouseholdMatchingRule,
    household_matching_rules_from_snapshot,
    stage_household_grouping,
)
from src.importing.profile_history import (
    StoredImportProfile,
    import_profiles_from_snapshot,
)
from src.storage.database_session import (
    AutomaticBackupResult,
    DatabaseSession,
)

from .import_review import ImportReviewDialog, import_decision_rows
from .import_wizard import RosterImportWizard


class GuidedImportStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class GuidedImportOutcome:
    status: GuidedImportStatus
    roster: RosterSnapshot | None = None
    backup_result: AutomaticBackupResult | None = None

    @property
    def completed(self) -> bool:
        return self.status is GuidedImportStatus.COMPLETED


class GuidedRosterImportController:
    """Review and commit an initial or later workbook as one revision."""

    def __init__(
        self,
        session: DatabaseSession,
        *,
        actor_provider: Callable[[], RevisionActor] = None,
        wizard_type=RosterImportWizard,
        review_type=ImportReviewDialog,
        message_box=QMessageBox,
    ) -> None:
        if not isinstance(session, DatabaseSession):
            raise TypeError("session은 DatabaseSession이어야 합니다.")
        self._session = session
        self._actor_provider = actor_provider or current_windows_actor
        self._wizard_type = wizard_type
        self._review_type = review_type
        self._message_box = message_box

    def run(self, parent: QWidget | None = None) -> GuidedImportOutcome:
        current_roster = self._session.database.current_roster()
        profiles, existing_household_rule = self._current_import_settings()
        wizard = self._wizard_type(
            existing_profiles=profiles,
            existing_household_rule=existing_household_rule,
            initial_directory=self._session.path.parent,
            allow_vacation_lunch=current_roster is not None,
            parent=parent,
        )
        if wizard.exec() != QDialog.DialogCode.Accepted:
            return GuidedImportOutcome(GuidedImportStatus.CANCELLED)
        selection = wizard.selection()
        purpose = getattr(selection, "purpose", ImportPurpose.DATABASE)
        lunch_events = ()
        lunch_delivering_ids = ()
        if purpose is ImportPurpose.VACATION_LUNCH:
            if current_roster is None:
                raise ValueError("방학 중식 명단에는 기존 운영 명단이 필요합니다.")
            if selection.preview.notices:
                self._message_box.warning(parent, "제외된 행 확인 필요", "원본에서 제외된 행이 있습니다. 방학 중식 명단을 모두 수정한 뒤 다시 가져와주세요.")
                return GuidedImportOutcome(GuidedImportStatus.CANCELLED)
            latest = self._session.database.latest_revision()
            assert latest is not None
            if latest.metadata.effective_on > selection.effective_on:
                self._message_box.warning(parent, "적용일 확인 필요", "현재 명단에 적용일 이후의 변경이 있습니다. 방학 중식 적용일을 다시 선택해주세요.")
                return GuidedImportOutcome(GuidedImportStatus.CANCELLED)
            historical = self._session.database.snapshot(latest.metadata.revision_id)
            lunch_events = status_events_from_historical_snapshot(historical, meal=Meal.LUNCH)
            if any(event.period.starts_on is not None and event.period.starts_on > selection.effective_on for event in lunch_events):
                self._message_box.warning(parent, "적용일 확인 필요", "적용일 뒤에 이미 예약된 중식 상태 변경이 있습니다. 적용일을 다시 선택해주세요.")
                return GuidedImportOutcome(GuidedImportStatus.CANCELLED)
            lunch_delivering_ids = tuple(
                person.person_id for person in current_roster.persons
                if Meal.LUNCH in current_roster.meals_for(person.person_id, on=selection.effective_on)
                and resolve_status_history(
                    person.person_id, Meal.LUNCH,
                    tuple(event for event in lunch_events if event.person_id == person.person_id),
                ).status_on(selection.effective_on) is MealStatus.DELIVERY
            )
        plan = stage_reconciliation(
            current_roster,
            selection.preview.observations,
            effective_on=selection.effective_on,
        )
        household_plan = None if purpose is ImportPurpose.VACATION_LUNCH else stage_household_grouping(
            selection.preview.observations,
            selection.household_rule,
            effective_on=selection.effective_on,
            roster=current_roster,
        )
        review = None
        if import_decision_rows(plan, household_plan):
            review = self._review_type(
                current_roster,
                plan,
                parent,
                household_plan=household_plan,
                **({"purpose": purpose, "lunch_delivering_ids": lunch_delivering_ids} if purpose is ImportPurpose.VACATION_LUNCH else {}),
            )
            if review.exec() != QDialog.DialogCode.Accepted:
                return GuidedImportOutcome(GuidedImportStatus.CANCELLED)
        try:
            prepared = build_import_revision(
                current_roster,
                selection.preview,
                plan,
                decisions=(
                    {
                        staged.observation.observation_id: ReconciliationDecision(
                            staged.observation.observation_id,
                            IdentityResolution.CREATE_NEW,
                            uuid4(),
                        )
                        for staged in plan.by_disposition(ReconciliationDisposition.NEW)
                    }
                    if review is None else review.decisions()
                ),
                actor=self._actor_provider(),
                recorded_at=datetime.now(UTC),
                revision_id=uuid4(),
                batch_id=uuid4(),
                existing_profile=selection.existing_profile,
                household_plan=household_plan,
                household_decisions=(
                    None if household_plan is None
                    else household_plan.default_decisions() if review is None
                    else review.household_decisions()
                ),
                existing_household_rule=(None if purpose is ImportPurpose.VACATION_LUNCH else selection.existing_household_rule),
                purpose=purpose,
                lunch_status_events=lunch_events,
            )
            backup_result = self._session.append_revision(prepared.record)
        except Exception as error:
            self._message_box.critical(
                parent,
                "명단을 저장하지 못했습니다",
                "운영 데이터에는 반영하지 않았습니다. 내용을 확인한 뒤 다시 시도해주세요.\n\n"
                + str(error),
            )
            return GuidedImportOutcome(GuidedImportStatus.FAILED)
        if backup_result.error is not None:
            self._message_box.warning(
                parent,
                "명단은 저장됐지만 자동 백업에 실패했습니다",
                "명단과 변경 이력은 운영 데이터에 정상 저장되었습니다. "
                "백업 폴더 연결을 확인한 뒤 프로그램에서 백업을 다시 실행해주세요.\n\n"
                + str(backup_result.error),
            )
        if purpose is ImportPurpose.VACATION_LUNCH and current_roster is not None:
            lunch = prepared.record.metadata.decisions["vacation_lunch"]
            present_ids = {UUID(value) for value in lunch["present"]}
            new_ids = {person.person_id for person in prepared.result.snapshot.persons} - {person.person_id for person in current_roster.persons}
            retained = len(present_ids & set(lunch_delivering_ids))
            restored = len(present_ids - set(lunch_delivering_ids) - new_ids)
            added = len(present_ids & new_ids)
            self._message_box.information(
                parent, "방학 중식 명단을 반영했습니다",
                f"기존 배송 유지 {retained}명 · 배송 재개 {restored}명 · 신규 {added}명 · 미배송 전환 {len(lunch['stopped'])}명\n적용일 {selection.effective_on.isoformat()}부터 반영됩니다.",
            )
        return GuidedImportOutcome(
            GuidedImportStatus.COMPLETED,
            prepared.result.snapshot,
            backup_result,
        )

    def _current_import_settings(
        self,
    ) -> tuple[
        tuple[StoredImportProfile, ...],
        StoredHouseholdMatchingRule | None,
    ]:
        latest = self._session.database.latest_revision()
        if latest is None:
            return (), None
        snapshot = self._session.database.snapshot(latest.metadata.revision_id)
        household_rules = household_matching_rules_from_snapshot(snapshot)
        if len(household_rules) > 1:
            raise ValueError("활성 가구 묶기 rule이 둘 이상이라 가져오기를 시작할 수 없습니다.")
        return (
            import_profiles_from_snapshot(snapshot),
            household_rules[0] if household_rules else None,
        )


class GuidedInitialImportController(GuidedRosterImportController):
    """Do not expose a blank roster; cancel returns to the startup boundary."""

    def run(self, parent: QWidget | None = None) -> GuidedImportOutcome:
        if self._session.database.current_roster() is not None:
            raise ValueError("처음 명단 가져오기는 저장된 명단이 없을 때만 할 수 있습니다.")
        return super().run(parent)


def current_windows_actor() -> RevisionActor:
    """Resolve the interactive Windows account without storing extra identity data."""

    username = getpass.getuser().strip()
    if not username:
        raise RuntimeError("현재 Windows 사용자명을 확인할 수 없습니다.")
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ("whoami", "/user", "/fo", "csv", "/nh"),
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            creationflags=creation_flags,
        )
        row = next(csv.reader(StringIO(completed.stdout)))
        sid = row[-1].strip()
    except (OSError, subprocess.SubprocessError, StopIteration, csv.Error) as error:
        raise RuntimeError("현재 Windows 사용자 SID를 확인할 수 없습니다.") from error
    if not sid.upper().startswith("S-"):
        raise RuntimeError("현재 Windows 사용자 SID 형식이 올바르지 않습니다.")
    return RevisionActor(username, sid)
