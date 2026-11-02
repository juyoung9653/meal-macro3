"""Run the SQLite v2 desktop application."""

from __future__ import annotations

from .user_messages import user_error

import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from .application import DesktopStartupController, StartupStatus
from src.version import APP_VERSION
from .portable_settings import DesktopStorageSettings, application_lock


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    QCoreApplication.setOrganizationName("분당구")
    QCoreApplication.setApplicationName("급식 배송 운영 SQLite v2")
    QCoreApplication.setApplicationVersion(APP_VERSION)
    from .window_preferences import install_window_preferences
    install_window_preferences(app)
    lock = None
    outcome = None
    try:
        lock = application_lock().acquire()
        settings = DesktopStorageSettings(app_lock=lock)
        outcome = DesktopStartupController(settings=settings).start()
        if not outcome.ready:
            return 1 if outcome.status is StartupStatus.FAILED else 0
        assert outcome.window is not None
        from .output_retention import OutputCleanupScheduler
        outcome.window._output_cleanup = OutputCleanupScheduler(settings, outcome.window)
        outcome.window.show()
        return app.exec()
    except Exception as error:
        QMessageBox.critical(None, "프로그램을 열 수 없습니다", user_error(error))
        return 1
    finally:
        try:
            if outcome is not None:
                outcome.close()
        finally:
            if lock is not None:
                lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
