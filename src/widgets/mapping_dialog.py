"""컬럼 매핑 편집 다이얼로그 — 명단 컬럼 ↔ 청구서 템플릿"""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


class _NoWheelComboBox(QComboBox):
    """마우스 휠로 선택값 바뀌는 것 방지"""

    def wheelEvent(self, event: QWheelEvent):
        event.ignore()


from ..config import DEFAULT_TEMPLATE_COLUMNS

# 배송일 계산용 플래그 — 템플릿 컬럼과 별도 섹션
_FLAG_FIELDS = [
    ("__breakfast", "조식 여부", "1.0=해당, 0/공란=미해당 (금요일 배송)"),
    ("__dinner", "석식 여부", "1.0=해당, 0/공란=미해당 (월~목 배송)"),
    ("__vacation", "방중 여부", "1.0=해당, 0/공란=미해당 (방학중 월~금 배송)"),
    ("__note", "비고 (일시중지 등)", ""),
]

# 플래그 키별 자동 탐색할 명단 컬럼명 후보
_FLAG_SEARCH_TERMS = {
    "__breakfast": ["조식"],
    "__dinner": ["석식"],
    "__vacation": ["중식", "방중"],
    "__note": ["비고"],
}


class MappingDialog(QDialog):
    """
    템플릿 컬럼 목록을 config.DEFAULT_TEMPLATE_COLUMNS 기준으로 동적 생성.
    테이블에서 각 템플릿 컬럼에 대응할 명단 컬럼을 선택.
    """

    def __init__(self, roster_headers: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("컬럼 매핑 설정")
        self.setMinimumWidth(650)
        self.resize(650, 500)

        self.roster_headers = [h.strip() for h in roster_headers if h.strip()]
        self._combo_map: dict[str, QComboBox] = {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        info = QLabel(
            "명단 파일의 각 컬럼이 청구서 템플릿의 어느 필드에 대응되는지 선택하세요."
        )
        info.setStyleSheet("color: #555; margin-bottom: 8px;")
        info.setWordWrap(True)
        layout.addWidget(info)

        # ── 테이블 ──
        table = QTableWidget()
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["템플릿 컬럼", "명단 컬럼 선택"])
        table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)

        row = 0
        # 템플릿 컬럼
        for col_name in DEFAULT_TEMPLATE_COLUMNS:
            table.insertRow(row)

            item = QTableWidgetItem(col_name)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 0, item)

            combo = self._make_combo()
            table.setCellWidget(row, 1, combo)
            self._combo_map[col_name] = combo
            row += 1

        # 구분선
        table.insertRow(row)
        sep = QTableWidgetItem("── 배송일 계산용 ──")
        sep.setFlags(sep.flags() & ~Qt.ItemFlag.ItemIsEditable)
        sep.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        f = sep.font()
        f.setBold(True)
        sep.setFont(f)
        table.setItem(row, 0, sep)
        table.setSpan(row, 0, 1, 2)
        row += 1

        # 플래그
        for key, label, tip in _FLAG_FIELDS:
            table.insertRow(row)

            item = QTableWidgetItem(label)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 0, item)

            combo = self._make_combo()
            if tip:
                combo.setToolTip(tip)
            table.setCellWidget(row, 1, combo)
            self._combo_map[key] = combo
            row += 1

        layout.addWidget(table, stretch=1)

        # ── 버튼 ──
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

    def _make_combo(self) -> QComboBox:
        combo = _NoWheelComboBox()
        combo.setMinimumWidth(200)
        combo.addItem("(선택 안 함)", "")
        for h in self.roster_headers:
            combo.addItem(h, h)
        return combo

    def _reset_defaults(self):
        """템플릿 컬럼명/플래그와 유사한 명단 헤더 자동 선택"""
        import difflib

        auto_map = {}
        for key in self._combo_map:
            if key.startswith("__"):
                terms = _FLAG_SEARCH_TERMS.get(key, [key])
                best = ""
                for t in terms:
                    if t in self.roster_headers:
                        best = t
                        break
                    matches = difflib.get_close_matches(
                        t, self.roster_headers, n=1, cutoff=0.5
                    )
                    if matches:
                        best = matches[0]
                        break
            else:
                if key in self.roster_headers:
                    best = key
                else:
                    matches = difflib.get_close_matches(
                        key, self.roster_headers, n=1, cutoff=0.5
                    )
                    best = matches[0] if matches else ""
            auto_map[key] = best
        self.set_values(auto_map)

    def set_values(self, mapping: dict):
        for key, value in mapping.items():
            combo = self._combo_map.get(key)
            if combo:
                idx = combo.findText(value)
                if idx >= 0:
                    combo.setCurrentIndex(idx)

    def get_values(self) -> dict:
        return {key: combo.currentData() for key, combo in self._combo_map.items()}

    def _accept(self):
        self.accept()
