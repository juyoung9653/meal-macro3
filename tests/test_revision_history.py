from datetime import UTC, date, datetime
from uuid import uuid4

from src.domain.export_security import EXPORT_PASSWORD_SETTING_KIND
from src.domain.delivery_routing import DRIVER_KIND, ROUTING_EVENT_KIND
from src.domain.household_shared import HOUSEHOLD_SHARED_KIND
from src.domain.projections import ASSIGNMENT_KIND, CUSTOM_FIELD_KIND, ENROLLMENT_KIND, HOUSEHOLD_KIND, MEMBERSHIP_KIND, PERSON_KIND, STATUS_EVENT_KIND
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
    assert rows[0][:3] == ("현재 이름", "조식", "조식")
    assert rows[0][3].startswith("조식 일시중지\n")
    assert "적용 기간: 2026-10-14 ~ 2026-10-16" in rows[0][3]
    assert "사유: 보호자 전화 요청" in rows[0][3]
    assert rows[1][0] == "삭제된 이름"
    assert "사유:" not in rows[1][3]  # Generated source text is not repeated.


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

    assert len(rows) == 2
    assert all(row[0] == "가져온 사람" for row in rows)
    assert (rows[0][2], rows[0][3]) == ("성명", "이전: 미지정\n이후: 가져온 사람")
    assert (rows[1][2], rows[1][3]) == ("학교명", "이전: 미지정\n이후: 새 학교")


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
    assert rows[1][2:] == ("제공기관", "이전: 기존 기관\n이후: 새 기관")
    assert rows[2][2] == "기타 변경"
    assert rows[2][3] == "항목을 추가했습니다."
    assert "future.secret_setting" not in rendered
    assert secret not in rendered
    assert "old-secret" not in rendered


def test_cancellation_marker_explains_person_meal_and_cancelled_action_in_korean():
    from dataclasses import replace
    from src.domain.meal_history_cancellation import CANCELLATION_KIND, CANCELLATION_DECISION

    person, event, marker_id = uuid4(), uuid4(), uuid4()
    state = {"person_id": str(person), "meal": "석식", "status": "일시중지",
             "starts_on": "2026-10-14", "ends_on": "2026-10-16"}
    marker = {"source_revision_id": str(uuid4()), "entity_kind": STATUS_EVENT_KIND,
              "entity_id": str(event), "starts_on": None, "ends_on": None}
    record = _record((
        AuditChange(STATUS_EVENT_KIND, event, ChangeOperation.DELETE, state, None),
        AuditChange(CANCELLATION_KIND, marker_id, ChangeOperation.INSERT, None, marker),
    ))
    record = replace(record, metadata=replace(record.metadata, decisions={CANCELLATION_DECISION: marker}))
    snapshot = HistoricalSnapshot(record.metadata.revision_id, {
        EntityRef(PERSON_KIND, person): {"name": "김하늘"},
    })
    rows = build_revision_change_rows(record, snapshot)
    assert rows[-1] == ("김하늘", "석식", "식종 변경 등록 취소",
                        "석식 일시중지 등록을 취소했습니다.\n적용 기간: 2026-10-14 ~ 2026-10-16")
    assert "meal.history_cancellation" not in repr(rows)
    assert str(event) not in repr(rows)


def test_batch_delivery_combines_automatic_enrollment_with_status_without_duplicate_people():
    from src.domain.meal_status_changes import build_batch_meal_status_revision
    from src.domain.projections import roster_audit_changes
    from src.domain.roster import Household, HouseholdMembership, InclusiveDateRange, Meal, Person, PersonProfile, RosterSnapshot
    from src.domain.status import MealStatus

    first, second, home, revision_id = uuid4(), uuid4(), uuid4(), uuid4()
    roster = RosterSnapshot(uuid4(),
        persons=(Person(first, PersonProfile("김하늘")), Person(second, PersonProfile("이수민"))),
        households=(Household(home),),
        memberships=(HouseholdMembership(first, home), HouseholdMembership(second, home)))
    targets = ((first, Meal.DINNER), (second, Meal.DINNER))
    prepared = build_batch_meal_status_revision(roster, targets, MealStatus.DELIVERY,
        InclusiveDateRange(DAY), actor=ACTOR, recorded_at=STAMP, revision_id=revision_id,
        event_ids={target: uuid4() for target in targets})
    before = {EntityRef(c.entity_kind, c.entity_id): c.after for c in roster_audit_changes(None, roster)}
    after = dict(before)
    for change in prepared.record.changes:
        after[EntityRef(change.entity_kind, change.entity_id)] = change.after
    rows = build_revision_change_rows(prepared.record, HistoricalSnapshot(revision_id, after),
        previous_snapshot=HistoricalSnapshot(roster.revision_id, before))
    assert len(rows) == 1
    assert rows[0][0] == "김하늘\n이수민"
    assert rows[0][3].startswith("석식 배송\n")
    assert "가입 기간 변경" in rows[0][3]
    assert "이전: 미지정" in rows[0][3]
    assert repr(rows).count("김하늘") == 1
    assert repr(rows).count("이수민") == 1


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
    assert rows[0][2] == "양식"
    assert "성명 칼럼" in rows[0][3]
    assert rows[1][2] == "가구 묶기 규칙"
    assert "핸드폰 / 주소" in rows[1][3]
    assert "모든 기준 일치" in rows[1][3]
    assert "nested-secret" not in repr(rows)


