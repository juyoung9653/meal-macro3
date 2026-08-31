"""PySide6 desktop shell for the SQLite v2 application."""

from __future__ import annotations

from .roster_window import RosterTableModel, RosterWindow, StatusHistoryDialog
from .import_review import ImportReviewDialog
from .import_wizard import ImportWizardSelection, RosterImportWizard
from .status_edit import StatusPeriodDialog
from .document_source import DocumentSourceDialog
from .document_hub import DocumentAction, DocumentHubDialog, DocumentSetupRequest
from .document_period import (
    DocumentPeriodDialog,
    DocumentPeriodSelection,
    suggest_document_period,
)
from .document_meal import (
    DocumentMealDialog,
    DocumentMealSelection,
    suggest_document_meals,
)
from .document_output import (
    DefaultDocumentDirectoryDialog,
    DocumentOutputDialog,
    DocumentOutputMode,
    DocumentOutputSelection,
    next_available_document_output,
    suggest_document_filename,
)
from .document_completion import (
    DocumentCompletionDialog,
    GeneratedDocumentResult,
)
from .document_generation import (
    DocumentGenerationController,
    DocumentGenerationOutcome,
    DocumentGenerationStatus,
)
from .settings_hub import SettingsAction, SettingsHubDialog
from .breakfast_delivery_policy import (
    BreakfastDeliveryPolicyController,
    BreakfastDeliveryPolicyDialog,
    BreakfastDeliveryPolicyOutcome,
    BreakfastDeliveryPolicyStatus,
)
from .document_template import (
    DocumentTemplateChangeOutcome,
    DocumentTemplateChangeStatus,
    DocumentTemplateController,
    DocumentTemplateDialog,
)
from .person_edit import (
    PersonProfileEditController,
    PersonProfileEditDialog,
    PersonProfileEditOutcome,
    PersonProfileEditStatus,
)
from .meal_status_edit import (
    MealStatusChangeController,
    MealStatusChangeDialog,
    MealStatusChangeOutcome,
    MealStatusChangeStatus,
)
from .unit_price_edit import (
    UnitPriceChangeController,
    UnitPriceChangeDialog,
    UnitPriceChangeOutcome,
    UnitPriceChangeStatus,
)
from .export_password import (
    ExportPasswordChangeOutcome,
    ExportPasswordChangeStatus,
    ExportPasswordController,
    ExportPasswordDialog,
)
from .service_adjustment import (
    PublicHolidayBatchDialog,
    ServiceAdjustmentAction,
    ServiceAdjustmentController,
    ServiceAdjustmentDialog,
    ServiceAdjustmentHubDialog,
    ServiceAdjustmentOutcome,
    ServiceAdjustmentRequest,
    ServiceAdjustmentStatus,
)
from .school_calendar_import import (
    SchoolCalendarImportController,
    SchoolCalendarImportDialog,
    SchoolCalendarImportOutcome,
    SchoolCalendarImportStatus,
    SchoolCalendarReviewDialog,
)
from .school_calendar_edit import (
    SchoolCalendarEditController,
    SchoolCalendarEditDialog,
    SchoolCalendarEditOutcome,
    SchoolCalendarEditStatus,
)

