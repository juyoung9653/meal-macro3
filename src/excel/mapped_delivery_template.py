"""Placeholder delivery forms: insert repeated rows and retain Excel styles."""
from copy import copy
from dataclasses import dataclass
import re

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.formula import Tokenizer
from openpyxl.formula.translate import Translator
from openpyxl.utils.datetime import to_excel
from openpyxl.worksheet.cell_range import CellRange

TOKEN = re.compile(r"\{([^{}\r\n]+)\}")
KEEP = "__keep_template"
DEFAULTS = {
    "연번": "sequence", "배송순서": "sequence", "학생": "name", "성명": "name", "이름": "name",
    "주소": "full_address", "전체 주소": "full_address", "도로명주소": "road_address",
    "상세주소": "detail_address", "연락처": "mobile_phone", "핸드폰": "mobile_phone",
    "전화번호": "telephone", "비고": "note", "관할동": "jurisdiction",
    "배송일자": "delivery_day", "배송일": "delivery_day", "날짜": "delivery_day",
    "배송담당": "driver_label", "명단제목": "meal_title", "식종": "meal_label",
    "선택식종": "meal_receives", "조식": "receives:breakfast",
    "중식": "receives:lunch", "석식": "receives:dinner",
}


@dataclass(frozen=True)
class DeliveryForm:
    sheet: str
    targets: tuple[str, ...]
    defaults: dict[str, str]
    suggested_row: int
    max_row: int


def read_delivery_form(workbook):
    """Recognize by placeholders; the sheet name and coordinates are free."""
    for ws in workbook:
        targets, counts = {}, {}
        for row in ws:
            count = 0
            for cell in row:
                if cell.data_type == "f" or not isinstance(cell.value, str):
                    continue
                for match in TOKEN.finditer(cell.value):
                    targets.setdefault(match.group(1), None)
                    count += 1
            if count:
                counts[row[0].row] = count
        if targets:
            suggested = max(counts, key=counts.get)  # First row wins ties.
            return DeliveryForm(ws.title, tuple(targets),
                                {key: DEFAULTS.get(key, "") for key in targets}, suggested, ws.max_row)
    return None


def inspect_delivery_form(path):
    workbook = load_workbook(path)
    try:
        return read_delivery_form(workbook)
    finally:
        workbook.close()


def _shift_formula(value, first, delta, sheet):
    """Adjust same-sheet references when inserting rows (including totals)."""
    if not isinstance(value, str) or not value.startswith("=") or not delta:
        return value
    parts = []
    for token in Tokenizer(value).items:
        text = token.value
        if token.type == "OPERAND" and token.subtype == "RANGE":
            prefix, sep, address = text.rpartition("!")
            if not sep or prefix.strip("'").replace("''", "'") == sheet:
                address = address if sep else text
                if ":" in address:
                    left, colon, right = address.rpartition(":")
                    endpoint = re.fullmatch(r"(\$?[A-Z]{1,3}\$?)([1-9][0-9]*)", right)
                    if endpoint and int(endpoint[2]) == first - 1:
                        # Expand a total ending on the sample's last row.
                        right = endpoint[1] + str(int(endpoint[2]) + delta)
                        left = re.sub(r"(\$?[A-Z]{1,3}\$?)([1-9][0-9]*)", lambda m:
                            m[1] + str(int(m[2]) + delta if int(m[2]) >= first else int(m[2])), left)
                        parts.append(prefix + sep + left + colon + right)
                        continue
                address = re.sub(r"(\$?[A-Z]{1,3}\$?)([1-9][0-9]*)", lambda m:
                    m[1] + str(int(m[2]) + delta if int(m[2]) >= first else int(m[2])), address)
                text = prefix + sep + address
        parts.append(text)
    return "=" + "".join(parts)