def test_normal_changes_resolve_saved_names_and_show_before_after_without_ids():
    person_id, field_id, driver_id, route_id = (uuid4() for _ in range(4))
    old_person = {"name": "김하늘", "custom_values": {str(field_id): {"kind": "scalar", "value": "기존"}}}
    new_person = {"name": "김하늘", "custom_values": {str(field_id): {"kind": "scalar", "value": "새 값"}}}
    record = _record((
        AuditChange(PERSON_KIND, person_id, ChangeOperation.UPDATE, old_person, new_person),
        AuditChange(ROUTING_EVENT_KIND, route_id, ChangeOperation.INSERT, None, {
            "person_id": str(person_id), "driver_ids": {"석식": str(driver_id)},
        }),
    ))
    snapshot = HistoricalSnapshot(record.metadata.revision_id, {
        EntityRef(PERSON_KIND, person_id): new_person,
        EntityRef(CUSTOM_FIELD_KIND, field_id): {"name": "알레르기"},
        EntityRef(DRIVER_KIND, driver_id): {"name": "박기사"},
    })

    rows = build_revision_change_rows(record, snapshot)

    assert rows[0][0] == "김하늘"
    assert rows[0][2:] == ("알레르기", "이전: 기존\n이후: 새 값")
    assert rows[1][2:] == ("배송담당", "이전: 미지정\n이후: 석식: 박기사")
    assert all(str(identifier) not in repr(rows) for identifier in (person_id, field_id, driver_id, route_id))


def test_user_text_and_dynamic_setting_names_are_not_hidden_as_internal_ids():
    text_id = str(uuid4())
    change = AuditChange(PERSON_KIND, uuid4(), ChangeOperation.UPDATE,
        {"name": "김하늘", "note": "이전", "overrides": {"석식": "보통"}},
        {"name": "김하늘", "note": text_id, "overrides": {"석식": "변경"}})
    record = _record((change,))
    rows = build_revision_change_rows(record, HistoricalSnapshot(record.metadata.revision_id, {}))

    assert text_id in repr(rows)
    assert "석식: 보통" in repr(rows)
    assert "석식: 변경" in repr(rows)


def test_sensitive_custom_field_name_redacts_its_value():
    field_id = uuid4()
    change = AuditChange(PERSON_KIND, uuid4(), ChangeOperation.INSERT, None, {
        "name": "김하늘",
        "custom_values": {str(field_id): {"kind": "scalar", "value": "hidden-secret"}},
    })
    record = _record((change,))
    snapshot = HistoricalSnapshot(record.metadata.revision_id, {
        EntityRef(CUSTOM_FIELD_KIND, field_id): {"name": "비밀번호"},
    })

    rows = build_revision_change_rows(record, snapshot)

    assert ("비밀번호", "설정 변경") in tuple(row[2:] for row in rows)
    assert "hidden-secret" not in repr(rows)


