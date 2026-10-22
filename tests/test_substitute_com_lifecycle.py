"""Excel ownership, apartment lifetime and native disconnect regressions."""
import subprocess
import sys
import threading
import types
import weakref
from pathlib import Path

import pytest

from src.excel import substitute_excel_metrics as metrics


@pytest.mark.parametrize("failure_stage", [None, "measure", "close", "quit"])
def test_owned_worker_releases_proxies_before_uninitializing(monkeypatch, failure_stage):
    import win32com.client  # Load its constants before installing the COM fake.
    caller = threading.get_ident()
    events, references = [], []

    def event(name):
        events.append((name, threading.get_ident()))
        if failure_stage == name:
            raise ValueError(name)

    class Proxy:
        def __init__(self):
            references.append(weakref.ref(self))

    class Book(Proxy):
        def Worksheets(self, index):
            return Proxy()

        def Close(self, **kwargs):
            assert kwargs == {"SaveChanges": False}
            event("close")

    class Application(Proxy):
        @property
        def Workbooks(self):
            return types.SimpleNamespace(Add=Book)

        def Quit(self):
            event("quit")

    def uninitialize():
        event("uninit")
        assert all(reference() is None for reference in references)

    monkeypatch.setitem(sys.modules, "pythoncom", types.SimpleNamespace(
        CoInitialize=lambda: event("init"), CoUninitialize=uninitialize))
    monkeypatch.setattr(metrics, "_create_excel_application", Application)

    def measure(*args):
        proxy = Proxy()
        proxy.cycle = proxy  # COM wrappers can contain cached bound methods.
        event("measure")

    monkeypatch.setattr(metrics, "_measure", measure)
    if failure_stage:
        with pytest.raises(metrics.SubstituteExcelError) as error:
            metrics.apply_excel_metrics(None, [], None, None)
        assert str(error.value.__cause__) == failure_stage
    else:
        metrics.apply_excel_metrics(None, [], None, None)
    assert [name for name, _ in events] == ["init", "measure", "close", "quit", "uninit"]
    assert len({thread for _, thread in events}) == 1
    assert all(thread != caller for _, thread in events)


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows Excel integration")
def test_repeated_native_exports_exit_without_com_disconnect():
    # A separate interpreter exposes native first-chance exception output even
    # when pytest captures it, and bounds an actual COM hang to 90 seconds.
    # Track only PIDs created by this test; never terminate unrelated Excel.
    script = r'''
import gc
import pythoncom
import win32api
import win32con
import win32event
import win32process
from tests.test_substitute_legacy_parity import synthetic
from src.domain.delivery_substitute import SubstituteOptions
from src.generators.substitute_generator import build_substitute_workbook
from src.excel import substitute_excel_metrics as metrics
factory = metrics._create_excel_application
handles = []
def tracked_factory():
    app = factory()
    pid = win32process.GetWindowThreadProcessId(app.Hwnd)[1]
    handles.append(win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid))
    return app
metrics._create_excel_application = tracked_factory
try:
    for _ in range(2):
        workbook = build_substitute_workbook(synthetic((1, 2)), SubstituteOptions("synthetic", ("name", "road_address")))
        assert workbook.active["A3"].value
        workbook.close()
    gc.collect()
    for handle in handles:
        assert win32event.WaitForSingleObject(handle, 10000) == win32event.WAIT_OBJECT_0, "Owned Excel did not exit"
finally:
    for handle in handles:
        handle.Close()
print("native exports complete")
'''
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script],
        cwd=Path(__file__).resolve().parents[1], capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "native exports complete" in result.stdout
    assert "Windows fatal exception" not in result.stderr, result.stderr
