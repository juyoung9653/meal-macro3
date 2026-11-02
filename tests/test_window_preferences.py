from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMainWindow, QMessageBox, QVBoxLayout
import pytest

from src.desktop_v2.window_preferences import WindowPreferences


class Editor(QDialog):
    def __init__(self):
        super().__init__()
        QVBoxLayout(self).addWidget(QLabel("합성 편집창"))
        self.resize(300, 200)


class OtherEditor(Editor):
    pass


@pytest.fixture
def manager(tmp_path):
    app = QApplication.instance() or QApplication([])
    prefs = WindowPreferences(app, settings=QSettings(str(tmp_path / "sizes.ini"), QSettings.Format.IniFormat))
    yield app, prefs
    app.removeEventFilter(prefs)
    app.aboutToQuit.disconnect(prefs.save_open_windows)
    prefs.deleteLater()


def test_buttons_size_restore_after_accept_and_window_types_stay_separate(manager):
    app, prefs = manager
    first = Editor()
    first.show()
    app.processEvents()
    assert first.windowFlags() & Qt.WindowType.WindowMinimizeButtonHint
    assert first.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint
    first.resize(610, 390)
    first.accept()
    app.processEvents()
    second, other = Editor(), OtherEditor()
    second.show()
    other.show()
    app.processEvents()
    assert second.size().width() == 610
    assert second.size().height() == 390
    assert other.size().width() == 300
    second.reject()
    other.close()


def test_maximized_restore_preserves_normal_size_and_embedded_widget_is_untouched(manager):
    app, prefs = manager
    first = Editor()
    first.show()
    first.resize(600, 400)
    app.processEvents()
    first.showMaximized()
    app.processEvents()
    first.close()
    second = Editor()
    second.show()
    app.processEvents()
    assert second.isMaximized()
    second.showNormal()
    app.processEvents()
    assert second.size().width() == 600
    second.close()
    embedded = Editor()
    embedded.setWindowFlags(Qt.WindowType.Widget)
    parent = Editor()
    embedded.setParent(parent)
    assert not prefs.eligible(embedded)
    parent.close()


@pytest.mark.parametrize("window_type", [Editor, QMainWindow, QMessageBox])
def test_title_bar_keeps_close_enabled(manager, window_type):
    app, prefs = manager
    window = window_type()
    window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    try:
        window.show()
        app.processEvents()
        assert window.windowFlags() & Qt.WindowType.WindowCloseButtonHint
        assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
        assert not window.windowFlags() & Qt.WindowType.WindowContextHelpButtonHint
        assert window.close()
        assert not window.isVisible()
    finally:
        window.hide()


def test_progress_window_keeps_close_blocked(manager):
    from PySide6.QtCore import QThread
    from src.desktop_v2.startup_loading import StartupLoadingDialog

    app, prefs = manager
    worker = QThread()
    window = StartupLoadingDialog(worker, message="합성 작업 진행 중")
    try:
        window.show()
        app.processEvents()
        assert not window.windowFlags() & Qt.WindowType.WindowCloseButtonHint
        assert not window.close()
        assert window.isVisible()
        window._finished()
        assert not window.isVisible()
    finally:
        window.hide()
