"""Startup coordinator that gates the roster window behind a real first import."""

from __future__ import annotations

from .user_messages import user_error

from .dialog_workflow import DialogRequest, dialog_workflow, invoke, launch

from collections import defaultdict
from dataclasses import dataclass, replace
from enum import StrEnum

from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

from src.domain.delivery_routing import (
    DRIVER_KIND, DRIVER_ORDER_KIND, ROUTING_EVENT_KIND, routing_state_from_snapshot,
)
from src.domain.roster import Meal
from src.domain.status import resolve_status_history
from src.domain.pause_conversion import PAUSE_CONVERSION_KIND, pause_conversion_from_snapshot
from src.domain.revisions import ChangeOperation, format_revision_timestamp
from src.domain.projections import PERSON_KIND
from src.domain.school_calendar import SCHOOL_LUNCH_SCHEDULE_KIND, school_lunch_calendar_from_snapshot
from src.domain.service_adjustments import SERVICE_ADJUSTMENT_SCHEDULE_KIND, service_adjustment_schedule_from_snapshot
from src.domain.household_shared import HOUSEHOLD_SHARED_KIND
from src.storage.database_session import DatabaseSession
from src.importing.profile_history import IMPORT_PROFILE_KIND, import_profiles_from_snapshot

from .custom_fields import CustomFieldController
from .complaints import ComplaintController
from .database_export import DatabaseExportController
from .current_sheet_export import CurrentSheetExportController
from .person_history import create_person_history_dialog
from .inspector_history import InspectorMealHistory
from .meal_history_cancel import MealHistoryCancelController
from .revision_history import GlobalRevisionHistoryDialog
from .complaints_history import ComplaintsHistoryDialog
from .database_access import BackupDirectoryDialog, CreateDatabaseDialog, DatabaseLocationDialog, DatabasePasswordChangeDialog, DesktopStorageSettings, LoginDialog
from .delivery_routing import DeliveryRoutingController
from .document_completion import (
    DocumentCompletionDialog,
    GeneratedDocumentResult,
)
from .document_generation import DocumentGenerationController
from .document_hub import DocumentAction, DocumentHubDialog, DocumentSetupRequest
from .document_meal import DocumentMealDialog, load_daily_report_setup, save_daily_report_choices
from .delivery_substitute import DeliverySubstituteDialog
from src.services.sqlite_delivery_substitute_service import prepare_delivery_substitute_data
from src.services.sqlite_delivery_report_service import delivery_forms_from_snapshot
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
from .roster_cell_edit import RosterCellEditController, is_roster_routing_cell
from .roster_row_edit import RosterRowEditController
from .provider_settings import ProviderSettingsController
from .column_input_rules import ColumnInputRuleController
from src.domain.column_input_rules import RULES_KIND, load_rules
from .monthly_statistics import MonthlyStatisticsDialog
from src.services.monthly_meal_statistics import build_monthly_meal_statistics
from .monthly_meal_history import MonthlyMealHistoryDialog
from .ledger_reader import load_revision_chain
from src.services.monthly_meal_history import build_monthly_meal_history
from .delivery_defaults import DeliveryDefaultsController
from .initial_roster_date import InitialRosterDateController
from .missing_household_repair import MissingHouseholdRepairController
from .roster_window import RosterWindow
from .sequence_edit import SequenceEditController, SequenceSaveStatus
from .sequence_settings import SequenceModeController
from .household_shared_columns import HouseholdSharedColumnController
from .household_shared_exceptions import HouseholdSharedExceptionsController
from .school_calendar_edit import (
    SchoolCalendarEditController,
    SchoolCalendarEditStatus,
)
from .school_calendar_import import SchoolCalendarImportController
from .service_adjustment import ServiceAdjustmentController
from .settings_hub import SettingsAction, SettingsHubDialog
from .unit_price_edit import UnitPriceChangeController
from .startup_loading import run_progress_task, run_startup_task


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


