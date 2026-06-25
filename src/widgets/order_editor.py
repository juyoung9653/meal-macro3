"""식종별 배송담당 순서 편집 다이얼로그.

cache.py → QSettings 기반 저장으로 변경.
"""

from __future__ import annotations

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

SETTINGS_ORG = "분당구"
SETTINGS_APP = "급식비 정산 프로그램"


class StaffOrderDialog(QDialog):
    """식종(조식/중식/석식)별 배송담당 출력 순서 편집.

    Args:
        staff_by_meal: {식종: [담당자명1, ...]}
        parent: 부모 위젯.
    """

    def __init__(self, staff_by_meal: dict[str, list[str]], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("배송담당 순서 편집")
        self.resize(520, 480)

        self._staff_by_meal = staff_by_meal
        self._lists: dict[str, QListWidget] = {}
        self._result: dict[str, list[str]] = {}
        self._settings = QSettings(SETTINGS_ORG, SETTINGS_APP)

        self._build_ui()
        self._load_cache()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        tabs = QTabWidget()

        for meal in ["조식", "중식", "석식"]:
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)

            label = QLabel(
                f"「{meal}」담당자 표시 순서\n"
                "항목을 선택 후 위/아래 버튼으로 이동하세요."
            )
            tab_layout.addWidget(label)

            lw = QListWidget()
            lw.setDragDropMode(QListWidget.DragDropMode.InternalMove)
            self._lists[meal] = lw
            for name in self._staff_by_meal.get(meal, []):
                lw.addItem(QListWidgetItem(name))
            tab_layout.addWidget(lw)

            # 위/아래 버튼
            btn_row = QHBoxLayout()
            btn_up = QPushButton("▲ 위로")
            btn_up.clicked.connect(lambda _, w=lw: self._move(w, -1))
            btn_down = QPushButton("▼ 아래로")
            btn_down.clicked.connect(lambda _, w=lw: self._move(w, 1))
            btn_row.addWidget(btn_up)
            btn_row.addWidget(btn_down)
            btn_row.addStretch()
            tab_layout.addLayout(btn_row)

            tabs.addTab(tab, meal)

        layout.addWidget(tabs)

        # 하단 버튼
        bottom = QHBoxLayout()
        bottom.addStretch()
        btn_save = QPushButton("저장")
        btn_save.clicked.connect(self._on_save)
        btn_cancel = QPushButton("취소")
        btn_cancel.clicked.connect(self.reject)
        bottom.addWidget(btn_save)
        bottom.addWidget(btn_cancel)
        layout.addLayout(bottom)

    @staticmethod
    def _move(lw: QListWidget, direction: int) -> None:
        row = lw.currentRow()
        if row < 0:
            return
        new_row = row + direction
        if new_row < 0 or new_row >= lw.count():
            return
        item = lw.takeItem(row)
        lw.insertItem(new_row, item)
        lw.setCurrentRow(new_row)

    def _load_cache(self) -> None:
        data: dict[str, list[str]] = {}
        for meal in ["조식", "중식", "석식"]:
            key = f"report/staff_order_{meal}"
            val = self._settings.value(key)
            if val is not None:
                try:
                    import json

                    if isinstance(val, str):
                        data[meal] = json.loads(val)
                    elif isinstance(val, list):
                        data[meal] = val
                except Exception:
                    pass

        if not data:
            return

        for meal in ["조식", "중식", "석식"]:
            cached: list[str] = data.get(meal, [])
            lw = self._lists.get(meal)
            if not lw:
                continue
            current = {lw.item(i).text() for i in range(lw.count())}
            ordered = [n for n in cached if n in current]
            remaining = [
                lw.item(i).text()
                for i in range(lw.count())
                if lw.item(i).text() not in ordered
            ]
            lw.clear()
            for name in ordered + remaining:
                lw.addItem(name)

    def _on_save(self) -> None:
        import json

        result: dict[str, list[str]] = {}
        for meal in ["조식", "중식", "석식"]:
            lw = self._lists.get(meal)
            if lw:
                items = [lw.item(i).text() for i in range(lw.count())]
                result[meal] = items
                key = f"report/staff_order_{meal}"
                self._settings.setValue(key, json.dumps(items, ensure_ascii=False))

        self._settings.sync()
        self._result = result
        self.accept()

    @property
    def staff_sort(self) -> dict[str, list[str]]:
        return self._result
