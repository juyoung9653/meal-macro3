"""Editable, data-free delivery workbook templates and their structural contract."""
from __future__ import annotations

from copy import copy
from pathlib import Path

from openpyxl import Workbook, load_workbook
from .substitute_template_columns import template_column_keys, template_column_widths, VERSION_ROW, VERSION_MARKER, MERGED_VERSION_MARKER, KEY_ROW, GROUP_ROWS, expected_template_merges, merge_template_groups
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

STYLE_SHEET = "배송대타"
STYLE_COLUMNS = ("sequence", "jurisdiction", "name", "detail_address", "road_address", "mobile_phone", "note", "__summary")
STYLE_ROLES = {"title": 1, "header": 2, "body": 3, "summary_header": 19,
               "summary": 20, "total": 21, "household2": 4, "household3": 6,
               "household4": 9, "household5": 13, "box": 18}


def validate_delivery_template(path: str | Path, kind: str) -> None:
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"양식 파일을 찾을 수 없습니다. 양식 확인/수정에서 다시 선택해주세요: {path}")
    try:
        wb = load_workbook(path)
        try:
            if kind == "delivery_report":
                if "aaa" not in wb.sheetnames or wb["aaa"].max_row < 9 or wb["aaa"].max_column < 9:
                    raise ValueError("일일식수는 aaa 시트와 A1:I9 기본 구조가 필요합니다.")
                if any(merge.min_row <= 9 and merge.max_row >= 4 for merge in wb["aaa"].merged_cells.ranges):
                    raise ValueError("일일식수 데이터 영역 4~9행은 셀을 병합하지 마세요.")
            elif kind in ("happy_report_dinner", "happy_report_lunch"):
                ws = wb.worksheets[0]
                lunch = kind == "happy_report_lunch"
                if (ws.max_row < 5 or ws.max_column < 10
                        or ws["B2"].value not in ("기사", "담당") or ws["C2"].value != "지역"
                        or ws["D3"].value != ("중식" if lunch else "조식")
                        or (not lunch and ws["E3"].value != "석식")):
                    raise ValueError("행복도시락 " + ("중식" if lunch else "조식·석식") + " 일일식수 양식의 첫 시트와 A1:J5 구조·제목을 유지해주세요.")
                allowed = {"A1:I1", "A2:A3", "B2:B3", "C2:C3", "D2:H2", "I2:I3", "J2:J3", "A5:C5"}
                allowed |= {"D3:E3", "F3:H3", "D4:E4", "F4:H4", "D5:E5", "F5:H5"} if lunch else set()
                if any(str(merge) not in allowed for merge in ws.merged_cells.ranges):
                    raise ValueError("행복도시락 일일식수의 기본 병합 위치를 유지해주세요.")
            elif kind == "delivery_substitute":
                from .mapped_delivery_template import read_delivery_form
                if read_delivery_form(wb) is not None:
                    return
                if STYLE_SHEET not in wb.sheetnames:
                    raise ValueError("배송대타 시트가 있는 기본 양식 파일을 선택해주세요.")
                ws = wb[STYLE_SHEET]
                if ws.max_row < 21:
                    raise ValueError("배송대타 양식의 1~21행 위치를 유지해주세요.")
                keys = template_column_keys(ws)
                expanded = ws.cell(VERSION_ROW, 1).value in (VERSION_MARKER, MERGED_VERSION_MARKER)
                if ws.max_row < 21 or (not expanded and (ws.max_column != 8 or tuple(ws.cell(2, col).value for col in range(1, 9)) != ("배송순서", "관할동", "이름", "상세주소", "신주소", "핸드폰", "비고2", "요약"))):
                    raise ValueError("배송대타 양식의 A:H 칼럼과 1~21행 위치, 2행 제목을 유지해주세요.")
                actual_merges = {str(merge) for merge in ws.merged_cells.ranges}
                if ws.cell(VERSION_ROW, 1).value == MERGED_VERSION_MARKER:
                    if actual_merges != expected_template_merges(keys):
                        raise ValueError("배송목록의 1~5식 예시 병합 구조가 변경되었습니다. 성명 외 칼럼의 가구별 병합을 유지해주세요.")
                elif any(merge.min_row != 1 or merge.max_row != 1 or merge.min_col != 1 or merge.max_col != len(keys) for merge in ws.merged_cells.ranges):
                    raise ValueError("배송대타 양식의 병합 구조가 올바르지 않습니다.")
                for width in template_column_widths(ws).values():
                    if width is None or not 1 <= width <= 255:
                        raise ValueError("배송목록 열 너비는 1~255 범위로 정해주세요.")
                if any(dim.hidden for dim in ws.column_dimensions.values()):
                    raise ValueError("배송목록 양식의 열을 숨기지 마세요. 출력 칼럼은 프로그램에서 선택해주세요.")
                for row in range(1, 22):
                    dimension = ws.row_dimensions[row]
                    height = dimension.height if dimension.height is not None else ws.sheet_format.defaultRowHeight
                    if height is None or not 1 <= height <= 409 or dimension.hidden:
                        raise ValueError("배송목록 1~21행은 숨기지 말고 행 높이를 1~409 범위로 정해주세요.")
                if any(not isinstance(ws.cell(2, col).value, str) or not ws.cell(2, col).value.strip()
                       for col in range(1, len(keys) + 1)):
                    raise ValueError("배송목록 칼럼 제목을 비우지 마세요.")
            else:
                raise ValueError("지원하지 않는 배송 양식입니다.")
        finally:
            wb.close()
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("양식을 읽지 못했습니다. 암호 없는 .xlsx 파일인지 확인하고 Excel에서 저장 후 다시 시도해주세요.") from error


