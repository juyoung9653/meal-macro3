"""Monthly counting and invoice history destinations selected from real templates."""

from datetime import UTC, datetime
from uuid import uuid4

from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QMessageBox, QVBoxLayout

from src.domain.invoice_settings import InvoiceSettings, build_invoice_settings_revision, invoice_settings_from_snapshot
from src.services.document_column_mappings import mapping_editor_spec
from .dialog_workflow import DialogRequest, dialog_workflow
from .import_controller import current_windows_actor
from .startup_loading import save_revision
from .theme import application_stylesheet, register_application_fonts


class InvoiceSettingsDialog(QDialog):
    def __init__(self, current, *, columns, parent=None):
        super().__init__(parent)
        register_application_fonts()
        self.setWindowTitle("식수·청구서 설정")
        self.setStyleSheet(application_stylesheet())
        self.resize(620, 340)
        layout = QVBoxLayout(self)
        title = QLabel("식수·청구서 설정")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        self.include_next_month = QCheckBox("이번 달에 묶어 배송하는 다음 달 식수 포함")
        self.include_next_month.setChecked(current.include_next_month)
        layout.addWidget(self.include_next_month)
        guide = QLabel("월별 식수·청구서·확인서에 함께 적용합니다. 예: 10/30 배송에 포함된 11/1 조식.\n일일식수는 실제 배송할 전체 식수를 표시합니다.")
        guide.setWordWrap(True)
        layout.addWidget(guide)
        form = QFormLayout()
        self.history_columns = {}
        for profile in ("급식", "행복도시락"):
            combo = QComboBox()
            combo.addItem("출력 안 함", "")
            for column in columns[profile]:
                combo.addItem(column.replace("\n", " "), column)
            selected = current.history_column(profile)
            if selected and combo.findData(selected) < 0:
                combo.addItem(f"현재 양식에 없음: {selected}", selected)
            combo.setCurrentIndex(combo.findData(selected))
            combo.setAccessibleName(f"{profile} 청구서 이력 칼럼")
            form.addRow(f"{profile} 양식 · 이력 넣을 칼럼", combo)
            self.history_columns[profile] = combo
        layout.addLayout(form)
        note = QLabel("선택한 템플릿 칼럼에는 명단 비고 대신 식종 변경 이력만 자동으로 들어갑니다.\n석식: 10/29~ 중지\n석식: 10/23~10/29 일시중지")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("quietAction")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selection(self):
        return InvoiceSettings(self.include_next_month.isChecked(),
                               self.history_columns["급식"].currentData(),
                               self.history_columns["행복도시락"].currentData())


class InvoiceSettingsController:
    def __init__(self, session):
        self.session = session

    @dialog_workflow
    def run(self, parent=None):
        try:
            roster = self.session.database.current_roster()
            if roster is None:
                raise ValueError("현재 명단이 없습니다.")
            snapshot = self.session.database.snapshot(roster.revision_id)
            columns = {}
            for profile in ("급식", "행복도시락"):
                spec = mapping_editor_spec(profile, "invoice", history_column="")
                columns[profile] = tuple(target for target in spec.targets if target not in spec.locked_targets)
            dialog = InvoiceSettingsDialog(invoice_settings_from_snapshot(snapshot), columns=columns, parent=parent)
            while (yield DialogRequest(dialog, self.session)) == QDialog.DialogCode.Accepted:
                selected = dialog.selection()
                if any(selected.history_column(profile) and selected.history_column(profile) not in columns[profile] for profile in columns):
                    QMessageBox.warning(dialog, "이력 칼럼 확인", "현재 템플릿에 있는 칼럼을 선택해주세요.")
                    continue
                try:
                    current_roster = self.session.database.current_roster()
                    if current_roster is None or current_roster.revision_id != snapshot.revision_id:
                        if current_roster is not None:
                            snapshot = self.session.database.snapshot(current_roster.revision_id)
                        QMessageBox.warning(dialog, "변경 사항 확인", "설정을 여는 동안 다른 변경이 저장됐습니다. 입력한 설정을 확인한 뒤 다시 저장해주세요.")
                        continue
                    record = build_invoice_settings_revision(snapshot, selected, actor=current_windows_actor(), recorded_at=datetime.now(UTC), revision_id=uuid4())
                    if record is None:
                        return None
                    backup = save_revision(self.session, record, parent=parent)
                except Exception as error:
                    QMessageBox.critical(dialog, "식수·청구서 설정을 저장하지 못했습니다", str(error))
                    continue
                if backup.error is not None:
                    QMessageBox.warning(parent, "설정은 저장됐지만 자동 백업에 실패했습니다", str(backup.error))
                return self.session.database.current_roster()
        except Exception as error:
            QMessageBox.critical(parent, "식수·청구서 설정을 저장하지 못했습니다", str(error))
        return None
