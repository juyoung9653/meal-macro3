"""Read populated cells of one worksheet without opening other sheets."""
from __future__ import annotations

from io import BytesIO
import posixpath
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from openpyxl import Workbook
from openpyxl.reader.strings import read_string_table
from openpyxl.utils.cell import coordinate_to_tuple

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _part(archive, sheet_name):
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    sheets = workbook.find(f"{{{_NS}}}sheets")
    sheet = next((s for s in sheets if s.get("name") == sheet_name), None)
    if sheet is None:
        raise ValueError(f"시트를 찾을 수 없습니다: {sheet_name}")
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    relation = next(r for r in relationships if r.get("Id") == sheet.get(f"{{{_REL}}}id"))
    if relation.get("TargetMode") == "External":
        raise ValueError("외부 시트는 지원하지 않습니다.")
    target = relation.get("Target")
    return posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)


def _cell_value(cell, strings):
    formula = cell.find(f"{{{_NS}}}f")
    if formula is not None:
        return "=" + (formula.text or ""), "f"
    kind = cell.get("t", "n")
    value = cell.find(f"{{{_NS}}}v")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{{{_NS}}}t")), "s"
    if value is None or value.text is None:
        return None, kind
    text = value.text
    if kind == "s":
        return strings[int(text)], "s"
    if kind == "b":
        return text == "1", "b"
    if kind in ("str", "e", "d"):
        return text, "s"
    try:
        return int(text), "n"
    except ValueError:
        try:
            return float(text), "n"
        except ValueError:
            return text, "s"


def read_sparse_book(payload, sheet_name, progress=lambda message: None):
    """Read populated cells in just one worksheet; ignore formatted empty cells."""
    with ZipFile(BytesIO(payload)) as archive:
        part = _part(archive, sheet_name)
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            progress("공유 문자열 읽는 중")
            with archive.open("xl/sharedStrings.xml") as stream:
                strings = read_string_table(stream)
        book = Workbook()
        sheet = book.active
        sheet.title = sheet_name
        original = {}
        progress("대상 시트의 값 읽는 중 (빈 서식 셀 제외)")
        with archive.open(part) as stream:
            stack = []
            count = 0
            for event, element in ET.iterparse(stream, events=("start", "end")):
                if event == "start":
                    stack.append(element)
                    continue
                local = element.tag.rsplit("}", 1)[-1]
                if local == "c":
                    coordinate = element.get("r")
                    value, kind = _cell_value(element, strings)
                    if coordinate and value is not None:
                        row, column = coordinate_to_tuple(coordinate)
                        cell = sheet.cell(row, column, value)
                        cell.data_type = kind
                        original[coordinate] = (value, kind)
                    count += 1
                    if count % 100000 == 0:
                        progress(f"대상 시트 XML {count:,}개 셀 확인 / 값 있는 셀 {len(original):,}개")
                # Retain only the descendants needed to decode a cell. Remove
                # completed siblings so a million formatted rows cannot pile up.
                parent = stack[-2] if len(stack) > 1 else None
                if local == "c" or not any(e.tag.rsplit("}", 1)[-1] == "c" for e in stack[:-1]):
                    element.clear()
                    if parent is not None:
                        parent.remove(element)
                stack.pop()
        progress(f"대상 시트 읽기 완료: 값 있는 셀 {len(original):,}개")
        return book

