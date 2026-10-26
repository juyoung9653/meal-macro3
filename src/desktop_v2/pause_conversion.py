"""DB-backed choice of same-month automatic pause conversion."""

from datetime import UTC, datetime
from uuid import uuid4

from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QLabel, QMessageBox, QVBoxLayout

from src.domain.pause_conversion import (
    PauseConversionSettings, build_pause_conversion_revision, pause_conversion_from_snapshot,
)
from .dialog_workflow import DialogRequest, dialog_workflow
from .import_controller import current_windows_actor
from .startup_loading import save_revision
from .theme import application_stylesheet, register_application_fonts


class PauseConversionDialog(QDialog):
    def __init__(self, current, parent=None):
        super().__init__(parent)
        register_application_fonts()
        self.setWindowTitle("일시중지 자동변환")
        self.setStyleSheet(application_stylesheet())
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        title = QLabel("일시중지 자동변환")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        self.mode = QComboBox()
        self.mode.setAccessibleName("일시중지 자동변환")
        self.mode.addItem("같은 달 안에서 자동변환", True)
        self.mode.addItem("자동변환 안 함", False)
        self.mode.setCurrentIndex(self.mode.findData(current.same_month))
        layout.addWidget(self.mode)
        guide = QLabel(
            "중지일과 배송 재개일이 같은 연도·월이면 그 사이를 일시중지로 표시합니다.\n"
            "예: 10/5 중지 → 10/20 배송이면 10/5~10/19는 일시중지입니다.\n"
            "10/31 중지 → 11/1 배송처럼 달을 넘기면 중지를 유지합니다.\n\n"
            "자동변환을 끄면 배송 재개 전까지 중지로 표시합니다.\n"
            "기존 기록의 조회에도 적용되며, 직접 입력한 일시중지는 유지됩니다."
        )
        guide.setWordWrap(True)
        layout.addWidget(guide)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("quietAction")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selection(self):
        return PauseConversionSettings(self.mode.currentData())


class PauseConversionController:
    def __init__(self, session):
        self.session = session

    @dialog_workflow
    def run(self, parent=None):
        try:
            roster = self.session.database.current_roster()
            if roster is None:
                raise ValueError("현재 명단이 없습니다.")
            snapshot = self.session.database.snapshot(roster.revision_id)
            dialog = PauseConversionDialog(pause_conversion_from_snapshot(snapshot), parent=parent)
            while (yield DialogRequest(dialog, self.session)) == QDialog.DialogCode.Accepted:
                try:
                    current = self.session.database.current_roster()
                    if current is None or current.revision_id != snapshot.revision_id:
                        if current is not None:
                            snapshot = self.session.database.snapshot(current.revision_id)
                        QMessageBox.warning(dialog, "변경 사항 확인", "설정을 여는 동안 다른 변경이 저장됐습니다. 입력한 설정을 확인한 뒤 다시 저장해주세요.")
                        continue
                    record = build_pause_conversion_revision(
                        snapshot, dialog.selection(), actor=current_windows_actor(),
                        recorded_at=datetime.now(UTC), revision_id=uuid4(),
                    )
                    if record is None:
                        return None
                    backup = save_revision(self.session, record, parent=parent)
                except Exception as error:
                    QMessageBox.critical(dialog, "일시중지 자동변환 설정을 저장하지 못했습니다", str(error))
                    continue
                if backup.error is not None:
                    QMessageBox.warning(parent, "설정은 저장됐지만 자동 백업에 실패했습니다", str(backup.error))
                return self.session.database.current_roster()
        except Exception as error:
            QMessageBox.critical(parent, "일시중지 자동변환 설정을 열지 못했습니다", str(error))
        return None
