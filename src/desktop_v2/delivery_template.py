"""Computer-local editable delivery templates, scoped to the operating file."""
from __future__ import annotations

from .user_messages import user_error

from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from shutil import copyfile

from openpyxl import load_workbook
from PySide6.QtCore import QSettings, QStandardPaths, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFileDialog, QLabel, QMessageBox, QPushButton, QVBoxLayout

from src.core.settings import get_settings
from src.domain.document_templates import DocumentTemplateProfile
from src.excel.delivery_templates import validate_delivery_template
from .document_hub import DocumentAction
from .theme import application_stylesheet, register_application_fonts


class DeliveryTemplateStore:
    def __init__(self, database_id: str, *, profile=DocumentTemplateProfile.SCHOOL_MEAL, settings=None, root=None):
        self.settings = settings if settings is not None else QSettings("분당구", "급식 배송 운영 SQLite v2")
        scope = sha256(database_id.encode("utf-8")).hexdigest()
        self.profile = DocumentTemplateProfile(profile)
        self.legacy_key = f"documents/delivery_templates_v1/{scope}/"
        self.key = self.legacy_key + self.profile.value + "/"
        self.root = Path(root) if root is not None else Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)) / "delivery_templates" / scope / self.profile.value

    def validation_kind(self, action, meal="석식"):
        if self.profile is DocumentTemplateProfile.HAPPY_LUNCHBOX and action is DocumentAction.DELIVERY_REPORT:
            return "happy_report_" + ("lunch" if meal == "중식" else "dinner")
        return action.value

    def selection_key(self, action, meal):
        return self.key + action.value + ("/lunch" if self.validation_kind(action, meal) == "happy_report_lunch" else "")

    def selected(self, action: DocumentAction, *, meal="석식") -> Path | None:
        key = self.selection_key(action, meal)
        if not self.settings.contains(key) and self.profile is DocumentTemplateProfile.SCHOOL_MEAL:
            key = self.legacy_key + action.value
        value = self.settings.value(key)
        return Path(value) if isinstance(value, str) and value else None

    def save(self, action: DocumentAction, path: Path | None, *, meal="석식") -> None:
        if path is not None:
            validate_delivery_template(path, self.validation_kind(action, meal))
        key = self.selection_key(action, meal)
        had_previous = self.settings.contains(key)
        previous = self.settings.value(key)
        self.settings.setValue(key, str(path.resolve()) if path else "")
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            if had_previous:
                self.settings.setValue(key, previous)
            else:
                self.settings.remove(key)
            raise OSError("양식 선택을 저장하지 못했습니다. PC 설정 저장 권한을 확인해주세요.")

    def create(self, action: DocumentAction, *, meal="석식") -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{action.label}_양식_{uuid4().hex[:8]}.xlsx"
        if self.profile is DocumentTemplateProfile.HAPPY_LUNCHBOX and action is DocumentAction.DELIVERY_REPORT:
            source = Path(get_settings().get_happy_report_template_path(meal))
            validate_delivery_template(source, self.validation_kind(action, meal))
            wb = load_workbook(source)
            try:
                guide = wb.create_sheet("수정 안내")
                for row, text in enumerate(("첫 시트의 A1:J5 구조와 제목·기존 병합을 유지하세요.",
                    "1~3행 제목, 4행 기사·지역별 본문, 5행 합계 서식을 사용합니다.",
                    "중식 양식과 조식·석식 양식을 각각 수정·저장합니다. 기사·지역·주소·비고 연결은 문서 항목 설정에서 공용으로 정합니다.",
                    "조식·석식은 배송 없는 날에도 마지막 식수를 표시하며 이전 기록이 없으면 빈칸입니다. 가구 집계는 DB의 가구 소속을 따릅니다.",
                    "글꼴·색·정렬·열 너비·행 높이·인쇄 설정을 수정할 수 있습니다. 기사 구분 테두리와 병합은 자동으로 만듭니다.",
                    "Excel에서 저장하고 닫은 뒤 이 양식 사용을 누르세요."), 1):
                    guide.cell(row, 1, text)
                guide.column_dimensions["A"].width = 130
                wb.save(path)
            finally:
                wb.close()
        elif action is DocumentAction.DELIVERY_SUBSTITUTE:
            source = Path(get_settings().get_substitute_template_path(self.profile.value))
            validate_delivery_template(source, action.value)
            copyfile(source, path)
        else:
            source = Path(get_settings().get_report_template_path())
            validate_delivery_template(source, action.value)
            wb = load_workbook(source)
            try:
                for name in list(wb.sheetnames):
                    if name != "aaa":
                        del wb[name]
                ws = wb["aaa"]
                ws["A1"] = "날짜 자동 입력"
                for row in ws.iter_rows(min_row=4, max_row=9, max_col=9):
                    for cell in row:
                        cell.value = None
                ws["A9"] = "합계"
                guide = wb.create_sheet("수정 안내")
                for row, text in enumerate(("aaa 시트: 글꼴·크기·글자색·채우기·테두리를 Excel에서 수정하세요.",
                    "1~3행은 제목, 4~8행은 담당별 본문입니다. 6번째 담당부터 8행 서식을 반복합니다.",
                    "9행은 합계 서식입니다. 데이터와 합계 위치는 인원에 맞춰 자동으로 정해집니다.",
                    "시트 이름과 A:I 칼럼, 1~9행 위치를 유지하세요. 데이터 영역 병합·행/열 삽입은 지원하지 않습니다.",
                    "직접 수정한 양식은 비고의 자동 글꼴/색과 변경 강조색 대신 지정한 서식을 사용합니다.",
                    "Excel에서 저장하고 닫은 뒤 이 양식 사용을 누르세요. 기존 생성 파일은 바뀌지 않습니다."), 1):
                    guide.cell(row, 1, text)
                guide.column_dimensions["A"].width = 120
                wb.save(path)
            finally:
                wb.close()
        return path


