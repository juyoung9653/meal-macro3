"""Generated history labels and setting values must not expose English keys."""
import re
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from PySide6.QtCore import Qt

from src.domain.daily_report_choices import DailyReportChoices, ReportRemarkOptions, build_daily_report_choices_revision
from src.domain.document_column_mappings import DocumentColumnMappingSetting, MAPPING_ENTITY_KIND
from src.domain.projections import PERSON_KIND, CUSTOM_FIELD_KIND
from src.domain.revisions import AuditChange, ChangeOperation, HistoricalSnapshot, EntityRef
from src.services.revision_history import build_revision_change_rows
from tests.test_revision_history import _record, ACTOR, STAMP


def assert_korean_generated_rows(rows):
    assert rows
    for row in rows:
        for text in row:
            assert not re.search(r"[A-Za-z]", text), text


def test_real_daily_report_remarks_use_plain_korean_including_position():
    old = HistoricalSnapshot(uuid4(), {})
    choice = DailyReportChoices(remarks=ReportRemarkOptions(
        columns=("note", "school", "status:breakfast"), position="below",
        separate_forms=("도시락",), include_excluded_deliveries=False))
    record = build_daily_report_choices_revision(old, choice, actor=ACTOR, recorded_at=STAMP, revision_id=uuid4())
    rows = build_revision_change_rows(record, old)
    assert_korean_generated_rows(rows)
    text = str(rows)
    for expected in ("비고 표시 설정", "비고를 넣을 위치", "기존 비고 아래", "학교명", "조식", "제외한 배송도 표시"):
        assert expected in text


def test_real_document_mapping_resolves_calculation_and_custom_ids():
    custom_id = uuid4()
    setting = DocumentColumnMappingSetting("급식", "invoice", 1,
        {"비고": "__auto_history", "합계": "__auto_total", "추가 정보": "custom:" + str(custom_id)}, uuid4(),
        {"비고": "__template_value"})
    change = AuditChange(MAPPING_ENTITY_KIND, uuid4(), ChangeOperation.INSERT, None, setting.public_manifest())
    record = _record((change,))
    snapshot = HistoricalSnapshot(record.metadata.revision_id, {EntityRef(CUSTOM_FIELD_KIND, custom_id): {"name": "연락 안내"}})
    rows = build_revision_change_rows(record, snapshot)
    assert_korean_generated_rows(rows)
    assert "식종 변경 이력" in str(rows) and "연락 안내" in str(rows)
    assert str(custom_id) not in str(rows)


@pytest.mark.parametrize("kind,state", [
    ("roster.custom-field", {"field_type": "number"}),
    ("meal_service_adjustment_schedule", {"changes": [{"treatment": "replacement", "origin": "manual", "period_start": "2026-10-28", "default_delivery_on": "2026-10-27"}]}),
])
def test_setting_enums_are_readable(kind, state):
    # Use the actual known kind for custom columns.
    if kind == "roster.custom-field":
        kind = CUSTOM_FIELD_KIND
    record = _record((AuditChange(kind, uuid4(), ChangeOperation.INSERT, None, state),))
    rows = build_revision_change_rows(record, HistoricalSnapshot(record.metadata.revision_id, {}))
    assert_korean_generated_rows(rows)
    assert "기타 설정" not in str(rows)


def test_unknown_field_does_not_expose_internal_name_and_user_text_survives():
    record = _record((AuditChange(PERSON_KIND, uuid4(), ChangeOperation.INSERT, None,
                               {"name": "가상 학생", "future_internal_flag": True, "note": "ABC 안내"}),))
    rows = build_revision_change_rows(record, HistoricalSnapshot(record.metadata.revision_id, {}))
    assert "future_internal_flag" not in str(rows)
    assert "기타 설정" in str(rows)
    assert "ABC 안내" in str(rows)
    assert all(not re.search(r"[A-Za-z]", row[2]) for row in rows)


def test_summary_translates_generated_reason_and_keeps_actor_name():
    from types import SimpleNamespace
    from src.desktop_v2.revision_history import RevisionSummaryModel
    summary = SimpleNamespace(revision_id=uuid4(), sequence=1, reason="Excel 명단 가져오기",
                              recorded_at=STAMP, actor=ACTOR, display_code="20261028-1")
    model = RevisionSummaryModel((summary,))
    text = model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole)
    assert ACTOR.username in text
    assert "엑셀 명단 불러오기" in text
    assert not re.search(r"[A-Za-z]", text.replace(ACTOR.username, ""))


def test_import_settings_show_column_positions_and_formats_in_korean():
    from src.importing.contracts import ImportMappingProfile, ProfileField
    from src.importing.profile_history import IMPORT_PROFILE_KIND
    mapping = ImportMappingProfile(uuid4(), 1, "합성 명단", "roster-xlsx",
        {ProfileField.NAME: "A", ProfileField.SCHOOL_NAME: "AA"}, combined_meals_column="C",
        driver_column="D", ignored_columns=("B",), meal_true_values=("배송",), meal_false_values=("중지",))
    record = _record((AuditChange(IMPORT_PROFILE_KIND, uuid4(), ChangeOperation.INSERT, None, mapping.manifest()),))
    rows = build_revision_change_rows(record, HistoricalSnapshot(record.metadata.revision_id, {}))
    assert_korean_generated_rows(rows)
    assert "27번째 열" in str(rows)
    assert "자동 인식" in str(rows)


def test_english_operator_column_names_are_preserved_as_original_text():
    setting = DocumentColumnMappingSetting("급식", "invoice", 1, {"Delivery Notes": "비고"}, uuid4())
    record = _record((AuditChange(MAPPING_ENTITY_KIND, uuid4(), ChangeOperation.INSERT, None, setting.public_manifest()),))
    rows = build_revision_change_rows(record, HistoricalSnapshot(record.metadata.revision_id, {}))
    assert "Delivery Notes" in str(rows)
