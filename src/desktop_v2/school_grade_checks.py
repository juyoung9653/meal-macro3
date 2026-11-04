"""School-specific grade choices for meal exceptions."""
from PySide6.QtWidgets import QWidget, QHBoxLayout, QCheckBox
from src.domain.school_calendar import normalize_grade, normalize_school_name


class SchoolGradeChecks(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._checks = {}
        self._school_name = ""

    def set_school(self, school_name):
        self._school_name = school_name
        self.set_grades(())

    def set_grades(self, grades):
        selected = {normalize_grade(item) for item in grades}
        key = normalize_school_name(self._school_name) if self._school_name else ""
        secondary = "중학교" in key or "고등학교" in key or key.endswith(("중", "고"))
        count = 3 if secondary and "초등학교" not in key else 6
        labels = [f"{grade}학년" for grade in range(1, count + 1)]
        labels.extend(sorted(selected - set(labels)))
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._checks.clear()
        for label in labels:
            box = QCheckBox(label)
            box.setChecked(label in selected)
            self._layout.addWidget(box)
            self._checks[label] = box
        self._layout.addStretch(1)

    def selected_grades(self):
        return frozenset(label for label, box in self._checks.items() if box.isChecked())
