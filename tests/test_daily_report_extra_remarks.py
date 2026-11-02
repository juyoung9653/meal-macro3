"""Additional report remarks keep fallback, shipment and persistence semantics."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from dataclasses import replace
from uuid import uuid4

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog
from openpyxl import load_workbook

from src.domain.daily_report_choices import DailyReportChoices, ReportRemarkOptions
from src.domain.projections import roster_from_historical_snapshot, roster_entity_states
from src.domain.revisions import HistoricalSnapshot
from src.domain.roster import CustomFieldDefinition, CustomFieldType, Meal
from src.services.sqlite_delivery_report_service import prepare_delivery_report_data
from tests.test_delivery_report_forms import _snapshot, DAY


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def source_with_notes():
    source, ids = _snapshot()
    roster = roster_from_historical_snapshot(source)
    field = CustomFieldDefinition(uuid4(), "추가 비고", CustomFieldType.NUMBER, 0)
    people = tuple(replace(person, profile=replace(person.profile,
        note="대체 비고" if person.profile.name != "아동4" else None,
        custom_values={field.definition_id: 0} if person.profile.name == "아동1" else {}))
        for index, person in enumerate(roster.persons))
    roster = replace(roster, persons=people, custom_fields=(field,))
    return HistoricalSnapshot(source.revision_id, roster_entity_states(roster)), f"custom:{field.definition_id}"


@pytest.mark.parametrize("position", ("above", "below"))
def test_first_nonblank_value_zero_and_multiple_forms_leave_counts_unchanged(position, tmp_path):
    source, first = source_with_notes()
    original = prepare_delivery_report_data(source, DAY, DAY, (Meal.DINNER,))
    options = ReportRemarkOptions((first, "note", "mobile"), position, ("식재료 박스", "도시락"))
    result = prepare_delivery_report_data(source, DAY, DAY, (Meal.DINNER,), remark_options=options)
    memo = result.reports[0].rows[0].memo
    extra = "0\n(아동1)\n대체 비고\n(아동2, 아동3)\n식재료 박스 별도\n(아동2, 아동3)\n도시락 별도\n(아동1)"
    existing = original.reports[0].rows[0].memo
    assert memo == (extra + "\n" + existing if position == "above" else existing + "\n" + extra)
    assert result.shipments == original.shipments
    assert result.reports[0].totals == original.reports[0].totals
    assert result.calculation_manifest["remark_options"]["columns"] == (first, "note", "mobile")
    from src.generators.report_generator import make_report_publication
    output = tmp_path / "report.xlsx"
    make_report_publication(result.reports, output, "assets/급식/일일식수템플릿.xlsx")
    book = load_workbook(output, rich_text=True)
    assert any(str(cell.value) == memo for row in book.active for cell in row)
    book.close()


def test_excluded_shipments_and_unknown_columns():
    source, first = source_with_notes()
    result = prepare_delivery_report_data(source, DAY, DAY, (Meal.DINNER,), delivery_forms=(None, "도시락"),
        remark_options=ReportRemarkOptions(("note",), "above", ("식재료 박스",), False))
    memo = result.reports[0].rows[0].memo
    assert "대체 비고\n(아동1)" in memo
    assert "별도" not in memo and "아동2" not in memo
    with pytest.raises(ValueError, match="원본에 없"):
        prepare_delivery_report_data(source, DAY, DAY, (Meal.DINNER,),
            remark_options=ReportRemarkOptions((f"custom:{uuid4()}",)))


def test_remark_checklist_priorities_missing_columns_and_default_collapsed(app):
    from src.desktop_v2.report_remarks import ReportRemarksDialog
    from src.desktop_v2.document_meal import DocumentMealDialog
    from src.desktop_v2.document_hub import DocumentAction
    from tests.test_desktop_v2_document_meal import _period
    dialog = ReportRemarksDialog(ReportRemarkOptions(), (("note", "비고"), ("mobile", "핸드폰")), (None, "BOX", "도시락"))
    assert dialog.options().columns == ()
    dialog.priority_list.item(0).setCheckState(Qt.CheckState.Checked)
    dialog.priority_list.item(1).setCheckState(Qt.CheckState.Checked)
    dialog.priority_list.setCurrentRow(1)
    dialog._move(-1)
    assert dialog.options().columns == ("mobile", "note")
    assert dialog.priority_list.item(0).text().startswith("1순위")
    dialog.position_combo.setCurrentIndex(1)
    dialog.form_list.item(0).setCheckState(Qt.CheckState.Checked)
    dialog.form_list.item(1).setCheckState(Qt.CheckState.Checked)
    assert dialog.options().separate_forms == ("BOX", "도시락")
    dialog._accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    missing = ReportRemarksDialog(ReportRemarkOptions(("missing",)), (), ())
    missing._accept()
    assert missing.result() != QDialog.DialogCode.Accepted
    missing.priority_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    missing._accept()
    assert missing.result() == QDialog.DialogCode.Accepted
    meal = DocumentMealDialog(_period(DocumentAction.DELIVERY_REPORT))
    meal.set_report_choices(DailyReportChoices(remarks=dialog.options()))
    assert meal._remark_panel.isHidden()
    meal._extra_settings_toggle.setChecked(True)
    assert not meal._remark_panel.isHidden()
    assert meal.selection().remark_options == dialog.options()
    for widget in (meal, missing, dialog):
        widget.close()


def test_choices_survive_database_reopen_and_cancel(app, tmp_path):
    from src.desktop_v2.document_meal import load_daily_report_choices, save_daily_report_choices
    from src.desktop_v2.report_remarks import ReportRemarksDialog
    from src.storage.database_session import DatabaseSession
    from tests.test_delivery_routing import initialize
    from tests.test_sequence_workflow import roster_fixture
    options = ReportRemarkOptions(("note", "mobile"), "below", ("BOX",), False)
    path = tmp_path / "remarks.sqlite3"
    with DatabaseSession.create(path, "password") as session:
        initialize(session, roster_fixture())
        save_daily_report_choices(session, DailyReportChoices(remarks=options))
    with DatabaseSession.open(path, "password") as session:
        choices = load_daily_report_choices(session)
        assert choices.remarks == options
        dialog = ReportRemarksDialog(choices.remarks, (("note", "비고"), ("mobile", "핸드폰")), (None, "BOX"))
        dialog.priority_list.item(0).setCheckState(Qt.CheckState.Unchecked)
        dialog.reject()
        assert load_daily_report_choices(session).remarks == options
        assert save_daily_report_choices(session, choices)[1] is None


def test_delivery_form_combo_uses_existing_and_accepts_new_values(app):
    from src.desktop_v2.person_edit import PersonProfileEditDialog
    source, _ = _snapshot()
    roster = roster_from_historical_snapshot(source)
    dialog = PersonProfileEditDialog(roster.persons[0], defaults_roster=roster, delivery_only=True)
    combo = dialog._delivery_forms[Meal.DINNER]
    assert combo.isEditable()
    assert {combo.itemText(i) for i in range(combo.count())} == {"", "도시락", "식재료 박스"}
    combo.setCurrentText("새 형태")
    assert dialog._delivery_values(Meal.DINNER)[2] == "새 형태"
    combo.setCurrentText("식재료 박스")
    assert dialog._delivery_values(Meal.DINNER)[2] == "식재료 박스"
    dialog._reset_delivery_meal(Meal.DINNER)
    assert combo.currentText() == "" and combo.findText("식재료 박스") >= 0
    dialog.close()


@pytest.mark.parametrize("enabled", (True, False))
def test_excluded_forms_are_memo_only_even_with_no_included_deliveries(enabled):
    source, _ = source_with_notes()
    options = ReportRemarkOptions(("note",), "above", ("식재료 박스",), enabled)
    original = prepare_delivery_report_data(source, DAY, DAY, (Meal.DINNER,), delivery_forms=(None,))
    result = prepare_delivery_report_data(source, DAY, DAY, (Meal.DINNER,), delivery_forms=(None,), remark_options=options)
    assert result.shipments == original.shipments
    assert result.reports[0].totals == original.reports[0].totals
    assert ("식재료 박스 별도\n(아동2, 아동3)" in result.reports[0].rows[0].memo) is enabled
    assert "대체 비고" not in result.reports[0].rows[0].memo
    assert result.calculation_manifest["target_entitlement_count"] == original.calculation_manifest["target_entitlement_count"]
    roster = roster_from_historical_snapshot(source)
    people = tuple(person for person in roster.persons if person.profile.name in ("아동2", "아동3"))
    ids = {person.person_id for person in people}
    roster = replace(roster, persons=people, memberships=(),
                     enrollments=tuple(item for item in roster.enrollments if item.person_id in ids),
                     assignments=tuple(item for item in roster.assignments if item.person_id in ids))
    source = HistoricalSnapshot(source.revision_id, roster_entity_states(roster))
    only_memo = prepare_delivery_report_data(source, DAY, DAY, (Meal.DINNER,), delivery_forms=(None,), remark_options=options)
    assert only_memo.shipments == ()
    assert bool(only_memo.reports) is enabled
    if enabled:
        assert only_memo.reports[0].totals.person_total == 0
        assert "식재료 박스 별도" in only_memo.reports[0].rows[0].memo


def test_include_excluded_checkbox_defaults_on_and_roundtrips(app):
    from src.desktop_v2.report_remarks import ReportRemarksDialog
    default = ReportRemarkOptions()
    assert default.include_excluded_deliveries is True
    dialog = ReportRemarksDialog(default, (), (None, "BOX"))
    assert dialog.include_excluded.text() == "제외한 배송도 표시"
    assert dialog.include_excluded.isChecked()
    dialog.include_excluded.setChecked(False)
    assert dialog.options().include_excluded_deliveries is False
    assert ReportRemarkOptions(**dialog.options().manifest()).include_excluded_deliveries is False
    dialog.close()
