"""명단 파일 로더 — Excel/CSV/TXT 파일 파싱, 암호 해제, 헤더 중복 제거"""

import csv
import io
import os
import re

import msoffcrypto
from PyQt6.QtWidgets import (
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QWidget,
)
from python_calamine import CalamineWorkbook, PasswordError

from ..core.settings import get_settings


class RosterLoader:
    """명단 파일 로드 전용 클래스. UI 없는 순수 로직 (단, 에러 대화상자는 parent 통해 표시)."""

    def __init__(self, parent: QWidget):
        self._parent = parent
        self.roster_headers: list[str] = []
        self.roster_data: list[dict] = []

    # ── Public API ──

    def load(self, path: str) -> tuple[list[str], list[dict]] | None:
        """파일 로드 → (headers, data) 또는 None (실패 시 에러 대화상자 표시)."""
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext in (".xlsx", ".xls", ".xlsb", ".ods"):
                raw = self._load_excel(path)
            elif ext == ".csv":
                raw = self._load_csv(path)
            elif ext == ".txt":
                raw = self._load_txt(path)
            else:
                QMessageBox.warning(self._parent, "알림", f"지원하지 않는 형식: {ext}")
                return None
        except Exception as e:
            QMessageBox.critical(self._parent, "오류", f"파일 읽기 실패:\n{e}")
            return None

        if not raw:
            QMessageBox.information(self._parent, "알림", "데이터가 비어 있습니다.")
            return None

        headers, data = raw
        self.roster_headers = headers
        self.roster_data = data
        return headers, data

    # ── 내부 파서 ──

    def _load_excel(self, path: str):
        try:
            wb = CalamineWorkbook.from_path(path)
        except PasswordError:
            pw = self._prompt_password()
            if pw is None:
                return None
            wb = self._open_encrypted(path, pw)
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

    # ── 암호 해제 ──

    def _prompt_password(self) -> str | None:
        dlg = QInputDialog(self._parent)
        dlg.setWindowTitle("암호 해제")
        dlg.setLabelText("이 파일은 암호로 보호되어 있습니다.\n암호를 입력하세요:")
        dlg.setTextEchoMode(QLineEdit.EchoMode.Password)
        dlg.resize(300, 120)
        return dlg.textValue().strip() if dlg.exec() else None

    def _open_encrypted(self, path: str, password: str):
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
                    QMessageBox.warning(
                        self._parent, "암호 오류", "암호가 올바르지 않습니다."
                    )
                    pw = self._prompt_password()
                    if pw is None:
                        return None
                    password = pw
                    attempts += 1
                    continue
                raise
        QMessageBox.warning(
            self._parent, "암호 오류", "최대 재시도 횟수를 초과했습니다."
        )
        return None

    # ── 정적 헬퍼 ──

    @staticmethod
    def _col_letter(idx: int) -> str:
        """0-based column index → Excel letter (A, B, ..., Z, AA, ...)."""
        result = ""
        while True:
            result = chr(idx % 26 + 65) + result
            idx = idx // 26 - 1
            if idx < 0:
                break
        return result

    @staticmethod
    def _is_name(v: str) -> bool:
        return bool(
            v
            and v.strip()
            and not re.fullmatch(r"\d+(\.\d+)?", v.strip())
            and not re.fullmatch(r"\d{2}\.\d{2}(\.\d{2,4})?", v.strip())
            and len(v.strip()) > 1
        )

    @staticmethod
    def _resolve_name_col(header: list) -> int:
        keywords = get_settings().get(
            "roster",
            "name_keywords",
            default=["성명", "이름", "성함", "학생명", "학생", "name", "Name"],
        )
        for i, col in enumerate(header):
            c = str(col).strip()
            if any(c == kw or c.startswith(kw) for kw in keywords):
                return i
        return 0

    # ── 파싱 코어 ──

    def _parse_rows(self, rows: list) -> tuple:
        if not rows:
            return [], []

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
