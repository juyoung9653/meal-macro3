import logging
import sys
import warnings

# openpyxl: Data Validation extension 경고 (무해) 제거
warnings.filterwarnings(
    "ignore",
    message="Data Validation extension is not supported",
    category=UserWarning,
)

from PyQt6.QtWidgets import QApplication

from src.core.settings import get_settings
from src.main_window import MainWindow

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(name)s | %(message)s",
)


def main():
    get_settings()  # 최초 로드 보장

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
