"""Check the real Windows owner/style contract, not just Qt transientParent."""
import os
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.isolated_ui
@pytest.mark.skipif(sys.platform != "win32", reason="Windows taskbar contract")
def test_work_windows_remain_independent_through_restore_and_reshow(tmp_path):
    script = textwrap.dedent('''
        import ctypes
        import sys
        from shiboken6 import isValid
        from PySide6.QtCore import QSettings, QSize, Qt
        from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox, QWidget
        from src.desktop_v2.dialog_workflow import DialogRequest, dialog_workflow, launch, _show_work_dialog
        from src.desktop_v2.window_preferences import WindowPreferences

        app = QApplication([])
        prefs = WindowPreferences(app, settings=QSettings(sys.argv[1], QSettings.IniFormat))
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetWindow.restype = ctypes.c_void_p
        user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        main = QWidget()
        main.setWindowTitle("Synthetic main")
        main.showMinimized()
        opened = []

        @dialog_workflow
        def work(parent):
            dialog = QDialog(parent)
            dialog.setWindowTitle("Synthetic taskbar window")
            dialog.setWindowState(Qt.WindowMinimized)
            opened.append(dialog)
            yield DialogRequest(dialog)

        def check(dialog):
            app.processEvents()
            hwnd = int(dialog.winId())
            assert dialog.parentWidget() is main
            assert dialog in main.findChildren(QDialog)
            assert dialog.windowHandle().transientParent() is None
            assert not user32.GetWindow(hwnd, 4), "Native owner hides the taskbar entry"
            style = user32.GetWindowLongW(hwnd, -20)
            assert style & 0x40000, "WS_EX_APPWINDOW missing"
            assert not style & 0x80, "WS_EX_TOOLWINDOW hides Alt+Tab"

        try:
            for maximized in (False, True):
                key = "windows/size_v1/PySide6.QtWidgets.QDialog/"
                prefs.settings.setValue(key + "size", QSize(420, 280))
                prefs.settings.setValue(key + "maximized", maximized)
                run = launch(work, main)
                dialog = opened[-1]
                check(dialog)
                dialog.showNormal()
                check(dialog)
                dialog.showMaximized()
                check(dialog)
                dialog.showMinimized()
                check(dialog)
                dialog.hide()
                dialog.windowHandle().setTransientParent(main.windowHandle())
                _show_work_dialog(dialog)
                check(dialog)
                main.close()
                app.processEvents()
                assert run.closed and (not isValid(dialog) or not dialog.isVisible())
                main.showMinimized()
            for dialog in (QMessageBox(main), QFileDialog(main)):
                if isinstance(dialog, QFileDialog):
                    dialog.setOption(QFileDialog.DontUseNativeDialog, True)
                dialog.setWindowState(Qt.WindowMinimized)
                _show_work_dialog(dialog)
                app.processEvents()
                assert user32.GetWindow(int(dialog.winId()), 4) == int(main.winId())
                assert not user32.GetWindowLongW(int(dialog.winId()), -20) & 0x40000
                dialog.close()
        finally:
            main.close()
            app.removeEventFilter(prefs)
    ''')
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "windows.ini")],
        env={**os.environ, "QT_QPA_PLATFORM": "windows", "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8", timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
