"""Concrete delivery worksheet renderer.

This module owns the delivery-sheet rendering boundary: it turns a delivery
DataFrame and typed presentation options into a fully formatted Workbook.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from openpyxl import Workbook

from src.excel.page_formatting.layout import apply_page_formatting
from src.excel.sheet_writer import write_sheet
from src.excel.text_portrait import preprocess_portrait


@dataclass(frozen=True)
class DeliveryPrintPolicy:
    """Print settings used when formatting a delivery worksheet."""

    orientation: int
    split_mode: int
    max_rows: int


@dataclass(frozen=True)
class DeliveryRenderRequest:
    """Presentation data required to render one delivery worksheet."""

    title: str
    meal: str
    staff: str
    date: str
    print_policy: DeliveryPrintPolicy


class DeliverySheetRenderer:
    """Render a delivery DataFrame into one formatted Excel workbook."""

    def render(self, df: pd.DataFrame, request: DeliveryRenderRequest) -> Workbook:
        """Create a workbook after preprocessing, writing, and formatting its sheet."""
        workbook = Workbook()
        worksheet = workbook.active
        if worksheet is None:
            worksheet = workbook.create_sheet()

        # Excel worksheet names are limited to 31 characters.
        worksheet.title = request.title[:31]

        processed = preprocess_portrait(df)
        write_sheet(
            worksheet,
            processed,
            meal=request.meal,
            ship=request.staff,
            date_str=request.date,
        )
        apply_page_formatting(
            worksheet,
            processed,
            mode=request.print_policy.split_mode,
            max_groups_limit=request.print_policy.max_rows,
            orientation_pref=request.print_policy.orientation,
        )
        return workbook
