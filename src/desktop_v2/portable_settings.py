"""Shared path settings beside the executable; personal UI settings stay local."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from PySide6.QtCore import QSettings
from src.storage.file_lock import ExclusiveFileLock

ORGANIZATION = "분당구"
APPLICATION = "급식 배송 운영 SQLite v2"
PATH_KEYS = ("storage/database_path", "storage/backup_directory", "documents/output_directory")


def application_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def application_lock(base: Path | None = None) -> ExclusiveFileLock:
    directory = (base or application_directory()) / "data"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise OSError("프로그램 설정 폴더에 접근할 수 없습니다. 연결과 쓰기 권한을 확인해주세요.") from error
    return ExclusiveFileLock(directory / "app.lock")


class DesktopStorageSettings:
    """Explicit QSettings injection preserves test/embedded callers' isolation."""

    def __init__(self, settings: QSettings | None = None, *, base_directory: Path | None = None,
                 legacy_settings: QSettings | None = None, app_lock: ExclusiveFileLock | None = None) -> None:
        self.base_directory = Path(base_directory or application_directory()).resolve()
        self.portable = settings is None
        if settings is not None:
            self._settings = settings
            return
        path = self.base_directory / "data" / "settings.ini"
        # Migration is only allowed under the application lock. An existing INI
        # is authoritative, including an empty file or missing path values.
        if not path.exists():
            if app_lock is None or not app_lock.acquired or app_lock.path != path.parent / "app.lock":
                raise OSError("공통 설정을 초기화하려면 프로그램 잠금이 필요합니다.")
            path.parent.mkdir(parents=True, exist_ok=True)
            legacy = legacy_settings if legacy_settings is not None else QSettings(ORGANIZATION, APPLICATION)
            values: dict[str, object] = {}
            for key in PATH_KEYS:
                if legacy.contains(key):
                    value = legacy.value(key, "")
                    values[key] = self._stored_path(value) if isinstance(value, str) and value.strip() else ""
            values["portable/initialized"] = True
            self._initialize(path, values)
        self._settings = QSettings(str(path), QSettings.Format.IniFormat)
        self._settings.sync()
        if self._settings.status() != QSettings.Status.NoError:
            raise OSError("공통 설정 파일을 읽을 수 없습니다. 연결과 권한을 확인해주세요.")

    @staticmethod
    def _initialize(path: Path, values: dict[str, object]) -> None:
        # Publish a complete INI only. A failed first write must not leave an
        # authoritative empty file that would suppress migration next time.
        descriptor, name = tempfile.mkstemp(prefix=".settings-", suffix=".ini", dir=path.parent)
        os.close(descriptor)
        temporary = None
        try:
            temporary = QSettings(name, QSettings.Format.IniFormat)
            for key, value in values.items():
                temporary.setValue(key, value)
            temporary.sync()
            if temporary.status() != QSettings.Status.NoError:
                raise OSError("공통 설정을 초기화하지 못했습니다. 연결과 쓰기 권한을 확인해주세요.")
            temporary = None  # Dispose QSettings before publishing/deleting its file.
            if os.name == "nt":
                os.rename(name, path)  # Windows rename refuses an existing target.
            else:
                os.link(name, path)  # Publish without replacing another file.
        finally:
            temporary = None
            Path(name).unlink(missing_ok=True)

    def _path(self, key: str) -> Path | None:
        value = self._settings.value(key, "")
        if not isinstance(value, str) or not value.strip():
            return None
        path = Path(value)
        return ((self.base_directory / path) if self.portable and not path.is_absolute() else path).resolve()

    def _stored_path(self, value: str | Path) -> str:
        path = Path(value)
        path = ((self.base_directory / path) if self.portable and not path.is_absolute() else path).resolve()
        if self.portable:
            try:
                return path.relative_to(self.base_directory).as_posix()
            except ValueError:
                pass
        return str(path)

    @property
    def database_path(self) -> Path | None:
        return self._path(PATH_KEYS[0])

    @property
    def backup_directory(self) -> Path | None:
        return self._path(PATH_KEYS[1])

    @property
    def document_output_directory(self) -> Path | None:
        return self._path(PATH_KEYS[2])

    def _save(self, values: dict[str, object]) -> None:
        previous = {key: (self._settings.contains(key), self._settings.value(key)) for key in values}
        try:
            for key, value in values.items():
                self._settings.setValue(key, value)
            self._settings.sync()
            if self._settings.status() != QSettings.Status.NoError:
                raise OSError("공통 경로 설정을 저장하지 못했습니다. 폴더 연결과 쓰기 권한을 확인해주세요.")
        except Exception:
            for key, (existed, value) in previous.items():
                if existed:
                    self._settings.setValue(key, value)
                else:
                    self._settings.remove(key)
            self._settings.sync()
            raise

    def save_paths(self, database_path: str | Path, backup_directory: str | Path) -> None:
        self._save({PATH_KEYS[0]: self._stored_path(database_path), PATH_KEYS[1]: self._stored_path(backup_directory)})

    def save_database_path(self, database_path: str | Path) -> None:
        self._save({PATH_KEYS[0]: self._stored_path(database_path)})

    def save_backup_directory(self, directory: str | Path) -> None:
        self._save({PATH_KEYS[1]: self._stored_path(directory)})

    def save_document_output_directory(self, directory: str | Path) -> None:
        self._save({PATH_KEYS[2]: self._stored_path(directory)})
