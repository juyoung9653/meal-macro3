from dataclasses import replace
from datetime import date
from time import sleep
from uuid import uuid4

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from src.desktop_v2.monthly_meal_history import MonthlyMealHistoryDialog
from src.desktop_v2.workspace_tools import MonthlyOverviewDialog
from src.services.monthly_meal_history import MonthlyMealHistory
from src.services.monthly_meal_statistics import build_monthly_meal_statistics
from tests.test_monthly_meal_history import _filter_entries, _visible_names
from tests.test_monthly_meal_statistics import _snapshot


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_default_revision_order_numeric_sort_cycle_and_multiple_columns(app):
    entries = tuple(replace(item, name="같은 이름" if index < 2 else "다른 이름", revision_code=code)
                    for index, (item, code) in enumerate(zip(_filter_entries(),
                        ("20261001-999", "20261001-1000", "20261002-001"))))
    dialog = MonthlyMealHistoryDialog(date(2026, 10, 1),
        lambda y, m: MonthlyMealHistory(uuid4(), y, m, entries))
    codes = lambda: [dialog.table.item(row, 7).text() for row in range(dialog.table.rowCount())]
    default = ["20261002-001", "20261001-1000", "20261001-999"]
    assert codes() == default
    header = dialog._column_search_header
    header.sectionClicked.emit(7)
    assert codes() == list(reversed(default))
    header.sectionClicked.emit(7)
    assert codes() == default
    header.sectionClicked.emit(7)
    assert not dialog._sort_criteria and codes() == default
    header.sectionClicked.emit(0)
    header.sectionClicked.emit(7)
    assert dialog._sort_criteria == (("name", Qt.AscendingOrder), ("revision", Qt.AscendingOrder))
    assert codes()[:2] == ["20261001-999", "20261001-1000"]
    dialog._set_column_search("name", "같은")
    assert _visible_names(dialog) == ("같은 이름", "같은 이름")
    dialog._shift_month(1)
    assert len(dialog._sort_criteria) == 2
    assert _visible_names(dialog) == ("같은 이름", "같은 이름")
    dialog.close()


@pytest.mark.parametrize("failed", [False, True])
def test_monthly_window_returns_to_front_after_delayed_progress(app, monkeypatch, failed):
    from src.desktop_v2 import startup_loading as progress
    monkeypatch.setattr(progress, "PROGRESS_DELAY_MS", 10)
    snapshot, _ = _snapshot(include_driver=False)
    owner, shown = [], []
    show = progress.StartupLoadingDialog.show
    def track_show(dialog):
        shown.append(dialog.parentWidget())
        show(dialog)
    monkeypatch.setattr(progress.StartupLoadingDialog, "show", track_show)
    def load_history(year, month):
        def read():
            sleep(0.05)
            if failed:
                raise ValueError("합성 조회 오류")
            return MonthlyMealHistory(snapshot.revision_id, year, month, _filter_entries())
        return progress.run_progress_task(read, message="조회 중", parent=owner[0])
    main = QWidget()
    main.show()
    dialog = MonthlyOverviewDialog(date(2026, 10, 1),
        lambda y, m: build_monthly_meal_statistics(snapshot, y, m), load_history, parent=main)
    owner.append(dialog)
    dialog.show()
    app.processEvents()
    activations = []
    activate = dialog.activateWindow
    def record_activation():
        activations.append(True)
        activate()
    monkeypatch.setattr(dialog, "activateWindow", record_activation)
    dialog.tabs.setCurrentIndex(1)
    app.processEvents()
    assert shown == [dialog]
    assert activations and dialog.isVisible() and dialog.isEnabled()
    assert dialog.tabs.currentIndex() == 1
    assert dialog.history.error.isHidden() is (not failed)
    dialog.close()
    main.close()
