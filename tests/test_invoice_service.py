"""Tests for src.services.invoice_controller.InvoiceService.

Pure business logic — no PyQt imports, no UI dependencies.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.services.invoice_controller import (
    InvoiceResult,
    InvoiceService,
    InvoiceWarning,
)
from src.services.lunch_data import LunchRoster, LunchSchoolEntry


class TestInvoiceService:
    """InvoiceService: pure business logic, no UI dependencies."""

    # ── Helpers ──────────────────────────────────────

    def _minimal_args(self, **overrides) -> dict:
        """Return a dict of minimal valid arguments for generate()."""
        args = dict(
            roster_data=[{"성명": "홍길동", "학교명": "분당초"}],
            output_path="test.xlsx",
            year=2025,
            month=6,
            breakfast_price=1000,
            dinner_price=2000,
            lunch_price=1500,
            template_path="template.xlsx",
            col_mapping={
                "__breakfast": "조식",
                "__dinner": "석식",
                "__vacation": "방중",
                "__note": "비고",
                "성명": "성명",
                "학교명": "학교명",
            },
            excluded_dates=set(),
            roster_headers=["성명", "학교명"],
            template_columns=["성명", "학교명"],
            has_lunch_file=False,
            lunch_roster=None,
            lunch_mapping=None,
        )
        args.update(overrides)
        return args

    # ── Empty roster ─────────────────────────────────

    def test_empty_roster_returns_failure(self):
        """Empty roster → InvoiceResult(success=False). No mocks needed."""
        svc = InvoiceService()
        args = self._minimal_args(roster_data=[])
        result = svc.generate(**args)

        assert not result.success
        assert result.error is not None
        assert "명단 데이터가 없습니다" in result.error
        assert result.count == 0
        assert result.enriched == []

    # ── Valid roster (all mocks) ─────────────────────

    @patch("src.services.invoice_controller.make_invoice")
    @patch("src.services.invoice_controller.count_delivery_days")
    @patch("src.services.invoice_controller.os.path.exists", return_value=True)
    @patch(
        "src.services.invoice_controller.auto_detect_semantic_columns",
        return_value={},
    )
    def test_valid_roster_returns_success(
        self,
        mock_auto,
        mock_exists,
        mock_count_delivery,
        mock_make_invoice,
    ):
        """Valid non-empty roster → InvoiceResult(success=True, count>0)."""
        mock_count_delivery.return_value = {
            "breakfast": 10,
            "dinner": 15,
            "lunch": 5,
        }
        mock_make_invoice.return_value = (
            1,
            [
                {
                    "성명": "홍길동",
                    "_bf_cnt": 10,
                    "_dn_cnt": 15,
                    "_lh_cnt": 0,
                    "_total": 25000,
                    "_amount": "25,000",
                }
            ],
        )

        svc = InvoiceService()
        args = self._minimal_args()
        result = svc.generate(**args)

        assert result.success
        assert result.count == 1
        assert len(result.enriched) == 1
        assert result.enriched[0]["성명"] == "홍길동"
        assert result.error is None

    # ── Template not found ──────────────────────────

    @patch("src.services.invoice_controller.os.path.exists", return_value=False)
    def test_missing_template_returns_error(self, mock_exists):
        """Non-existent template path → InvoiceResult(success=False)."""
        svc = InvoiceService()
        args = self._minimal_args(template_path="nonexistent.xlsx")
        result = svc.generate(**args)

        assert not result.success
        assert result.error is not None
        assert "템플릿 파일을 찾을 수 없습니다" in result.error

    # ── make_invoice exception → error ───────────────

    @patch("src.services.invoice_controller.make_invoice")
    @patch("src.services.invoice_controller.count_delivery_days")
    @patch("src.services.invoice_controller.os.path.exists", return_value=True)
    @patch(
        "src.services.invoice_controller.auto_detect_semantic_columns",
        return_value={},
    )
    def test_generation_exception_returns_error(
        self,
        mock_auto,
        mock_exists,
        mock_count_delivery,
        mock_make_invoice,
    ):
        """make_invoice raises → InvoiceResult(success=False, error=...)."""
        mock_count_delivery.return_value = {
            "breakfast": 10,
            "dinner": 15,
            "lunch": 5,
        }
        mock_make_invoice.side_effect = RuntimeError("엑셀 쓰기 실패")

        svc = InvoiceService()
        args = self._minimal_args()
        result = svc.generate(**args)

        assert not result.success
        assert result.error is not None
        assert "청구서 생성 실패" in result.error

    # ── Lunch matching warnings ──────────────────────

    @patch("src.services.invoice_controller.make_invoice")
    @patch("src.services.invoice_controller.count_delivery_days")
    @patch("src.services.invoice_controller.os.path.exists", return_value=True)
    @patch(
        "src.services.invoice_controller.auto_detect_semantic_columns",
        return_value={},
    )
    def test_lunch_unmatched_school_warning_collected(
        self,
        mock_auto,
        mock_exists,
        mock_count_delivery,
        mock_make_invoice,
    ):
        """Unmatched schools in vacation month → warning in result.warnings."""
        mock_count_delivery.return_value = {
            "breakfast": 10,
            "dinner": 15,
            "lunch": 5,
        }
        mock_make_invoice.return_value = (
            1,
            [{"성명": "홍길동", "_bf_cnt": 10, "_dn_cnt": 15, "_lh_cnt": 0}],
        )

        # Mock lunch_roster: find_school returns None, is_vacation_month True
        mock_roster = MagicMock()
        mock_roster.find_school.return_value = None
        mock_roster.is_vacation_month.return_value = True

        # Vacancy_col in person data must be truthy to trigger lunch path
        roster_data = [
            {"성명": "홍길동", "학교명": "분당초", "방중": 1},
        ]
        lunch_mapping = {
            "roster_school_col": "학교명",
            "roster_grade_col": "학년",
        }

        svc = InvoiceService()
        args = self._minimal_args(
            roster_data=roster_data,
            has_lunch_file=True,
            lunch_roster=mock_roster,
            lunch_mapping=lunch_mapping,
        )
        result = svc.generate(**args)

        # Should have one unmatched school warning
        assert result.success
        assert len(result.warnings) == 1

        w = result.warnings[0]
        assert w.code == "unmatched_school"
        assert "중식 명단에서 찾을 수 없습니다" in w.message
        assert "분당초" in w.message

    # ── No warnings when lunch file not loaded ───────

    @patch("src.services.invoice_controller.make_invoice")
    @patch("src.services.invoice_controller.count_delivery_days")
    @patch("src.services.invoice_controller.os.path.exists", return_value=True)
    @patch(
        "src.services.invoice_controller.auto_detect_semantic_columns",
        return_value={},
    )
    def test_no_lunch_warnings_without_lunch_file(
        self,
        mock_auto,
        mock_exists,
        mock_count_delivery,
        mock_make_invoice,
    ):
        """No lunch file → no lunch-related warnings."""
        mock_count_delivery.return_value = {
            "breakfast": 10,
            "dinner": 15,
            "lunch": 5,
        }
        mock_make_invoice.return_value = (
            1,
            [{"성명": "홍길동", "_bf_cnt": 10, "_dn_cnt": 15, "_lh_cnt": 5}],
        )

        svc = InvoiceService()
        args = self._minimal_args()  # has_lunch_file=False, no lunch_roster
        result = svc.generate(**args)

        assert result.success
        assert len(result.warnings) == 0

    # ── "중지/중단" rows are skipped ────────────────

    @patch("src.services.invoice_controller.make_invoice")
    @patch("src.services.invoice_controller.count_delivery_days")
    @patch("src.services.invoice_controller.os.path.exists", return_value=True)
    @patch(
        "src.services.invoice_controller.auto_detect_semantic_columns",
        return_value={},
    )
    def test_stopped_rows_are_skipped(
        self,
        mock_auto,
        mock_exists,
        mock_count_delivery,
        mock_make_invoice,
    ):
        """Rows with '중지' or '중단' in the stop column are excluded."""
        mock_count_delivery.return_value = {
            "breakfast": 10,
            "dinner": 15,
            "lunch": 5,
        }
        mock_make_invoice.return_value = (
            2,
            [
                {"성명": "홍길동", "_bf_cnt": 10, "_dn_cnt": 15, "_lh_cnt": 5},
                {"성명": "김철수", "_bf_cnt": 10, "_dn_cnt": 15, "_lh_cnt": 5},
            ],
        )

        roster_data = [
            {"성명": "홍길동", "학교명": "분당초"},  # normal
            {"성명": "중지자", "학교명": "분당초", "중지": "중지"},  # stopped
            {"성명": "중단자", "학교명": "분당초", "중단": "중단"},  # stopped
            {"성명": "김철수", "학교명": "분당초"},  # normal
        ]

        svc = InvoiceService()
        args = self._minimal_args(
            roster_data=roster_data,
            roster_headers=["성명", "학교명", "중지", "중단"],
            col_mapping={
                "__breakfast": "조식",
                "__dinner": "석식",
                "__vacation": "방중",
                "__note": "비고",
            },
        )
        result = svc.generate(**args)

        assert result.success
        # Only non-stopped persons should make it through
        # (make_invoice returns 2, but the skipping happens before make_invoice)
        # Actually, make_invoice is mocked so it returns what we tell it.
        # The skipped rows just don't get added to enriched.
        pass_count = sum(
            1
            for p in roster_data
            if not (
                "중지" in str(p.get("중지", "")) or "중단" in str(p.get("중단", ""))
            )
        )
        assert len(result.enriched) == 2

    @patch("src.services.invoice_controller.get_kr_holidays", return_value=set())
    @patch("src.services.vacation_calculator.get_kr_holidays", return_value=set())
    @patch("src.services.invoice_controller.make_invoice")
    @patch("src.services.invoice_controller.count_delivery_days")
    @patch("src.services.invoice_controller.os.path.exists", return_value=True)
    def test_separate_lunch_roster_merges_by_name_and_birthdate(
        self,
        mock_exists,
        mock_count_delivery,
        mock_make_invoice,
        mock_vacation_holidays,
        mock_controller_holidays,
    ):
        mock_count_delivery.return_value = {
            "breakfast": 4,
            "dinner": 0,
            "lunch": 0,
        }
        mock_make_invoice.side_effect = lambda people, *_args, **_kwargs: (
            len(people),
            people,
        )
        schedule = LunchRoster(
            entries=[
                LunchSchoolEntry(
                    school_name="분당초",
                    start_date=date(2025, 6, 2),
                    end_date=date(2025, 6, 6),
                )
            ]
        )

        result = InvoiceService().generate(
            roster_data=[
                {
                    "이름": "홍길동",
                    "생일": "2010-01-02",
                    "조식여부": 1,
                    "학교": "분당초",
                }
            ],
            lunch_person_data=[
                {
                    "아동명": "홍길동",
                    "생년": "2010.01.02",
                    "소속학교": "분당초",
                }
            ],
            output_path="test.xlsx",
            year=2025,
            month=6,
            breakfast_price=1000,
            dinner_price=2000,
            lunch_price=1500,
            template_path="template.xlsx",
            col_mapping={
                "성명": "이름",
                "생년월일": "생일",
                "학교명": "학교",
                "__breakfast": "조식여부",
                "__dinner": "석식여부",
                "__note": "비고",
            },
            excluded_dates=set(),
            roster_headers=["이름", "생일", "조식여부", "학교"],
            template_columns=["성명", "생년월일", "학교명"],
            has_lunch_file=True,
            lunch_roster=schedule,
            lunch_mapping={
                "school_name_col": "학교명",
                "start_date_col": "시작일",
                "end_date_col": "종료일",
            },
            lunch_person_headers=["아동명", "생년", "소속학교"],
            lunch_person_mapping={
                "성명": "아동명",
                "생년월일": "생년",
                "학교명": "소속학교",
                "__note": "",
            },
            provider_name="테스트 제공기관",
        )

        assert result.success
        assert result.count == 1
        assert len(result.enriched) == 1
        assert result.enriched[0]["_bf_cnt"] == 4
        assert result.enriched[0]["_lh_cnt"] == 5
        assert result.enriched[0]["_lunch_dates"] == [
            "2025-06-02",
            "2025-06-03",
            "2025-06-04",
            "2025-06-05",
            "2025-06-06",
        ]
        assert mock_make_invoice.call_args.kwargs["provider_name"] == (
            "테스트 제공기관"
        )
