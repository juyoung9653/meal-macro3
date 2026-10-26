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
from src.services.document_column_mappings import effective_mapping, mapping_editor_spec
from src.templates.config import auto_detect_semantic_columns


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


def test_confirmation_address_and_jurisdiction_are_editable():
    spec = mapping_editor_spec("급식", "confirmation")
    assert spec.locked_targets == ()
    assert {"도로명주소", "관할동"} <= set(spec.targets)
    assert "상세주소" not in spec.targets


def test_semantic_total_prefers_normalized_exact_header_over_relationship_substring():
    detected = auto_detect_semantic_columns(
        ["아동과의\n관계", "계 (급식수)", "청구금액 (원)"]
    )

    assert detected["__calc_total"] == "계 (급식수)"
    assert detected["__calc_price"] == "청구금액 (원)"


def test_semantic_total_does_not_use_single_character_substring_fallback():
    detected = auto_detect_semantic_columns(["아동과의 관계", "제공기관"])

    assert detected["__calc_total"] == "계"


@pytest.mark.parametrize("profile", ["급식", "행복도시락"])
def test_invoice_relationship_is_editable_but_total_and_provider_are_automatic(profile):
    spec = mapping_editor_spec(profile, "invoice")
    normalized = {
        "".join(target.split()).replace("(급식수)", ""): target
        for target in spec.targets
    }
    relationship = normalized["아동과의관계"]
    total = normalized["계"]

    assert spec.defaults[relationship] == "아동과의 관계"
    assert relationship not in spec.locked_targets
    assert total in spec.locked_targets
    assert "제공기관" in spec.locked_targets
    assert spec.target_labels["제공기관"] == "제공기관 (공통 설정 자동 적용)"


def test_effective_invoice_mapping_ignores_stale_automatic_column_overrides():
    defaults = {
        "__calc_total": "계 (급식수)",
        "아동과의 관계": "아동과의 관계",
        "제공기관": "제공기관",
    }

    mapping = effective_mapping(
        defaults,
        {
            "아동과의 관계": "custom:relationship",
            "계 (급식수)": "아동과의 관계",
            "제공기관": "학교명",
            "연번": "학교명",
        },
    )

    assert mapping["아동과의 관계"] == "custom:relationship"
    assert "계 (급식수)" not in mapping
    assert mapping["제공기관"] == "제공기관"
    assert "연번" not in mapping


@pytest.mark.parametrize("profile", ["급식", "행복도시락"])
def test_confirmation_address_mapping_and_fallbacks_survive_revision(profile):
    from src.services.document_column_mappings import (
        build_mapping_revision as prepare, confirmation_default_mapping,
        effective_fallbacks, load_mapping_fallbacks,
    )
    original_id = UUID("6e000000-0000-0000-0000-000000000021")
    changed_id = UUID("6e000000-0000-0000-0000-000000000022")
    desired = {"도로명주소": "비고", "관할동": "학교명"}
    fallbacks = {"도로명주소": "도로명주소", "관할동": "관할동"}
    change = prepare(
        original_id, HistoricalSnapshot(original_id, {}), profile, "confirmation", desired,
        desired_fallbacks=fallbacks, actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=datetime(2026, 6, 1, tzinfo=UTC), revision_id=changed_id,
    )
    saved = HistoricalSnapshot(changed_id, {
        EntityRef(MAPPING_ENTITY_KIND, mapping_entity_id(profile, "confirmation")): change.record.changes[0].after,
    })
    assert load_mapping_overrides(saved, profile, "confirmation") == desired
    assert effective_fallbacks(
        confirmation_default_mapping(), load_mapping_fallbacks(saved, profile, "confirmation"),
        document_kind="confirmation",
    ) == fallbacks
