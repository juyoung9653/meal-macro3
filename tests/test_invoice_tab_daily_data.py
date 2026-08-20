"""청구서 생성 없이 현재 명단을 일일배송 탭에 전달하는 테스트."""

from __future__ import annotations

import os
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

import src.widgets.invoice_tab as invoice_tab_module
from src.core.invoice_data import InvoiceData
from src.main_window import MainWindow

from .conftest import make_settings_manager


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_current_roster_becomes_daily_data_without_invoice_generation(
    tmp_path, monkeypatch, qapp
):
    ui_settings = QSettings(
        str(tmp_path / "invoice-tab.ini"), QSettings.Format.IniFormat
    )
    monkeypatch.setattr(invoice_tab_module, "QSettings", lambda *_args: ui_settings)
    monkeypatch.setattr(
        invoice_tab_module, "get_settings", lambda: make_settings_manager()
    )
    tab = invoice_tab_module.InvoiceTab()
    headers = ["성명", "조식", "석식", "학기중 비고", "X", "U", "관할동", "주소"]
    tab._controller.set_roster_headers(headers)
    tab._controller.set_roster_path("roster.xlsx")
    tab._loader.roster_data = [
        {
            "성명": "홍길동",
            "조식": 1,
            "석식": 1,
            "학기중 비고": "5/2석식일시중지",
            "X": "기사 A",
            "U": "1순위 구역",
            "관할동": "2순위 구역",
            "주소": "주소",
        }
    ]

    data = tab.get_daily_delivery_data()

    assert tab.get_invoices() == []
    assert len(data.invoices) == 1
    assert data.invoices[0]["_breakfast"] == 1
    assert data.invoices[0]["_dinner"] == 1
    assert data.invoices[0]["_note"] == "5/2석식일시중지"
    assert data.invoices[0]["X"] == "기사 A"
    assert data.col_mapping["__breakfast"] == "조식"
    assert data.col_mapping["__dinner"] == "석식"
    assert data.col_mapping["__note"] == "학기중 비고"
    tab.close()


def test_main_window_uses_roster_based_data_for_daily_tab():
    payload = InvoiceData(invoices=[{"성명": "홍길동"}])
    window = Mock()
    window.invoice_tab.get_daily_delivery_data.return_value = payload

    MainWindow._on_tab_changed(window, 2)

    window.invoice_tab.get_daily_delivery_data.assert_called_once_with()
    window.invoice_tab.get_invoice_data.assert_not_called()
    window.daily_delivery_tab.set_invoice_data.assert_called_once_with(payload)


def test_lunch_roster_is_independent_and_can_enable_invoice_generation(
    tmp_path, monkeypatch, qapp
):
    ui_settings = QSettings(
        str(tmp_path / "separate-rosters.ini"), QSettings.Format.IniFormat
    )
    monkeypatch.setattr(invoice_tab_module, "QSettings", lambda *_args: ui_settings)
    monkeypatch.setattr(
        invoice_tab_module, "get_settings", lambda: make_settings_manager()
    )
    tab = invoice_tab_module.InvoiceTab()
    tab._loader.roster_data = []
    tab._lunch_person_loader.roster_data = [
        {"성명": "중식아동", "생년월일": "2010-01-01"}
    ]

    tab._update_generate_enabled()

    assert tab.generate_btn.isEnabled()
    assert tab.get_daily_delivery_data().invoices == []
    assert tab.provider_name_edit.text() == "행복도시락"
    tab.close()
