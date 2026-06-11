"""중식 명단 컬럼 매핑 다이얼로그 — 중식 명단 컬럼 ↔ 필수 필드 + 교차 참조."""

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


class LunchMappingDialog(BaseMappingDialog):
    """중식 명단 컬럼 매핑 다이얼로그.

    섹션:
      1. 중식 명단 컬럼 매핑 (6개 필수 필드)
      2. 교차 참조 (명단의 학교명/학년 컬럼)
    """

    def __init__(
        self,
        lunch_headers: list[str],
        roster_headers: list[str] | None = None,
        parent=None,
    ):
        lunch_headers = [h.strip() for h in lunch_headers if h.strip()]
        roster_headers = (
            [h.strip() for h in roster_headers if h.strip()] if roster_headers else []
        )

        rows = self._build_rows()
        super().__init__(
            title="중식 명단 컬럼 매핑",
            rows=rows,
            headers_list=[lunch_headers, roster_headers],
            column_labels=["중식 명단 컬럼", "명단 컬럼 (교차참조)"],
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
                    combo_column=1,
                    source_index=1,
                )
            )

        return rows

    def _info_text(self) -> str:
        return (
            "중식 명단 파일의 각 컬럼을 아래 항목에 매핑하세요.\n"
            "하단의 '교차 참조'는 명단과 중식 명단 간 학교명 매칭에 사용됩니다."
        )
