"""Tests for _parsing helpers: _normalize_headers and _rows_to_dicts."""

from src.excel._parsing import _normalize_headers, _rows_to_dicts

# ── _normalize_headers ──


def test_normalize_headers_normal():
    """Normal headers pass through unchanged."""
    raw = ["Name", "Age", "City"]
    assert _normalize_headers(raw) == ["Name", "Age", "City"]


def test_normalize_headers_empty():
    """Empty headers get ColN names."""
    raw = ["Name", "", "Age"]
    assert _normalize_headers(raw) == ["Name", "Col1", "Age"]


def test_normalize_headers_all_empty():
    """All empty headers get sequential ColN names."""
    raw = ["", "", ""]
    assert _normalize_headers(raw) == ["Col0", "Col1", "Col2"]


def test_normalize_headers_duplicates():
    """Duplicate headers get _N suffix."""
    raw = ["Name", "Name", "Age"]
    assert _normalize_headers(raw) == ["Name", "Name_2", "Age"]


def test_normalize_headers_multiple_duplicates():
    """Multiple duplicates of the same name increment correctly."""
    raw = ["A", "A", "B", "A"]
    assert _normalize_headers(raw) == ["A", "A_2", "B", "A_3"]


def test_normalize_headers_newline():
    """Newlines in headers are replaced with spaces."""
    raw = ["First\nName", "Age"]
    assert _normalize_headers(raw) == ["First Name", "Age"]


def test_normalize_headers_empty_sequential():
    """Empty headers get sequential ColN names (Col0, Col1, ...)."""
    raw = ["", "", "Name"]
    result = _normalize_headers(raw)
    assert result == ["Col0", "Col1", "Name"]


def test_normalize_headers_collision_with_generated_coln():
    """Real header matching a generated ColN gets _2 suffix."""
    raw = ["", "Col0", "Name"]
    result = _normalize_headers(raw)
    assert result == ["Col0", "Col0_2", "Name"]


def test_normalize_headers_strips_whitespace():
    """Whitespace-only headers are treated as empty."""
    raw = ["  Name  ", "   ", "City"]
    assert _normalize_headers(raw) == ["Name", "Col1", "City"]


# ── _rows_to_dicts ──


def test_rows_to_dicts_normal():
    """Normal data rows become dicts with correct keys."""
    headers = ["Name", "Age"]
    rows = [["Alice", "30"], ["Bob", "25"]]
    assert _rows_to_dicts(headers, [["Alice", "30"], ["Bob", "25"]]) == [
        {"Name": "Alice", "Age": "30"},
        {"Name": "Bob", "Age": "25"},
    ]


def test_rows_to_dicts_skip_empty_rows():
    """Rows with all-empty values are skipped entirely."""
    headers = ["Name", "Age"]
    rows = [["Alice", "30"], ["", ""], ["Bob", "25"]]
    assert _rows_to_dicts(headers, rows) == [
        {"Name": "Alice", "Age": "30"},
        {"Name": "Bob", "Age": "25"},
    ]


def test_rows_to_dicts_mixed_empty_nonempty():
    """Rows with partial data are kept; only fully empty rows are skipped."""
    headers = ["Name", "Age"]
    rows = [["Alice", "30"], ["", "25"], ["Bob", ""]]
    assert _rows_to_dicts(headers, rows) == [
        {"Name": "Alice", "Age": "30"},
        {"Name": "", "Age": "25"},
        {"Name": "Bob", "Age": ""},
    ]


def test_rows_to_dicts_all_empty():
    """All rows empty → empty result."""
    headers = ["A", "B"]
    rows = [["", ""], ["", ""]]
    assert _rows_to_dicts(headers, rows) == []


def test_rows_to_dicts_none_values():
    """None values become empty strings."""
    headers = ["A", "B"]
    rows = [[None, "hello"]]
    assert _rows_to_dicts(headers, rows) == [{"A": "", "B": "hello"}]


def test_rows_to_dicts_short_row():
    """Row shorter than headers gets empty strings for missing columns."""
    headers = ["A", "B", "C"]
    rows = [["x", "y"]]  # missing C
    assert _rows_to_dicts(headers, rows) == [{"A": "x", "B": "y", "C": ""}]


def test_rows_to_dicts_long_row():
    """Row longer than headers — extra values are ignored."""
    headers = ["A", "B"]
    rows = [["x", "y", "z"]]  # extra z
    assert _rows_to_dicts(headers, rows) == [{"A": "x", "B": "y"}]
