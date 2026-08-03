"""Atomic publication helpers for generated Excel workbooks."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any


def publish_workbook(workbook: Any, target: str | Path) -> None:
    """Save ``workbook`` to a staging file, then atomically publish it.

    The staging file is created alongside ``target`` so ``os.replace`` stays on
    the same filesystem.  The previous target remains untouched unless both
    save and close complete successfully.
    """
    target_path = Path(target)
    staging_path: Path | None = None

    try:
        try:
            descriptor, staging_name = tempfile.mkstemp(
                prefix=f".{target_path.name}.",
                suffix=target_path.suffix,
                dir=target_path.parent,
            )
            staging_path = Path(staging_name)
            os.close(descriptor)
            workbook.save(staging_path)
        finally:
            workbook.close()

        os.replace(staging_path, target_path)
    finally:
        if staging_path is not None:
            staging_path.unlink(missing_ok=True)