def test_membership_change_names_old_and_new_households_with_same_dates():
    from src.domain.projections import MEMBERSHIP_KIND

    moving, old_member, new_member, former_member = (uuid4() for _ in range(4))
    old_home, new_home = uuid4(), uuid4()

    def membership(person, home, start="2026-10-01", end=None):
        return {"person_id": str(person), "periods": [{
            "household_id": str(home), "starts_on": start, "ends_on": end,
        }]}

    before = membership(moving, old_home)
    after = membership(moving, new_home)
    record = _record((AuditChange(MEMBERSHIP_KIND, moving, ChangeOperation.UPDATE, before, after),))
    common = {
        EntityRef(PERSON_KIND, moving): {"name": "이동 대상"},
        EntityRef(PERSON_KIND, old_member): {"name": "이전 가구원"},
        EntityRef(PERSON_KIND, new_member): {"name": "새 가구원"},
        EntityRef(PERSON_KIND, former_member): {"name": "과거 가구원"},
        EntityRef(MEMBERSHIP_KIND, old_member): membership(old_member, old_home),
        EntityRef(MEMBERSHIP_KIND, new_member): membership(new_member, new_home),
        EntityRef(MEMBERSHIP_KIND, former_member): membership(
            former_member, old_home, "2026-01-01", "2026-09-30"),
    }
    previous = HistoricalSnapshot(record.metadata.parent_revision_id, {
        **common, EntityRef(MEMBERSHIP_KIND, moving): before,
    })
    current = HistoricalSnapshot(record.metadata.revision_id, {
        **common, EntityRef(MEMBERSHIP_KIND, moving): after,
    })
    row = build_revision_change_rows(record, current, previous_snapshot=previous)[0]
    text = row[3]
    assert "같은 가구가 바뀌었습니다" in row[2]
    assert "이전: 이동 대상, 이전 가구원 가구" in text
    assert "이후: 새 가구원, 이동 대상 가구" in text
    assert "과거 가구원" not in text
    assert "2026-10-01 ~ " in text
    assert str(old_home) not in text and str(new_home) not in text


def test_matching_status_events_group_meals_with_middle_dots_at_first_position():
    person_id, other_id = uuid4(), uuid4()

    def status(person, meal, *, reason=None):
        return {"person_id": str(person), "meal": meal, "status": "중지",
                "starts_on": "2026-10-16", "ends_on": None,
                "source_text": reason or f"{meal} 중지 · 2026-10-16 ~ 계속"}

    changes = (
        AuditChange(STATUS_EVENT_KIND, uuid4(), ChangeOperation.INSERT, None,
                    status(person_id, "석식")),
        AuditChange(PROVIDER_SETTING_KIND, uuid4(), ChangeOperation.UPDATE,
                    {"provider": "이전 기관"}, {"provider": "새 기관"}),
        AuditChange(STATUS_EVENT_KIND, uuid4(), ChangeOperation.INSERT, None,
                    status(person_id, "중식")),
        AuditChange(STATUS_EVENT_KIND, uuid4(), ChangeOperation.INSERT, None,
                    status(other_id, "석식")),
        AuditChange(STATUS_EVENT_KIND, uuid4(), ChangeOperation.INSERT, None,
                    status(person_id, "조식", reason="보호자 요청")),
        AuditChange(STATUS_EVENT_KIND, uuid4(), ChangeOperation.INSERT, None,
                    status(person_id, "석식")),
    )
    record = _record(changes)
    snapshot = HistoricalSnapshot(record.metadata.revision_id, {
        EntityRef(PERSON_KIND, person_id): {"name": "같은 이름"},
        EntityRef(PERSON_KIND, other_id): {"name": "같은 이름"},
    })

    rows = build_revision_change_rows(record, snapshot)

    assert len(rows) == 5
    assert rows[0][1] == "석식 · 중식"
    assert rows[0][3] == "석식 · 중식 중지\n적용 기간: 2026-10-16 ~ "
    assert rows[1][2] == "제공기관"  # Unrelated row keeps its ledger position.
    assert rows[2][1] == "석식"  # Same visible name, different person ID.
    assert rows[3][3].startswith("조식 중지\n적용 기간: 2026-10-16 ~ \n사유: 보호자 요청")
    assert rows[4][1] == "석식"  # A second event for one meal remains visible.