def _repeat_rows(ws, start, end, count):
    """Duplicate a selected block with its row heights, merges and formulas."""
    height = end - start + 1
    delta = height * (count - 1)
    cells = {(row, col): copy(cell) for (row, col), cell in ws._cells.items()
             if row >= start and not isinstance(cell, MergedCell)}
    dimensions = {row: copy(dim) for row, dim in ws.row_dimensions.items() if row >= start}
    merges = [copy(merged) for merged in ws.merged_cells.ranges if merged.max_row >= start]
    outside_merges = set()
    print_ranges = tuple(ws._print_area.ranges) if ws.print_area else ()
    for merged in merges:
        if merged.min_row < start or merged.min_row <= end < merged.max_row:
            outside = print_ranges and all(merged.min_col > area.max_col or merged.max_col < area.min_col
                                           for area in print_ranges)
            if not outside:
                raise ValueError("복사할 행의 경계에 병합 셀이 걸쳐 있습니다. 병합된 행 전체를 복사 범위로 선택해주세요.")
            outside_merges.add(str(merged))
        ws.unmerge_cells(str(merged))
    for key in list(ws._cells):
        if key[0] >= start:
            del ws._cells[key]
    for row in list(ws.row_dimensions):
        if row >= start:
            del ws.row_dimensions[row]
    for (row, col), source in cells.items():
        destinations = [row + height * index for index in range(count)] if row <= end else [row + delta]
        for destination in destinations:
            target = ws.cell(destination, col)
            value = source.value
            if source.data_type == "f":
                value = (Translator(value, origin=source.coordinate).translate_formula(target.coordinate)
                         if row <= end else _shift_formula(value, end + 1, delta, ws.title))
            target.value, target._style = value, copy(source._style)
            target.data_type = source.data_type
            target.comment = copy(source.comment)
            if source.hyperlink:
                target._hyperlink = copy(source.hyperlink)
                target._hyperlink.ref = target.coordinate
    for row, dimension in dimensions.items():
        destinations = [row + height * index for index in range(count)] if row <= end else [row + delta]
        for destination in destinations:
            ws.row_dimensions[destination] = copy(dimension)
            ws.row_dimensions[destination].index = destination
    for merged in merges:
        if str(merged) in outside_merges:
            merged.max_row += delta
            ws.merge_cells(str(merged))
            continue
        for offset in ([height * index for index in range(count)] if merged.max_row <= end else [delta]):
            shifted = copy(merged)
            shifted.shift(row_shift=offset)
            ws.merge_cells(str(shifted))
    if start > 1:
        for row in ws.iter_rows(max_row=start - 1):
            for cell in row:
                if cell.data_type == "f":
                    cell.value = _shift_formula(cell.value, end + 1, delta, ws.title)
    if ws.print_area:
        areas = []
        for area in ws._print_area.ranges:
            shifted = CellRange(str(area))
            if shifted.min_row > end:
                shifted.min_row += delta
            if shifted.max_row >= start:
                shifted.max_row += delta
            areas.append(str(shifted))
        ws.print_area = areas
    return height


def _write(cell, value, workbook):
    from datetime import date
    if isinstance(value, date):
        # Preserve the template's number format exactly.
        cell.value = to_excel(value, workbook.epoch)
    else:
        cell.value = value if value != "" else None
        if isinstance(value, str) and value:
            cell.data_type = "s"


def build_mapped_delivery_workbook(path, data, options):
    workbook = load_workbook(path)
    try:
        form = read_delivery_form(workbook)
        if form is None:
            workbook.close()
            return None
        rows = data.selected_rows(options)
        mapping = {**form.defaults, **options.template_mapping}
        if (set(mapping) | set(options.template_fallbacks)) - set(form.targets):
            raise ValueError("양식의 플레이스홀더가 바뀌었습니다. 배송목록의 양식 항목 연결을 다시 확인해주세요.")
        from src.domain.delivery_substitute import DELIVERY_SOURCES
        known = {column.key for column in (*data.columns, *DELIVERY_SOURCES)}
        if (set(mapping.values()) | set(options.template_fallbacks.values())) - known - {"", KEEP}:
            raise ValueError("양식에 연결한 항목이 현재 명단에 없습니다. 배송목록의 양식 항목 연결을 다시 선택해주세요.")
        start, end = options.template_rows or (form.suggested_row, form.suggested_row)
        if not 1 <= start <= end <= form.max_row:
            raise ValueError("복사할 행이 양식 범위를 벗어났습니다. 시작 행과 끝 행을 다시 선택해주세요.")
        ws = workbook[form.sheet]
        if not any(TOKEN.search(str(cell.value or "")) for row in ws.iter_rows(min_row=start, max_row=end) for cell in row):
            raise ValueError("복사할 행에 플레이스홀더가 없습니다. 대상자 항목이 들어 있는 행을 선택해주세요.")
        height = _repeat_rows(ws, start, end, len(rows))
        context = {"delivery_day": data.day, "driver_label": rows[0].driver_label,
                   "meal_title": data.meal.value + "명단", "meal_label": data.meal.value,
                   "jurisdiction": ", ".join(dict.fromkeys(row.values.get("jurisdiction", "") for row in rows
                                                           if row.values.get("jurisdiction")))}
        for row in ws:
            for cell in row:
                if cell.data_type == "f" or not isinstance(cell.value, str):
                    continue
                matches = list(TOKEN.finditer(cell.value))
                if not matches:
                    continue
                in_body = start <= cell.row < start + height * len(rows)
                person = rows[(cell.row - start) // height] if in_body else None
                def resolve(key):
                    values = {**context, **dict(person.values)} if person else dict(context)
                    values["delivery_day"] = data.day
                    values["meal_label"], values["meal_title"] = data.meal.value, data.meal.value + "명단"
                    for source in (mapping.get(key, ""), options.template_fallbacks.get(key, "")):
                        if source == KEEP:
                            return "{" + key + "}"
                        value = values.get(source, "")
                        if value is None or (isinstance(value, str) and not value.strip()):
                            continue
                        if source.startswith("receives:") or source == "meal_receives":
                            return 1
                        return value
                    return ""
                if len(matches) == 1 and matches[0].group(0) == cell.value:
                    value = resolve(matches[0].group(1))
                else:
                    value = TOKEN.sub(lambda match: str(resolve(match.group(1))), cell.value)
                _write(cell, value, workbook)
        return workbook
    except Exception:
        workbook.close()
        raise
