"""Retained dialog continuations driven by Qt's normal application event loop.

Controllers retain a blocking API for startup and synchronous callers. Desktop
signal handlers explicitly use ``launch``; no nested dialog event loop is entered.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from functools import wraps
import logging
import weakref

from PySide6.QtCore import QEvent, QObject, QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox, QWidget
from shiboken6 import isValid


@dataclass
class DialogRequest:
    dialog: QDialog
    session: object = None
    revision_id: object = field(default=None, init=False)

    def _revision_id(self):
        if self.session is None:
            return None
        latest = self.session.database.latest_revision()
        return None if latest is None else latest.metadata.revision_id

    def capture_revision(self):
        # Blocking startup callers retain their existing dependency contract.
        self.revision_id = self._revision_id()

    def unchanged(self):
        return self._revision_id() == self.revision_id


def dialog_workflow(function):
    """Expose generator steps while retaining the controller's blocking API."""
    @wraps(function)
    def blocking(*args, **kwargs):
        steps = function(*args, **kwargs)
        result = None
        try:
            while True:
                try:
                    request = steps.send(result)
                except StopIteration as done:
                    return done.value
                result = request.dialog.exec()
        finally:
            steps.close()
    blocking.steps = function
    return blocking


def invoke(method, *args, **kwargs):
    """Delegate to steps; injected synchronous controller doubles still work."""
    steps = getattr(method, "steps", None)
    if steps is None:
        return method(*args, **kwargs)
    owner = getattr(method, "__self__", None)
    if owner is None:
        return (yield from steps(*args, **kwargs))
    return (yield from steps(owner, *args, **kwargs))


def launch(method, *args, **kwargs):
    """Return immediately with a retained workflow owned by the main window."""
    widgets = [value for value in (getattr(method, "__self__", None), *args, *kwargs.values())
               if isinstance(value, QWidget)]
    if not widgets:
        return method(*args, **kwargs)
    context = owner = widgets[0]
    while owner.parentWidget() is not None:
        owner = owner.parentWidget()
    run = DialogWorkflow(owner, invoke(method, *args, **kwargs), context=context)
    run._manager.submit(run.advance)
    return run


def _show_work_dialog(dialog):
    """Show a retained dialog without making its main window a native owner."""
    if isinstance(dialog, (QMessageBox, QFileDialog)):
        dialog.show()
        return
    # Polish may change window flags (including restored title-bar buttons).
    # Do that before detaching; otherwise Qt recreates the native owner on show.
    dialog.ensurePolished()
    dialog.winId()
    handle = dialog.windowHandle()
    if handle is not None:
        handle.setTransientParent(None)
    _configure_taskbar_window(dialog)
    dialog.show()
    # Keep the QWidget parent for lifetime management and findChildren(). Qt's
    # transientParent alone does not clear the existing Windows GW_OWNER.
    handle = dialog.windowHandle()
    if handle is not None:
        handle.setTransientParent(None)
    _configure_taskbar_window(dialog)


def _configure_taskbar_window(dialog):
    if QApplication.platformName() != "windows":
        return
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
    get_long.restype = ctypes.c_ssize_t
    set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    set_long.restype = ctypes.c_ssize_t
    hwnd = int(dialog.winId())

    def set_value(index, value):
        if get_long(hwnd, index) == value:
            return
        ctypes.set_last_error(0)
        previous = set_long(hwnd, index, value)
        if not previous and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())

    set_value(-8, 0)  # GWLP_HWNDPARENT: native owner, not QWidget lifetime parent.
    style = get_long(hwnd, -20)  # GWL_EXSTYLE
    desired = (style | 0x00040000) & ~0x00000080  # APPWINDOW, without TOOLWINDOW
    if style != desired:
        set_value(-20, desired)
        user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SetWindowPos.restype = ctypes.c_int
        # Refresh cached styles without moving, resizing or activating the window.
        if not user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0037):
            raise ctypes.WinError(ctypes.get_last_error())


