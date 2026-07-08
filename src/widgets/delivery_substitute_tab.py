"""④ 배송대타 탭 — 배송 대타 매크로 기능을 PyQt6로 포팅.

DbReader(분당구_급식 db.xlsx)를 유일한 데이터 소스로 사용.
- 배송전체(ID, 식종, 날짜 O/X) + 명단(ID→성명) + 배송정보(배송순서/주소) 조인
- 템플릿(assets/급식_배송대타_템플릿.xlsx)에 행 삽입 방식으로 출력.
"""

from __future__ import annotations

import logging

import pandas as pd
from PyQt6.QtCore import QDate
from PyQt6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.core.settings import get_settings
from src.excel._parsing import _parse_date_col
from src.excel.data_source import DataSource
from src.excel.db_reader import get_db_reader
from src.excel.delivery_io import Choice, make_result, uniq
from src.excel.delivery_viewer import show_excel

log = logging.getLogger(__name__)

TEMPLATE_OPTIONS = ["전체", "상세주소X"]

# 템플릿별 make_result에 전달할 컬럼 목록
TEMPLATE_COLS: dict[str, list[str]] = {
    "전체": [
        "배송순서",
        "관할동",
        "이름",
        "상세주소",
        "신주소",
        "핸드폰",
        "비고2",
        "식사 구분",
    ],
    "상세주소X": [
        "배송순서",
        "관할동",
        "이름",
        "신주소",
        "핸드폰",
        "비고2",
        "식사 구분",
    ],
}


