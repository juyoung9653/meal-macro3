"""Verify InMemoryDataSource conforms to DataSource protocol."""

from datetime import date

from src.excel.data_source import DataSource
from tests.conftest import InMemoryDataSource


def test_in_memory_is_datasource():
    reader = InMemoryDataSource()
    assert isinstance(reader, DataSource)


def test_in_memory_basics():
    reader = InMemoryDataSource(
        roster=[{"성명": "홍길동"}],
        year=2026,
        month=6,
        holiday_set={date(2026, 6, 6)},
    )
    assert reader.read_roster() == [{"성명": "홍길동"}]
    assert reader.detect_month() == (2026, 6)
    assert date(2026, 6, 6) in reader.get_holiday_set()
    assert reader.read_delivery_matrix() == []
    assert reader.build_order_map() == {}


def test_in_memory_delivery_data():
    reader = InMemoryDataSource(
        delivery_matrix=[
            {"ID": "P001", "식종": "조식", "6/1": "O"},
        ],
        delivery_map={"P001": {"ID": "P001", "phone": "010-1234"}},
        order_map={"010-1234|서울": {"배송순서": "1"}},
    )
    assert len(reader.read_delivery_matrix()) == 1
    assert reader.build_delivery_map()["P001"]["phone"] == "010-1234"
    assert reader.build_order_map()["0101234|서울"]["배송순서"] == "1"


def test_in_memory_db_path():
    reader = InMemoryDataSource(db_path="/fake/path.xlsx")
    assert reader.db_path == "/fake/path.xlsx"
