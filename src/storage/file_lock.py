"""Process-lifetime exclusive handles, including Windows/SMB share denial."""
from __future__ import annotations

import os
from pathlib import Path


class FileLockError(OSError):
    """The lock is busy or its location is inaccessible."""


class ExclusiveFileLock:
    """Keep the sidecar on disk; only the operating-system handle owns the lock."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self._handle = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> ExclusiveFileLock:
        if self.acquired:
            return self
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            create = kernel.CreateFileW
            create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            create.restype = wintypes.HANDLE
            handle = create(str(self.path), 0x80000000 | 0x40000000, 0, None, 4, 0x80, None)
            if handle == ctypes.c_void_p(-1).value:
                code = ctypes.get_last_error()
                if code in (32, 33):
                    raise FileLockError("다른 컴퓨터 또는 창에서 사용 중입니다. 사용 중인 프로그램을 닫은 뒤 다시 실행해주세요.")
                raise FileLockError(f"잠금 파일을 열 수 없습니다. 폴더 연결과 쓰기 권한을 확인해주세요. (오류 {code})")
            self._handle = handle
        else:
            import fcntl
            try:
                handle = open(self.path, "a+b")
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BaseException:
                    handle.close()
                    raise
            except OSError as error:
                raise FileLockError("다른 프로그램에서 사용 중이거나 잠금 폴더에 접근할 수 없습니다.") from error
            self._handle = handle
        return self

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL
            close(handle)
        else:
            handle.close()

    def __enter__(self) -> ExclusiveFileLock:
        return self.acquire()

    def __exit__(self, *_args) -> None:
        self.close()


def database_lock(path: str | Path) -> ExclusiveFileLock:
    return ExclusiveFileLock(str(Path(path).resolve()) + ".lock")
