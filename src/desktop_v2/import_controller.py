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
from uuid import uuid4

from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

from src.domain.revisions import RevisionActor
from src.domain.roster import RosterSnapshot
from src.importing import (
    StoredHouseholdMatchingRule,
    StoredImportProfile,
    build_import_revision,
    household_matching_rules_from_snapshot,
    import_profiles_from_snapshot,
    stage_household_grouping,
    stage_reconciliation,
)
from src.storage import AutomaticBackupResult, DatabaseSession

from .import_review import ImportReviewDialog
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
            parent=parent,
        )
        if wizard.exec() != QDialog.DialogCode.Accepted:
            return GuidedImportOutcome(GuidedImportStatus.CANCELLED)
        selection = wizard.selection()
        plan = stage_reconciliation(
            current_roster,
            selection.preview.observations,
            effective_on=selection.effective_on,
        )
        household_plan = stage_household_grouping(
            selection.preview.observations,
            selection.household_rule,
            effective_on=selection.effective_on,
            roster=current_roster,
        )
        review = self._review_type(
            current_roster,
            plan,
            parent,
            household_plan=household_plan,
        )
        if review.exec() != QDialog.DialogCode.Accepted:
            return GuidedImportOutcome(GuidedImportStatus.CANCELLED)
        try:
            prepared = build_import_revision(
                current_roster,
                selection.preview,
                plan,
                decisions=review.decisions(),
                actor=self._actor_provider(),
                recorded_at=datetime.now(UTC),
                revision_id=uuid4(),
                batch_id=uuid4(),
                existing_profile=selection.existing_profile,
                household_plan=household_plan,
                household_decisions=review.household_decisions(),
                existing_household_rule=selection.existing_household_rule,
            )
            backup_result = self._session.append_revision(prepared.record)
        except Exception as error:
            self._message_box.critical(
                parent,
                "명단을 저장하지 못했습니다",
                "DB에는 반영하지 않았습니다. 내용을 확인한 뒤 다시 시도해주세요.\n\n"
                + str(error),
            )
            return GuidedImportOutcome(GuidedImportStatus.FAILED)
        if backup_result.error is not None:
            self._message_box.warning(
                parent,
                "명단은 저장됐지만 자동 백업에 실패했습니다",
                "명단과 변경 이력은 DB에 정상 저장되었습니다. "
                "백업 폴더 연결을 확인한 뒤 프로그램에서 백업을 다시 실행해주세요.\n\n"
                + str(backup_result.error),
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
            raise ValueError("최초 가져오기는 리비전이 없는 DB에서만 실행할 수 있습니다.")
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
