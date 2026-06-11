"""Tests for src.services.roster_loader.RosterParser.

Pure parser — no PyQt imports, no UI dependencies.
"""

import pytest

from src.services.roster_loader import RosterParser


class TestColLetter:
    """RosterParser._col_letter: 0-based index → Excel column letter."""

    def test_a(self):
        assert RosterParser._col_letter(0) == "A"

    def test_z(self):
        assert RosterParser._col_letter(25) == "Z"

    def test_aa(self):
        assert RosterParser._col_letter(26) == "AA"

    def test_zz(self):
        assert RosterParser._col_letter(701) == "ZZ"

    def test_az(self):
        assert RosterParser._col_letter(51) == "AZ"

    def test_ba(self):
        assert RosterParser._col_letter(52) == "BA"


class TestIsName:
    """RosterParser._is_name: check if value looks like a person's name."""

    def test_valid_korean_name(self):
        assert RosterParser._is_name("홍길동")

    def test_valid_two_char_name(self):
        assert RosterParser._is_name("길동")

    def test_single_char_rejected(self):
        assert not RosterParser._is_name("김")

    def test_number_rejected(self):
        assert not RosterParser._is_name("12345")

    def test_decimal_rejected(self):
        assert not RosterParser._is_name("12.34")

    def test_date_rejected(self):
        assert not RosterParser._is_name("01.15")

    def test_full_date_rejected(self):
        assert not RosterParser._is_name("25.01.15")

    def test_empty_rejected(self):
        assert not RosterParser._is_name("")

    def test_whitespace_rejected(self):
        assert not RosterParser._is_name("   ")

    def test_zero_rejected(self):
        assert not RosterParser._is_name("0")


class TestResolveNameCol:
    """RosterParser._resolve_name_col: find name column by keywords."""

    def test_finds_seongmyeong(self):
        headers = ["학교명", "성명", "학년"]
        assert RosterParser._resolve_name_col(headers) == 1

    def test_finds_ireum(self):
        headers = ["학교명", "이름", "비고"]
        assert RosterParser._resolve_name_col(headers) == 1

    def test_finds_seongham(self):
        headers = ["번호", "성함", "학년"]
        assert RosterParser._resolve_name_col(headers) == 1

    def test_finds_haksaengmyeong(self):
        headers = ["학생명", "학년", "반"]
        assert RosterParser._resolve_name_col(headers) == 0

    def test_finds_haksaeng(self):
        headers = ["학생", "학교명"]
        assert RosterParser._resolve_name_col(headers) == 0

    def test_finds_name_english(self):
        headers = ["Name", "School"]
        assert RosterParser._resolve_name_col(headers) == 0

    def test_falls_back_to_zero(self):
        headers = ["학교명", "학년", "반"]
        assert RosterParser._resolve_name_col(headers) == 0

    def test_empty_headers(self):
        assert RosterParser._resolve_name_col([]) == 0

    def test_partial_match_startswith(self):
        """Keyword '성명' matches header '성명_1' (starts with)."""
        headers = ["성명_1", "학교명"]
        assert RosterParser._resolve_name_col(headers) == 0

    def test_custom_keywords(self):
        """_resolve_name_col accepts explicit name_keywords override."""
        headers = ["학과", "교수명", "직급"]
        # Default keywords won't find "교수명"
        assert RosterParser._resolve_name_col(headers) == 0
        # Custom keywords does find it
        assert RosterParser._resolve_name_col(headers, name_keywords=["교수명"]) == 1


