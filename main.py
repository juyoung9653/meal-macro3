import logging
import os
import sys
import warnings

# openpyxl: Data Validation extension 경고 (무해) 제거
warnings.filterwarnings(
    "ignore",
    message="Data Validation extension is not supported",
    category=UserWarning,
)

from PyQt6.QtWidgets import QApplication, QMessageBox

from src.core.settings import get_settings
from src.main_window import MainWindow

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(name)s | %(message)s",
)

REQUIRED_TEMPLATES: list[tuple[str, str]] = [
    ("청구서 템플릿", "assets/급식_청구서템플릿.xlsx"),
    ("확인서 템플릿", "assets/급식_확인서템플릿.xlsx"),
    ("배송리포트 템플릿", "assets/report_template.xlsx"),
]


def _check_templates() -> None:
    """필수 템플릿 존재 여부 확인. 없으면 안내 후 종료."""
    from src.core.paths import asset_path

    missing = []
    for name, rel_path in REQUIRED_TEMPLATES:
        full = asset_path(rel_path)
        if not os.path.exists(full):
            missing.append(f"• {name}: {rel_path}")

    if missing:
        msg = (
            "필수 템플릿 파일을 찾을 수 없습니다.\n\n" + "\n".join(missing) + "\n\n"
            "exe 파일과 같은 폴더에 assets/ 폴더를 넣어주세요."
        )
        QMessageBox.critical(None, "템플릿 누락", msg)
        sys.exit(1)


def main():
    get_settings()  # 최초 로드 보장

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    _check_templates()

    window = MainWindow()
    window.show()
    window.post_init_load()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
