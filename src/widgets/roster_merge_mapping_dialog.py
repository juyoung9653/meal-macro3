"""합칠 명단의 Excel 열을 프로그램 역할에 직접 연결한다."""

from __future__ import annotations

from openpyxl.utils import get_column_letter
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

from ..services.roster_merge_service import (
    MERGE_MEAL_ROLE,
    MERGE_SOURCE_ROLES,
    REQUIRED_MERGE_SOURCE_ROLES,
    SourceLayout,
)


class RosterMergeMappingDialog(QDialog):
    """열 문자와 헤더명을 함께 보여주고 합칠 명단 매핑을 확정한다."""

    def __init__(self, layout: SourceLayout, parent: QWidget | None = None):
        super().__init__(parent)
        self._layout = layout
        self._combos: dict[str, QComboBox] = {}
        self._result: dict[str, int] | None = None
        self.setWindowTitle("합칠 명단 Excel 열 선택")
        self.setMinimumWidth(600)
        self.resize(680, 720)
        self._build_ui()
        self._update_validation()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        description = QLabel(
            f"시트: {self._layout.sheet_name} · 헤더 행: {self._layout.header_row}행\n"
            "각 항목에 해당하는 Excel 열을 직접 선택하세요. "
            "성명·생년월일·합쳐진 식종은 필수입니다. "
            "합쳐진 식종 셀은 조식·중식·석식을 한 셀에 함께 적은 열입니다."
        )
        description.setWordWrap(True)
        root.addWidget(description)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        form_widget = QWidget()
        form = QFormLayout(form_widget)
        for role in MERGE_SOURCE_ROLES:
            combo = QComboBox()
            combo.addItem("— 사용 안 함 —", None)
            for column, header in enumerate(self._layout.headers, start=1):
                display_header = header or "(빈 헤더)"
                combo.addItem(
                    f"{get_column_letter(column)} · {display_header}",
                    column,
                )
            initial = _initial_column(role, self._layout.headers)
            if initial is not None:
                combo.setCurrentIndex(combo.findData(initial))
            combo.currentIndexChanged.connect(self._update_validation)
            self._combos[role] = combo
            required = " *" if role in REQUIRED_MERGE_SOURCE_ROLES else " (선택)"
            label = "식종(한 셀에 합쳐진 값)" if role == MERGE_MEAL_ROLE else role
            form.addRow(f"{label}{required}:", combo)
        scroll.setWidget(form_widget)
        root.addWidget(scroll, 1)

        self.validation_label = QLabel()
        self.validation_label.setWordWrap(True)
        root.addWidget(self.validation_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.apply_button = QPushButton("이 열로 합치기 준비")
        self.apply_button.clicked.connect(self._accept_selection)
        cancel_button = QPushButton("취소")
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.apply_button)
        buttons.addWidget(cancel_button)
        root.addLayout(buttons)

    def _selected_columns(self) -> dict[str, int]:
        return {
            role: int(combo.currentData())
            for role, combo in self._combos.items()
            if isinstance(combo.currentData(), int)
        }

    def _validation_error(self) -> str | None:
        selected = self._selected_columns()
        missing = [role for role in REQUIRED_MERGE_SOURCE_ROLES if role not in selected]
        if missing:
            return "선택하지 않은 필수 열: " + ", ".join(missing)
        columns = tuple(selected.values())
        if len(set(columns)) != len(columns):
            return "각 항목은 서로 다른 Excel 열을 선택해야 합니다."
        return None

    def _update_validation(self) -> None:
        error = self._validation_error()
        self.apply_button.setEnabled(error is None)
        if error is None:
            self.validation_label.setText("선택한 열로 명단을 합칠 수 있습니다.")
            self.validation_label.setStyleSheet("color: #080;")
        else:
            self.validation_label.setText(error)
            self.validation_label.setStyleSheet("color: #b36b00;")

    def _accept_selection(self) -> None:
        if self._validation_error() is not None:
            return
        self._result = self._selected_columns()
        self.accept()

    @classmethod
    def get_selection(
        cls,
        layout: SourceLayout,
        *,
        parent: QWidget | None = None,
    ) -> dict[str, int] | None:
        dialog = cls(layout, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dict(dialog._result or {})


def _initial_column(role: str, headers: tuple[str, ...]) -> int | None:
    normalized_role = _normalize(role)
    exact = [
        column
        for column, header in enumerate(headers, start=1)
        if _normalize(header) == normalized_role
    ]
    if len(exact) == 1:
        return exact[0]
    if role == MERGE_MEAL_ROLE:
        candidates = [
            column
            for column, header in enumerate(headers, start=1)
            if any(word in _normalize(header) for word in ("식종", "급식", "식사"))
        ]
        if len(candidates) == 1:
            return candidates[0]
    return None


def _normalize(value: object) -> str:
    return "" if value is None else " ".join(str(value).split()).casefold()
