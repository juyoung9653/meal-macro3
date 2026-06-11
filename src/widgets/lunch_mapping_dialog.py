"""중식 명단 컬럼 매핑 다이얼로그 — 중식 명단 컬럼 ↔ 필수 필드 + 교차 참조 + 청구서 컬럼."""

from .base_mapping_dialog import (
    BaseMappingDialog,
    MappingFieldDef,
    MappingSection,
)

# ── 중식 명단 필수 필드 정의 ──

_LUNCH_FIELDS = [
    ("lunch_school_name", "학교명", "중식 명단의 학교명 컬럼"),
    ("lunch_vacation_date", "방학식 일자", "방학 시작일 (YYYY.MM.DD 형식)"),
    ("lunch_opening_date", "개학식 일자", "개학일 (YYYY.MM.DD 형식)"),
    ("lunch_vacation_meal", "방학식 급식 제공 여부", "O/X, Y/N 등"),
    ("lunch_opening_meal", "개학식 급식 제공 여부", "O/X, Y/N 등"),
    ("lunch_note", "비고", "방학/개학 관련 특이사항"),
]

# 자동 탐지용 검색어
_LUNCH_SEARCH_TERMS = {
    "lunch_school_name": ["학교명", "학교"],
    "lunch_vacation_date": ["방학식", "방학실", "방학 날짜", "방학일자"],
    "lunch_opening_date": ["개학식", "개학", "개학 날짜", "개학일자"],
    "lunch_vacation_meal": ["방학식 급식", "방학 급식", "방학식급식"],
    "lunch_opening_meal": ["개학일 급식", "개학식 급식", "개학 급식", "개학식급식"],
    "lunch_note": ["비고", "특이사항", "메모"],
}

# ── 교차 참조 필드 (명단 ↔ 중식 명단) ──

_CROSS_REF_FIELDS = [
    ("roster_school_col", "명단 학교명 컬럼", "명단에서 학교명이 있는 컬럼"),
    ("roster_grade_col", "명단 학년 컬럼", "명단에서 학년 정보가 있는 컬럼"),
]

_CROSS_REF_SEARCH_TERMS = {
    "roster_school_col": ["학교명", "학교", "소속", "기관명"],
    "roster_grade_col": ["학년", "학년도", "grade", "Grade"],
}

# ── 청구서 컬럼 매핑 (방학 계산 결과 → 청구서 템플릿) ──

_INVOICE_COL_FIELDS = [
    ("delivery_start_col", "배송시작일 컬럼", "청구서 템플릿의 배송시작일 컬럼"),
    ("delivery_end_col", "배송종료일 컬럼", "청구서 템플릿의 배송종료일 컬럼"),
]

_INVOICE_COL_SEARCH_TERMS = {
    "delivery_start_col": ["배송시작", "배송시작일", "배송 시작일"],
    "delivery_end_col": ["배송종료", "배송종료일", "배송 종료일"],
}


class LunchMappingDialog(BaseMappingDialog):
    """중식 명단 컬럼 매핑 다이얼로그.

    섹션:
      1. 중식 명단 컬럼 매핑 (6개 필수 필드)
      2. 교차 참조 (명단의 학교명/학년 컬럼)
      3. 청구서 컬럼 매핑 (배송시작일/종료일 → 청구서 템플릿)
    """

    def __init__(
        self,
        lunch_headers: list[str],
        roster_headers: list[str] | None = None,
        template_columns: list[str] | None = None,
        parent=None,
    ):
        lunch_headers = [h.strip() for h in lunch_headers if h.strip()]
        roster_headers = (
            [h.strip() for h in roster_headers if h.strip()] if roster_headers else []
        )
        template_columns = (
            [h.strip() for h in template_columns if h.strip()]
            if template_columns
            else []
        )

        rows = self._build_rows()
        super().__init__(
            title="중식 명단 컬럼 매핑",
            rows=rows,
            headers_list=[lunch_headers, roster_headers, template_columns],
            column_labels=["값 선택"],
            difflib_cutoff=0.4,
            parent=parent,
        )

    @staticmethod
    def _build_rows() -> list:
        rows: list = []

        # ── 섹션 1: 중식 명단 컬럼 매핑 ──
        rows.append(MappingSection("중식 명단 컬럼 매핑"))
        for key, label, tip in _LUNCH_FIELDS:
            terms = _LUNCH_SEARCH_TERMS.get(key, [key])
            rows.append(
                MappingFieldDef(
                    key=key,
                    label=label,
                    tooltip=tip,
                    search_terms=terms,
                    combo_column=0,
                    source_index=0,
                )
            )

        # ── 섹션 2: 교차 참조 ──
        rows.append(MappingSection("교차 참조 (명단 ↔ 중식 명단)"))
        for key, label, tip in _CROSS_REF_FIELDS:
            terms = _CROSS_REF_SEARCH_TERMS.get(key, [key])
            rows.append(
                MappingFieldDef(
                    key=key,
                    label=label,
                    tooltip=tip,
                    search_terms=terms,
                    combo_column=0,
                    source_index=1,
                )
            )

        # ── 섹션 3: 청구서 컬럼 매핑 ──
        rows.append(MappingSection("청구서 컬럼 매핑 (계산 결과 → 템플릿)"))
        for key, label, tip in _INVOICE_COL_FIELDS:
            terms = _INVOICE_COL_SEARCH_TERMS.get(key, [key])
            rows.append(
                MappingFieldDef(
                    key=key,
                    label=label,
                    tooltip=tip,
                    search_terms=terms,
                    combo_column=0,
                    source_index=2,
                )
            )

        return rows

    def _info_text(self) -> str:
        return (
            "중식 명단 파일의 각 컬럼을 아래 항목에 매핑하세요.\n"
            "교차 참조 항목은 명단 파일의 컬럼에서 선택합니다.\n"
            "청구서 컬럼 매핑은 청구서 템플릿의 컬럼에서 선택합니다."
        )
