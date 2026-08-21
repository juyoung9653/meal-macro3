"""컬럼 매핑 다이얼로그의 필수 항목 검증 테스트."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QDialog

import src.widgets.base_mapping_dialog as base_mapping_module
from src.generators.daily_delivery_generator import (
    DAILY_DRIVER_KEY,
    DAILY_REGION_PRIMARY_KEY,
)
from src.services.invoice_controller import LUNCH_PERSON_REQUIRED_FIELDS
from src.widgets.base_mapping_dialog import MappingFieldDef
from src.widgets.lunch_mapping_dialog import LunchMappingDialog
from src.widgets.mapping_dialog import MappingDialog


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_schedule_dialog_rejects_missing_required_columns(
    monkeypatch, qapp
):
    warnings = []
    monkeypatch.setattr(
        base_mapping_module.QMessageBox,
        "warning",
        lambda *args: warnings.append(args),
    )
    dialog = LunchMappingDialog(["학교명", "시작일", "종료일"])

    dialog._accept()

    assert dialog.result() == QDialog.DialogCode.Rejected
    assert warnings

    dialog.set_values(
        {
            "school_name_col": "학교명",
            "start_date_col": "시작일",
            "end_date_col": "종료일",
        }
    )
    dialog._accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    dialog.close()


def test_lunch_person_dialog_requires_invoice_and_daily_delivery_columns(
    monkeypatch, qapp
):
    warnings = []
    monkeypatch.setattr(
        base_mapping_module.QMessageBox,
        "warning",
        lambda *args: warnings.append(args),
    )
    headers = [
        *LUNCH_PERSON_REQUIRED_FIELDS,
        "배송담당",
        "담당 구역 1순위",
    ]
    required = LUNCH_PERSON_REQUIRED_FIELDS + (
        DAILY_DRIVER_KEY,
        DAILY_REGION_PRIMARY_KEY,
    )
    dialog = MappingDialog(
        headers,
        list(LUNCH_PERSON_REQUIRED_FIELDS),
        flag_keys=(DAILY_DRIVER_KEY, DAILY_REGION_PRIMARY_KEY, "__note"),
        required_keys=required,
    )

    required_rows = {
        row.key
        for row in dialog._rows
        if isinstance(row, MappingFieldDef) and row.required
    }
    assert required_rows == set(required)

    dialog._accept()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert warnings

    mapping = {field_name: field_name for field_name in LUNCH_PERSON_REQUIRED_FIELDS}
    mapping.update(
        {
            DAILY_DRIVER_KEY: "배송담당",
            DAILY_REGION_PRIMARY_KEY: "담당 구역 1순위",
        }
    )
    dialog.set_values(mapping)
    dialog._accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    dialog.close()
