"""First failure is quiet, persisted failure is retried and reported on reopen."""
import json
from uuid import uuid4

import pytest

from src.storage.backup_diagnostics import BackupDiagnostics
from src.storage.database_session import DatabaseSession
from tests.test_encrypted_database import _roster_snapshot, _roster_record


def fail_backup(*args, **kwargs):
    raise OSError("test backup folder unavailable")


@pytest.mark.parametrize("retry_fails", [False, True])
def test_commit_survives_failure_and_reopen_retries(tmp_path, monkeypatch, retry_fails):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    path = tmp_path / "source.sqlite3"
    folder = tmp_path / "backups"
    folder.mkdir()
    roster = _roster_snapshot(uuid4())
    with DatabaseSession.create(path, "password", backup_directory=folder) as session:
        with monkeypatch.context() as patch:
            patch.setattr(session._backup_service, "create_backup", fail_backup)
            result = session.append_revision(_roster_record(roster))
            assert result.error and not result.warning_required
            assert session.database.current_roster() == roster
            assert not session.try_backup_now().warning_required
            log = result.log_path
            events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            assert events[-1]["error_type"] == "OSError"
            assert "test backup folder unavailable" in events[-1]["traceback"]
            assert events[-1]["event"] == "backup_failed"
    with DatabaseSession.open(path, "password", backup_directory=folder) as session:
        assert session._backup_diagnostics.pending_from_previous_session
        if retry_fails:
            monkeypatch.setattr(session._backup_service, "create_backup", fail_backup)
        result = session.retry_backup_on_open()
        assert result.warning_required is retry_fails
        assert session.retry_backup_on_open() is result
        assert session.database.current_roster() == roster
        events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert events[-1]["event"] == ("startup_retry_failed" if retry_fails else "backup_recovered")
    if not retry_fails:
        with DatabaseSession.open(path, "password", backup_directory=folder) as session:
            assert not session._backup_diagnostics.pending_from_previous_session
        with DatabaseSession.restore_and_open_with_user_password(result.record.path, tmp_path / "restored.sqlite3", "password") as restored:
            assert restored.database.current_roster() == roster


def test_first_failure_at_startup_waits_until_next_restart(tmp_path, monkeypatch):
    folder = tmp_path / "backups"
    folder.mkdir()
    path = tmp_path / "source.sqlite3"
    with DatabaseSession.create(path, "password", backup_directory=folder) as session:
        monkeypatch.setattr(session._backup_service, "create_backup", fail_backup)
        assert not session.retry_backup_on_open().warning_required
    with DatabaseSession.open(path, "password", backup_directory=folder) as session:
        monkeypatch.setattr(session._backup_service, "create_backup", fail_backup)
        assert session.retry_backup_on_open().warning_required


def test_log_falls_back_to_this_pc_when_database_folder_is_unwritable(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    (tmp_path / "logs").write_text("blocks log directory", encoding="utf-8")
    identifier = uuid4()
    diagnostics = BackupDiagnostics(tmp_path / "source.sqlite3", identifier)
    path = diagnostics.failed(OSError("share offline"), tmp_path / "backups")
    assert path.is_relative_to(tmp_path / "local")
    reopened = BackupDiagnostics(diagnostics.database_path, identifier)
    assert reopened.pending_from_previous_session
    diagnostics.succeeded()
    assert not diagnostics._pending(path.parent)
    assert "backup_recovered" in path.read_text(encoding="utf-8")


def test_diagnostics_failure_never_overrides_database_result(tmp_path):
    diagnostics = BackupDiagnostics(tmp_path / "source.sqlite3", uuid4())
    blocked = tmp_path / "blocked"
    blocked.write_text("file", encoding="utf-8")
    diagnostics.directories = (blocked, blocked)
    assert diagnostics.failed(OSError("offline"), tmp_path) is None
    diagnostics.succeeded()


def test_bad_state_file_does_not_prevent_open(tmp_path):
    identifier = uuid4()
    diagnostics = BackupDiagnostics(tmp_path / "source.sqlite3", identifier)
    directory = diagnostics.directories[0]
    directory.mkdir()
    (directory / (diagnostics.name + ".state.json")).write_text("[]", encoding="utf-8")
    assert not BackupDiagnostics(diagnostics.database_path, identifier).pending_from_previous_session
