"""Template widths survive selection, native measurement and serialization."""
from dataclasses import replace

import openpyxl
import pytest
from openpyxl.utils import get_column_letter

from src.excel.delivery_templates import create_substitute_template, load_substitute_widths
from src.excel import substitute_excel_metrics as metrics
from src.generators import substitute_generator
from src.services.sqlite_delivery_substitute_service import prepare_delivery_substitute_data
from src.domain.roster import Meal
from tests.test_delivery_substitute_v2 import source, options, DAY, CUSTOM, app
from src.excel.substitute_template_columns import sync_substitute_template, template_column_keys
from src.domain.delivery_substitute import SubstituteColumn, STANDARD_COLUMNS


def test_template_shared_dimensions_and_default_width(tmp_path):
    path = tmp_path / "template.xlsx"
    create_substitute_template(path)
    book = openpyxl.load_workbook(path)
    sheet = book["배송대타"]
    sheet.column_dimensions.group("D", "F")
    sheet.column_dimensions["D"].width = 31.25
    del sheet.column_dimensions["B"]
    sheet.sheet_format.defaultColWidth = 17.5
    book.save(path)
    book.close()
    widths = load_substitute_widths(path)
    assert widths["jurisdiction"] == 17.5
    assert [widths[key] for key in ("detail_address", "road_address", "mobile_phone")] == [31.25] * 3


@pytest.mark.parametrize("orientation", ["portrait", "landscape"])
@pytest.mark.parametrize("edited_width", [None, 19.375])
def test_selected_item_widths_follow_reordering_and_saved_template(tmp_path, monkeypatch, orientation, edited_width):
    path = tmp_path / "template.xlsx"
    create_substitute_template(path)
    if edited_width is not None:
        book = openpyxl.load_workbook(path)
        book["배송대타"].column_dimensions["E"].width = edited_width
        book.save(path)
        book.close()
    snapshot, _ = source()
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST, fallbacks={})
    selected = replace(options(data), columns=("road_address", f"custom:{CUSTOM}", "name"), orientation=orientation)
    expected = (edited_width or 28, 22, 14, 10, 10, 10, 10)
    def measure(workbook, columns, layout, selected, **kwargs):
        assert layout.fixed_column_widths == expected
        assert tuple(workbook.active.column_dimensions[get_column_letter(i)].width
                     for i in range(1, layout.used_cols + 1)) == expected
    monkeypatch.setattr(substitute_generator, "apply_excel_metrics", measure)
    book = substitute_generator.build_substitute_workbook(data, selected, template_path=path)
    output = tmp_path / "output.xlsx"
    book.save(output)
    book.close()
    reopened = openpyxl.load_workbook(output)
    try:
        assert tuple(reopened.active.column_dimensions[get_column_letter(i)].width
                     for i in range(1, 8)) == expected
    finally:
        reopened.close()


@pytest.mark.slow
@pytest.mark.excel
@pytest.mark.parametrize("orientation", ["portrait", "landscape"])
@pytest.mark.parametrize("group_size", [2, 5])
def test_native_template_widths_heights_and_fonts(tmp_path, orientation, group_size):
    path = tmp_path / "template.xlsx"
    create_substitute_template(path)
    template = openpyxl.load_workbook(path)
    template["배송대타"].column_dimensions["G"].width = 18.25
    from openpyxl.styles import Font
    start = 4 if group_size == 2 else 13
    template["배송대타"].cell(start, 7).font = Font(name="Arial", size=15)
    expected_heights = tuple(31 + 10 * offset for offset in range(group_size))
    for offset, height in enumerate(expected_heights):
        template["배송대타"].row_dimensions[start + offset].height = height
    template.save(path)
    template.close()
    snapshot, _ = source(sizes=(group_size,))
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST, fallbacks={})
    key = f"custom:{CUSTOM}"
    data = replace(data, rows=tuple(replace(row, values={**row.values,
        key: " ".join(f"합성메모{i:02d}" for i in range(20))}) for row in data.rows))
    selected = replace(options(data), columns=(key, "name", "road_address"), orientation=orientation)
    sync_substitute_template(path, data.columns, selected.columns)
    book = substitute_generator.build_substitute_workbook(data, selected, template_path=path)
    output = tmp_path / "output.xlsx"
    try:
        assert tuple(book.active.row_dimensions[row].height for row in range(3, 3 + group_size)) == expected_heights
        assert book.active["A3"].font.sz == 15
        assert book.active.page_setup.orientation == orientation
        assert book.active.page_setup.fitToWidth == 1
        book.save(output)
    finally:
        book.close()
    reopened = openpyxl.load_workbook(output)
    try:
        assert [reopened.active.column_dimensions[c].width for c in "ABCDEFG"] == [18.25, 14, 28, 10, 10, 10, 10]
        assert f"A3:A{2 + group_size}" in {str(merged) for merged in reopened.active.merged_cells.ranges}
        assert tuple(reopened.active.row_dimensions[row].height for row in range(3, 3 + group_size)) == expected_heights
    finally:
        reopened.close()


