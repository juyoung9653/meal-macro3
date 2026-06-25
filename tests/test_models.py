"""Tests for src.core.models — build_enriched_person_dict."""

from src.core.models import build_enriched_person_dict


class TestBuildEnrichedPersonDict:
    def test_basic_conversion(self):
        """All flags, note, totals → correct dict fields."""
        d = build_enriched_person_dict(
            raw={"성명": "홍길동", "학교명": "분당초"},
            name="홍길동",
            dept="분당초",
            note="4/2석식일시중지",
            flag_breakfast=True,
            flag_dinner=True,
            flag_lunch=False,
            _total=10,
            _amount="50000",
        )
        assert d["성명"] == "홍길동"
        assert d["학교명"] == "분당초"
        assert d["_breakfast"] == 1
        assert d["_dinner"] == 1
        assert d["_lunch"] == 0
        assert d["_note"] == "4/2석식일시중지"
        assert d["_total"] == 10
        assert d["_amount"] == "50000"

    def test_all_flags_false(self):
        """No flags → all _breakfast/_dinner/_lunch are 0, note empty."""
        d = build_enriched_person_dict(raw={"성명": "홍길동"})
        assert d["_breakfast"] == 0
        assert d["_dinner"] == 0
        assert d["_lunch"] == 0
        assert d["_note"] == ""
        assert d["_total"] == 0
        assert d["_amount"] == ""

    def test_mixed_flags(self):
        """Only breakfast + lunch → correct 1/0."""
        d = build_enriched_person_dict(
            raw={},
            flag_breakfast=True,
            flag_dinner=False,
            flag_lunch=True,
        )
        assert d["_breakfast"] == 1
        assert d["_dinner"] == 0
        assert d["_lunch"] == 1

    def test_raw_dict_not_mutated(self):
        """Original raw dict should not acquire enriched keys."""
        raw = {"성명": "홍길동"}
        d = build_enriched_person_dict(raw=raw, flag_breakfast=True)
        assert "_breakfast" not in raw
        assert d["_breakfast"] == 1
        assert raw["성명"] == "홍길동"

    def test_note_preserved(self):
        """Explicit note appears in output."""
        d = build_enriched_person_dict(raw={}, note="테스트비고")
        assert d["_note"] == "테스트비고"
        d2 = build_enriched_person_dict(raw={}, note="")
        assert d2["_note"] == ""

    def test_custom_total_and_amount(self):
        """Custom _total and __amount reflected in output."""
        d = build_enriched_person_dict(raw={}, _total=42, _amount="99999")
        assert d["_total"] == 42
        assert d["_amount"] == "99999"
