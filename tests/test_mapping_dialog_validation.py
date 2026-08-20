"""컬럼 매핑 다이얼로그의 필수 항목 검증 테스트."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QDialog

import src.widgets.base_mapping_dialog as base_mapping_module
from src.widgets.lunch_mapping_dialog import LunchMappingDialog


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
