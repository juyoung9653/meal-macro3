"""Copy editable worksheet presentation between template and output workbooks."""

from copy import copy
from openpyxl.cell.cell import MergedCell


def copy_template_sheet(source, target):
    for row in source.iter_rows():
        for cell in row:
            output = target[cell.coordinate]
            if not isinstance(cell, MergedCell):
                output.value = cell.value
                if cell.comment is not None:
                    output.comment = copy(cell.comment)
                if cell.hyperlink is not None:
                    output._hyperlink = copy(cell.hyperlink)
            _copy_style(cell, output)
    for key, dimension in source.column_dimensions.items():
        target.column_dimensions[key] = copy(dimension)
        target.column_dimensions[key].parent = target
        _copy_style(dimension, target.column_dimensions[key])
    for key, dimension in source.row_dimensions.items():
        target.row_dimensions[key] = copy(dimension)
        target.row_dimensions[key].parent = target
        _copy_style(dimension, target.row_dimensions[key])
    for area in source.merged_cells.ranges:
        target.merge_cells(str(area))
    for key in ('sheet_format', 'sheet_properties', 'page_margins', 'page_setup',
                'print_options', 'oddHeader', 'oddFooter', 'evenHeader', 'evenFooter',
                'firstHeader', 'firstFooter', 'row_breaks', 'col_breaks'):
        setattr(target, key, copy(getattr(source, key)))
    target.page_setup._parent = target
    target.print_title_rows = source.print_title_rows
    target.print_title_cols = source.print_title_cols
    target.freeze_panes = source.freeze_panes
    target.sheet_view.showGridLines = source.sheet_view.showGridLines
    target.sheet_view.zoomScale = source.sheet_view.zoomScale
    target.sheet_view.zoomScaleNormal = source.sheet_view.zoomScaleNormal
    target.sheet_state = source.sheet_state


def _copy_style(source, target):
    if source.has_style:
        for key in ('font', 'fill', 'border', 'alignment', 'protection'):
            setattr(target, key, copy(getattr(source, key)))
        target.number_format = source.number_format