def test_status_update_and_delete_keep_before_after_event_details():
    person_id = uuid4()
    before = {"person_id": str(person_id), "meal": "석식", "status": "일시중지",
              "starts_on": "2026-10-16", "ends_on": "2026-10-19",
              "source_text": "보호자 요청"}
    after = {**before, "status": "중지", "ends_on": None}
    record = _record((
        AuditChange(STATUS_EVENT_KIND, uuid4(), ChangeOperation.UPDATE, before, after),
        AuditChange(STATUS_EVENT_KIND, uuid4(), ChangeOperation.DELETE, before, None),
    ))
    snapshot = HistoricalSnapshot(record.metadata.revision_id, {})

    rows = build_revision_change_rows(record, snapshot)

    assert len(rows) == 2
    assert rows[0][3].startswith("석식 중지 수정\n이전:\n상태: 일시중지")
    assert "적용 기간: 2026-10-16 ~ 2026-10-19" in rows[0][3]
    assert "이후:\n상태: 중지\n적용 기간: 2026-10-16 ~ " in rows[0][3]
    assert rows[1][3].startswith("석식 일시중지 기록 삭제\n이전:\n상태: 일시중지")
    assert "사유: 보호자 요청" in rows[1][3]


def test_roster_column_aliases_apply_to_fields_and_status_column_only():
    person_id, field_id = uuid4(), uuid4()
    before = {"name": "김하늘", "school_name": "기존 학교", "mobile_phone": "010-1111-1111",
              "road_address": "이전 주소", "custom_values": {str(field_id): {"kind": "scalar", "value": "없음"}}}
    after = {**before, "school_name": "새 학교", "mobile_phone": "010-2222-2222",
             "road_address": "새 주소", "custom_values": {str(field_id): {"kind": "scalar", "value": "있음"}}}
    status = lambda meal: {"person_id": str(person_id), "meal": meal, "status": "중지",
                           "starts_on": "2026-10-16", "ends_on": None, "source_text": None}
    record = _record((
        AuditChange(PERSON_KIND, person_id, ChangeOperation.UPDATE, before, after),
        AuditChange(STATUS_EVENT_KIND, uuid4(), ChangeOperation.INSERT, None, status("석식")),
        AuditChange(STATUS_EVENT_KIND, uuid4(), ChangeOperation.INSERT, None, status("중식")),
    ))
    snapshot = HistoricalSnapshot(record.metadata.revision_id, {
        EntityRef(PERSON_KIND, person_id): after,
        EntityRef(CUSTOM_FIELD_KIND, field_id): {"name": "알레르기"},
    })
    labels = {"school": "학교 별칭", "mobile": "휴대전화", "address": "주소 별칭",
              f"custom:{field_id}": "주의 사항", "status:dinner": "저녁", "status:lunch": "점심"}

    rows = build_revision_change_rows(record, snapshot, column_labels=labels)

    assert [row[2] for row in rows] == ["주의 사항", "휴대전화", "주소 별칭", "학교 별칭", "저녁 · 점심"]
    assert rows[-1][1] == "석식 · 중식"
    assert rows[-1][3].startswith("석식 · 중식 중지\n")


def test_household_target_lists_only_members_active_on_effective_date():
    household_id = uuid4()
    active_a, active_b, expired, future = (uuid4() for _ in range(4))

    def membership(person_id, start, end):
        return {"person_id": str(person_id), "periods": [{
            "household_id": str(household_id), "starts_on": start, "ends_on": end,
        }]}

    record = _record((AuditChange(HOUSEHOLD_KIND, household_id, ChangeOperation.UPDATE,
                                  {"delivery_settings": []},
                                  {"delivery_settings": [{"meal": "석식", "weekdays": [0]}]}),))
    snapshot = HistoricalSnapshot(record.metadata.revision_id, {
        EntityRef(PERSON_KIND, active_a): {"name": "김하늘"},
        EntityRef(PERSON_KIND, active_b): {"name": "이수민"},
        EntityRef(PERSON_KIND, expired): {"name": "과거 가구원"},
        EntityRef(PERSON_KIND, future): {"name": "미래 가구원"},
        EntityRef(MEMBERSHIP_KIND, active_a): membership(active_a, "2026-10-01", None),
        EntityRef(MEMBERSHIP_KIND, active_b): membership(active_b, "2026-10-14", "2026-10-14"),
        EntityRef(MEMBERSHIP_KIND, expired): membership(expired, "2026-01-01", "2026-10-13"),
        EntityRef(MEMBERSHIP_KIND, future): membership(future, "2026-10-15", None),
    })

    rows = build_revision_change_rows(record, snapshot)

    assert rows[0][0] == "김하늘\n이수민"
    assert "과거 가구원" not in repr(rows)
    assert "미래 가구원" not in repr(rows)


