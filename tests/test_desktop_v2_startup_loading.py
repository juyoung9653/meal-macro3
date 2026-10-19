"""The startup wait modal remains visible while database work runs off-thread."""

from __future__ import annotations

import os
from threading import Event, get_ident

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from src.desktop_v2.startup_loading import StartupLoadingDialog, run_startup_task


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("fail", [False, True])
def test_startup_loading_is_visible_and_responsive_until_task_finishes(app, fail):
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
                run_startup_task(task, message="테스트 운영 데이터를 읽는 중입니다.")
        else:
            assert run_startup_task(
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
