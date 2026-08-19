"""일일배송 탭의 배송 담당/구역 컬럼 선택 테스트."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from src.core.invoice_data import InvoiceData
from src.widgets.daily_delivery_tab import DailyDeliveryTab


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _tab_with_settings(tmp_path, qapp) -> DailyDeliveryTab:
    tab = DailyDeliveryTab()
    tab._settings = QSettings(
        str(tmp_path / "daily-tab.ini"), QSettings.Format.IniFormat
    )
    return tab


def test_column_combos_use_matching_header_names(tmp_path, qapp):
    tab = _tab_with_settings(tmp_path, qapp)
    headers = [
        "성명",
        "배송담당",
        "담당 구역 1순위",
        "담당 구역 2순위",
        "주소",
        "조식",
        "석식",
        "학기중 비고",
    ]
    data = InvoiceData(
        invoices=[{header: "값" for header in headers}],
        roster_headers=headers,
        col_mapping={"주소": "주소"},
    )

    tab.set_invoice_data(data)

    assert tab.driver_combo.currentData() == "배송담당"
    assert tab.region_primary_combo.currentData() == "담당 구역 1순위"
    assert tab.region_secondary_combo.currentData() == "담당 구역 2순위"
    assert tab.address_combo.currentData() == "주소"
    assert tab.breakfast_combo.currentData() == "조식"
    assert tab.dinner_combo.currentData() == "석식"
    assert tab.note_combo.currentData() == "학기중 비고"
    assert tab.generate_btn.isEnabled()
    tab.close()


def test_primary_region_alone_still_enables_generation(tmp_path, qapp):
    tab = _tab_with_settings(tmp_path, qapp)
    headers = ["성명", "배송담당", "담당 구역 1순위", "주소", "조식"]

    tab.set_invoice_data(
        InvoiceData(
            invoices=[{header: "값" for header in headers}],
            roster_headers=headers,
            col_mapping={"주소": "주소"},
        )
    )

    assert tab.region_primary_combo.currentData() == "담당 구역 1순위"
    assert tab.region_secondary_combo.currentData() == ""
    assert tab.breakfast_combo.currentData() == "조식"
    assert tab.generate_btn.isEnabled()
    tab.close()


def test_saved_sheet_columns_restore_without_business_value_hardcoding(tmp_path, qapp):
    tab = _tab_with_settings(tmp_path, qapp)
    tab._settings.setValue("daily/driver_col", "X")
    tab._settings.setValue("daily/region_primary_col", "U")
    tab._settings.setValue("daily/region_secondary_col", "관할동")
    tab._settings.setValue("daily/address_col", "주소")
    headers = ["성명", "U", "X", "관할동", "주소"]

    tab.set_invoice_data(
        InvoiceData(
            invoices=[{header: "값" for header in headers}],
            roster_headers=headers,
        )
    )

    assert tab.driver_combo.currentData() == "X"
    assert tab.region_primary_combo.currentData() == "U"
    assert tab.region_secondary_combo.currentData() == "관할동"
    assert tab.address_combo.currentData() == "주소"
    tab.close()


def test_meal_columns_can_use_arbitrary_sheet_headers(tmp_path, qapp):
    tab = _tab_with_settings(tmp_path, qapp)
    tab._settings.setValue("daily/breakfast_col", "48.0")
    tab._settings.setValue("daily/dinner_col", "44.0")
    tab._settings.setValue("daily/note_col", "메모")
    headers = ["기사", "구역", "주소", "48.0", "44.0", "메모"]
    rows = [
        {"기사": "A", "구역": "가", "주소": "1", "48.0": 1, "44.0": 0},
        {"기사": "A", "구역": "가", "주소": "2", "48.0": 1, "44.0": 1},
    ]

    tab.set_invoice_data(InvoiceData(invoices=rows, roster_headers=headers))

    assert tab.breakfast_combo.currentData() == "48.0"
    assert tab.dinner_combo.currentData() == "44.0"
    assert tab.note_combo.currentData() == "메모"
    assert tab.meal_summary_label.text() == "대상: 조식 2명 · 석식 1명"
    prepared = tab._prepare_people()
    assert [person["_breakfast"] for person in prepared] == [1, 1]
    assert [person["_dinner"] for person in prepared] == [0, 1]
    tab.close()