def test_expansion_rename_reorder_and_new_column_preserve_individual_styles(tmp_path, monkeypatch):
    from openpyxl.styles import Font
    from src.excel.delivery_templates import validate_delivery_template
    path = tmp_path / "template.xlsx"
    create_substitute_template(path)
    custom = SubstituteColumn(f"custom:{CUSTOM}", "사용자 비고")
    columns = (*STANDARD_COLUMNS, custom)
    assert sync_substitute_template(path, columns, (custom.key, "name"))
    book = openpyxl.load_workbook(path)
    sheet = book["배송대타"]
    assert template_column_keys(sheet)[:2] == (custom.key, "name")
    assert sheet.row_dimensions[22].hidden and sheet.row_dimensions[23].hidden
    sheet.column_dimensions["A"].width = 35.5
    sheet["A3"].font = Font(name="Arial", size=13, color="123456")
    book.save(path)
    book.close()
    newer = SubstituteColumn("custom:later", "새 항목")
    renamed = replace(custom, label="이름 바꾼 칼럼")
    assert sync_substitute_template(path, (*STANDARD_COLUMNS, renamed, newer), ("road_address", custom.key))
    validate_delivery_template(path, "delivery_substitute")
    book = openpyxl.load_workbook(path)
    sheet = book["배송대타"]
    keys = template_column_keys(sheet)
    assert keys[:2] == ("road_address", custom.key)
    assert keys[-1] == "__summary"
    assert sheet["B2"].value == renamed.label
    assert sheet.column_dimensions["B"].width == 35.5
    assert sheet["B3"].font.name == "Arial" and sheet["B3"].font.sz == 13
    assert sheet.cell(3, keys.index(newer.key) + 1).font.sz == sheet.cell(3, keys.index("note") + 1).font.sz
    book.close()
    before = path.read_bytes()
    assert not sync_substitute_template(path, (*STANDARD_COLUMNS, renamed, newer), ("road_address", custom.key))
    assert path.read_bytes() == before
    # Removed columns retain their format for historical output.
    sync_substitute_template(path, STANDARD_COLUMNS, ("name",))
    assert load_substitute_widths(path)[custom.key] == 35.5
    snapshot, _ = source(sizes=(1,))
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST, fallbacks={})
    selected = replace(options(data), columns=(custom.key, "name"))
    monkeypatch.setattr(substitute_generator, "apply_excel_metrics", lambda *args, **kwargs: None)
    output = substitute_generator.build_substitute_workbook(data, selected, template_path=path)
    try:
        assert output.active.column_dimensions["A"].width == 35.5
        assert output.active["A3"].font.name == "Arial"
        assert output.active["A3"].font.sz == 13
    finally:
        output.close()


def test_editor_uses_saved_checked_order_and_adds_current_columns_only_to_template(tmp_path, app):
    import json
    from PySide6.QtCore import QSettings
    from src.desktop_v2.delivery_template import DeliveryTemplateDialog, DeliveryTemplateStore
    from src.desktop_v2.document_hub import DocumentAction
    from src.desktop_v2.substitute_preferences import preferences
    settings = QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat)
    store = DeliveryTemplateStore("synthetic", settings=settings, root=tmp_path / "forms")
    _, key = preferences("synthetic", settings)
    saved = json.dumps({"columns": ["name", "road_address"], "order": ["telephone", "name", "road_address"]})
    settings.setValue(key, saved)
    opened = []
    dialog = DeliveryTemplateDialog(DocumentAction.DELIVERY_SUBSTITUTE, "synthetic", store=store,
                                    opener=lambda url: opened.append(url) or True)
    dialog.set_substitute_columns((*STANDARD_COLUMNS, SubstituteColumn("custom:new", "새 칼럼")))
    dialog._open()
    assert opened
    book = openpyxl.load_workbook(dialog.path)
    try:
        keys = template_column_keys(book["배송대타"])
        assert keys[:2] == ("name", "road_address")
        assert "custom:new" in keys
    finally:
        book.close()
    assert settings.value(key) == saved
    dialog.reject()
    assert store.selected(DocumentAction.DELIVERY_SUBSTITUTE) is None


def test_failed_atomic_expansion_keeps_existing_template(tmp_path, monkeypatch):
    from src.excel import substitute_template_columns as module
    path = tmp_path / "template.xlsx"
    create_substitute_template(path)
    before = path.read_bytes()
    def locked(*args):
        raise PermissionError("synthetic locked template")
    monkeypatch.setattr(module.os, "replace", locked)
    with pytest.raises(PermissionError):
        sync_substitute_template(path, STANDARD_COLUMNS, ("name",))
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]
