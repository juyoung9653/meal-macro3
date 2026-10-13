# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable,
    VarFileInfo, VarStruct, VSVersionInfo,
)
from src.version import APP_VERSION, WINDOWS_VERSION


holiday_datas = collect_data_files(
    "holidays",
    includes=["locale/ko/LC_MESSAGES/KR.mo"],
)

a = Analysis(
    ["sqlite_v2.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("assets/report_template.xlsx", "assets"),
        ("assets/급식_청구서템플릿.xlsx", "assets"),
        ("assets/급식_확인서템플릿.xlsx", "assets"),
        ("assets/행복도시락_청구서템플릿.xlsx", "assets"),
        ("assets/행복도시락_확인서템플릿.xlsx", "assets"),
    ] + holiday_datas,
    hiddenimports=["holidays.countries.south_korea"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="meal_delivery",
    version=VSVersionInfo(
        ffi=FixedFileInfo(filevers=WINDOWS_VERSION, prodvers=WINDOWS_VERSION),
        kids=[
            StringFileInfo([StringTable("041204B0", [
                StringStruct("FileDescription", "급식 배송 운영"),
                StringStruct("ProductName", "급식 배송 운영"),
                StringStruct("FileVersion", APP_VERSION),
                StringStruct("ProductVersion", APP_VERSION),
                StringStruct("OriginalFilename", "meal_delivery.exe"),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0412, 1200])]),
        ],
    ),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
