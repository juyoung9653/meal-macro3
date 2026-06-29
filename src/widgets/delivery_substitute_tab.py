"""④ 배송대타 탭 — 배송 대타 매크로 기능을 PyQt6로 포팅.

DbReader(분당구_급식 db.xlsx)를 유일한 데이터 소스로 사용.
- 배송전체(ID, 식종, 날짜 O/X) + 명단(ID→성명) + 배송정보(배송순서/주소) 조인
"""

from __future__ import annotations

import logging

import pandas as pd
from PyQt6.QtCore import QDate
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.core.settings import get_settings
from src.excel._parsing import _is_date_col, _parse_date_col
from src.excel.data_source import DataSource
from src.excel.db_reader import get_db_reader
from src.excel.delivery_io import Choice, make_result, uniq
from src.excel.delivery_viewer import show_excel

log = logging.getLogger(__name__)

ORI_OPTIONS = ["자동", "세로", "가로"]
SPLIT_OPTIONS = ["동기준", "그룹기준"]


class DeliverySubstituteTab(QWidget):
    """④ 배송대타 탭 — DB 필터 및 A4 최적화 출력."""

    def __init__(self, meal_type: str = "", data_source: DataSource | None = None):
        super().__init__()
        self._s = get_settings()
        self._meal_type = meal_type
        self._ds: DataSource | None = data_source

        # 컬럼 설정
        self._pick_cols: list[str] = self._s.get_delivery_config("pick_cols") or []
        delivery_st = self._s.get_delivery_settings()
        cols_data = delivery_st.get("cols", {})
        self._order: list[str] = cols_data.get("order", self._pick_cols[:])
        self._enabled: dict[str, bool] = cols_data.get(
            "enabled", {c: True for c in self._pick_cols}
        )

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

        # ── 인쇄 설정 ──
        ds = self._s.get_delivery_settings()
        print_group = QGroupBox("인쇄 설정")
        print_grid = QGridLayout(print_group)
        print_grid.addWidget(QLabel("방향:"), 0, 0)
        self.ori_combo = QComboBox()
        self.ori_combo.addItems(ORI_OPTIONS)
        self.ori_combo.setCurrentText(ds.get("orientation", "세로"))
        print_grid.addWidget(self.ori_combo, 0, 1)
        print_grid.addWidget(QLabel("분할:"), 0, 2)
        self.split_combo = QComboBox()
        self.split_combo.addItems(SPLIT_OPTIONS)
        self.split_combo.setCurrentText(ds.get("split_mode", "동기준"))
        print_grid.addWidget(self.split_combo, 0, 3)
        print_grid.addWidget(QLabel("최대그룹:"), 0, 4)
        self.max_rows_edit = QLineEdit(str(self._s.get_delivery_config("max_rows")))
        self.max_rows_edit.setFixedWidth(60)
        print_grid.addWidget(self.max_rows_edit, 0, 5)
        print_grid.setColumnStretch(6, 1)
        layout.addWidget(print_group)

        # ── 출력 컬럼 ──
        col_group = QGroupBox("출력 컬럼")
        col_layout = QHBoxLayout(col_group)
        self.col_list = QListWidget()
        self.col_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        col_layout.addWidget(self.col_list)

        btn_col = QVBoxLayout()
        self.up_btn = QPushButton("▲ 위")
        self.up_btn.clicked.connect(lambda: self._move_column(-1))
        self.down_btn = QPushButton("▼ 아래")
        self.down_btn.clicked.connect(lambda: self._move_column(1))
        self.toggle_btn = QPushButton("토글")
        self.toggle_btn.clicked.connect(self._toggle_column)
        btn_col.addWidget(self.up_btn)
        btn_col.addWidget(self.down_btn)
        btn_col.addWidget(self.toggle_btn)
        btn_col.addStretch()
        col_layout.addLayout(btn_col)
        layout.addWidget(col_group)

        self._render_column_list()

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
            # DataSource 확보
            if self._ds is None:
                self._ds = get_db_reader(self._s.get("db", "path"))

            # DbReader 기반 DF 빌드
            self._df = self._build_delivery_df()

            if self._df is None or self._df.empty:
                self.status_label.setText("배송 데이터 없음")
                return False

            # 식사 구분(식종) / 배송담당 목록 추출
            col_meal = self._s.get_delivery_config("col_meal")  # "식사 구분"
            col_ship = self._s.get_delivery_config("col_ship")  # "배송_new"
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
        """DbReader 배송전체 + 명단 + 배송정보를 조인하여 flat DataFrame 생성.

        Result columns (for make_result compatibility):
            식사 구분, 배송_new, 배송순서, 이름, 관할동,
            상세주소, 신주소, 핸드폰, 비고2, 날짜플래그컬럼들(6/1, 6/2, ...)
        """
        # 1. 배송전체: ID, 식종, 날짜별 O/X
        matrix = self._ds.read_delivery_matrix()
        if not matrix:
            return None

        # 2. 명단: ID → 성명, 핸드폰, 주소
        roster = self._ds.read_roster()
        roster_by_id: dict[str, dict] = {}
        for p in roster:
            pid = str(p.get("ID", "")).strip()
            if pid:
                roster_by_id[pid] = p

        # 3. 배송정보: 배송순서, 배송담당, 주소 (key: 핸드폰|주소)
        order_map = self._ds.build_order_map()

        # 4. 조인
        rows: list[dict] = []
        for m in matrix:
            pid = str(m.get("ID", "")).strip()
            person = roster_by_id.get(pid, {})

            name = person.get("성명", "")
            phone = (
                str(person.get("핸드폰", "")).strip().replace("-", "").replace(" ", "")
            )
            addr = str(person.get("도로명주소", "")).strip()

            # 배송정보 lookup
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

            # 날짜 플래그 컬럼 (6/1, 6/2, ...)
            for k, v in m.items():
                if k not in ("ID", "식종"):
                    row[k] = v

            rows.append(row)

        return pd.DataFrame(rows) if rows else None

    # ── 컬럼 리스트 ──

    def _render_column_list(self):
        self.col_list.clear()
        for c in self._order:
            mark = "✓" if self._enabled.get(c, True) else " "
            self.col_list.addItem(f"[{mark}] {c}")

    def _toggle_column(self):
        item = self.col_list.currentItem()
        if item is None:
            return
        text = item.text()
        col_name = text[4:]  # "[✓] 이름" → "이름"
        self._enabled[col_name] = not self._enabled.get(col_name, True)
        self._render_column_list()
        for i in range(self.col_list.count()):
            if self.col_list.item(i).text().endswith(col_name):
                self.col_list.setCurrentRow(i)
                break
        self._persist()

    def _move_column(self, d: int):
        row = self.col_list.currentRow()
        if row < 0:
            return
        new_row = row + d
        if 0 <= new_row < len(self._order):
            self._order[row], self._order[new_row] = (
                self._order[new_row],
                self._order[row],
            )
            self._render_column_list()
            self.col_list.setCurrentRow(new_row)
            self._persist()

    def _persist(self):
        self._s.set_delivery_settings(
            db_path="",
            order=self._order[:],
            enabled={c: bool(self._enabled.get(c, True)) for c in self._pick_cols},
            orientation=self.ori_combo.currentText(),
            split_mode=self.split_combo.currentText(),
        )

    # ── 실행 ──

    def _on_run(self):
        if self._df is None:
            if not self._load_db_data():
                QMessageBox.warning(self, "오류", "DB 데이터를 불러오지 못했습니다.")
                return

        qd = self.date_edit.date()
        m, d = qd.month(), qd.day()
        target_md = (m, d)

        # 배송전체 M/D 컬럼명 찾기
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

        cols = [c for c in self._order if self._enabled.get(c, False)]
        if not cols:
            QMessageBox.warning(self, "오류", "출력할 컬럼을 1개 이상 선택해 주세요.")
            return

        try:
            max_rows = int(self.max_rows_edit.text())
        except ValueError:
            QMessageBox.warning(self, "오류", "최대그룹에는 숫자를 입력해 주세요.")
            return

        self._persist()
        self.status_label.setText("처리 중...")
        from PyQt6.QtWidgets import QApplication

        QApplication.processEvents()

        try:
            meal = self.meal_combo.currentText()
            ship = self.ship_combo.currentText()

            choice = Choice(meal=meal, ship=ship, flag_col=flag_col, cols=cols)
            out = make_result(self._df, choice)

            date_label = f"{qd.year()}{m:02d}{d:02d}"
            show_excel(
                df=out,
                title="배송대타",
                meal=meal,
                ship=ship,
                date_str=date_label,
                ori=ORI_OPTIONS.index(self.ori_combo.currentText()),
                split=SPLIT_OPTIONS.index(self.split_combo.currentText()) + 1,
                max_rows=max_rows,
            )

            self.status_label.setText("완료")
            QMessageBox.information(self, "완료", "엑셀 출력이 완료되었습니다.")
        except Exception as e:
            self.status_label.setText(f"오류: {e}")
            QMessageBox.critical(self, "오류", f"실행 중 오류 발생:\n{e}")
            log.exception("_on_run")
