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
from src.widgets.base_mapping_dialog import MappingFieldDef, MappingSection
from src.widgets.invoice_tab import _source_mapping_columns, _unify_note_mapping
from src.widgets.lunch_mapping_dialog import LunchMappingDialog
from src.widgets.mapping_dialog import MappingDialog


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_roster_mapping_hides_output_only_columns_and_uses_one_section(qapp):
    template_columns = [
        "성명",
        "주소",
        "조식",
        "석식",
        "중식",
        "계",
        "청구금액",
        "배송 시작일",
        "배송종료일",
        "비고",
    ]

    source_columns = _source_mapping_columns(template_columns)
    dialog = MappingDialog(
        ["이름", "주소지", "조식 대상", "석식 대상", "비고"],
        source_columns,
        flag_keys=("__breakfast", "__dinner", "__note"),
    )

    visible_keys = {
        row.key for row in dialog._rows if isinstance(row, MappingFieldDef)
    }
    sections = [row for row in dialog._rows if isinstance(row, MappingSection)]
    assert source_columns == ["성명", "주소"]
    assert visible_keys == {
        "성명",
        "주소",
        "__breakfast",
        "__dinner",
        "__note",
    }
    assert [section.title for section in sections] == ["명단에서 가져올 컬럼"]
    dialog.close()


def test_one_note_mapping_updates_calculation_and_template_output():
    mapping = _unify_note_mapping(
        {"성명": "이름", "__note": "특이사항"},
        ["성명", "비고"],
    )

    assert mapping["__note"] == "특이사항"
    assert mapping["비고"] == "특이사항"


def test_old_template_note_mapping_migrates_to_single_note_selector():
    mapping = _unify_note_mapping(
        {"성명": "이름", "비고": "예전 비고 컬럼"},
        ["성명", "비고"],
    )

    assert mapping["__note"] == "예전 비고 컬럼"
    assert mapping["비고"] == "예전 비고 컬럼"


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
