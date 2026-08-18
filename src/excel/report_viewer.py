"""배송 리포트를 임시 Excel 파일로 생성해 실행한다."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from src.generators.report_generator import export_multi
from src.services.delivery_service import DeliveryReport

log = logging.getLogger(__name__)


def open_excel(filepath: str | Path) -> None:
    """Excel 연결 프로그램으로 기존 리포트 파일을 연다."""
    os.startfile(str(filepath))


def show_excel(results: Sequence[DeliveryReport]) -> str:
    """리포트를 임시 파일로 생성해 열고 생성 경로를 반환한다."""
    if not results:
        raise ValueError("출력할 리포트 결과가 없습니다.")

    first_day = results[0].day
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f"배송리포트_{first_day:%Y%m%d}_",
        suffix=".xlsx",
    )
    os.close(descriptor)
    temp_path = Path(temp_name)

    try:
        export_multi(results, temp_path)
        if not temp_path.is_file() or temp_path.stat().st_size == 0:
            raise FileNotFoundError(f"리포트 파일이 생성되지 않았습니다: {temp_path}")
        open_excel(temp_path)
    except Exception:
        log.exception("Failed to create/open report excel file")
        temp_path.unlink(missing_ok=True)
        raise

    return str(temp_path)
