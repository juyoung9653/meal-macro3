"""Stable column identities for editable, expandable substitute templates."""
from copy import copy
from pathlib import Path
from tempfile import NamedTemporaryFile
import os

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.worksheet.dimensions import DEFAULT_COLUMN_WIDTH

KEY_ROW = 22
VERSION_ROW = 23
VERSION_MARKER = "meal-delivery-substitute-columns-v1"
MERGED_VERSION_MARKER = "meal-delivery-substitute-columns-v2"
GROUP_ROWS = ((3, 3), (4, 5), (6, 8), (9, 12), (13, 17))


def expected_template_merges(keys):
    merges = {f"A1:{get_column_letter(len(keys))}1"}
    for index, key in enumerate(keys, 1):
        if key != "name":
            letter = get_column_letter(index)
            merges.update(f"{letter}{start}:{letter}{end}" for start, end in GROUP_ROWS if end > start)
    return merges


def merge_template_groups(ws, keys):
    for start, end in GROUP_ROWS:
        if start == end:
            continue
        for index, key in enumerate(keys, 1):
            if key != "name":
                ws.merge_cells(start_row=start, end_row=end, start_column=index, end_column=index)



def template_column_keys(ws):
    from .delivery_templates import STYLE_COLUMNS
    if ws.cell(VERSION_ROW, 1).value not in (VERSION_MARKER, MERGED_VERSION_MARKER):
        return STYLE_COLUMNS
    keys = tuple(ws.cell(KEY_ROW, col).value for col in range(1, ws.max_column + 1))
    if (any(not isinstance(key, str) or not key for key in keys)
            or len(set(keys)) != len(keys) or not set(STYLE_COLUMNS) <= set(keys)):
        raise ValueError("배송목록 양식의 칼럼 연결 정보가 손상되었습니다. 숨긴 22·23행을 유지해주세요.")
    return keys


def template_column_widths(ws):
    keys = template_column_keys(ws)
    widths = dict.fromkeys(keys, ws.sheet_format.defaultColWidth or DEFAULT_COLUMN_WIDTH)
    for letter, dimension in ws.column_dimensions.items():
        start = dimension.min or column_index_from_string(letter)
        end = dimension.max or start
        for index in range(max(1, start), min(len(keys), end) + 1):
            widths[keys[index - 1]] = dimension.width
    return widths


