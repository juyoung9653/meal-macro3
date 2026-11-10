"""Computer-local editable delivery templates, scoped to the operating file."""
from __future__ import annotations

from .user_messages import user_error

import json
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
from src.excel.report_template_layout import ReportTemplateLayout
from src.excel.substitute_template_columns import sync_substitute_template
from src.domain.delivery_substitute import STANDARD_COLUMNS
from src.services.delivery_template_backup import DeliveryTemplateBackup
from .substitute_preferences import preferences
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

    @property
    def substitute_backup(self):
        return DeliveryTemplateBackup(self.root)

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

    def _report_layout_key(self, path):
        scope = str(Path(path).resolve()).casefold() if path is not None else "default"
        return self.key + "report_rows/" + sha256(scope.encode("utf-8")).hexdigest()

    def report_layout(self, path=None):
        raw = self.settings.value(self._report_layout_key(path))
        if raw is None:
            return ReportTemplateLayout()
        try:
            return ReportTemplateLayout(**json.loads(raw))
        except (TypeError, ValueError) as error:
            raise ValueError("저장된 일일식수 행 설정을 읽지 못했습니다. 양식 확인/수정에서 본문·복사 행 설정을 다시 지정해주세요.") from error

    def save(self, action: DocumentAction, path: Path | None, *, meal="석식", report_layout=None) -> None:
        if path is not None:
            validate_delivery_template(path, self.validation_kind(action, meal))
            if action is DocumentAction.DELIVERY_SUBSTITUTE:
                self.substitute_backup.remember(path)
        key = self.selection_key(action, meal)
        updates = {key: str(path.resolve()) if path else ""}
        if action is DocumentAction.DELIVERY_REPORT and self.profile is DocumentTemplateProfile.SCHOOL_MEAL:
            layout = report_layout or self.report_layout(path)
            workbook = load_workbook(path or get_settings().get_report_template_path())
            try:
                layout.validate(workbook["aaa"])
            finally:
                workbook.close()
            updates[self._report_layout_key(path)] = json.dumps(layout.manifest())
        previous = {name: (self.settings.contains(name), self.settings.value(name)) for name in updates}
        for name, value in updates.items():
            self.settings.setValue(name, value)
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            for name, (existed, value) in previous.items():
                if existed:
                    self.settings.setValue(name, value)
                else:
                    self.settings.remove(name)
            self.settings.sync()
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
                    "모든 식종은 선택일의 배송·일시중지 대상자를 1명씩 세며 중지·미가입은 제외합니다. 배송요일·묶음 수량·학교 일정은 적용하지 않습니다. 가구 집계는 DB의 가구 소속을 따릅니다.",
                    "글꼴·색·테두리·정렬·줄바꿈·열 너비·행 높이·인쇄 설정을 그대로 적용합니다. 내용이 잘려도 자동으로 늘리거나 줄이지 않습니다. 기사별 병합·집계는 자동으로 만듭니다.",
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
                rows = self.report_layout()
                rows.validate(ws)
                ws["A1"] = "날짜 자동 입력"
                for row in ws.iter_rows(min_row=rows.body_start, max_row=rows.body_end, max_col=9):
                    for cell in row:
                        cell.value = None
                for column in range(1, 10):
                    ws.cell(rows.total_row, column).value = "합계" if column == 1 else None
                guide = wb.create_sheet("수정 안내")
                for row, text in enumerate(("aaa 시트: 글꼴·크기·글자색·채우기·테두리를 Excel에서 수정하세요.",
                    "기본은 4~8행 본문·6행 복사 기준·9행 합계입니다. 양식 확인/수정의 본문·복사 행 설정에서 바꿀 수 있습니다.",
                    "기사가 많으면 복사 기준 행 바로 아래에 행을 삽입합니다. 이후 본문과 끝 테두리·합계·하단 문구도 함께 내려갑니다.",
                    "aaa 시트와 A:I 칼럼, 1~2행 날짜·식종 제목을 유지하세요. 행을 옮기면 본문·합계 위치를 다시 지정하고 해당 영역은 병합하지 마세요.",
                    "열 너비·행 높이·글꼴·테두리·정렬·인쇄 설정을 유지합니다. 이전 배송일과 가구 수가 다른 칸과 기사 이름은 연한 파란색으로 강조합니다.",
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
        self._database_id = database_id
        self._substitute_columns = STANDARD_COLUMNS
        self.store = store if store is not None else DeliveryTemplateStore(database_id, profile=profile)
        self.profile = self.store.profile
        self.meal = "석식"
        self.path = self.store.selected(action)
        self._saved_path = self.path
        self._report_layouts = {}
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
        if self.profile is DocumentTemplateProfile.SCHOOL_MEAL and action is DocumentAction.DELIVERY_REPORT:
            self.resize(720, 560)
            self.setMinimumHeight(530)
            self.report_rows_label = QLabel()
            self.report_rows_label.setWordWrap(True)
            layout.addWidget(self.report_rows_label)
            button = QPushButton("본문·복사 행 설정")
            button.setObjectName("quietAction")
            button.clicked.connect(self._edit_report_rows)
            layout.addWidget(button)
        if action is DocumentAction.DELIVERY_SUBSTITUTE:
            limits = QLabel("자유 양식은 값을 넣을 셀에 {성명}, {주소}, {배송일자}처럼 플레이스홀더를 입력하세요. "
                            "배송목록을 만들 때 각 플레이스홀더에 넣을 항목과 복사할 행을 선택합니다. 플레이스홀더가 가장 많은 행을 자동 제안하며 Excel 서식을 그대로 사용합니다. "
                            "기존 배송대타 예시 양식은 기존 칼럼·가구별 서식 방식을 유지합니다.")
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
        if hasattr(self, "report_rows_label"):
            rows = self._report_layout()
            self.report_rows_label.setText(f"본문 {rows.body_start}~{rows.body_end}행 · 복사 기준 {rows.copy_row}행 · 합계 {rows.total_row}행")

    def _report_layout(self):
        if self.path not in self._report_layouts:
            self._report_layouts[self.path] = self.store.report_layout(self.path)
        return self._report_layouts[self.path]

    def _edit_report_rows(self):
        from .report_template_layout import ReportTemplateLayoutDialog
        try:
            path = self.path or Path(get_settings().get_report_template_path())
            validate_delivery_template(path, "delivery_report")
            dialog = ReportTemplateLayoutDialog(path, self._report_layout(), self)
        except Exception as error:
            QMessageBox.critical(self, "행 설정을 열지 못했습니다", user_error(error))
            return
        dialog.template_path = self.path
        dialog.accepted.connect(self._apply_report_rows)
        dialog.finished.connect(dialog.deleteLater)
        self._report_rows_dialog = dialog
        dialog.open()

    def _apply_report_rows(self):
        dialog = self._report_rows_dialog
        self._report_layouts[dialog.template_path] = dialog.selection()
        self._refresh()

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

    def set_substitute_columns(self, columns):
        self._substitute_columns = tuple(columns)

    def _sync_substitute(self):
        if self.action is not DocumentAction.DELIVERY_SUBSTITUTE or self.path is None:
            return
        resolved = self.store.substitute_backup.resolve(self.path)
        if resolved.used_previous:
            self.store.root.mkdir(parents=True, exist_ok=True)
            self.path = self.store.root / f"배송목록_이전정상본_{uuid4().hex[:8]}.xlsx"
            self.path.write_bytes(resolved.contents)
            self._refresh()
            QMessageBox.warning(self, "배송목록 이전 정상 양식 사용", resolved.warning)
        store, key = preferences(self._database_id, self.store.settings)
        selected = tuple(column.key for column in STANDARD_COLUMNS[:6])
        note2 = next((column.key for column in self._substitute_columns
                      if column.key.startswith("custom:") and column.label.strip() == "비고2"), "note")
        selected += (note2,)
        try:
            saved = json.loads(store.value(key, "{}"))
            if isinstance(saved, dict) and isinstance(saved.get("columns"), list):
                selected = [key for key in saved["columns"] if isinstance(key, str)]
        except (TypeError, ValueError):
            pass
        sync_substitute_template(self.path, self._substitute_columns, selected)
        self.store.substitute_backup.remember(self.path)

    def _open(self):
        try:
            if self.path is None:
                rows = self._report_layout() if hasattr(self, "report_rows_label") else None
                self.path = self.store.create(self.action, meal=self.meal)
                if rows is not None:
                    self._report_layouts[self.path] = rows
                self._refresh()
            if self.action is DocumentAction.DELIVERY_SUBSTITUTE:
                self._sync_substitute()
            else:
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
            self._sync_substitute()
            options = {"report_layout": self._report_layout()} if hasattr(self, "report_rows_label") else {}
            self.store.save(self.action, self.path, meal=self.meal, **options)
        except Exception as error:
            QMessageBox.critical(self, "양식을 저장하지 못했습니다", user_error(error))
            return
        super().accept()
