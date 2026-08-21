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
    개인정보·급식 대상·비고 등 명단에서 가져올 항목을 한 구역에 표시한다.
    """

    def __init__(
        self,
        roster_headers: list[str],
        template_columns: list[str],
        parent=None,
        *,
        title: str = "컬럼 매핑 설정",
        flag_keys: tuple[str, ...] | None = None,
        info_text: str | None = None,
        required_keys: tuple[str, ...] = (),
    ):
        roster_headers = [h.strip() for h in roster_headers if h.strip()]
        self._custom_info_text = info_text

        rows = self._build_rows(template_columns, flag_keys, required_keys)
        super().__init__(
            title=title,
            rows=rows,
            headers_list=[roster_headers],
            column_labels=["값 선택"],
            difflib_cutoff=0.5,
            parent=parent,
        )

    @staticmethod
    def _build_rows(
        template_columns: list[str],
        flag_keys: tuple[str, ...] | None = None,
        required_keys: tuple[str, ...] = (),
    ) -> list:
        s = get_settings()
        flag_fields = s.get_flag_fields()
        flag_search = s.get_flag_search_terms()

        rows: list = []

        rows.append(MappingSection("명단에서 가져올 컬럼"))
        for col in template_columns:
            if col == "제공기관":
                continue
            rows.append(
                MappingFieldDef(
                    key=col,
                    label=col,
                    search_terms=[col],
                    required=col in required_keys,
                )
            )

        selected_flag_keys = flag_keys or tuple(flag_fields)
        for key in selected_flag_keys:
            info = flag_fields.get(key, {"label": key, "tooltip": ""})
            terms = flag_search.get(key, [key])
            rows.append(
                MappingFieldDef(
                    key=key,
                    label=info.get("label", key),
                    tooltip=info.get("tooltip", ""),
                    search_terms=terms or [key],
                    required=key in required_keys,
                )
            )

        return rows

    def _info_text(self) -> str:
        if self._custom_info_text:
            return self._custom_info_text
        return (
            "명단 파일의 각 컬럼이 청구서 템플릿의 어느 필드에 대응되는지 선택하세요."
        )