class DeliveryTemplateDialog(QDialog):
    def __init__(self, action: DocumentAction, database_id: str, parent=None, *, profile=DocumentTemplateProfile.SCHOOL_MEAL, store=None, opener=None):
        if action not in (DocumentAction.DELIVERY_REPORT, DocumentAction.DELIVERY_SUBSTITUTE):
            raise ValueError("배송 문서 양식을 선택해주세요.")
        super().__init__(parent)
        self.action = action
        self.store = store if store is not None else DeliveryTemplateStore(database_id, profile=profile)
        self.profile = self.store.profile
        self.meal = "석식"
        self.path = self.store.selected(action)
        self._saved_path = self.path
        self._opener = opener or QDesktopServices.openUrl
        self.setWindowTitle(f"{self.profile.value} · {action.label} 양식 확인/수정")
        self.resize(720, 430)
        self.setMinimumSize(620, 400)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        title = QLabel(f"{self.profile.value} · {action.label}")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        note = QLabel("Excel에서 글꼴·색·테두리를 직접 수정할 수 있습니다.\n" + "처음 열면 해당 업체 기본 양식의 개인 정보 없는 복사본을 만듭니다.")
        note.setWordWrap(True)
        layout.addWidget(note)
        if self.profile is DocumentTemplateProfile.HAPPY_LUNCHBOX and action is DocumentAction.DELIVERY_REPORT:
            self.meal_combo = QComboBox()
            self.meal_combo.addItem("조식·석식 양식", "석식")
            self.meal_combo.addItem("중식 양식", "중식")
            self.meal_combo.currentIndexChanged.connect(self._change_meal)
            layout.addWidget(self.meal_combo)
            layout.addWidget(QLabel("기사·지역·주소·비고 항목 연결은 두 양식이 공용으로 사용합니다."))
        self.path_label = QLabel()
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.path_label)
        for label, handler in (("Excel에서 양식 열기", self._open), ("다른 양식 파일 선택", self._choose), ("기본 양식 사용", self._reset)):
            button = QPushButton(label)
            button.setObjectName("quietAction")
            button.clicked.connect(handler)
            layout.addWidget(button)
        if action is DocumentAction.DELIVERY_SUBSTITUTE:
            limits = QLabel("템플릿의 셀 정렬·줄바꿈·표시 형식·열 너비·행 높이·인쇄 설정 변경은 출력에 반영되지 않습니다. "
                            "정렬과 용지·페이지 구분은 배송목록을 만들 때 선택하세요.")
            limits.setWordWrap(True)
            layout.addWidget(limits)
        hint = QLabel("양식의 ‘수정 안내’ 시트를 확인하세요. Excel에서 저장하고 닫은 뒤 ‘이 양식 사용’을 누르세요.\n선택은 이 PC의 운영 파일·급식/행복도시락별로 기억합니다. 취소하면 기존 양식 선택을 유지하며, Excel에서 이미 저장한 수정 내용은 되돌리지 않습니다.")
        hint.setWordWrap(True)
        hint.setObjectName("settingsHubNote")
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("이 양식 사용")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("quietAction")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh()

    def _refresh(self):
        self.path_label.setText(str(self.path) if self.path else f"현재 선택: {self.profile.value} 기본 양식")

    def _change_meal(self, index):
        requested = self.meal_combo.itemData(index)
        if self.path != self._saved_path and QMessageBox.question(self, "저장하지 않은 양식 선택",
                "현재 양식 선택을 버리고 다른 식종으로 이동할까요?",
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Discard:
            self.meal_combo.blockSignals(True)
            self.meal_combo.setCurrentIndex(self.meal_combo.findData(self.meal))
            self.meal_combo.blockSignals(False)
            return
        self.meal = requested
        self.path = self.store.selected(self.action, meal=self.meal)
        self._saved_path = self.path
        self._refresh()

    def _open(self):
        try:
            if self.path is None:
                self.path = self.store.create(self.action, meal=self.meal)
                self._refresh()
            validate_delivery_template(self.path, self.store.validation_kind(self.action, self.meal))
            if not self._opener(QUrl.fromLocalFile(str(self.path.resolve()))):
                raise OSError("Excel을 열지 못했습니다. 표시된 경로의 파일을 직접 열어주세요.")
        except Exception as error:
            QMessageBox.critical(self, "양식을 열지 못했습니다", user_error(error))

    def _choose(self):
        filename, _ = QFileDialog.getOpenFileName(self, "배송 양식 선택", str(self.path or ""), "Excel 양식 (*.xlsx)")
        if not filename:
            return
        try:
            validate_delivery_template(filename, self.store.validation_kind(self.action, self.meal))
        except Exception as error:
            QMessageBox.critical(self, "양식을 선택하지 못했습니다", user_error(error))
            return
        self.path = Path(filename)
        self._refresh()

    def _reset(self):
        self.path = None
        self._refresh()

    def accept(self):
        try:
            self.store.save(self.action, self.path, meal=self.meal)
        except Exception as error:
            QMessageBox.critical(self, "양식을 저장하지 못했습니다", user_error(error))
            return
        super().accept()