class TestParse:
    """RosterParser.parse: raw rows → (headers, data)."""

    # ── Fixture for default parser ──

    @pytest.fixture
    def parser(self):
        return RosterParser()

    # ── Default keyword path (no explicit name_keywords) ──

    def test_empty_rows(self, parser):
        headers, data = parser.parse([])
        assert headers == []
        assert data == []

    def test_single_header_row_only(self, parser):
        rows = [["성명", "학교명"]]
        headers, data = parser.parse(rows)
        assert headers == ["성명", "학교명"]
        assert data == []

    def test_normal_rows(self, parser):
        rows = [
            ["성명", "학교명"],
            ["홍길동", "분당초"],
            ["김철수", "수내초"],
        ]
        headers, data = parser.parse(rows)
        assert headers == ["성명", "학교명"]
        assert len(data) == 2
        assert data[0] == {"성명": "홍길동", "학교명": "분당초"}
        assert data[1] == {"성명": "김철수", "학교명": "수내초"}

    def test_filters_non_name_rows(self, parser):
        rows = [
            ["성명", "학교명"],
            ["홍길동", "분당초"],
            ["12345", "수내초"],
            ["01.15", "수내초"],
            ["김철수", "매화초"],
        ]
        headers, data = parser.parse(rows)
        assert len(data) == 2
        names = [d["성명"] for d in data]
        assert "홍길동" in names
        assert "김철수" in names
        assert "12345" not in [d.get("성명", "") for d in data]

    def test_dedup_headers(self, parser):
        rows = [
            ["성명", "성명", "학교명"],
            ["홍길동", "부가정보", "분당초"],
        ]
        headers, data = parser.parse(rows)
        assert headers == ["성명", "성명_2", "학교명"]
        assert data[0]["성명"] == "홍길동"
        assert data[0]["성명_2"] == "부가정보"

    def test_empty_header_gets_col_letter(self, parser):
        rows = [
            ["성명", "", "학교명"],
            ["홍길동", "3반", "분당초"],
        ]
        headers, data = parser.parse(rows)
        assert headers == ["성명", "B", "학교명"]
        assert data[0]["B"] == "3반"

    def test_triple_dedup(self, parser):
        rows = [
            ["성명", "성명", "성명"],
            ["홍길동", "정보1", "정보2"],
        ]
        headers, data = parser.parse(rows)
        assert headers == ["성명", "성명_2", "성명_3"]

    def test_missing_value_in_row(self, parser):
        rows = [
            ["성명", "학교명", "비고"],
            ["홍길동", "분당초"],
        ]
        headers, data = parser.parse(rows)
        assert len(data) == 1
        assert data[0]["성명"] == "홍길동"
        assert data[0]["학교명"] == "분당초"
        assert data[0]["비고"] == ""

    def test_name_out_of_bounds_empty_string(self, parser):
        """When name_idx exceeds row length, name_val is empty → row filtered."""
        rows = [
            ["성명", "학교명"],
            [""],
        ]
        headers, data = parser.parse(rows)
        assert len(data) == 0

    def test_multiple_empty_headers_get_unique_letters(self, parser):
        rows = [
            ["", "", "성명"],
            ["A1", "B1", "홍길동"],
        ]
        headers, data = parser.parse(rows)
        assert headers == ["A", "B", "성명"]
        assert data[0]["A"] == "A1"
        assert data[0]["B"] == "B1"

    # ── Custom name_keywords via constructor ──

    def test_constructor_custom_keywords(self):
        """Parser constructed with custom keywords uses them."""
        p = RosterParser(name_keywords=["학생이름", "학생"])
        rows = [
            ["학생이름", "학교"],
            ["홍길동", "분당초"],
        ]
        headers, data = p.parse(rows)
        assert len(data) == 1
        assert data[0]["학생이름"] == "홍길동"

    def test_constructor_default_fallback(self):
        """Parser with name_keywords=None uses DEFAULT_NAME_KEYWORDS."""
        p = RosterParser(name_keywords=None)
        rows = [
            ["성명", "학교명"],
            ["홍길동", "분당초"],
        ]
        headers, data = p.parse(rows)
        assert len(data) == 1

    # ── Custom name_keywords passed directly to parse() ──

    def test_parse_custom_keywords_override(self):
        """parse() name_keywords param overrides constructor value."""
        p = RosterParser(name_keywords=["defaultkw"])
        rows = [
            ["StudentName", "School"],
            ["Hong", "Bundang"],
        ]
        # Without override, "defaultkw" doesn't match "StudentName" → fallback to col 0
        headers, data = p.parse(rows)
        assert len(data) == 1  # col 0 matched as fallback
        assert data[0]["StudentName"] == "Hong"

    def test_parse_custom_keywords_explicit(self):
        """Explicit name_keywords to parse() resolves correctly."""
        p = RosterParser()  # no constructor keywords
        rows = [
            ["StudentName", "School"],
            ["Hong", "Bundang"],
        ]
        headers, data = p.parse(rows, name_keywords=["StudentName"])
        assert len(data) == 1
        assert data[0]["StudentName"] == "Hong"
