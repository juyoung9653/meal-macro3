"""Path resolution for PyInstaller --onefile builds."""

import os
import sys


def asset_path(relative: str) -> str:
    """Resolve 'assets/...' path.

    Priority in frozen build:
    1. ``<exe-dir>/assets/...`` (user-side override)
    2. ``<tmp>/assets/...`` (bundled fallback)

    In development, returns ``relative`` as-is.
    """
    if not getattr(sys, "frozen", False):
        return relative

    # 1) exe 옆 assets/ 우선
    exe_dir = os.path.dirname(sys.executable)
    external = os.path.join(exe_dir, relative)
    if os.path.exists(external):
        return external

    # 2) 내장 번들 (tmp)
    return os.path.join(sys._MEIPASS, relative)
