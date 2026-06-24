"""Tests for src.services.invoice_controller.InvoiceService — db.xlsx 기반."""

from datetime import date
from unittest.mock import patch

import pytest

from src.services.invoice_controller import (
    InvoiceGenerationConfig,
    InvoiceResult,
    InvoiceService,
    InvoiceWarning,
    _count_delivery_from_row,
)
from tests.conftest import InMemoryDataSource


class TestCountDeliveryFromRow:
    """배송전체 row에서 1/O 개수 집계 (식종 구분 없음)."""

    def test_O_3개(self):
        row = {"식종": "조식", "6/1": "O", "6/2": "O", "6/3": "O", "6/4": "X"}
        assert _count_delivery_from_row(row) == 3

    def test_1_2개(self):
        row = {"식종": "석식", "6/1": "1", "6/2": "0", "6/3": "1"}
        assert _count_delivery_from_row(row) == 2

    def test_O와_1_혼합(self):
        row = {"6/1": "O", "6/2": "1", "6/3": "0", "6/4": "X"}
        assert _count_delivery_from_row(row) == 2

    def test_빈_row(self):
        assert _count_delivery_from_row({}) == 0

    def test_날짜_컬럼_아닌_것_무시(self):
        row = {"관할동": "분당", "6/1": "O", "이름": "1"}
        assert _count_delivery_from_row(row) == 1

    def test_요일_붙은_날짜_컬럼(self):
        row = {"6/1 월": "1", "6/2 화": "1", "6/3 수": "0"}
        assert _count_delivery_from_row(row) == 2


class TestInvoiceGenerationConfig:
    """InvoiceGenerationConfig dataclass defaults."""

    def test_default_meal_type(self):
        cfg = InvoiceGenerationConfig(
            roster_data=[],
            template_path="/fake/tpl.xlsx",
            output_path="/fake/out.xlsx",
            year=2026,
            month=6,
        )
        assert cfg.meal_type == "급식"

    def test_custom_meal_type(self):
        cfg = InvoiceGenerationConfig(
            roster_data=[],
            template_path="/fake/tpl.xlsx",
            output_path="/fake/out.xlsx",
            year=2026,
            month=6,
            meal_type="행복도시락",
        )
        assert cfg.meal_type == "행복도시락"


