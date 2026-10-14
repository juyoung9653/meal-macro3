from datetime import UTC, date, datetime
from uuid import uuid4

from src.domain.roster import (
    CustomFieldDefinition,
    CustomFieldType,
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.revisions import (
    HistoricalSnapshot,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.domain.status import StatusEvent
from src.domain.complaints import Complaint, complaint_state
from src.domain.intake import IntakeChannel
from src.domain.projections import STATUS_EVENT_KIND
from src.domain.revisions import EntityRef
from src.domain.status import MealStatus
from src.services.database_export import build_database_workbook


def _record(revision_id, change):
    return RevisionRecord(
        RevisionMetadata(revision_id, None, datetime(2026, 9, 1, 1, tzinfo=UTC), None,
                         RevisionActor("tester", "sid"), "정보 변경", RevisionSource.UI,
                         display_code="20260901-001"),
        (change,),
    )


def test_one_different_enrolled_meal_driver_makes_entire_roster_long_and_keeps_literals():
    custom_id = uuid4()
    custom = CustomFieldDefinition(custom_id, "사용자값", CustomFieldType.TEXT, 0)
    first = Person(uuid4(), PersonProfile("=수식", telephone="01001234", mobile_phone="00123", custom_values={custom_id: "=safe"}))
    second = Person(uuid4(), PersonProfile("둘째"))
    roster = RosterSnapshot(
        uuid4(), persons=(first, second), custom_fields=(custom,),
        enrollments=(MealEnrollment(first.person_id, Meal.BREAKFAST), MealEnrollment(first.person_id, Meal.LUNCH), MealEnrollment(second.person_id, Meal.DINNER)),
    )
    # Routing events deliberately use distinct IDs for the first person's meals.
    from src.domain.delivery_routing import (
        DRIVER_KIND,
        ROUTING_EVENT_KIND,
        RoutingEvent,
    )
    breakfast, lunch = uuid4(), uuid4()
    event = RoutingEvent(uuid4(), first.person_id, date(2026, 1, 1), 1, {Meal.BREAKFAST: breakfast, Meal.LUNCH: lunch})
    snapshot = HistoricalSnapshot(roster.revision_id, {
        EntityRef(DRIVER_KIND, breakfast): {"name": "동명이인", "telephone": "1"},
        EntityRef(DRIVER_KIND, lunch): {"name": "동명이인", "telephone": "2"},
        EntityRef(ROUTING_EVENT_KIND, event.event_id): {"person_id": str(first.person_id), "effective_on": "2026-01-01", "position": 1, "driver_ids": {"조식": str(breakfast), "중식": str(lunch)}, "road_address": None, "detail_address": None},
    })
    book = build_database_workbook(roster, snapshot, (), day=date(2026, 9, 1), sections=("roster",))
    sheet = book["명단"]
    headers = [cell.value for cell in sheet[1]]
    assert "식종" in headers and "조식 순서" not in headers
    assert sheet.cell(2, 2).value == "=수식"
    assert sheet.cell(2, 2).data_type == "s"
    assert sheet.cell(2, headers.index("전화번호") + 1).value == "01001234"
    assert any(row[0].value == str(second.person_id) and row[headers.index("가입")].value == "가입" for row in sheet.iter_rows(min_row=2))
    combined = book["명단(통합)"]
    combined_headers = [cell.value for cell in combined[1]]
    first_row = next(row for row in combined.iter_rows(min_row=2, values_only=True) if row[0] == str(first.person_id))
    assert first_row[1] == "=수식"
    assert combined.cell(2, 2).data_type == "s"
    assert first_row[combined_headers.index("조식 배송담당")] == "동명이인"
    assert first_row[combined_headers.index("중식 배송담당")] == "동명이인"
    assert len(list(combined.iter_rows(min_row=2))) == 2
    assert sheet.freeze_panes == combined.freeze_panes == "A2"
    assert sheet.auto_filter.ref.startswith("A1:")
    assert book.sheetnames[-1] == "내보내기 정보"
    assert dict(list(book["내보내기 정보"].values)[1:])["행 형식"] == "명단=식종별, 명단(통합)=통합"


def test_exports_status_and_complaint_history_from_passed_snapshot_only():
    person = Person(uuid4(), PersonProfile("아동"))
    roster = RosterSnapshot(uuid4(), persons=(person,), enrollments=(MealEnrollment(person.person_id, Meal.DINNER),))
    event = StatusEvent(uuid4(), uuid4(), person.person_id, Meal.DINNER, MealStatus.STOPPED, InclusiveDateRange(date(2026, 9, 1), None), datetime(2026, 9, 1, 1, tzinfo=UTC), "중지")
    complaint = Complaint(uuid4(), person.person_id, datetime(2026, 9, 2, 1, tzinfo=UTC), IntakeChannel.PHONE, "=민원", Meal.DINNER)
    snapshot = HistoricalSnapshot(roster.revision_id, {
        EntityRef(STATUS_EVENT_KIND, event.event_id): {"revision_id": str(event.revision_id), "person_id": str(person.person_id), "meal": "석식", "status": "중지", "starts_on": "2026-09-01", "ends_on": None, "recorded_at": event.recorded_at.isoformat(), "source_text": "중지"},
        EntityRef("complaint", complaint.complaint_id): complaint_state(complaint),
    })
    book = build_database_workbook(roster, snapshot, (), day=date(2026, 9, 2))
    assert book["명단"].cell(2, 2).value == "아동"
    complaint_sheet = book["민원"]
    assert complaint_sheet.cell(2, 9).value == "=민원"
    assert complaint_sheet.cell(2, 9).data_type == "s"
    assert "식종 변경 이력" in book.sheetnames
