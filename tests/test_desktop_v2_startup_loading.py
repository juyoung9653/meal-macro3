"""The startup wait modal remains visible while database work runs off-thread."""

from __future__ import annotations

import os
from threading import Event, get_ident

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from src.desktop_v2.startup_loading import StartupLoadingDialog, run_startup_task, run_progress_task, save_revision


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("runner", [run_startup_task, run_progress_task])
@pytest.mark.parametrize("fail", [False, True])
def test_startup_loading_is_visible_and_responsive_until_task_finishes(app, fail, runner):
    entered, released = Event(), Event()
    ui_thread = get_ident()
    observed = []

    def task():
        assert get_ident() != ui_thread
        entered.set()
        assert released.wait(5), "UI event loop did not continue during startup load"
        if fail:
            raise OSError("delayed read failed")
        return "loaded"

    def heartbeat():
        if not entered.is_set() or released.is_set():
            return
        dialog = next(
            (
                widget
                for widget in app.topLevelWidgets()
                if isinstance(widget, StartupLoadingDialog) and widget.isVisible()
            ),
            None,
        )
        if dialog is None:
            return
        dialog.reject()
        dialog.close()
        observed.append((get_ident(), dialog.isVisible(), dialog.progress.maximum()))
        released.set()

    timer = QTimer()
    timer.timeout.connect(heartbeat)
    timer.start(10)
    try:
        if fail:
            with pytest.raises(OSError, match="delayed read failed"):
                runner(task, message="테스트 운영 데이터를 읽는 중입니다.")
        else:
            assert runner(
                task, message="테스트 운영 데이터를 읽는 중입니다."
            ) == "loaded"
    finally:
        timer.stop()
        released.set()

    assert observed == [(ui_thread, True, 0)]
    assert not any(
        isinstance(widget, StartupLoadingDialog) and widget.isVisible()
        for widget in app.topLevelWidgets()
    )


def test_nested_data_task_stays_on_same_worker_and_partial_backup_result_survives(app):
    from types import SimpleNamespace
    ui_thread = get_ident()
    threads = []
    result = SimpleNamespace(error=OSError("backup unavailable"))

    def append(record):
        threads.append(get_ident())
        assert record == "revision"
        return result

    session = SimpleNamespace(append_revision=append)

    def outer():
        threads.append(get_ident())
        return save_revision(session, "revision")

    assert run_progress_task(outer, message="저장하는 중입니다.") is result
    assert len(threads) == 2 and threads[0] == threads[1] != ui_thread


def test_progress_has_task_specific_text(app):
    from PySide6.QtWidgets import QLabel
    observed = []
    entered, released = Event(), Event()

    def task():
        entered.set()
        assert released.wait(5)

    def inspect():
        if not entered.is_set():
            return
        dialog = app.activeModalWidget()
        if isinstance(dialog, StartupLoadingDialog):
            observed.append((dialog.windowTitle(), " ".join(label.text() for label in dialog.findChildren(QLabel))))
            released.set()

    timer = QTimer()
    timer.timeout.connect(inspect)
    timer.start(10)
    try:
        run_progress_task(task, title="변경 내용을 저장하는 중입니다", message="자동 백업을 처리하는 중입니다.")
    finally:
        timer.stop()
        released.set()
    assert observed
    assert observed[0][0] == "변경 내용을 저장하는 중입니다"
    assert "운영 화면이 열립니다" not in observed[0][1]
    assert "자동 백업을 처리하는 중입니다." in observed[0][1]