class TestInvoiceServiceGenerate:
    """InMemoryDataSource로 InvoiceService.generate() 테스트.

    배송전체는 Power Query가 이미 모든 휴무일/제외일을 반영한 최종 결과.
    추가 차감 없이 그대로 사용.
    """

    @pytest.fixture
    def mock_reader(self):
        reader = InMemoryDataSource()
        with patch(
            "src.services.invoice_controller.get_db_reader", return_value=reader
        ):
            yield reader

    @pytest.fixture
    def mock_make_invoice(self):
        with patch("src.services.invoice_controller.make_invoice") as m:

            def _side_effect(enriched, *args, **kwargs):
                return len(enriched), enriched

            m.side_effect = _side_effect
            yield m

    @pytest.fixture
    def mock_exists(self):
        with patch("src.services.invoice_controller.os.path.exists", return_value=True):
            yield

    @pytest.fixture
    def svc(self):
        return InvoiceService()

    def test_빈_명단(self, svc, mock_reader):
        result = svc.generate(
            InvoiceGenerationConfig(
                roster_data=[],
                output_path="/tmp/test.xlsx",
                year=2026,
                month=6,
                template_path="/fake/tpl.xlsx",
                col_mapping={},
                excluded_dates=set(),
                roster_headers=[],
                template_columns=[],
            )
        )
        assert not result.success
        assert "명단 데이터" in (result.error or "")

    def test_조식_플래그_O_집계(self, svc, mock_reader, mock_make_invoice, mock_exists):
        mock_reader._delivery_matrix = [
            {
                "ID": "P001",
                "식종": "조식",
                "6/1": "O",
                "6/2": "O",
                "6/3": "X",
                "6/4": "O",
            },
        ]
        mock_reader._order_map = {
            "010-1234|서울": {"배송순서": "1", "배송담당": "김배송"},
        }

        result = svc.generate(
            InvoiceGenerationConfig(
                roster_data=[
                    {
                        "ID": "P001",
                        "성명": "홍길동",
                        "학교명": "분당초",
                        "조식 여부": "1",
                        "석식 여부": "0",
                        "중식 여부": "0",
                        "핸드폰": "010-1234",
                        "도로명주소": "서울",
                    }
                ],
                output_path="/tmp/test.xlsx",
                year=2026,
                month=6,
                template_path="/fake/tpl.xlsx",
                col_mapping={},
                excluded_dates=set(),
                roster_headers=["ID", "성명", "조식 여부"],
                template_columns=["성명"],
            )
        )
        assert result.success
        assert len(result.enriched) == 1
        assert result.enriched[0]["_bf_cnt"] == 3
        assert result.enriched[0]["_dn_cnt"] == 0
        assert result.enriched[0]["배송담당"] == "김배송"

    def test_배송전체_카운트_그대로_사용(
        self, svc, mock_reader, mock_make_invoice, mock_exists
    ):
        """배송전체 값이 최종 결과. excluded_dates/holidays 추가 차감 없음."""
        mock_reader._delivery_matrix = [
            {"ID": "P001", "식종": "조식", "6/1": "O", "6/2": "O", "6/3": "O"},
        ]
        mock_reader._order_map = {}

        result = svc.generate(
            InvoiceGenerationConfig(
                roster_data=[
                    {
                        "ID": "P001",
                        "성명": "홍길동",
                        "학교명": "분당초",
                        "조식 여부": "1",
                        "석식 여부": "0",
                        "중식 여부": "0",
                        "핸드폰": "",
                        "도로명주소": "",
                    }
                ],
                output_path="/tmp/test.xlsx",
                year=2026,
                month=6,
                template_path="/fake/tpl.xlsx",
                col_mapping={},
                excluded_dates={
                    date(2026, 6, 1)
                },  # 제외일 있어도 무시 (배송전체 이미 반영)
                roster_headers=["ID", "성명", "조식 여부"],
                template_columns=["성명"],
            )
        )
        assert result.success
        assert result.enriched[0]["_bf_cnt"] == 3  # 배송전체 그대로 3

    def test_플래그_0이면_건수_0(
        self, svc, mock_reader, mock_make_invoice, mock_exists
    ):
        mock_reader._delivery_matrix = [
            {"ID": "P001", "식종": "조식", "6/1": "O", "6/2": "O"},
        ]
        mock_reader._order_map = {}

        result = svc.generate(
            InvoiceGenerationConfig(
                roster_data=[
                    {
                        "ID": "P001",
                        "성명": "홍길동",
                        "학교명": "분당초",
                        "조식 여부": "0",
                        "석식 여부": "0",
                        "중식 여부": "0",
                        "핸드폰": "",
                        "도로명주소": "",
                    }
                ],
                output_path="/tmp/test.xlsx",
                year=2026,
                month=6,
                template_path="/fake/tpl.xlsx",
                col_mapping={},
                excluded_dates=set(),
                roster_headers=["ID", "성명", "조식 여부"],
                template_columns=["성명"],
            )
        )
        assert result.success
        assert result.enriched[0]["_bf_cnt"] == 0

    def test_석식_BOX는_석식으로_집계(
        self, svc, mock_reader, mock_make_invoice, mock_exists
    ):
        """석식_BOX → 석식으로 합산, 중식 아님."""
        mock_reader._delivery_matrix = [
            {"ID": "P001", "식종": "석식", "6/1": "1", "6/2": "1"},
            {"ID": "P001", "식종": "석식_BOX", "6/3": "1", "6/4": "1", "6/5": "1"},
        ]
        mock_reader._order_map = {}

        result = svc.generate(
            InvoiceGenerationConfig(
                roster_data=[
                    {
                        "ID": "P001",
                        "성명": "홍길동",
                        "학교명": "분당초",
                        "조식 여부": "0",
                        "석식 여부": "1",
                        "중식 여부": "0",
                        "핸드폰": "",
                        "도로명주소": "",
                    }
                ],
                output_path="/fake/test.xlsx",
                year=2026,
                month=6,
                template_path="/fake/tpl.xlsx",
                col_mapping={},
                excluded_dates=set(),
                roster_headers=["ID", "성명", "석식 여부"],
                template_columns=["성명"],
            )
        )
        assert result.success
        assert result.enriched[0]["_dn_cnt"] == 5  # 석식2 + 석식_BOX3
        assert result.enriched[0]["_lh_cnt"] == 0  # 중식 아님
