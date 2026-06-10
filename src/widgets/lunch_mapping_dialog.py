"""중식 명단 컬럼 매핑 다이얼로그 — 중식 명단 컬럼 ↔ 필수 필드 + 교차 참조."""

import difflib

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
    """마우스 휠로 선택값 바뀌는 것 방지."""

    def wheelEvent(self, event: QWheelEvent):
        event.ignore()


# ── 중식 명단 필수 필드 정의 ──

_LUNCH_FIELDS = [
    ("lunch_school_name", "학교명", "중식 명단의 학교명 컬럼"),
    ("lunch_vacation_date", "방학식 일자", "방학 시작일 (YYYY.MM.DD 형식)"),
    ("lunch_opening_date", "개학식 일자", "개학일 (YYYY.MM.DD 형식)"),
    ("lunch_vacation_meal", "방학식 급식 제공 여부", "O/X, Y/N 등"),
    ("lunch_opening_meal", "개학식 급식 제공 여부", "O/X, Y/N 등"),
    ("lunch_note", "비고", "방학/개학 관련 특이사항"),
]

# 자동 탐지용 검색어
_LUNCH_SEARCH_TERMS = {
    "lunch_school_name": ["학교명", "학교"],
    "lunch_vacation_date": ["방학식", "방학실", "방학 날짜", "방학일자"],
    "lunch_opening_date": ["개학식", "개학", "개학 날짜", "개학일자"],
    "lunch_vacation_meal": ["방학식 급식", "방학 급식", "방학식급식"],
    "lunch_opening_meal": ["개학일 급식", "개학식 급식", "개학 급식", "개학식급식"],
    "lunch_note": ["비고", "특이사항", "메모"],
}

# ── 교차 참조 필드 (명단 ↔ 중식 명단) ──

_CROSS_REF_FIELDS = [
    ("roster_school_col", "명단 학교명 컬럼", "명단에서 학교명이 있는 컬럼"),
    ("roster_grade_col", "명단 학년 컬럼", "명단에서 학년 정보가 있는 컬럼"),
]

_CROSS_REF_SEARCH_TERMS = {
    "roster_school_col": ["학교명", "학교", "소속", "기관명"],
    "roster_grade_col": ["학년", "학년도", "grade", "Grade"],
}


class LunchMappingDialog(QDialog):
    """중식 명단 컬럼 매핑 다이얼로그.

    섹션:
      1. 중식 명단 컬럼 매핑 (6개 필수 필드)
      2. 교차 참조 (명단의 학교명/학년 컬럼)
    """

    def __init__(
        self,
        lunch_headers: list[str],
        roster_headers: list[str] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("중식 명단 컬럼 매핑")
        self.setMinimumWidth(650)
        self.resize(650, 500)

        self._lunch_headers = [h.strip() for h in lunch_headers if h.strip()]
        self._roster_headers = (
            [h.strip() for h in roster_headers if h.strip()] if roster_headers else []
        )
        self._combo_map: dict[str, QComboBox] = {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        info = QLabel(
            "중식 명단 파일의 각 컬럼을 아래 항목에 매핑하세요.\n"
            "하단의 '교차 참조'는 명단과 중식 명단 간 학교명 매칭에 사용됩니다."
        )
        info.setStyleSheet("color: #555; margin-bottom: 8px;")
        info.setWordWrap(True)
        layout.addWidget(info)

        table = QTableWidget()
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(
            ["항목", "중식 명단 컬럼", "명단 컬럼 (교차참조)"]
        )
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)

        row = 0

        # ── 섹션 1: 중식 명단 컬럼 매핑 ──
        sep_item = QTableWidgetItem("── 중식 명단 컬럼 매핑 ──")
        sep_item.setFlags(sep_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        sep_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        f = sep_item.font()
        f.setBold(True)
        sep_item.setFont(f)
        table.insertRow(row)
        table.setItem(row, 0, sep_item)
        table.setSpan(row, 0, 1, 3)
        row += 1

        for key, label, tip in _LUNCH_FIELDS:
            table.insertRow(row)

            item = QTableWidgetItem(label)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if tip:
                item.setToolTip(tip)
            table.setItem(row, 0, item)

            # 중식 명단 컬럼 콤보
            lunch_combo = self._make_combo(self._lunch_headers)
            if tip:
                lunch_combo.setToolTip(tip)
            table.setCellWidget(row, 1, lunch_combo)
            self._combo_map[key] = lunch_combo

            # 교차참조 컬럼은 빈 셀 (or N/A)
            na_item = QTableWidgetItem("(해당없음)")
            na_item.setFlags(na_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            na_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(row, 2, na_item)
            row += 1

        # ── 섹션 2: 교차 참조 ──
        sep2_item = QTableWidgetItem("── 교차 참조 (명단 ↔ 중식 명단) ──")
        sep2_item.setFlags(sep2_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        sep2_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        f2 = sep2_item.font()
        f2.setBold(True)
        sep2_item.setFont(f2)
        table.insertRow(row)
        table.setItem(row, 0, sep2_item)
        table.setSpan(row, 0, 1, 3)
        row += 1

        for key, label, tip in _CROSS_REF_FIELDS:
            table.insertRow(row)

            item = QTableWidgetItem(label)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if tip:
                item.setToolTip(tip)
            table.setItem(row, 0, item)

            # 중식 명단 쪽은 N/A
            na_item = QTableWidgetItem("(해당없음)")
            na_item.setFlags(na_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            na_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(row, 1, na_item)

            # 명단 컬럼 콤보
            roster_combo = self._make_combo(self._roster_headers)
            if tip:
                roster_combo.setToolTip(tip)
            table.setCellWidget(row, 2, roster_combo)
            self._combo_map[key] = roster_combo
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

    def _make_combo(self, headers: list[str]) -> QComboBox:
        """헤더 목록을 선택하는 콤보박스."""
        combo = _NoWheelComboBox()
        combo.setMinimumWidth(200)
        combo.addItem("(선택 안 함)", "")
        for h in headers:
            combo.addItem(h, h)
        return combo

    def _reset_defaults(self):
        """부분일치로 기본값 자동 선택."""
        auto_map: dict[str, str] = {}

        for key in _LUNCH_SEARCH_TERMS:
            terms = _LUNCH_SEARCH_TERMS[key]
            best = ""
            for t in terms:
                if t in self._lunch_headers:
                    best = t
                    break
                matches = difflib.get_close_matches(
                    t, self._lunch_headers, n=1, cutoff=0.4
                )
                if matches:
                    best = matches[0]
                    break
            auto_map[key] = best

        for key in _CROSS_REF_SEARCH_TERMS:
            terms = _CROSS_REF_SEARCH_TERMS[key]
            best = ""
            for t in terms:
                if t in self._roster_headers:
                    best = t
                    break
                matches = difflib.get_close_matches(
                    t, self._roster_headers, n=1, cutoff=0.4
                )
                if matches:
                    best = matches[0]
                    break
            auto_map[key] = best

        self.set_values(auto_map)

    def set_values(self, mapping: dict):
        """매핑 값 일괄 설정."""
        for key, value in mapping.items():
            combo = self._combo_map.get(key)
            if combo:
                idx = combo.findText(value)
                if idx >= 0:
                    combo.setCurrentIndex(idx)

    def get_values(self) -> dict:
        """매핑 값 반환."""
        return {key: combo.currentData() for key, combo in self._combo_map.items()}

    def _accept(self):
        self.accept()