@pytest.mark.parametrize("fail", [False, True])
def test_fast_task_never_shows_dialog_and_restores_windows(app, monkeypatch, fail):
    from PySide6.QtCore import QEventLoop
    from PySide6.QtWidgets import QWidget
    from src.desktop_v2 import startup_loading as loading

    shown = []
    class ObservedDialog(StartupLoadingDialog):
        def showEvent(self, event):
            shown.append(True)
            super().showEvent(event)

    monkeypatch.setattr(loading, "StartupLoadingDialog", ObservedDialog)
    parent, disabled = QWidget(), QWidget()
    parent.show()
    disabled.setEnabled(False)
    disabled.show()
    entered, released = Event(), Event()
    states = []
    timer = QTimer()
    def heartbeat():
        if entered.is_set():
            states.append((parent.isEnabled(), disabled.isEnabled()))
            released.set()
    timer.timeout.connect(heartbeat)
    timer.start(10)
    def task():
        entered.set()
        assert released.wait(5)
        if fail:
            raise ValueError("fast failure")
        return "done"
    try:
        if fail:
            with pytest.raises(ValueError, match="fast failure"):
                loading.run_progress_task(task, message="test", parent=parent)
        else:
            assert loading.run_progress_task(task, message="test", parent=parent) == "done"
        timer.stop()
        assert states and states[0] == (False, False)
        assert parent.isEnabled() and not disabled.isEnabled()
        # Process past the original deadline to catch orphaned show timers.
        loop = QEventLoop()
        QTimer.singleShot(1100, loop.quit)
        loop.exec()
        assert shown == []
    finally:
        timer.stop()
        released.set()
        parent.close()
        disabled.close()
        parent.deleteLater()
        disabled.deleteLater()


def test_slow_task_shows_only_after_one_second_with_enabled_modal(app):
    from time import monotonic
    from PySide6.QtWidgets import QWidget
    parent = QWidget()
    parent.show()
    released = Event()
    observations = []
    started = monotonic()
    def task():
        assert released.wait(5)
    def heartbeat():
        dialog = app.activeModalWidget()
        observations.append((monotonic() - started, isinstance(dialog, StartupLoadingDialog)))
        if isinstance(dialog, StartupLoadingDialog):
            assert dialog.isEnabled()
            assert not parent.isEnabled()
            released.set()
    timer = QTimer()
    timer.timeout.connect(heartbeat)
    timer.start(10)
    try:
        run_progress_task(task, message="test", parent=parent)
        assert parent.isEnabled()
        assert any(elapsed < 0.9 and not visible for elapsed, visible in observations)
        first_show = next(elapsed for elapsed, visible in observations if visible)
        assert first_show >= 1.0
    finally:
        timer.stop()
        released.set()
        parent.close()
        parent.deleteLater()


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("after", ["stay", "new_dialog", "close"])
def test_common_progress_restores_work_window_and_preserves_followup_dialog(app, monkeypatch, fail, after):
    from PySide6.QtWidgets import QDialog, QLineEdit, QVBoxLayout, QWidget
    from src.desktop_v2 import startup_loading as loading
    monkeypatch.setattr(loading, "PROGRESS_DELAY_MS", 10)
    main = QWidget()
    main.show()
    source = QDialog(main)
    layout = QVBoxLayout(source)
    editor = QLineEdit("입력 중인 값")
    layout.addWidget(editor)
    source.show()
    source.activateWindow()
    editor.setFocus()
    editor.selectAll()
    app.processEvents()
    activations = []
    original_activate = source.activateWindow
    def activate():
        activations.append(True)
        original_activate()
    monkeypatch.setattr(source, "activateWindow", activate)
    released = Event()
    timer = QTimer()
    timer.timeout.connect(lambda: released.set() if isinstance(app.activeModalWidget(), StartupLoadingDialog) else None)
    timer.start(5)
    def work():
        assert released.wait(5)
        if fail:
            raise ValueError("합성 저장 오류")
    following = None
    try:
        # Even a call incorrectly parented to the main window returns to the active editor.
        if fail:
            with pytest.raises(ValueError, match="합성 저장 오류"):
                run_progress_task(work, message="검증", parent=main)
        else:
            run_progress_task(work, message="검증", parent=main)
        assert len(activations) == 1
        assert editor.selectedText() == "입력 중인 값"
        if after == "new_dialog":
            following = QDialog(source)
            following.show()
        elif after == "close":
            source.close()
        app.processEvents()
        assert len(activations) == (2 if after == "stay" else 1)
    finally:
        timer.stop()
        released.set()
        if following is not None:
            following.close()
        source.close()
        main.close()
