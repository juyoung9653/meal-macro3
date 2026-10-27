"""Responsive modal shown while startup reads the operating database."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from PySide6.QtCore import QEventLoop, Qt, QThread, QTimer, Slot
from shiboken6 import isValid
from PySide6.QtWidgets import QApplication, QDialog, QFrame, QLabel, QLineEdit, QProgressBar, QVBoxLayout, QWidget

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
        title: str = "운영 데이터를 불러오는 중입니다",
    ) -> None:
        super().__init__(parent)
        register_application_fonts()
        self.setObjectName("databaseAccessDialog")
        self.setWindowTitle(title)
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

        title = QLabel(title)
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
        wait_notice = QLabel("잠시만 기다려주세요. 작업이 끝나면 자동으로 닫힙니다.")
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


PROGRESS_DELAY_MS = 1000


def run_worker_with_delayed_progress(worker: QThread, dialog: QDialog) -> None:
    """Wait responsively, showing the modal only for work lasting a full second."""
    app = QApplication.instance()
    windows = [window for window in app.topLevelWidgets()
               if window is not dialog and window.isVisible() and window.isEnabled()]
    source_window = app.activeWindow()
    if source_window not in windows:
        parent = dialog.parentWidget()
        source_window = parent.window() if parent is not None else None
    if source_window not in windows:
        source_window = None
    source_focus = source_window.focusWidget() if source_window is not None else None
    source_text_selection = (
        (source_focus.text(), source_focus.cursorPosition(), source_focus.selectionStart(),
         source_focus.selectionLength()) if isinstance(source_focus, QLineEdit) else None
    )
    shown = False
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.setTimerType(Qt.TimerType.PreciseTimer)

    def show_if_running():
        nonlocal shown
        if worker.isRunning():
            shown = True
            dialog.setEnabled(True)
            dialog.show()

    def restore_source_window():
        if source_window is None or not isValid(source_window):
            return
        if not source_window.isVisible() or not source_window.isEnabled():
            return
        # Respect a window selected during the wait or before the queued restore.
        active_window = app.activeWindow()
        if (active_window is not None and active_window not in (dialog, source_window)
                and active_window.isVisible()):
            return
        if any(window is not dialog and window is not source_window
               and window not in windows and window.isVisible()
               for window in app.topLevelWidgets()):
            return
        source_window.raise_()
        source_window.activateWindow()
        if source_focus is not None and isValid(source_focus) and source_focus.isVisible() and source_focus.isEnabled():
            source_focus.setFocus(Qt.FocusReason.OtherFocusReason)
            if source_text_selection is not None:
                text, cursor, start, length = source_text_selection
                if source_focus.text() == text:
                    if start >= 0:
                        source_focus.setSelection(start, length)
                    else:
                        source_focus.setCursorPosition(cursor)

    timer.timeout.connect(show_if_running)
    worker.finished.connect(loop.quit, Qt.ConnectionType.QueuedConnection)
    try:
        # Prevent duplicate edits while the wait window is still hidden.
        for window in windows:
            window.setEnabled(False)
        worker.start()
        timer.start(PROGRESS_DELAY_MS)
        loop.exec()
    finally:
        timer.stop()
        worker.wait()
        dialog.hide()
        for window in windows:
            if isValid(window):
                window.setEnabled(True)
        if shown:
            restore_source_window()
            QTimer.singleShot(0, restore_source_window)
        dialog.deleteLater()
        worker.finished.disconnect(loop.quit)


def take_worker_result(worker):
    """Transfer an outcome without leaving worker/exception traceback cycles.

    A traceback retains the worker's run frame and its data-task closure. Clear
    the stored exception on the GUI caller before it can later be collected by
    another worker, which could otherwise destroy captured widgets off-thread.
    """
    try:
        if worker.error is not None:
            raise worker.error
        return worker.result
    finally:
        worker.error = None


_T = TypeVar("_T")


def run_progress_task(
    task: Callable[[], _T],
    *,
    message: str,
    parent: QWidget | None = None,
    title: str = "처리 중입니다",
) -> _T:
    """Wait responsively for data-only work; return or raise on the caller thread.

    The task must not access widgets. Calls from an existing worker run directly
    so a data helper cannot create a nested modal or move UI work off-thread.
    Without a QApplication, data-only controller callers remain synchronous.
    """
    app = QApplication.instance()
    if app is None or QThread.currentThread() != app.thread():
        return task()
    worker = _StartupTaskWorker(task)
    dialog = StartupLoadingDialog(
        worker, message=message, parent=parent if isinstance(parent, QWidget) else None, title=title,
    )
    run_worker_with_delayed_progress(worker, dialog)
    return take_worker_result(worker)


def run_startup_task(
    task: Callable[[], _T],
    *,
    message: str,
    parent: QWidget | None = None,
) -> _T:
    """Run database-only startup work without blocking GUI event processing."""
    return run_progress_task(
        task, message=message, parent=parent,
        title="운영 데이터를 불러오는 중입니다",
    )


def save_revision(session, record, *, parent: QWidget | None = None):
    """Keep revision commit, projection refresh and automatic backup together.

    DatabaseSession owns commit/backup semantics. Its partial-backup result is
    returned unchanged so each controller can show its existing warning.
    """
    return run_progress_task(
        lambda: session.append_revision(record),
        title="변경 내용을 저장하는 중입니다",
        message="변경 내용과 이력을 저장하고 자동 백업을 처리하는 중입니다.",
        parent=parent,
    )
