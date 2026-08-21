"""청구서 생성 없이 현재 명단을 일일배송 탭에 전달하는 테스트."""

from __future__ import annotations

import json
import os
from datetime import date
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

import src.widgets.invoice_tab as invoice_tab_module
from src.core.invoice_data import InvoiceData
from src.generators.daily_delivery_generator import (
    DAILY_ADDRESS_KEY,
    DAILY_DRIVER_KEY,
    DAILY_REGION_PRIMARY_KEY,
)
from src.main_window import MainWindow
from src.services.invoice_controller import LUNCH_PERSON_REQUIRED_FIELDS
from src.services.lunch_data import LunchRoster, LunchSchoolEntry

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


def test_lunch_roster_is_independent_and_becomes_daily_delivery_data(
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
    tab.year_spin.setValue(2025)
    tab.month_spin.setValue(6)
    tab._loader.roster_data = []
    lunch_person = {
        "구분": "아동",
        "학교명": "분당초",
        "학년": "3",
        "성명": "중식아동",
        "생년월일": "2010-01-01",
        "세대주성명": "보호자",
        "아동과의관계": "자녀",
        "관할동": "정자동",
        "주소": "정자동 1-1",
        "핸드폰": "010-0000-0000",
        "배송담당": "기사 A",
        "담당 구역 1순위": "정자 구역",
    }
    lunch_mapping = {
        field_name: field_name for field_name in LUNCH_PERSON_REQUIRED_FIELDS
    }
    lunch_mapping.update(
        {
            DAILY_DRIVER_KEY: "배송담당",
            DAILY_REGION_PRIMARY_KEY: "담당 구역 1순위",
            "__note": "",
        }
    )
    tab._lunch_person_loader.roster_data = [lunch_person]
    tab._lunch_person_loader.roster_headers = list(lunch_person)
    tab._lunch_person_mapping = lunch_mapping
    tab._controller.set_lunch_people(
        [lunch_person], list(lunch_person), lunch_mapping, "lunch.xlsx"
    )
    tab._controller.set_lunch_roster(
        LunchRoster(
            entries=[
                LunchSchoolEntry(
                    school_name="분당초",
                    start_date=date(2025, 6, 2),
                    end_date=date(2025, 6, 6),
                )
            ]
        ),
        {},
        "schedule.xlsx",
    )

    tab._update_generate_enabled()
    daily_data = tab.get_daily_delivery_data()

    assert tab.generate_btn.isEnabled()
    assert len(daily_data.invoices) == 1
    assert daily_data.invoices[0]["_lunch"] == 1
    assert daily_data.invoices[0][DAILY_DRIVER_KEY] == "기사 A"
    assert daily_data.invoices[0][DAILY_ADDRESS_KEY] == "정자동 1-1"
    assert daily_data.preparation_error is None
    assert tab.provider_name_edit.text() == "행복도시락"
    tab.close()


def test_saved_mapping_survives_restart_and_beats_stale_result_cache(
    tmp_path, monkeypatch, qapp
):
    settings_path = tmp_path / "persistent-mapping.ini"
    monkeypatch.setattr(
        invoice_tab_module,
        "QSettings",
        lambda *_args: QSettings(str(settings_path), QSettings.Format.IniFormat),
    )
    monkeypatch.setattr(
        invoice_tab_module, "get_settings", lambda: make_settings_manager()
    )

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    saved_mapping = {
        "성명": "이름",
        "생년월일": "생일",
        "__breakfast": "조식 대상",
        "__dinner": "석식 대상",
        "__note": "특이사항",
    }

    first_tab = invoice_tab_module.InvoiceTab()
    first_tab._controller.set_col_mapping(saved_mapping)
    first_tab._controller.output_dir = str(output_dir)
    first_tab._save_settings()
    first_tab.close()

    stale_payload = {
        "invoices": [{"성명": "캐시 대상자"}],
        "col_mapping": {
            "성명": "오래된 이름 컬럼",
            "생년월일": "오래된 생일 컬럼",
            "__breakfast": "오래된 조식 컬럼",
        },
        "year": 2025,
        "month": 1,
        "excluded_dates": [],
        "roster_headers": [],
        "roster_path": None,
    }
    (output_dir / ".invoice_data_cache.json").write_text(
        json.dumps(stale_payload, ensure_ascii=False), encoding="utf-8"
    )

    restarted_tab = invoice_tab_module.InvoiceTab()
    restored = restarted_tab.get_col_mapping()

    for key, value in saved_mapping.items():
        assert restored[key] == value
    note_template_columns = [
        column for column in restarted_tab._all_template_columns if "비고" in column
    ]
    for column in note_template_columns:
        assert restored[column] == saved_mapping["__note"]
    persisted = QSettings(
        str(settings_path), QSettings.Format.IniFormat
    ).value("invoice/col_mapping")
    for key, value in saved_mapping.items():
        assert persisted[key] == value
    for column in note_template_columns:
        assert persisted[column] == saved_mapping["__note"]
    restarted_tab.close()
