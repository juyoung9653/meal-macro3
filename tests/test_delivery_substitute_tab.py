"""배송대타 탭의 최대그룹 입력 검증 테스트."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from src.core.delivery_matrix import AnnualDeliveryBook
from src.widgets import delivery_substitute_tab as tab_module


@pytest.fixture
def qapp(tmp_path: Path):
    previous_format = QSettings.defaultFormat()
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path / "settings"),
    )
    app = QApplication.instance() or QApplication([])
    yield app
    QSettings.setDefaultFormat(previous_format)


@pytest.mark.parametrize("value", ["0", "-3"])
def test_on_run_rejects_non_positive_max_groups(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
):
    tab = tab_module.DeliverySubstituteTab()
    warning_calls: list[tuple[str, str]] = []

    def unexpected_make_result(*_args, **_kwargs):
        pytest.fail("비양수 최대그룹 입력으로 결과 생성을 호출하면 안 됩니다.")

    try:
        tab._book = cast(AnnualDeliveryBook, object())
        monkeypatch.setattr(tab, "_selection_problem", lambda: None)
        monkeypatch.setattr(tab, "_selected_day", lambda: date(2026, 7, 28))
        monkeypatch.setattr(tab_module, "make_result", unexpected_make_result)
        monkeypatch.setattr(
            tab_module.QMessageBox,
            "warning",
            lambda _parent, title, message: warning_calls.append((title, message)),
        )
        tab.max_rows_edit.setText(value)

        tab._on_run()

        assert warning_calls == [
            ("오류", "최대그룹에는 1 이상의 정수를 입력해 주세요.")
        ]
    finally:
        tab.close()
