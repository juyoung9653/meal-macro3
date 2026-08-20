"""학교별 방학 일정의 3컬럼 로딩 및 적용 테스트."""

from datetime import date

from src.services import vacation_calculator
from src.widgets.invoice_tab import _build_lunch_roster
from src.widgets.lunch_mapping_dialog import _SCHEDULE_FIELDS


def test_schedule_mapping_exposes_exactly_three_columns():
    assert [field[0] for field in _SCHEDULE_FIELDS] == [
        "school_name_col",
        "start_date_col",
        "end_date_col",
    ]


def test_three_schedule_columns_create_inclusive_delivery_range(monkeypatch):
    monkeypatch.setattr(
        vacation_calculator, "get_kr_holidays", lambda _year: set()
    )
    roster = _build_lunch_roster(
        ["학교", "방학시작", "방학종료", "사용하지 않는 칼럼"],
        [
            {
                "학교": "분당초",
                "방학시작": "2025-06-02",
                "방학종료": "2025-06-06",
                "사용하지 않는 칼럼": "무시",
            }
        ],
        {
            "school_name_col": "학교",
            "start_date_col": "방학시작",
            "end_date_col": "방학종료",
        },
        "schedule.xlsx",
        2025,
    )

    assert len(roster.entries) == 1
    entry = roster.entries[0]
    assert entry.start_date == date(2025, 6, 2)
    assert entry.end_date == date(2025, 6, 6)
    assert roster.is_vacation_month(2025, 6)
    assert vacation_calculator.get_lunch_delivery_dates_with_note(
        2025, 6, entry
    ) == [
        date(2025, 6, 2),
        date(2025, 6, 3),
        date(2025, 6, 4),
        date(2025, 6, 5),
        date(2025, 6, 6),
    ]
