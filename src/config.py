"""컬럼 매핑 설정 — 명단→청구서 컬럼 대응"""

DEFAULT_ROSTER_TO_TEMPLATE = {
    # 명단 컬럼명 → 청구서 템플릿 컬럼 인덱스(0-based)
    # 컬럼명 기준 매핑이며 GUI에서 수정 가능
    "roster_name_col": "성명",
    "roster_dept_col": "학교명",
    "roster_grade_col": "학년",
    "roster_birth_col": "생년월일",
    "roster_head_col": "세대주성명",
    "roster_relation_col": "관계",
    "roster_dong_col": "관할동",
    "roster_addr_col": "주소",
    "roster_phone_col": "휴대전화",
    "roster_breakfast_col": "조식",  # 학기중 조식 여부
    "roster_dinner_col": "석식",  # 학기중 석식 여부
    "roster_vacation_col": "방중",  # 방학중 급식 여부
    "roster_note_col": "학기중 비고",  # 비고(일시중지/신규)
    "roster_category_col": "해당구",  # 구분(신규/중/고)
}

DEFAULT_TEMPLATE_COLUMNS = [
    "연번",  # A
    "구분",  # B
    "학교명",  # C
    "학년",  # D
    "성명",  # E
    "생년월일",  # F
    "세대주성명",  # G
    "아동과의관계",  # H
    "관할동",  # I
    "주소",  # J
    "핸드폰",  # K
    "조식",  # L (학기중 조식 건수)
    "석식",  # M (학기중 석식 건수)
    "중식",  # N (방학 중식 건수)
    "계",  # O
    "청구금액",  # P
    "제공기관",  # Q
    "비고",  # R
]

TEMPLATE_SHEET_NAME = "학기중"
TEMPLATE_DATA_START_ROW = 4  # 4행부터 데이터 입력
