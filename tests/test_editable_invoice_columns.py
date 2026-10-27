"""Editable calculation sources must survive saving and reach Excel cells."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
from datetime import UTC, date, datetime
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import msoffcrypto
import openpyxl
import pytest
from PySide6.QtWidgets import QApplication

from src.core.delivery_matrix import RosterEntry
from src.core.invoice_data import MealPrices, MonthlyDelivery, MonthlyDeliveryBatch
from src.desktop_v2.document_column_mapping import DocumentColumnMappingDialog
from src.domain.document_column_sources import (
    AUTO_SEQUENCE, AUTO_BREAKFAST, AUTO_LUNCH, AUTO_DINNER, AUTO_TOTAL,
    AUTO_PRICE, AUTO_HISTORY, TEMPLATE_VALUE,
)
from src.domain.revisions import RevisionActor
from src.generators.invoice_generator import make_invoice_publication
from src.services.document_column_mappings import (
    build_mapping_revision, invoice_default_mapping, load_mapping_overrides,
    load_mapping_fallbacks, mapping_editor_spec,
)
from src.templates.config import read_template_columns


def _batch():
    person = MonthlyDelivery(
        RosterEntry(2, {"성명": "예시 이름", "비고": "개인 메모", "제공기관": "공통 기관"}),
        "석식: 6/2~ 중지", "기사", "001", 2, 3, 4, (),
    )
    return MonthlyDeliveryBatch(2026, 6, MealPrices(1000, 2000, 3000), False, (person,))


def _template(path):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "청구서"
    for column, name in enumerate(("순서", "이름", "아침", "낮", "저녁", "식사 합계", "결제액", "상태 기록", "메모", "업체", "자체 수식"), 1):
        sheet.cell(2, column, name)
    sheet["K4"] = "=C4+10"
    workbook.save(path)
    workbook.close()
    return path


def _new_mapping():
    return {"순서": AUTO_SEQUENCE, "이름": "성명", "아침": AUTO_BREAKFAST,
            "낮": AUTO_LUNCH, "저녁": AUTO_DINNER, "식사 합계": AUTO_TOTAL,
            "결제액": AUTO_PRICE, "상태 기록": AUTO_HISTORY, "메모": "비고",
            "업체": "제공기관", "자체 수식": TEMPLATE_VALUE}


@pytest.mark.parametrize("profile", ["급식", "행복도시락"])
def test_every_invoice_target_has_editable_calculation_defaults_and_reset(profile):
    app = QApplication.instance() or QApplication([])
    scopes = [(p, k) for p in ("급식", "행복도시락") for k in ("invoice", "confirmation")]
    specs = {scope: mapping_editor_spec(*scope) for scope in scopes}
    view = DocumentColumnMappingDialog(specs, {scope: {} for scope in scopes})
    try:
        view._profile_combo.setCurrentIndex(view._profile_combo.findData(profile))
        spec = specs[(profile, "invoice")]
        assert set(view._combos) == set(spec.targets)
        defaults = {t: s for t, s in spec.defaults.items() if s in (
            AUTO_SEQUENCE, AUTO_BREAKFAST, AUTO_LUNCH, AUTO_DINNER, AUTO_TOTAL,
            AUTO_PRICE, AUTO_HISTORY, "제공기관")}
        assert len(defaults) >= 7
        for target, source in defaults.items():
            combo = view._combos[target]
            assert combo.isEnabled() and combo.currentData() == source
            combo.setCurrentIndex(combo.findData("성명"))
        assert view._dirty()
        view._reset_scope()
        assert all(view.desired_mapping()[t] == s for t, s in defaults.items())
        assert not view._dirty()
    finally:
        view.close()


def test_new_template_columns_receive_moved_calculations_and_preserve_formulas(tmp_path):
    template = _template(tmp_path / "new-template.xlsx")
    batch = _batch()
    batch = replace(batch, deliveries=(batch.deliveries[0], batch.deliveries[0]))
    output = tmp_path / "result.xlsx"
    make_invoice_publication(batch, _new_mapping(), str(output), str(template))
    workbook = openpyxl.load_workbook(output)
    try:
        sheet = workbook["청구서"]
        assert [sheet.cell(4, c).value for c in range(1, 12)] == [
            1, "예시 이름", 2, 3, 4, "=C4+E4+D4", "=C4*1000+E4*3000+D4*2000",
            "석식: 6/2~ 중지", "개인 메모", "공통 기관", "=C4+10"]
        assert sheet["A5"].value == 2
        assert sheet["F5"].value == "=C5+E5+D5"
        assert sheet["K5"].value == "=C5+10"
        assert batch.deliveries[0].roster.extra["비고"] == "개인 메모"
    finally:
        workbook.close()


def test_replaced_counts_do_not_corrupt_remaining_auto_amount_and_blank_clears(tmp_path):
    template = _template(tmp_path / "template.xlsx")
    output = tmp_path / "result.xlsx"
    mapping = _new_mapping()
    mapping.update({"순서": "성명", "아침": "비고", "저녁": "", "상태 기록": "비고", "업체": "성명"})
    make_invoice_publication(_batch(), mapping, str(output), str(template))
    workbook = openpyxl.load_workbook(output)
    try:
        sheet = workbook["청구서"]
        assert sheet["A4"].value == "예시 이름"
        assert sheet["C4"].value == "개인 메모"
        assert sheet["E4"].value is None
        assert sheet["F4"].value == 9
        assert sheet["G4"].value == 20000
        assert sheet["H4"].value == "개인 메모"
        assert sheet["J4"].value == "예시 이름"
    finally:
        workbook.close()


def test_calculated_fallback_zero_and_per_person_template_fallback(tmp_path):
    template = _template(tmp_path / "template.xlsx")
    batch = _batch()
    first = replace(batch.deliveries[0], breakfast_count=0,
                    roster=RosterEntry(2, {"custom": "first row"}))
    second = replace(first, roster=RosterEntry(3, {}))
    batch = replace(batch, deliveries=(first, second))
    mapping = _new_mapping()
    mapping.update({"순서": "", "아침": AUTO_BREAKFAST, "자체 수식": "custom"})
    output = tmp_path / "fallback.xlsx"
    make_invoice_publication(batch, mapping, str(output), str(template),
        column_fallbacks={"순서": AUTO_SEQUENCE, "아침": AUTO_TOTAL, "자체 수식": TEMPLATE_VALUE})
    workbook = openpyxl.load_workbook(output)
    try:
        sheet = workbook["청구서"]
        assert sheet["A4"].value == 1 and sheet["A5"].value == 2
        assert sheet["C4"].value is None  # 0 is valid, not a missing value.
        assert sheet["K4"].value == "first row"
        assert sheet["K5"].value == "=C5+10"  # Original template, not first person's text.
    finally:
        workbook.close()


def test_replaced_template_is_reread_for_editor_without_restart(tmp_path):
    template = _template(tmp_path / "template.xlsx")
    settings = SimpleNamespace(get_invoice_template_path=lambda profile: template)
    before = mapping_editor_spec("급식", "invoice", settings=settings)
    assert "아침" in before.targets
    workbook = openpyxl.load_workbook(template)
    workbook["청구서"]["C2"] = "변경된 조식 칼럼"
    workbook.save(template)
    workbook.close()
    after = mapping_editor_spec("급식", "invoice", settings=settings)
    assert "변경된 조식 칼럼" in after.targets and "아침" not in after.targets
    assert AUTO_BREAKFAST in after.selectable_source_options


def test_blank_spacer_in_changed_template_keeps_real_excel_positions(tmp_path):
    template = _template(tmp_path / "spaced.xlsx")
    workbook = openpyxl.load_workbook(template)
    workbook["청구서"].insert_cols(3)
    workbook.save(template)
    workbook.close()
    output = tmp_path / "result.xlsx"
    make_invoice_publication(_batch(), _new_mapping(), str(output), str(template))
    workbook = openpyxl.load_workbook(output)
    try:
        sheet = workbook["청구서"]
        assert sheet["C4"].value is None
        assert sheet["D4"].value == 2
        assert sheet["G4"].value == "=D4+F4+E4"
        assert sheet["H4"].value == "=D4*1000+F4*3000+E4*2000"
        assert sheet["J4"].value == "개인 메모"
    finally:
        workbook.close()


def test_saved_unlocked_columns_reopen_and_reach_controller_output(tmp_path):
    from tests.test_desktop_v2_document_generation import (
        _initial_revision, _Messages, REVISION_ID, DocumentPeriodSelection,
        DocumentAction, Meal, DocumentMealSelection, next_available_document_output, DocumentOutputMode,
    )
    from src.desktop_v2.document_generation import DocumentGenerationController
    from src.storage.database_session import DatabaseSession

    path = tmp_path / "data.sqlite3"
    template = "assets/급식/급식_청구서템플릿.xlsx"
    defaults = invoice_default_mapping(template, "급식", False)
    price_target = defaults["__calc_price"]
    history_target = next(t for t, s in defaults.items() if s == AUTO_HISTORY)
    desired = {"연번": "성명", "조식": "학교명", "제공기관": "학교명",
               price_target: "", history_target: "비고"}
    with DatabaseSession.create(path, "password") as session:
        session.append_revision(_initial_revision())
        setting = build_mapping_revision(REVISION_ID, session.database.snapshot(REVISION_ID),
            "급식", "invoice", desired, desired_fallbacks={price_target: AUTO_PRICE},
            actor=RevisionActor("tester", "sid"), recorded_at=datetime.now(UTC), revision_id=uuid4())
        session.append_revision(setting.record)
    with DatabaseSession.open(path, "password") as session:
        current = session.database.current_roster()
        snapshot = session.database.snapshot(current.revision_id)
        assert load_mapping_overrides(snapshot, "급식", "invoice") == desired
        assert load_mapping_fallbacks(snapshot, "급식", "invoice") == {price_target: AUTO_PRICE}
        source = session.database.revision_summaries()[0]
        period = DocumentPeriodSelection(DocumentAction.INVOICE, source, date(2026, 6, 17),
            Meal.LUNCH, date(2026, 6, 1), date(2026, 6, 30))
        selection = next_available_document_output(DocumentMealSelection(period, (Meal.LUNCH,)),
            tmp_path, DocumentOutputMode.SAVED_DEFAULT)
        result = DocumentGenerationController(session, message_box=_Messages).run(selection)
        assert result.completed
        with selection.output_path.open("rb") as file:
            encrypted = msoffcrypto.OfficeFile(file)
            encrypted.load_key(password="xlsx-secret")
            buffer = BytesIO()
            encrypted.decrypt(buffer)
        buffer.seek(0)
        workbook = openpyxl.load_workbook(buffer)
        try:
            columns = read_template_columns(template, sheet_name="청구서")
            def value(target): return workbook["청구서"].cell(4, columns.index(target) + 1).value
            assert value("연번") == "홍길동"
            assert value("조식") == value("제공기관") == "푸른초"
            assert value(history_target) == "알레르기 확인"
            assert isinstance(value(price_target), (int, float)) and value(price_target) > 0
        finally:
            workbook.close()
            buffer.close()