@dataclass(frozen=True, slots=True)
class _StartupPreparation:
    roster: object
    histories: dict
    revision_display_code: str
    imported_profile_fields: frozenset[str]
    snapshot: object


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
        current_sheet_export_controller_type=CurrentSheetExportController,
        provider_settings_controller_type=ProviderSettingsController,
        delivery_defaults_controller_type=DeliveryDefaultsController,
        initial_roster_date_controller_type=InitialRosterDateController,
        missing_household_repair_controller_type=MissingHouseholdRepairController,
        sequence_mode_controller_type=SequenceModeController,
        household_shared_column_controller_type=HouseholdSharedColumnController,
        household_shared_exceptions_controller_type=HouseholdSharedExceptionsController,
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
        delivery_substitute_dialog_type=DeliverySubstituteDialog,
        document_output_dialog_type=DocumentOutputDialog,
        document_completion_dialog_type=DocumentCompletionDialog,
        settings_hub_dialog_type=SettingsHubDialog,
        database_location_dialog_type=DatabaseLocationDialog,
        backup_directory_dialog_type=BackupDirectoryDialog,
        database_password_dialog_type=DatabasePasswordChangeDialog,
        revision_history_dialog_type=GlobalRevisionHistoryDialog,
        complaint_history_dialog_type=ComplaintsHistoryDialog,
        document_directory_dialog_type=DefaultDocumentDirectoryDialog,
        roster_window_type=RosterWindow,
        message_box=QMessageBox,
        startup_task_runner=run_startup_task,
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
        self._current_sheet_export_controller_type = current_sheet_export_controller_type
        self._provider_settings_controller_type = provider_settings_controller_type
        self._delivery_defaults_controller_type = delivery_defaults_controller_type
        self._initial_roster_date_controller_type = initial_roster_date_controller_type
        self._missing_household_repair_controller_type = missing_household_repair_controller_type
        self._sequence_mode_controller_type = sequence_mode_controller_type
        self._household_shared_column_controller_type = household_shared_column_controller_type
        self._household_shared_exceptions_controller_type = household_shared_exceptions_controller_type
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
        self._delivery_substitute_dialog_type = delivery_substitute_dialog_type
        self._document_output_dialog_type = document_output_dialog_type
        self._document_completion_dialog_type = document_completion_dialog_type
        self._settings_hub_dialog_type = settings_hub_dialog_type
        self._database_location_dialog_type = database_location_dialog_type
        self._backup_directory_dialog_type = backup_directory_dialog_type
        self._database_password_dialog_type = database_password_dialog_type
        self._revision_history_dialog_type = revision_history_dialog_type
        self._complaint_history_dialog_type = complaint_history_dialog_type
        self._document_directory_dialog_type = document_directory_dialog_type
        self._roster_window_type = roster_window_type
        self._message_box = message_box
        self._startup_task_runner = startup_task_runner

    def start(self, parent: QWidget | None = None) -> DesktopStartupOutcome:
        access_dialog = None
        outcome = None
        try:
            access_dialog = self._access_dialog(parent)
            outcome = self._start_with_dialog(access_dialog, parent)
            return outcome
        except Exception as error:
            self._message_box.critical(parent, "운영 데이터를 열지 못했습니다", user_error(error))
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
        if initial_backup is None or not initial_backup.attempted:
            initial_backup = self._startup_task_runner(
                session.retry_backup_on_open,
                message="자동 백업의 누락된 내용을 확인하고 다시 저장하는 중입니다.",
                parent=parent,
            )
        if initial_backup.warning_required:
            detail = "운영 데이터는 저장되어 있습니다. 재시작 후에도 자동 백업에 실패했습니다. 백업 폴더 연결과 쓰기 권한을 확인해주세요.\n\n" + user_error(initial_backup.error)
            self._message_box.warning(parent, "재시작 후 자동 백업 재시도 실패", detail)

        preparation = self._startup_task_runner(
            lambda: self._prepare_startup(session),
            message="명단과 변경 이력을 읽고 운영 화면을 준비하는 중입니다.",
            parent=parent,
        )
        if preparation is None:
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
            if roster is not None:
                preparation = self._startup_task_runner(
                    lambda: self._prepare_startup(session, roster),
                    message="가져온 명단과 변경 이력을 읽고 운영 화면을 준비하는 중입니다.",
                    parent=parent,
                )
        else:
            roster = preparation.roster
        if roster is None or not roster.persons:
            session.close()
            self._message_box.critical(
                parent,
                "명단이 아직 없습니다",
                "기존 Excel 명단 불러오기를 완료해야 운영 화면을 열 수 있습니다.",
            )
            return DesktopStartupOutcome(StartupStatus.ONBOARDING_PENDING)

        histories = preparation.histories
        window = self._roster_window_type(
            roster,
            histories,
            revision_display_code=preparation.revision_display_code,
            view_settings_scope=str(session.credentials.database_id),
            imported_profile_fields=preparation.imported_profile_fields,
        )
        path_setter = getattr(window, "set_operating_database", None)
        if callable(path_setter):
            path_setter(session.path)
        self._refresh_school_calendar(session, window, roster, snapshot=preparation.snapshot)
        self._refresh_service_adjustment_schedule(
            session, window, roster, snapshot=preparation.snapshot,
        )
        self._refresh_household_shared_columns(session, window, roster, snapshot=preparation.snapshot)
        self._refresh_column_input_rules(window, preparation.snapshot)
        routing_setter = getattr(window, "set_routing_state", None)
        if callable(routing_setter):
            routing_setter(routing_state_from_snapshot(preparation.snapshot))
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
                lambda person: meal_history.rows_for(window.roster, person.person_id),
                history_presence_provider=lambda person_id: meal_history.has_visible_history(
                    window.roster, person_id
                ),
            )
        for signal_name, handler in (
            ("complaint_requested", lambda person_id, meal: launch(self._run_complaint, session, window, person_id, meal)),
            ("database_export_requested", lambda: launch(self._run_database_export, session, window)),
            ("current_sheet_export_requested", lambda snapshot: launch(self._run_current_sheet_export, session, window, snapshot)),
            ("driver_households_export_requested", lambda snapshot, dialog: launch(self._run_current_sheet_export, session, window, snapshot, parent=dialog)),
            ("meal_history_cancel_requested", lambda target, expected_revision_id: launch(self._run_meal_history_cancel, session, window, target, expected_revision_id)),
            ("revision_history_requested", lambda: launch(self._run_revision_history, session, window)),
            ("complaint_history_requested", lambda: launch(self._run_complaint_history, session, window)),
            ("driver_replacement_requested", lambda: launch(self._run_driver_replacement, session, window)),
            ("drivers_requested", lambda: launch(self._run_driver_registry, session, window)),
            ("driver_assignment_requested", lambda person_id, meal: launch(self._run_driver_assignment, session, window, person_id, meal)),
            ("batch_driver_assignment_requested", lambda targets: launch(self._run_batch_driver_assignment, session, window, targets)),
            ("sequence_edit_requested", lambda person_id: launch(self._run_sequence_edit, session, window, person_id)),
            ("household_sequence_save_requested", lambda targets, value, dialog: self._save_household_sequence(session, window, targets, value, dialog)),
            ("household_order_save_requested", lambda values, dialog: self._save_household_order(session, window, values, dialog)),
            ("person_add_requested", lambda selected_meal: launch(self._run_roster_row_add, session, window, selected_meal)),
            ("person_delete_requested", lambda person_id: launch(self._run_roster_row_delete, session, window, person_id)),
            ("relocation_requested", lambda person_id: launch(self._run_relocation, session, window, person_id)),
            ("cell_edit_requested", lambda person_id, column_key, text: launch(self._run_roster_cell_edit, session, window, person_id, column_key, text)),
            ("column_input_rule_requested", lambda column_key: launch(self._run_column_input_rule, session, window, column_key)),
            ("monthly_statistics_requested", lambda: launch(self._run_monthly_statistics, session, window)),
            ("monthly_overview_requested", lambda: launch(self._run_monthly_overview, session, window)),
            ("monthly_meal_history_requested", lambda: launch(self._run_monthly_meal_history, session, window)),
            ("household_shared_column_requested", lambda column_key: launch(self._run_household_shared_column, session, window, column_key)),
            ("household_shared_exception_requested", lambda person_id, column_key, enabled: launch(self._run_household_shared_exception, session, window, person_id, column_key, enabled)),
        ):
            signal = getattr(window, signal_name, None)
            if signal is not None and hasattr(signal, "connect"):
                signal.connect(handler)
        import_signal = getattr(window, "import_review_requested", None)
        if import_signal is not None and hasattr(import_signal, "connect"):
            import_signal.connect(
                lambda: launch(self._run_followup_import, session, window)
            )
        person_edit_signal = getattr(window, "person_edit_requested", None)
        if person_edit_signal is not None and hasattr(person_edit_signal, "connect"):
            person_edit_signal.connect(
                lambda person_id: launch(self._run_person_edit,
                    session,
                    window,
                    person_id,
                )
            )
        household_delivery_signal = getattr(window, "delivery_settings_requested", None)
        if household_delivery_signal is not None and hasattr(household_delivery_signal, "connect"):
            household_delivery_signal.connect(
                lambda person_id: launch(self._run_person_edit, session, window, person_id, delivery_only=True)
            )
        status_signal = getattr(window, "status_action_requested", None)
        if status_signal is not None and hasattr(status_signal, "connect"):
            status_signal.connect(
                lambda person_id, meal, status: launch(self._run_meal_status_change,
                    session,
                    window,
                    person_id,
                    meal,
                    status,
                )
            )
        batch_signal = getattr(window, "batch_status_action_requested", None)
        if batch_signal is not None and hasattr(batch_signal, "connect"):
            batch_signal.connect(lambda targets, status: launch(self._run_batch_meal_status_change, session, window, targets, status))
        unit_price_signal = getattr(window, "unit_price_requested", None)
        if unit_price_signal is not None and hasattr(unit_price_signal, "connect"):
            unit_price_signal.connect(
                lambda: launch(self._run_unit_price_change, session, window)
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
                lambda: launch(self._run_service_adjustment_change, session, window)
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
                lambda: launch(self._run_school_calendar, session, window)
            )
        custom_fields_signal = getattr(window, "custom_fields_requested", None)
        if custom_fields_signal is not None and hasattr(custom_fields_signal, "connect"):
            custom_fields_signal.connect(lambda: launch(self._run_custom_fields, session, window))
        settings_signal = getattr(window, "settings_requested", None)
        if settings_signal is not None and hasattr(settings_signal, "connect"):
            settings_signal.connect(
                lambda: launch(self._run_settings_hub, session, window)
            )
        documents_signal = getattr(window, "documents_requested", None)
        if documents_signal is not None and hasattr(documents_signal, "connect"):
            documents_signal.connect(
                lambda: launch(self._run_document_hub, session, window)
            )
        generated_signal = getattr(window, "document_generated", None)
        if generated_signal is not None and hasattr(generated_signal, "connect"):
            generated_signal.connect(
                lambda result: launch(self._show_document_completion, result, window)
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
                lambda selection: launch(self._run_document_generation,
                    session,
                    window,
                    selection,
                )
            )
        if callable(getattr(window, "export_view_layout", None)):
            from .user_view_layout import UserViewLayoutController
            window._user_view_layout_controller = UserViewLayoutController(session, window)
            window._user_view_layout_controller.publish()
        return DesktopStartupOutcome(StartupStatus.READY, session, window)

    @dialog_workflow
    def _run_revision_history(self, session: DatabaseSession, window) -> None:
        try:
            dialog = self._revision_history_dialog_type(session.database, window)
            configure = getattr(dialog, "configure_actions", None)
            if callable(configure):
                def restart():
                    dialog.close()
                    window.close()
                configure(session, self._settings, restart)
        except Exception:  # noqa: BLE001 - keep history payloads out of error UI
            self._message_box.critical(
                window,
                "전체 변경 이력을 열지 못했습니다",
                "변경 이력을 읽지 못했습니다. 창을 다시 열어주세요.",
            )
            return
        yield DialogRequest(dialog)

    @dialog_workflow
    def _run_complaint_history(self, session: DatabaseSession, window) -> None:
        try:
            dialog = self._complaint_history_dialog_type(session.database, window)
        except Exception:  # noqa: BLE001 - keep complaint content out of error UI
            self._message_box.critical(
                window,
                "전체 민원을 열지 못했습니다",
                "민원을 읽지 못했습니다. 창을 다시 열어주세요.",
            )
            return
        yield DialogRequest(dialog)

    def _apply_routing_outcome(self, session, window, outcome) -> None:
        if outcome.roster is not None:
            displayed = getattr(window, "roster", None)
            already_displayed = displayed is not None and displayed.revision_id == outcome.roster.revision_id
            if already_displayed or self._replace_roster_if_changed(session, window, outcome.roster):
                window.set_routing_state(outcome.routing_state)

    @dialog_workflow
    def _run_driver_registry(self, session, window) -> None:
        controller = self._routing_controller_type(session)
        options = {}
        if isinstance(controller, DeliveryRoutingController):
            options["on_saved"] = lambda outcome: self._apply_routing_outcome(session, window, outcome)
            options["service_day"] = window.table_model.service_day
        outcome = yield from invoke(controller.run_drivers, parent=window, **options)
        self._apply_routing_outcome(session, window, outcome)

    @dialog_workflow
    def _run_driver_replacement(self, session, window) -> None:
        outcome = (yield from invoke(self._routing_controller_type(session).run_replacement,
            window.table_model.service_day, parent=window
        ))
        self._apply_routing_outcome(session, window, outcome)

    @dialog_workflow
    def _run_driver_assignment(self, session, window, person_id, meal) -> None:
        outcome = (yield from invoke(self._routing_controller_type(session).run_assignment,
            person_id, meal, window.table_model.service_day, parent=window
        ))
        self._apply_routing_outcome(session, window, outcome)

    @dialog_workflow
    def _run_batch_driver_assignment(self, session, window, targets) -> None:
        outcome = (yield from invoke(self._routing_controller_type(session).run_batch_assignment,
            targets, window.table_model.service_day, parent=window
        ))
        self._apply_routing_outcome(session, window, outcome)

    @dialog_workflow
    def _run_sequence_edit(self, session, window, person_id) -> None:
        roster = (yield from invoke(SequenceEditController(session).run, person_id, parent=window))
        if roster is not None:
            self._replace_roster_if_changed(session, window, roster)

    def _save_household_sequence(self, session, window, targets, value, dialog) -> None:
        result = SequenceEditController(session).save_value(targets, value, parent=dialog)
        if result.status is SequenceSaveStatus.SAVED:
            if not self._replace_roster_if_changed(session, window, result.roster):
                dialog.show_sequence_result(result)
                return
            refresh = getattr(dialog, "refresh_rows", None)
            if callable(refresh):
                refresh()
            else:
                if hasattr(dialog, "include_hidden"):
                    rows = window.driver_household_rows(include_hidden=dialog.include_hidden)
                else:
                    rows = window.driver_household_rows()
                dialog.replace_rows(rows)
        dialog.show_sequence_result(result)

    def _save_household_order(self, session, window, values, dialog) -> None:
        result = SequenceEditController(session).save_order(
            values, expected_revision_id=dialog.source_revision_id, parent=dialog,
        )
        if result.status is SequenceSaveStatus.SAVED:
            if self._replace_roster_if_changed(session, window, result.roster):
                dialog.source_revision_id = result.roster.revision_id
                dialog.refresh_rows()
        dialog.show_sequence_result(result)

    @dialog_workflow
    def _run_complaint(self, session, window, person_id, meal) -> None:
        outcome = (yield from invoke(self._complaint_controller_type(session).run,
            person_id, meal, window, household_day=window.table_model.service_day
        ))
        if outcome.completed and outcome.roster is not None:
            self._replace_roster_if_changed(session, window, outcome.roster)

    @dialog_workflow
    def _run_database_export(self, session, window) -> None:
        password = (yield from invoke(self._export_password_controller_type(session).ensure_configured, window))
        if not password.completed:
            return
        if password.roster is not None:
            self._replace_roster_if_changed(session, window, password.roster)
        (yield from invoke(self._database_export_controller_type(session).run,
            window.table_model.service_day, window,
            directory=self._settings.document_output_directory,
        ))

    @dialog_workflow
    def _run_current_sheet_export(self, session, window, snapshot, *, parent=None) -> None:
        export_parent = window if parent is None else parent
        password = (yield from invoke(self._export_password_controller_type(session).ensure_configured, export_parent))
        if not password.completed or password.setting is None:
            return
        if password.roster is not None:
            self._replace_roster_if_changed(session, window, password.roster)
        (yield from invoke(self._current_sheet_export_controller_type().run,
            snapshot,
            password.setting.password,
            export_parent,
            directory=self._settings.document_output_directory,
        ))

    def _create_person_history(self, session, person, meal, parent):
        try:
            dialog = create_person_history_dialog(session, person, meal, parent)
            signal = getattr(dialog, "meal_history_cancel_requested", None)
            if signal is not None:
                signal.connect(lambda target: launch(
                    self._run_meal_history_cancel, session, parent, target,
                    dialog.expected_revision_id, source_dialog=dialog,
                ))
            return dialog
        except Exception as error:
            self._message_box.critical(parent, "이력을 읽지 못했습니다", user_error(error))
            return None

    @dialog_workflow
    def _run_meal_history_cancel(self, session, window, target, expected_revision_id, *, source_dialog=None):
        roster = yield from invoke(
            MealHistoryCancelController(session).run,
            target, expected_revision_id,
            parent=source_dialog if source_dialog is not None else window,
        )
        if roster is not None:
            self._replace_roster_if_changed(session, window, roster)
            if source_dialog is not None:
                try:
                    source_dialog.reload_meal_history(session)
                except Exception as error:
                    self._message_box.warning(
                        source_dialog, "취소는 저장됐지만 이력을 새로고침하지 못했습니다",
                        "취소 내용은 저장되었습니다. 이력 창을 닫고 다시 열어주세요.\n\n" + user_error(error),
                    )

    @dialog_workflow
    def _run_relocation(self, session, window, person_id) -> None:
        outcome = (yield from invoke(self._routing_controller_type(session).run_relocation,
            person_id, window.table_model.service_day, parent=window
        ))
        self._apply_routing_outcome(session, window, outcome)

    @dialog_workflow
    def _run_custom_fields(self, session: DatabaseSession, window) -> None:
        controller = self._custom_field_controller_type(session)
        options = {}
        if isinstance(controller, CustomFieldController):
            options["on_saved"] = lambda roster: self._replace_roster_if_changed(session, window, roster)
        outcome = yield from invoke(controller.run, parent=window, **options)
        if outcome.roster is not None:
            self._replace_roster_if_changed(session, window, outcome.roster)

    @dialog_workflow
    def _run_monthly_overview(self, session, window):
        from .workspace_tools import MonthlyOverviewDialog
        dialog = None

        def load(year, month, history=False):
            def read():
                current = session.database.current_roster()
                if current is None:
                    raise ValueError("현재 명단이 없습니다.")
                snapshot = session.database.snapshot(current.revision_id)
                if history:
                    return build_monthly_meal_history(snapshot, load_revision_chain(session.database, current.revision_id), year, month)
                return build_monthly_meal_statistics(snapshot, year, month)
            return run_progress_task(read, message="선택한 달의 운영 자료를 조회하는 중입니다.", parent=dialog or window)

        dialog = MonthlyOverviewDialog(window.table_model.service_day, load,
                            lambda year, month: load(year, month, True), parent=window)
        yield DialogRequest(dialog)

    @dialog_workflow
    def _run_monthly_statistics(self, session, window):
        def load(year, month):
            current = session.database.current_roster()
            if current is None:
                raise ValueError("현재 명단이 없습니다.")
            snapshot = session.database.snapshot(current.revision_id)
            return run_progress_task(
                lambda: build_monthly_meal_statistics(snapshot, year, month),
                title="월별 식수를 계산하는 중입니다",
                message="실제 배송일별 식수를 집계하는 중입니다.",
                parent=window if isinstance(window, QWidget) else None,
            )

        dialog = MonthlyStatisticsDialog(window.table_model.service_day, load, parent=window)
        yield DialogRequest(dialog)

    @dialog_workflow
    def _run_monthly_meal_history(self, session, window):
        dialog = None
        def load(year, month):
            def read():
                current = session.database.current_roster()
                if current is None:
                    raise ValueError("현재 명단이 없습니다.")
                snapshot = session.database.snapshot(current.revision_id)
                revisions = load_revision_chain(session.database, current.revision_id)
                return build_monthly_meal_history(snapshot, revisions, year, month)

            return run_progress_task(
                read, title="월별 식종 변경 이력을 읽는 중입니다",
                message="선택한 달의 적용 기간과 변경 이력을 확인하는 중입니다.",
                parent=dialog or (window if isinstance(window, QWidget) else None),
            )

        dialog = MonthlyMealHistoryDialog(window.table_model.service_day, load, parent=window)
        yield DialogRequest(dialog)

    @dialog_workflow
    def _run_column_input_rule(self, session, window, column_key):
        column = next((item for item in window.table_model.columns if item.key == column_key), None)
        if column is None:
            return
        changed = (yield from invoke(ColumnInputRuleController(session).run, column_key, column.label, parent=window))
        if changed:
            self._replace_roster(session, window, session.database.current_roster())

    @staticmethod
    def _refresh_column_input_rules(window, snapshot) -> None:
        delegate = getattr(window, "_cell_edit_delegate", None)
        setter = getattr(delegate, "set_input_rules", None)
        if callable(setter):
            setter(load_rules(snapshot))

    @dialog_workflow
    def _run_household_shared_column(self, session, window, column_key) -> None:
        column = next((item for item in window.table_model.columns if item.key == column_key), None)
        label = column.label if column is not None else window._column_aliases.get(column_key, "배송 설정")
        outcome = (yield from invoke(self._household_shared_column_controller_type(session).run,
            column_key, label, window.table_model.service_day, parent=window,
        ))
        if outcome.roster is not None:
            self._replace_roster_if_changed(session, window, outcome.roster)

    def _run_household_shared_exception(
        self, session, window, person_id, column_key, enabled,
    ) -> None:
        outcome = self._household_shared_exceptions_controller_type(
            session
        ).set_exception(
            person_id, column_key, enabled, window.table_model.service_day,
            parent=window,
        )
        if outcome.roster is not None:
            self._replace_roster_if_changed(session, window, outcome.roster)
            return

        def load_state():
            current = session.database.current_roster()
            return current, None if current is None else session.database.snapshot(current.revision_id)

        try:
            current, snapshot = run_progress_task(
                load_state, parent=window,
                title="공용 예외 설정을 불러오는 중입니다",
                message="저장된 공용 예외 설정을 확인하는 중입니다.",
            )
        except Exception as error:
            self._message_box.critical(window, "공용 예외 설정을 읽지 못했습니다", user_error(error))
            return
        if current is not None:
            self._refresh_household_shared_columns(session, window, current, snapshot=snapshot)

    @dialog_workflow
    def _run_document_hub(self, session: DatabaseSession, window) -> None:
        from .document_navigation import DOCUMENT_BACK
        service_day = window.table_model.service_day
        selected_meal = window.table_model.meal
        hub = self._document_hub_dialog_type(parent=window)
        action = None
        period = meal = substitute = output = None
        meal_period = substitute_input = output_document = None
        stage = "hub"
        while True:
            if stage == "hub":
                if (yield DialogRequest(hub)) != QDialog.DialogCode.Accepted:
                    return
                if hub.selected_action == action and period is not None:
                    stage = "period"
                    continue
                action = hub.selected_action
                meal = substitute = output = None
                meal_period = substitute_input = output_document = None
                if action not in (
                    DocumentAction.INVOICE,
                    DocumentAction.CONFIRMATION,
                    DocumentAction.DELIVERY_REPORT,
                    DocumentAction.DELIVERY_SUBSTITUTE,
                ):
                    self._message_box.information(
                        window,
                        f"{action.label} 연결 준비 중",
                        "현재 단계에서는 청구서, 확인서와 일일식수를 만들 수 있습니다.",
                    )
                    return
                if not (yield from invoke(self._ensure_document_prerequisites, session, window, action)):
                    return
                try:
                    summaries = run_progress_task(
                        session.database.revision_summaries,
                        title="문서 원본을 불러오는 중입니다",
                        message="문서에 사용할 변경번호 목록을 읽는 중입니다.", parent=window,
                    )
                except Exception as error:
                    self._message_box.critical(window, "문서 원본을 읽지 못했습니다", user_error(error))
                    return
                if not summaries:
                    self._message_box.information(
                        window, "문서 원본이 없습니다", "문서에 사용할 변경 이력이 없습니다.",
                    )
                    return
                setup = DocumentSetupRequest(
                    action,
                    max(summaries, key=lambda summary: summary.sequence),
                    service_day,
                    selected_meal,
                )
                period = self._document_period_dialog_type(setup, parent=window)
                source_options_setter = getattr(period, "set_source_options_available", None)
                if callable(source_options_setter):
                    source_options_setter(len(summaries) > 1)
                stage = "period"
                continue
            if stage == "period":
                period_result = yield DialogRequest(period)
                if period_result == DOCUMENT_BACK:
                    stage = "hub"
                    continue
                if period_result == DocumentPeriodDialog.REVISION_SELECTION_REQUESTED:
                    source = self._document_source_dialog_type(summaries, parent=window)
                    if isinstance(source, DocumentSourceDialog) and hasattr(session.database, "search_revision_ids"):
                        source._search_provider = lambda text: run_progress_task(
                            lambda: session.database.search_revision_ids(text),
                            title="변경 이력을 검색하는 중입니다",
                            message="변경번호와 변경 내용을 확인합니다.", parent=source)
                    selection_setter = getattr(source, "set_selected_summary", None)
                    if callable(selection_setter):
                        selection_setter(period.request.source)
                    archive_opener = getattr(source, "show_archive", None)
                    if callable(archive_opener):
                        archive_opener()
                    if (yield DialogRequest(source)) == QDialog.DialogCode.Accepted:
                        period.set_source(source.selected_summary)
                    continue
                if period_result != QDialog.DialogCode.Accepted:
                    return
                selected_period = period.selected_period()
                if meal is None or selected_period != meal_period:
                    meal = self._document_meal_dialog_type(
                        selected_period,
                        parent=window,
                    )
                    forms_setter = getattr(meal, "set_available_delivery_forms", None)
                    if action is DocumentAction.DELIVERY_REPORT and callable(forms_setter):
                        try:
                            forms = run_progress_task(
                                lambda: delivery_forms_from_snapshot(
                                    session.database.snapshot(selected_period.source.revision_id)
                                ),
                                title="배송 형태를 불러오는 중입니다",
                                message="선택한 변경번호의 배송 형태를 확인하는 중입니다.",
                                parent=window,
                            )
                            forms_setter(forms)
                            columns_setter = getattr(meal, "set_report_columns", None)
                            if callable(columns_setter):
                                from src.services.delivery_report_remarks import report_remark_columns
                                from src.domain.projections import roster_from_historical_snapshot
                                source = session.database.snapshot(selected_period.source.revision_id)
                                columns = report_remark_columns(roster_from_historical_snapshot(source))
                                labels = {column.key: column.label for column in getattr(getattr(window, "table_model", None), "columns", ())}
                                columns_setter(tuple((key, labels.get(key, label)) for key, label in columns))
                        except Exception as error:
                            self._message_box.critical(
                                window, "배송 형태를 읽지 못했습니다", user_error(error),
                            )
                            return
                    if action is DocumentAction.DELIVERY_REPORT:
                        preferences_setter = getattr(meal, "set_report_choices", None)
                        if callable(preferences_setter):
                            try:
                                choices, profile = run_progress_task(lambda: load_daily_report_setup(session),
                                    title="일일식수 선택을 불러오는 중입니다", message="운영 DB에 저장된 식종과 배송 형태를 확인합니다.", parent=window)
                                preferences_setter(choices, profile=profile)
                            except Exception as error:
                                self._message_box.critical(window, "일일식수 선택을 읽지 못했습니다", user_error(error))
                                return
                    meal_period = selected_period
                stage = "meal"
                continue
            if stage == "meal":
                meal_result = yield DialogRequest(meal)
                if meal_result == DOCUMENT_BACK:
                    stage = "period"
                    continue
                if meal_result != QDialog.DialogCode.Accepted:
                    return
                document = meal.selection()
                choices_getter = getattr(meal, "selected_report_choices", None)
                if action is DocumentAction.DELIVERY_REPORT and callable(choices_getter):
                    try:
                        roster, backup = save_daily_report_choices(session, choices_getter(), parent=window)
                        self._replace_roster_if_changed(session, window, roster)
                    except Exception as error:
                        self._message_box.critical(window, "일일식수 선택을 저장하지 못했습니다", user_error(error))
                        continue
                    if backup is not None and backup.warning_required:
                        self._message_box.warning(window, "선택은 저장됐지만 자동 백업에 실패했습니다", user_error(backup.error))
                stage = "substitute" if action is DocumentAction.DELIVERY_SUBSTITUTE else "output"
                continue
            if stage == "substitute":
                try:
                    base_document = meal.selection()
                    if substitute is None or substitute_input != base_document:
                        document = base_document
                        from .substitute_preferences import preferences, exclude_hidden_columns
                        from src.services.sqlite_delivery_substitute_service import substitute_columns_from_roster, ROSTER_COLUMN_KEYS
                        model = getattr(window, "table_model", None)
                        roster_columns = getattr(model, "columns", None)
                        def prepare_substitute():
                            source = session.database.snapshot(document.period.source.revision_id)
                            columns = (substitute_columns_from_roster(roster_columns, source_snapshot=source)
                                       if roster_columns is not None else None)
                            return prepare_delivery_substitute_data(
                                source,
                                document.period.starts_on, document.meals[0],
                                mapping_snapshot=session.database.snapshot(session.database.current_roster().revision_id),
                                columns=columns, fallbacks={},
                            )
                        prepared = run_progress_task(
                            prepare_substitute,
                            title="배송대타 대상을 불러오는 중입니다",
                            message="선택한 날짜의 실제 배송 대상과 담당자를 확인하고 있습니다.", parent=window,
                        )
                        if not prepared.rows:
                            self._message_box.information(window, "배송대타 대상이 없습니다", "선택한 날짜·식종에 실제 배송할 대상이 없습니다.")
                            return
                        substitute = self._delivery_substitute_dialog_type(document, prepared, parent=window)
                        scope_setter = getattr(substitute, "set_preferences_scope", None)
                        if callable(scope_setter):
                            scope_setter(str(session.credentials.database_id))
                        visibility_setter = getattr(substitute, "set_visible_columns", None)
                        visible_getter = getattr(window, "columns_visible_in_any_sheet", None)
                        store, key = preferences(str(session.credentials.database_id))
                        if callable(visibility_setter) and callable(visible_getter) and exclude_hidden_columns(store, key):
                            visibility_setter({ROSTER_COLUMN_KEYS.get(key, key) for key in visible_getter()})
                        substitute_input = base_document
                    substitute_result = yield DialogRequest(substitute)
                    if substitute_result == DOCUMENT_BACK:
                        stage = "meal"
                        continue
                    if substitute_result != QDialog.DialogCode.Accepted:
                        return
                    document = substitute.selection()
                except Exception as error:
                    self._message_box.critical(window, "배송대타 대상을 읽지 못했습니다", user_error(error))
                    return
                stage = "output"
                continue
            if stage == "output":
                if output is None or output_document != document:
                    output = self._document_output_dialog_type(
                        document, self._settings.document_output_directory, parent=window,
                    )
                    output_document = document
                output_result = yield DialogRequest(output)
                if output_result == DOCUMENT_BACK:
                    stage = "substitute" if action is DocumentAction.DELIVERY_SUBSTITUTE else "meal"
                    continue
                if output_result != QDialog.DialogCode.Accepted:
                    return
                selection = output.selection()
                if selection.mode is DocumentOutputMode.NEW_DEFAULT:
                    try:
                        self._settings.save_document_output_directory(selection.directory)
                    except OSError as error:
                        self._message_box.critical(window, "문서 저장 폴더를 저장하지 못했습니다", user_error(error))
                        return
                selected_signal = getattr(window, "document_output_selected", None)
                if selected_signal is not None and hasattr(selected_signal, "emit"):
                    selected_signal.emit(selection)

                return

    @dialog_workflow
    def _show_document_completion(self, result, window) -> None:
        try:
            if not isinstance(result, GeneratedDocumentResult):
                raise TypeError("문서 생성 결과가 올바르지 않습니다.")
            # The standard dialog verifies on construction, including direct
            # callers. Injected factories still receive a verified result here.
            if self._document_completion_dialog_type is not DocumentCompletionDialog:
                run_progress_task(
                    result.verify_file, title="문서 파일을 확인하는 중입니다",
                    message="완료 화면을 열기 전에 저장된 문서의 내용을 확인하는 중입니다.", parent=window,
                )
            dialog = self._document_completion_dialog_type(
                result,
                parent=window,
            )
        except (OSError, TypeError, ValueError) as error:
            self._message_box.critical(
                window,
                "문서 완료 상태를 확인하지 못했습니다",
                "파일이 없거나 생성 결과가 달라 완료 화면을 열 수 없습니다.\n\n"
                + user_error(error),
            )
            return
        (yield DialogRequest(dialog))

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

    @dialog_workflow
    def _run_settings_hub(self, session: DatabaseSession, window) -> None:
        hub = self._settings_hub_dialog_type(parent=window)
        if (yield DialogRequest(hub)) != QDialog.DialogCode.Accepted:
            return
        if hub.selected_action is SettingsAction.OUTPUT_RETENTION:
            from .output_retention import OutputRetentionDialog
            yield DialogRequest(OutputRetentionDialog(self._settings, parent=window))
            return
        if hub.selected_action is SettingsAction.USER_VIEW_LAYOUT:
            yield from invoke(window._user_view_layout_controller.run)
            return
        if hub.selected_action is SettingsAction.JUSO_KEY:
            from .old_address import JusoKeyDialog
            yield DialogRequest(JusoKeyDialog(parent=window))
            return
        if hub.selected_action is SettingsAction.OLD_ADDRESS:
            from .old_address import OldAddressController
            controller = OldAddressController(session)
            controller.on_saved = lambda roster: self._replace_roster_if_changed(session, window, roster)
            yield from invoke(controller.run, window.table_model.columns,
                              window.table_model.service_day, parent=window)
            return
        if hub.selected_action is SettingsAction.DATABASE_LOCATION:
            dialog = self._database_location_dialog_type(session, self._settings, parent=window)
            if (yield DialogRequest(dialog)) == QDialog.DialogCode.Accepted:
                window.close()
            return
        if hub.selected_action is SettingsAction.BACKUP_DIRECTORY:
            dialog = self._backup_directory_dialog_type(session, self._settings, parent=window)
            (yield DialogRequest(dialog))
            return
        if hub.selected_action is SettingsAction.DATABASE_PASSWORD:
            dialog = self._database_password_dialog_type(session, parent=window)
            if (yield DialogRequest(dialog)) != QDialog.DialogCode.Accepted:
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
                if dialog.backup_result.warning_required:
                    details.append(f"새 백업 오류: {user_error(dialog.backup_result.error)}")
                if dialog.backup_result.rotation_error is not None:
                    details.append(f"기존 백업 처리 오류: {user_error(dialog.backup_result.rotation_error)}")
                if rotation is not None:
                    details.extend(f"{path.name}: {user_error(reason)}" for path, reason in rotation.failed)
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
                    yield DialogRequest(warning)
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
        if hub.selected_action is SettingsAction.INITIAL_ROSTER_DATE:
            outcome = (yield from invoke(self._initial_roster_date_controller_type(session).run, parent=window))
            if outcome.completed and outcome.roster is not None:
                self._replace_roster_if_changed(session, window, outcome.roster)
            return
        if hub.selected_action is SettingsAction.STOPPED_HIGHLIGHT:
            yield from invoke(window.open_stopped_highlight_settings)
            return
        if hub.selected_action is SettingsAction.SUBSTITUTE_COLUMNS:
            from .substitute_preferences import SubstituteColumnVisibilityDialog
            yield DialogRequest(SubstituteColumnVisibilityDialog(str(session.credentials.database_id), parent=window))
            return
        if hub.selected_action is SettingsAction.PAUSE_CONVERSION:
            from .pause_conversion import PauseConversionController
            roster = (yield from invoke(PauseConversionController(session).run, parent=window))
            if roster is not None:
                self._replace_roster_if_changed(session, window, roster)
            return
        if hub.selected_action is SettingsAction.HOUSEHOLD_GROUPING:
            from .household_grouping import HouseholdGroupingController
            roster = (yield from invoke(HouseholdGroupingController(session).run, parent=window))
            if roster is not None:
                self._replace_roster_if_changed(session, window, roster)
            return
        if hub.selected_action is SettingsAction.MISSING_HOUSEHOLD_REPAIR:
            outcome = (yield from invoke(self._missing_household_repair_controller_type(session).run, parent=window))
            if outcome.completed and outcome.roster is not None:
                self._replace_roster_if_changed(session, window, outcome.roster)
            return
        if hub.selected_action is SettingsAction.DELIVERY_DEFAULTS:
            outcome = (yield from invoke(self._delivery_defaults_controller_type(session).run, parent=window))
            if outcome.completed and outcome.roster is not None:
                self._replace_roster_if_changed(session, window, outcome.roster)
            return
        if hub.selected_action is SettingsAction.SEQUENCE_MODE:
            outcome = (yield from invoke(self._sequence_mode_controller_type(session).run, parent=window))
            if outcome.completed and outcome.roster is not None:
                self._replace_roster_if_changed(session, window, outcome.roster)
            return
        if hub.selected_action is SettingsAction.PROVIDER:
            outcome = (yield from invoke(self._provider_settings_controller_type(session).run, parent=window))
            if outcome.completed and outcome.roster is not None:
                self._replace_roster_if_changed(session, window, outcome.roster)
            return
        if hub.selected_action is SettingsAction.EXPORT_PASSWORD:
            (yield from invoke(self._run_export_password_change, session, window))
            return
        if hub.selected_action is SettingsAction.DOCUMENT_TEMPLATE:
            (yield from invoke(self._run_document_template_change, session, window))
            return
        if hub.selected_action is SettingsAction.INVOICE_SETTINGS:
            from .invoice_settings import InvoiceSettingsController
            roster = (yield from invoke(InvoiceSettingsController(session).run, parent=window))
            if roster is not None:
                self._replace_roster_if_changed(session, window, roster)
            return
        if hub.selected_action is SettingsAction.DOCUMENT_COLUMN_MAPPING:
            (yield from invoke(self._run_document_column_mapping, session, window))
            return
        dialog = self._document_directory_dialog_type(
            self._settings.document_output_directory,
            parent=window,
        )
        if (yield DialogRequest(dialog)) != QDialog.DialogCode.Accepted:
            return
        try:
            self._settings.save_document_output_directory(dialog.selected_directory)
        except OSError as error:
            self._message_box.critical(window, "문서 저장 폴더를 저장하지 못했습니다", user_error(error))

    @dialog_workflow
    def _run_followup_import(self, session: DatabaseSession, window) -> None:
        controller = self._followup_controller_type(session)
        outcome = (yield from invoke(controller.run, window))
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

    @dialog_workflow
    def _run_person_edit(self, session: DatabaseSession, window, person_id, *, delivery_only: bool = False) -> None:
        controller = self._profile_edit_controller_type(session)
        outcome = (
            (yield from invoke(controller.run_delivery_settings, person_id, window, service_day=window.table_model.service_day))
            if delivery_only else (yield from invoke(controller.run, person_id, window))
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

    @dialog_workflow
    def _run_roster_cell_edit(
        self,
        session: DatabaseSession,
        window,
        person_id,
        column_key: str,
        text: str,
    ) -> None:
        controller = self._roster_cell_edit_controller_type(session)
        if is_roster_routing_cell(column_key):
            outcome = yield from invoke(controller.save,
                person_id,
                column_key,
                text,
                window,
                service_day=window.table_model.service_day,
            )
        else:
            outcome = yield from invoke(controller.save, person_id, column_key, text, window)
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

    @dialog_workflow
    def _run_roster_row_add(self, session: DatabaseSession, window, selected_meal) -> None:
        outcome = (yield from invoke(self._roster_row_controller_type(session).add,
            selected_meal, window, service_day=window.table_model.service_day,
        ))
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
        self._replace_roster(session, window, roster)

    @dialog_workflow
    def _run_meal_status_change(
        self,
        session: DatabaseSession,
        window,
        person_id,
        meal,
        status,
    ) -> None:
        controller = self._meal_status_controller_type(session)
        outcome = (yield from invoke(controller.run,
            person_id,
            meal,
            status,
            window.table_model.service_day,
            window,
        ))
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

    @dialog_workflow
    def _run_batch_meal_status_change(self, session, window, targets, status) -> None:
        outcome = (yield from invoke(self._meal_status_controller_type(session).run_batch,
            targets, status, window.table_model.service_day, window,
        ))
        if outcome.completed and outcome.roster is not None:
            self._replace_roster_if_changed(session, window, outcome.roster)

    @dialog_workflow
    def _run_unit_price_change(self, session: DatabaseSession, window) -> None:
        controller = self._unit_price_controller_type(session)
        outcome = (yield from invoke(controller.run, window))
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

    @dialog_workflow
    def _run_service_adjustment_change(
        self,
        session: DatabaseSession,
        window,
    ) -> None:
        controller = self._service_adjustment_controller_type(session)
        outcome = (yield from invoke(controller.run, window))
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

    @dialog_workflow
    def _run_school_calendar(
        self,
        session: DatabaseSession,
        window,
    ) -> None:
        controller = self._school_calendar_edit_controller_type(session)
        outcome = (yield from invoke(controller.run, window))
        if outcome.status is SchoolCalendarEditStatus.IMPORT_REQUESTED:
            (yield from invoke(self._run_school_calendar_import, session, window))
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

    @dialog_workflow
    def _run_school_calendar_import(
        self,
        session: DatabaseSession,
        window,
    ) -> None:
        controller = self._school_calendar_controller_type(session)
        outcome = (yield from invoke(controller.run, window))
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

    @dialog_workflow
    def _run_export_password_change(
        self,
        session: DatabaseSession,
        window,
    ) -> None:
        controller = self._export_password_controller_type(session)
        outcome = (yield from invoke(controller.run, window))
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

    @dialog_workflow
    def _run_document_template_change(
        self,
        session: DatabaseSession,
        window,
    ) -> None:
        controller = self._document_template_controller_type(session)
        outcome = (yield from invoke(controller.run, window))
        if not outcome.completed or outcome.roster is None:
            return
        self._replace_roster_if_changed(session, window, outcome.roster)

    @dialog_workflow
    def _run_document_column_mapping(self, session: DatabaseSession, window) -> None:
        controller_type = self._document_column_mapping_controller_type
        if controller_type is None:
            from .document_column_mapping import DocumentColumnMappingController
            controller_type = DocumentColumnMappingController
        outcome = (yield from invoke(controller_type(session).run, window))
        if outcome.completed and outcome.roster is not None:
            self._replace_roster_if_changed(session, window, outcome.roster)

    @dialog_workflow
    def _ensure_document_prerequisites(
        self,
        session: DatabaseSession,
        window,
        action: DocumentAction,
    ) -> bool:
        if action is DocumentAction.DELIVERY_REPORT:
            return True
        if action in (DocumentAction.INVOICE, DocumentAction.CONFIRMATION):
            template = (yield from invoke(self._document_template_controller_type(
                session
            ).ensure_configured, window))
            if not template.completed:
                return False
            if template.roster is not None:
                self._replace_roster_if_changed(session, window, template.roster)

        password = (yield from invoke(self._export_password_controller_type(
            session
        ).ensure_configured, window))
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
        return self._replace_roster(session, window, roster)

    def _replace_roster(self, session, window, roster) -> bool:
        selection_getter = getattr(window, "selected_person_cell", None)
        selected_cell = selection_getter() if callable(selection_getter) else None
        try:
            preparation = run_progress_task(
                lambda: self._prepare_startup(session, roster),
                title="명단을 새로 불러오는 중입니다",
                message="저장된 명단과 변경 이력을 읽는 중입니다.",
                parent=window if isinstance(window, QWidget) else None,
            )
        except Exception as error:
            self._message_box.critical(
                window, "저장된 명단을 새로 불러오지 못했습니다",
                "저장된 내용은 유지됩니다. 명단을 다시 열어주세요.\n\n" + user_error(error),
            )
            return False
        window.replace_roster(
            roster, preparation.histories,
            revision_display_code=preparation.revision_display_code,
        )
        self._refresh_school_calendar(session, window, roster, snapshot=preparation.snapshot)
        self._refresh_service_adjustment_schedule(
            session, window, roster, snapshot=preparation.snapshot,
        )
        self._refresh_household_shared_columns(session, window, roster, snapshot=preparation.snapshot)
        self._refresh_column_input_rules(window, preparation.snapshot)
        routing_setter = getattr(window, "set_routing_state", None)
        if callable(routing_setter):
            routing_setter(routing_state_from_snapshot(preparation.snapshot))
        setter = getattr(window, "set_imported_profile_fields", None)
        if callable(setter):
            setter(preparation.imported_profile_fields)
        selection_restorer = getattr(window, "restore_person_cell", None)
        if selected_cell is not None and callable(selection_restorer):
            selection_restorer(selected_cell)
        return True

    @staticmethod
    def _refresh_household_shared_columns(
        session, window, roster, *, snapshot=None,
    ) -> None:
        state_setter = getattr(window, "set_household_shared_state", None)
        setter = getattr(window, "set_household_shared_columns", None)
        if not callable(state_setter) and not callable(setter):
            return
        from src.domain.household_shared import (
            HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID,
            shared_columns_from_state, shared_exceptions_from_state,
        )
        if snapshot is None:
            snapshot = session.database.snapshot(roster.revision_id)
        stored = snapshot.entity(
            HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID,
        )
        columns = shared_columns_from_state(stored)
        exceptions = shared_exceptions_from_state(stored)
        if callable(state_setter):
            state_setter(columns, exceptions)
            return
        if callable(setter):
            setter(columns)

    @staticmethod
    def _imported_profile_fields(session, roster) -> frozenset[str]:
        profiles = import_profiles_from_snapshot(session.database.snapshot(roster.revision_id))
        return frozenset(field.value for stored in profiles for field in stored.profile.profile_columns)

    @staticmethod
    def _refresh_school_calendar(session, window, roster, *, snapshot=None) -> None:
        setter = getattr(window, "set_school_calendar", None)
        if callable(setter):
            if snapshot is None:
                snapshot = session.database.snapshot(roster.revision_id)
            setter(school_lunch_calendar_from_snapshot(
                snapshot
            ))

    @staticmethod
    def _refresh_service_adjustment_schedule(
        session, window, roster, *, snapshot=None,
    ) -> None:
        setter = getattr(window, "set_service_adjustment_schedule", None)
        if callable(setter):
            if snapshot is None:
                snapshot = session.database.snapshot(roster.revision_id)
            setter(service_adjustment_schedule_from_snapshot(snapshot))

    @staticmethod
    def _histories(session: DatabaseSession, roster, snapshot=None):
        if not roster.persons:
            return {}
        if snapshot is None:
            snapshot = session.database.snapshot(roster.revision_id)
        conversion = pause_conversion_from_snapshot(snapshot)
        events_by_key = defaultdict(list)
        for event in session.database.status_events():
            events_by_key[(event.person_id, event.meal)].append(event)
        return {
            (person.person_id, meal): resolve_status_history(
                person.person_id,
                meal,
                tuple(events_by_key[(person.person_id, meal)]),
                same_month_pause=conversion.same_month,
            )
            for person in roster.persons
            for meal in Meal
        }

    def _prepare_startup(self, session: DatabaseSession, roster=None):
        if roster is None:
            roster = session.database.current_roster()
        if roster is None:
            return None
        previous_session, previous = getattr(self, "_last_preparation", (None, None))
        record = getattr(session.database, "last_committed_record", None)
        if (previous_session is session and previous is not None and record is not None
                and record.metadata.revision_id == roster.revision_id
                and record.metadata.parent_revision_id == previous.roster.revision_id
                and all(change.entity_kind == PERSON_KIND
                        and change.operation is ChangeOperation.UPDATE for change in record.changes)):
            # Profile corrections do not alter schedules, input rules or meal
            # status histories. The table already refreshes only changed people.
            prepared = replace(
                previous, roster=roster,
                snapshot=replace(previous.snapshot, revision_id=roster.revision_id),
                revision_display_code=self._revision_display_code(session, roster),
            )
            self._last_preparation = (session, prepared)
            return prepared
        read_kinds = getattr(session.database, "snapshot_for_kinds", None)
        snapshot = (
            read_kinds(roster.revision_id, (
                DRIVER_KIND, DRIVER_ORDER_KIND, ROUTING_EVENT_KIND,
                SCHOOL_LUNCH_SCHEDULE_KIND, SERVICE_ADJUSTMENT_SCHEDULE_KIND,
                HOUSEHOLD_SHARED_KIND, IMPORT_PROFILE_KIND, RULES_KIND, PAUSE_CONVERSION_KIND,
            ))
            if callable(read_kinds) else session.database.snapshot(roster.revision_id)
        )
        profiles = import_profiles_from_snapshot(snapshot)
        prepared = _StartupPreparation(
            roster=roster,
            histories=self._histories(session, roster, snapshot),
            revision_display_code=self._revision_display_code(session, roster),
            imported_profile_fields=frozenset(
                field.value
                for stored in profiles
                for field in stored.profile.profile_columns
            ),
            snapshot=snapshot,
        )
        self._last_preparation = (session, prepared)
        return prepared

    @staticmethod
    def _revision_display_code(session: DatabaseSession, roster) -> str:
        read_metadata = getattr(session.database, "revision_metadata", None)
        metadata = (
            read_metadata(roster.revision_id) if callable(read_metadata)
            else session.database.revision(roster.revision_id).metadata
        )
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
