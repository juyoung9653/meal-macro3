import csv
import io
import os
import re

import msoffcrypto
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from python_calamine import CalamineWorkbook, PasswordError

from ..settings import get_settings


class RosterTab(QWidget):
    """명단 파일 선택 및 조회 탭"""

    def __init__(self):
        super().__init__()
        self.roster_path = None
        self.roster_data = []
        self.roster_headers = []
        s = get_settings()
        org = s.get("app", "organization", default="분당구")
        title = s.get("app", "title", default="급식비정산")
        self._settings = QSettings(org, title)
        self._setup_ui()
        self._restore_state()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        file_group = QGroupBox("명단 파일 선택")
        file_layout = QHBoxLayout(file_group)
        self.file_label = QLabel("선택된 파일 없음")
        self.file_label.setStyleSheet("color: #888;")
        self.select_btn = QPushButton("파일 탐색")
        self.select_btn.clicked.connect(self._select_file)
        file_layout.addWidget(self.file_label, stretch=1)
        file_layout.addWidget(self.select_btn)

        table_group = QGroupBox("명단 목록")
        table_layout = QVBoxLayout(table_group)
        self.roster_table = QTableWidget()
        self.roster_table.setColumnCount(4)
        self.roster_table.setHorizontalHeaderLabels(["번호", "이름", "소속", "비고"])
        self.roster_table.horizontalHeader().setStretchLastSection(True)
        self.roster_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.roster_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.roster_table.setAlternatingRowColors(True)
        table_layout.addWidget(self.roster_table)

        self.info_label = QLabel("총 0명")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignRight)

        layout.addWidget(file_group)
        layout.addWidget(table_group, stretch=1)
        layout.addWidget(self.info_label)

    def _select_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "명단 파일 선택",
            "",
            "지원 파일 (*.xlsx *.xls *.csv *.txt);;Excel (*.xlsx *.xls);;CSV (*.csv);;텍스트 (*.txt);;모든 파일 (*.*)",
        )
        if path:
            self.roster_path = path
            self.file_label.setText(os.path.basename(path))
            self.file_label.setStyleSheet("color: #000;")
            self._settings.setValue("roster/last_path", path)
            self._settings.sync()
            self._load_roster(path)

    def _restore_state(self):
        last = self._settings.value("roster/last_path", "")
        if last and os.path.exists(last):
            self.roster_path = last
            self.file_label.setText(os.path.basename(last))
            self.file_label.setStyleSheet("color: #000;")
            self._load_roster(last)

    # ── 메인 로드 ──

    def _load_roster(self, path: str):
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext in (".xlsx", ".xls", ".xlsb", ".ods"):
                raw = self._load_excel()
            elif ext == ".csv":
                raw = self._load_csv(path)
            elif ext == ".txt":
                raw = self._load_txt(path)
            else:
                QMessageBox.warning(self, "알림", f"지원하지 않는 형식: {ext}")
                return
        except Exception as e:
            QMessageBox.critical(self, "오류", f"파일 읽기 실패:\n{e}")
            return

        if not raw:
            QMessageBox.information(self, "알림", "데이터가 비어 있습니다.")
            return

        self.roster_headers, self.roster_data = raw
        self._update_table()

    # ── 파일별 파서 ──

    def _load_excel(self):
        """(headers, data) 반환, data key=header명"""
        try:
            wb = CalamineWorkbook.from_path(self.roster_path)
        except PasswordError:
            pw = self._prompt_password()
            if pw is None:
                return None
            wb = self._open_encrypted(
                self.roster_path, pw
            )  # handles retries internally
            if wb is None:
                return None
        sheet = wb.get_sheet_by_index(0)
        rows = list(sheet.iter_rows())
        return self._parse_rows(rows)

    def _load_csv(self, path: str):
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)
        return self._parse_rows(rows)

    def _load_txt(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        headers = ["이름"]
        data = [{"이름": line} for line in lines]
        return headers, data

    # ── 암호 ──

    def _prompt_password(self) -> str | None:
        dlg = QInputDialog(self)
        dlg.setWindowTitle("암호 해제")
        dlg.setLabelText("이 파일은 암호로 보호되어 있습니다.\n암호를 입력하세요:")
        dlg.setTextEchoMode(QLineEdit.EchoMode.Password)
        dlg.resize(300, 120)
        return dlg.textValue().strip() if dlg.exec() else None

    def _open_encrypted(self, path: str, password: str):
        """암호 걸린 엑셀 파일 열기. 실패시 반복 재시도."""
        attempts = 0
        max_attempts = 10
        while attempts < max_attempts:
            try:
                with open(path, "rb") as f:
                    off = msoffcrypto.OfficeFile(f)
                    off.load_key(password=password)
                    buf = io.BytesIO()
                    off.decrypt(buf)
                buf.seek(0)
                return CalamineWorkbook.from_filelike(buf)
            except Exception as e:
                msg = str(e).lower()
                if "password" in msg or "key" in msg or "wrong" in msg:
                    QMessageBox.warning(self, "암호 오류", "암호가 올바르지 않습니다.")
                    pw = self._prompt_password()
                    if pw is None:
                        return None  # user cancelled
                    password = pw
                    attempts += 1
                    continue
                raise  # non-password error → propagate
        QMessageBox.warning(self, "암호 오류", "최대 재시도 횟수를 초과했습니다.")
        return None

    # ── 파싱 코어 ──

    @staticmethod
    def _col_letter(idx: int) -> str:
        """0-based index → Excel column letter (0=A, 1=B, ..., 25=Z, 26=AA, ...)"""
        result = ""
        i = idx
        while True:
            result = chr(i % 26 + 65) + result
            i = i // 26 - 1
            if i < 0:
                break
        return result

    @staticmethod
    def _is_name(v: str) -> bool:
        if not v or not v.strip():
            return False
        v = v.strip()
        if re.fullmatch(r"\d+(\.\d+)?", v):
            return False
        if re.fullmatch(r"\d{2}\.\d{2}(\.\d{2,4})?", v):
            return False
        if len(v) <= 1:
            return False
        return True

    def _parse_rows(self, rows: list) -> tuple:
        """
        rows[0] = 헤더
        데이터 행 중 이름값이 실제 이름인 것만 dict로.
        dict key = 헤더명 (중복시 _2, _3 …). 빈 헤더는 인덱스로.
        반환: (headers: list[str], data: list[dict])
        """
        if not rows:
            return [], []

        # 헤더 정리 (중복 방지)
        raw_headers = [str(c) for c in rows[0]]
        headers = []
        seen = {}
        for h in raw_headers:
            h = h.strip()
            if not h:
                h = self._col_letter(len(headers))
            if h in seen:
                seen[h] += 1
                h = f"{h}_{seen[h]}"
            else:
                seen[h] = 1
            headers.append(h)

        # name-like 컬럼 찾기
        name_idx = self._resolve_name_col(raw_headers)

        data = []
        for row in rows[1:]:
            vals = [str(c) for c in row]
            name_val = vals[name_idx] if name_idx < len(vals) else ""
            if not self._is_name(name_val):
                continue
            entry = {
                headers[i]: vals[i] if i < len(vals) else ""
                for i in range(len(headers))
            }
            data.append(entry)

        return headers, data

    @staticmethod
    def _resolve_name_col(header: list) -> int:
        """이름 컬럼 인덱스 찾기"""
        s = get_settings()
        keywords = s.get(
            "roster",
            "name_keywords",
            default=["성명", "이름", "성함", "학생명", "학생", "name", "Name"],
        )
        for i, col in enumerate(header):
            c = str(col).strip()
            if any(c == kw or c.startswith(kw) for kw in keywords):
                return i
        return 0

    # ── UI 갱신 (미리보기용 name/dept/note) ──

    def _update_table(self):
        preview_cols = self._resolve_preview_cols()
        self.roster_table.setRowCount(len(self.roster_data))
        for i, row in enumerate(self.roster_data):
            self.roster_table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.roster_table.setItem(
                i, 1, QTableWidgetItem(row.get(preview_cols["name"], ""))
            )
            self.roster_table.setItem(
                i, 2, QTableWidgetItem(row.get(preview_cols["dept"], ""))
            )
            self.roster_table.setItem(
                i, 3, QTableWidgetItem(row.get(preview_cols["note"], ""))
            )
        self.info_label.setText(f"총 {len(self.roster_data)}명")

    def _resolve_preview_cols(self) -> dict:
        s = get_settings()
        if not self.roster_headers:
            return {"name": "성명", "dept": "학교명", "note": "비고"}
        h = self.roster_headers
        name_kw = s.get("roster", "preview_name_kw", default=["성명", "이름", "성함"])
        dept_kw = s.get("roster", "preview_dept_kw", default=["학교명", "학교", "소속"])
        note_kw = s.get(
            "roster", "preview_note_kw", default=["학기중 비고", "비고", "특이사항"]
        )
        name = next(
            (c for c in h if any(c.startswith(k) for k in name_kw)),
            h[0] if h else "성명",
        )
        dept = next(
            (c for c in h if any(c.startswith(k) for k in dept_kw)),
            h[1] if len(h) > 1 else "",
        )
        note = next(
            (c for c in h if any(c.startswith(k) for k in note_kw)), h[-1] if h else ""
        )
        return {"name": name, "dept": dept, "note": note}

    def get_roster_path(self):
        return self.roster_path

    def get_roster_data(self):
        return self.roster_data

    def get_roster_headers(self):
        return self.roster_headers
