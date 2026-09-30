"""일자별·식종별 식수 집계와 표시 테스트."""

from __future__ import annotations

import os
from datetime import date
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

import src.services.meal_summary as summary_module
from src.core.invoice_data import InvoiceData
from src.core.meal_types import MealType
from src.main_window import MainWindow
from src.services.meal_summary import build_monthly_meal_summary
from src.widgets.meal_summary_tab import MealSummaryTab


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def fixed_schedule(monkeypatch):
    monkeypatch.setattr(
        summary_module,
        "_get_schedule",
        lambda: {
            MealType.BREAKFAST: {4, 5, 6},
            MealType.DINNER: {0, 1, 2, 3},
            MealType.LUNCH: {0, 1, 2, 3, 4},
        },
    )
    monkeypatch.setattr(summary_module, "get_kr_holidays", lambda _year: set())


def test_summary_uses_schedule_notes_exclusions_and_confirmed_lunch_dates(
    fixed_schedule,
):
    people = [
        {
            "_breakfast": 1,
            "_dinner": 1,
            "_lunch": 0,
            "_note": "6/5조식일시중지",
        },
        {
            "_breakfast": 1,
            "_dinner": 0,
            "_lunch": 1,
            "_note": "",
            "_lunch_dates": ["2026-06-02"],
        },
    ]

    rows = build_monthly_meal_summary(
        people,
        2026,
        6,
        excluded_dates={date(2026, 6, 1)},
    )
    by_date = {row.delivery_date: row for row in rows}

    assert len(rows) == 30
    assert by_date[date(2026, 6, 1)].dinner == 0
    assert by_date[date(2026, 6, 2)].lunch == 1
    assert by_date[date(2026, 6, 5)].breakfast == 1
    assert by_date[date(2026, 6, 6)].breakfast == 2


@pytest.mark.parametrize(
    ("note", "excluded_dates", "expected_breakfast"),
    [
        ("", None, 12),
        ("", {date(2026, 9, 25)}, 11),
        ("9/25조식일시중지", None, 11),
        ("", {date(2026, 9, 26)}, 11),
    ],
)
def test_summary_september_holidays_and_breakfast_exceptions(
    fixed_schedule, monkeypatch, note, excluded_dates, expected_breakfast
):
    holidays = {date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 26)}
    monkeypatch.setattr(summary_module, "get_kr_holidays", lambda _year: holidays)
    rows = build_monthly_meal_summary(
        [{"_breakfast": 1, "_dinner": 1, "_lunch": 1, "_note": note}],
        2026,
        9,
        excluded_dates=excluded_dates,
    )
    by_date = {row.delivery_date: row for row in rows}

    assert sum(row.breakfast for row in rows) == expected_breakfast
    assert sum(row.dinner for row in rows) == 17
    assert sum(row.lunch for row in rows) == 20
    assert by_date[date(2026, 9, 25)].lunch == 0
    assert by_date[date(2026, 9, 25)].breakfast == (
        0 if note or excluded_dates == {date(2026, 9, 25)} else 1
    )


def test_summary_tab_renders_every_date_and_month_total(qapp, fixed_schedule):
    tab = MealSummaryTab()
    tab.set_month(2026, 2)
    tab.set_invoice_data(
        InvoiceData(
            invoices=[{"_breakfast": 1, "_dinner": 0, "_lunch": 0, "_note": ""}],
            year=2026,
            month=2,
        )
    )

    assert tab.table.rowCount() == 29
    assert tab.table.item(28, 0).text() == "월 합계"
    assert int(tab.table.item(28, 2).text().replace(",", "")) > 0
    assert "2026년 2월" in tab.status_label.text()
    tab.close()


def test_main_window_refreshes_summary_for_selected_month():
    payload = InvoiceData(invoices=[{"성명": "홍길동"}], year=2026, month=7)
    window = Mock()
    window.meal_summary_tab.selected_month.return_value = (2026, 7)
    window.invoice_tab.get_daily_delivery_data.return_value = payload
    window._refresh_meal_summary = Mock(
        side_effect=lambda year, month: MainWindow._refresh_meal_summary(
            window, year, month
        )
    )

    MainWindow._on_tab_changed(window, 3)

    window._refresh_meal_summary.assert_called_once_with(2026, 7)
    window.invoice_tab.get_daily_delivery_data.assert_called_once_with(2026, 7)
    window.meal_summary_tab.set_invoice_data.assert_called_once_with(payload)
