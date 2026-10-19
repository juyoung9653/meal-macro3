"""Responsive modal shown while startup reads the operating database."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, QThread, QTimer, Slot
from PySide6.QtWidgets import QDialog, QFrame, QLabel, QProgressBar, QVBoxLayout, QWidget

from .theme import application_stylesheet, register_application_fonts


class _StartupTaskWorker(QThread):
    def __init__(self, task: Callable[[], Any]) -> None:
        super().__init__()
        self._task = task
        self.result: Any = None
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            self.result = self._task()
        except Exception as error:  # noqa: BLE001 - re-raised on the UI thread
            self.error = error


class StartupLoadingDialog(QDialog):
    """Keep the Qt event loop responsive until one startup task finishes."""

    def __init__(
        self,
        worker: QThread,
        *,
        message: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        register_application_fonts()
        self.setObjectName("databaseAccessDialog")
        self.setWindowTitle("운영 데이터 불러오는 중")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.setStyleSheet(application_stylesheet())
        self.setMinimumWidth(460)
        self._active = True

        shell = QVBoxLayout(self)
        shell.setContentsMargins(18, 18, 18, 18)
        panel = QFrame()
        panel.setObjectName("startupCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(30, 26, 30, 26)
        layout.setSpacing(12)

        title = QLabel("운영 데이터를 불러오고 있습니다")
        title.setObjectName("startupTitle")
        layout.addWidget(title)
        detail = QLabel(message)
        detail.setObjectName("startupSubtitle")
        detail.setWordWrap(True)
        layout.addWidget(detail)
        self.progress = QProgressBar()
        self.progress.setObjectName("importBusyProgress")
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        wait_notice = QLabel("잠시만 기다려주세요. 완료되면 운영 화면이 열립니다.")
        wait_notice.setObjectName("fieldHelp")
        wait_notice.setWordWrap(True)
        layout.addWidget(wait_notice)
        shell.addWidget(panel)

        worker.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)

    @Slot()
    def _finished(self) -> None:
        self._active = False
        self.accept()

    def reject(self) -> None:
        if not self._active:
            super().reject()

    def closeEvent(self, event) -> None:
        if self._active:
            event.ignore()
        else:
            super().closeEvent(event)


def run_startup_task(
    task: Callable[[], Any],
    *,
    message: str,
    parent: QWidget | None = None,
) -> Any:
    """Run database-only startup work without blocking GUI event processing."""
    worker = _StartupTaskWorker(task)
    dialog = StartupLoadingDialog(worker, message=message, parent=parent)
    QTimer.singleShot(0, worker.start)
    try:
        dialog.exec()
    finally:
        worker.wait()
        dialog.deleteLater()
    if worker.error is not None:
        raise worker.error
    return worker.result
