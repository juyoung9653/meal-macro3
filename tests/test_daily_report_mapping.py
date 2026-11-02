from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from src.domain.document_column_mappings import MAPPING_ENTITY_KIND, mapping_entity_id
from src.domain.projections import roster_entity_states, roster_from_historical_snapshot
from src.domain.revisions import EntityRef, HistoricalSnapshot, RevisionActor
from src.domain.roster import Meal
from src.services.document_column_mappings import build_mapping_revision, load_mapping_overrides
from src.services.happy_delivery_report import prepare_happy_report_data
from src.services.sqlite_delivery_report_service import prepare_delivery_report_data
from tests.test_sqlite_delivery_report_service import _snapshot


def _mapped(snapshot, profile, overrides, fallbacks=None):
    return HistoricalSnapshot(uuid4(), {
        **snapshot.entities,
        EntityRef(MAPPING_ENTITY_KIND, mapping_entity_id(profile, "daily_report")): {
            "policy_version": 2, "profile": profile, "document_kind": "daily_report", "version": 1,
            "overrides": overrides, "fallbacks": fallbacks or {}, "changed_in_revision_id": str(uuid4()),
        },
    })


def test_school_uses_latest_mapping_with_old_roster_and_keeps_counts():
    source, _ = _snapshot(people=2, meals=(Meal.DINNER, Meal.LUNCH))
    roster = roster_from_historical_snapshot(source)
    roster = replace(roster, persons=tuple(replace(person, profile=replace(person.profile, note="요청사항"))
                                           for person in roster.persons))
    source = HistoricalSnapshot(source.revision_id, {**source.entities, **roster_entity_states(roster)})
    latest = _mapped(source, "급식", {"driver": "학교명", "region": "", "address": "", "note": "비고"},
                     {"region": "성명"})
    before = prepare_delivery_report_data(source, date(2026, 8, 17), date(2026, 8, 17), (Meal.DINNER, Meal.LUNCH))
    after = prepare_delivery_report_data(source, date(2026, 8, 17), date(2026, 8, 17), (Meal.DINNER, Meal.LUNCH),
                                         mapping_snapshot=latest)
    assert before.shipments == after.shipments
    assert [r.totals for r in after.reports] == [r.totals for r in before.reports]
    for report in after.reports:
        assert report.rows[0].staff_label == "푸른초\n(1)"
        assert report.rows[0].reference_areas == "아동1, 아동2"
        assert report.rows[0].memo.count("요청사항") == 1
    assert after.calculation_manifest["column_mapping"]["profile"] == "급식"
    # The other profile's mappings must not leak into Happy reports.
    happy = prepare_happy_report_data(source, date(2026, 8, 17), date(2026, 8, 17), (Meal.DINNER,), mapping_snapshot=latest)
    assert happy.reports[0].rows[0].route.driver == "김기사"


@pytest.mark.parametrize("profile,prepare", [("급식", prepare_delivery_report_data), ("행복도시락", prepare_happy_report_data)])
def test_deleted_daily_mapping_column_fails_before_output(profile, prepare):
    source, _ = _snapshot(meals=(Meal.DINNER,))
    latest = _mapped(source, profile, {"note": f"custom:{uuid4()}"})
    with pytest.raises(ValueError, match="사용자 칼럼"):
        prepare(source, date(2026, 8, 17), date(2026, 8, 17), (Meal.DINNER,), mapping_snapshot=latest)


@pytest.mark.parametrize("profile", ["급식", "행복도시락"])
def test_daily_mapping_revision_preserves_both_priorities(profile):
    source, _ = _snapshot(meals=(Meal.DINNER,))
    prepared = build_mapping_revision(source.revision_id, source, profile, "daily_report", {"region": "학교명"},
        desired_fallbacks={"region": "관할동"}, roster=roster_from_historical_snapshot(source),
        actor=RevisionActor("test", "테스트"), recorded_at=datetime.now(UTC), revision_id=uuid4())
    change = prepared.record.changes[0]
    restored = HistoricalSnapshot(prepared.setting.changed_in_revision_id, {
        **source.entities, EntityRef(MAPPING_ENTITY_KIND, mapping_entity_id(profile, "daily_report")): change.after,
    })
    assert load_mapping_overrides(restored, profile, "daily_report")["region"] == "학교명"
    assert prepared.setting.fallbacks == {"region": "관할동"}


@pytest.mark.parametrize("profile", ["급식", "행복도시락"])
def test_mouse_selects_daily_report_and_both_mapping_priorities(profile):
    from PySide6.QtCore import Qt, QPoint
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialog
    from src.desktop_v2.document_column_mapping import DocumentColumnMappingDialog, _SCOPES
    from src.services.document_column_mappings import mapping_editor_spec
    from src.domain.document_column_sources import AUTO_HISTORY
    app = QApplication.instance() or QApplication([])
    source, _ = _snapshot()
    roster = roster_from_historical_snapshot(source)
    specs = {scope: mapping_editor_spec(*scope, roster=roster) for scope in _SCOPES}
    dialog = DocumentColumnMappingDialog(specs, {scope: {} for scope in specs})
    dialog.show()
    app.processEvents()

    def choose(combo, value):
        QTest.mouseClick(combo, Qt.MouseButton.LeftButton, pos=QPoint(combo.width() - 10, combo.height() // 2))
        app.processEvents()
        index = combo.model().index(combo.findData(value), 0)
        combo.view().scrollTo(index)
        app.processEvents()
        QTest.mouseMove(combo.view().viewport(), combo.view().visualRect(index).center())
        QTest.qWait(200)
        QTest.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton,
                         pos=combo.view().visualRect(index).center())
        app.processEvents()
        assert combo.currentData() == value

    try:
        choose(dialog._profile_combo, profile)
        choose(dialog._document_combo, "daily_report")
        choose(dialog._combos["region"], "학교명")
        choose(dialog._fallback_combos["region"], "관할동")
        choose(dialog._combos["note"], "비고")
        choose(dialog._combos["note"], AUTO_HISTORY)
        choose(dialog._fallback_combos["note"], "비고")
        QTest.mouseClick(dialog._save_button, Qt.MouseButton.LeftButton)
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.selected_profile == profile
        assert dialog.document_kind == "daily_report"
        assert dialog.desired_mapping()["region"] == "학교명"
        assert dialog.desired_fallbacks()["region"] == "관할동"
        assert dialog.desired_mapping()["note"] == AUTO_HISTORY
        assert dialog.desired_fallbacks()["note"] == "비고"
    finally:
        dialog.close()
