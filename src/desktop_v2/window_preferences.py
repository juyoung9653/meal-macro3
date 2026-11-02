"""Computer-local sizes for independently opened application windows."""
from PySide6.QtCore import QEvent, QObject, QSettings, QSize, Qt
from PySide6.QtWidgets import QDialog, QMainWindow, QMessageBox


class WindowPreferences(QObject):
    def __init__(self, application, *, settings=None):
        super().__init__(application)
        self.settings = settings if settings is not None else QSettings("분당구", "급식 배송 운영 SQLite v2")
        self.application = application
        application.installEventFilter(self)
        application.aboutToQuit.connect(self.save_open_windows)

    @staticmethod
    def eligible(window):
        return (isinstance(window, (QDialog, QMainWindow)) and window.isWindow()
                and not window.windowFlags() & Qt.WindowType.FramelessWindowHint)

    @staticmethod
    def key(window):
        # Titles may contain names, dates or file paths; never store them as keys.
        return f"windows/size_v1/{type(window).__module__}.{type(window).__qualname__}/"

    def save(self, window):
        if not self.eligible(window) or isinstance(window, QMessageBox) or not window.property("sizePreferencesRestored"):
            return
        size = window.normalGeometry().size() if window.isMaximized() or window.isMinimized() else window.size()
        if size.isValid():
            self.settings.setValue(self.key(window) + "size", size)
            self.settings.setValue(self.key(window) + "maximized", window.isMaximized())

    def save_open_windows(self):
        for window in self.application.topLevelWidgets():
            self.save(window)
        self.settings.sync()

    def eventFilter(self, watched, event):
        if not self.eligible(watched):
            return False
        if event.type() == QEvent.Type.Polish:
            # Inverting a PySide enum can also clear higher bits such as Close.
            # Toggle only these hints, preserving each window's close policy.
            watched.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, True)
            watched.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        elif event.type() == QEvent.Type.Show and not watched.property("sizePreferencesRestored"):
            watched.setProperty("sizePreferencesRestored", True)
            if not isinstance(watched, QMessageBox):
                size = self.settings.value(self.key(watched) + "size")
                if isinstance(size, QSize) and size.isValid():
                    screen = watched.screen()
                    if screen is not None:
                        size = size.boundedTo(screen.availableGeometry().size())
                    watched.resize(size.expandedTo(watched.minimumSize()))
                if self.settings.value(self.key(watched) + "maximized", False, type=bool):
                    watched.setWindowState(watched.windowState() | Qt.WindowState.WindowMaximized)
        elif event.type() == QEvent.Type.Hide:
            self.save(watched)
        return False


def install_window_preferences(application):
    if not hasattr(application, "_window_preferences"):
        application._window_preferences = WindowPreferences(application)
    return application._window_preferences
