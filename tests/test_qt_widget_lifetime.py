"""Widget helper callbacks must not leave GUI objects for worker-thread GC."""
from __future__ import annotations

import gc
import os
from threading import get_ident
from weakref import ref

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QThread, Qt
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget

from src.desktop_v2.theme import register_application_fonts


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_placeholder_callback_does_not_retain_line_or_its_parent(app):
    register_application_fonts()
    main_thread = get_ident()
    destroyed = []
    parent = QWidget()
    edit = QLineEdit(parent)
    edit.setPlaceholderText("synthetic placeholder")
    parent.show()
    app.processEvents()
    assert edit.findChild(QWidget, "fullPlaceholderText") is not None
    edit.destroyed.connect(lambda *_args: destroyed.append(get_ident()), Qt.ConnectionType.DirectConnection)
    parent_ref, edit_ref = ref(parent), ref(edit)
    parent.close()
    del edit, parent

    # Refcount release must destroy these on the caller's GUI thread. A strong
    # textChanged -> lambda -> QLineEdit cycle instead survives until cyclic GC.
    assert parent_ref() is None
    assert edit_ref() is None
    assert destroyed == [main_thread]


def test_closed_placeholder_window_cannot_be_finalized_by_worker_collection(app):
    register_application_fonts()
    main_thread = get_ident()
    destroyed = []

    def close_window():
        parent = QWidget()
        edit = QLineEdit(parent)
        parent.show()
        app.processEvents()
        edit.destroyed.connect(lambda *_args: destroyed.append(get_ident()), Qt.ConnectionType.DirectConnection)
        parent.close()
        return ref(parent), ref(edit)

    parent_ref, edit_ref = close_window()
    assert parent_ref() is None and edit_ref() is None

    class Collector(QThread):
        def run(self):
            self.worker_thread = get_ident()
            gc.collect()

    collector = Collector()
    collector.start()
    assert collector.wait(5000)
    assert collector.worker_thread != main_thread
    assert destroyed == [main_thread]


def test_failed_progress_task_releases_captured_parent_on_gui_thread(app):
    from src.desktop_v2.startup_loading import run_progress_task
    register_application_fonts()
    gc.collect()
    main_thread = get_ident()
    destroyed, failures = [], []

    def failing_operation():
        parent = QWidget()
        parent.destroyed.connect(lambda *_args: destroyed.append(get_ident()), Qt.ConnectionType.DirectConnection)
        parent_ref = ref(parent)

        def fail(held_parent=parent):
            # Existing data loaders may close over their owning controller/dialog.
            # No widget is accessed by the worker.
            raise OSError("synthetic read failure")

        try:
            run_progress_task(fail, message="synthetic failure", parent=parent)
        except OSError as error:
            failures.append(str(error))
        parent.close()
        return parent_ref

    parent_ref = failing_operation()
    retained_after_handling = parent_ref() is not None
    # Also clean up the unfixed case on the GUI thread before reporting failure.
    gc.collect()
    assert failures == ["synthetic read failure"]
    assert not retained_after_handling
    assert destroyed == [main_thread]
