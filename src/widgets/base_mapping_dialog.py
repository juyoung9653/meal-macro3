"""Generic column mapping dialog base — shared by MappingDialog & LunchMappingDialog."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


class _NoWheelComboBox(QComboBox):
    """마우스 휠로 선택값 바뀌는 것 방지"""

    def wheelEvent(self, event: QWheelEvent):
        event.ignore()


@dataclass
class MappingFieldDef:
    """A single field to map in the dialog.

    Attributes:
        key: Internal field name (e.g. ``"__breakfast"``).
        label: UI label text.
        tooltip: Optional tooltip shown on the combo box.
        search_terms: Terms for difflib auto-detection.
        combo_column: Which combo column (~ 0-based index into ``column_labels``)
            this field's combo box lives in.
        source_index: Index into ``headers_list`` whose headers are used for
            this field's combo box.
        required: Whether the dialog must have a selected source column.
    """

    key: str
    label: str
    tooltip: str = ""
    search_terms: list[str] = field(default_factory=list)
    combo_column: int = 0
    source_index: int = 0
    required: bool = False


@dataclass
class MappingSection:
    """A section separator row in the dialog table."""

    title: str


RowDef = MappingFieldDef | MappingSection


class BaseMappingDialog(QDialog):
    """Generic column mapping dialog.

    Subclasses create a list of :class:`RowDef` items (mix of
    :class:`MappingFieldDef` and :class:`MappingSection`) and call
    ``super().__init__(title, rows, headers_list, column_labels, parent)``.
    """

    def __init__(
        self,
        title: str,
        rows: list[RowDef],
        headers_list: list[list[str]],
        column_labels: list[str],
        difflib_cutoff: float = 0.5,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(650)
        self.resize(650, 500)

        self._rows = rows
        self._headers_list = headers_list
        self._column_labels = column_labels
        self._difflib_cutoff = difflib_cutoff
        self._combo_map: dict[str, QComboBox] = {}
        self._setup_ui()

    # ── UI construction ────────────────────────────────────────────

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        info = QLabel(self._info_text())
        info.setStyleSheet("color: #555; margin-bottom: 8px;")
        info.setWordWrap(True)
        layout.addWidget(info)

        num_combo = len(self._column_labels)
        num_cols = 1 + num_combo  # label column + combo columns

        table = QTableWidget()
        table.setColumnCount(num_cols)
        headers = ["항목"] + list(self._column_labels)
        table.setHorizontalHeaderLabels(headers)
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for c in range(num_combo):
            hdr.setSectionResizeMode(1 + c, QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)

        row_idx = 0
        for row_def in self._rows:
            if isinstance(row_def, MappingSection):
                row_idx = self._add_section_row(table, row_idx, row_def, num_cols)
            else:
                row_idx = self._add_field_row(table, row_idx, row_def, num_combo)

        layout.addWidget(table, stretch=1)

        # ── Buttons ──
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        reset_btn = QPushButton("기본값으로 초기화")
        reset_btn.clicked.connect(self._reset_defaults)
        ok_btn = QPushButton("확인")
        ok_btn.clicked.connect(self._accept)
        cancel_btn = QPushButton("취소")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(reset_btn)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def _info_text(self) -> str:
        """Override in subclass to provide custom info label text."""
        return "각 항목에 대응되는 컬럼을 선택하세요."

    def _make_combo(self, headers: list[str]) -> QComboBox:
        """Create a combo box populated with the given header list."""
        combo = _NoWheelComboBox()
        combo.setMinimumWidth(200)
        combo.addItem("(선택 안 함)", "")
        for h in headers:
            combo.addItem(h, h)
        return combo

    def _add_section_row(
        self, table: QTableWidget, row: int, section: MappingSection, num_cols: int
    ) -> int:
        """Insert a section separator row."""
        table.insertRow(row)
        item = QTableWidgetItem(f"── {section.title} ──")
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        f = item.font()
        f.setBold(True)
        item.setFont(f)
        table.setItem(row, 0, item)
        table.setSpan(row, 0, 1, num_cols)
        return row + 1

    def _add_field_row(
        self, table: QTableWidget, row: int, field: MappingFieldDef, num_combo: int
    ) -> int:
        """Insert a field mapping row with combos / N/A cells."""
        table.insertRow(row)

        # Label cell
        label = f"{field.label} *" if field.required else field.label
        label_item = QTableWidgetItem(label)
        label_item.setFlags(label_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(row, 0, label_item)

        # Combo columns
        for c in range(num_combo):
            if c == field.combo_column:
                combo = self._make_combo(self._headers_list[field.source_index])
                if field.tooltip:
                    combo.setToolTip(field.tooltip)
                table.setCellWidget(row, 1 + c, combo)
                self._combo_map[field.key] = combo
            else:
                na_item = QTableWidgetItem("(해당없음)")
                na_item.setFlags(na_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                na_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                table.setItem(row, 1 + c, na_item)

        return row + 1

    # ── Public API ─────────────────────────────────────────────────

    def _reset_defaults(self):
        """Auto-detect mapping for every field using difflib."""
        auto_map: dict[str, str] = {}
        for row_def in self._rows:
            if isinstance(row_def, MappingFieldDef) and row_def.search_terms:
                headers = self._headers_list[row_def.source_index]
                best = self._best_match(row_def.search_terms, headers)
                auto_map[row_def.key] = best
        self.set_values(auto_map)

    def set_values(self, mapping: dict):
        """Set dialog state from a saved mapping dict."""
        for key, value in mapping.items():
            combo = self._combo_map.get(key)
            if combo:
                idx = combo.findText(value)
                if idx >= 0:
                    combo.setCurrentIndex(idx)

    def get_values(self) -> dict:
        """Read current mapping state from the dialog."""
        return {key: combo.currentData() for key, combo in self._combo_map.items()}

    def _accept(self):
        """Validate and accept the dialog."""
        missing = [
            row_def.label
            for row_def in self._rows
            if isinstance(row_def, MappingFieldDef)
            and row_def.required
            and not self._combo_map[row_def.key].currentData()
        ]
        if missing:
            QMessageBox.warning(
                self,
                "필수 컬럼 선택",
                "다음 필수 컬럼을 선택하세요:\n" + ", ".join(missing),
            )
            return
        self.accept()

    # ── Internal helpers ───────────────────────────────────────────

    def _best_match(self, terms: list[str], headers: list[str]) -> str:
        """Return the best matching header for the given search terms."""
        cutoff = self._difflib_cutoff
        for t in terms:
            if t in headers:
                return t
            matches = difflib.get_close_matches(t, headers, n=1, cutoff=cutoff)
            if matches:
                return matches[0]
        return ""

    @staticmethod
    def _na_item() -> QTableWidgetItem:
        """Create a read-only '(해당없음)' table item."""
        item = QTableWidgetItem("(해당없음)")
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return item
