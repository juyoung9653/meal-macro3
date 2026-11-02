"""Persistent automatic-backup failure state and diagnostics, without DB secrets."""
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
import traceback

from .database_credentials import _write_file


class BackupDiagnostics:
    def __init__(self, database_path, database_id):
        self.database_path = Path(database_path)
        key = hashlib.sha256(str(self.database_path).casefold().encode()).hexdigest()[:12]
        self.name = f"backup-{database_id}-{key}"
        local = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "BundangMeals" / "logs"
        self.directories = (self.database_path.parent / "logs", local)
        self.pending_from_previous_session = any(self._pending(directory) for directory in self.directories)

    def _pending(self, directory):
        try:
            return json.loads((directory / (self.name + ".state.json")).read_text(encoding="utf-8")).get("pending") is True
        except (OSError, ValueError, AttributeError):
            return False

    def failed(self, error, backup_directory, *, startup_retry=False):
        timestamp = datetime.now(UTC).isoformat()
        state = {"pending": True, "failed_at": timestamp}
        event = {"at": timestamp, "event": "startup_retry_failed" if startup_retry else "backup_failed",
                 "database": str(self.database_path), "backup_directory": str(backup_directory),
                 "error_type": type(error).__name__, "error": str(error),
                 "traceback": "".join(traceback.format_exception(type(error), error, error.__traceback__))}
        for directory in self.directories:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                _write_file(directory / (self.name + ".state.json"), json.dumps(state).encode(), replace_existing=True)
                path = directory / (self.name + ".jsonl")
                if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
                    os.replace(path, directory / (self.name + ".previous.jsonl"))
                with path.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(event, ensure_ascii=False) + "\n")
                return path
            except OSError:
                continue
        return None

    def succeeded(self):
        for directory in self.directories:
            state_path = directory / (self.name + ".state.json")
            try:
                if not self._pending(directory):
                    continue
                # Record recovery while retaining the error log for diagnosis.
                with (directory / (self.name + ".jsonl")).open("a", encoding="utf-8") as output:
                    output.write(json.dumps({"at": datetime.now(UTC).isoformat(), "event": "backup_recovered"}) + "\n")
            except OSError:
                pass
            try:
                state_path.unlink(missing_ok=True)
            except OSError:
                pass
