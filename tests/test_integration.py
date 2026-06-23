"""Integration tests: end-to-end pipeline from data to Excel output."""

import os
from unittest.mock import patch

import openpyxl
import pytest

from src.services.invoice_controller import InvoiceGenerationConfig, InvoiceService
from src.templates.config import read_template_columns
from tests.conftest import InMemoryDataSource


class TestInvoicePipeline:
    """End-to-end: InMemoryDataSource → InvoiceService → make_invoice → Excel."""

    @pytest.fixture(autouse=True)
    def _clear_template_cache(self):
        """Prevent lru_cache pollution across tests."""
        read_template_columns.cache_clear()
        yield
        read_template_columns.cache_clear()

    @pytest.fixture
    def template_path(self):
        path = "assets/청구서템플릿.xlsx"
        if not os.path.exists(path):
            pytest.skip("Template file not found")
        return path

    @pytest.fixture
    def data_source(self):
        """Minimal realistic test data: 1 person with breakfast only, 1 with dinner only.

        Note: delivery_matrix rows are combined per (ID, 식종) because
        InvoiceService uses max() across duplicate (ID+meal) rows, not sum.
        """
        return InMemoryDataSource(
            roster=[
                {
                    "ID": "P001",
                    "성명": "홍길동",
                    "학교명": "분당초",
                    "조식 여부": "1",
                    "석식 여부": "0",
                    "중식 여부": "0",
                    "핸드폰": "010-1111-2222",
                    "도로명주소": "분당구 정자동",
                    "비고": "",
                },
                {
                    "ID": "P002",
                    "성명": "김철수",
                    "학교명": "분당초",
                    "조식 여부": "0",
                    "석식 여부": "1",
                    "중식 여부": "0",
                    "핸드폰": "010-3333-4444",
                    "도로명주소": "분당구 수내동",
                    "비고": "알러지 있음",
                },
            ],
            delivery_matrix=[
                # P001: 5 breakfast deliveries (combined into one row)
                {
                    "ID": "P001",
                    "식종": "조식",
                    "6/1": "O",
                    "6/2": "O",
                    "6/3": "X",
                    "6/4": "O",
                    "6/5": "O",
                    "6/6": "O",
                },
                # P002: 3 dinner deliveries
                {
                    "ID": "P002",
                    "식종": "석식",
                    "6/1": "1",
                    "6/2": "1",
                    "6/3": "1",
                },
            ],
            order_map={
                "010-1111-2222|분당구 정자동": {"배송순서": "1", "배송담당": "김배송"},
                "010-3333-4444|분당구 수내동": {"배송순서": "2", "배송담당": "이배송"},
            },
            year=2026,
            month=6,
        )

    @pytest.fixture
    def service(self):
        return InvoiceService()

    def test_generate_invoice_creates_valid_excel(
        self, tmp_path, template_path, data_source, service
    ):
        """Full pipeline: generate invoice → open output → verify structure."""
        output_path = str(tmp_path / "test_invoice.xlsx")

        config = InvoiceGenerationConfig(
            roster_data=data_source.read_roster(),
            template_path=template_path,
            output_path=output_path,
            year=2026,
            month=6,
            col_mapping={},
            excluded_dates=set(),
            roster_headers=[
                "ID",
                "성명",
                "학교명",
                "조식 여부",
                "석식 여부",
                "중식 여부",
                "핸드폰",
                "도로명주소",
                "비고",
            ],
            template_columns=[],
        )

        # Patch get_db_reader so InvoiceService uses our InMemoryDataSource
        with patch(
            "src.services.invoice_controller.get_db_reader", return_value=data_source
        ):
            result = service.generate(config)

        # 1. Result is successful
        assert result.success, f"Invoice generation failed: {result.error}"
        assert result.count > 0, "Should have at least one person in output"

        # 2. Output file exists and is valid Excel
        assert os.path.exists(output_path), f"Output not created: {output_path}"
        wb = openpyxl.load_workbook(output_path)

        # 3. Has expected sheets (총괄 + 학기중 for non-vacation month)
        assert len(wb.sheetnames) >= 1
        assert "총괄" in wb.sheetnames or any("총괄" in s for s in wb.sheetnames)

        # 4. First sheet has data rows (more than just header)
        first_sheet = wb[wb.sheetnames[0]]
        assert first_sheet.max_row > 3, "Sheet should have data rows beyond headers"

        # 5. Enriched data has correct counts
        enriched = result.enriched
        assert len(enriched) >= 1

        # Find 홍길동 (should have bf_cnt=5 from delivery_matrix)
        gildong = next((p for p in enriched if p.get("성명") == "홍길동"), None)
        assert gildong is not None, "홍길동 should be in output"
        assert gildong["_bf_cnt"] == 5, (
            f"Expected 5 breakfast deliveries, got {gildong['_bf_cnt']}"
        )
        assert gildong["_dn_cnt"] == 0

        # Find 김철수 (should have dn_cnt=3)
        chulsoo = next((p for p in enriched if p.get("성명") == "김철수"), None)
        assert chulsoo is not None, "김철수 should be in output"
        assert chulsoo["_dn_cnt"] == 3, (
            f"Expected 3 dinner deliveries, got {chulsoo['_dn_cnt']}"
        )
        assert chulsoo["_bf_cnt"] == 0

        # 6. Order map info applied
        assert gildong.get("배송담당") == "김배송"
        assert chulsoo.get("배송담당") == "이배송"

    def test_empty_roster_returns_error(self, tmp_path, template_path, service):
        """Edge case: empty roster should fail gracefully."""
        config = InvoiceGenerationConfig(
            roster_data=[],
            template_path=template_path,
            output_path=str(tmp_path / "empty.xlsx"),
            year=2026,
            month=6,
        )
        result = service.generate(config)
        assert not result.success
        assert result.error is not None