class _DialogManager(QObject):
    """One event filter per main window; completed workflows are plain Python."""
    def __init__(self, owner):
        super().__init__(owner)
        self.workflows = set()
        self._pending = deque()
        self._busy = False
        self._scheduled = False
        owner._dialog_workflows = self.workflows
        owner.installEventFilter(self)

    def submit(self, callback, *args, defer=False):
        self._pending.append((callback, args))
        if self._busy or self._scheduled:
            return
        if defer:
            self._scheduled = True
            QTimer.singleShot(0, self._drain)
        else:
            self._drain()

    def _drain(self):
        if self._busy:
            return
        self._scheduled = False
        self._busy = True
        try:
            # Progress tasks process Qt events while they run. New accepted
            # dialogs wait here until the current save and refresh both finish.
            while self._pending:
                callback, args = self._pending.popleft()
                callback(*args)
        finally:
            self._busy = False

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Close:
            self._pending.clear()
            for workflow in tuple(self.workflows):
                workflow.cancel()
        return False


class DialogWorkflow:
    def __init__(self, owner, steps, *, context=None):
        self._owner = weakref.ref(owner)
        self.steps = steps
        self.request = None
        self.closed = False
        self.error = None
        self._advancing = False
        self._dialogs = []
        self._finished_connected = False
        self._context = context if isinstance(context, QDialog) else None
        manager = getattr(owner, "_dialog_manager", None)
        if manager is None:
            manager = owner._dialog_manager = _DialogManager(owner)
        self._manager = manager
        manager.workflows.add(self)
        if self._context is not None:
            self._context.finished.connect(self.cancel)

    @property
    def owner(self):
        return self._owner()

    def cancel(self, *_args):
        if self.closed:
            return
        self.closed = True
        if not self._advancing:
            self.steps.close()
        self._manager.workflows.discard(self)
        if self._context is not None and isValid(self._context):
            self._context.finished.disconnect(self.cancel)
        for dialog in self._dialogs:
            if isValid(dialog):
                if self._finished_connected and self.request is not None and dialog is self.request.dialog:
                    dialog.finished.disconnect(self._finished)
                    self._finished_connected = False
                dialog.close()
                dialog.deleteLater()
        self._dialogs.clear()
        self.request = None

    def advance(self, result=None):
        if self.closed:
            return
        self._advancing = True
        try:
            self.request = self.steps.send(result)
            if self.closed:
                return
            self.request.capture_revision()
            dialog = self.request.dialog
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
            dialog.setWindowModality(Qt.WindowModality.NonModal)
            dialog.setModal(False)
            self._dialogs.append(dialog)
            dialog.finished.connect(self._finished)
            self._finished_connected = True
            _show_work_dialog(dialog)
        except StopIteration:
            self.cancel()
        except Exception as error:
            self.error = error
            logging.getLogger(__name__).error("Desktop dialog workflow failed (%s)", type(error).__name__)
            self.cancel()
            owner = self.owner
            if owner is not None and isValid(owner):
                QMessageBox.critical(owner, "창 작업을 완료하지 못했습니다",
                    "작업 중 오류가 발생했습니다. 현재 명단을 확인한 뒤 창을 다시 열어주세요.")
        finally:
            self._advancing = False
            if self.closed:
                self.steps.close()

    def _finished(self, result):
        if self.closed:
            return
        request = self.request
        request.dialog.finished.disconnect(self._finished)
        self._finished_connected = False
        # A save may run a progress event loop. Resume after Qt's accept()/done()
        # stack unwinds so it cannot delete its own dialog while emitting finished.
        self._manager.submit(self._resume_finished, request, result, defer=True)

    def _resume_finished(self, request, result):
        owner = self.owner
        if self.closed or owner is None or not isValid(owner):
            return
        if result == QDialog.DialogCode.Accepted and request.session is not None:
            try:
                unchanged = request.unchanged()
            except Exception as error:
                self.error = error
                unchanged = False
            if not unchanged:
                request.dialog.finished.connect(self._finished)
                self._finished_connected = True
                _show_work_dialog(request.dialog)
                QMessageBox.warning(request.dialog, "다른 창에서 명단이 변경되었습니다",
                    "이 창을 연 뒤 명단 또는 설정이 변경되어 저장하지 않았습니다.\n"
                    "입력 내용은 이 창에 남아 있습니다. 필요한 내용을 복사한 뒤 취소하고 다시 열어주세요.")
                return
        self.advance(result)