def test_identical_household_edits_merge_only_changed_people_in_same_home():
    home_a, home_b = uuid4(), uuid4()
    first, second, other_home, unchanged, expired = (uuid4() for _ in range(5))
    names = {first: "김하늘", second: "이수민", other_home: "김하늘",
             unchanged: "미변경 가구원", expired: "과거 가구원"}
    homes = {first: home_a, second: home_a, other_home: home_b,
             unchanged: home_a, expired: home_a}
    changes = tuple(AuditChange(PERSON_KIND, person_id, ChangeOperation.UPDATE,
                                {"name": names[person_id], "road_address": "이전 주소"},
                                {"name": names[person_id], "road_address": "새 주소"})
                    for person_id in (first, other_home, second))
    record = _record(changes)
    entities = {}
    for person_id, name in names.items():
        entities[EntityRef(PERSON_KIND, person_id)] = {"name": name}
        entities[EntityRef(MEMBERSHIP_KIND, person_id)] = {
            "person_id": str(person_id), "periods": [{
                "household_id": str(homes[person_id]),
                "starts_on": "2026-01-01",
                "ends_on": "2026-10-13" if person_id == expired else None,
            }],
        }

    rows = build_revision_change_rows(record, HistoricalSnapshot(record.metadata.revision_id, entities))

    assert len(rows) == 2
    assert rows[0][0] == "김하늘\n이수민"
    assert rows[1][0] == "김하늘"  # Same visible name in another home stays separate.
    assert "미변경 가구원" not in repr(rows)
    assert "과거 가구원" not in repr(rows)


def test_household_target_preserves_two_members_with_the_same_name():
    home, first, second = uuid4(), uuid4(), uuid4()
    change = AuditChange(HOUSEHOLD_KIND, home, ChangeOperation.UPDATE,
                         {"delivery_settings": []}, {"delivery_settings": [{"meal": "석식"}]})
    record = _record((change,))
    snapshot = HistoricalSnapshot(record.metadata.revision_id, {
        EntityRef(PERSON_KIND, first): {"name": "김하늘"},
        EntityRef(PERSON_KIND, second): {"name": "김하늘"},
        EntityRef(MEMBERSHIP_KIND, first): {"person_id": str(first), "periods": [
            {"household_id": str(home), "starts_on": None, "ends_on": None}]},
        EntityRef(MEMBERSHIP_KIND, second): {"person_id": str(second), "periods": [
            {"household_id": str(home), "starts_on": None, "ends_on": None}]},
    })

    rows = build_revision_change_rows(record, snapshot)

    assert rows[0][0] == "김하늘\n김하늘"


def test_same_header_alias_does_not_merge_distinct_custom_fields_in_one_home():
    home, first, second, field_a, field_b = (uuid4() for _ in range(5))
    changes = (
        AuditChange(PERSON_KIND, first, ChangeOperation.UPDATE,
                    {"name": "김하늘", "custom_values": {str(field_a): {"kind": "scalar", "value": "이전"}}},
                    {"name": "김하늘", "custom_values": {str(field_a): {"kind": "scalar", "value": "새 값"}}}),
        AuditChange(PERSON_KIND, second, ChangeOperation.UPDATE,
                    {"name": "이수민", "custom_values": {str(field_b): {"kind": "scalar", "value": "이전"}}},
                    {"name": "이수민", "custom_values": {str(field_b): {"kind": "scalar", "value": "새 값"}}}),
    )
    record = _record(changes)
    entities = {}
    for person_id, name in ((first, "김하늘"), (second, "이수민")):
        entities[EntityRef(PERSON_KIND, person_id)] = {"name": name}
        entities[EntityRef(MEMBERSHIP_KIND, person_id)] = {"person_id": str(person_id),
            "periods": [{"household_id": str(home), "starts_on": None, "ends_on": None}]}
    snapshot = HistoricalSnapshot(record.metadata.revision_id, entities)

    rows = build_revision_change_rows(record, snapshot, column_labels={
        f"custom:{field_a}": "같은 제목", f"custom:{field_b}": "같은 제목",
    })

    assert len(rows) == 2
    assert [row[0] for row in rows] == ["김하늘", "이수민"]
    assert [row[2] for row in rows] == ["같은 제목", "같은 제목"]


