import unittest

from src.config import FONT_BASE_SIZE
from src.excel.page_formatting.layout import MIN_FONT_SIZE, _font_sizes


class FontSizeTests(unittest.TestCase):
    def test_uses_configured_base_sizes(self) -> None:
        self.assertEqual(_font_sizes(0), (FONT_BASE_SIZE + 2, FONT_BASE_SIZE))

    def test_never_returns_excel_invalid_size(self) -> None:
        self.assertEqual(_font_sizes(-100), (MIN_FONT_SIZE, MIN_FONT_SIZE))


if __name__ == "__main__":
    unittest.main()
