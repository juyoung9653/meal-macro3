"""Responsive source tiles mapped to stable database destination keys."""
from math import ceil
from uuid import uuid4

from PySide6.QtCore import QMimeData, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QDrag
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLayout,
    QMenu, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from .theme import CANVAS, FOCUS, INK_SOFT, LINE, MUTED, ROUTE, SURFACE, TEXT

_MIME = "application/x-roster-import-column"


class TileLayout(QLayout):
    """Equal-width cards wrap without horizontal scrolling."""
    def __init__(self, parent=None, *, cell_width=220):
        super().__init__(parent)
        self.items = []
        self.cell_width = cell_width
        self.setContentsMargins(4, 4, 4, 4)
        self.setSpacing(8)

    def addItem(self, item):
        self.items.append(item)

    def count(self):
        return len(self.items)

    def itemAt(self, index):
        return self.items[index] if 0 <= index < len(self.items) else None

    def takeAt(self, index):
        return self.items.pop(index) if 0 <= index < len(self.items) else None

    def expandingDirections(self):
        return Qt.Orientation.Horizontal

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), False)

    def minimumSize(self):
        return QSize(120, 40)

    def sizeHint(self):
        return QSize(self.cell_width * 3, self.heightForWidth(self.cell_width * 3))

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, True)

    def _arrange(self, rect, apply):
        left, top, right, bottom = self.getContentsMargins()
        width = max(1, rect.width() - left - right)
        count = max(1, (width + self.spacing()) // (self.cell_width + self.spacing()))
        cell_width = max(1, (width - (count - 1) * self.spacing()) // count)
        height = max((item.sizeHint().height() for item in self.items), default=0)
        for index, item in enumerate(self.items):
            if apply:
                item.setGeometry(QRect(rect.x() + left + (index % count) * (cell_width + self.spacing()),
                                       rect.y() + top + (index // count) * (height + self.spacing()),
                                       cell_width, height))
        rows = ceil(len(self.items) / count)
        return top + bottom + rows * height + max(0, rows - 1) * self.spacing()


class SourceTile(QPushButton):
    def __init__(self, board, source, label, examples):
        super().__init__(board)
        self.board, self.source = board, source
        self._press = QPoint()
        self.setObjectName("importSourceTile")
        self.setCheckable(True)
        self.setAutoDefault(False)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(66)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setAccessibleName(label + " 원본 칼럼 타일")
        self.setToolTip(label + "\n" + (examples or "값 예시 없음") + "\n끌어 놓거나, 선택 후 DB 칼럼을 클릭하세요.")
        body = QVBoxLayout(self)
        body.setContentsMargins(10, 6, 10, 6)
        for text, muted in ((label, False), (examples or "값 예시 없음", True)):
            item = QLabel(text)
            item.setTextFormat(Qt.TextFormat.PlainText)
            item.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            item.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            if muted:
                item.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
            body.addWidget(item)
        self.clicked.connect(lambda: board.select_source(source))

    def mousePressEvent(self, event):
        self._press = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and (event.position().toPoint() - self._press).manhattanLength() >= QApplication.startDragDistance():
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(_MIME, (self.board.drag_id + ":" + self.source).encode())
            drag.setMimeData(mime)
            drag.setPixmap(self.grab())
            drag.setHotSpot(self._press)
            self.board._drag_timer.start()
            try:
                drag.exec(Qt.DropAction.MoveAction)
            finally:
                self.board._drag_timer.stop()
                self.setDown(False)
            return
        super().mouseMoveEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        for target, label in self.board.targets:
            action = menu.addAction(label)
            action.setEnabled(self.board.target_enabled(target))
            action.triggered.connect(lambda _checked=False, key=target: self.board.assign_source(self.source, key))
        menu.exec(event.globalPos())


class DropArea(QFrame):
    def __init__(self, board, target, label):
        super().__init__()
        self.board, self.target = board, target
        self.setObjectName("importDropArea")
        self.setAcceptDrops(True)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        body = QVBoxLayout(self)
        body.setContentsMargins(8, 6, 8, 8)
        self.heading = QPushButton(label)
        self.heading.setAutoDefault(False)
        self.heading.setObjectName("importTargetHeading")
        self.heading.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.heading.setToolTip(label)
        self.heading.setAccessibleName(label + "에 연결")
        self.heading.clicked.connect(lambda: board.assign_selected(target))
        body.addWidget(self.heading)
        self.badge = QLabel("문서 사용")
        self.badge.setStyleSheet(f"color: {ROUTE}; font-size: 11px;")
        self.badge.setVisible(target in board.document_targets)
        body.addWidget(self.badge)
        self.empty = QPushButton("여기에 놓기")
        self.empty.setAutoDefault(False)
        self.empty.setObjectName("importEmptySlot")
        self.empty.setMinimumWidth(0)
        self.empty.setFixedHeight(66)
        self.empty.clicked.connect(lambda: board.assign_selected(target))
        if target == "reference":
            description = "원본 파일과 대조할 번호를 불러오기 이력에 보관합니다. 명단 칼럼이나 배송순서로 사용하지 않습니다. 연결하지 않아도 됩니다."
            self.heading.setToolTip(description)
            self.empty.setToolTip(description)
        body.addWidget(self.empty)
        self.tiles = TileLayout(cell_width=210) if target in ("ignore", "new") else QVBoxLayout()
        self.tiles.setContentsMargins(0, 0, 0, 0)
        body.addLayout(self.tiles)

    def _source(self, event):
        value = bytes(event.mimeData().data(_MIME)).decode("utf-8", errors="replace")
        prefix = self.board.drag_id + ":"
        source = value[len(prefix):] if value.startswith(prefix) else None
        return source if source in self.board.source_tiles else None

    def dragEnterEvent(self, event):
        if self._source(event) and self.board.target_enabled(self.target):
            self.setProperty("dropHover", True)
            self.style().unpolish(self)
            self.style().polish(self)
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if self._source(event) and self.board.target_enabled(self.target):
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._clear_hover()
        event.accept()

    def _clear_hover(self):
        self.setProperty("dropHover", False)
        self.style().unpolish(self)
        self.style().polish(self)

    def dropEvent(self, event):
        self._clear_hover()
        source = self._source(event)
        if source and self.board.assign_source(source, self.target):
            event.acceptProposedAction()


class ImportColumnsTable(QWidget):
    """Keep the existing mapping contract while presenting a tile board."""
    destination_changed = Signal(str, str)
    summary_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.drag_id = uuid4().hex
        self._drag_timer = QTimer(self)
        self._drag_timer.setInterval(40)
        self._drag_timer.timeout.connect(lambda: self._scroll_drag_at(QCursor.pos()))
        self.combos, self._selected, self.source_tiles, self.cards = {}, {}, {}, {}
        self.targets = []
        self.document_targets = frozenset()
        self._selected_source = None
        self._meals_enabled = True
        self.setStyleSheet(f"""
            QFrame#importDropArea {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 5px; }}
            QFrame#importDropArea[dropHover="true"] {{ border: 2px solid {ROUTE}; background: {CANVAS}; }}
            QPushButton#importSourceTile {{ background: {CANVAS}; color: {TEXT}; border: 1px solid {LINE}; border-radius: 4px; padding: 0; }}
            QPushButton#importSourceTile:checked, QPushButton#importSourceTile:focus {{ border: 2px solid {FOCUS}; }}
            QPushButton#importTargetHeading {{ color: {TEXT}; text-align: left; border: none; background: transparent; font-weight: 600; padding: 2px; }}
            QPushButton#importEmptySlot {{ color: {MUTED}; border: 1px dashed {LINE}; background: transparent; border-radius: 4px; }}
            QPushButton#importTargetHeading:focus, QPushButton#importEmptySlot:focus {{ border: 2px solid {FOCUS}; }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        note = QLabel("엑셀 타일을 DB 칼럼에 끌어 놓으세요. 타일을 선택한 뒤 DB 칼럼을 클릭해도 연결됩니다. 끌면서 위·아래 가장자리에 머물면 스크롤됩니다.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.priority_note = QLabel()
        self.priority_note.setWordWrap(True)
        self.priority_note.setObjectName("muted")
        self.priority_note.hide()
        layout.addWidget(self.priority_note)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.grid = QWidget()
        self.grid_layout = QVBoxLayout(self.grid)
        self.grid_layout.setContentsMargins(4, 4, 4, 4)
        self.grid_layout.setSpacing(16)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.sections, self.section_layouts, self.section_counts, self.section_empty = {}, {}, {}, {}
        for key, title, description, background, foreground in (
            ("document", "문서에 사용하는 칼럼", "청구서·확인서의 항목 연결에 사용하는 칼럼입니다.", ROUTE, SURFACE),
            ("meal", "식종", "엑셀의 식종 열 하나 또는 조식·중식·석식 열을 연결하세요.", INK_SOFT, SURFACE),
            ("other", "나머지 칼럼", "문서 사용 칼럼 외의 명단 정보·배송 정보·원본 참조입니다.", CANVAS, TEXT),
        ):
            section = QFrame()
            section.setObjectName("importMappingSection")
            section.setAccessibleName(title)
            section.setStyleSheet(f"QFrame#importMappingSection {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 5px; }}")
            body = QVBoxLayout(section)
            body.setContentsMargins(0, 0, 0, 8)
            header = QFrame()
            header.setObjectName("importSectionHeader")
            header.setStyleSheet(f"QFrame#importSectionHeader {{ background: {background}; border: none; border-radius: 4px; }} QLabel {{ color: {foreground}; background: transparent; border: none; }}")
            header_layout = QVBoxLayout(header)
            header_layout.setContentsMargins(12, 10, 12, 10)
            title_row = QHBoxLayout()
            label = QLabel(title)
            label.setStyleSheet("font-weight: 600; font-size: 14px;")
            title_row.addWidget(label, 1)
            count = QLabel()
            title_row.addWidget(count)
            header_layout.addLayout(title_row)
            detail = QLabel(description)
            detail.setWordWrap(True)
            header_layout.addWidget(detail)
            body.addWidget(header)
            cards = TileLayout()
            cards.setContentsMargins(8, 4, 8, 0)
            body.addLayout(cards)
            empty = QLabel("해당하는 칼럼이 없습니다.")
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color: {MUTED}; padding: 8px 12px;")
            body.addWidget(empty)
            self.sections[key], self.section_layouts[key] = section, cards
            self.section_counts[key], self.section_empty[key] = count, empty
            self.grid_layout.addWidget(section)
        self.scroll = QScrollArea()
        self.scroll.setAccessibleName("DB 칼럼 연결 자리 — 문서에 사용하는 칼럼, 식종, 나머지 칼럼")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.grid)
        self.scroll.setMinimumHeight(280)
        self.scroll.setMaximumHeight(380)
        layout.addWidget(self.scroll)
        self.pool = DropArea(self, "ignore", "아직 연결하지 않은 엑셀 칼럼 · 가져오지 않음")
        self.pool.empty.setText("타일을 여기로 내려놓으면 연결이 해제됩니다")
        layout.addWidget(self.pool)
        self.new_area = DropArea(self, "new", "+ 새 DB 칼럼 추가 (문자)")
        self.new_area.empty.setText("선택하거나 끌어 놓은 원본 열 이름으로 새 칼럼 추가")
        layout.addWidget(self.new_area)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setObjectName("errorMessage")
        self.error.hide()
        layout.addWidget(self.error)
        self._combo_state = QWidget(self)
        self._combo_state.hide()

    def _scroll_drag_at(self, global_pos):
        areas = [self.scroll]
        ancestor = self.parentWidget()
        while ancestor is not None:
            if isinstance(ancestor, QScrollArea):
                areas.append(ancestor)
            ancestor = ancestor.parentWidget()
        for area in areas:
            viewport = area.viewport()
            point = viewport.mapFromGlobal(global_pos)
            if not viewport.rect().contains(point):
                continue
            direction = -1 if point.y() < 36 else (1 if point.y() > viewport.height() - 36 else 0)
            bar = area.verticalScrollBar()
            before = bar.value()
            bar.setValue(before + direction * 18)
            if bar.value() != before:
                return

    def set_document_targets(self, targets):
        self.document_targets = frozenset(targets)
        self._order_cards()

    def _order_cards(self):
        for layout in self.section_layouts.values():
            while layout.count():
                layout.takeAt(0)
        for key, card in self.cards.items():
            group = "meal" if key.startswith("meal:") else ("document" if key in self.document_targets else "other")
            card.badge.setVisible(key in self.document_targets)
            self.section_layouts[group].addWidget(card)
            card.show()
        for key, layout in self.section_layouts.items():
            self.sections[key].setVisible(key != "meal" or layout.count() > 0)
            self.section_counts[key].setText(f"{layout.count()}개")
            self.section_empty[key].setVisible(layout.count() == 0)
            layout.invalidate()
        self.grid_layout.invalidate()

    def populate(self, inspection, targets, selected):
        self.error.hide()
        for widget in (*self.source_tiles.values(), *self.combos.values(), *self.cards.values()):
            widget.hide()
            widget.deleteLater()
        for area in (*self.cards.values(), self.pool, self.new_area):
            while area.tiles.count():
                area.tiles.takeAt(0)
        self.source_tiles, self.combos, self.cards = {}, {}, {}
        self.targets = list(targets)
        self._selected_source = None
        self._selected = dict(selected)
        for key, label in targets:
            if key not in ("ignore", "new"):
                self.cards[key] = DropArea(self, key, label)
        self._order_cards()
        for row, column in enumerate(inspection.columns):
            examples = tuple(dict.fromkeys(sample.values[row] for sample in inspection.preview_rows
                                          if row < len(sample.values) and sample.values[row]))[:3]
            self.source_tiles[column.letter] = SourceTile(self, column.letter, column.label,
                " · ".join(value.replace("\n", " / ") for value in examples))
            combo = QComboBox(self._combo_state)
            for key, label in targets:
                combo.addItem(label, key)
            combo.currentIndexChanged.connect(lambda _index, source=column.letter: self._changed(source))
            self.combos[column.letter] = combo
        self.sync(selected)
        self.set_meal_targets_enabled(self._meals_enabled)

    def sync(self, selected):
        self._selected = dict(selected)
        for source, combo in self.combos.items():
            value = selected.get(source, "ignore")
            combo.blockSignals(True)
            if combo.findData(value) < 0:
                combo.addItem("삭제된 칼럼 — 다시 선택해주세요", value)
            combo.setCurrentIndex(combo.findData(value))
            combo.blockSignals(False)
        self._refresh_tiles()

    def _refresh_tiles(self):
        for area in (*self.cards.values(), self.pool, self.new_area):
            while area.tiles.count():
                area.tiles.takeAt(0)
        for source, tile in self.source_tiles.items():
            target = self._selected.get(source, "ignore")
            area = self.new_area if target == "new" else self.cards.get(target, self.pool)
            area.tiles.addWidget(tile)
            tile.setChecked(source == self._selected_source)
            tile.show()
        for area in (*self.cards.values(), self.pool, self.new_area):
            area.empty.setVisible(area.tiles.count() == 0)
            area.layout().invalidate()
        linked = sum(value != "ignore" and not value.startswith("missing:") for value in self._selected.values())
        groups = "문서에 사용하는 칼럼 / "
        if any(key.startswith("meal:") for key in self.cards):
            groups += "식종 / "
        groups += "나머지 칼럼"
        self.scroll.setAccessibleName("DB 칼럼 연결 자리 — " + groups)
        self.status.setText(f"연결 {linked} / {len(self.source_tiles)}개 · {groups} 순서로 표시합니다.")
        missing_count = sum(value.startswith("missing:") for value in self._selected.values())
        summary = f"연결 {linked}개 · 가져오지 않음 {len(self.source_tiles) - linked - missing_count}개"
        if missing_count:
            summary += f" · 다시 연결 필요 {missing_count}개"
        self.summary_changed.emit(summary)
        missing = bool(missing_count)
        if missing:
            self.error.setText("삭제된 DB 칼럼에 연결된 타일이 있습니다. 아래 대기 타일을 다시 연결하거나 ‘가져오지 않음’으로 정해주세요.")
            self.error.show()
        self.grid_layout.invalidate()

    def target_enabled(self, target):
        return self._meals_enabled or not target.startswith("meal:")

    def set_meal_targets_enabled(self, enabled):
        self._meals_enabled = enabled
        for key, card in self.cards.items():
            card.setEnabled(self.target_enabled(key))
            card.setToolTip("" if self.target_enabled(key) else "방학 중식은 선택한 방학 기간에 명단의 대상자를 등록합니다.")
        for combo in self.combos.values():
            for index in range(combo.count()):
                combo.model().item(index).setEnabled(self.target_enabled(str(combo.itemData(index))))

    def select_source(self, source):
        self._selected_source = None if source == self._selected_source else source
        for key, tile in self.source_tiles.items():
            tile.setChecked(key == self._selected_source)

    def assign_selected(self, target):
        if self._selected_source is not None:
            self.assign_source(self._selected_source, target)

    def assign_source(self, source, target):
        if source not in self.source_tiles or target not in dict(self.targets) or not self.target_enabled(target):
            return False
        self.error.hide()
        self._selected_source = None
        if target not in ("ignore", "new"):
            displaced = next((other for other, value in self._selected.items() if other != source and value == target), None)
            if displaced:
                self._selected[displaced] = "ignore"
                self.destination_changed.emit(displaced, "ignore")
        self._selected[source] = target
        self.destination_changed.emit(source, target)
        self.sync(self._selected)
        return True

    def _changed(self, source):
        value = self.combos[source].currentData()
        duplicate = next((other for other, target in self._selected.items() if other != source and target == value), None)
        if value not in ("ignore", "new") and duplicate is not None:
            self.error.setText(f"이미 {duplicate}열에 연결된 칼럼입니다. 기존 연결을 바꾼 뒤 선택해주세요.")
            self.error.show()
            self.sync(self._selected)
            return
        self.assign_source(source, value)


class ImportColumnMappingDialog(QDialog):
    """A retained, nonblocking workspace for the wizard's existing mapping state."""
    def __init__(self, board, parent=None):
        super().__init__(parent)
        self.board = board
        self.setWindowTitle("엑셀 열과 DB 칼럼 연결")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setSizeGripEnabled(True)
        self.resize(1200, 850)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        title = QLabel("엑셀 열 → DB 칼럼 연결")
        title.setObjectName("sectionLabel")
        layout.addWidget(title)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(board)
        layout.addWidget(self.scroll, 1)
        footer = QHBoxLayout()
        note = QLabel("연결을 확인한 뒤 ‘연결 확인 완료’를 누르세요. 명단은 불러오기 화면에서 저장합니다.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        footer.addWidget(note, 1)
        self.done_button = QPushButton("연결 확인 완료")
        self.done_button.setObjectName("primaryAction")
        self.done_button.setAutoDefault(False)
        self.done_button.clicked.connect(self.accept)
        footer.addWidget(self.done_button)
        layout.addLayout(footer)
        self.finished.connect(board._drag_timer.stop)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Use the large workspace while leaving the source pool below the grid.
        # Long source pools continue below the viewport instead of shrinking tiles.
        height = max(280, int(event.size().height() * 0.55))
        self.board.scroll.setMinimumHeight(height)
        self.board.scroll.setMaximumHeight(height)
