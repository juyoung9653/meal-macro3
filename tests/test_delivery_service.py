"""services/delivery_service.py 단위 테스트 — InMemoryDataSource 활용."""

from datetime import date

import pytest

from src.core.meal_types import MEAL_RULES
from src.services.delivery_service import DeliveryService
from tests.conftest import InMemoryDataSource


def _build_delivery_matrix(staff_phone_pairs, date_cols):
    """Build a delivery_matrix list[dict] for testing.

    staff_phone_pairs: [(staff_name, phone), ...]
    date_cols: [(month, day), ...] → creates "M/D" columns with "1" values
    """
    rows = []
    for staff, phone in staff_phone_pairs:
        row = {
            "ID": phone,
            "식종": "중식(월,화,수,목,금)",
            "성명": f"사람{phone}",
        }
        for m, d in date_cols:
            row[f"{m}/{d}"] = "1"
        rows.append(row)
    return rows


def _build_roster(phone_list):
    """Build a roster list[dict]."""
    rows = []
    for phone in phone_list:
        rows.append(
            {
                "ID": phone,
                "성명": f"사람{phone}",
                "핸드폰": phone,
                "비고": "",
                "도로명주소": f"주소{phone}",
                "관할동": "분당동",
                "배송담당": "",
            }
        )
    return rows


def _build_order_map(staff_phone_pairs):
    """Build order_map for testing."""
    result = {}
    for staff, phone in staff_phone_pairs:
        addr = f"주소{phone}"
        key = f"{phone}|{addr}"
        result[key] = {"배송담당": staff, "배송순서": "1", "핸드폰": phone}
    return result


def _make_date(m, d):
    return date(2026, m, d)


class TestDeliveryServiceBasic:
    def test_build_report_simple(self):
        pairs = [("김담당", "01011112222")]
        ds = InMemoryDataSource(
            delivery_matrix=_build_delivery_matrix(pairs, [(6, 15)]),
            roster=_build_roster(["01011112222"]),
            order_map=_build_order_map(pairs),
            holiday_set=set(),
        )
        svc = DeliveryService(ds)
        result = svc.build_report(date(2026, 6, 15), "중식")

        assert len(result) >= 2  # header + data rows
        headers = result[0]
        assert "배송담당" in headers[0]

    def test_build_report_no_delivery(self):
        pairs = [("김담당", "01011112222")]
        ds = InMemoryDataSource(
            delivery_matrix=_build_delivery_matrix(pairs, [(6, 15)]),
            roster=_build_roster(["01011112222"]),
            order_map=_build_order_map(pairs),
            holiday_set=set(),
        )
        svc = DeliveryService(ds)
        result = svc.build_report(date(2026, 6, 16), "중식")
        # Date 6/16 not in matrix → should return error
        assert len(result) == 1
        assert "날짜" in str(result[0][0])

    def test_invalid_meal(self):
        ds = InMemoryDataSource()
        svc = DeliveryService(ds)
        result = svc.build_report(date(2026, 6, 15), "아침")
        assert "유효하지 않은 식종" in str(result[0][0])

    def test_non_delivery_day_for_meal(self):
        # 토요일(6/13)은 중식 배송일 아님 → 중식은 월~금만
        saturday = date(2026, 6, 13)  # Saturday
        assert saturday.weekday() == 5  # Saturday
        # 중식은 월~금인데 토요일 → build_report가 오류 반환해야 함
        ds = InMemoryDataSource()
        svc = DeliveryService(ds)
        result = svc.build_report(saturday, "중식")
        assert len(result) == 1
        assert "배송일이 아닙니다" in str(result[0][0])

    def test_meal_rules(self):
        # Monday=0, Tuesday=1, ..., Friday=4, Saturday=5, Sunday=6
        assert 4 in MEAL_RULES["조식"]  # Friday only
        assert 0 in MEAL_RULES["중식"]  # Monday
        assert 4 in MEAL_RULES["중식"]  # Friday
        assert 5 not in MEAL_RULES["중식"]  # Saturday
        assert 3 in MEAL_RULES["석식"]  # Thursday
        assert 5 not in MEAL_RULES["석식"]  # Saturday


class TestBuildAll:
    def test_build_all_week_range(self):
        pairs = [("김담당", "01011112222")]
        # Monday 6/15 ~ Wednesday 6/17
        ds = InMemoryDataSource(
            delivery_matrix=_build_delivery_matrix(pairs, [(6, 15), (6, 16), (6, 17)]),
            roster=_build_roster(["01011112222"]),
            order_map=_build_order_map(pairs),
            holiday_set=set(),
        )
        svc = DeliveryService(ds)
        # 6/15 Monday → 중식, 석식 (월~목 석식)
        results = svc.build_all(date(2026, 6, 15), date(2026, 6, 17))
        assert len(results) >= 2  # at least 2 days * meals

    def test_build_all_with_holiday(self):
        pairs = [("김담당", "01011112222")]
        holiday = date(2026, 6, 16)
        ds = InMemoryDataSource(
            delivery_matrix=_build_delivery_matrix(pairs, [(6, 15), (6, 16), (6, 17)]),
            roster=_build_roster(["01011112222"]),
            order_map=_build_order_map(pairs),
            holiday_set={holiday},
        )
        svc = DeliveryService(ds)
        results = svc.build_all(date(2026, 6, 15), date(2026, 6, 17))
        # 6/16 should be skipped (holiday)
        date_strs = {r[0] for r in results}
        assert "20260616" not in date_strs

    def test_build_all_meal_filter(self):
        pairs = [("김담당", "01011112222")]
        ds = InMemoryDataSource(
            delivery_matrix=_build_delivery_matrix(pairs, [(6, 15), (6, 16)]),
            roster=_build_roster(["01011112222"]),
            order_map=_build_order_map(pairs),
            holiday_set=set(),
        )
        svc = DeliveryService(ds)
        results = svc.build_all(
            date(2026, 6, 15), date(2026, 6, 16), meal_filter="중식"
        )
        for _, meal, _ in results:
            assert meal == "중식"


class TestMultipleStaff:
    def test_staff_aggregation(self):
        pairs = [
            ("김담당", "01011112222"),
            ("김담당", "01022223333"),
            ("이담당", "01033334444"),
        ]
        ds = InMemoryDataSource(
            delivery_matrix=_build_delivery_matrix(pairs, [(6, 15)]),
            roster=_build_roster(["01011112222", "01022223333", "01033334444"]),
            order_map=_build_order_map(pairs),
            holiday_set=set(),
        )
        svc = DeliveryService(ds)
        result = svc.build_report(date(2026, 6, 15), "중식")

        # Find staff rows
        staff_names = [r[0].split("\n")[0] for r in result[1:] if r[0] != "합계"]
        assert "김담당" in staff_names
        assert "이담당" in staff_names

    def test_staff_sort_order(self):
        pairs = [
            ("이담당", "01033334444"),
            ("김담당", "01011112222"),
        ]
        ds = InMemoryDataSource(
            delivery_matrix=_build_delivery_matrix(pairs, [(6, 15)]),
            roster=_build_roster(["01011112222", "01033334444"]),
            order_map=_build_order_map(pairs),
            holiday_set=set(),
        )
        svc = DeliveryService(ds, staff_sort={"중식": ["김담당", "이담당"]})
        result = svc.build_report(date(2026, 6, 15), "중식")

        staff_names = [r[0].split("\n")[0] for r in result[1:] if r[0] != "합계"]
        if len(staff_names) >= 2:
            assert staff_names[0] == "김담당"
            assert staff_names[1] == "이담당"
