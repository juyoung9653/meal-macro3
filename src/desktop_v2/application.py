"""Startup coordinator that gates the roster window behind a real first import."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

from src.domain.delivery_routing import routing_state_from_snapshot
from src.domain.roster import Meal
from src.domain.status import resolve_status_history
from src.domain.revisions import format_revision_timestamp
from src.domain.school_calendar import school_lunch_calendar_from_snapshot
from src.storage.database_session import DatabaseSession

from .custom_fields import CustomFieldController
from .complaints import ComplaintController
from .database_export import DatabaseExportController
from .person_history import create_person_history_dialog
from .inspector_history import InspectorMealHistory
from .database_access import BackupDirectoryDialog, CreateDatabaseDialog, DatabaseLocationDialog, DatabasePasswordChangeDialog, DesktopStorageSettings, LoginDialog
from .delivery_routing import DeliveryRoutingController
from .document_completion import (
    DocumentCompletionDialog,
    GeneratedDocumentResult,
)
from .document_generation import DocumentGenerationController
from .document_hub import DocumentAction, DocumentHubDialog, DocumentSetupRequest
from .document_meal import DocumentMealDialog
from .document_output import (
    DefaultDocumentDirectoryDialog,
    DocumentOutputDialog,
    DocumentOutputMode,
)
from .document_period import DocumentPeriodDialog
from .document_source import DocumentSourceDialog
from .document_template import DocumentTemplateController
from .export_password import ExportPasswordController
from .import_controller import (
    GuidedImportStatus,
    GuidedInitialImportController,
    GuidedRosterImportController,
)
from .meal_status_edit import MealStatusChangeController
from .person_edit import PersonProfileEditController
from .roster_cell_edit import RosterCellEditController
from .roster_row_edit import RosterRowEditController
from .provider_settings import ProviderSettingsController
from .delivery_defaults import DeliveryDefaultsController
from .roster_window import RosterWindow
from .sequence_edit import SequenceEditController, SequenceSaveStatus
from .school_calendar_edit import (
    SchoolCalendarEditController,
    SchoolCalendarEditStatus,
)
from .school_calendar_import import SchoolCalendarImportController
from .service_adjustment import ServiceAdjustmentController
from .settings_hub import SettingsAction, SettingsHubDialog
from .unit_price_edit import UnitPriceChangeController


class StartupStatus(StrEnum):
    READY = "ready"
    CANCELLED = "cancelled"
    ONBOARDING_PENDING = "onboarding_pending"
    FAILED = "failed"


@dataclass(slots=True)
class DesktopStartupOutcome:
    status: StartupStatus
    session: DatabaseSession | None = None
    window: RosterWindow | None = None

    @property
    def ready(self) -> bool:
        return self.status is StartupStatus.READY

    def close(self) -> None:
        try:
            if self.window is not None:
                self.window.close()
        finally:
            if self.session is not None:
                self.session.close()
            self.window = None
            self.session = None


class DesktopStartupController:
    """Create/login, mandatory import, then and only then construct the roster UI."""

    def __init__(
        self,
        *,
        settings: DesktopStorageSettings | None = None,
        create_dialog_type=CreateDatabaseDialog,
        login_dialog_type=LoginDialog,
        guided_controller_type=GuidedInitialImportController,
        followup_controller_type=GuidedRosterImportController,
        profile_edit_controller_type=PersonProfileEditController,
        roster_cell_edit_controller_type=RosterCellEditController,
        roster_row_controller_type=RosterRowEditController,
        custom_field_controller_type=CustomFieldController,
        routing_controller_type=DeliveryRoutingController,
        complaint_controller_type=ComplaintController,
        database_export_controller_type=DatabaseExportController,
        provider_settings_controller_type=ProviderSettingsController,
        delivery_defaults_controller_type=DeliveryDefaultsController,
        meal_status_controller_type=MealStatusChangeController,
        service_adjustment_controller_type=ServiceAdjustmentController,
        school_calendar_controller_type=SchoolCalendarImportController,
        school_calendar_edit_controller_type=SchoolCalendarEditController,
        unit_price_controller_type=UnitPriceChangeController,
        export_password_controller_type=ExportPasswordController,
        document_template_controller_type=DocumentTemplateController,
        document_column_mapping_controller_type=None,
        document_generation_controller_type=DocumentGenerationController,
        document_hub_dialog_type=DocumentHubDialog,
        document_source_dialog_type=DocumentSourceDialog,
        document_period_dialog_type=DocumentPeriodDialog,
        document_meal_dialog_type=DocumentMealDialog,
        document_output_dialog_type=DocumentOutputDialog,
        document_completion_dialog_type=DocumentCompletionDialog,
        settings_hub_dialog_type=SettingsHubDialog,
        database_location_dialog_type=DatabaseLocationDialog,
        backup_directory_dialog_type=BackupDirectoryDialog,
        database_password_dialog_type=DatabasePasswordChangeDialog,
        document_directory_dialog_type=DefaultDocumentDirectoryDialog,
        roster_window_type=RosterWindow,
        message_box=QMessageBox,
    ) -> None:
        self._settings = settings or DesktopStorageSettings()
        self._create_dialog_type = create_dialog_type
        self._login_dialog_type = login_dialog_type
        self._guided_controller_type = guided_controller_type
        self._followup_controller_type = followup_controller_type
        self._profile_edit_controller_type = profile_edit_controller_type
        self._roster_cell_edit_controller_type = roster_cell_edit_controller_type
        self._roster_row_controller_type = roster_row_controller_type
        self._custom_field_controller_type = custom_field_controller_type
        self._routing_controller_type = routing_controller_type
        self._complaint_controller_type = complaint_controller_type
        self._database_export_controller_type = database_export_controller_type
        self._provider_settings_controller_type = provider_settings_controller_type
        self._delivery_defaults_controller_type = delivery_defaults_controller_type
        self._meal_status_controller_type = meal_status_controller_type
        self._service_adjustment_controller_type = (
            service_adjustment_controller_type
        )
        self._school_calendar_controller_type = school_calendar_controller_type
        self._school_calendar_edit_controller_type = (
            school_calendar_edit_controller_type
        )
        self._unit_price_controller_type = unit_price_controller_type
        self._export_password_controller_type = export_password_controller_type
        self._document_template_controller_type = document_template_controller_type
        self._document_column_mapping_controller_type = document_column_mapping_controller_type
        self._document_generation_controller_type = document_generation_controller_type
        self._document_hub_dialog_type = document_hub_dialog_type
        self._document_source_dialog_type = document_source_dialog_type
        self._document_period_dialog_type = document_period_dialog_type
        self._document_meal_dialog_type = document_meal_dialog_type
        self._document_output_dialog_type = document_output_dialog_type
        self._document_completion_dialog_type = document_completion_dialog_type
        self._settings_hub_dialog_type = settings_hub_dialog_type
        self._database_location_dialog_type = database_location_dialog_type
        self._backup_directory_dialog_type = backup_directory_dialog_type
        self._database_password_dialog_type = database_password_dialog_type
        self._document_directory_dialog_type = document_directory_dialog_type
        self._roster_window_type = roster_window_type
        self._message_box = message_box

    def start(self, parent: QWidget | None = None) -> DesktopStartupOutcome:
        access_dialog = None
        outcome = None
        try:
            access_dialog = self._access_dialog(parent)
            outcome = self._start_with_dialog(access_dialog, parent)
            return outcome
        except Exception as error:
            self._message_box.critical(parent, "운영 데이터를 열지 못했습니다", str(error))
            return DesktopStartupOutcome(StartupStatus.FAILED)
        finally:
            if access_dialog is not None and (outcome is None or not outcome.ready):
                session = getattr(access_dialog, "session", None)
                if session is not None:
                    session.close()
                release = getattr(access_dialog, "release_reservation", None)
                if release is not None:
                    release()

    def _start_with_dialog(self, access_dialog, parent) -> DesktopStartupOutcome:
        if access_dialog.exec() != QDialog.DialogCode.Accepted:
            return DesktopStartupOutcome(StartupStatus.CANCELLED)
        session = access_dialog.session
        if session is None:
            self._message_box.critical(
                parent,
                "운영 데이터를 열지 못했습니다",
                "운영 데이터를 열 수 없습니다. 프로그램을 다시 시작해주세요.",
            )
            return DesktopStartupOutcome(StartupStatus.FAILED)
        initial_backup = getattr(access_dialog, "backup_result", None)
        if initial_backup is not None and initial_backup.error is not None:
            self._message_box.warning(
                parent,
                "운영 데이터는 만들었지만 첫 백업에 실패했습니다",
                "운영 데이터는 정상 생성되었습니다. 백업 폴더 연결을 확인해주세요.\n\n"
                + str(initial_backup.error),
            )

        roster = session.database.current_roster()
        if roster is None:
            guided = self._guided_controller_type(session)
            outcome = guided.run(parent)
            if not outcome.completed:
                session.close()
                status = (
                    StartupStatus.ONBOARDING_PENDING
                    if outcome.status is GuidedImportStatus.CANCELLED
                    else StartupStatus.FAILED
                )
                return DesktopStartupOutcome(status)
            roster = outcome.roster
        if roster is None or not roster.persons:
            session.close()
            self._message_box.critical(
                parent,
                "명단이 아직 없습니다",
                "기존 Excel 명단 가져오기를 완료해야 운영 화면을 열 수 있습니다.",
            )
            return DesktopStartupOutcome(StartupStatus.ONBOARDING_PENDING)

        histories = self._histories(session, roster)
        window = self._roster_window_type(
            roster,
            histories,
            revision_display_code=self._revision_display_code(session, roster),
            view_settings_scope=str(session.credentials.database_id),
        )
        self._refresh_school_calendar(session, window, roster)
        routing_setter = getattr(window, "set_routing_state", None)
        if callable(routing_setter):
            routing_setter(routing_state_from_snapshot(session.database.snapshot(roster.revision_id)))
        history_setter = getattr(window, "set_history_provider", None)
        if callable(history_setter):
            history_setter(
                lambda person, meal, parent: self._create_person_history(
                    session, person, meal, parent
                )
            )
        meal_history_setter = getattr(window, "set_meal_change_history_provider", None)
        if callable(meal_history_setter):
            meal_history = InspectorMealHistory(session.database)
            meal_history_setter(
                lambda person: meal_history.rows_for(window.roster, person.person_id)
            )
        for signal_name, handler in (
            ("complaint_requested", lambda person_id, meal: self._run_complaint(session, window, person_id, meal)),
            ("database_export_requested", lambda: self._run_database_export(session, window)),
            ("driver_replacement_requested", lambda: self._run_driver_replacement(session, window)),
            ("drivers_requested", lambda: self._run_driver_registry(session, window)),
            ("driver_assignment_requested", lambda person_id, meal: self._run_driver_assignment(session, window, person_id, meal)),
            ("batch_driver_assignment_requested", lambda targets: self._run_batch_driver_assignment(session, window, targets)),
            ("sequence_edit_requested", lambda person_id: self._run_sequence_edit(session, window, person_id)),
            ("household_sequence_save_requested", lambda targets, value, dialog: self._save_household_sequence(session, window, targets, value, dialog)),
            ("person_add_requested", lambda selected_meal: self._run_roster_row_add(session, window, selected_meal)),
            ("person_delete_requested", lambda person_id: self._run_roster_row_delete(session, window, person_id)),
            ("relocation_requested", lambda person_id: self._run_relocation(session, window, person_id)),
            ("cell_edit_requested", lambda person_id, column_key, text: self._run_roster_cell_edit(session, window, person_id, column_key, text)),
        ):
            signal = getattr(window, signal_name, None)
            if signal is not None and hasattr(signal, "connect"):
                signal.connect(handler)
        import_signal = getattr(window, "import_review_requested", None)
        if import_signal is not None and hasattr(import_signal, "connect"):
            import_signal.connect(
                lambda: self._run_followup_import(session, window)
            )
        person_edit_signal = getattr(window, "person_edit_requested", None)
        if person_edit_signal is not None and hasattr(person_edit_signal, "connect"):
            person_edit_signal.connect(
                lambda person_id: self._run_person_edit(
                    session,
                    window,
                    person_id,
                )
            )
        household_delivery_signal = getattr(window, "delivery_settings_requested", None)
        if household_delivery_signal is not None and hasattr(household_delivery_signal, "connect"):
            household_delivery_signal.connect(
                lambda person_id: self._run_person_edit(session, window, person_id, delivery_only=True)
            )
        status_signal = getattr(window, "status_action_requested", None)
        if status_signal is not None and hasattr(status_signal, "connect"):
            status_signal.connect(
                lambda person_id, meal, status: self._run_meal_status_change(
                    session,
                    window,
                    person_id,
                    meal,
                    status,
                )
            )
        batch_signal = getattr(window, "batch_status_action_requested", None)
        if batch_signal is not None and hasattr(batch_signal, "connect"):
            batch_signal.connect(lambda targets, status: self._run_batch_meal_status_change(session, window, targets, status))
        unit_price_signal = getattr(window, "unit_price_requested", None)
        if unit_price_signal is not None and hasattr(unit_price_signal, "connect"):
            unit_price_signal.connect(
                lambda: self._run_unit_price_change(session, window)
            )
        service_adjustment_signal = getattr(
            window,
            "service_adjustment_requested",
            None,
        )
        if service_adjustment_signal is not None and hasattr(
            service_adjustment_signal,
            "connect",
        ):
            service_adjustment_signal.connect(
                lambda: self._run_service_adjustment_change(session, window)
            )
        school_calendar_signal = getattr(
            window,
            "school_calendar_requested",
            None,
        )
        if school_calendar_signal is not None and hasattr(
            school_calendar_signal,
            "connect",
        ):
            school_calendar_signal.connect(
                lambda: self._run_school_calendar(session, window)
            )
        custom_fields_signal = getattr(window, "custom_fields_requested", None)
        if custom_fields_signal is not None and hasattr(custom_fields_signal, "connect"):
            custom_fields_signal.connect(lambda: self._run_custom_fields(session, window))
        settings_signal = getattr(window, "settings_requested", None)
        if settings_signal is not None and hasattr(settings_signal, "connect"):
            settings_signal.connect(
                lambda: self._run_settings_hub(session, window)
            )
        documents_signal = getattr(window, "documents_requested", None)
        if documents_signal is not None and hasattr(documents_signal, "connect"):
            documents_signal.connect(
                lambda: self._run_document_hub(session, window)
            )
        generated_signal = getattr(window, "document_generated", None)
        if generated_signal is not None and hasattr(generated_signal, "connect"):
            generated_signal.connect(
                lambda result: self._show_document_completion(result, window)
            )
        output_selected_signal = getattr(
            window,
            "document_output_selected",
            None,
        )
        if output_selected_signal is not None and hasattr(
            output_selected_signal,
            "connect",
        ):
            output_selected_signal.connect(
                lambda selection: self._run_document_generation(
                    session,
                    window,
                    selection,
                )
            )
        return DesktopStartupOutcome(StartupStatus.READY, session, window)

    def _apply_routing_outcome(self, session, window, outcome) -> None:
        if outcome.roster is not None:
            self._replace_roster_if_changed(session, window, outcome.roster)
            window.set_routing_state(outcome.routing_state)

    def _run_driver_registry(self, session, window) -> None:
        outcome = self._routing_controller_type(session).run_drivers(parent=window)
        self._apply_routing_outcome(session, window, outcome)

    def _run_driver_replacement(self, session, window) -> None:
        outcome = self._routing_controller_type(session).run_replacement(
            window.table_model.service_day, parent=window
        )
        self._apply_routing_outcome(session, window, outcome)

    def _run_driver_assignment(self, session, window, person_id, meal) -> None:
        outcome = self._routing_controller_type(session).run_assignment(
            person_id, meal, window.table_model.service_day, parent=window
        )
        self._apply_routing_outcome(session, window, outcome)

    def _run_batch_driver_assignment(self, session, window, targets) -> None:
        outcome = self._routing_controller_type(session).run_batch_assignment(
            targets, window.table_model.service_day, parent=window
        )
        self._apply_routing_outcome(session, window, outcome)

    def _run_sequence_edit(self, session, window, person_id) -> None:
        roster = SequenceEditController(session).run(person_id, parent=window)
        if roster is not None:
            self._replace_roster_if_changed(session, window, roster)

    def _save_household_sequence(self, session, window, targets, value, dialog) -> None:
        result = SequenceEditController(session).save_value(targets, value)
        if result.status is SequenceSaveStatus.SAVED:
            self._replace_roster_if_changed(session, window, result.roster)
            dialog.replace_rows(window.driver_household_rows())
        dialog.show_sequence_result(result)

    def _run_complaint(self, session, window, person_id, meal) -> None:
        outcome = self._complaint_controller_type(session).run(
            person_id, meal, window, household_day=window.table_model.service_day
        )
        if outcome.completed and outcome.roster is not None:
            self._replace_roster_if_changed(session, window, outcome.roster)

    def _run_database_export(self, session, window) -> None:
        password = self._export_password_controller_type(session).ensure_configured(window)
        if not password.completed:
            return
        if password.roster is not None:
            self._replace_roster_if_changed(session, window, password.roster)
        self._database_export_controller_type(session).run(
            window.table_model.service_day, window,
            directory=self._settings.document_output_directory,
        )

    def _create_person_history(self, session, person, meal, parent):
        try:
            return create_person_history_dialog(session, person, meal, parent)
        except Exception as error:
            self._message_box.critical(parent, "이력을 읽지 못했습니다", str(error))
            return None

    def _run_relocation(self, session, window, person_id) -> None:
        outcome = self._routing_controller_type(session).run_relocation(
            person_id, window.table_model.service_day, parent=window
        )
        self._apply_routing_outcome(session, window, outcome)

    def _run_custom_fields(self, session: DatabaseSession, window) -> None:
        outcome = self._custom_field_controller_type(session).run(parent=window)
        if outcome.roster is not None:
            self._replace_roster_if_changed(session, window, outcome.roster)

    def _run_document_hub(self, session: DatabaseSession, window) -> None:
        hub = self._document_hub_dialog_type(parent=window)
        if hub.exec() != QDialog.DialogCode.Accepted:
            return
        action = hub.selected_action
        if action not in (
            DocumentAction.INVOICE,
            DocumentAction.CONFIRMATION,
            DocumentAction.DELIVERY_REPORT,
        ):
            self._message_box.information(
                window,
                f"{action.label} 연결 준비 중",
                "현재 단계에서는 청구서, 확인서와 배송 리포트를 만들 수 있습니다.",
            )
            return
        if not self._ensure_document_prerequisites(session, window, action):
            return
        source = self._document_source_dialog_type(
            session.database.revision_summaries(),
            parent=window,
        )
        if source.exec() != QDialog.DialogCode.Accepted:
            return
        setup = DocumentSetupRequest(
            action,
            source.selected_summary,
            window.table_model.service_day,
            window.table_model.meal,
        )
        period = self._document_period_dialog_type(setup, parent=window)
        if period.exec() != QDialog.DialogCode.Accepted:
            return
        meal = self._document_meal_dialog_type(
            period.selected_period(),
            parent=window,
        )
        if meal.exec() != QDialog.DialogCode.Accepted:
            return
        output = self._document_output_dialog_type(
            meal.selection(),
            self._settings.document_output_directory,
            parent=window,
        )
        if output.exec() != QDialog.DialogCode.Accepted:
            return
        selection = output.selection()
        if selection.mode is DocumentOutputMode.NEW_DEFAULT:
            try:
                self._settings.save_document_output_directory(selection.directory)
            except OSError as error:
                self._message_box.critical(window, "문서 저장 폴더를 저장하지 못했습니다", str(error))
                return
        selected_signal = getattr(window, "document_output_selected", None)
        if selected_signal is not None and hasattr(selected_signal, "emit"):
            selected_signal.emit(selection)

    def _show_document_completion(self, result, window) -> None:
        try:
            if not isinstance(result, GeneratedDocumentResult):
                raise TypeError("문서 생성 결과가 올바르지 않습니다.")
            result.verify_file()
            dialog = self._document_completion_dialog_type(
                result,
                parent=window,
            )
        except (OSError, TypeError, ValueError) as error:
            self._message_box.critical(
                window,
                "문서 완료 상태를 확인하지 못했습니다",
                "파일이 없거나 생성 결과가 달라 완료 화면을 열 수 없습니다.\n\n"
                + str(error),
            )
            return
        dialog.exec()

    def _run_document_generation(self, session, window, selection) -> None:
        controller = self._document_generation_controller_type(session)
        outcome = controller.run(selection, window)
        if not outcome.completed or outcome.result is None:
            return
        if outcome.roster is not None:
            self._replace_roster_if_changed(session, window, outcome.roster)
        generated_signal = getattr(window, "document_generated", None)
        if generated_signal is not None and hasattr(generated_signal, "emit"):
            generated_signal.emit(outcome.result)

    def _run_settings_hub(self, session: DatabaseSession, window) -> None:
        hub = self._settings_hub_dialog_type(parent=window)
        if hub.exec() != QDialog.DialogCode.Accepted:
            return
        if hub.selected_action is SettingsAction.DATABASE_LOCATION:
            dialog = self._database_location_dialog_type(session, self._settings, parent=window)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                window.close()
            return
        if hub.selected_action is SettingsAction.BACKUP_DIRECTORY:
            dialog = self._backup_directory_dialog_type(session, self._settings, parent=window)
            dialog.exec()
            return
        if hub.selected_action is SettingsAction.DATABASE_PASSWORD:
            dialog = self._database_password_dialog_type(session, parent=window)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            if not dialog.password_changed:
                self._message_box.information(window, "변경 없음", "현재 비밀번호와 새 비밀번호가 같습니다.")
            elif (dialog.backup_result.error is not None or
                  dialog.backup_result.rotation_error is not None or
                  (dialog.backup_result.rotation is not None and
                   (dialog.backup_result.rotation.failed or dialog.backup_result.rotation.unreadable))):
                rotation = dialog.backup_result.rotation
                updated = 0 if rotation is None else len(rotation.updated)
                failed = 0 if rotation is None else len(rotation.failed)
                unreadable = 0 if rotation is None else len(rotation.unreadable)
                details = []
                if dialog.backup_result.error is not None:
                    details.append(f"새 백업 오류: {dialog.backup_result.error}")
                if dialog.backup_result.rotation_error is not None:
                    details.append(f"기존 백업 처리 오류: {dialog.backup_result.rotation_error}")
                if rotation is not None:
                    details.extend(f"{path.name}: {reason}" for path, reason in rotation.failed)
                    details.extend(f"{path.name}: 읽거나 검증할 수 없음" for path in rotation.unreadable)
                title = "비밀번호 변경 완료 · 백업 확인 필요"
                summary = (
                    "운영 파일 비밀번호는 새 비밀번호로 변경됐습니다.\n"
                    f"기존 백업 갱신: {updated}개, 실패: {failed}개, 확인 불가: {unreadable}개.\n"
                    "갱신 실패 백업은 변경 전 상태로 보존됐습니다. 마지막으로 해당 백업을 열던 비밀번호는 파일마다 다를 수 있습니다. "
                    "확인 불가 파일은 손상 가능성이 있으니 별도로 확인해주세요.\n"
                    "설정된 백업 폴더 밖의 복사본도 기존 비밀번호를 사용합니다."
                )
                if self._message_box is QMessageBox and details:
                    warning = QMessageBox(QMessageBox.Icon.Warning, title, summary, QMessageBox.StandardButton.Ok, window)
                    warning.setDetailedText("\n".join(details))
                    warning.exec()
                else:
                    self._message_box.warning(window, title, summary)
            else:
                rotation = dialog.backup_result.rotation
                backup_note = (
                    f"설정된 백업 폴더의 기존 백업 {len(rotation.updated)}개도 새 비밀번호로 엽니다. "
                    if rotation is not None else
                    "자동 백업 폴더가 설정되지 않아 기존 백업은 변경하지 않았습니다. "
                )
                self._message_box.information(
                    window,
                    "비밀번호 변경 완료",
                    "운영 파일 비밀번호가 변경됐습니다. 앞으로 새 비밀번호로 열 수 있습니다. "
                    + backup_note
                    + "폴더 밖의 복사본은 기존 비밀번호를 사용합니다.",
                )
            return
        if hub.selected_action is SettingsAction.DELIVERY_DEFAULTS:
            outcome = self._delivery_defaults_controller_type(session).run(parent=window)
            if outcome.completed and outcome.roster is not None:
                self._replace_roster_if_changed(session, window, outcome.roster)
            return
        if hub.selected_action is SettingsAction.PROVIDER:
            outcome = self._provider_settings_controller_type(session).run(parent=window)
            if outcome.completed and outcome.roster is not None:
                self._replace_roster_if_changed(session, window, outcome.roster)
            return
        if hub.selected_action is SettingsAction.EXPORT_PASSWORD:
            self._run_export_password_change(session, window)
            return
        if hub.selected_action is SettingsAction.DOCUMENT_TEMPLATE:
            self._run_document_template_change(session, window)
            return
        if hub.selected_action is SettingsAction.DOCUMENT_COLUMN_MAPPING:
            self._run_document_column_mapping(session, window)
            return
        dialog = self._document_directory_dialog_type(
            self._settings.document_output_directory,
            parent=window,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self._settings.save_document_output_directory(dialog.selected_directory)
        except OSError as error:
            self._message_box.critical(window, "문서 저장 폴더를 저장하지 못했습니다", str(error))

    def _run_followup_import(self, session: DatabaseSession, window) -> None:
        controller = self._followup_controller_type(session)
        outcome = controller.run(window)
        if not outcome.completed:
            return
        roster = outcome.roster
        if roster is None:
            self._message_box.critical(
                window,
                "가져온 명단을 표시하지 못했습니다",
                "명단은 저장됐지만 새 화면 상태를 확인할 수 없습니다. 프로그램을 다시 열어주세요.",
            )
            return
        self._replace_roster(session, window, roster)

    def _run_person_edit(self, session: DatabaseSession, window, person_id, *, delivery_only: bool = False) -> None:
        controller = self._profile_edit_controller_type(session)
        outcome = (
            controller.run_delivery_settings(person_id, window, service_day=window.table_model.service_day)
            if delivery_only else controller.run(person_id, window)
        )
        if not outcome.completed:
            return
        roster = outcome.roster
        if roster is None:
            self._message_box.critical(
                window,
                "수정한 사람 정보를 표시하지 못했습니다",
                "정보는 저장됐지만 새 화면 상태를 확인할 수 없습니다. "
                "프로그램을 다시 열어주세요.",
            )
            return
        self._replace_roster(session, window, roster)

    def _run_roster_cell_edit(
        self,
        session: DatabaseSession,
        window,
        person_id,
        column_key: str,
        text: str,
    ) -> None:
        outcome = self._roster_cell_edit_controller_type(session).save(
            person_id, column_key, text, window
        )
        if not outcome.completed:
            return
        roster = outcome.roster
        if roster is None:
            self._message_box.critical(
                window,
                "수정한 명단 셀을 표시하지 못했습니다",
                "값은 저장됐지만 새 화면 상태를 확인할 수 없습니다. 프로그램을 다시 열어주세요.",
            )
            return
        self._replace_roster(session, window, roster)

    def _run_roster_row_add(self, session: DatabaseSession, window, selected_meal) -> None:
        outcome = self._roster_row_controller_type(session).add(
            selected_meal, window, service_day=window.table_model.service_day,
        )
        self._apply_roster_row_outcome(session, window, outcome)

    def _run_roster_row_delete(self, session: DatabaseSession, window, person_id) -> None:
        outcome = self._roster_row_controller_type(session).delete(person_id, window)
        self._apply_roster_row_outcome(session, window, outcome)

    def _apply_roster_row_outcome(self, session: DatabaseSession, window, outcome) -> None:
        if not outcome.completed:
            return
        roster = outcome.roster
        if roster is None:
            self._message_box.critical(window, "변경된 명단을 표시하지 못했습니다", "명단은 저장됐지만 화면을 갱신할 수 없습니다. 프로그램을 다시 열어주세요.")
            return
        window.replace_roster(roster, self._histories(session, roster), revision_display_code=self._revision_display_code(session, roster))
        routing_setter = getattr(window, "set_routing_state", None)
        if callable(routing_setter):
            routing_setter(routing_state_from_snapshot(session.database.snapshot(roster.revision_id)))
        self._refresh_school_calendar(session, window, roster)

    def _run_meal_status_change(
        self,
        session: DatabaseSession,
        window,
        person_id,
        meal,
        status,
    ) -> None:
        controller = self._meal_status_controller_type(session)
        outcome = controller.run(
            person_id,
            meal,
            status,
            window.table_model.service_day,
            window,
        )
        if not outcome.completed:
            return
        roster = outcome.roster
        if roster is None:
            self._message_box.critical(
                window,
                "변경한 배송 상태를 표시하지 못했습니다",
                "상태는 저장됐지만 새 화면 상태를 확인할 수 없습니다. "
                "프로그램을 다시 열어주세요.",
            )
            return
        self._replace_roster(session, window, roster)

    def _run_batch_meal_status_change(self, session, window, targets, status) -> None:
        outcome = self._meal_status_controller_type(session).run_batch(
            targets, status, window.table_model.service_day, window,
        )
        if outcome.completed and outcome.roster is not None:
            self._replace_roster_if_changed(session, window, outcome.roster)

    def _run_unit_price_change(self, session: DatabaseSession, window) -> None:
        controller = self._unit_price_controller_type(session)
        outcome = controller.run(window)
        if not outcome.completed:
            return
        roster = outcome.roster
        if roster is None:
            self._message_box.critical(
                window,
                "변경한 단가를 표시하지 못했습니다",
                "단가는 저장됐지만 새 화면 상태를 확인할 수 없습니다. 프로그램을 다시 열어주세요.",
            )
            return
        self._replace_roster(session, window, roster)

    def _run_service_adjustment_change(
        self,
        session: DatabaseSession,
        window,
    ) -> None:
        controller = self._service_adjustment_controller_type(session)
        outcome = controller.run(window)
        if not outcome.completed:
            return
        roster = outcome.roster
        if roster is None:
            self._message_box.critical(
                window,
                "변경한 휴무 일정을 표시하지 못했습니다",
                "일정은 저장됐지만 새 화면 상태를 확인할 수 없습니다. "
                "프로그램을 다시 열어주세요.",
            )
            return
        self._replace_roster(session, window, roster)

    def _run_school_calendar(
        self,
        session: DatabaseSession,
        window,
    ) -> None:
        controller = self._school_calendar_edit_controller_type(session)
        outcome = controller.run(window)
        if outcome.status is SchoolCalendarEditStatus.IMPORT_REQUESTED:
            self._run_school_calendar_import(session, window)
            return
        if not outcome.completed:
            return
        roster = outcome.roster
        if roster is None:
            self._message_box.critical(
                window,
                "수정한 학교 일정을 표시하지 못했습니다",
                "일정은 저장됐지만 새 화면 상태를 확인할 수 없습니다. "
                "프로그램을 다시 열어주세요.",
            )
            return
        self._replace_roster(session, window, roster)

    def _run_school_calendar_import(
        self,
        session: DatabaseSession,
        window,
    ) -> None:
        controller = self._school_calendar_controller_type(session)
        outcome = controller.run(window)
        if not outcome.completed:
            return
        roster = outcome.roster
        if roster is None:
            self._message_box.critical(
                window,
                "가져온 학교 일정을 표시하지 못했습니다",
                "일정은 저장됐지만 새 화면 상태를 확인할 수 없습니다. "
                "프로그램을 다시 열어주세요.",
            )
            return
        self._replace_roster(session, window, roster)

    def _run_export_password_change(
        self,
        session: DatabaseSession,
        window,
    ) -> None:
        controller = self._export_password_controller_type(session)
        outcome = controller.run(window)
        if not outcome.completed:
            return
        roster = outcome.roster
        if roster is None:
            self._message_box.critical(
                window,
                "변경한 Excel 비밀번호 상태를 표시하지 못했습니다",
                "비밀번호는 저장됐지만 새 화면 상태를 확인할 수 없습니다. 프로그램을 다시 열어주세요.",
            )
            return
        self._replace_roster_if_changed(session, window, roster)

    def _run_document_template_change(
        self,
        session: DatabaseSession,
        window,
    ) -> None:
        controller = self._document_template_controller_type(session)
        outcome = controller.run(window)
        if not outcome.completed or outcome.roster is None:
            return
        self._replace_roster_if_changed(session, window, outcome.roster)

    def _run_document_column_mapping(self, session: DatabaseSession, window) -> None:
        controller_type = self._document_column_mapping_controller_type
        if controller_type is None:
            from .document_column_mapping import DocumentColumnMappingController
            controller_type = DocumentColumnMappingController
        outcome = controller_type(session).run(window)
        if outcome.completed and outcome.roster is not None:
            self._replace_roster_if_changed(session, window, outcome.roster)

    def _ensure_document_prerequisites(
        self,
        session: DatabaseSession,
        window,
        action: DocumentAction,
    ) -> bool:
        if action in (DocumentAction.INVOICE, DocumentAction.CONFIRMATION):
            template = self._document_template_controller_type(
                session
            ).ensure_configured(window)
            if not template.completed:
                return False
            if template.roster is not None:
                self._replace_roster_if_changed(session, window, template.roster)

        password = self._export_password_controller_type(
            session
        ).ensure_configured(window)
        if not password.completed:
            return False
        if password.roster is not None:
            self._replace_roster_if_changed(session, window, password.roster)
        return True

    def _replace_roster_if_changed(self, session, window, roster) -> bool:
        displayed = getattr(window, "roster", None)
        if (
            displayed is not None
            and displayed.revision_id == roster.revision_id
        ):
            return False
        self._replace_roster(session, window, roster)
        return True

    def _replace_roster(self, session, window, roster) -> None:
        window.replace_roster(
            roster,
            self._histories(session, roster),
            revision_display_code=self._revision_display_code(session, roster),
        )
        self._refresh_school_calendar(session, window, roster)

    @staticmethod
    def _refresh_school_calendar(session, window, roster) -> None:
        setter = getattr(window, "set_school_calendar", None)
        if callable(setter):
            setter(school_lunch_calendar_from_snapshot(
                session.database.snapshot(roster.revision_id)
            ))

    @staticmethod
    def _histories(session: DatabaseSession, roster):
        if not roster.persons:
            return {}
        events_by_key = defaultdict(list)
        for event in session.database.status_events():
            events_by_key[(event.person_id, event.meal)].append(event)
        return {
            (person.person_id, meal): resolve_status_history(
                person.person_id,
                meal,
                tuple(events_by_key[(person.person_id, meal)]),
            )
            for person in roster.persons
            for meal in Meal
        }

    @staticmethod
    def _revision_display_code(session: DatabaseSession, roster) -> str:
        metadata = session.database.revision(roster.revision_id).metadata
        return format_revision_timestamp(metadata.recorded_at)

    def _access_dialog(self, parent: QWidget | None):
        database_path = self._settings.database_path
        if database_path is None:
            return self._create_dialog_type(settings=self._settings, parent=parent)
        try:
            database_path.stat()
        except FileNotFoundError:
            # A deliberately deleted DB can be recreated, but an unavailable
            # configured share/parent must not silently become first setup.
            database_path.parent.stat()
            return self._create_dialog_type(settings=self._settings, parent=parent)
        return self._login_dialog_type(
            database_path,
            backup_directory=self._settings.backup_directory,
            parent=parent,
        )
