"""Visual tokens for the date-spine operations workspace."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QComboBox


INK = "#17313D"
INK_SOFT = "#244653"
CANVAS = "#EDF3F5"
SURFACE = "#FFFFFF"
LINE = "#CAD8DD"
TEXT = "#17313D"
MUTED = "#637A83"
ROUTE = "#16788A"
DELIVERY = "#2E7D5B"
PAUSED = "#C77B24"
STOPPED = "#B64A48"
NOT_ENROLLED = "#82949A"
FOCUS = "#55B7C6"
_FONTS_REGISTERED = False
_COMBO_WHEEL_GUARD: QObject | None = None


class _ComboWheelGuard(QObject):
    """Keep a closed combo box from changing under an accidental wheel turn."""

    def eventFilter(self, watched, event) -> bool:
        if (
            isinstance(watched, QComboBox)
            and event.type() == QEvent.Type.Wheel
            and not watched.view().isVisible()
        ):
            event.accept()
            return True
        return super().eventFilter(watched, event)


def register_application_fonts() -> None:
    """Register supported Windows fonts when Qt misses the system catalog."""

    global _COMBO_WHEEL_GUARD, _FONTS_REGISTERED
    application = QApplication.instance()
    if application is not None and _COMBO_WHEEL_GUARD is None:
        _COMBO_WHEEL_GUARD = _ComboWheelGuard(application)
        application.installEventFilter(_COMBO_WHEEL_GUARD)
    if _FONTS_REGISTERED:
        return
    for font_path in (
        Path("C:/Windows/Fonts/malgun.ttf"),
        Path("C:/Windows/Fonts/malgunbd.ttf"),
        Path("C:/Windows/Fonts/bahnschrift.ttf"),
    ):
        if font_path.is_file():
            QFontDatabase.addApplicationFont(str(font_path))
    _FONTS_REGISTERED = True


def application_stylesheet() -> str:
    return f"""
    QWidget {{
        color: {TEXT};
        font-family: "Malgun Gothic", "Noto Sans CJK KR", sans-serif;
        font-size: 13px;
    }}
    QMainWindow, QDialog {{ background: {CANVAS}; }}
    QDialog#databaseAccessDialog {{ background: {CANVAS}; }}
    QFrame#startupRail {{
        background: {INK};
        border: none;
        border-radius: 7px;
    }}
    QFrame#startupCard {{
        background: {SURFACE};
        border: 1px solid {LINE};
        border-radius: 7px;
    }}
    QLabel#startupProduct {{
        color: #9BC7CF;
        font-size: 11px;
        font-weight: 800;
        letter-spacing: 1px;
    }}
    QLabel#startupRailTitle {{
        color: #FFFFFF;
        font-size: 22px;
        font-weight: 800;
    }}
    QLabel#startupRailCopy {{ color: #BDD4D9; line-height: 1.45; }}
    QLabel#routeDot {{
        color: {FOCUS};
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 16px;
        font-weight: 800;
    }}
    QLabel#routeStop {{ color: #E8F3F5; font-size: 12px; font-weight: 700; }}
    QFrame#routeLine {{
        background: #456672;
        border: none;
        min-width: 2px;
        max-width: 2px;
    }}
    QLabel#startupTitle {{ font-size: 24px; font-weight: 800; }}
    QLabel#startupSubtitle, QLabel#fieldHelp {{ color: {MUTED}; }}
    QLabel#fieldLabel {{ color: #35545F; font-size: 12px; font-weight: 800; }}
    QLabel#recoveryNotice {{
        color: #6F531E;
        background: #FFF5DD;
        border: 1px solid #E8C979;
        border-radius: 5px;
        padding: 9px 11px;
    }}
    QLabel#exportPasswordStatus {{
        color: #0E5E6C;
        background: #EAF5F6;
        border: 1px solid #A8CFD6;
        border-left: 6px solid {ROUTE};
        border-radius: 5px;
        padding: 11px 13px;
        font-size: 15px;
        font-weight: 800;
    }}
    QLabel#exportPasswordStatus[configured="false"] {{
        color: #6F531E;
        background: #FFF5DD;
        border-color: #E8C979;
        border-left-color: {PAUSED};
    }}
    QFrame#currentExportPasswordPanel {{
        background: #F5FAFA;
        border: 1px solid #C5DADD;
        border-radius: 5px;
    }}
    QLabel#currentExportPasswordHint {{
        color: {MUTED};
        background: transparent;
        border: none;
        font-size: 11px;
    }}
    QLineEdit#currentExportPasswordEdit {{
        background: #FFFFFF;
        border-color: #A8C5CA;
    }}
    QLabel#errorMessage {{
        color: #8E3432;
        background: #FDECEB;
        border: 1px solid #EDB6B3;
        border-radius: 5px;
        padding: 8px 10px;
        font-weight: 700;
    }}
    QPushButton#linkAction {{
        background: transparent;
        color: {ROUTE};
        border: none;
        padding: 6px 2px;
        text-align: left;
        font-weight: 700;
    }}
    QPushButton#linkAction:hover {{ color: #0E5E6C; text-decoration: underline; }}
    QPushButton#browseAction {{
        background: #F3F7F8;
        border: 1px solid {LINE};
        padding: 7px 10px;
        border-radius: 4px;
        font-weight: 700;
    }}
    QPushButton#householdCriterionCheck {{
        background: #FFFFFF;
        color: {INK_SOFT};
        border: 2px solid #AFC2C8;
        border-radius: 5px;
        padding: 8px 13px;
        text-align: left;
        font-weight: 800;
    }}
    QPushButton#householdCriterionCheck:hover {{
        background: #F1F8F9;
        border-color: {FOCUS};
    }}
    QPushButton#householdCriterionCheck:checked {{
        background: {ROUTE};
        color: #FFFFFF;
        border-color: #0E6372;
        font-weight: 900;
    }}
    QFrame#importBusyPanel {{
        background: #EAF5F6;
        border: 1px solid #91C5CD;
        border-left: 6px solid {ROUTE};
        border-radius: 5px;
    }}
    QLabel#importBusyTitle {{
        color: {INK};
        font-size: 14px;
        font-weight: 800;
    }}
    QLabel#importBusyDetail {{ color: {MUTED}; font-size: 11px; }}
    QProgressBar#importBusyProgress {{
        background: #D5E8EB;
        border: none;
        border-radius: 3px;
        min-height: 7px;
        max-height: 7px;
        text-align: center;
    }}
    QProgressBar#importBusyProgress::chunk {{
        background: {ROUTE};
        border-radius: 3px;
    }}
    QDialog#importCorrectionDialog {{ background: {CANVAS}; }}
    QFrame#importCorrectionLaunchPanel {{
        background: #FFF8E9;
        border: 1px solid #E3C77B;
        border-left: 6px solid {PAUSED};
        border-radius: 5px;
    }}
    QFrame#importCorrectionPanel {{
        background: #FFF8E9;
        border: 1px solid #E3C77B;
        border-left: 6px solid {PAUSED};
        border-radius: 5px;
    }}
    QLabel#importCorrectionStamp {{
        color: #FFFFFF;
        background: {PAUSED};
        border-radius: 3px;
        padding: 7px 9px;
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 13px;
        font-weight: 800;
    }}
    QLabel#importCorrectionTitle {{
        color: #6F531E;
        font-size: 14px;
        font-weight: 900;
    }}
    QLabel#importCorrectionHelp {{ color: #7B673A; font-size: 11px; }}
    QTableWidget#importCorrectionTable {{
        background: #FFFFFF;
        alternate-background-color: #FFFCF4;
        border: 1px solid #DEC98D;
        gridline-color: #E9DDBB;
    }}
    QLineEdit#importCorrectionEditor,
    QComboBox#importCorrectionEditor {{
        background: #FFFFFF;
        border: 2px solid #D2A84E;
        min-height: 23px;
        font-weight: 700;
    }}
    QFrame#importCorrectionApplied {{
        background: #EAF5F2;
        border: 1px solid #A9D1C4;
        border-left: 6px solid {DELIVERY};
        border-radius: 5px;
    }}
    QLabel#importCorrectionAppliedText {{
        color: #245D4B;
        font-weight: 800;
    }}
    QFrame#mealFormatGuide {{
        background: #F6FAFB;
        border: 1px solid #C7D9DD;
        border-left: 5px solid {ROUTE};
        border-radius: 5px;
    }}
    QLabel#mealFormatGuideTitle {{ color: {INK}; font-weight: 800; }}
    QLabel#mealFormatGuideExample {{ color: {MUTED}; font-size: 11px; }}
    QFrame#mealDetectionPanel {{
        background: #EAF5F2;
        border: 1px solid #A9D1C4;
        border-radius: 5px;
    }}
    QLabel#mealDetectionStatus {{ color: #245D4B; font-weight: 800; }}
    QCheckBox {{ color: #49646E; spacing: 7px; }}
    QCheckBox::indicator {{ width: 16px; height: 16px; }}
    QFrame#dateSpine {{ background: {INK}; border: none; }}
    QLabel#productEyebrow {{
        color: #9BC7CF;
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 1px;
    }}
    QLabel#serviceDate {{
        color: #FFFFFF;
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 31px;
        font-weight: 700;
    }}
    QLabel#serviceWeekday {{ color: #BDD4D9; font-size: 13px; }}
    QLabel#spineLabel {{ color: #9FBAC1; font-size: 11px; font-weight: 700; }}
    QLabel#spineMetric {{ color: #FFFFFF; font-size: 14px; font-weight: 700; }}
    QToolButton#dateStep {{
        background: {INK_SOFT};
        color: #FFFFFF;
        border: 1px solid #3B5C68;
        min-width: 32px;
        min-height: 30px;
        border-radius: 4px;
    }}
    QToolButton#dateStep:hover {{ background: #315967; }}
    QDateEdit#serviceDateEdit {{
        background: {INK_SOFT};
        color: #FFFFFF;
        border: 1px solid #456672;
        padding: 6px 8px;
        border-radius: 4px;
    }}
    QPushButton#mealLane {{
        background: transparent;
        color: #BED0D5;
        text-align: left;
        border: none;
        border-left: 4px solid #4B6570;
        padding: 10px 12px;
        font-weight: 700;
    }}
    QPushButton#mealLane:hover {{ background: {INK_SOFT}; color: #FFFFFF; }}
    QPushButton#mealLane:checked {{
        background: #FFFFFF;
        color: {INK};
        border-left: 4px solid {FOCUS};
    }}
    QFrame#workspaceHeader, QFrame#inspector, QFrame#tableFrame {{
        background: {SURFACE};
        border: 1px solid {LINE};
    }}
    QFrame#editHeader {{
        background: #E8F3F5;
        border: 1px solid #BDD9DE;
        border-left: 5px solid {ROUTE};
    }}
    QFrame#statusChangeHeader {{
        background: {INK};
        border: 1px solid {INK};
        border-left: 5px solid {FOCUS};
    }}
    QFrame#statusChangeHeader QLabel#editEyebrow {{ color: #9BC7CF; }}
    QFrame#statusChangeHeader QLabel#workspaceTitle {{ color: #FFFFFF; }}
    QFrame#statusChangeHeader QLabel#workspaceSubtitle {{ color: #BDD4D9; }}
    QFrame#unitPriceHeader {{
        background: {INK};
        border: 1px solid {INK};
        border-left: 5px solid {FOCUS};
        border-radius: 5px;
    }}
    QFrame#unitPriceHeader QLabel#editEyebrow {{ color: #9BC7CF; }}
    QFrame#unitPriceHeader QLabel#workspaceTitle {{ color: #FFFFFF; }}
    QFrame#unitPriceHeader QLabel#workspaceSubtitle {{ color: #BDD4D9; }}
    QFrame#breakfastPolicyHeader {{
        background: {INK};
        border: 1px solid {INK};
        border-left: 5px solid {FOCUS};
        border-radius: 5px;
    }}
    QFrame#breakfastPolicyHeader QLabel#editEyebrow {{ color: #9BC7CF; }}
    QFrame#breakfastPolicyHeader QLabel#workspaceTitle {{ color: #FFFFFF; }}
    QFrame#breakfastPolicyHeader QLabel#workspaceSubtitle {{ color: #BDD4D9; }}
    QFrame#serviceAdjustmentHeader {{
        background: {INK};
        border: 1px solid {INK};
        border-left: 5px solid {FOCUS};
        border-radius: 5px;
    }}
    QFrame#serviceAdjustmentHeader QLabel#editEyebrow {{ color: #9BC7CF; }}
    QFrame#serviceAdjustmentHeader QLabel#workspaceTitle {{ color: #FFFFFF; }}
    QFrame#serviceAdjustmentHeader QLabel#workspaceSubtitle {{ color: #BDD4D9; }}
    QFrame#holidayCandidatePanel {{
        background: {SURFACE};
        border: 1px solid {LINE};
        border-left: 5px solid {ROUTE};
        border-radius: 5px;
    }}
    QLabel#holidayCandidateNotice {{
        color: #285866;
        background: #EAF5F6;
        border: 1px solid #B9D9DE;
        border-radius: 4px;
        padding: 8px 10px;
    }}
    QLabel#holidayCandidateSummary {{
        color: {INK_SOFT};
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-weight: 800;
    }}
    QFrame#holidayReviewTicket {{
        background: #FFF5DD;
        border: 1px solid #E8C979;
        border-left: 5px solid {PAUSED};
        border-radius: 5px;
    }}
    QLabel#holidayReviewStamp {{
        color: #FFFFFF;
        background: {PAUSED};
        border-radius: 3px;
        padding: 7px 10px;
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 18px;
        font-weight: 800;
    }}
    QLabel#holidayReviewName {{ color: {INK}; font-size: 17px; font-weight: 800; }}
    QFrame#holidayBatchTicket {{
        background: #FFF5DD;
        border: 1px solid #E8C979;
        border-left: 5px solid {PAUSED};
        border-radius: 5px;
    }}
    QLabel#holidayBatchStamp {{
        color: #FFFFFF;
        background: {PAUSED};
        border-radius: 3px;
        padding: 10px 12px;
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 20px;
        font-weight: 800;
    }}
    QLabel#holidayBatchSelection {{
        color: {INK_SOFT};
        font-weight: 800;
    }}
    QFrame#documentSourceHeader {{
        background: {INK};
        border: 1px solid {INK};
        border-left: 5px solid {FOCUS};
        border-radius: 5px;
    }}
    QFrame#documentSourceHeader QLabel#editEyebrow {{ color: #9BC7CF; }}
    QFrame#documentSourceHeader QLabel#workspaceTitle {{ color: #FFFFFF; }}
    QFrame#documentSourceHeader QLabel#workspaceSubtitle {{ color: #BDD4D9; }}
    QFrame#documentHubHeader {{
        background: {INK};
        border: 1px solid {INK};
        border-left: 5px solid {FOCUS};
        border-radius: 5px;
    }}
    QFrame#documentHubHeader QLabel#editEyebrow {{ color: #9BC7CF; }}
    QFrame#documentHubHeader QLabel#workspaceTitle {{ color: #FFFFFF; }}
    QFrame#documentHubHeader QLabel#workspaceSubtitle {{ color: #BDD4D9; }}
    QFrame#documentTray {{
        background: #F7FAFB;
        border: 1px solid {LINE};
        border-top: 4px solid {ROUTE};
        border-radius: 6px;
    }}
    QFrame#documentTray[category="settlement"] {{ border-top-color: {PAUSED}; }}
    QLabel#documentTrayTitle {{
        color: {INK};
        font-size: 16px;
        font-weight: 800;
    }}
    QLabel#documentTrayDescription {{ color: {MUTED}; font-size: 11px; }}
    QPushButton#documentActionCard {{
        background: #FFFFFF;
        border: 1px solid #D2DEE2;
        border-left: 5px solid {ROUTE};
        border-radius: 5px;
        padding: 0;
        text-align: left;
    }}
    QPushButton#documentActionCard[category="settlement"] {{
        border-left-color: {PAUSED};
    }}
    QPushButton#documentActionCard:hover {{
        background: #EDF7F8;
        border-color: #9CC8D0;
    }}
    QPushButton#documentActionCard[category="settlement"]:hover {{
        background: #FFF7E8;
        border-color: #DFC384;
    }}
    QPushButton#documentActionCard:focus {{ border: 2px solid {FOCUS}; }}
    QLabel#documentSettlementStamp, QLabel#documentRouteStamp {{
        color: #FFFFFF;
        background: {ROUTE};
        border: none;
        border-radius: 4px;
        font-size: 13px;
        font-weight: 800;
    }}
    QLabel#documentSettlementStamp {{ background: {PAUSED}; }}
    QLabel#documentCardTitle {{ color: {INK}; font-size: 17px; font-weight: 800; }}
    QLabel#documentCardDescription {{ color: {MUTED}; font-size: 11px; }}
    QLabel#documentHubHint {{
        color: #285866;
        background: #EAF5F6;
        border: 1px solid #B9D9DE;
        border-left: 5px solid {ROUTE};
        border-radius: 4px;
        padding: 9px 11px;
        font-weight: 700;
    }}
    QFrame#documentPeriodHeader {{
        background: {INK};
        border: 1px solid {INK};
        border-left: 5px solid {FOCUS};
        border-radius: 5px;
    }}
    QFrame#documentPeriodHeader QLabel#editEyebrow {{ color: #9BC7CF; }}
    QFrame#documentPeriodHeader QLabel#workspaceTitle {{ color: #FFFFFF; }}
    QFrame#documentPeriodHeader QLabel#workspaceSubtitle {{ color: #BDD4D9; }}
    QFrame#documentPeriodAnchor {{
        background: #EAF5F6;
        border: 1px solid #A8CFD6;
        border-left: 5px solid {ROUTE};
        border-radius: 5px;
    }}
    QLabel#documentPeriodAnchorStamp {{
        color: #FFFFFF;
        background: {ROUTE};
        border-radius: 4px;
        padding: 8px 7px;
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 20px;
        font-weight: 800;
    }}
    QLabel#documentPeriodAnchorDetail {{ color: {INK_SOFT}; font-weight: 800; }}
    QLabel#documentPeriodSummary {{
        color: #285866;
        background: #EAF5F6;
        border: 1px solid #B9D9DE;
        border-left: 5px solid {ROUTE};
        border-radius: 4px;
        padding: 10px 12px;
        font-size: 14px;
        font-weight: 800;
    }}
    QFrame#documentMealHeader {{
        background: {INK};
        border: 1px solid {INK};
        border-left: 5px solid {FOCUS};
        border-radius: 5px;
    }}
    QFrame#documentMealHeader QLabel#editEyebrow {{ color: #9BC7CF; }}
    QFrame#documentMealHeader QLabel#workspaceTitle {{ color: #FFFFFF; }}
    QFrame#documentMealHeader QLabel#workspaceSubtitle {{ color: #BDD4D9; }}
    QFrame#documentMealContext {{
        background: #EAF5F6;
        border: 1px solid #A8CFD6;
        border-left: 5px solid {ROUTE};
        border-radius: 5px;
    }}
    QLabel#documentMealContextStamp {{
        color: #FFFFFF;
        background: {ROUTE};
        border-radius: 4px;
        padding: 8px 7px;
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 19px;
        font-weight: 800;
    }}
    QLabel#documentMealContextDetail {{ color: {INK_SOFT}; font-weight: 800; }}
    QFrame#documentMealCard {{
        background: #FFFFFF;
        border: 1px solid #D2DEE2;
        border-top: 4px solid #AEBCC1;
        border-radius: 5px;
    }}
    QFrame#documentMealCard[selected="true"] {{
        background: #EDF7F8;
        border: 1px solid #8ABFC8;
        border-top: 4px solid {ROUTE};
    }}
    QAbstractButton#documentMealCheck {{
        color: {INK};
        font-size: 16px;
        font-weight: 800;
        spacing: 7px;
    }}
    QLabel#documentMealAnchorBadge {{
        color: #FFFFFF;
        background: {PAUSED};
        border-radius: 3px;
        padding: 4px 7px;
        font-size: 10px;
        font-weight: 800;
    }}
    QLabel#documentMealDescription {{ color: {MUTED}; font-size: 11px; }}
    QLabel#documentMealSummary {{
        color: #285866;
        background: #EAF5F6;
        border: 1px solid #B9D9DE;
        border-left: 5px solid {ROUTE};
        border-radius: 4px;
        padding: 10px 12px;
        font-size: 14px;
        font-weight: 800;
    }}
    QFrame#settingsHubHeader,
    QFrame#documentTemplateHeader,
    QFrame#documentOutputHeader,
    QFrame#documentDirectoryHeader {{
        background: {INK};
        border: 1px solid {INK};
        border-left: 5px solid {FOCUS};
        border-radius: 5px;
    }}
    QFrame#settingsHubHeader QLabel#editEyebrow,
    QFrame#documentTemplateHeader QLabel#editEyebrow,
    QFrame#documentOutputHeader QLabel#editEyebrow,
    QFrame#documentDirectoryHeader QLabel#editEyebrow {{ color: #9BC7CF; }}
    QFrame#settingsHubHeader QLabel#workspaceTitle,
    QFrame#documentTemplateHeader QLabel#workspaceTitle,
    QFrame#documentOutputHeader QLabel#workspaceTitle,
    QFrame#documentDirectoryHeader QLabel#workspaceTitle {{ color: #FFFFFF; }}
    QFrame#settingsHubHeader QLabel#workspaceSubtitle,
    QFrame#documentTemplateHeader QLabel#workspaceSubtitle,
    QFrame#documentOutputHeader QLabel#workspaceSubtitle,
    QFrame#documentDirectoryHeader QLabel#workspaceSubtitle {{ color: #BDD4D9; }}
    QPushButton#settingsActionCard {{
        background: #FFFFFF;
        border: 1px solid #D2DEE2;
        border-top: 5px solid {PAUSED};
        border-radius: 5px;
        padding: 0;
        text-align: left;
    }}
    QPushButton#settingsActionCard[scope="document_directory"] {{
        border-top-color: {ROUTE};
    }}
    QPushButton#settingsActionCard[scope="breakfast_delivery_policy"] {{
        border-top-color: {ROUTE};
    }}
    QPushButton#settingsActionCard:hover {{
        background: #FFF8E9;
        border-color: #D8BC79;
    }}
    QPushButton#settingsActionCard[scope="document_directory"]:hover {{
        background: #EDF7F8;
        border-color: #8ABFC8;
    }}
    QPushButton#settingsActionCard[scope="breakfast_delivery_policy"]:hover {{
        background: #EDF7F8;
        border-color: #8ABFC8;
    }}
    QPushButton#settingsActionCard:focus {{ border: 2px solid {FOCUS}; }}
    QLabel#settingsActionStamp {{
        color: #FFFFFF;
        background: {PAUSED};
        border-radius: 4px;
        font-size: 14px;
        font-weight: 800;
    }}
    QLabel#settingsActionStamp[scope="document_directory"] {{
        background: {ROUTE};
    }}
    QLabel#settingsActionStamp[scope="breakfast_delivery_policy"] {{
        background: {ROUTE};
    }}
    QLabel#settingsScopeLabel {{
        color: #81621F;
        font-size: 10px;
        font-weight: 800;
    }}
    QLabel#settingsScopeLabel[scope="document_directory"] {{ color: {ROUTE}; }}
    QLabel#settingsScopeLabel[scope="breakfast_delivery_policy"] {{ color: {ROUTE}; }}
    QLabel#settingsCardTitle {{ color: {INK}; font-size: 16px; font-weight: 800; }}
    QLabel#settingsCardDescription {{ color: {MUTED}; font-size: 11px; }}
    QLabel#settingsHubNote {{
        color: #285866;
        background: #EAF5F6;
        border: 1px solid #B9D9DE;
        border-left: 5px solid {ROUTE};
        border-radius: 4px;
        padding: 9px 11px;
        font-weight: 700;
    }}
    QFrame#documentTemplateCard {{
        background: #FFFFFF;
        border: 1px solid #CFDDE1;
        border-top: 5px solid #AAB9BE;
        border-radius: 5px;
    }}
    QFrame#documentTemplateCard[selected="true"] {{
        background: #EDF7F8;
        border: 1px solid #8ABFC8;
        border-top: 5px solid {ROUTE};
    }}
    QLabel#documentTemplateStamp {{
        color: #FFFFFF;
        background: {INK};
        border-radius: 4px;
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 14px;
        font-weight: 800;
    }}
    QRadioButton#documentTemplateChoice {{
        color: {INK};
        font-size: 18px;
        font-weight: 800;
        spacing: 8px;
    }}
    QLabel#documentTemplateDescription {{ color: {MUTED}; font-size: 12px; }}
    QLabel#documentTemplateCurrent {{
        color: #1E6751;
        background: #E7F4ED;
        border: 1px solid #AAD2C0;
        border-radius: 3px;
        padding: 5px 8px;
        font-weight: 800;
    }}
    QFrame#breakfastPolicyCurrent {{
        background: #EAF5F6;
        border: 1px solid #A8CFD6;
        border-left: 5px solid {ROUTE};
        border-radius: 5px;
    }}
    QLabel#breakfastPolicyStamp {{
        color: #FFFFFF;
        background: {ROUTE};
        border-radius: 4px;
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 14px;
        font-weight: 800;
    }}
    QLabel#breakfastPolicyCurrentText {{
        color: {INK_SOFT};
        font-weight: 800;
    }}
    QFrame#breakfastPolicyCard {{
        background: #FFFFFF;
        border: 1px solid #CFDDE1;
        border-left: 5px solid #AAB9BE;
        border-radius: 5px;
    }}
    QFrame#breakfastPolicyCard[selected="true"] {{
        background: #EDF7F8;
        border: 1px solid #8ABFC8;
        border-left: 5px solid {ROUTE};
    }}
    QRadioButton#breakfastPolicyChoice {{
        color: {INK};
        font-size: 16px;
        font-weight: 800;
        spacing: 8px;
    }}
    QLabel#breakfastPolicyDescription {{ color: {MUTED}; font-size: 12px; }}
    QLabel#breakfastPolicyExample {{
        color: #285866;
        background: #F2F8F9;
        border-radius: 3px;
        padding: 5px 8px;
        font-size: 11px;
        font-weight: 700;
    }}
    QFrame#breakfastPolicyDatePanel {{
        background: #F7FAFB;
        border: 1px solid #C9D8DC;
        border-left: 5px solid {ROUTE};
        border-radius: 5px;
    }}
    QDateEdit#breakfastPolicyDate {{
        background: #FFFFFF;
        border: 1px solid {LINE};
        padding: 7px 9px;
        border-radius: 4px;
        min-height: 25px;
        min-width: 140px;
    }}
    QLabel#breakfastPolicyDateGuide {{ color: {MUTED}; }}
    QLabel#breakfastPolicyNote {{
        color: #6F531E;
        background: #FFF5DD;
        border: 1px solid #E8C979;
        border-left: 5px solid {PAUSED};
        border-radius: 4px;
        padding: 8px 10px;
        font-weight: 700;
    }}
    QFrame#documentOutputContext {{
        background: #EAF5F6;
        border: 1px solid #A8CFD6;
        border-left: 5px solid {ROUTE};
        border-radius: 5px;
    }}
    QLabel#documentOutputContextStamp {{
        color: #FFFFFF;
        background: {ROUTE};
        border-radius: 4px;
        padding: 7px 6px;
        font-weight: 800;
    }}
    QLabel#documentOutputContextDetail {{ color: {INK_SOFT}; font-weight: 800; }}
    QFrame#documentOutputDestination,
    QFrame#documentDirectoryPanel {{
        background: #F7FAFB;
        border: 1px solid #C9D8DC;
        border-left: 7px solid {ROUTE};
        border-radius: 5px;
    }}
    QFrame#documentOutputDestination[mode="one_time"] {{
        background: #FFF7E6;
        border-color: #DFC27B;
        border-left-color: {PAUSED};
    }}
    QFrame#documentOutputDestination[mode="new_default"] {{
        background: #EDF7F8;
        border-color: #91C2CA;
    }}
    QFrame#documentOutputDestination[mode="missing"],
    QFrame#documentDirectoryPanel[available="false"] {{
        background: #F4F6F7;
        border-color: #CFD8DB;
        border-left-color: #AAB8BC;
    }}
    QLabel#documentOutputFolderStamp {{
        color: #FFFFFF;
        background: {ROUTE};
        border-radius: 4px;
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 15px;
        font-weight: 800;
    }}
    QLabel#documentOutputFolderStamp[mode="one_time"] {{
        background: {PAUSED};
    }}
    QLabel#documentOutputFolderStamp[mode="missing"] {{
        background: #8C9DA2;
    }}
    QLabel#documentOutputFolderStatus {{
        color: {ROUTE};
        font-size: 11px;
        font-weight: 800;
    }}
    QLabel#documentOutputFolderStatus[mode="one_time"] {{ color: #81621F; }}
    QLabel#documentOutputFolderName {{
        color: {INK};
        font-size: 20px;
        font-weight: 800;
    }}
    QLabel#documentOutputFolderPath {{ color: {INK_SOFT}; font-weight: 700; }}
    QFrame#documentOutputFilenamePanel {{
        background: #FFFFFF;
        border: 1px dashed #9DB0B6;
        border-left: 6px solid {INK};
        border-radius: 4px;
    }}
    QFrame#documentOutputFilenamePanel[available="false"] {{
        background: #F4F6F7;
        border-color: #C9D3D6;
        border-left-color: #AAB8BC;
    }}
    QFrame#documentOutputFilenamePanel[reissue="true"] {{
        background: #FFF7E6;
        border-color: #D9B969;
        border-left-color: {PAUSED};
    }}
    QLabel#documentOutputFilenameStamp {{
        color: #FFFFFF;
        background: {INK};
        border-radius: 3px;
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 13px;
        font-weight: 800;
        letter-spacing: 1px;
    }}
    QLabel#documentOutputFilenameStatus {{
        color: {INK_SOFT};
        font-size: 10px;
        font-weight: 800;
    }}
    QLabel#documentOutputFilename {{
        color: {INK};
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 15px;
        font-weight: 800;
    }}
    QFrame#documentCompletionHeader {{
        background: {INK};
        border: 1px solid {INK};
        border-left: 5px solid {FOCUS};
        border-radius: 5px;
    }}
    QFrame#documentCompletionHeader QLabel#editEyebrow {{ color: #9BC7CF; }}
    QFrame#documentCompletionHeader QLabel#workspaceTitle {{ color: #FFFFFF; }}
    QFrame#documentCompletionHeader QLabel#workspaceSubtitle {{ color: #BDD4D9; }}
    QFrame#documentCompletionTicket {{
        background: #FFFFFF;
        border: 1px solid #B9CDD2;
        border-top: 5px solid {ROUTE};
        border-radius: 5px;
    }}
    QFrame#documentCompletionStamp {{
        background: {ROUTE};
        border: none;
        border-radius: 4px;
    }}
    QLabel#documentCompletionStampTop {{
        color: #BFE0E5;
        font-size: 12px;
        font-weight: 800;
        letter-spacing: 2px;
    }}
    QLabel#documentCompletionStampMain {{
        color: #FFFFFF;
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 28px;
        font-weight: 800;
    }}
    QLabel#documentCompletionKind {{
        color: {ROUTE};
        font-size: 12px;
        font-weight: 800;
    }}
    QLabel#documentCompletionFilename {{
        color: {INK};
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 20px;
        font-weight: 800;
    }}
    QLabel#documentCompletionContext {{
        color: {INK_SOFT};
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 13px;
        font-weight: 700;
        line-height: 1.4;
    }}
    QLabel#documentCompletionVerification {{
        color: #1B674E;
        background: #E9F5EF;
        border: 1px solid #AFD4C2;
        border-left: 5px solid {DELIVERY};
        border-radius: 4px;
        padding: 8px 10px;
        font-weight: 800;
    }}
    QFrame#documentCompletionShelf {{
        background: #F4F8F9;
        border: 1px dashed #A9BDC2;
        border-radius: 4px;
    }}
    QLabel#documentCompletionPath {{
        color: {INK_SOFT};
        font-size: 12px;
        font-weight: 700;
    }}
    QLabel#documentCompletionNotice {{
        color: #6F531E;
        background: #FFF5DD;
        border: 1px solid #E8C979;
        border-left: 5px solid {PAUSED};
        border-radius: 4px;
        padding: 9px 11px;
        font-weight: 800;
    }}
    QLabel#documentCompletionStatus {{
        color: #1B674E;
        background: #E9F5EF;
        border: 1px solid #AFD4C2;
        border-left: 5px solid {DELIVERY};
        border-radius: 4px;
        padding: 8px 10px;
        font-weight: 700;
    }}
    QLabel#documentCompletionStatus[outcome="error"] {{
        color: #7A3230;
        background: #FDECEB;
        border-color: #E5B4B2;
        border-left-color: {STOPPED};
    }}
    QFrame#documentDirectoryPanel[available="true"] {{
        background: #EDF7F8;
        border-color: #91C2CA;
    }}
    QSpinBox#documentPeriodYear, QComboBox#documentPeriodMonth,
    QDateEdit#documentPeriodDate {{
        background: #FFFFFF;
        border: 1px solid {LINE};
        padding: 7px 9px;
        border-radius: 4px;
        min-height: 25px;
        font-weight: 800;
    }}
    QFrame#documentSourceTicket {{
        background: #EAF5F6;
        border: 1px solid #A8CFD6;
        border-left: 6px solid {ROUTE};
        border-radius: 5px;
    }}
    QFrame#documentSourceTicket[historical="true"] {{
        background: #FFF5DD;
        border: 1px solid #E8C979;
        border-left: 6px solid {PAUSED};
    }}
    QLabel#documentSourceStamp {{
        color: #FFFFFF;
        background: {ROUTE};
        border-radius: 3px;
        padding: 4px 8px;
        font-size: 11px;
        font-weight: 800;
    }}
    QLabel#documentSourceStamp[historical="true"] {{ background: {PAUSED}; }}
    QLabel#documentSourceCode {{
        color: {INK};
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 29px;
        font-weight: 800;
    }}
    QLabel#documentSourceReason {{ color: {INK_SOFT}; font-weight: 800; }}
    QPushButton#archiveAction {{
        background: #FFFFFF;
        color: {ROUTE};
        border: 1px solid #A8CFD6;
        padding: 9px 12px;
        border-radius: 4px;
        text-align: left;
        font-weight: 800;
    }}
    QPushButton#archiveAction:hover {{ background: #EDF7F8; }}
    QPushButton#archiveAction:checked {{ background: #EAF5F6; }}
    QPushButton#archiveAction:disabled {{
        color: #93A5AA;
        background: #F2F5F6;
        border-color: #D7E0E3;
    }}
    QFrame#revisionArchive {{
        background: {SURFACE};
        border: 1px solid {LINE};
        border-radius: 5px;
    }}
    QListWidget#revisionArchiveList {{
        background: #F7FAFB;
        border: 1px solid #D9E4E7;
        padding: 4px;
    }}
    QListWidget#revisionArchiveList::item {{
        border-bottom: 1px solid #DFE8EA;
        padding: 9px 8px;
    }}
    QListWidget#revisionArchiveList::item:selected {{
        background: #FFF1CF;
        color: {TEXT};
        border-left: 4px solid {PAUSED};
    }}
    QLabel#documentSourceNote {{
        color: #285866;
        background: #EAF5F6;
        border: 1px solid #B9D9DE;
        border-radius: 4px;
        padding: 9px 11px;
        font-weight: 700;
    }}
    QLabel#documentSourceNote[historical="true"] {{
        color: #6F531E;
        background: #FFF5DD;
        border-color: #E8C979;
    }}
    QFrame#editSection {{
        background: {SURFACE};
        border: 1px solid {LINE};
        border-radius: 5px;
    }}
    QLabel#editEyebrow {{
        color: {ROUTE};
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 11px;
        font-weight: 800;
        letter-spacing: 1px;
    }}
    QLabel#workspaceTitle {{ font-size: 20px; font-weight: 800; }}
    QLabel#workspaceSubtitle, QLabel#muted {{ color: {MUTED}; }}
    QLineEdit, QComboBox, QPlainTextEdit, QSpinBox {{
        background: #FFFFFF;
        border: 1px solid {LINE};
        padding: 7px 9px;
        border-radius: 4px;
    }}
    QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QPlainTextEdit:focus, QSpinBox:focus {{
        border: 2px solid {FOCUS};
    }}
    QSpinBox#unitPriceInput {{
        color: {INK};
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 19px;
        font-weight: 800;
        min-height: 25px;
    }}
    QDateEdit#unitPriceDate {{
        background: #FFFFFF;
        border: 1px solid {LINE};
        padding: 7px 9px;
        border-radius: 4px;
        min-height: 25px;
    }}
    QPushButton#primaryAction {{
        background: {ROUTE};
        color: #FFFFFF;
        border: none;
        padding: 8px 13px;
        border-radius: 4px;
        font-weight: 700;
    }}
    QPushButton#primaryAction:hover {{ background: #116777; }}
    QPushButton#primaryAction:disabled {{
        background: #A9B9BE;
        color: #EEF3F4;
    }}
    QPushButton#quietAction {{
        background: #FFFFFF;
        border: 1px solid {LINE};
        padding: 7px 11px;
        border-radius: 4px;
        font-weight: 700;
    }}
    QPushButton#quietAction:hover {{ background: #F3F7F8; }}
    QPushButton#quietAction:disabled {{ color: #9AABB0; background: #F1F5F6; }}
    QPushButton#documentHubAction {{
        background: #FFF4D9;
        color: #6F531E;
        border: 1px solid #E2C36F;
        border-left: 4px solid {PAUSED};
        padding: 7px 12px;
        border-radius: 4px;
        font-weight: 800;
    }}
    QPushButton#documentHubAction:hover {{ background: #FFE9B5; }}
    QLabel#clearNotice {{
        color: #6F531E;
        background: #FFF5DD;
        border: 1px solid #E8C979;
        border-left: 5px solid {PAUSED};
        border-radius: 4px;
        padding: 9px 11px;
        font-weight: 800;
    }}
    QFrame#mealChoiceCard {{
        background: {SURFACE};
        border: 1px solid {LINE};
        border-left: 4px solid #A9B9BE;
        border-radius: 5px;
    }}
    QFrame#mealChoiceCard[selected="true"] {{
        background: #EAF5F6;
        border: 1px solid #A8CFD6;
        border-left: 4px solid {ROUTE};
    }}
    QCheckBox#mealChoiceCheck {{ font-size: 16px; font-weight: 800; }}
    QFrame#serviceTreatmentCard {{
        background: {SURFACE};
        border: 1px solid {LINE};
        border-left: 4px solid #A9B9BE;
        border-radius: 5px;
    }}
    QFrame#serviceTreatmentCard[selected="true"] {{
        background: #EAF5F6;
        border: 1px solid #A8CFD6;
        border-left: 4px solid {ROUTE};
    }}
    QRadioButton#serviceTreatmentRadio {{ font-size: 15px; font-weight: 800; }}
    QFrame#replacementDeliveryPanel {{
        background: #F3F8F9;
        border: 1px solid #C8DADF;
        border-left: 5px solid {ROUTE};
        border-radius: 5px;
    }}
    QFrame#replacementManifest {{
        background: #F8FBFB;
        border: 1px solid #BBD2D6;
        border-left: 5px solid {FOCUS};
        border-radius: 5px;
    }}
    QLabel#serviceAdjustmentSummary {{
        color: #1D4C57;
        background: #EAF5F6;
        border: 1px solid #A8CFD6;
        border-left: 5px solid {ROUTE};
        border-radius: 4px;
        padding: 9px 11px;
        font-weight: 700;
    }}
    QLabel#mealCurrentState {{ color: {MUTED}; font-size: 12px; }}
    QPushButton#statusChoice {{
        background: {SURFACE};
        color: #49646E;
        border: 1px solid {LINE};
        border-radius: 5px;
        padding: 9px 8px;
        font-weight: 700;
    }}
    QPushButton#statusChoice:hover {{ background: #F3F7F8; }}
    QPushButton#statusChoice:checked {{
        background: #EAF5F6;
        color: {TEXT};
        border: 2px solid {ROUTE};
    }}
    QPushButton#statusChoice[statusKind="paused"]:checked {{
        background: #FFF5DD;
        border: 2px solid {PAUSED};
    }}
    QPushButton#statusChoice[statusKind="stopped"]:checked {{
        background: #FDECEB;
        border: 2px solid {STOPPED};
    }}
    QFrame#changeSummary {{
        background: #F3F7F8;
        border: 1px solid #C8DADF;
        border-left: 5px solid {ROUTE};
        border-radius: 4px;
    }}
    QLabel#summaryText {{
        color: {INK_SOFT};
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 13px;
        font-weight: 700;
    }}
    QFrame#priceChangeSummary {{
        background: #F3F7F8;
        border: 1px solid #C8DADF;
        border-radius: 4px;
    }}
    QFrame#effectiveDateStamp {{
        background: {ROUTE};
        border: none;
        min-width: 86px;
        max-width: 86px;
    }}
    QLabel#priceStampLabel {{
        color: #BFE0E5;
        font-size: 10px;
        font-weight: 800;
    }}
    QLabel#priceStampDate {{
        color: #FFFFFF;
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 20px;
        font-weight: 800;
    }}
    QTableView {{
        background: #FFFFFF;
        alternate-background-color: #F6F9FA;
        border: none;
        gridline-color: #E2EAED;
        selection-background-color: #DCEFF2;
        selection-color: {TEXT};
    }}
    QHeaderView::section {{
        background: #E8F0F2;
        color: #3E5963;
        border: none;
        border-right: 1px solid #D5E0E3;
        border-bottom: 1px solid #C4D3D8;
        padding: 8px 7px;
        font-size: 12px;
        font-weight: 700;
    }}
    QLabel#inspectorTitle {{ font-size: 16px; font-weight: 800; }}
    QLabel#sectionLabel {{
        color: {ROUTE};
        font-size: 11px;
        font-weight: 800;
    }}
    QLabel#personName {{ font-size: 23px; font-weight: 800; }}
    QLabel#statusBadge {{
        color: #FFFFFF;
        padding: 4px 8px;
        border-radius: 3px;
        font-size: 12px;
        font-weight: 800;
    }}
    QListWidget {{
        background: #F7FAFB;
        border: 1px solid #D9E4E7;
        padding: 3px;
    }}
    QMenu {{ background: #FFFFFF; border: 1px solid {LINE}; padding: 5px; }}
    QMenu::item {{ padding: 7px 26px 7px 10px; }}
    QMenu::item:selected {{ background: #DCEFF2; color: {TEXT}; }}
    QTabWidget::pane {{ background: #FFFFFF; border: 1px solid {LINE}; }}
    QTabBar::tab {{ padding: 7px 12px; background: #DDE7EA; }}
    QTabBar::tab:selected {{ background: #FFFFFF; font-weight: 700; }}
    QFrame#schoolCalendarHeader,
    QFrame#schoolCalendarReviewHeader,
    QFrame#schoolCalendarEditHeader {{
        background: {INK};
        border: 1px solid {INK};
        border-left: 5px solid {FOCUS};
        border-radius: 5px;
    }}
    QFrame#schoolCalendarHeader QLabel#editEyebrow,
    QFrame#schoolCalendarReviewHeader QLabel#editEyebrow,
    QFrame#schoolCalendarEditHeader QLabel#editEyebrow {{ color: #9BC7CF; }}
    QFrame#schoolCalendarHeader QLabel#workspaceTitle,
    QFrame#schoolCalendarReviewHeader QLabel#workspaceTitle,
    QFrame#schoolCalendarEditHeader QLabel#workspaceTitle {{ color: #FFFFFF; }}
    QFrame#schoolCalendarHeader QLabel#workspaceSubtitle,
    QFrame#schoolCalendarReviewHeader QLabel#workspaceSubtitle,
    QFrame#schoolCalendarEditHeader QLabel#workspaceSubtitle {{ color: #BDD4D9; }}
    QFrame#schoolCalendarSource,
    QFrame#schoolCalendarMapping,
    QFrame#schoolCalendarCoverage {{
        background: #FFFFFF;
        border: 1px solid {LINE};
        border-radius: 5px;
    }}
    QLabel#schoolCalendarMappingStatus,
    QLabel#schoolCalendarReviewSummary {{
        color: #285866;
        background: #EAF5F6;
        border: 1px solid #B9D9DE;
        border-left: 5px solid {ROUTE};
        border-radius: 4px;
        padding: 9px 11px;
        font-weight: 800;
    }}
    QLabel#schoolCalendarMappingStatus[outcome="review"],
    QLabel#schoolCalendarReviewSummary[outcome="review"] {{
        color: #6F531E;
        background: #FFF5DD;
        border-color: #E8C979;
        border-left-color: {PAUSED};
    }}
    QTableWidget#schoolCalendarPreviewTable,
    QTableWidget#schoolCalendarLedger,
    QTableWidget#schoolCalendarStoredLedger {{
        background: #FFFFFF;
        alternate-background-color: #F7FAFB;
        border: 1px solid #C8D7DB;
        gridline-color: #DDE7EA;
    }}
    QLabel#schoolCalendarStamp {{
        color: #FFFFFF;
        background: {ROUTE};
        border: 1px solid #3993A2;
        border-radius: 4px;
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 22px;
        font-weight: 800;
    }}
    QDateEdit#schoolCalendarDate,
    QComboBox#schoolCalendarMark {{
        background: #FFFFFF;
        border: 1px solid #B9CCD1;
        padding: 5px 7px;
        min-height: 22px;
    }}
    QScrollArea#schoolCalendarNotes {{
        background: transparent;
        border: none;
    }}
    QFrame#schoolCalendarNoteCard {{
        background: #FFF8E9;
        border: 1px solid #E2C982;
        border-left: 6px solid {PAUSED};
        border-radius: 5px;
    }}
    QLabel#schoolCalendarNoteHeading {{
        color: #76591C;
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-weight: 800;
    }}
    QLabel#schoolCalendarSourceNote {{ color: {INK}; font-weight: 800; }}
    QLabel#schoolCalendarNoteHint {{ color: #81621F; font-size: 11px; }}
    QFrame#schoolCalendarNoteFields {{
        background: #FFFFFF;
        border: 1px solid #E4D4A8;
        border-radius: 4px;
    }}
    QLabel#schoolCalendarNoNotes {{
        color: #1B674E;
        background: #E9F5EF;
        border: 1px solid #AFD4C2;
        border-left: 5px solid {DELIVERY};
        border-radius: 4px;
        padding: 10px 12px;
        font-weight: 800;
    }}
    QLabel#schoolCalendarEditStamp {{
        color: #FFFFFF;
        background: {ROUTE};
        border: 1px solid #52A7B3;
        border-radius: 4px;
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 20px;
        font-weight: 800;
    }}
    QLabel#schoolCalendarEditSummary {{
        color: #35545F;
        background: #F4F8F9;
        border: 1px solid #CCDDE1;
        border-left: 5px solid #A6BBC0;
        border-radius: 4px;
        padding: 9px 11px;
        font-weight: 800;
    }}
    QLabel#schoolCalendarEditSummary[changed="true"] {{
        color: #155E69;
        background: #EAF5F6;
        border-color: #A8CFD6;
        border-left-color: {ROUTE};
    }}
    QFrame#schoolCalendarSchoolRail,
    QFrame#schoolCalendarEditWorkspace {{
        background: #FFFFFF;
        border: 1px solid {LINE};
        border-radius: 5px;
    }}
    QListWidget#schoolCalendarSchoolList {{
        background: #F7FAFB;
        border: 1px solid #CFDDE0;
        outline: none;
        padding: 4px;
    }}
    QListWidget#schoolCalendarSchoolList::item {{
        border-bottom: 1px solid #E2EAEC;
        padding: 9px 7px;
    }}
    QListWidget#schoolCalendarSchoolList::item:selected {{
        color: {INK};
        background: #DDF0F2;
        border-left: 4px solid {ROUTE};
        font-weight: 800;
    }}
    QLabel#schoolCalendarSelectedSchool {{
        color: {INK};
        font-size: 19px;
        font-weight: 800;
    }}
    QFrame#schoolCalendarSegmentTicket {{
        background: #F6FAFA;
        border: 1px solid #C8DADD;
        border-left: 5px solid {ROUTE};
        border-radius: 5px;
    }}
    QLabel#schoolCalendarEditMode {{
        color: #225966;
        font-family: "Bahnschrift SemiCondensed", "Malgun Gothic";
        font-size: 14px;
        font-weight: 800;
    }}
    QLabel#schoolCalendarRuleHelp {{
        color: {MUTED};
        font-size: 11px;
    }}
    QLineEdit#schoolCalendarGrades {{
        background: #FFFFFF;
        border: 1px solid #B9CCD1;
        padding: 6px 7px;
    }}
    """
