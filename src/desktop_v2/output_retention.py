"""Output retention controls and automatic cleanup while the app is running."""
from concurrent.futures import ThreadPoolExecutor
import logging
from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QFormLayout, QLabel, QSpinBox, QVBoxLayout
from src.services.output_retention import cleanup_output_files
from .theme import application_stylesheet, register_application_fonts


class OutputRetentionDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("출력 파일 보관기간")
        self.resize(540, 290)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        title = QLabel("출력 파일 보관기간")
        title.setObjectName("workspaceTitle")
        root.addWidget(title)
        description = QLabel("output 폴더 안에서 마지막 수정 후 보관기간이 지난 파일을 자동 삭제합니다. 기본값은 1개월입니다.")
        description.setWordWrap(True)
        root.addWidget(description)
        location = QLabel(str(settings.base_directory / "output"))
        location.setWordWrap(True)
        location.setObjectName("muted")
        root.addWidget(location)
        form = QFormLayout()
        self.months = QSpinBox()
        self.months.setRange(0, 120)
        self.months.setSuffix("개월")
        self.months.setSpecialValueText("자동 삭제 안 함")
        self.months.setValue(settings.output_retention_months)
        form.addRow("보관기간", self.months)
        root.addLayout(form)
        note = QLabel("앱을 열 때와 실행 중 매시간 확인합니다. 하위 폴더의 파일도 포함하며, 다른 저장 폴더는 정리하지 않습니다. 변경한 기간은 다음 확인부터 적용합니다.")
        note.setWordWrap(True)
        root.addWidget(note)
        self.error = QLabel()
        self.error.setObjectName("errorMessage")
        self.error.setWordWrap(True)
        self.error.hide()
        root.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _save(self):
        try:
            self._settings.save_output_retention_months(self.months.value())
        except (OSError, ValueError) as error:
            self.error.setText(str(error))
            self.error.show()
            return
        self.accept()


class OutputCleanupScheduler(QObject):
    def __init__(self, settings, parent):
        super().__init__(parent)
        self._settings = settings
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="output-retention")
        self._future = None
        self._stopped = False
        self.timer = QTimer(self)
        self.timer.setInterval(60 * 60 * 1000)
        self.timer.timeout.connect(self.check)
        QApplication.instance().aboutToQuit.connect(self.stop)
        self.destroyed.connect(lambda: self._pool.shutdown(wait=False, cancel_futures=True))
        self.timer.start()
        QTimer.singleShot(0, self.check)

    def check(self):
        if self._stopped or (self._future is not None and not self._future.done()):
            return
        if self._future is not None:
            try:
                self._future.result()
            except Exception:
                logging.getLogger(__name__).exception("Output retention check failed")
        self._future = self._pool.submit(cleanup_output_files, self._settings.base_directory,
                                         self._settings.output_retention_months)

    def stop(self):
        self._stopped = True
        self.timer.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)
