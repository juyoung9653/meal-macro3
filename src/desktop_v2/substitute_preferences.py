"""Computer-local substitute list visibility, scoped to the operating file."""

from hashlib import sha256

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout

from .theme import application_stylesheet, register_application_fonts


def preferences(database_id, settings=None):
    store = settings if settings is not None else QSettings("분당구", "급식 배송 운영 SQLite v2")
    return store, "documents/substitute_v1/" + sha256(database_id.encode("utf-8")).hexdigest()


def exclude_hidden_columns(store, key):
    value = store.value(key + "/exclude_all_hidden", True)
    return value if type(value) is bool else str(value).lower() != "false"


class SubstituteColumnVisibilityDialog(QDialog):
    def __init__(self, database_id, parent=None, *, settings=None):
        super().__init__(parent)
        self.store, self.key = preferences(database_id, settings)
        register_application_fonts()
        self.setWindowTitle("배송대타 칼럼 표시")
        self.setStyleSheet(application_stylesheet())
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        self.exclude = QCheckBox("모든 시트에서 숨긴 칼럼 제외")
        self.exclude.setChecked(exclude_hidden_columns(self.store, self.key))
        layout.addWidget(self.exclude)
        note = QLabel(
            "어느 한 시트에서라도 보이는 칼럼은 배송대타 목록에 표시합니다.\n"
            "해제하면 모든 시트에서 숨긴 칼럼도 선택할 수 있습니다.\n\n"
            "이 PC의 운영 파일별로 기억합니다."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self):
        self.store.setValue(self.key + "/exclude_all_hidden", self.exclude.isChecked())
        self.store.sync()
        super().accept()
