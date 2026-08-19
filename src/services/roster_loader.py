"""명단 파일 로더 — Excel/CSV/TXT 파일 파싱, 암호 해제, 헤더 중복 제거"""

import csv
import io
import os
import re
from collections.abc import Callable
from typing import Any, ClassVar

import msoffcrypto
from PyQt6.QtWidgets import (
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QWidget,
)
from python_calamine import CalamineWorkbook, PasswordError

from ..core.settings import get_settings


class RosterParser:
    """Pure parser: raw rows → (headers, data). No UI dependencies."""

    DEFAULT_NAME_KEYWORDS: ClassVar[list[str]] = [
        "성명",
        "이름",
        "성함",
        "학생명",
        "학생",
        "name",
        "Name",
    ]

    def __init__(self, name_keywords: list[str] | None = None):
        self._name_keywords = name_keywords

    def parse(
        self,
        rows: list[list],
        name_keywords: list[str] | None = None,
    ) -> tuple[list[str], list[dict]]:
        """Raw grid rows → (deduped headers, list of dict entries)."""
        if not rows:
            return [], []

        kw = name_keywords if name_keywords is not None else self._name_keywords
        header_index = RosterParser._find_header_row(rows, kw)
        raw_headers = [str(c) for c in rows[header_index]]
        headers = []
        seen: dict[str, int] = {}
        for h in raw_headers:
            h = h.strip()
            if not h:
                h = RosterParser._col_letter(len(headers))
            if h in seen:
                seen[h] += 1
                h = f"{h}_{seen[h]}"
            else:
                seen[h] = 1
            headers.append(h)

        name_idx = RosterParser._resolve_name_col(raw_headers, name_keywords=kw)

        data = []
        for row in rows[header_index + 1 :]:
            vals = [str(c) for c in row]
            name_val = vals[name_idx] if name_idx < len(vals) else ""
            if not RosterParser._is_name(name_val):
                continue
            entry = {
                headers[i]: vals[i] if i < len(vals) else ""
                for i in range(len(headers))
            }
            data.append(entry)

        return headers, data

    @staticmethod
    def _find_header_row(
        rows: list[list], name_keywords: list[str] | None = None
    ) -> int:
        """앞부분에서 이름 컬럼이 있는 실제 헤더 행을 찾고 없으면 첫 행을 쓴다."""

        keywords = name_keywords or RosterParser.DEFAULT_NAME_KEYWORDS
        for row_index, row in enumerate(rows[:20]):
            for column in row:
                value = str(column).strip()
                if any(
                    value == keyword or value.startswith(keyword)
                    for keyword in keywords
                ):
                    return row_index
        return 0

    @staticmethod
    def _col_letter(idx: int) -> str:
        """0-based column index ŌåÆ Excel letter (A, B, ..., Z, AA, ...)."""
        result = ""
        while True:
            result = chr(idx % 26 + 65) + result
            idx = idx // 26 - 1
            if idx < 0:
                break
        return result

    @staticmethod
    def _is_name(v: str) -> bool:
        """Check if value looks like a person's name (not a number or date)."""
        return bool(
            v
            and v.strip()
            and not re.fullmatch(r"\d+(\.\d+)?", v.strip())
            and not re.fullmatch(r"\d{2}\.\d{2}(\.\d{2,4})?", v.strip())
            and len(v.strip()) > 1
        )

    @staticmethod
    def _resolve_name_col(
        header: list,
        name_keywords: list[str] | None = None,
    ) -> int:
        """Find the name column index by keyword matching against header values."""
        if name_keywords is None:
            name_keywords = RosterParser.DEFAULT_NAME_KEYWORDS
        for i, col in enumerate(header):
            c = str(col).strip()
            if any(c == kw or c.startswith(kw) for kw in name_keywords):
                return i
        return 0


