"""Retention is month-based, opt-out capable and confined to output files."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from datetime import datetime, timezone
from pathlib import Path
import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog, QWidget
from src.services.output_retention import cleanup_output_files, retention_cutoff
from src.desktop_v2.portable_settings import DesktopStorageSettings
from src.desktop_v2.output_retention import OutputRetentionDialog, OutputCleanupScheduler


def make_file(path, when):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("test", encoding="utf-8")
    os.utime(path, (when.timestamp(), when.timestamp()))
    return path


def test_month_boundary_recursive_files_and_disabled_cleanup(tmp_path):
    now = datetime(2026, 3, 31, 12, tzinfo=timezone.utc)
    cutoff = datetime(2026, 2, 28, 12, tzinfo=timezone.utc)
    assert retention_cutoff(now, 1) == cutoff
    old = make_file(tmp_path / "output/sub/old.xlsx", cutoff)
    fresh = make_file(tmp_path / "output/new.xlsx", now)
    outside = make_file(tmp_path / "elsewhere/old.xlsx", cutoff)
    assert cleanup_output_files(tmp_path, 0, now=now).deleted == ()
    assert old.exists()
    result = cleanup_output_files(tmp_path, 1, now=now)
    assert result.deleted == (old,) and result.failed == ()
    assert fresh.exists() and outside.exists() and old.parent.is_dir()
    assert cleanup_output_files(tmp_path, 1, now=now).deleted == ()


def test_linked_directories_and_delete_failures_are_preserved(tmp_path, monkeypatch):
    from src.services import output_retention as module
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    protected = make_file(tmp_path / "output/link/external.xlsx", old)
    locked = make_file(tmp_path / "output/locked.xlsx", old)
    normal = make_file(tmp_path / "output/old.xlsx", old)
    original_linked = module._linked
    monkeypatch.setattr(module, "_linked", lambda path: path.name == "link" or original_linked(path))
    original_unlink = Path.unlink
    def unlink(path, *args, **kwargs):
        if path == locked:
            raise PermissionError("locked")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    result = cleanup_output_files(tmp_path, 1, now=now)
    assert result.deleted == (normal,) and result.failed == (locked,)
    assert protected.exists() and locked.exists()
    monkeypatch.setattr(module, "_linked", lambda path: path.name == "output")
    assert cleanup_output_files(tmp_path, 1, now=now).deleted == ()


def test_retention_settings_cancel_save_reopen_and_scheduler(tmp_path):
    app = QApplication.instance() or QApplication([])
    ini = tmp_path / "settings.ini"
    settings = DesktopStorageSettings(QSettings(str(ini), QSettings.Format.IniFormat), base_directory=tmp_path)
    assert settings.output_retention_months == 1
    dialog = OutputRetentionDialog(settings)
    dialog.months.setValue(3)
    dialog.reject()
    assert settings.output_retention_months == 1
    dialog._save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    reopened = DesktopStorageSettings(QSettings(str(ini), QSettings.Format.IniFormat), base_directory=tmp_path)
    assert reopened.output_retention_months == 3
    old = make_file(tmp_path / "output/expired.xlsx", datetime(2000, 1, 1, tzinfo=timezone.utc))
    owner = QWidget()
    scheduler = OutputCleanupScheduler(reopened, owner)
    scheduler.check()
    assert scheduler._future.result(timeout=5).deleted == (old,)
    assert scheduler.timer.interval() == 3600000
    scheduler.stop()
    owner.close()
    settings.save_output_retention_months(0)
    assert settings.output_retention_months == 0
    with pytest.raises(ValueError):
        settings.save_output_retention_months(-1)
