"""Tests for src.core.models — RosterPerson, EnrichedPerson, person_to_dict."""

from src.core.models import EnrichedPerson, RosterPerson, person_to_dict


class TestRosterPerson:
    def test_construction_minimal(self):
        p = RosterPerson(raw={"성명": "홍길동"}, name="홍길동")
        assert p.raw == {"성명": "홍길동"}
        assert p.name == "홍길동"
        assert p.dept == ""

    def test_construction_full(self):
        p = RosterPerson(
            raw={"성명": "김철수", "학교명": "분당초"}, name="김철수", dept="분당초"
        )
        assert p.name == "김철수"
        assert p.dept == "분당초"

    def test_getitem_proxies_to_raw(self):
        p = RosterPerson(raw={"성명": "이영희", "학교명": "수내초"}, name="이영희")
        assert p["성명"] == "이영희"
        assert p["학교명"] == "수내초"
        assert p["존재하지않음"] == ""

    def test_get_method(self):
        p = RosterPerson(raw={"성명": "박민수"}, name="박민수")
        assert p.get("성명") == "박민수"
        assert p.get("존재하지않음") == ""
        assert p.get("존재하지않음", default="없음") == "없음"


class TestEnrichedPerson:
    def test_construction_minimal(self):
        rp = RosterPerson(raw={"성명": "홍길동"}, name="홍길동")
        ep = EnrichedPerson(person=rp)
        assert ep.person is rp
        assert ep.note == ""
        assert ep.flag_breakfast is False
        assert ep.flag_dinner is False
        assert ep.flag_lunch is False
        assert ep._total == 0
        assert ep._amount == ""

    def test_construction_with_flags(self):
        rp = RosterPerson(raw={"성명": "김철수"}, name="김철수")
        ep = EnrichedPerson(
            person=rp,
            note="4/2석식일시중지",
            flag_breakfast=True,
            flag_dinner=True,
            _total=10,
            _amount="50000",
        )
        assert ep.note == "4/2석식일시중지"
        assert ep.flag_breakfast is True
        assert ep.flag_dinner is True
        assert ep.flag_lunch is False
        assert ep._total == 10
        assert ep._amount == "50000"

    def test_getitem_special_keys(self):
        rp = RosterPerson(raw={"성명": "홍길동"}, name="홍길동")
        ep = EnrichedPerson(
            person=rp, flag_breakfast=True, note="test", _total=5, _amount="30000"
        )
        assert ep["_breakfast"] is True
        assert ep["_dinner"] is False
        assert ep["_lunch"] is False
        assert ep["_note"] == "test"
        assert ep["_total"] == 5
        assert ep["_amount"] == "30000"

    def test_getitem_falls_through_to_person(self):
        rp = RosterPerson(raw={"성명": "홍길동", "학교명": "분당초"}, name="홍길동")
        ep = EnrichedPerson(person=rp)
        assert ep["성명"] == "홍길동"
        assert ep["학교명"] == "분당초"

    def test_getitem_unknown_key(self):
        rp = RosterPerson(raw={"성명": "홍길동"}, name="홍길동")
        ep = EnrichedPerson(person=rp)
        assert ep["존재하지않음"] == ""

    def test_get_method(self):
        rp = RosterPerson(raw={"성명": "홍길동"}, name="홍길동")
        ep = EnrichedPerson(person=rp, _total=10)
        assert ep.get("_total") == 10
        assert ep.get("_breakfast") is False
        assert ep.get("성명") == "홍길동"
        assert ep.get("존재하지않음") == ""
        assert ep.get("존재하지않음", default="N/A") == "N/A"

    def test_contains_raw_keys(self):
        rp = RosterPerson(raw={"성명": "홍길동"}, name="홍길동")
        ep = EnrichedPerson(person=rp)
        assert "성명" in ep
        assert "존재하지않음" not in ep

    def test_contains_special_keys(self):
        rp = RosterPerson(raw={"성명": "홍길동"}, name="홍길동")
        ep = EnrichedPerson(person=rp)
        assert "_breakfast" in ep
        assert "_dinner" in ep
        assert "_lunch" in ep
        assert "_note" in ep
        assert "_total" in ep
        assert "_amount" in ep
        assert "_없는키" not in ep


class TestPersonToDict:
    def test_basic_conversion(self):
        rp = RosterPerson(raw={"성명": "홍길동", "학교명": "분당초"}, name="홍길동")
        ep = EnrichedPerson(
            person=rp,
            note="4/2석식일시중지",
            flag_breakfast=True,
            flag_dinner=True,
            flag_lunch=False,
            _total=10,
            _amount="50000",
        )
        d = person_to_dict(ep)
        assert d["성명"] == "홍길동"
        assert d["학교명"] == "분당초"
        assert d["_breakfast"] == 1
        assert d["_dinner"] == 1
        assert d["_lunch"] == 0
        assert d["_note"] == "4/2석식일시중지"
        assert d["_total"] == 10
        assert d["_amount"] == "50000"

    def test_all_flags_false(self):
        rp = RosterPerson(raw={"성명": "홍길동"}, name="홍길동")
        ep = EnrichedPerson(person=rp)
        d = person_to_dict(ep)
        assert d["_breakfast"] == 0
        assert d["_dinner"] == 0
        assert d["_lunch"] == 0
        assert d["_note"] == ""

    def test_raw_dict_not_mutated(self):
        """person_to_dict should not mutate the original raw dict."""
        raw = {"성명": "홍길동"}
        rp = RosterPerson(raw=raw, name="홍길동")
        ep = EnrichedPerson(person=rp, flag_breakfast=True)
        d = person_to_dict(ep)
        assert "_breakfast" not in raw
        assert d["_breakfast"] == 1
        assert raw["성명"] == "홍길동"
