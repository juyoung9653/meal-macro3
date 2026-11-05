# -*- mode: python ; coding: utf-8 -*-
"""Standalone console tool; Python is bundled and no app assets are required."""
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable,
    VarFileInfo, VarStruct, VSVersionInfo,
)
from src.version import APP_VERSION, WINDOWS_VERSION

a = Analysis(
    ["scripts/cleanup_september30_stops.py"],
    pathex=[SPECPATH],
    binaries=[],
    datas=collect_data_files("holidays", includes=["locale/ko/LC_MESSAGES/KR.mo"]),
    hiddenimports=["holidays.countries.south_korea"],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="meal_stop_cleanup",
    version=VSVersionInfo(
        ffi=FixedFileInfo(filevers=WINDOWS_VERSION, prodvers=WINDOWS_VERSION),
        kids=[
            StringFileInfo([StringTable("041204B0", [
                StringStruct("FileDescription", "중지 식종 미배송 정리"),
                StringStruct("ProductName", "급식 배송 운영 정리 도구"),
                StringStruct("FileVersion", APP_VERSION),
                StringStruct("ProductVersion", APP_VERSION),
                StringStruct("OriginalFilename", "meal_stop_cleanup.exe"),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0412, 1200])]),
        ],
    ),
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    upx_exclude=[], runtime_tmpdir=None, console=True,
    disable_windowed_traceback=False,
)