def sync_substitute_template(path, columns, selected=()):
    """Add missing items and put checked items first, preserving styles by ID.

    Only the caller's editable template is changed; bundled defaults are copied
    by the desktop layer. Removed fields remain available for historical output.
    """
    from .delivery_templates import STYLE_SHEET, validate_delivery_template
    path = Path(path)
    validate_delivery_template(path, "delivery_substitute")
    original = path.read_bytes()
    book = load_workbook(path)
    temporary = None
    try:
        ws = book[STYLE_SHEET]
        keys = template_column_keys(ws)
        labels = {key: ws.cell(2, index).value for index, key in enumerate(keys, 1)}
        labels.update({column.key: column.label for column in columns})
        order = list(dict.fromkeys((*keys, *(column.key for column in columns))))
        selected = list(dict.fromkeys(key for key in selected if key in order and key != "__summary"))
        order = selected + [key for key in order if key not in selected and key != "__summary"] + ["__summary"]
        if (ws.cell(VERSION_ROW, 1).value == MERGED_VERSION_MARKER and tuple(order) == keys
                and all(ws.cell(2, i).value == labels[key] for i, key in enumerate(keys, 1))):
            return False
        widths = template_column_widths(ws)
        samples = {key: [(ws.cell(row, col).value, copy(ws.cell(row, col)._style))
                         for row in range(1, 22)] for col, key in enumerate(keys, 1)}
        title = ws.cell(1, 1).value, copy(ws.cell(1, 1)._style)
        for merged in list(ws.merged_cells.ranges):
            ws.unmerge_cells(str(merged))
        ws.column_dimensions.clear()
        for col, key in enumerate(order, 1):
            source = samples.get(key, samples["note"])
            for row, (value, style) in enumerate(source, 1):
                cell = ws.cell(row, col)
                cell.value, cell._style = value, copy(style)
                if key not in samples and 3 <= row <= 18:
                    cell.value = "[" + labels[key] + "]"
                    cell.data_type = "s"
            ws.cell(2, col, labels[key]).data_type = "s"
            ws.cell(KEY_ROW, col, key).data_type = "s"
            ws.column_dimensions[get_column_letter(col)].width = widths.get(key, widths["note"])
        ws.cell(1, 1).value, ws.cell(1, 1)._style = title
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(order))
        merge_template_groups(ws, order)
        ws.cell(VERSION_ROW, 1, MERGED_VERSION_MARKER)
        ws.row_dimensions[KEY_ROW].hidden = True
        ws.row_dimensions[VERSION_ROW].hidden = True
        ws.print_area = f"A1:{get_column_letter(len(order))}21"
        ws.freeze_panes = "A3"
        guide = book["수정 안내"] if "수정 안내" in book.sheetnames else book.create_sheet("수정 안내")
        instructions = (
            "배송대타 시트에 모든 칼럼을 모아 둡니다. 새 칼럼은 양식을 열거나 문서를 만들 때 자동 추가합니다.",
            "배송목록에서 마지막으로 체크한 칼럼은 출력 순서대로 앞에 배치하며 나머지 칼럼과 요약은 뒤에 둡니다.",
            "각 칼럼의 열 너비·글꼴·크기·글자색·채우기·테두리를 수정하세요. 가로·세로 출력 모두 해당 열 너비를 유지합니다.",
            "새 칼럼은 비고 칼럼의 너비·서식을 복사해 시작하며 이후 각각 수정할 수 있습니다.",
            "1행 제목, 2행 칼럼 제목, 3행 본문 서식을 사용합니다. 출력 순서와 이름이 바뀌어도 같은 칼럼에 적용합니다.",
            "3행은 1식, 4~5행은 2식, 6~8행은 3식, 9~12행은 4식, 13~17행은 5식 예시입니다. 성명 외 칼럼은 실제 출력처럼 병합합니다.",
            "각 식수 예시의 행 높이·글꼴·색·테두리를 수정하면 해당 크기의 출력 가구에 적용합니다. 6식 이상은 1식 행 높이·본문 서식을 반복합니다. 18행 바깥 테두리는 BOX 가구에 적용합니다.",
            "19행 집계 제목, 20행 관할동 합계, 21행 전체 합계 서식을 사용합니다.",
            "숨긴 22·23행은 칼럼 연결 정보입니다. 시트 이름·행 위치를 유지하고 직접 행/열 삽입·삭제·병합은 하지 마세요.",
            "이름은 프로그램에서 바꾸세요. 삭제된 칼럼도 기존 양식에는 남겨 과거 원본의 서식을 보존합니다.",
            "실제 명단은 양식에 저장하지 않습니다. 예시 값은 출력할 때 실제 값으로 바뀝니다.",
            "셀 정렬·줄바꿈·표시 형식·인쇄 설정 변경은 출력에 반영되지 않습니다. 정렬·용지 방향·페이지 구분은 프로그램에서 선택하세요.",
            "행 높이는 지정한 값으로 고정합니다. 긴 내용이 잘리면 해당 식수 예시의 높이를 늘려주세요. 인쇄는 가로 한 페이지에 맞추므로 용지 방향에 따라 배율이 달라질 수 있습니다.",
            "Excel에서 저장하고 닫은 뒤 이 양식 사용을 누르세요.",
        )
        for row in range(1, max(guide.max_row, len(instructions)) + 1):
            guide.cell(row, 1).value = instructions[row - 1] if row <= len(instructions) else None
        guide.column_dimensions["A"].width = 130
        with NamedTemporaryFile(dir=path.parent, suffix=".xlsx", delete=False) as stream:
            temporary = Path(stream.name)
        book.save(temporary)
        validate_delivery_template(temporary, "delivery_substitute")
        if path.read_bytes() != original:
            raise ValueError("다른 프로그램에서 양식을 변경했습니다. Excel을 닫고 다시 시도해주세요.")
        os.replace(temporary, path)
        return True
    finally:
        book.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