class DeliverySubstituteTab(QWidget):
    """④ 배송대타 탭 — DB 필터 및 템플릿 출력."""

    def __init__(self, meal_type: str = "", data_source: DataSource | None = None):
        super().__init__()
        self._s = get_settings()
        self._meal_type = meal_type
        self._ds: DataSource | None = data_source

        # DB 데이터
        self._df: pd.DataFrame | None = None
        self._meals: list[str] = []
        self._ships: list[str] = []

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── 식사 구분 / 배송담당 ──
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("식사 구분"))
        self.meal_combo = QComboBox()
        row1.addWidget(self.meal_combo)
        row1.addSpacing(20)
        row1.addWidget(QLabel("배송담당"))
        self.ship_combo = QComboBox()
        row1.addWidget(self.ship_combo)
        row1.addStretch()
        layout.addLayout(row1)

        # ── 날짜 (캘린더 선택) ──
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("날짜"))
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        row2.addWidget(self.date_edit)
        row2.addStretch()
        layout.addLayout(row2)

        # ── 템플릿 선택 ──
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("템플릿"))
        self.template_combo = QComboBox()
        self.template_combo.addItems(TEMPLATE_OPTIONS)
        ds = self._s.get_delivery_settings()
        saved_tpl = ds.get("template", "전체")
        if saved_tpl in TEMPLATE_OPTIONS:
            self.template_combo.setCurrentText(saved_tpl)
        row3.addWidget(self.template_combo)
        row3.addStretch()
        layout.addLayout(row3)

        # ── 실행 ──
        self.run_btn = QPushButton("실행")
        self.run_btn.setMinimumHeight(36)
        self.run_btn.clicked.connect(self._on_run)
        layout.addWidget(self.run_btn)

        # ── 상태 ──
        self.status_label = QLabel("대기 중 (명단 파일을 선택해 주세요)")
        layout.addWidget(self.status_label)

        layout.addStretch()

    # ── Public: MainWindow 연동 ──

    def set_meal_type(self, meal_type: str):
        self._meal_type = meal_type

    def on_roster_changed(self):
        """MainWindow에서 명단 파일 변경 시 호출. DbReader에서 데이터 로드."""
        self.status_label.setText("명단 로드 중...")
        try:
            if self._load_db_data():
                self.status_label.setText("명단 로드 완료")
            else:
                self.status_label.setText("명단 로드 실패")
        except Exception as e:
            self.status_label.setText(f"명단 로드 오류: {e}")
            log.exception("on_roster_changed")

    # ── 데이터 로드 ──

    def _load_db_data(self) -> bool:
        """DataSource(DbReader)에서 배송 데이터 로드.

        배송전체(식종+날짜) + 명단(성명) + 배송정보(배송순서/주소)를
        조인하여 배송 대타에 필요한 flat DataFrame을 생성한다.
        """
        try:
            if self._ds is None:
                self._ds = get_db_reader(self._s.get("db", "path"))

            self._df = self._build_delivery_df()

            if self._df is None or self._df.empty:
                self.status_label.setText("배송 데이터 없음")
                return False

            col_meal = self._s.get_delivery_config("col_meal")
            col_ship = self._s.get_delivery_config("col_ship")
            self._meals = uniq(self._df, col_meal)
            self._ships = uniq(self._df, col_ship)

            self.meal_combo.clear()
            self.meal_combo.addItems(self._meals)
            self.ship_combo.clear()
            self.ship_combo.addItems(self._ships)

            if self._meals:
                self.meal_combo.setCurrentText(self._meals[0])
            if self._ships:
                self.ship_combo.setCurrentText(self._ships[0])

            self.status_label.setText(f"로드 완료 ({len(self._df)}행)")
            return True

        except Exception as e:
            self.status_label.setText(f"로드 실패: {e}")
            log.exception("_load_db_data")
            return False

    def _build_delivery_df(self) -> pd.DataFrame | None:
        """DbReader 배송전체 + 명단 + 배송정보를 조인하여 flat DataFrame 생성."""
        matrix = self._ds.read_delivery_matrix()
        if not matrix:
            return None

        roster = self._ds.read_roster()
        roster_by_id: dict[str, dict] = {}
        for p in roster:
            pid = str(p.get("ID", "")).strip()
            if pid:
                roster_by_id[pid] = p

        order_map = self._ds.build_order_map()

        rows: list[dict] = []
        for m in matrix:
            pid = str(m.get("ID", "")).strip()
            person = roster_by_id.get(pid, {})

            name = person.get("성명", "")
            phone = (
                str(person.get("핸드폰", "")).strip().replace("-", "").replace(" ", "")
            )
            addr = str(person.get("도로명주소", "")).strip()

            key = f"{phone}|{addr}" if phone and addr else ""
            order_info = order_map.get(key, {}) if key else {}

            row: dict = {
                "식사 구분": m.get("식종", ""),
                "배송_new": order_info.get("배송담당", ""),
                "배송순서": order_info.get("배송순서", ""),
                "이름": name,
                "관할동": person.get("관할동", ""),
                "상세주소": person.get("상세주소", ""),
                "신주소": order_info.get("도로명주소", addr),
                "핸드폰": order_info.get("핸드폰", phone),
                "비고2": order_info.get("비고", ""),
            }

            for k, v in m.items():
                if k not in ("ID", "식종"):
                    row[k] = v

            rows.append(row)

        return pd.DataFrame(rows) if rows else None

    # ── 실행 ──

    def _on_run(self):
        if self._df is None:
            if not self._load_db_data():
                QMessageBox.warning(self, "오류", "DB 데이터를 불러오지 못했습니다.")
                return

        qd = self.date_edit.date()
        m, d = qd.month(), qd.day()
        target_md = (m, d)

        flag_col = ""
        for col in self._df.columns:
            parsed = _parse_date_col(col)
            if parsed == target_md:
                flag_col = col
                break
        if not flag_col:
            QMessageBox.warning(
                self, "오류", f"'{m}/{d}' 날짜 컬럼을 배송전체에서 찾을 수 없습니다."
            )
            return

        template_choice = self.template_combo.currentText()
        cols = TEMPLATE_COLS.get(template_choice, TEMPLATE_COLS["전체"])

        self._s.set_delivery_settings(
            template=template_choice,
        )
        self.status_label.setText("처리 중...")
        from PyQt6.QtWidgets import QApplication

        QApplication.processEvents()

        try:
            meal = self.meal_combo.currentText()
            ship = self.ship_combo.currentText()

            choice = Choice(meal=meal, ship=ship, flag_col=flag_col, cols=cols)
            out = make_result(self._df, choice)

            if out.empty:
                QMessageBox.information(
                    self, "알림", "해당 조건에 맞는 데이터가 없습니다."
                )
                self.status_label.setText("대기 중")
                return

            date_label = f"{qd.year()}{m:02d}{d:02d}"
            show_excel(
                df=out,
                meal=meal,
                ship=ship,
                date_str=date_label,
                template_choice=template_choice,
            )

            self.status_label.setText("완료")
            QMessageBox.information(self, "완료", "엑셀 출력이 완료되었습니다.")
        except Exception as e:
            self.status_label.setText(f"오류: {e}")
            QMessageBox.critical(self, "오류", f"실행 중 오류 발생:\n{e}")
            log.exception("_on_run")
