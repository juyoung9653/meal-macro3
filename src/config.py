"""템플릿 설정 — 명단→청구서 컬럼 대응"""

import functools

import openpyxl

TEMPLATE_SHEET_NAME = "학기중"
TEMPLATE_DATA_START_ROW = 4  # 4행부터 데이터 입력

# ── 의미론적 컬럼 역할 ─────────────────────────────
# auto-detect용 패턴 + GUI 표시용 레이블.
# 템플릿 컬럼명이 이 패턴에 매칭되면 해당 역할로 자동 인식.
# 매칭 실패 시 mapping dialog 에서 수동 지정 가능.
# 패턴: exact match > substring match (순서 우선)
SEMANTIC_COLUMN_ROLES: dict[str, dict] = {
    "__calc_breakfast": {
        "label": "조식 건수 입력",
        "patterns": ["조식"],
        "default": "조식",
    },
    "__calc_dinner": {
        "label": "석식 건수 입력",
        "patterns": ["석식"],
        "default": "석식",
    },
    "__calc_lunch": {
        "label": "중식 건수 입력",
        "patterns": ["중식"],
        "default": "중식",
    },
    "__calc_total": {
        "label": "합계(계) 수식",
        "patterns": ["계"],
        "default": "계",
    },
    "__calc_price": {
        "label": "청구금액 수식",
        "patterns": ["청구금액"],
        "default": "청구금액",
    },
    "__calc_provider": {
        "label": "제공기관 입력",
        "patterns": ["제공기관"],
        "default": "제공기관",
    },
}


def auto_detect_semantic_columns(template_columns: list[str]) -> dict[str, str]:
    """템플릿 컬럼명 목록에서 의미론적 역할 자동 탐지.

    Returns:
        {"__calc_breakfast": "조식", "__calc_dinner": "석식", ...}
        매칭 실패 시 default 값 fallback.
    """

    def find(patterns: list[str]) -> str:
        for col in template_columns:
            for p in patterns:
                if col == p:
                    return col
        for col in template_columns:
            for p in patterns:
                if p in col:
                    return col
        return ""

    result: dict[str, str] = {}
    for key, info in SEMANTIC_COLUMN_ROLES.items():
        found = find(info["patterns"])
        result[key] = found or info["default"]
    return result


@functools.lru_cache(maxsize=2)
def read_template_columns(template_path: str) -> list[str]:
    """템플릿 파일 Row 1-2 헤더를 읽어 컬럼명 목록 반환.

    머지된 셀 처리, 개행 제거, 괄호 접미사 제거 자동 처리.
    """
    wb = openpyxl.load_workbook(template_path)
    ws = wb[TEMPLATE_SHEET_NAME]

    columns: list[str] = []
    for col in range(1, ws.max_column + 1):
        c1 = ws.cell(row=1, column=col)
        c2 = ws.cell(row=2, column=col)

        name_r2 = (c2.value or "").strip()

        if isinstance(c1, openpyxl.cell.cell.MergedCell):
            # Row 1 머지됐지만 Row 2에 실제 컬럼명 있으면 사용 (조식/석식 등)
            if name_r2:
                columns.append(name_r2)
            continue

        name_r1 = (c1.value or "").strip()

        if name_r2:
            # 하위 컬럼 (학기중→조식/석식, 방학→중식)
            columns.append(name_r2)
        elif name_r1:
            name = name_r1.replace("\n", "")
            for suffix in ["(급식수)", "(원)"]:
                if name.endswith(suffix):
                    name = name[: -len(suffix)]
            columns.append(name)

    return columns


def col_name_to_letter(name: str, template_columns: list[str]) -> str:
    """컬럼명 → 엑셀 컬럼 문자 (A, B, C...).

    template_columns 리스트 내 위치 기반.
    """
    if name not in template_columns:
        return ""
    return chr(ord("A") + template_columns.index(name))
