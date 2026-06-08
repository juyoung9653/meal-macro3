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

from ..config import SEMANTIC_COLUMN_ROLES, auto_detect_semantic_columns


class _NoWheelComboBox(QComboBox):
    """마우스 휠로 선택값 바뀌는 것 방지"""

    def wheelEvent(self, event: QWheelEvent):
        event.ignore()


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
    템플릿 컬럼 목록을 template_columns 기준으로 동적 생성.
    3개 섹션:
      1. 템플릿 컬럼 ↔ 명단 컬럼 매핑
      2. 계산값 입력 대상 (템플릿 내 특수 컬럼 지정)
      3. 배송일 계산용 플래그
    """

    def __init__(
        self, roster_headers: list[str], template_columns: list[str], parent=None
    ):
        super().__init__(parent)
        self.setWindowTitle("컬럼 매핑 설정")
        self.setMinimumWidth(650)
        self.resize(650, 500)

        self.roster_headers = [h.strip() for h in roster_headers if h.strip()]
        self._template_columns = template_columns
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
        table.setHorizontalHeaderLabels(["항목", "값 선택"])
        table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)

        row = 0
        # ── 섹션 1: 템플릿 컬럼 → 명단 컬럼 ──
        for col_name in self._template_columns:
            table.insertRow(row)

            item = QTableWidgetItem(col_name)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 0, item)

            combo = self._make_roster_combo()
            table.setCellWidget(row, 1, combo)
            self._combo_map[col_name] = combo
            row += 1

        # ── 섹션 2: 계산값 입력 대상 (auto-detect + GUI override) ──
        table.insertRow(row)
        sep1 = QTableWidgetItem("── 계산값 입력 대상 ──")
        sep1.setFlags(sep1.flags() & ~Qt.ItemFlag.ItemIsEditable)
        sep1.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        f = sep1.font()
        f.setBold(True)
        sep1.setFont(f)
        table.setItem(row, 0, sep1)
        table.setSpan(row, 0, 1, 2)
        row += 1

        _semantic_defaults = auto_detect_semantic_columns(self._template_columns)

        for key, info in SEMANTIC_COLUMN_ROLES.items():
            table.insertRow(row)

            item = QTableWidgetItem(info["label"])
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 0, item)

            combo = self._make_template_combo()
            table.setCellWidget(row, 1, combo)
            # auto-detect 값 기본 선택
            detected = _semantic_defaults.get(key, "")
            if detected:
                idx = combo.findText(detected)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            self._combo_map[key] = combo
            row += 1

        # ── 섹션 3: 배송일 계산용 플래그 ──
        table.insertRow(row)
        sep2 = QTableWidgetItem("── 배송일 계산용 ──")
        sep2.setFlags(sep2.flags() & ~Qt.ItemFlag.ItemIsEditable)
        sep2.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        f2 = sep2.font()
        f2.setBold(True)
        sep2.setFont(f2)
        table.setItem(row, 0, sep2)
        table.setSpan(row, 0, 1, 2)
        row += 1

        for key, label, tip in _FLAG_FIELDS:
            table.insertRow(row)

            item = QTableWidgetItem(label)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 0, item)

            combo = self._make_roster_combo()
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

    def _make_roster_combo(self) -> QComboBox:
        """명단 컬럼명을 선택하는 콤보박스"""
        combo = _NoWheelComboBox()
        combo.setMinimumWidth(200)
        combo.addItem("(선택 안 함)", "")
        for h in self.roster_headers:
            combo.addItem(h, h)
        return combo

    def _make_template_combo(self) -> QComboBox:
        """템플릿 컬럼명을 선택하는 콤보박스"""
        combo = _NoWheelComboBox()
        combo.setMinimumWidth(200)
        for col in self._template_columns:
            combo.addItem(col, col)
        return combo

    def _reset_defaults(self):
        """템플릿 컬럼명/플래그/계산값 자동 선택"""
        import difflib

        auto_map = {}
        for key in self._combo_map:
            if key.startswith("__calc_"):
                # 계산값 대상: auto_detect
                detected = auto_detect_semantic_columns(self._template_columns)
                auto_map[key] = detected.get(key, "")
            elif key.startswith("__"):
                # 플래그: 명단 헤더에서 검색
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
                auto_map[key] = best
            else:
                # 일반 템플릿 컬럼: 명단 헤더와 exact/match
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
