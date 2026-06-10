import logging
import sys

from PyQt6.QtWidgets import QApplication

from src.core.settings import get_settings
from src.main_window import MainWindow

logging.basicConfig(
    level=logging.NOTSET,
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
