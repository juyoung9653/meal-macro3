"""Mapping controls for user-authored delivery placeholder workbooks."""
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSpinBox, QTreeWidget, QTreeWidgetItem, QComboBox, QHeaderView
from src.domain.delivery_substitute import DELIVERY_SOURCES
from src.excel.mapped_delivery_template import KEEP


class DeliveryPlaceholderMapping(QWidget):
    def __init__(self, form, columns, saved=None, parent=None, *, legacy_primary=None):
        super().__init__(parent)
        self.form = form
        saved = saved or {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(f"{form.sheet} · 양식의 {{플레이스홀더}}에 넣을 항목을 선택하세요.\n"
                       f"플레이스홀더가 가장 많은 {form.suggested_row}행을 복사 행으로 제안합니다. 같은 개수이면 위쪽 행을 제안합니다.")
        label.setWordWrap(True)
        layout.addWidget(label)
        row = QHBoxLayout()
        row.addWidget(QLabel("복사·끼워넣기 범위"))
        self.start, self.end = QSpinBox(), QSpinBox()
        saved_rows = saved.get("template_rows")
        valid_rows = (isinstance(saved_rows, (list, tuple)) and len(saved_rows) == 2
                      and all(type(value) is int for value in saved_rows)
                      and 1 <= saved_rows[0] <= saved_rows[1] <= form.max_row)
        for index, spin in enumerate((self.start, self.end)):
            spin.setRange(1, form.max_row)
            spin.setValue(saved_rows[index] if valid_rows else form.suggested_row)
            spin.setSuffix("행")
            spin.setAccessibleName("복사 시작 행" if index == 0 else "복사 끝 행")
            row.addWidget(spin)
            if index == 0:
                row.addWidget(QLabel("~"))
        row.addStretch(1)
        layout.addLayout(row)
        self.table = QTreeWidget()
        self.table.setRootIsDecorated(False)
        self.table.setColumnCount(3)
        self.table.setHeaderLabels(["양식의 플레이스홀더", "1순위", "2순위 · 1순위가 비면"])
        self.table.header().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.combos = {}
        self.fallback_combos = {}
        sources = {column.key: column.label for column in (*columns, *DELIVERY_SOURCES)}
        defaults = {**{label: key for key, label in sources.items()},
                    **{key: value for key, value in form.defaults.items() if value}}
        mappings = saved.get("template_mapping", {})
        if not isinstance(mappings, dict):
            mappings = {}
        fallbacks = saved.get("template_fallbacks", {})
        if not isinstance(fallbacks, dict):
            fallbacks = {}
        for token in form.targets:
            item = QTreeWidgetItem(["{" + token + "}", ""])
            self.table.addTopLevelItem(item)
            combo = QComboBox()
            combo.setAccessibleName("{" + token + "} 연결 항목")
            combo.addItem("비움", "")
            combo.addItem("플레이스홀더 그대로 유지", KEEP)
            for key, text in sources.items():
                combo.addItem(text, key)
            value = mappings.get(token, defaults.get(token, ""))
            fallback = fallbacks.get(token, "")
            legacy_key = "road_address" if value == "full_address" else value
            if token not in mappings and legacy_key in (legacy_primary or {}):
                value, fallback = legacy_primary[legacy_key], legacy_key
            index = combo.findData(value)
            combo.setCurrentIndex(index if index >= 0 else 0)
            self.table.setItemWidget(item, 1, combo)
            self.combos[token] = combo
            secondary = QComboBox()
            secondary.setAccessibleName("{" + token + "} 2순위")
            secondary.addItem("사용 안 함", "")
            for key, text in sources.items():
                secondary.addItem(text, key)
            index = secondary.findData(fallback)
            secondary.setCurrentIndex(index if index >= 0 else 0)
            self.table.setItemWidget(item, 2, secondary)
            self.fallback_combos[token] = secondary
        layout.addWidget(self.table, 1)
        hint = QLabel("대상자마다 선택한 행을 복사해 끼워 넣습니다. 글꼴·색·테두리·정렬·표시 형식·행 높이·열 너비·인쇄 설정은 Excel 양식을 따릅니다.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    def mapping(self):
        return {token: combo.currentData() for token, combo in self.combos.items()}

    def rows(self):
        return self.start.value(), self.end.value()

    def fallbacks(self):
        return {token: combo.currentData() for token, combo in self.fallback_combos.items() if combo.currentData()}
