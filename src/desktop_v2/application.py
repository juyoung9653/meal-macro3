"""Startup coordinator that gates the roster window behind a real first import."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

from src.domain.roster import Meal
from src.storage import DatabaseSession

from .database_access import CreateDatabaseDialog, DesktopStorageSettings, LoginDialog
from .breakfast_delivery_policy import BreakfastDeliveryPolicyController
from .document_completion import (
    DocumentCompletionDialog,
    GeneratedDocumentResult,
)
from .document_hub import DocumentAction, DocumentHubDialog, DocumentSetupRequest
from .document_generation import DocumentGenerationController
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
from .person_edit import PersonProfileEditController
from .meal_status_edit import MealStatusChangeController
from .roster_window import RosterWindow
from .service_adjustment import ServiceAdjustmentController
from .school_calendar_import import SchoolCalendarImportController
from .school_calendar_edit import (
    SchoolCalendarEditController,
    SchoolCalendarEditStatus,
)
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
        if self.window is not None:
            self.window.close()
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
        meal_status_controller_type=MealStatusChangeController,
        service_adjustment_controller_type=ServiceAdjustmentController,
        school_calendar_controller_type=SchoolCalendarImportController,
        school_calendar_edit_controller_type=SchoolCalendarEditController,
        unit_price_controller_type=UnitPriceChangeController,
        export_password_controller_type=ExportPasswordController,
        breakfast_delivery_policy_controller_type=(
            BreakfastDeliveryPolicyController
        ),
        document_template_controller_type=DocumentTemplateController,
        document_generation_controller_type=DocumentGenerationController,
        document_hub_dialog_type=DocumentHubDialog,
        document_source_dialog_type=DocumentSourceDialog,
        document_period_dialog_type=DocumentPeriodDialog,
        document_meal_dialog_type=DocumentMealDialog,
        document_output_dialog_type=DocumentOutputDialog,
        document_completion_dialog_type=DocumentCompletionDialog,
        settings_hub_dialog_type=SettingsHubDialog,
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
        self._breakfast_delivery_policy_controller_type = (
            breakfast_delivery_policy_controller_type
        )
        self._document_template_controller_type = document_template_controller_type
        self._document_generation_controller_type = document_generation_controller_type
        self._document_hub_dialog_type = document_hub_dialog_type
        self._document_source_dialog_type = document_source_dialog_type
        self._document_period_dialog_type = document_period_dialog_type
        self._document_meal_dialog_type = document_meal_dialog_type
        self._document_output_dialog_type = document_output_dialog_type
        self._document_completion_dialog_type = document_completion_dialog_type
        self._settings_hub_dialog_type = settings_hub_dialog_type
        self._document_directory_dialog_type = document_directory_dialog_type
        self._roster_window_type = roster_window_type
        self._message_box = message_box

    def start(self, parent: QWidget | None = None) -> DesktopStartupOutcome:
        access_dialog = self._access_dialog(parent)
        if access_dialog.exec() != QDialog.DialogCode.Accepted:
            return DesktopStartupOutcome(StartupStatus.CANCELLED)
        session = access_dialog.session
        if session is None:
            self._message_box.critical(
                parent,
                "운영 DB를 열지 못했습니다",
                "열린 DB 세션이 없습니다. 프로그램을 다시 시작해주세요.",
            )
            return DesktopStartupOutcome(StartupStatus.FAILED)
        initial_backup = getattr(access_dialog, "backup_result", None)
        if initial_backup is not None and initial_backup.error is not None:
            self._message_box.warning(
                parent,
                "운영 DB는 만들었지만 첫 백업에 실패했습니다",
                "DB는 정상 생성되었습니다. 백업 폴더 연결을 확인해주세요.\n\n"
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
        )
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
            self._settings.save_document_output_directory(selection.directory)
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
        if hub.selected_action is SettingsAction.EXPORT_PASSWORD:
            self._run_export_password_change(session, window)
            return
        if hub.selected_action is SettingsAction.DOCUMENT_TEMPLATE:
            self._run_document_template_change(session, window)
            return
        if hub.selected_action is SettingsAction.BREAKFAST_DELIVERY_POLICY:
            self._run_breakfast_delivery_policy_change(session, window)
            return
        dialog = self._document_directory_dialog_type(
            self._settings.document_output_directory,
            parent=window,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._settings.save_document_output_directory(
            dialog.selected_directory
        )

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
        window.replace_roster(
            roster,
            self._histories(session, roster),
            revision_display_code=self._revision_display_code(session, roster),
        )

    def _run_person_edit(self, session: DatabaseSession, window, person_id) -> None:
        controller = self._profile_edit_controller_type(session)
        outcome = controller.run(person_id, window)
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
        window.replace_roster(
            roster,
            self._histories(session, roster),
            revision_display_code=self._revision_display_code(session, roster),
        )

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
        window.replace_roster(
            roster,
            self._histories(session, roster),
            revision_display_code=self._revision_display_code(session, roster),
        )

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
        window.replace_roster(
            roster,
            self._histories(session, roster),
            revision_display_code=self._revision_display_code(session, roster),
        )

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
        window.replace_roster(
            roster,
            self._histories(session, roster),
            revision_display_code=self._revision_display_code(session, roster),
        )

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
        window.replace_roster(
            roster,
            self._histories(session, roster),
            revision_display_code=self._revision_display_code(session, roster),
        )

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
        window.replace_roster(
            roster,
            self._histories(session, roster),
            revision_display_code=self._revision_display_code(session, roster),
        )

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

    def _run_breakfast_delivery_policy_change(
        self,
        session: DatabaseSession,
        window,
    ) -> None:
        controller = self._breakfast_delivery_policy_controller_type(session)
        outcome = controller.run(window.table_model.service_day, window)
        if not outcome.completed or outcome.roster is None:
            return
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
        window.replace_roster(
            roster,
            self._histories(session, roster),
            revision_display_code=self._revision_display_code(session, roster),
        )
        return True

    @staticmethod
    def _histories(session: DatabaseSession, roster):
        return {
            (person.person_id, meal): session.database.status_history(
                person.person_id,
                meal,
            )
            for person in roster.persons
            for meal in Meal
        }

    @staticmethod
    def _revision_display_code(session: DatabaseSession, roster) -> str:
        return session.database.revision_display_code(roster.revision_id)

    def _access_dialog(self, parent: QWidget | None):
        database_path = self._settings.database_path
        if database_path is None:
            return self._create_dialog_type(settings=self._settings, parent=parent)
        return self._login_dialog_type(
            database_path,
            backup_directory=self._settings.backup_directory,
            parent=parent,
        )
