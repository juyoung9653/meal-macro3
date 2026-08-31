"""Smoke-test the proposed Python 3.14 desktop deployment stack.

This deliberately uses a fixed, non-secret key because the database is
temporary test data. Production key creation and wrapping are outside the
scope of this packaging spike.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import apsw
import PySide6
from PySide6.QtCore import qVersion
from PySide6.QtWidgets import QApplication, QWidget


_CIPHER = "chacha20"
_TEST_KEY = bytes.fromhex(
    "00112233445566778899aabbccddeeff"
    "ffeeddccbbaa99887766554433221100"
)
_WRONG_KEY = bytes([_TEST_KEY[0] ^ 0xFF]) + _TEST_KEY[1:]
_MARKER = "sqlite-desktop-v2"


class SmokeFailure(RuntimeError):
    """Raised when a packaging-spike invariant is not satisfied."""


def _configure_encryption(connection: apsw.Connection, key: bytes) -> None:
    connection.pragma("cipher", _CIPHER)
    connection.pragma("hexkey", key.hex())


def _create_encrypted_database(path: Path) -> None:
    connection = apsw.Connection(str(path))
    try:
        _configure_encryption(connection, _TEST_KEY)
        connection.execute("CREATE TABLE smoke_check (marker TEXT NOT NULL)")
        connection.execute("INSERT INTO smoke_check VALUES (?)", (_MARKER,))
    finally:
        connection.close()


def _read_marker(path: Path, key: bytes) -> str:
    connection = apsw.Connection(str(path))
    try:
        _configure_encryption(connection, key)
        row = connection.execute("SELECT marker FROM smoke_check").fetchone()
    finally:
        connection.close()
    if row is None:
        raise SmokeFailure("encrypted database did not contain the marker row")
    return str(row[0])


def _rejects_plain_sqlite(path: Path) -> bool:
    connection = sqlite3.connect(path)
    try:
        connection.execute("SELECT marker FROM smoke_check").fetchone()
    except sqlite3.DatabaseError:
        return True
    finally:
        connection.close()
    return False


def _rejects_wrong_key(path: Path) -> bool:
    try:
        _read_marker(path, _WRONG_KEY)
    except apsw.Error:
        return True
    return False


def _exercise_gui() -> str:
    application = QApplication.instance() or QApplication([])
    widget = QWidget()
    widget.setWindowTitle("PySide6 packaging smoke")
    widget.show()
    application.processEvents()
    rendered_title = widget.windowTitle()
    widget.close()
    application.processEvents()
    if rendered_title != "PySide6 packaging smoke":
        raise SmokeFailure("PySide6 widget did not retain its title")
    return qVersion()


def run_smoke() -> dict[str, object]:
    qt_runtime = _exercise_gui()
    with tempfile.TemporaryDirectory(prefix="sqlite-v2-smoke-") as temp_dir:
        database_path = Path(temp_dir) / "encrypted.sqlite3"
        _create_encrypted_database(database_path)

        encrypted_header = not database_path.read_bytes().startswith(
            b"SQLite format 3\x00"
        )
        plain_sqlite_rejected = _rejects_plain_sqlite(database_path)
        wrong_key_rejected = _rejects_wrong_key(database_path)
        correct_key_marker = _read_marker(database_path, _TEST_KEY)

    checks = {
        "gui_widget": True,
        "encrypted_header": encrypted_header,
        "plain_sqlite_rejected": plain_sqlite_rejected,
        "wrong_key_rejected": wrong_key_rejected,
        "correct_key_reopened": correct_key_marker == _MARKER,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise SmokeFailure(f"failed checks: {', '.join(failed)}")

    return {
        "checks": checks,
        "versions": {
            "apsw": apsw.apswversion(),
            "sqlite": apsw.sqlitelibversion(),
            "sqlite3mc": apsw.mc_version,
            "pyside6": PySide6.__version__,
            "qt": qt_runtime,
        },
    }


if __name__ == "__main__":
    print(json.dumps(run_smoke(), ensure_ascii=False, sort_keys=True))
