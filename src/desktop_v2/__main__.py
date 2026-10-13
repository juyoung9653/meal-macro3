"""Run the SQLite v2 desktop application."""

from __future__ import annotations

import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from .application import DesktopStartupController, StartupStatus
from src.version import APP_VERSION


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    QCoreApplication.setOrganizationName("분당구")
    QCoreApplication.setApplicationName("급식 배송 운영 SQLite v2")
    QCoreApplication.setApplicationVersion(APP_VERSION)
    outcome = DesktopStartupController().start()
    if not outcome.ready:
        return 1 if outcome.status is StartupStatus.FAILED else 0
    assert outcome.window is not None
    outcome.window.show()
    try:
        return app.exec()
    finally:
        outcome.close()


if __name__ == "__main__":
    raise SystemExit(main())
