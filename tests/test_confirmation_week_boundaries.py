"""DB 계산부터 확인서 파일까지 주말 조식이 같은 주차에 표시되는지 검증."""

from datetime import date

import openpyxl
import pytest

from src.domain.invoice_settings import INVOICE_SETTINGS_ID, INVOICE_SETTINGS_KIND
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.domain.roster import Meal
from src.generators.confirm_generator import make_confirm_publication
from src.services.sqlite_document_service import prepare_monthly_document_data
from src.templates.template_scanner import scan_placeholders
from tests.test_sqlite_document_service import _source_snapshot


@pytest.mark.parametrize("profile", ("급식", "행복도시락"))
@pytest.mark.parametrize("include_next_month", (True, False))
@pytest.mark.parametrize("show_weekend", (True, False))
@pytest.mark.parametrize("year,month,first_end,last_count,last_label,extended_label", [
    (2026, 10, 4, 2, "10.26~10.31", "10.26~11.01"),
    (2026, 7, 5, 1, "07.27~07.31", "07.27~08.02"),
    (2027, 12, 5, 1, "12.27~12.31", "12.27~01.02"),
])
def test_weekend_breakfast_stays_in_same_week(
    tmp_path, profile, include_next_month, show_weekend,
    year, month, first_end, last_count, last_label, extended_label,
):
    source = _source_snapshot(include_school=False)
    states = dict(source.entities)
    states[EntityRef(INVOICE_SETTINGS_KIND, INVOICE_SETTINGS_ID)] = {
        "include_next_month": include_next_month,
    }
    source = HistoricalSnapshot(source.revision_id, states)
    prepared = prepare_monthly_document_data(
        source, date(year, month, 1), date(year, month, 31), (Meal.BREAKFAST,),
    )
    person = prepared.batch.deliveries[0]
    assert person.weekly[0].start_date == date(year, month, 1)
    assert person.weekly[0].end_date == date(year, month, first_end)
    expected_counts = [3, 3, 3, 3, 3 if include_next_month else last_count]
    assert [week.breakfast_count for week in person.weekly] == expected_counts
    assert person.breakfast_count == sum(expected_counts)

    template = f"assets/{profile}/{profile}_확인서템플릿.xlsx"
    output = tmp_path / "confirmation.xlsx"
    make_confirm_publication(prepared.batch, {}, output, template, meal_type=profile,
                             include_next_month_weekend=show_weekend)
    placeholders = scan_placeholders(template, ["1명"])["1명"]
    workbook = openpyxl.load_workbook(output)
    try:
        sheet = workbook["정자동"]
        assert sheet.cell(*placeholders["week1"][0]).value == f"{month:02d}.01~{month:02d}.{first_end:02d}"
        assert sheet.cell(*placeholders["week5"][0]).value == (extended_label if show_weekend else last_label)
        for week, count in enumerate(expected_counts, start=1):
            assert sheet.cell(*placeholders[f"sum_b_1_{week}"][0]).value == count
    finally:
        workbook.close()