_DATABASE_ACCESS_EXPORTS = {
    "CreateDatabaseDialog",
    "BreakfastDeliveryPolicyController",
    "BreakfastDeliveryPolicyDialog",
    "BreakfastDeliveryPolicyOutcome",
    "BreakfastDeliveryPolicyStatus",
    "DesktopStorageSettings",
    "LoginDialog",
    "MealStatusChangeController",
    "MealStatusChangeDialog",
    "MealStatusChangeOutcome",
    "MealStatusChangeStatus",
    "PersonProfileEditController",
    "PersonProfileEditDialog",
    "PersonProfileEditOutcome",
    "PersonProfileEditStatus",
    "RecoveryPasswordDialog",
    "RestoreBackupDialog",
}
_CONTROLLER_EXPORTS = {
    "DesktopStartupController": ("application", "DesktopStartupController"),
    "DesktopStartupOutcome": ("application", "DesktopStartupOutcome"),
    "GuidedImportOutcome": ("import_controller", "GuidedImportOutcome"),
    "GuidedImportStatus": ("import_controller", "GuidedImportStatus"),
    "GuidedInitialImportController": (
        "import_controller",
        "GuidedInitialImportController",
    ),
    "GuidedRosterImportController": (
        "import_controller",
        "GuidedRosterImportController",
    ),
    "StartupStatus": ("application", "StartupStatus"),
    "current_windows_actor": ("import_controller", "current_windows_actor"),
}

__all__ = [
    "CreateDatabaseDialog",
    "DesktopStorageSettings",
    "DesktopStartupController",
    "DesktopStartupOutcome",
    "DocumentAction",
    "DefaultDocumentDirectoryDialog",
    "DocumentCompletionDialog",
    "DocumentHubDialog",
    "DocumentGenerationController",
    "DocumentGenerationOutcome",
    "DocumentGenerationStatus",
    "DocumentMealDialog",
    "DocumentMealSelection",
    "DocumentOutputDialog",
    "DocumentOutputMode",
    "DocumentOutputSelection",
    "DocumentPeriodDialog",
    "DocumentPeriodSelection",
    "DocumentSetupRequest",
    "DocumentSourceDialog",
    "DocumentTemplateChangeOutcome",
    "DocumentTemplateChangeStatus",
    "DocumentTemplateController",
    "DocumentTemplateDialog",
    "ExportPasswordChangeOutcome",
    "ExportPasswordChangeStatus",
    "ExportPasswordController",
    "ExportPasswordDialog",
    "GuidedImportOutcome",
    "GuidedImportStatus",
    "GuidedInitialImportController",
    "GuidedRosterImportController",
    "GeneratedDocumentResult",
    "ImportReviewDialog",
    "ImportWizardSelection",
    "LoginDialog",
    "RecoveryPasswordDialog",
    "RestoreBackupDialog",
    "RosterTableModel",
    "RosterImportWizard",
    "RosterWindow",
    "PublicHolidayBatchDialog",
    "ServiceAdjustmentAction",
    "ServiceAdjustmentController",
    "ServiceAdjustmentDialog",
    "ServiceAdjustmentHubDialog",
    "ServiceAdjustmentOutcome",
    "ServiceAdjustmentRequest",
    "ServiceAdjustmentStatus",
    "SchoolCalendarImportController",
    "SchoolCalendarImportDialog",
    "SchoolCalendarImportOutcome",
    "SchoolCalendarImportStatus",
    "SchoolCalendarReviewDialog",
    "SchoolCalendarEditController",
    "SchoolCalendarEditDialog",
    "SchoolCalendarEditOutcome",
    "SchoolCalendarEditStatus",
    "SettingsAction",
    "SettingsHubDialog",
    "StatusHistoryDialog",
    "StatusPeriodDialog",
    "StartupStatus",
    "UnitPriceChangeController",
    "UnitPriceChangeDialog",
    "UnitPriceChangeOutcome",
    "UnitPriceChangeStatus",
    "current_windows_actor",
    "next_available_document_output",
    "suggest_document_filename",
    "suggest_document_period",
    "suggest_document_meals",
]


def __getattr__(name: str):
    if name in _DATABASE_ACCESS_EXPORTS:
        from . import database_access

        value = getattr(database_access, name)
    elif name in _CONTROLLER_EXPORTS:
        module_name, attribute_name = _CONTROLLER_EXPORTS[name]
        if module_name == "application":
            from . import application as module
        else:
            from . import import_controller as module
        value = getattr(module, attribute_name)
    else:
        raise AttributeError(name)
    globals()[name] = value
    return value
