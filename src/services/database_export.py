"""Safe, presentation-ready XLSX export of a selected canonical snapshot."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from src.domain.revisions import (
    HistoricalSnapshot,
    RevisionRecord,
)
from src.domain.roster import (
    Meal,
    RosterSnapshot,
)
from src.domain.status import resolve_status_history
from src.domain.pause_conversion import pause_conversion_from_snapshot
from src.domain.projections import status_events_from_historical_snapshot
from src.domain.daily_driver_order import DAILY_DRIVER_ORDER_KIND
from src.domain.delivery_routing import (
    DRIVER_KIND,
    DRIVER_ORDER_KIND,
    ROUTING_EVENT_KIND,
    routing_state_from_snapshot,
)
from src.domain.revisions import format_revision_timestamp
from src.domain.projections import (
    ASSIGNMENT_KIND,
    CUSTOM_FIELD_KIND,
    ENROLLMENT_KIND,
    HOUSEHOLD_KIND,
    MEMBERSHIP_KIND,
    PERSON_KIND,
    STATUS_EVENT_KIND,
)
from src.domain.school_calendar import (
    SchoolLunchScheduleMissingError,
    school_lunch_calendar_from_snapshot,
)
from src.services.person_history import build_history_tables, is_roster_import_history

_KST = ZoneInfo("Asia/Seoul")
_MEALS = (Meal.BREAKFAST, Meal.LUNCH, Meal.DINNER)
_PROFILE_COLUMNS = (
    ("성명", "name"), ("생년월일", "birth_date"), ("취학유형", "school_type"), ("학교명", "school_name"),
    ("학년", "grade"), ("반", "class_name"), ("세대주 성명", "household_head_name"), ("아동과의 관계", "relationship_to_child"),
    ("관할동", "jurisdiction"), ("제공기관", "provider"), ("도로명주소", "road_address"),
    ("상세주소", "detail_address"), ("전화번호", "telephone"), ("핸드폰", "mobile_phone"), ("비고", "note"),
)
_ENTITY_LABELS = {
    PERSON_KIND: "사람", HOUSEHOLD_KIND: "가구", MEMBERSHIP_KIND: "가구원",
    ENROLLMENT_KIND: "식종 가입", ASSIGNMENT_KIND: "배송배정",
    STATUS_EVENT_KIND: "식종 상태", DRIVER_KIND: "배송담당",
    DRIVER_ORDER_KIND: "기사 표시 순서",
    DAILY_DRIVER_ORDER_KIND: "일일식수 기사 순서",
    ROUTING_EVENT_KIND: "배송 변경", CUSTOM_FIELD_KIND: "사용자 항목",
    "complaint": "민원",
}
_OPERATION_LABELS = {"insert": "추가", "update": "수정", "delete": "삭제"}


def build_database_workbook(
    roster: RosterSnapshot,
    snapshot: HistoricalSnapshot,
    revisions: tuple[RevisionRecord, ...],
    *, day: date,
    sections: tuple[str, ...] = ("roster", "complaints", "history"),
    progress=None,
) -> Workbook:
    """Return an in-memory workbook. All strings are literal text, never formulas."""
    if not isinstance(day, date) or isinstance(day, datetime):
        raise TypeError("day는 date여야 합니다.")
    if not sections or any(section not in {"roster", "complaints", "history"} for section in sections):
        raise ValueError("알 수 없는 내보내기 구역입니다.")
    if roster.revision_id != snapshot.revision_id:
        raise ValueError("명단과 내보내기 원본의 변경번호가 다릅니다.")
    progress = progress or (lambda stage, completed, total: None)
    progress("배송담당·조회 기준일 적용 중", 0, 0)
    book = Workbook()
    book.remove(book.active)
    routing = routing_state_from_snapshot(snapshot)
    calendar = school_lunch_calendar_from_snapshot(snapshot)
    metadata = _metadata(roster, revisions, day)
    if "roster" in sections:
        long_mode = _roster_sheets(book, roster, snapshot, routing, calendar, day, progress)
        metadata["행 형식"] = "명단=식종별, 명단(통합)=통합" if long_mode else "명단=통합"
    progress("민원·변경 이력 시트 작성 중", 0, 0)
    tables = (
        build_history_tables(roster, snapshot, revisions)
        if {"complaints", "history"}.intersection(sections) else {}
    )
    if "complaints" in sections:
        _add_table(book, "민원", *tables["민원"])
    if "history" in sections:
        _add_table(book, "정보 변경 이력", *tables["정보 변경"])
        _add_table(book, "식종 변경 이력", *tables["식종 변경"])
        _global_history(book, revisions)
    _add_table(book, "내보내기 정보", ("항목", "값"), tuple(metadata.items()))
    return book


def _roster_sheets(book, roster, snapshot, routing, calendar, day, progress):
    custom = tuple(sorted(roster.custom_fields, key=lambda item: item.display_order))
    resolved = {person.person_id: routing.resolve(roster, person.person_id, day) for person in roster.persons}
    drivers = {person_id: tuple(_driver_identity(view, meal) for meal in _MEALS) for person_id, view in resolved.items()}
    enrolled = {person.person_id: roster.meals_for(person.person_id, on=day) for person in roster.persons}
    long_mode = any(len({drivers[p.person_id][_MEALS.index(meal)] for meal in enrolled[p.person_id]}) > 1 for p in roster.persons)
    headers = ["사람 UUID", *_profile_headers(custom)]
    headers += (["식종", "가입", "상태", "배송담당", "배송순서"] if long_mode else ["조식", "중식", "석식", "배송담당", "조식 순서", "중식 순서", "석식 순서"])
    rows = []
    combined_rows = []
    for index, person in enumerate(roster.persons, 1):
        if index == 1 or index % 100 == 0 or index == len(roster.persons):
            progress("명단 내용 준비 중", index, len(roster.persons))
        profile = person.profile
        effective = resolved[person.person_id]
        profile_values = [_profile_value(profile, key) for _, key in _PROFILE_COLUMNS]
        profile_values[_profile_headers(custom).index("관할동")] = effective.jurisdiction
        profile_values[_profile_headers(custom).index("도로명주소")] = effective.road_address
        profile_values[_profile_headers(custom).index("상세주소")] = effective.detail_address
        base = [str(person.person_id), *profile_values, *[profile.custom_values.get(item.definition_id) for item in custom]]
        meals = enrolled[person.person_id]
        if long_mode:
            status_cells = [_status(snapshot, person.person_id, meal, day, profile, calendar) if meal in meals else "미가입" for meal in _MEALS]
            combined_rows.append((*base, *status_cells,
                                  *(effective.drivers[meal] if meal in meals else "" for meal in _MEALS),
                                  *(roster.resolve_assignment(person.person_id, meal).sequence or "" if meal in meals else "" for meal in _MEALS)))
            if not meals:
                rows.append((*base, "", "미가입", "", "", ""))
            for meal in meals:
                status = status_cells[_MEALS.index(meal)]
                rows.append((*base, meal.value, "가입", status, effective.drivers[meal], roster.resolve_assignment(person.person_id, meal).sequence or ""))
        else:
            status_cells = [_status(snapshot, person.person_id, meal, day, profile, calendar) if meal in meals else "미가입" for meal in _MEALS]
            common = [effective.drivers[meal] for meal in meals]
            shared = common[0] if common and len(set(common)) == 1 else ""
            rows.append((*base, *status_cells, shared, *(roster.resolve_assignment(person.person_id, meal).sequence or "" for meal in _MEALS)))
    progress("명단·가입기간·배송배정 시트 작성 중", 0, 0)
    _add_table(book, "명단", tuple(headers), tuple(rows))
    if long_mode:
        combined_headers = ("사람 UUID", *_profile_headers(custom), "조식", "중식", "석식",
                            "조식 배송담당", "중식 배송담당", "석식 배송담당",
                            "조식 순서", "중식 순서", "석식 순서")
        _add_table(book, "명단(통합)", combined_headers, tuple(combined_rows))
    enrollment_rows = [(str(item.person_id), item.meal.value, item.period.starts_on, item.period.ends_on) for item in roster.enrollments]
    _add_table(book, "가입기간", ("사람 UUID", "식종", "시작일", "종료일"), tuple(enrollment_rows))
    assignment_rows = [(str(item.person_id), "공통" if item.meal is None else item.meal.value, item.driver, item.sequence) for item in roster.assignments]
    _add_table(book, "배송배정", ("사람 UUID", "식종", "배송담당", "배송순서"), tuple(assignment_rows))
    return long_mode


def _status(snapshot, person_id, meal, day, profile, calendar):
    if meal is Meal.LUNCH:
        try:
            if not calendar.vendor_service_available(profile, day):
                return "-"
        except SchoolLunchScheduleMissingError:
            return "-"
    status = resolve_status_history(
        person_id, meal, status_events_from_historical_snapshot(snapshot, person_id=person_id, meal=meal),
        same_month_pause=pause_conversion_from_snapshot(snapshot).same_month,
    ).status_on(day).value
    return "미배송" if status == "중지" else status


def _driver_identity(view, meal):
    """Keep legacy names distinct when no registered driver UUID exists."""
    identifier = view.driver_ids[meal]
    return ("id", str(identifier)) if identifier is not None else ("name", view.drivers[meal].strip())


def _profile_headers(custom):
    return [label for label, _ in _PROFILE_COLUMNS] + [item.name for item in custom]


def _profile_value(profile, key):
    return getattr(profile, key)


def _metadata(roster, revisions, day):
    latest = revisions[-1].metadata if revisions else None
    return {"내보내기 기준일": day.isoformat(), "명단 revision UUID": str(roster.revision_id), "변경번호": "" if latest is None else format_revision_timestamp(latest.recorded_at), "시간대": "Asia/Seoul (KST)"}


def _global_history(book, revisions):
    headings = ("기록 시각(KST)", "적용일", "변경번호", "사람 UUID", "대상 종류", "대상 UUID", "작업", "사유", "변경자")
    rows = []
    allowed = _ENTITY_LABELS.keys()
    for record in sorted(revisions, key=lambda item: item.metadata.recorded_at):
        if is_roster_import_history(record):
            continue
        for change in record.changes:
            if change.entity_kind not in allowed:
                continue
            state = change.after or change.before or {}
            person_id = state.get("person_id", "") if hasattr(state, "get") else ""
            if change.entity_kind == "roster.person":
                person_id = str(change.entity_id)
            rows.append((_kst(record.metadata.recorded_at), record.metadata.effective_on, format_revision_timestamp(record.metadata.recorded_at), person_id, _ENTITY_LABELS[change.entity_kind], str(change.entity_id), _OPERATION_LABELS[change.operation.value], record.metadata.reason, record.metadata.actor.username))
    _add_table(book, "변경 이력", headings, tuple(rows))


def _add_table(book, title, headings, rows):
    if len(headings) > 16_384:
        raise ValueError("Excel 열 한도(16,384)를 초과했습니다.")
    for heading in headings:
        _validate_cell_value(heading)
    if len(rows) + 1 > 1_048_576:
        raise ValueError("Excel 행 한도(1,048,576)를 초과했습니다.")
    sheet = book.create_sheet(title)
    sheet.append(tuple(headings))
    header_row = sheet.max_row
    for row in rows:
        if len(row) > 16_384:
            raise ValueError("Excel 열 한도(16,384)를 초과했습니다.")
        for value in row:
            _validate_cell_value(value)
        sheet.append(tuple("" if value is None else value for value in row))
    for row in sheet.iter_rows():
        for cell in row:
            if isinstance(cell.value, str):
                if len(cell.value) > 32_767:
                    raise ValueError("Excel 셀은 32,767자를 초과할 수 없습니다.")
                _literal(cell, cell.value)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.font = Font(name="맑은 고딕", size=10)
    for cell in sheet[header_row]:
        cell.font = Font(name="맑은 고딕", size=10, bold=True)
        cell.fill = PatternFill("solid", fgColor="D9EAF7")
    sheet.freeze_panes = f"A{header_row + 1}"
    sheet.auto_filter.ref = f"A{header_row}:{get_column_letter(max(1, len(headings)))}{max(header_row, sheet.max_row)}"
    for index, heading in enumerate(headings, 1):
        sheet.column_dimensions[get_column_letter(index)].width = min(45, max(12, len(str(heading)) + 4))
    return sheet


def _literal(cell, value):
    _validate_cell_value(value)
    cell.value = value
    if isinstance(value, str):
        cell.data_type = "s"


def _validate_cell_value(value):
    if isinstance(value, str) and len(value) > 32_767:
        raise ValueError("Excel 셀은 32,767자를 초과할 수 없습니다.")


def _kst(value):
    return value.astimezone(_KST).replace(tzinfo=None) if value.tzinfo else value.replace(tzinfo=None)



def build_complaints_workbook(headings, rows):
    """Export the visible complaint rows, preserving literal text and dates."""
    book = Workbook()
    book.remove(book.active)
    try:
        sheet = _add_table(book, "민원", headings, rows)
        for row in sheet.iter_rows(min_row=2):
            row[0].number_format = "yyyy-mm-dd"
        sheet.column_dimensions["E"].width = 65
        sheet.column_dimensions["G"].width = 24
        return book
    except Exception:
        book.close()
        raise