def test_shared_columns_and_exceptions_get_separate_target_column_action_rows():
    person_id = uuid4()
    before = {"columns": [], "address_columns_version": 2}
    after = {"columns": ["address"], "address_columns_version": 2,
             "exceptions": [{"person_id": str(person_id), "column": "address"}]}
    record = _record((AuditChange(HOUSEHOLD_SHARED_KIND, uuid4(), ChangeOperation.UPDATE,
                                  before, after),))
    snapshot = HistoricalSnapshot(record.metadata.revision_id, {
        EntityRef(PERSON_KIND, person_id): {"name": "김하늘"},
    })

    rows = build_revision_change_rows(record, snapshot, column_labels={"address": "주소 별칭"})

    assert rows == (("전체 명단", "", "주소 별칭", "가구 공용 지정"),
                    ("김하늘", "", "주소 별칭", "공용 예외 지정"))

    remove_record = _record((AuditChange(HOUSEHOLD_SHARED_KIND, uuid4(),
                                         ChangeOperation.UPDATE, after, before),))
    removed = build_revision_change_rows(remove_record, snapshot,
                                         column_labels={"address": "주소 별칭"})
    assert removed == (("전체 명단", "", "주소 별칭", "가구 공용 해제"),
                       ("김하늘", "", "주소 별칭", "공용 예외 해제"))


def test_empty_fields_and_internal_routing_position_do_not_make_ghost_rows():
    person_id, field_id = uuid4(), uuid4()
    record = _record((
        AuditChange(PERSON_KIND, person_id, ChangeOperation.UPDATE,
                    {"name": "김하늘", "road_address": None,
                     "custom_values": {str(field_id): {"kind": "scalar", "value": None}}},
                    {"name": "김하늘", "road_address": "",
                     "custom_values": {str(field_id): {"kind": "scalar", "value": ""}}}),
        AuditChange(ROUTING_EVENT_KIND, uuid4(), ChangeOperation.UPDATE,
                    {"person_id": str(person_id), "position": 1},
                    {"person_id": str(person_id), "position": 2}),
    ))
    snapshot = HistoricalSnapshot(record.metadata.revision_id, {
        EntityRef(PERSON_KIND, person_id): {"name": "김하늘"},
        EntityRef(CUSTOM_FIELD_KIND, field_id): {"name": "비고2"},
    })

    assert build_revision_change_rows(record, snapshot) == ()


def test_custom_typed_and_untyped_empty_values_are_equal_but_deletion_is_visible():
    person_id, field_id = uuid4(), uuid4()
    empty_change = AuditChange(PERSON_KIND, person_id, ChangeOperation.UPDATE,
        {"name": "김하늘", "custom_values": {str(field_id): ""}},
        {"name": "김하늘", "custom_values": {str(field_id): {"kind": "scalar", "value": None}}})
    delete_change = AuditChange(PERSON_KIND, person_id, ChangeOperation.UPDATE,
        {"name": "김하늘", "custom_values": {str(field_id): {"kind": "scalar", "value": "내용"}}},
        {"name": "김하늘", "custom_values": {str(field_id): {"kind": "scalar", "value": ""}}})
    record = _record((delete_change,))
    snapshot = HistoricalSnapshot(record.metadata.revision_id, {
        EntityRef(PERSON_KIND, person_id): {"name": "김하늘"},
        EntityRef(CUSTOM_FIELD_KIND, field_id): {"name": "비고2"},
    })

    empty_record = _record((empty_change,))
    assert build_revision_change_rows(empty_record, snapshot) == ()
    assert build_revision_change_rows(record, snapshot) == (
        ("김하늘", "", "비고2", "이전: 내용\n이후: 미지정"),
    )


def test_same_rendered_household_names_do_not_hide_distinct_household_ids():
    person_id, old_home, new_home = uuid4(), uuid4(), uuid4()
    old = {"person_id": str(person_id), "periods": [{
        "household_id": str(old_home), "starts_on": "2026-10-01", "ends_on": None}]}
    new = {"person_id": str(person_id), "periods": [{
        "household_id": str(new_home), "starts_on": "2026-10-01", "ends_on": None}]}
    record = _record((AuditChange(MEMBERSHIP_KIND, person_id, ChangeOperation.UPDATE, old, new),))
    previous = HistoricalSnapshot(record.metadata.parent_revision_id, {
        EntityRef(PERSON_KIND, person_id): {"name": "김하늘"},
        EntityRef(MEMBERSHIP_KIND, person_id): old,
    })
    current = HistoricalSnapshot(record.metadata.revision_id, {
        EntityRef(PERSON_KIND, person_id): {"name": "김하늘"},
        EntityRef(MEMBERSHIP_KIND, person_id): new,
    })

    rows = build_revision_change_rows(record, current, previous_snapshot=previous)

    assert len(rows) == 1
    assert rows[0][3].count("김하늘 가구") == 2
    assert str(old_home) not in repr(rows) and str(new_home) not in repr(rows)