def _new_substitute_template():
    """Build the shipped printable form using synthetic placeholders only."""
    wb = Workbook()
    ws = wb.active
    ws.title = STYLE_SHEET
    thin = Side(style="thin", color="000000")
    thick = Side(style="thick", color="000000")
    for row in range(1, 22):
        role = next((name for name, number in STYLE_ROLES.items() if number == row), "body")
        for col in range(1, 9):
            cell = ws.cell(row, col, "")
            cell.font = Font(name="맑은 고딕", size=12 if row == 1 else 10 if row == 2 else 8,
                             bold=role in ("title", "header", "summary_header", "summary", "total"), color="000000")
            cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
            cell.alignment = Alignment(vertical="bottom", wrap_text=True)
            if role in ("header", "summary_header", "total"):
                cell.fill = PatternFill("solid", fgColor="DCE6F1")
        ws.row_dimensions[row].height = 25
    ws["A1"] = "식종: [식종] | 배송담당: [담당자] | 날짜: [배송일]"
    ws.merge_cells("A1:H1")
    for col, label in enumerate(("배송순서", "관할동", "이름", "상세주소", "신주소", "핸드폰", "비고2", "요약"), 1):
        ws.cell(2, col, label)
    groups = ((3, 3, 1, None), (4, 5, 2, "E6B8B7"), (6, 8, 3, "B8CCE4"),
              (9, 12, 4, "D8E4BC"), (13, 17, 5, "CCC0DA"), (18, 18, 1, None))
    for index, (start, end, count, color) in enumerate(groups, 1):
        for row in range(start, end + 1):
            values = (str(index).zfill(3), "[관할동]", "[성명]", "[상세주소]", "[도로명주소]", "[연락처]", "[비고]" if row != 18 else "[BOX 가구]", count if row == start else "")
            for col, value in enumerate(values, 1):
                cell = ws.cell(row, col, value)
                if color:
                    cell.fill = PatternFill("solid", fgColor=color)
                if row == 18:
                    cell.border = Border(left=thick if col == 1 else thin,
                                         right=thick if col == 8 else thin, top=thick, bottom=thick)
    for col, value in enumerate(("관할동", "1식", "2식", "3식", "4식", "5식", "식수 합계", ""), 1):
        ws.cell(19, col, value)
    ws["A20"], ws["A21"] = "[관할동별 합계]", "전체"
    for col in range(2, 8):
        ws.cell(20, col, "[자동 집계]")
        ws.cell(21, col, "[자동 집계]")
    for col, width in zip("ABCDEFGH", (10, 12, 14, 18, 28, 20, 22, 10)):
        ws.column_dimensions[col].width = width
    merge_template_groups(ws, STYLE_COLUMNS)
    for index, key in enumerate(STYLE_COLUMNS, 1):
        ws.cell(KEY_ROW, index, key)
    ws.cell(VERSION_ROW, 1, MERGED_VERSION_MARKER)
    ws.row_dimensions[KEY_ROW].hidden = True
    ws.row_dimensions[VERSION_ROW].hidden = True
    ws.print_area = "A1:H21"
    ws.print_title_rows = "1:2"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    guide = wb.create_sheet("수정 안내")
    for row, text in enumerate(("배송대타 시트의 실제 배송표에서 글꼴·크기·글자색·채우기·테두리를 직접 수정하세요.",
        "1행 제목, 2행 칼럼 제목, 3행 본문 서식을 사용합니다. 각 칼럼의 서식은 출력 순서를 바꿔도 해당 항목에 적용합니다.",
        "3행은 1식, 4~5행은 2식, 6~8행은 3식, 9~12행은 4식, 13~17행은 5식입니다. 각 예시의 높이·글꼴·색·테두리를 출력에 적용합니다.",
        "18행의 바깥 테두리는 BOX 가구에 적용합니다. 일반 본문 테두리보다 우선합니다.",
        "19행 집계 제목, 20행 관할동 합계, 21행 전체 합계 서식을 사용합니다.",
        "새 칼럼은 양식을 열 때 자동 추가되며 비고의 너비·서식을 복사해 시작합니다. 이후 각 칼럼을 따로 수정할 수 있습니다.",
        "시트 이름·2행 제목·행과 열 위치를 유지하세요. 행/열 삽입과 병합은 지원하지 않습니다.",
        "샘플의 이름과 주소는 실제 정보가 아닙니다. 생성 시 실제 데이터로 바꾸며 가구별 병합·집계는 자동으로 처리합니다.",
        '템플릿에서 셀 정렬(가로·세로), 줄바꿈·들여쓰기·회전, 표시 형식, 인쇄 설정을 바꿔도 출력에는 반영되지 않습니다.',
        '가로·세로 정렬은 배송대타 만들기의 칼럼별 정렬에서, 출력 칼럼·순서·용지·페이지 구분은 프로그램에서 설정하세요.',
        '열 너비는 가로·세로 용지 모두 해당 항목의 템플릿 너비를 유지합니다. 확장한 양식에서는 각 칼럼의 너비를 사용하며 추가 집계 열은 요약 칼럼 너비를 사용합니다. 행 높이는 각 식수 예시의 값을 유지합니다. 긴 내용이 잘리면 예시의 높이를 늘려주세요. 샘플 값·수식을 바꿔도 출력 데이터나 계산 방식은 바뀌지 않습니다.',
        "Excel에서 저장하고 닫은 뒤 생성하세요."), 1):
        guide.cell(row, 1, text)
    guide.column_dimensions["A"].width = 130
    return wb


