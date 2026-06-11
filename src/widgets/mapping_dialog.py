"""컬럼 매핑 편집 다이얼로그 — 명단 컬럼 ↔ 청구서 템플릿"""

from ..core.settings import get_settings
from .base_mapping_dialog import (
    BaseMappingDialog,
    MappingFieldDef,
    MappingSection,
)


class MappingDialog(BaseMappingDialog):
    """
    템플릿 컬럼 목록을 template_columns 기준으로 동적 생성.
    2개 섹션:
      1. 템플릿 컬럼 ↔ 명단 컬럼 매핑
      2. 배송일 계산용 플래그
    """

    def __init__(
        self, roster_headers: list[str], template_columns: list[str], parent=None
    ):
        roster_headers = [h.strip() for h in roster_headers if h.strip()]

        rows = self._build_rows(template_columns)
        super().__init__(
            title="컬럼 매핑 설정",
            rows=rows,
            headers_list=[roster_headers],
            column_labels=["값 선택"],
            difflib_cutoff=0.5,
            parent=parent,
        )

    @staticmethod
    def _build_rows(template_columns: list[str]) -> list:
        s = get_settings()
        flag_fields = s.get_flag_fields()
        flag_search = s.get_flag_search_terms()

        rows: list = []

        # ── 섹션 1: 템플릿 컬럼 → 명단 컬럼 ──
        rows.append(MappingSection("템플릿 컬럼 → 명단 컬럼"))
        for col in template_columns:
            rows.append(
                MappingFieldDef(
                    key=col,
                    label=col,
                    search_terms=[col],
                )
            )

        # ── 섹션 2: 배송일 계산용 플래그 ──
        rows.append(MappingSection("배송일 계산용"))
        for key, info in flag_fields.items():
            terms = flag_search.get(key, [key])
            rows.append(
                MappingFieldDef(
                    key=key,
                    label=info.get("label", key),
                    tooltip=info.get("tooltip", ""),
                    search_terms=terms or [key],
                )
            )

        return rows

    def _info_text(self) -> str:
        return (
            "명단 파일의 각 컬럼이 청구서 템플릿의 어느 필드에 대응되는지 선택하세요."
        )
