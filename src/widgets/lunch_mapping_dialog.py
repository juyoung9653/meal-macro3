"""학교별 방학 일정의 세 컬럼을 선택하는 매핑 다이얼로그."""

from .base_mapping_dialog import BaseMappingDialog, MappingFieldDef, MappingSection

_SCHEDULE_FIELDS = [
    (
        "school_name_col",
        "학교명",
        "방학 일정을 적용할 학교명 컬럼",
        ["학교명", "학교", "소속학교"],
    ),
    (
        "start_date_col",
        "시작일",
        "중식 배송을 시작하는 날짜 컬럼 (해당 날짜 포함)",
        ["시작일", "방학 시작일", "방학날짜", "방학 날짜", "방학식"],
    ),
    (
        "end_date_col",
        "종료일",
        "중식 배송을 종료하는 날짜 컬럼 (해당 날짜 포함)",
        ["종료일", "방학 종료일", "개학날짜", "개학 날짜", "개학식"],
    ),
]


class LunchMappingDialog(BaseMappingDialog):
    """학교명·시작일·종료일만 가져오는 방학 일정 매핑 UI.

    기존 클래스명을 유지해 저장된 실행 환경과 import 호환성을 보존한다.
    """

    def __init__(self, schedule_headers: list[str], parent=None):
        schedule_headers = [h.strip() for h in schedule_headers if h.strip()]
        rows: list = [MappingSection("학교별 방학 일정 컬럼")]
        for key, label, tooltip, search_terms in _SCHEDULE_FIELDS:
            rows.append(
                MappingFieldDef(
                    key=key,
                    label=label,
                    tooltip=tooltip,
                    search_terms=search_terms,
                    required=True,
                )
            )

        super().__init__(
            title="학교별 방학 일정 컬럼 매핑",
            rows=rows,
            headers_list=[schedule_headers],
            column_labels=["일정 파일 컬럼"],
            difflib_cutoff=0.4,
            parent=parent,
        )

    def _info_text(self) -> str:
        return (
            "학교별 방학 일정 파일에서 학교명, 시작일, 종료일 세 컬럼만 "
            "선택하세요. 시작일과 종료일은 모두 배송 기간에 포함됩니다."
        )
