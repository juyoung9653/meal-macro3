"""Confirmation delivery selection uses the invoice value without phone mapping."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from src.core.invoice_data import InvoiceData
from src.widgets import confirm_tab


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_selected_delivery_column_passes_first_value_and_ignores_legacy_phone(
    tmp_path, qapp, monkeypatch
):
    monkeypatch.setattr(confirm_tab.ConfirmTab, "_restore_state", lambda self: None)
    tab = confirm_tab.ConfirmTab()
    tab._settings = QSettings(str(tmp_path / "confirm.ini"), QSettings.Format.IniFormat)
    tab._settings.setValue("confirm/delivery_phone_map", {"기사": "010-1111-2222"})
    tab._settings.setValue("confirm/delivery_col", "배송담당")
    tab.output_dir = str(tmp_path)
    tab.set_invoice_data(InvoiceData(
        invoices=[{"배송담당": "기사", "_bf_cnt": 1}],
        roster_headers=["배송담당"], year=2026, month=9,
    ))
    captured = {}
    monkeypatch.setattr(confirm_tab, "make_confirm", lambda **kwargs: captured.update(kwargs) or [])
    monkeypatch.setattr(confirm_tab.QMessageBox, "information", lambda *args: None)

    tab._generate_confirms()

    assert tab.delivery_col_combo.currentData() == "배송담당"
    assert not hasattr(tab, "delivery_table")
    assert captured["delivery_person"] == "기사"
    assert captured["delivery_phone"] == ""
    assert tab._settings.value("confirm/delivery_phone_map") == {"기사": "010-1111-2222"}


def test_unselected_delivery_column_passes_blank(tmp_path, qapp, monkeypatch):
    monkeypatch.setattr(confirm_tab.ConfirmTab, "_restore_state", lambda self: None)
    tab = confirm_tab.ConfirmTab()
    tab._settings = QSettings(str(tmp_path / "confirm.ini"), QSettings.Format.IniFormat)
    tab.output_dir = str(tmp_path)
    tab.set_invoice_data(InvoiceData(
        invoices=[{"배송담당": "기사", "_bf_cnt": 1}],
        roster_headers=["배송담당"], year=2026, month=9,
    ))
    captured = {}
    monkeypatch.setattr(confirm_tab, "make_confirm", lambda **kwargs: captured.update(kwargs) or [])
    monkeypatch.setattr(confirm_tab.QMessageBox, "information", lambda *args: None)

    tab._generate_confirms()

    assert captured["delivery_person"] == ""
    assert captured["delivery_phone"] == ""
