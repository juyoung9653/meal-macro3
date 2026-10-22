"""AnnualDeliveryBook → 청구서 Excel 통합 테스트."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import openpyxl
import pytest
from openpyxl.worksheet.table import Table
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from src.core.delivery_matrix import (
    AnnualDeliveryBook,
    DeliveryCellValue,
    DeliveryRow,
    RosterEntry,
)
from src.core.invoice_data import (
    InvoiceData,
    MealPrices,
    MonthlyDelivery,
    MonthlyDeliveryBatch,
)
from src.core.models import FULL_ADDRESS_FIELD
from src.excel.annual_workbook import TABLE_NAME, AnnualWorkbook
from src.generators.confirm_generator import make_confirm_publication
from src.services.invoice_controller import (
    InvoiceGenerationConfig,
    InvoiceResult,
    InvoiceService,
)
from src.templates.config import read_template_columns
from src.widgets.confirm_tab import ConfirmTab
from src.widgets.invoice_tab import InvoiceTab


@pytest.fixture(autouse=True)
def _clear_template_cache():
    read_template_columns.cache_clear()
    yield
    read_template_columns.cache_clear()


@pytest.fixture
def qapp(tmp_path: Path):
    previous_format = QSettings.defaultFormat()
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path / "settings"),
    )
    app = QApplication.instance() or QApplication([])
    yield app
    QSettings.setDefaultFormat(previous_format)


@pytest.fixture
def template_path() -> str:
    path = "assets/급식/급식_청구서템플릿.xlsx"
    if not os.path.exists(path):
        pytest.skip("급식 청구서 템플릿 없음")
    return path


@pytest.fixture
def confirm_template_path() -> str:
    path = "assets/급식/급식_확인서템플릿.xlsx"
    if not os.path.exists(path):
        pytest.skip("급식 확인서 템플릿 없음")
    return path


def _delivery_row(
    person: RosterEntry,
    source_row: int,
    meal: str,
    staff: str,
    sequence: object,
    dates: tuple[date, ...],
    *,
    delivered: tuple[date, ...] = (),
) -> DeliveryRow:
    values = {day: DeliveryCellValue.BLANK for day in dates}
    values.update({day: DeliveryCellValue.DELIVERED for day in delivered})
    return DeliveryRow(
        person=person,
        meal=meal,
        staff=staff,
        sequence=sequence,
        values=values,
        source_row=source_row,
    )


def _book() -> AnnualDeliveryBook:
    # 실제 물리 열처럼 역순이며 다른 월도 포함한다.
    dates = (
        date(2026, 7, 1),
        date(2026, 6, 4),
        date(2026, 6, 3),
        date(2026, 6, 2),
        date(2026, 6, 1),
    )
    hong = RosterEntry(
        source_row=2,
        extra={
            "성명": "홍길동",
            "학교명": "분당초",
            "비고": "원본 비고",
            "조식 여부": 0,
        },
    )
    kim = RosterEntry(
        source_row=5,
        extra={"성명": "김철수", "학교명": "분당초", "비고": ""},
    )
    paused_person = RosterEntry(
        source_row=8,
        extra={"성명": "배송중지", "학교명": "분당초", "비고": ""},
    )
    roster = (hong, kim, paused_person)
    rows = (
        _delivery_row(
            hong,
            2,
            "조식",
            "김배송",
            "003",
            dates,
            delivered=(date(2026, 6, 1), date(2026, 6, 4), date(2026, 7, 1)),
        ),
        _delivery_row(hong, 3, "중식", "김배송", "003", dates),
        _delivery_row(hong, 4, "석식", "김배송", "003", dates),
        _delivery_row(kim, 5, "조식", "이배송", 2, dates),
        _delivery_row(kim, 6, "중식", "이배송", 2, dates),
        _delivery_row(
            kim,
            7,
            "석식",
            "이배송",
            2,
            dates,
            delivered=(
                date(2026, 6, 1),
                date(2026, 6, 2),
                date(2026, 6, 3),
            ),
        ),
        _delivery_row(paused_person, 8, "조식", "박배송", 9, dates),
        _delivery_row(
            paused_person,
            9,
            "중식",
            "박배송",
            9,
            dates,
        ),
        _delivery_row(paused_person, 10, "석식", "박배송", 9, dates),
    )
    return AnnualDeliveryBook(2026, roster, rows, dates)


def _write_annual_workbook(path: Path) -> Path:
    wb = openpyxl.Workbook()
    roster = wb.worksheets[0]
    roster.title = "선택 파일 데이터"
    # 실제 물리 열은 7월→6월→5월 역순이다.
    roster.append(
        [
            "성명",
            "생년월일",
            "학교명",
            "관할동",
            "도로명주소",
            "상세주소",
            "세대주\n성명",
            "핸드폰",
            "비고",
            "식종",
            "배송담당",
            "연번",
            "20260701",
            "20260608",
            "20260607",
            "20260602",
            "20260601",
            "20260531",
        ]
    )
    # 같은 성명의 연속 식종 행은 인적사항 값이 달라도 한 사람으로 읽는다.
    breakfast_profile = [
        "홍길동",
        "2015-03-02",
        None,
        "정자동",
        "성남대로 1",
        "101호",
        "홍보호",
        "010-1111-1111",
        "첫 비고",
    ]
    lunch_profile = [
        "홍길동",
        20150302,
        "분당초",
        "정자동",
        "성남대로 1",
        "101호",
        "홍보호",
        "010-1111-1111",
        None,
    ]
    dinner_profile = [
        "홍길동",
        date(2015, 3, 2),
        "다른 학교 표기",
        "정자동",
        "성남대로 1",
        "101호",
        "홍보호",
        "01011111111",
        "둘째 비고",
    ]
    roster.append(
        [
            *breakfast_profile,
            "조식",
            "기사A",
            "003",
            1,
            None,
            1,
            None,
            1,
            1,
        ]
    )
    roster.append(
        [
            *lunch_profile,
            "중식",
            "기사A",
            "003",
            None,
            None,
            None,
            None,
            None,
            None,
        ]
    )
    roster.append(
        [
            *dinner_profile,
            "석식",
            "7월기사",
            "007",
            1,
            None,
            None,
            None,
            None,
            None,
        ]
    )
    roster.add_table(Table(displayName=TABLE_NAME, ref="A1:R4"))
    wb.save(path)
    wb.close()
    return path


def test_generate_invoice_creates_valid_workbook_from_annual_book(
    tmp_path: Path, template_path: str
):
    output = tmp_path / "청구서_202606.xlsx"
    book = _book()
    config = InvoiceGenerationConfig(
        template_path=template_path,
        output_path=str(output),
        year=2026,
        month=6,
        annual_book=book,
        col_mapping={
            "성명": "성명",
            "학교명": "학교명",
            "비고": "비고",
            "__calc_breakfast": "조식",
            "__calc_dinner": "석식",
            "__calc_lunch": "중식",
            "__calc_price": "청구금액(원)",
        },
    )

    result = InvoiceService().generate(config)

    assert result.success, result.error
    assert result.count == len(result.batch.deliveries) == 2
    assert len(book.roster) == 3
    assert [entry.source_row for entry in book.roster] == [2, 5, 8]
    assert [person.roster.extra.get("성명") for person in result.batch.deliveries] == [
        "홍길동",
        "김철수",
    ]
    assert all("ID" not in person.roster.extra for person in result.batch.deliveries)
    assert output.is_file()
    assert result.batch.deliveries[0].breakfast_count == 6
    assert result.batch.deliveries[0].dinner_count == 0
    assert result.batch.deliveries[0].delivery_staff == "김배송"
    assert result.batch.deliveries[1].dinner_count == 3
    assert result.batch.deliveries[1].delivery_staff == "이배송"

    wb = openpyxl.load_workbook(output, data_only=False)
    try:
        assert wb.sheetnames == ["청구서"]
        ws = wb["청구서"]
        assert ws.max_row >= 5
        # 실제 템플릿 M/N/O = 중식/석식/조식
        assert ws["A4"].value == 1  # 청구서 연번은 배송순서가 아닌 출력 행 순번이다.
        assert ws["O4"].value == 6
        assert ws["N5"].value == 3
        assert "9500" in str(ws["Q4"].value)
    finally:
        wb.close()


def test_happy_lunch_maps_combined_address_into_invoice(
    qapp: QApplication,
    tmp_path: Path,
):
    template = "assets/행복도시락/행복도시락_청구서템플릿.xlsx"
    if not os.path.exists(template):
        pytest.skip("행복도시락 청구서 템플릿 없음")

    day = date(2026, 6, 1)
    person = RosterEntry(
        source_row=2,
        extra={
            "성명": "주소대상자",
            "도로명주소": "성남대로 1",
            "상세주소": "101호",
        },
    )
    book = AnnualDeliveryBook(
        2026,
        (person,),
        (
            _delivery_row(person, 2, "조식", "기사A", "001", (day,)),
            _delivery_row(
                person,
                3,
                "중식",
                "기사A",
                "001",
                (day,),
                delivered=(day,),
            ),
            _delivery_row(person, 4, "석식", "기사A", "001", (day,)),
        ),
        (day,),
    )

    tab = InvoiceTab(meal_type="행복도시락")
    try:
        tab.set_annual_book(book)
        assert tab._col_mapping["주소"] == FULL_ADDRESS_FIELD
        output = tmp_path / "행복도시락_청구서.xlsx"
        result = InvoiceService().generate(
            InvoiceGenerationConfig(
                template_path=template,
                output_path=str(output),
                year=2026,
                month=6,
                annual_book=book,
                col_mapping=tab._col_mapping,
                meal_type="행복도시락",
            )
        )
    finally:
        tab.close()

    assert result.success, result.error
    assert (
        result.batch.deliveries[0].roster.extra["도로명주소"],
        result.batch.deliveries[0].roster.extra["상세주소"],
    ) == ("성남대로 1", "101호")
    workbook = openpyxl.load_workbook(output, data_only=False)
    try:
        assert any(
            cell.value == "성남대로 1 101호"
            for worksheet in workbook.worksheets
            for row in worksheet.iter_rows()
            for cell in row
        )
    finally:
        workbook.close()


def test_blank_only_month_is_rejected_as_unentered(tmp_path: Path, template_path: str):
    day = date(2026, 6, 1)
    dates = (day,)
    person = RosterEntry(source_row=2, extra={"성명": "배송중지"})
    book = AnnualDeliveryBook(
        2026,
        (person,),
        (
            _delivery_row(person, 2, "조식", "", "", dates),
            _delivery_row(
                person,
                3,
                "중식",
                "",
                "",
                dates,
            ),
            _delivery_row(person, 4, "석식", "", "", dates),
        ),
        dates,
    )
    output = tmp_path / "청구서_미입력.xlsx"

    result = InvoiceService().generate(
        InvoiceGenerationConfig(
            template_path=template_path,
            output_path=str(output),
            year=2026,
            month=6,
            annual_book=book,
        )
    )

    assert not result.success
    assert "미입력" in (result.error or "")
    assert not output.exists()


def test_invoice_tab_displays_and_forwards_only_output_people(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "src.widgets.invoice_tab.read_template_columns",
        lambda *_args, **_kwargs: ["성명", "학교명"],
    )
    active = MonthlyDelivery(
        roster=RosterEntry(2, {"성명": "활성 대상", "학교명": "분당초"}),
        note="",
        delivery_staff="기사A",
        delivery_sequence="001",
        breakfast_count=2,
        lunch_count=0,
        dinner_count=0,
        weekly=(),
    )

    batch = MonthlyDeliveryBatch(
        year=2026,
        month=6,
        prices=MealPrices(breakfast=9500, lunch=9500, dinner=9500),
        is_vacation_month=False,
        deliveries=(active,),
    )
    service = Mock()
    service.generate.return_value = InvoiceResult(success=True, count=1, batch=batch)
    tab = InvoiceTab()
    try:
        tab._service = service
        tab._book = _book()
        tab._year = 2026
        tab._month = 6
        tab.output_dir = str(tmp_path)
        tab._col_mapping = {"성명": "성명", "학교명": "학교명"}

        with (
            patch("src.widgets.invoice_tab.QMessageBox.information") as information,
            patch("src.widgets.invoice_tab.QMessageBox.critical") as critical,
        ):
            tab._generate_invoices()

        critical.assert_not_called()
        service.generate.assert_called_once()
        assert tab.get_invoice_data().batch is batch
        assert tab.result_table.rowCount() == 1
        name_item = tab.result_table.item(0, 0)
        assert name_item is not None
        assert name_item.text() == "활성 대상"
        assert tab.info_label.text() == "생성된 청구서: 1건 (처음 1건 표시)"
        assert "청구서 1건 생성 완료!" in information.call_args.args[2]

        invoice_data = tab.get_invoice_data()
        assert invoice_data.batch is batch
        assert invoice_data.year == 2026
        assert invoice_data.month == 6
    finally:
        tab.close()


def test_real_annual_xlsx_to_invoice_and_confirm_end_to_end(
    tmp_path: Path,
    template_path: str,
    confirm_template_path: str,
):
    annual_path = _write_annual_workbook(tmp_path / "annual.xlsx")
    invoice_path = tmp_path / "invoice.xlsx"
    confirm_dir = tmp_path / "confirm"
    confirm_dir.mkdir()

    book = AnnualWorkbook(annual_path).load()
    assert len(book.roster) == 1
    person = book.roster[0]
    assert person.source_row == 2
    assert person.extra["성명"] == "홍길동"
    assert person.extra["학교명"] == "분당초"
    assert person.extra["세대주 성명"] == "홍보호"
    assert person.extra["핸드폰"] == "010-1111-1111"
    assert person.extra["비고"] == "첫 비고\n둘째 비고"
    assert len(book.profile_conflicts) == 1
    assert book.profile_conflicts[0].field == "학교명"
    assert "ID" not in person.extra
    person_rows = book.rows_by_person[person]
    assert tuple(row.source_row for row in person_rows) == (2, 3, 4)
    assert tuple(row.meal for row in person_rows) == ("조식", "중식", "석식")
    assert all(row.person is person for row in person_rows)
    assert book.dates == (
        date(2026, 5, 31),
        date(2026, 6, 1),
        date(2026, 6, 2),
        date(2026, 6, 7),
        date(2026, 6, 8),
        date(2026, 7, 1),
    )

    result = InvoiceService().generate(
        InvoiceGenerationConfig(
            template_path=template_path,
            output_path=str(invoice_path),
            year=2026,
            month=6,
            annual_book=book,
            col_mapping={
                "성명": "성명",
                "학교명": "학교명",
                "비고": "비고",
                "__calc_breakfast": "조식",
                "__calc_dinner": "석식",
                "__calc_lunch": "중식",
                "__calc_price": "청구금액(원)",
            },
        )
    )

    assert result.success, result.error
    assert result.count == len(result.batch.deliveries) == 1
    assert result.warnings == []
    assert "ID" not in result.batch.deliveries[0].roster.extra
    assert invoice_path.is_file()
    assert result.batch.deliveries[0].breakfast_count == 2
    assert (
        result.batch.deliveries[0].roster.extra["도로명주소"],
        result.batch.deliveries[0].roster.extra["상세주소"],
    ) == ("성남대로 1", "101호")
    assert result.batch.deliveries[0].delivery_staff == "기사A"
    assert result.batch.deliveries[0].delivery_sequence == "003"
    assert result.batch.deliveries[0].weekly[0].breakfast_count == 1
    assert result.batch.deliveries[0].weekly[1].breakfast_count == 1

    invoice_wb = openpyxl.load_workbook(invoice_path, data_only=False)
    try:
        invoice_ws = invoice_wb["청구서"]
        assert invoice_ws["A4"].value == 1
        assert invoice_ws["O4"].value == 2
        assert "DB검토" not in invoice_wb.sheetnames
    finally:
        invoice_wb.close()

    assert result.batch is not None
    created, _publication = make_confirm_publication(
        batch=result.batch,
        col_mapping={},
        output_path=confirm_dir / "확인서_202606.xlsx",
        template_path=confirm_template_path,
        meal_type="급식",
    )
    confirm_path = confirm_dir / created[0]["file"]
    assert confirm_path.is_file()
    assert created[0]["sheet_name"] == "정자동"

    confirm_wb = openpyxl.load_workbook(confirm_path, data_only=False)
    try:
        confirm_ws = confirm_wb["정자동"]
        assert confirm_ws["F3"].value == "성남대로 1 101호"
        assert confirm_ws["C6"].value == "홍보호"
        assert confirm_ws["K3"].value == "기사A"
        assert confirm_ws["G6"].value == 1
        assert confirm_ws["G7"].value == 1
        assert all(
            "None" not in cell.value
            for row in confirm_ws.iter_rows()
            for cell in row
            if isinstance(cell.value, str)
        )
    finally:
        confirm_wb.close()


def test_confirmation_tab_preserves_configured_filename_and_batch(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    batch = MonthlyDeliveryBatch(
        2027,
        3,
        MealPrices(9500, 9500, 9500),
        False,
        (
            MonthlyDelivery(
                RosterEntry(2, {"성명": "대상자"}), "", "기사A", "003", 1, 0, 0, ()
            ),
        ),
    )
    filename = "배송확인_2027-03.xlsx"
    created = [
        {
            "sheet_name": "정자동",
            "file": filename,
            "total_hh": 1,
            "count_1": 1,
            "count_2": 0,
            "count_3": 0,
            "count_4": 0,
            "count_5": 0,
        }
    ]
    writer = Mock(return_value=(created, None))
    monkeypatch.setattr("src.widgets.confirm_tab.make_confirm_publication", writer)
    tab = ConfirmTab()
    try:
        tab._s.set(
            "confirm",
            "output_filename_format",
            value="배송확인_{year}-{month:02d}.xlsx",
        )
        tab._data = InvoiceData(batch=batch, col_mapping={"성명": "이름"})
        tab.output_dir = str(tmp_path)
        with (
            patch("src.widgets.confirm_tab.QMessageBox.information"),
            patch("src.widgets.confirm_tab.QMessageBox.critical") as critical,
        ):
            tab._generate_confirms()
        critical.assert_not_called()
        assert writer.call_args.kwargs["batch"] is batch
        assert writer.call_args.kwargs["col_mapping"] == {"성명": "이름"}
        assert Path(writer.call_args.kwargs["output_path"]) == tmp_path / filename
        assert Path(tab._last_output_path) == tmp_path / filename
        assert tab.open_file_btn.isEnabled()
        assert tab.confirm_table.rowCount() == 1
    finally:
        tab.close()
