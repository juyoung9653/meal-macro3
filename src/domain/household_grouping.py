"""Database-wide, explicit column criteria for automatic household merges."""
from collections.abc import Mapping
from unicodedata import normalize
from uuid import UUID

from .revisions import AuditChange, ChangeOperation, RevisionMetadata, RevisionRecord, RevisionSource
from .roster import normalize_phone

GROUPING_KIND = "roster.household_grouping"
GROUPING_ID = UUID("1514b830-653a-4e56-9bac-775a43922159")
PROFILE_COLUMNS = {
    "mobile": ("mobile_phone", "핸드폰"),
    "telephone": ("telephone", "전화번호"),
    "address": ("road_address", "도로명주소"),
    "detail_address": ("detail_address", "상세주소"),
    "household_head_name": ("household_head_name", "세대주 성명"),
    "jurisdiction": ("jurisdiction", "관할동"),
    "name": ("name", "성명"),
    "birth_date": ("birth_date", "생년월일"),
    "school": ("school_name", "학교"),
    "school_type": ("school_type", "취학유형"),
    "grade": ("grade", "학년"),
    "relationship_to_child": ("relationship_to_child", "아동과의 관계"),
    "note": ("note", "비고"),
}


def grouping_columns_from_state(state):
    if state is None:
        return ("mobile",)
    if not isinstance(state, Mapping) or state.get("match_mode") != "all":
        raise ValueError("가구 묶기 기준을 다시 선택해주세요.")
    columns = state.get("columns")
    if not isinstance(columns, (list, tuple)) or not columns or len(set(columns)) != len(columns):
        raise ValueError("가구를 묶을 칼럼을 하나 이상 선택해주세요.")
    for key in columns:
        if not isinstance(key, str):
            raise ValueError("가구 묶기 칼럼이 올바르지 않습니다.")
        if key not in PROFILE_COLUMNS:
            if not key.startswith("custom:"):
                raise ValueError("가구 묶기에 사용할 수 없는 칼럼입니다.")
            UUID(key[7:])
    return tuple(columns)


def grouping_columns(snapshot):
    return grouping_columns_from_state(None if snapshot is None else snapshot.entity(GROUPING_KIND, GROUPING_ID))


def grouping_options(roster):
    return tuple((key, label) for key, (_, label) in PROFILE_COLUMNS.items()) + tuple(
        (f"custom:{field.definition_id}", field.name) for field in roster.custom_fields)


def profile_grouping_key(profile, columns, *, routing=None):
    result = []
    for key in columns:
        if key.startswith("custom:"):
            value = profile.custom_values.get(UUID(key[7:]))
        else:
            field = PROFILE_COLUMNS[key][0]
            source = routing if routing is not None and key in ("address", "detail_address", "jurisdiction") else profile
            value = getattr(source, field)
        text = "" if value is None else normalize("NFKC", str(value)).strip()
        result.append(normalize_phone(text) if key in ("mobile", "telephone") else text)
    return tuple(result)


def build_grouping_revision(snapshot, roster, columns, on, *, actor, recorded_at, revision_id):
    after = {"columns": list(columns), "match_mode": "all"}
    grouping_columns_from_state(after)
    if not set(columns) <= {key for key, _ in grouping_options(roster)}:
        raise ValueError("삭제된 칼럼을 제외하고 가구 묶기 기준을 다시 선택해주세요.")
    before = snapshot.entity(GROUPING_KIND, GROUPING_ID)
    return RevisionRecord(RevisionMetadata(revision_id, snapshot.revision_id, recorded_at, on,
        actor, "가구 묶기 기준 적용", RevisionSource.UI), (
        AuditChange(GROUPING_KIND, GROUPING_ID,
            ChangeOperation.INSERT if before is None else ChangeOperation.UPDATE, before, after),))
