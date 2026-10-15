from datetime import UTC, date, datetime
from uuid import uuid4

from src.domain.export_security import EXPORT_PASSWORD_SETTING_KIND
from src.domain.projections import PERSON_KIND, STATUS_EVENT_KIND
from src.domain.provider_settings import PROVIDER_SETTING_KIND
from src.domain.revisions import (
    AuditChange,
    ChangeOperation,
    EntityRef,
    HistoricalSnapshot,
    ImportBatch,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.services.revision_history import build_revision_change_rows

STAMP = datetime(2026, 10, 14, 1, 30, tzinfo=UTC)
DAY = date(2026, 10, 14)
ACTOR = RevisionActor("tester", "sid")


def _record(changes, *, source=RevisionSource.UI, import_batch=False):
    revision_id = uuid4()
    metadata = RevisionMetadata(
        revision_id,
        uuid4(),
        STAMP,
        DAY,
        ACTOR,
        "Excel 명단 가져오기" if import_batch else "변경",
        source,
        decisions={"import_purpose": "database"} if import_batch else {},
    )
    batch = None
    if import_batch:
        batch = ImportBatch(
            uuid4(), revision_id, "roster.xlsx", "0" * 64, 1, "Sheet1",
            "fingerprint", {}, DAY,
        )
    return RevisionRecord(metadata, tuple(changes), batch)


def test_revision_rows_keep_separate_status_events_and_resolve_deleted_names():
    current_person_id = uuid4()
    deleted_person_id = uuid4()
    current_event_id = uuid4()
    deleted_event_id = uuid4()
    current_state = {
        "person_id": str(current_person_id),
        "meal": "조식",
        "status": "일시중지",
        "starts_on": "2026-10-14",
        "ends_on": "2026-10-16",
        "source_text": "보호자 전화 요청",
    }
    deleted_state = {
        "person_id": str(deleted_person_id),
        "meal": "석식",
        "status": "중지",
        "starts_on": "2026-10-14",
        "ends_on": None,
        "source_text": "석식 중지 · 2026-10-14 ~ 계속",
    }
    record = _record((
        AuditChange(STATUS_EVENT_KIND, current_event_id, ChangeOperation.INSERT, None, current_state),
        AuditChange(STATUS_EVENT_KIND, deleted_event_id, ChangeOperation.INSERT, None, deleted_state),
    ))
    snapshot = HistoricalSnapshot(
        record.metadata.revision_id,
        {EntityRef(PERSON_KIND, current_person_id): {"name": "현재 이름"}},
    )
    previous = HistoricalSnapshot(
        record.metadata.parent_revision_id,
        {
            EntityRef(PERSON_KIND, current_person_id): {"name": "이전 이름"},
            EntityRef(PERSON_KIND, deleted_person_id): {"name": "삭제된 이름"},
        },
    )

    rows = build_revision_change_rows(record, snapshot, previous_snapshot=previous)

    assert len(rows) == 2
    assert rows[0][:3] == ("현재 이름", "조식", "상태 변경")
    assert "상태: 일시중지" in rows[0][3]
    assert "기간: 2026-10-14 ~ 2026-10-16" in rows[0][3]
    assert "원문: 보호자 전화 요청" in rows[0][3]
    assert rows[1][0] == "삭제된 이름"
    assert "석식 중지" not in rows[1][3]  # Generated source text is not repeated.


def test_revision_rows_include_ordinary_imports_and_safe_setting_details():
    person_id = uuid4()
    change = AuditChange(
        PERSON_KIND,
        person_id,
        ChangeOperation.INSERT,
        None,
        {"name": "가져온 사람", "school_name": "새 학교", "custom_values": {}},
    )
    record = _record((change,), source=RevisionSource.IMPORT, import_batch=True)
    snapshot = HistoricalSnapshot(
        record.metadata.revision_id,
        {EntityRef(PERSON_KIND, person_id): change.after},
    )

    rows = build_revision_change_rows(record, snapshot)

    assert len(rows) == 1
    assert rows[0][0] == "가져온 사람"
    assert rows[0][2] == "개인정보"
    assert "학교명: 새 학교" in rows[0][3]


def test_revision_rows_redact_password_and_summarize_unknown_kind_without_payload():
    secret = "do-not-display-this"
    password_change = AuditChange(
        EXPORT_PASSWORD_SETTING_KIND,
        uuid4(),
        ChangeOperation.UPDATE,
        {"version": 1, "password": "old-secret"},
        {"version": 2, "password": secret},
    )
    provider_change = AuditChange(
        PROVIDER_SETTING_KIND,
        uuid4(),
        ChangeOperation.UPDATE,
        {"provider": "기존 기관"},
        {"provider": "새 기관"},
    )
    unknown_change = AuditChange(
        "future.secret_setting",
        uuid4(),
        ChangeOperation.INSERT,
        None,
        {"secret": secret, "nested": {"password": secret}},
    )
    record = _record((password_change, provider_change, unknown_change))
    snapshot = HistoricalSnapshot(record.metadata.revision_id, {})

    rows = build_revision_change_rows(record, snapshot)
    rendered = repr(rows)

    assert rows[0][3] == "비밀번호 설정 변경"
    assert rows[1][3] == "수정 · 제공기관: 기존 기관 → 새 기관"
    assert rows[2][2] == "future.secret_setting"
    assert rows[2][3] == "future.secret_setting · 추가"
    assert secret not in rendered
    assert "old-secret" not in rendered


def test_revision_rows_reject_invalid_snapshots_explicitly():
    record = _record((
        AuditChange("future.kind", uuid4(), ChangeOperation.INSERT, None, {"value": 1}),
    ))
    snapshot = HistoricalSnapshot(record.metadata.revision_id, {})

    try:
        build_revision_change_rows(record, snapshot, previous_snapshot=object())
    except TypeError as error:
        assert "previous_snapshot" in str(error)
    else:
        raise AssertionError("invalid previous_snapshot must fail")


def test_import_settings_show_localized_details_without_nested_secrets():
    from src.importing.households import HOUSEHOLD_MATCHING_RULE_KIND
    from src.importing.profile_history import IMPORT_PROFILE_KIND

    record = _record((
        AuditChange(IMPORT_PROFILE_KIND, uuid4(), ChangeOperation.INSERT, None, {
            "profile": {"name": "명단 연결", "profile_columns": {"name": "성명 칼럼"},
                        "password": "nested-secret"},
        }),
        AuditChange(HOUSEHOLD_MATCHING_RULE_KIND, uuid4(), ChangeOperation.INSERT, None, {
            "rule": {"criteria": ["mobile_phone", "address"], "match_mode": "all"},
        }),
    ))
    rows = build_revision_change_rows(record, HistoricalSnapshot(record.metadata.revision_id, {}))
    assert rows[0][2] == "Excel 가져오기 설정"
    assert "성명 칼럼" in rows[0][3]
    assert rows[1][2] == "가구 묶기 기준"
    assert "핸드폰 / 주소" in rows[1][3]
    assert "모든 기준 일치" in rows[1][3]
    assert "nested-secret" not in repr(rows)