class FileReader:
    """Pure file I/O: path ŌåÆ raw rows. Handles Excel/CSV/TXT formats."""

    @staticmethod
    def _open_excel(
        path: str, password: str | None = None
    ) -> CalamineWorkbook | None:
        try:
            if password:
                with open(path, "rb") as f:
                    off = msoffcrypto.OfficeFile(f)
                    off.load_key(password=password)
                    buf = io.BytesIO()
                    off.decrypt(buf)
                buf.seek(0)
            else:
                return CalamineWorkbook.from_path(path)
        except PasswordError:
            return None
        return CalamineWorkbook.from_filelike(buf)

    @staticmethod
    def list_excel_sheets(
        path: str, password: str | None = None
    ) -> list[str] | None:
        """워크북 시트명을 순서대로 반환한다.

        암호가 필요하거나 틀리면 ``None``을 반환한다.
        """

        wb = FileReader._open_excel(path, password)
        return None if wb is None else list(wb.sheet_names)

    @staticmethod
    def read_excel(
        path: str,
        password: str | None = None,
        sheet_name: str | None = None,
    ) -> list[list] | None:
        """선택한 Excel 시트를 읽는다. 시트 미지정 시 첫 시트를 사용한다.

        암호가 필요하거나 틀리면 ``None``을 반환하고, 그 밖의 I/O 오류는
        호출자에게 전달한다.
        """

        wb = FileReader._open_excel(path, password)
        if wb is None:
            return None
        if sheet_name is not None and sheet_name not in wb.sheet_names:
            raise ValueError(f"시트 '{sheet_name}'을(를) 찾을 수 없습니다.")
        sheet = (
            wb.get_sheet_by_name(sheet_name)
            if sheet_name is not None
            else wb.get_sheet_by_index(0)
        )
        return list(sheet.iter_rows())

    @staticmethod
    def read_csv(path: str) -> list[list]:
        """Read CSV file (UTF-8 BOM) ŌåÆ raw rows."""
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            return list(reader)

    @staticmethod
    def read_txt(path: str) -> list[list]:
        """Read plain text file (one name per line) ŌåÆ raw rows with \"이름\" header."""
        with open(path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        return [["이름"]] + [[line] for line in lines]


class RosterLoader:
    """UI-aware wrapper: provides dialogs, delegates parsing to RosterParser."""

    def __init__(self, parent: QWidget):
        self._parent = parent
        self.roster_headers: list[str] = []
        self.roster_data: list[dict] = []
        self._parser = RosterParser()  # instance kept for method call consistency
        self._excel_passwords: dict[str, str] = {}

    # ── Public API ──

    def list_sheets(self, path: str) -> list[str] | None:
        """Excel 파일의 시트 목록을 반환하고 암호는 현재 실행 중에 재사용한다."""

        ext = os.path.splitext(path)[1].lower()
        if ext not in (".xlsx", ".xls", ".xlsb", ".ods"):
            return []
        try:
            result = self._run_excel_operation(
                path,
                lambda password: FileReader.list_excel_sheets(path, password),
            )
            return list(result) if result is not None else None
        except Exception as error:  # noqa: BLE001 - UI 경계에서 대화상자로 변환
            QMessageBox.critical(self._parent, "오류", f"시트 목록 읽기 실패:\n{error}")
            return None

    def load(
        self, path: str, sheet_name: str | None = None
    ) -> tuple[list[str], list[dict]] | None:
        """파일 로드 ŌåÆ (headers, data) 또는 None (실패 시 에러 대화상자 표시)."""
        ext = os.path.splitext(path)[1].lower()
        try:
            name_keywords = get_settings().get(
                "roster", "name_keywords", default=RosterParser.DEFAULT_NAME_KEYWORDS
            )
            self._parser = RosterParser(name_keywords=name_keywords)

            if ext in (".xlsx", ".xls", ".xlsb", ".ods"):
                raw = self._load_excel(path, sheet_name)
                if raw is None:
                    return None
                headers, data = self._parser.parse(raw)
            elif ext == ".csv":
                raw_rows = FileReader.read_csv(path)
                if not raw_rows:
                    QMessageBox.information(
                        self._parent, "알림", "데이터가 비어 있습니다."
                    )
                    return None
                headers, data = self._parser.parse(raw_rows)
            elif ext == ".txt":
                raw_rows = FileReader.read_txt(path)
                if not raw_rows:
                    QMessageBox.information(
                        self._parent, "알림", "데이터가 비어 있습니다."
                    )
                    return None
                headers, data = self._parser.parse(raw_rows)
            else:
                QMessageBox.warning(self._parent, "알림", f"지원하지 않는 형식: {ext}")
                return None
        except Exception as e:  # noqa: BLE001 - UI 경계에서 대화상자로 변환
            QMessageBox.critical(self._parent, "오류", f"파일 읽기 실패:\n{e}")
            return None

        if not data:
            QMessageBox.information(self._parent, "알림", "데이터가 비어 있습니다.")
            return None

        self.roster_headers = headers
        self.roster_data = data
        return headers, data

    # ── 내부 UI 로직 ──

    def _load_excel(
        self, path: str, sheet_name: str | None = None
    ) -> list[list] | None:
        """Load Excel with UI for password prompts."""
        result = self._run_excel_operation(
            path,
            lambda password: FileReader.read_excel(
                path, password=password, sheet_name=sheet_name
            ),
        )
        return result if isinstance(result, list) else None

    # ── 암호 해제 ──

    def _prompt_password(self) -> str | None:
        dlg = QInputDialog(self._parent)
        dlg.setWindowTitle("암호 해제")
        dlg.setLabelText("이 파일은 암호로 보호되어 있습니다.\n암호를 입력하세요:")
        dlg.setTextEchoMode(QLineEdit.EchoMode.Password)
        dlg.resize(300, 120)
        return dlg.textValue().strip() if dlg.exec() else None

    def _run_excel_operation(
        self,
        path: str,
        operation: Callable[[str | None], Any | None],
    ) -> Any | None:
        """암호 입력 재시도를 포함해 Excel 조회 작업 하나를 실행한다."""

        cached_password = self._excel_passwords.get(path)
        try:
            result = operation(cached_password)
        except Exception as error:
            if cached_password is None or not self._is_password_error(error):
                raise
            result = None
        if result is not None:
            return result

        password = self._prompt_password()
        attempts = 0
        max_attempts = 10
        while password is not None and attempts < max_attempts:
            try:
                result = operation(password)
            except Exception as error:
                if not self._is_password_error(error):
                    raise
                result = None
            if result is not None:
                self._excel_passwords[path] = password
                return result
            QMessageBox.warning(self._parent, "암호 오류", "암호가 올바르지 않습니다.")
            attempts += 1
            if attempts >= max_attempts:
                break
            password = self._prompt_password()
        if password is None:
            return None
        QMessageBox.warning(
            self._parent, "암호 오류", "최대 재시도 횟수를 초과했습니다."
        )
        return None

    @staticmethod
    def _is_password_error(error: Exception) -> bool:
        message = str(error).lower()
        return any(word in message for word in ("password", "key", "wrong"))
