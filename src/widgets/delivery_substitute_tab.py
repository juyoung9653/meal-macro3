"""④ 배송대타 탭 — 연간 배송 데이터의 실제 배송행을 출력한다."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
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

from src.core.delivery_matrix import AnnualDeliveryBook
from src.core.settings import get_settings
from src.excel.delivery_io import (
    Choice,
    available_delivery_dates,
    available_meals,
    available_staff,
    make_result,
)
from src.excel.delivery_viewer import show_excel

log = logging.getLogger(__name__)

ORI_OPTIONS = ["자동", "세로", "가로"]
SPLIT_OPTIONS = ["동기준", "그룹기준"]


class DeliverySubstituteTab(QWidget):
    """④ 배송대타 탭 — 연간 배송 데이터 필터 및 A4 최적화 출력."""

    def __init__(
        self,
        meal_type: str = "",
        annual_book: AnnualDeliveryBook | None = None,
    ):
        super().__init__()
        self._s = get_settings()
        self._meal_type = meal_type
        self._book: AnnualDeliveryBook | None = None

        # 컬럼 설정
        self._pick_cols: list[str] = self._s.get_delivery_config("pick_cols") or []
        delivery_st = self._s.get_delivery_settings()
        cols_data = delivery_st.get("cols", {})
        stored_order = cols_data.get("order", self._pick_cols[:])
        self._order: list[str] = [
            column for column in stored_order if column in self._pick_cols
        ]
        self._order.extend(
            column for column in self._pick_cols if column not in self._order
        )
        stored_enabled = cols_data.get("enabled", {})
        self._enabled: dict[str, bool] = {
            column: bool(stored_enabled.get(column, True))
            for column in self._pick_cols
        }

        self._setup_ui()
        self.set_annual_book(annual_book)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── 식사 구분 / 배송담당 ──
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("식사 구분"))
        self.meal_combo = QComboBox()
        self.meal_combo.currentIndexChanged.connect(self._on_meal_changed)
        row1.addWidget(self.meal_combo)
        row1.addSpacing(20)
        row1.addWidget(QLabel("배송담당"))
        self.ship_combo = QComboBox()
        self.ship_combo.currentIndexChanged.connect(self._on_staff_changed)
        row1.addWidget(self.ship_combo)
        row1.addStretch()
        layout.addLayout(row1)

        # ── 실제 배송 입력 날짜 ──
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("날짜"))
        self.date_combo = QComboBox()
        self.date_combo.currentIndexChanged.connect(self._on_date_changed)
        row2.addWidget(self.date_combo)
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
        self.run_btn.setEnabled(False)
        self.run_btn.clicked.connect(self._on_run)
        layout.addWidget(self.run_btn)

        # ── 상태 ──
        self.status_label = QLabel("배송 데이터 미로드")
        layout.addWidget(self.status_label)

        layout.addStretch()

    # ── Public: MainWindow 연동 ──

    def set_meal_type(self, meal_type: str):
        """앱 업체명을 보관한다. 배송 식종 선택에는 사용하지 않는다."""
        self._meal_type = meal_type

    def set_annual_book(self, book: AnnualDeliveryBook | None):
        """연간 배송 책을 설정하고 실제 입력 날짜의 선택지를 초기화한다."""
        self._book = book
        self.run_btn.setEnabled(False)
        self._clear_combo(self.date_combo)
        self._clear_combo(self.meal_combo)
        self._clear_combo(self.ship_combo)
        self.date_combo.setEnabled(False)
        self.meal_combo.setEnabled(False)
        self.ship_combo.setEnabled(False)

        if book is None:
            self.status_label.setText("배송 데이터 미로드")
            return

        days = available_delivery_dates(book)
        if not days:
            self.status_label.setText("선택 가능한 배송 날짜가 없습니다.")
            return

        was_blocked = self.date_combo.blockSignals(True)
        try:
            for day in days:
                self.date_combo.addItem(day.strftime("%Y%m%d"), day)
            today = datetime.now(timezone.utc).astimezone().date()
            selected_index = days.index(today) if today in days else len(days) - 1
            self.date_combo.setCurrentIndex(selected_index)
        finally:
            self.date_combo.blockSignals(was_blocked)

        self.date_combo.setEnabled(True)
        self._refresh_meals()

    def _on_date_changed(self, _index: int = -1):
        self._refresh_meals()

    def _on_meal_changed(self, _index: int = -1):
        self._refresh_staff()

    def _on_staff_changed(self, _index: int = -1):
        self._update_run_state()

    def _refresh_meals(self):
        previous_meal = self.meal_combo.currentText()
        previous_staff = self.ship_combo.currentText()
        day = self._selected_day()
        meals = (
            available_meals(self._book, day)
            if self._book is not None and day is not None
            else []
        )
        self._replace_text_items(self.meal_combo, meals, previous_meal)
        self.meal_combo.setEnabled(bool(meals))
        self._refresh_staff(previous_staff)

    def _refresh_staff(self, preferred_staff: str | None = None):
        if preferred_staff is None:
            preferred_staff = self.ship_combo.currentText()

        day = self._selected_day()
        meal = self.meal_combo.currentText()
        staff = (
            available_staff(self._book, day, meal)
            if self._book is not None and day is not None and meal
            else []
        )
        self._replace_text_items(self.ship_combo, staff, preferred_staff)
        self.ship_combo.setEnabled(bool(staff))
        self._update_run_state()

    def _selected_day(self) -> date | None:
        value = self.date_combo.currentData()
        return value if isinstance(value, date) else None

    def _selection_problem(self) -> str | None:
        book = self._book
        if book is None:
            return "배송 데이터 미로드"

        day = self._selected_day()
        if day is None:
            if self.date_combo.count() == 0:
                return "선택 가능한 배송 날짜가 없습니다."
            return "선택한 날짜 정보가 올바른 date 값이 아닙니다."
        if day not in available_delivery_dates(book):
            return f"{day:%Y%m%d}은(는) 선택 가능한 배송 날짜가 아닙니다."

        meals = available_meals(book, day)
        if not meals:
            return f"{day:%Y%m%d}: 배송 대상 없음"

        meal = self.meal_combo.currentText()
        if meal not in meals:
            return "식종을 선택해 주세요."

        blank_staff_rows = self._blank_staff_rows(book, day, meal)
        if blank_staff_rows:
            rows = "; ".join(blank_staff_rows)
            return (
                f"{day:%Y%m%d} {meal} 배송 대상 중 배송담당이 빈칸인 "
                f"대상자: {rows}. 해당 식종은 실행할 수 없습니다."
            )

        ship = self.ship_combo.currentText()
        if ship not in available_staff(book, day, meal):
            return "유효한 배송담당을 선택해 주세요."
        return None

    def _update_run_state(self):
        problem = self._selection_problem()
        self.run_btn.setEnabled(problem is None)
        if problem is not None:
            self.status_label.setText(problem)
            return

        day = self._selected_day()
        if day is None:
            return
        self.status_label.setText(
            f"선택 완료: {day:%Y%m%d} / {self.meal_combo.currentText()} / "
            f"{self.ship_combo.currentText()}"
        )

    @staticmethod
    def _blank_staff_rows(
        book: AnnualDeliveryBook, day: date, meal: str
    ) -> list[str]:
        rows: list[str] = []
        seen_people: set[int] = set()
        for row in book.delivered_rows_on(day, meal=meal):
            if isinstance(row.staff, str) and row.staff.strip():
                continue
            if row.person.source_row in seen_people:
                continue
            seen_people.add(row.person.source_row)
            name = row.person.display_name or "(빈 값)"
            rows.append(f"성명={name}, Excel 원본 행={row.source_row}")
        return rows

    @staticmethod
    def _clear_combo(combo: QComboBox):
        was_blocked = combo.blockSignals(True)
        try:
            combo.clear()
        finally:
            combo.blockSignals(was_blocked)

    @staticmethod
    def _replace_text_items(
        combo: QComboBox, values: list[str], preferred: str
    ):
        was_blocked = combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItems(values)
            if values:
                preferred_index = combo.findText(preferred)
                combo.setCurrentIndex(max(preferred_index, 0))
        finally:
            combo.blockSignals(was_blocked)

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
            candidate = self.col_list.item(i)
            if candidate is not None and candidate.text().endswith(col_name):
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
            order=self._order[:],
            enabled={c: bool(self._enabled.get(c, True)) for c in self._pick_cols},
            orientation=self.ori_combo.currentText(),
            split_mode=self.split_combo.currentText(),
        )

    # ── 실행 ──

    def _on_run(self):
        problem = self._selection_problem()
        if problem is not None:
            self.run_btn.setEnabled(False)
            self.status_label.setText(problem)
            QMessageBox.warning(self, "실행 불가", problem)
            return

        book = self._book
        day = self._selected_day()
        if book is None or day is None:
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
        if max_rows <= 0:
            QMessageBox.warning(
                self, "오류", "최대그룹에는 1 이상의 정수를 입력해 주세요."
            )
            return

        self._persist()
        self.status_label.setText("처리 중...")
        from PyQt6.QtWidgets import QApplication

        QApplication.processEvents()

        meal = self.meal_combo.currentText()
        ship = self.ship_combo.currentText()
        try:
            choice = Choice(meal=meal, ship=ship, day=day, cols=cols)
            out = make_result(book, choice)
            if out.empty:
                message = "선택한 조건에 해당하는 배송 대상이 없습니다."
                self.status_label.setText("배송 대상 없음")
                QMessageBox.information(self, "안내", message)
                return

            show_excel(
                df=out,
                title="배송대타",
                meal=meal,
                ship=ship,
                date_str=day.strftime("%Y%m%d"),
                ori=ORI_OPTIONS.index(self.ori_combo.currentText()),
                split=SPLIT_OPTIONS.index(self.split_combo.currentText()) + 1,
                max_rows=max_rows,
            )

            self.status_label.setText("완료")
            QMessageBox.information(self, "완료", "엑셀 출력이 완료되었습니다.")
        except Exception as e:
            self.status_label.setText(f"오류: {e}")
            QMessageBox.critical(self, "오류", str(e))
            log.exception("_on_run")
