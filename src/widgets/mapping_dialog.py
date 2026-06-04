"""컬럼 매핑 편집 다이얼로그 — 명단 컬럼 ↔ 청구서 템플릿"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..config import DEFAULT_TEMPLATE_COLUMNS


class MappingDialog(QDialog):
    """
    명단 컬럼 → 청구서 템플릿 각 필드에 매핑.
    사용자가 드롭다운으로 명단의 어떤 컬럼을 쓸지 선택.
    """

    # 청구서 템플릿에서 사용자가 지정해야 할 필드들
    FIELDS = [
        ("roster_category_col", "구분 (신규/중/고)", False),
        ("roster_dept_col", "학교명", False),
        ("roster_grade_col", "학년", False),
        ("roster_name_col", "성명 *", True),
        ("roster_birth_col", "생년월일", False),
        ("roster_head_col", "세대주성명", False),
        ("roster_relation_col", "아동과의관계", False),
        ("roster_dong_col", "관할동", False),
        ("roster_addr_col", "주소", False),
        ("roster_phone_col", "핸드폰", False),
        ("", "── 급식 플래그 (배송일 계산용) ──", False),  # 구분선
        ("roster_breakfast_col", "조식 여부 컬럼 *", True),
        ("roster_dinner_col", "석식 여부 컬럼 *", True),
        ("roster_vacation_col", "방중 여부 컬럼 *", True),
        ("roster_note_col", "비고 (일시중지 등)", False),
    ]

    COLUMN_HELP = {
        "roster_breakfast_col": "1.0=해당, 0/공란=미해당 (금요일 배송)",
        "roster_dinner_col": "1.0=해당, 0/공란=미해당 (월~목 배송)",
        "roster_vacation_col": "1.0=해당, 0/공란=미해당 (방학중 월~금 배송)",
    }

    def __init__(self, roster_headers: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("컬럼 매핑 설정")
        self.setMinimumWidth(650)
        self.resize(650, 500)

        self.roster_headers = roster_headers
        self._combo_map: dict[str, QComboBox] = {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # 안내
        info = QLabel(
            "명단 파일의 각 컬럼이 청구서 템플릿의 어느 필드에 대응되는지 선택하세요.\n"
            "* 표시는 필수 항목입니다."
        )
        info.setStyleSheet("color: #555; margin-bottom: 8px;")
        info.setWordWrap(True)
        layout.addWidget(info)

        # 폼
        form_group = QGroupBox("명단 컬럼 → 청구서 필드")
        form = QFormLayout(form_group)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(6)

        for key, label, required in self.FIELDS:
            if not key:
                # 구분선
                sep = QLabel(f"<b>{label}</b>")
                sep.setStyleSheet("margin-top: 12px;")
                form.addRow(sep)
                continue

            lbl_text = label
            if required:
                lbl_text = f"<b>{lbl_text}</b>"

            combo = QComboBox()
            combo.setMinimumWidth(200)
            combo.addItem("(선택 안 함)", "")
            for h in self.roster_headers:
                combo.addItem(h, h)
            combo.setCurrentIndex(0)

            # 도움말
            tip = self.COLUMN_HELP.get(key, "")
            if tip:
                combo.setToolTip(tip)

            self._combo_map[key] = combo
            form.addRow(QLabel(lbl_text), combo)
            if tip:
                hint = QLabel(f'<span style="color:#888; font-size:10px;">{tip}</span>')
                form.addRow("", hint)

        layout.addWidget(form_group, stretch=1)

        # 버튼
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

    def _reset_defaults(self):
        """자동 추천: 헤더명 기반으로 가장 비슷한 컬럼 찾아 선택"""
        from ..config import DEFAULT_ROSTER_TO_TEMPLATE

        auto_map = {}
        for key, _, _ in self.FIELDS:
            if not key:
                continue
            default_hint = DEFAULT_ROSTER_TO_TEMPLATE.get(key, "")
            if default_hint:
                best = self._find_best_match(default_hint)
                auto_map[key] = best
            else:
                auto_map[key] = ""
        self.set_values(auto_map)

    def _find_best_match(self, hint: str) -> str:
        """hint 문자열과 가장 유사한 헤더 찾기"""
        import difflib

        if hint in self.roster_headers:
            return hint
        matches = difflib.get_close_matches(hint, self.roster_headers, n=1, cutoff=0.5)
        return matches[0] if matches else ""

    def set_values(self, mapping: dict):
        """외부에서 매핑값 주입"""
        for key, value in mapping.items():
            combo = self._combo_map.get(key)
            if combo:
                idx = combo.findText(value)
                if idx >= 0:
                    combo.setCurrentIndex(idx)

    def get_values(self) -> dict:
        """{설정키: 명단컬럼명} 반환"""
        result = {}
        for key, combo in self._combo_map.items():
            result[key] = combo.currentData()
        return result

    def _accept(self):
        # required 체크
        missing = []
        for key, label, required in self.FIELDS:
            if not key:
                continue
            if required:
                val = self._combo_map[key].currentData()
                if not val:
                    missing.append(label)
        if missing:
            QMessageBox.warning(
                self,
                "필수 항목",
                f"다음 필수 항목을 선택하세요:\n" + "\n".join(missing),
            )
            return
        self.accept()
