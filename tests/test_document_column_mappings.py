from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from src.domain.document_column_mappings import (
    MAPPING_ENTITY_KIND, build_mapping_revision, load_mapping_overrides,
    mapping_entity_id,
)
from src.domain.revisions import EntityRef, HistoricalSnapshot, RevisionActor
from src.generators.confirm_generator import _mapped_source_column
from src.services.document_column_mappings import mapping_editor_spec


def test_scope_reset_noop_and_history():
    original_id = UUID("6e000000-0000-0000-0000-000000000001")
    changed_id = UUID("6e000000-0000-0000-0000-000000000002")
    reset_id = UUID("6e000000-0000-0000-0000-000000000003")
    actor = RevisionActor("tester", "S-1-5-21-test")
    now = datetime(2026, 6, 1, tzinfo=UTC)
    original = HistoricalSnapshot(original_id, {})
    args = dict(defaults={"성명": "성명"}, allowed_sources=("", "성명", "학교명"), actor=actor, recorded_at=now)
    assert build_mapping_revision(original_id, original, "급식", "confirmation", {"성명": "성명"}, revision_id=changed_id, **args) is None
    changed = build_mapping_revision(original_id, original, "급식", "confirmation", {"성명": "학교명"}, revision_id=changed_id, **args)
    assert changed is not None
    saved = HistoricalSnapshot(changed_id, {EntityRef(MAPPING_ENTITY_KIND, mapping_entity_id("급식", "confirmation")): changed.record.changes[0].after})
    assert load_mapping_overrides(original, "급식", "confirmation") == {}
    assert load_mapping_overrides(saved, "급식", "confirmation") == {"성명": "학교명"}
    assert load_mapping_overrides(saved, "급식", "invoice") == {}
    assert load_mapping_overrides(saved, "행복도시락", "confirmation") == {}
    assert build_mapping_revision(changed_id, saved, "급식", "confirmation", {"성명": "학교명"}, revision_id=reset_id, **args) is None
    reset = build_mapping_revision(changed_id, saved, "급식", "confirmation", {"성명": "성명"}, revision_id=reset_id, **args)
    assert reset is not None
    assert reset.setting.overrides == {}
    assert reset.setting.version == 2


def test_rejects_unknown_target_source_and_locked_column():
    original_id = UUID("6e000000-0000-0000-0000-000000000011")
    new_id = UUID("6e000000-0000-0000-0000-000000000012")
    original = HistoricalSnapshot(original_id, {})
    common = dict(defaults={"성명": "성명", "연번": ""}, allowed_sources=("", "성명"), locked_targets=("연번",), actor=RevisionActor("tester", "S-1-5-21-test"), recorded_at=datetime(2026, 6, 1, tzinfo=UTC), revision_id=new_id)
    for desired in ({"미등록": "성명"}, {"성명": "custom:bad"}, {"연번": "성명"}):
        with pytest.raises(ValueError):
            build_mapping_revision(original_id, original, "급식", "invoice", desired, **common)


def test_confirmation_explicit_empty_mapping_clears_person_field():
    assert _mapped_source_column({"성명": ""}, "성명", "이름") == ""
    assert _mapped_source_column({"이름": "학교명"}, "성명", "이름") == "학교명"


def test_confirmation_locked_grouping_fields_have_accurate_reason():
    spec = mapping_editor_spec("급식", "confirmation")
    assert set(spec.locked_targets) == {"도로명주소", "상세주소", "관할동"}
    for target in spec.locked_targets:
        assert spec.target_labels[target] == f"{target} (가구 묶음 기준)"
