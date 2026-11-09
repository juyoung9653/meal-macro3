"""Application controller for the mandatory first roster import."""

from __future__ import annotations

from .user_messages import user_error

from .dialog_workflow import DialogRequest, dialog_workflow, invoke

from .startup_loading import save_revision

from src.storage.database_session import committed_roster_after_save

import csv
import getpass
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from io import StringIO
from uuid import UUID, uuid4

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QMessageBox, QWidget

from src.domain.revisions import RevisionActor
from src.domain.roster import Meal, RosterSnapshot
from src.domain.projections import status_events_from_historical_snapshot
from src.domain.status import MealStatus, resolve_status_history
from src.importing.application import (
    IdentityResolution,
    ReconciliationDecision,
    apply_reconciliation,
)
from src.importing.revision import (
    ImportPurpose,
    build_import_revision,
)
from src.importing.reconciliation import (
    ReconciliationDisposition,
    stage_reconciliation,
    stage_lunch_reconciliation,
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
from .lunch_value_review import LunchValueReviewDialog
from .import_value_review import ImportValueReviewDialog
from src.importing.values import import_value_rows
from src.importing.lunch_values import lunch_value_rows
from src.importing.revision import NoImportChanges


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
        value_review_type=LunchValueReviewDialog,
        shared_review_type=None,
        import_value_review_type=ImportValueReviewDialog,
        message_box=QMessageBox,
    ) -> None:
        if not isinstance(session, DatabaseSession):
            raise TypeError("session은 DatabaseSession이어야 합니다.")
        self._session = session
        self._actor_provider = actor_provider or current_windows_actor
        self._wizard_type = wizard_type
        self._review_type = review_type
        self._value_review_type = value_review_type
        self._import_value_review_type = import_value_review_type
        self._message_box = message_box
        # Local import avoids the shared-settings controller's actor-provider cycle.
        from .household_shared_columns import HouseholdSharedColumnDialog
        self._shared_review_type = shared_review_type or HouseholdSharedColumnDialog

    @dialog_workflow
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
        model = getattr(parent, "table_model", None)
        if current_roster is not None and hasattr(wizard, "set_lunch_calendar_entries"):
            from src.services.school_calendar_history import school_calendar_import_history
            try:
                wizard.set_lunch_calendar_entries(school_calendar_import_history(
                    self._session.database, current_roster.revision_id))
            except Exception as error:
                self._message_box.critical(parent, "방학 기록을 읽지 못했습니다", user_error(error))
                return GuidedImportOutcome(GuidedImportStatus.FAILED)
        labels = {column.key: column.label for column in getattr(model, "columns", ())}
        if hasattr(wizard, "set_database_columns"):
            wizard.set_database_columns(current_roster, labels)
        if hasattr(wizard, "set_document_targets"):
            latest = self._session.database.latest_revision()
            if latest is not None:
                from src.services.import_column_priority import document_import_targets
                targets, unavailable = document_import_targets(
                    self._session.database.snapshot(latest.metadata.revision_id), current_roster)
                wizard.set_document_targets(targets, unavailable)
        if current_roster is None and hasattr(wizard, "set_initial_import_defaults"):
            wizard.set_initial_import_defaults()
        if (yield DialogRequest(wizard, self._session)) != QDialog.DialogCode.Accepted:
            return GuidedImportOutcome(GuidedImportStatus.CANCELLED)
        selection = wizard.selection()
        purpose = getattr(selection, "purpose", ImportPurpose.DATABASE)
        from src.importing.vacation_lunch import LunchDuplicatePolicy
        calendar_entry = getattr(selection, "lunch_calendar_entry", None)
        duplicate_policy = getattr(selection, "lunch_duplicate_policy", LunchDuplicatePolicy.UPDATE)
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
            # A calendar/settings revision's effective date is not a lunch
            # status conflict. Check the actual lunch events below instead.
            historical = self._session.database.snapshot(latest.metadata.revision_id)
            lunch_events = status_events_from_historical_snapshot(historical, meal=Meal.LUNCH)
            if calendar_entry is None and any(event.period.starts_on is not None and event.period.starts_on > selection.effective_on for event in lunch_events):
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
        reconcile = stage_lunch_reconciliation if purpose is ImportPurpose.VACATION_LUNCH else stage_reconciliation
        plan = reconcile(
            current_roster,
            selection.preview.observations,
            effective_on=selection.effective_on,
        )
        from src.domain.household_grouping import GROUPING_KIND, GROUPING_ID, grouping_columns
        grouping_snapshot = self._session.database.snapshot(current_roster.revision_id) if current_roster is not None else None
        explicit_grouping = grouping_snapshot is not None and grouping_snapshot.entity(GROUPING_KIND, GROUPING_ID) is not None
        household_plan = None if purpose is ImportPurpose.VACATION_LUNCH else stage_household_grouping(
            selection.preview.observations,
            selection.household_rule,
            effective_on=selection.effective_on,
            roster=current_roster,
            separate_only=explicit_grouping,
        )
        review = None
        if import_decision_rows(plan, household_plan):
            review = self._review_type(
                current_roster,
                plan,
                parent,
                household_plan=household_plan,
                preview=selection.preview,
                column_labels=labels,
                **({"purpose": purpose, "lunch_delivering_ids": lunch_delivering_ids} if purpose is ImportPurpose.VACATION_LUNCH else {}),
            )
            if (yield DialogRequest(review, self._session)) != QDialog.DialogCode.Accepted:
                return GuidedImportOutcome(GuidedImportStatus.CANCELLED)
        try:
            decisions = (
                {
                    staged.observation.observation_id: ReconciliationDecision(
                        staged.observation.observation_id, IdentityResolution.CREATE_NEW, uuid4(),
                    )
                    for staged in plan.by_disposition(ReconciliationDisposition.NEW)
                } if review is None else review.decisions()
            )
            revision_id = uuid4()
            value_choices = None
            if current_roster is not None and not (
                purpose is ImportPurpose.VACATION_LUNCH
                and calendar_entry is not None and duplicate_policy is LunchDuplicatePolicy.KEEP
            ):
                candidate = apply_reconciliation(
                    current_roster, plan, new_revision_id=revision_id,
                    decisions=decisions, custom_fields=selection.preview.custom_fields,
                )
                lunch = purpose is ImportPurpose.VACATION_LUNCH
                rows = (lunch_value_rows if lunch else import_value_rows)(current_roster, candidate, plan)
                from src.importing.values import conflicting_value_rows
                value_choices = {row.key: row.proposed for row in rows if not row.automatic}
                if conflicting_value_rows(rows):
                    dialog_type = self._value_review_type if lunch else self._import_value_review_type
                    value_review = dialog_type(current_roster, candidate, plan, parent)
                    if hasattr(value_review, "export_requested"):
                        value_review.export_requested.connect(
                            lambda snapshot, dialog=value_review: self._export_value_conflicts(dialog, snapshot))
                    if (yield DialogRequest(value_review, self._session)) != QDialog.DialogCode.Accepted:
                        return GuidedImportOutcome(GuidedImportStatus.CANCELLED)
                    value_choices.update(value_review.choices())
            prepared = build_import_revision(
                current_roster,
                selection.preview,
                plan,
                decisions=decisions,
                actor=self._actor_provider(),
                recorded_at=datetime.now(UTC),
                revision_id=revision_id,
                batch_id=uuid4(),
                existing_profile=selection.existing_profile,
                household_plan=household_plan,
                household_grouping_columns=grouping_columns(grouping_snapshot),
                household_grouping_snapshot=grouping_snapshot,
                household_decisions=(
                    None if household_plan is None
                    else household_plan.default_decisions() if review is None
                    else review.household_decisions()
                ),
                existing_household_rule=(None if purpose is ImportPurpose.VACATION_LUNCH else selection.existing_household_rule),
                purpose=purpose,
                lunch_status_events=lunch_events,
                lunch_value_choices=value_choices if purpose is ImportPurpose.VACATION_LUNCH else None,
                lunch_calendar_entry=calendar_entry,
                lunch_duplicate_policy=duplicate_policy,
                value_choices=value_choices if purpose is ImportPurpose.DATABASE else None,
            )
            if current_roster is not None:
                record = yield from self._review_shared_values(prepared.record, current_roster, parent)
                if record is None:
                    return GuidedImportOutcome(GuidedImportStatus.CANCELLED)
                from .mobile_household_merge import review_mobile_household_merge
                record = yield from invoke(review_mobile_household_merge, self._session, record, parent)
                if record is None:
                    return GuidedImportOutcome(GuidedImportStatus.CANCELLED)
                prepared = replace(prepared, record=record)
            backup_result = save_revision(self._session, prepared.record, parent=parent)
        except NoImportChanges:
            self._message_box.information(parent, "기존 명단을 유지했습니다", "선택한 결과로 변경할 내용이 없어 새 변경 기록과 백업을 만들지 않았습니다.")
            return GuidedImportOutcome(GuidedImportStatus.COMPLETED, self._session.database.current_roster())
        except Exception as error:
            self._message_box.critical(
                parent,
                "명단을 저장하지 못했습니다",
                "운영 데이터에는 반영하지 않았습니다. 내용을 확인한 뒤 다시 시도해주세요.\n\n"
                + user_error(error),
            )
            return GuidedImportOutcome(GuidedImportStatus.FAILED)
        if backup_result.warning_required:
            self._message_box.warning(
                parent,
                "명단은 저장됐지만 자동 백업에 실패했습니다",
                "명단과 변경 이력은 운영 데이터에 정상 저장되었습니다. "
                "백업 폴더 연결을 확인한 뒤 프로그램에서 백업을 다시 실행해주세요.\n\n"
                + user_error(backup_result.error),
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
                (f"방학 {calendar_entry.label}\n"
                 f"중식 기간 반영 {len(present_ids)}명 · 중복 유지 {len(lunch.get('kept', ()))}명\n"
                 "학교·학년별 중식 기간에만 등록했습니다. 이번 파일에 없는 기존 대상자는 유지했습니다."
                 if calendar_entry is not None else
                 f"기존 배송 유지 {retained}명 · 배송 재개 {restored}명 · 신규 {added}명 · 미배송 전환 {len(lunch['stopped'])}명\n적용일 {selection.effective_on.isoformat()}부터 반영됩니다."),
            )
        return GuidedImportOutcome(
            GuidedImportStatus.COMPLETED,
            committed_roster_after_save(self._session, prepared.result.snapshot),
            backup_result,
        )

    def _export_value_conflicts(self, parent, snapshot):
        from src.domain.export_security import require_export_password_setting
        from .current_sheet_export import CurrentSheetExportController
        from .import_conflict_export import build_conflict_workbook
        from .startup_loading import run_progress_task

        def password():
            current = self._session.database.current_roster()
            return require_export_password_setting(
                self._session.database.snapshot(current.revision_id)).password

        parent.export_button.setEnabled(False)
        try:
            shared_password = run_progress_task(password, parent=parent,
                title="Excel 설정 확인", message="공통 Excel 비밀번호를 확인합니다.")
            CurrentSheetExportController(workbook_builder=build_conflict_workbook,
                message_box=self._message_box).run(snapshot, shared_password, parent,
                    directory=self._session.path.parent)
        except Exception as error:
            self._message_box.critical(parent, "불일치를 내보내지 못했습니다", user_error(error))
        finally:
            parent.export_button.setEnabled(bool(parent.rows))

    def _review_shared_values(self, record, current_roster, parent):
        from src.domain.household_shared import (
            HouseholdSharedConflict, expand_household_shared_revision,
            household_shared_components, household_shared_rows,
        )
        from src.domain.import_shared import (
            IMPORT_SHARED_VALUES, apply_import_shared_values,
            import_candidate_snapshot, with_import_shared_values,
        )
        from src.domain.projections import roster_from_historical_snapshot

        snapshot = self._session.database.snapshot(current_roster.revision_id)
        while True:
            try:
                # Dry-run the same rules used by storage, before attempting a save.
                expand_household_shared_revision(snapshot, record)
                return record
            except HouseholdSharedConflict as conflict:
                key, on = conflict.column_key, conflict.effective_on
                if key in record.metadata.decisions.get(IMPORT_SHARED_VALUES, {}):
                    raise
            candidate = import_candidate_snapshot(snapshot, apply_import_shared_values(snapshot, record))
            roster = roster_from_historical_snapshot(candidate)
            components = household_shared_components(roster, key, snapshot=candidate)
            rows = household_shared_rows(roster, key, on, snapshot=candidate)
            values = {row.household_id: row.values for row in rows}
            conflicts = tuple(row for row in rows if any(value is None for value in row.values))
            label = ({"sequence": "배송순서", "delivery_settings": "배송 설정",
                      "status:breakfast": "조식", "status:lunch": "중식", "status:dinner": "석식"}
                     .get(key, components[0].label))
            dialog = self._shared_review_type(label, True, components, conflicts or rows, on, parent)
            dialog.setWindowTitle("명단 불러오기 · 공용 값 확인 · " + label)
            dialog.enabled.setEnabled(False)
            dialog.note.setText(
                f"가져올 명단의 {on:%Y-%m-%d} 기준 공용 값을 확인해주세요. "
                "왼쪽 값 셀을 클릭하면 오른쪽 입력칸이 그 내용으로 교체되며 직접 수정할 수도 있습니다. "
                "모든 칼럼을 확인한 뒤 명단과 함께 저장합니다. 중간에 취소하면 아무것도 저장하지 않습니다."
            )
            dialog.buttons.button(QDialogButtonBox.StandardButton.Save).setText("다음")

            def validate(enabled, draft_values):
                draft = with_import_shared_values(record, key, on, {**values, **draft_values})
                apply_import_shared_values(snapshot, draft)

            dialog.validation_callback = validate
            if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
                return None
            record = with_import_shared_values(record, key, on, {**values, **dialog.selected_values()})

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
            raise ValueError("활성 가구 묶기 rule이 둘 이상이라 불러오기를 시작할 수 없습니다.")
        return (
            import_profiles_from_snapshot(snapshot),
            household_rules[0] if household_rules else None,
        )


class GuidedInitialImportController(GuidedRosterImportController):
    """Do not expose a blank roster; cancel returns to the startup boundary."""

    def run(self, parent: QWidget | None = None) -> GuidedImportOutcome:
        if self._session.database.current_roster() is not None:
            raise ValueError("처음 명단 불러오기는 저장된 명단이 없을 때만 할 수 있습니다.")
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