def create_substitute_template(path: str | Path) -> None:
    wb = _new_substitute_template()
    try:
        wb.save(path)
    finally:
        wb.close()


def _substitute_styles(wb):
    keys = template_column_keys(wb[STYLE_SHEET])
    ws = wb[STYLE_SHEET]
    merged = ws.cell(VERSION_ROW, 1).value == MERGED_VERSION_MARKER
    groups = tuple(tuple(tuple((copy(ws.cell(row, col).font), copy(ws.cell(row, col).fill),
                                copy(ws.cell(row, col).border)) for col in range(1, len(keys) + 1))
                         for row in range(start, end + 1)) for start, end in GROUP_ROWS) if merged else ()
    heights = tuple(tuple(ws.row_dimensions[row].height or ws.sheet_format.defaultRowHeight
                           for row in range(start, end + 1)) for start, end in GROUP_ROWS) if merged else ()
    return {"column_keys": keys, "normal_font": copy(wb._named_styles[0].font),
            "group_styles": groups, "group_row_heights": heights, **{role: tuple((copy(wb[STYLE_SHEET].cell(row, col).font),
                         copy(wb[STYLE_SHEET].cell(row, col).fill),
                         copy(wb[STYLE_SHEET].cell(row, col).border)) for col in range(1, len(keys) + 1))
            for role, row in STYLE_ROLES.items()}}


def substitute_styles_are_default(styles) -> bool:
    """Unedited forms retain the established native font sizing/pagination."""
    baseline = _new_substitute_template()
    try:
        return styles == _substitute_styles(baseline)
    finally:
        baseline.close()


def load_substitute_styles(path: str | Path):
    validate_delivery_template(path, "delivery_substitute")
    wb = load_workbook(path)
    try:
        return _substitute_styles(wb)
    finally:
        wb.close()


def load_substitute_widths(path: str | Path) -> dict[str, float]:
    """Read item widths, including Excel's shared column-dimension ranges."""
    wb = load_workbook(path)
    try:
        return template_column_widths(wb[STYLE_SHEET])
    finally:
        wb.close()
