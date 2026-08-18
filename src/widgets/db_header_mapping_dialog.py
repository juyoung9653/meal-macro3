"""선택한 ``db`` 테이블의 연도와 프로그램 필드를 직접 매핑한다."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..excel._parsing import parse_annual_date_header
from ..excel.annual_workbook import (
    HEADER_MAPPING_ROLES,
    REQUIRED_HEADER_ROLES,
    ROW_VARYING_PROFILE_HEADER_ROLES,
)


@dataclass(frozen=True)
class DbImportSelection:
    """대화상자에서 확정한 DB 헤더 매핑과 사용할 날짜 연도."""

    mapping: dict[str, str]
    year: int


class DbHeaderMappingDialog(QDialog):
    """프로그램에서 사용하는 필드를 실제 Excel 헤더에 연결한다."""

    def __init__(
        self,
        headers: Sequence[str],
        current_mapping: Mapping[str, str] | None = None,
        current_year: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("db 가져오기 설정")
        self.setMinimumWidth(560)
        self.resize(620, 700)

        self._headers = tuple(headers)
        self._selectable_headers = tuple(
            header
            for header in self._headers
            if header and parse_annual_date_header(header) is None
        )
        self._available_years = tuple(
            sorted(
                {
                    parsed.year
                    for header in self._headers
                    if (parsed := parse_annual_date_header(header)) is not None
                }
            )
        )
        self._current_mapping = dict(current_mapping or {})
        self._current_year = current_year
        self._combos: dict[str, QComboBox] = {}
        self._result: dict[str, str] = {}
        self._result_year: int | None = None

        self._build_ui()
        self._update_validation()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        description = QLabel(
            "사용할 날짜 연도와 각 프로그램 필드에 해당하는 Excel 헤더를 "
            "선택하세요.\n식종·배송담당과 인적사항은 청구서·확인서 "
            "생성을 위해 필수이며, 배송 순번만 사용하지 않을 수 있습니다.\n"
            "식종은 헤더 이름에 '식종'이 유일하게 포함되어 있으면 자동으로 "
            "선택합니다.\n"
            "*는 헤더 연결이 필수라는 뜻이며 개별 데이터 셀은 빈칸일 수 "
            "있습니다. 비고는 식종별 행마다 달라도 내용을 합쳐 사용합니다."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        year_form = QFormLayout()
        self.year_combo = QComboBox()
        self.year_combo.setObjectName("db_selected_year")
        self.year_combo.addItem("— 연도 선택 —", None)
        for year in self._available_years:
            self.year_combo.addItem(f"{year}년", year)

        selected_year = self._current_year
        if selected_year not in self._available_years and len(self._available_years) == 1:
            selected_year = self._available_years[0]
        selected_year_index = self.year_combo.findData(selected_year)
        self.year_combo.setCurrentIndex(max(selected_year_index, 0))
        self.year_combo.currentIndexChanged.connect(self._update_validation)
        year_form.addRow("사용 연도 *:", self.year_combo)
        layout.addLayout(year_form)

        field_label = QLabel("DB 헤더 매핑  (* 헤더 연결 필수)")
        field_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(field_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        form_widget = QWidget()
        form = QFormLayout(form_widget)
        for role in HEADER_MAPPING_ROLES:
            combo = QComboBox()
            combo.setObjectName(f"header_mapping_{role}")
            empty_label = (
                "— 헤더 선택 —"
                if role in REQUIRED_HEADER_ROLES
                else "— 사용 안 함 —"
            )
            combo.addItem(empty_label, "")
            for header in self._selectable_headers:
                combo.addItem(header, header)

            selected = _initial_header_selection(
                role,
                self._current_mapping.get(role, role),
                self._selectable_headers,
            )
            selected_index = combo.findData(selected)
            combo.setCurrentIndex(max(selected_index, 0))
            combo.currentIndexChanged.connect(self._update_validation)
            self._combos[role] = combo
            role_label = (
                f"{role} (행별 입력 가능)"
                if role in ROW_VARYING_PROFILE_HEADER_ROLES
                else role
            )
            suffix = " *" if role in REQUIRED_HEADER_ROLES else " (선택)"
            form.addRow(f"{role_label}{suffix}:", combo)
        scroll.setWidget(form_widget)
        layout.addWidget(scroll, 1)

        self.validation_label = QLabel()
        self.validation_label.setWordWrap(True)
        layout.addWidget(self.validation_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.save_button = QPushButton("설정 적용")
        self.save_button.clicked.connect(self._on_save)
        cancel_button = QPushButton("취소")
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.save_button)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)

    def _selected_values(self) -> dict[str, str]:
        return {
            role: str(combo.currentData() or "")
            for role, combo in self._combos.items()
        }

    def _validation_error(self) -> str | None:
        if not isinstance(self.year_combo.currentData(), int):
            if self._available_years:
                years = ", ".join(str(year) for year in self._available_years)
                return f"사용할 연도를 선택해 주세요. 발견 연도: {years}"
            return "YYYYMMDD 날짜 헤더가 없습니다."

        mapping = self._selected_values()
        missing = [role for role in REQUIRED_HEADER_ROLES if not mapping[role]]
        if missing:
            return "선택하지 않은 필수 필드: " + ", ".join(missing)

        selected = tuple(header for header in mapping.values() if header)
        if len(set(selected)) != len(selected):
            return "각 프로그램 필드는 서로 다른 헤더를 선택해야 합니다."
        return None

    def _update_validation(self, _index: int | None = None) -> None:
        error = self._validation_error()
        self.save_button.setEnabled(error is None)
        if error is None:
            self.validation_label.setText("✓ 설정을 적용할 수 있습니다.")
            self.validation_label.setStyleSheet("color: #080;")
        else:
            self.validation_label.setText(error)
            self.validation_label.setStyleSheet("color: #b36b00;")

    def _on_save(self) -> None:
        if self._validation_error() is not None:
            self._update_validation()
            return
        selected_year = self.year_combo.currentData()
        if not isinstance(selected_year, int):
            return
        self._result = self._selected_values()
        self._result_year = selected_year
        self.accept()

    @property
    def selected_mapping(self) -> dict[str, str]:
        return dict(self._result)

    @property
    def selected_year(self) -> int | None:
        return self._result_year

    @classmethod
    def get_selection(
        cls,
        headers: Sequence[str],
        current_mapping: Mapping[str, str] | None = None,
        current_year: int | None = None,
        parent: QWidget | None = None,
    ) -> DbImportSelection | None:
        dialog = cls(
            headers,
            current_mapping=current_mapping,
            current_year=current_year,
            parent=parent,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        year = dialog.selected_year
        if year is None:  # pragma: no cover - 저장 버튼 검증의 방어 코드
            return None
        return DbImportSelection(dialog.selected_mapping, year)

    @classmethod
    def get_mapping(
        cls,
        headers: Sequence[str],
        current_mapping: Mapping[str, str] | None = None,
        parent: QWidget | None = None,
    ) -> dict[str, str] | None:
        """단일 연도 호출부를 위한 이전 API 호환 래퍼."""
        selection = cls.get_selection(
            headers,
            current_mapping=current_mapping,
            parent=parent,
        )
        return None if selection is None else selection.mapping


def _initial_header_selection(
    role: str,
    configured_header: str,
    available_headers: Sequence[str],
) -> str:
    """저장값·표준명·식종 부분 일치 순으로 안전한 초기 선택을 찾는다."""
    if configured_header in available_headers:
        return configured_header
    if role in available_headers:
        return role
    if role != "식종":
        return ""

    role_key = "".join(role.split()).casefold()
    matches = tuple(
        header
        for header in available_headers
        if role_key in "".join(header.split()).casefold()
    )
    return matches[0] if len(matches) == 1 else ""
