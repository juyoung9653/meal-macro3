"""Bounded real-popup regression for populated manifest filter freezes."""

import os
from pathlib import Path
import subprocess
import sys


def test_large_manifest_filters_return_and_keep_rows_aligned():
    # A subprocess turns a regressed UI hang into a bounded assertion failure.
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", "from tests.test_driver_filter_responsiveness import _exercise_filters; _exercise_filters()"],
        cwd=root,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONUTF8": "1"},
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _exercise_filters():
    import faulthandler
    from dataclasses import replace
    from datetime import date
    from uuid import uuid4

    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QHeaderView

    from src.desktop_v2.column_search import ColumnValuePicker
    from src.desktop_v2.driver_households import DriverHouseholdsDialog
    from src.domain.roster import Meal
    from src.services.driver_households import DriverHouseholdRow

    faulthandler.dump_traceback_later(10)
    app = QApplication.instance() or QApplication([])
    rows = tuple(
        DriverHouseholdRow(
            uuid4(), None, f"Driver {i % 2}", (uuid4(),),
            (f"Household {i}", "Second\nThird\nFourth" if i % 2 == 0 else "Second"),
            (Meal.DINNER if i % 2 else Meal.BREAKFAST,),
            f"Address {i % 2}", (f"010-{i % 2}",), (f"00{i % 2 + 1}",),
        )
        for i in range(400)
    )
    dialog = DriverHouseholdsDialog(rows, service_day=date(2026, 9, 22), drivers=())
    dialog.show()
    app.processEvents()
    header = dialog._column_search_header
    dialog.table.sortItems(3, Qt.SortOrder.AscendingOrder)
    dialog.table.selectRow(0)
    identity = dialog.table.item(0, 0).data(Qt.ItemDataRole.UserRole)
    # Same-size data replacement must also avoid per-cell auto-height work.
    dialog.replace_rows(tuple(replace(row, contacts=(row.contacts[0] + "\nDetail",)) for row in rows))
    app.processEvents()
    assert dialog.table.currentItem() is not None
    assert dialog.table.item(dialog.table.currentRow(), 0).data(Qt.ItemDataRole.UserRole) == identity
    assert dialog.table.verticalHeader().sectionResizeMode(0) == QHeaderView.ResizeMode.ResizeToContents
    assert dialog.table.rowHeight(0) > dialog.table.rowHeight(len(rows) - 1)

    def picker_for(key):
        header.open_value_filter(key)
        app.processEvents()
        assert header._menu.isVisible()
        return header._menu.findChild(ColumnValuePicker)

    def click(button):
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        app.processEvents()

    for key, column in (("sequence", 3), ("driver", 0), ("members", 1), ("meals", 2), ("address", 4), ("contacts", 5)):
        values = dialog.column_filter_values(key)
        for value in values[:2]:
            picker = picker_for(key)
            click(picker.select_none)
            item = next(picker.list.item(i) for i in range(picker.list.count())
                        if picker.list.item(i).data(Qt.ItemDataRole.UserRole) == value)
            item.setCheckState(Qt.CheckState.Checked)
            click(picker.apply)
            assert not header._menu.isVisible()
            expected = sum(dialog._display_values(row)[column] == value for row in dialog._rows)
            assert dialog.table.rowCount() == expected
            assert all(dialog.table.item(i, column).text() == value for i in range(expected))
            assert header.sortIndicatorSection() == 3
            assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
            assert header.buttons[key].property("filterActive") is True

        picker = picker_for(key)
        picker.search.setText("missing synthetic value")
        click(picker.cancel)
        assert dialog.table.rowCount() == expected
        assert dialog._column_searches.get(key, "") == ""

        picker = picker_for(key)
        picker.search.setText("missing synthetic value")
        click(picker.apply)
        assert dialog.table.rowCount() == 0
        assert "해당" in dialog.empty.text()
        picker = picker_for(key)
        click(picker.reset)
        assert dialog.table.rowCount() == len(rows)
        assert not header._menu.isVisible()
        assert header.buttons[key].property("filterActive") is False

        picker = picker_for(key)
        click(picker.select_none)
        click(picker.apply)
        assert dialog.table.rowCount() == 0
        picker = picker_for(key)
        click(picker.reset)
        assert dialog.table.rowCount() == len(rows)

    dialog.close()
    faulthandler.cancel_dump_traceback_later()
