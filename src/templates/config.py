"""템플릿 설정 — 명단→청구서 컬럼 대응"""

import functools

import openpyxl
from openpyxl.cell.cell import MergedCell

from ..core.settings import get_settings

# ── 설정에서 값 조회 (설정 없으면 기본값 fallback) ──


def get_template_sheet_name(is_vacation: bool = False, meal_type: str = "급식") -> str:
    """청구서 템플릿 시트명 반환.

    급식 → "청구서", 행복도시락 → "학기_총괄" (방학이면 "방학_총괄").
    """
    if meal_type == "급식":
        return "청구서"
    base = get_settings().get("templates", "sheet_name", default="학기_총괄")
    if not is_vacation:
        return base
    # prefix 치환: 학기_ → 방학_
    if base.startswith("학기_"):
        return "방학_" + base[3:]
    return base


def get_template_data_start_row():
    return get_settings().get("templates", "data_start_row", default=4)


def _get_semantic_column_roles():
    roles = get_settings().get("semantic_columns", default={})
    if not roles:
        raise RuntimeError("semantic_columns not configured in settings")
    return roles


def auto_detect_semantic_columns(template_columns: list[str]) -> dict[str, str]:
    """템플릿 컬럼명 목록에서 의미론적 역할 자동 탐지.

    Returns:
        {"__calc_breakfast": "조식", "__calc_dinner": "석식", ...}
        매칭 실패 시 default 값 fallback.
    """
    roles = _get_semantic_column_roles()

    def find(patterns: list[str]) -> str:
        for col in template_columns:
            if col in patterns:
                return col
        for col in template_columns:
            for p in patterns:
                if p in col:
                    return col
        return ""

    result: dict[str, str] = {}
    for key, info in roles.items():
        found = find(info.get("patterns", []))
        result[key] = found or info.get("default", "")
    return result


@functools.lru_cache(maxsize=4)
def read_template_columns(
    template_path: str, sheet_name: str | None = None, meal_type: str = "급식"
) -> list[str]:
    """템플릿 파일 Row 1-2 헤더를 읽어 컬럼명 목록 반환.

    머지된 셀 처리, 개행 제거, 괄호 접미사 제거 자동 처리.
    sheet_name이 None이면 get_template_sheet_name(meal_type=meal_type) 사용.
    """
    if sheet_name is None:
        sheet_name = get_template_sheet_name(meal_type=meal_type)
    wb = openpyxl.load_workbook(template_path)
    try:
        ws = wb[sheet_name]

        columns: list[str] = []
        for col in range(1, ws.max_column + 1):
            c1 = ws.cell(row=1, column=col)
            c2 = ws.cell(row=2, column=col)

            name_r2 = str(c2.value or "").strip()

            if isinstance(c1, MergedCell):
                if name_r2:
                    columns.append(name_r2)
                continue

            name_r1 = str(c1.value or "").strip()

            if name_r2:
                columns.append(name_r2)
            elif name_r1:
                name = name_r1.replace("\n", "")
                for suffix in ["(급식수)", "(원)"]:
                    name = name.removesuffix(suffix)
                columns.append(name)

        return columns
    finally:
        wb.close()


def col_name_to_index(template_columns: list[str]) -> dict[str, int]:
    """컬럼명 → 1-based 컬럼 인덱스 dict (한 번만 계산)."""
    return {name: i + 1 for i, name in enumerate(template_columns)}
