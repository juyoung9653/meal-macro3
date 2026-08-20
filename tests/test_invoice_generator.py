"""청구서 행 공통값 및 정규화된 명단 필드 테스트."""

from openpyxl import Workbook, load_workbook

from src.generators.invoice_generator import make_invoice


def _make_template(path):
    workbook = Workbook()
    workbook.remove(workbook.active)
    columns = ["연번", "성명", "제공기관", "조식", "석식", "중식", "청구금액"]
    for sheet_name in ("학기_총괄", "학기_학기중"):
        worksheet = workbook.create_sheet(sheet_name)
        for index, column in enumerate(columns, 1):
            worksheet.cell(1, index, column)
        worksheet.cell(4, 3, "기존 하드코딩 값")
    workbook.save(path)


def test_provider_name_overrides_template_on_every_invoice_sheet(tmp_path):
    template = tmp_path / "invoice-template.xlsx"
    output = tmp_path / "invoice.xlsx"
    _make_template(template)
    person = {
        "성명": "홍길동",
        "_bf_cnt": 1,
        "_dn_cnt": 1,
        "_lh_cnt": 0,
    }

    make_invoice(
        [person],
        {
            "성명": "서로 다른 원본 이름 칼럼",
            "__calc_breakfast": "조식",
            "__calc_dinner": "석식",
            "__calc_lunch": "중식",
            "__calc_price": "청구금액",
        },
        str(output),
        template_path=str(template),
        year=2025,
        month=6,
        unit_price_breakfast=1000,
        unit_price_dinner=1000,
        unit_price_lunch=1000,
        provider_name="공통 제공기관",
    )

    workbook = load_workbook(output, data_only=False)
    for sheet_name in ("총괄", "학기중"):
        assert workbook[sheet_name].cell(4, 2).value == "홍길동"
        assert workbook[sheet_name].cell(4, 3).value == "공통 제공기관"
