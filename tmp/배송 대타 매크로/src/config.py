from typing import Literal

SHEET_NAME = "종합_jon(DB)"
HEADER_ROW = 2
DATA_ROW = 4

COL_MEAL = "식사 구분"
COL_SHIP = "배송_new"
COL_ORD = "배송순서"
COL_NAME = "이름"
COL_DONG = "관할동"
COL_SUM = "요약"
COL_ADDR = "신주소"
COL_PHONE = "핸드폰"
COL_NOTE = "비고2"
BOX_SUFFIX = "_BOX"

PICK_COLS = [COL_ORD, COL_DONG, COL_NAME, "상세주소", COL_ADDR, COL_PHONE, COL_NOTE]

# 18행 넘어가면 19행이 짤려서 다음 페이지로 됨
MAX_ROWS = 18
A4_PAGE: Literal["자동", "세로", "가로"] = "세로"
PAGE_H = 842
PAGE_W = 595
SPLIT_MODE: Literal["사용 안함", "동기준", "행기준"] = "동기준"

FONT_BASE_SIZE = 8  # 기본 폰트 크기