def test_row_add_and_delete_show_meal_period_and_actual_assignment_columns():
    person_id = uuid4()
    enrollment = {"person_id": str(person_id), "meal": "석식", "periods": [
        {"starts_on": "2026-10-14", "ends_on": None}]}
    assignment = {"person_id": str(person_id), "meal": "석식",
                  "driver": "박기사", "sequence": "001"}
    record = _record((
        AuditChange(ENROLLMENT_KIND, uuid4(), ChangeOperation.INSERT, None, enrollment),
        AuditChange(ASSIGNMENT_KIND, uuid4(), ChangeOperation.INSERT, None, assignment),
        AuditChange(ASSIGNMENT_KIND, uuid4(), ChangeOperation.DELETE, assignment, None),
    ))
    snapshot = HistoricalSnapshot(record.metadata.revision_id, {
        EntityRef(PERSON_KIND, person_id): {"name": "김하늘"},
    })

    rows = build_revision_change_rows(record, snapshot, column_labels={
        "status:dinner": "저녁", "sequence": "순서",
    })

    assert rows == (
        ("김하늘", "석식", "저녁", "이전: 미지정\n이후: 적용 기간: 2026-10-14 ~ "),
        ("김하늘", "석식", "배송담당", "석식\n이전: 미지정\n이후: 박기사"),
        ("김하늘", "석식", "순서", "석식\n이전: 미지정\n이후: 001"),
        ("김하늘", "석식", "배송담당", "석식\n이전: 박기사\n이후: 미지정"),
        ("김하늘", "석식", "순서", "석식\n이전: 001\n이후: 미지정"),
    )


def test_row_add_omits_unset_assignment_fields_and_empty_enrollment_stub():
    person_id = uuid4()
    record = _record((
        AuditChange(ENROLLMENT_KIND, uuid4(), ChangeOperation.INSERT, None,
                    {"person_id": str(person_id), "meal": "중식", "periods": []}),
        AuditChange(ASSIGNMENT_KIND, uuid4(), ChangeOperation.INSERT, None,
                    {"person_id": str(person_id), "meal": "석식",
                     "driver": None, "sequence": "001"}),
        AuditChange(ASSIGNMENT_KIND, uuid4(), ChangeOperation.INSERT, None,
                    {"person_id": str(person_id), "meal": "중식",
                     "driver": "박기사", "sequence": None}),
    ))
    snapshot = HistoricalSnapshot(record.metadata.revision_id, {
        EntityRef(PERSON_KIND, person_id): {"name": "김하늘"},
    })

    assert build_revision_change_rows(record, snapshot) == (
        ("김하늘", "석식", "배송순서", "석식\n이전: 미지정\n이후: 001"),
        ("김하늘", "중식", "배송담당", "중식\n이전: 미지정\n이후: 박기사"),
    )


def test_same_named_driver_identity_and_sensitive_setting_changes_stay_visible():
    person_id, first_driver, second_driver = uuid4(), uuid4(), uuid4()
    record = _record((
        AuditChange(ROUTING_EVENT_KIND, uuid4(), ChangeOperation.UPDATE,
                    {"person_id": str(person_id), "driver_ids": {"석식": str(first_driver)}},
                    {"person_id": str(person_id), "driver_ids": {"석식": str(second_driver)}}),
        AuditChange(PROVIDER_SETTING_KIND, uuid4(), ChangeOperation.UPDATE,
                    {"password": "old-secret"}, {"password": "new-secret"}),
    ))
    snapshot = HistoricalSnapshot(record.metadata.revision_id, {
        EntityRef(PERSON_KIND, person_id): {"name": "김하늘"},
        EntityRef(DRIVER_KIND, first_driver): {"name": "동명이인"},
        EntityRef(DRIVER_KIND, second_driver): {"name": "동명이인"},
    })

    rows = build_revision_change_rows(record, snapshot)

    assert rows[0][2:] == ("배송담당", "이전: 석식: 동명이인\n이후: 석식: 동명이인")
    assert rows[1][3] == "민감한 설정 변경"
    assert "old-secret" not in repr(rows) and "new-secret" not in repr(rows)
