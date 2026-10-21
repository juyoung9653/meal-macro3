"""Revision-backed input rules for editable roster columns."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from .revisions import AuditChange, ChangeOperation, HistoricalSnapshot, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource

RULES_KIND = "column_input_rules"
RULES_ID = UUID("d8a74ea6-c358-50fd-b624-9eb897ed5387")
DEFAULT_FORMATS = {"telephone": "phone", "mobile": "phone", "birth_date": "date"}
FORMATS = frozenset({"text", "phone", "date"})


@dataclass(frozen=True, slots=True)
class InputRule:
    format: str = "text"
    max_length: int | None = None

    def __post_init__(self) -> None:
        if self.format not in FORMATS:
            raise ValueError("지원하지 않는 입력 형식입니다.")
        if self.max_length is not None and (isinstance(self.max_length, bool) or not isinstance(self.max_length, int) or self.max_length < 1):
            raise ValueError("최대 글자 수는 1 이상의 숫자여야 합니다.")


def default_rule(key: str) -> InputRule:
    return InputRule(DEFAULT_FORMATS.get(key, "text"))


def load_rules(snapshot: HistoricalSnapshot | None) -> dict[str, InputRule]:
    if snapshot is None:
        return {}
    state = snapshot.entity(RULES_KIND, RULES_ID)
    if state is None:
        return {}
    if state.get("version") != 1 or not isinstance(state.get("rules"), Mapping):
        raise ValueError("저장된 입력 제한 설정이 올바르지 않습니다.")
    result = {}
    for key, item in state["rules"].items():
        if not isinstance(key, str) or not isinstance(item, Mapping):
            raise ValueError("저장된 입력 제한 설정이 올바르지 않습니다.")
        result[key] = InputRule(item.get("format"), item.get("max_length"))
    return result


def rule_for(rules: Mapping[str, InputRule], key: str) -> InputRule:
    return rules.get(key, default_rule(key))


def validate_input(key: str, value: str, rule: InputRule) -> None:
    if not value:
        return
    if rule.max_length is not None and len(value) > rule.max_length:
        raise ValueError(f"최대 {rule.max_length}글자까지 입력할 수 있습니다.")
    if rule.format == "phone":
        if not (len(value) in (12, 13) and value.count("-") == 2 and
                ((len(value) == 12 and value[3] == "-" and value[7] == "-") or
                 (len(value) == 13 and value[3] == "-" and value[8] == "-")) and
                all(part.isascii() and part.isdigit() for part in value.split("-"))):
            raise ValueError("전화번호는 000-000-0000 또는 000-0000-0000 형식으로 입력해주세요.")
    elif rule.format == "date":
        try:
            if len(value) != 10 or value[4] != "-" or value[7] != "-" or not all(part.isascii() and part.isdigit() for part in value.split("-")):
                raise ValueError
            date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("날짜는 실제 존재하는 YYYY-MM-DD 형식으로 입력해주세요.") from error


PROFILE_KEYS = {
    "name": "name", "birth_date": "birth_date", "school": "school_name",
    "school_type": "school_type", "grade": "grade", "household_head_name": "household_head_name",
    "relationship_to_child": "relationship_to_child", "jurisdiction": "jurisdiction",
    "address": "road_address", "detail_address": "detail_address", "telephone": "telephone",
    "mobile": "mobile_phone", "note": "note",
}


def profile_value(profile, key: str) -> str:
    if key.startswith("custom:"):
        value = profile.custom_values.get(UUID(key[7:]))
    else:
        value = getattr(profile, PROFILE_KEYS[key])
    return value.isoformat() if isinstance(value, date) else "" if value is None else str(value)


def validate_profile_changes(original, updated, rules: Mapping[str, InputRule]) -> None:
    for key in (*PROFILE_KEYS, *(f"custom:{item}" for item in set(original.custom_values) | set(updated.custom_values))):
        old, new = profile_value(original, key), profile_value(updated, key)
        if old != new:
            validate_input(key, new, rule_for(rules, key))


def validate_new_profile(profile, rules: Mapping[str, InputRule]) -> None:
    for key in (*PROFILE_KEYS, *(f"custom:{item}" for item in profile.custom_values)):
        validate_input(key, profile_value(profile, key), rule_for(rules, key))


def build_rules_revision(current_revision_id: UUID, snapshot: HistoricalSnapshot, key: str, rule: InputRule, *, actor: RevisionActor, recorded_at: datetime, revision_id: UUID) -> RevisionRecord | None:
    if snapshot.revision_id != current_revision_id or revision_id == current_revision_id:
        raise ValueError("입력 제한 설정의 변경번호가 올바르지 않습니다.")
    if not isinstance(actor, RevisionActor) or recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("입력 제한 설정의 작성자가 올바르지 않습니다.")
    current = load_rules(snapshot)
    if rule_for(current, key) == rule:
        return None
    current[key] = rule
    # Store explicit defaults only if another rule still differs from its default.
    rules = {k: {"format": v.format, "max_length": v.max_length} for k, v in current.items() if v != default_rule(k)}
    before = snapshot.entity(RULES_KIND, RULES_ID)
    after = {"version": 1, "rules": rules}
    change = AuditChange(RULES_KIND, RULES_ID, ChangeOperation.INSERT if before is None else ChangeOperation.UPDATE, before, after)
    metadata = RevisionMetadata(revision_id, current_revision_id, recorded_at, None, actor, "칼럼 입력 제한 설정", RevisionSource.UI, decisions={"column_input_rules": {"column": key}}, rule_versions={"column_input_rules": 1})
    return RevisionRecord(metadata, (change,))
