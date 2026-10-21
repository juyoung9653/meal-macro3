"""Strict household lookup with actionable delivery-document diagnostics."""
from __future__ import annotations

from datetime import date
from uuid import UUID

from src.domain.roster import RosterSnapshot
from src.services.sqlite_document_service import HouseholdServiceDatesError


def require_delivery_household(roster: RosterSnapshot, person_id: UUID,
                               service_dates: tuple[date, ...]) -> UUID:
    households = {household.household_id if (household := roster.household_for(person_id, on=day)) else None
                  for day in service_dates}
    if households and None not in households and len(households) == 1:
        return next(iter(households))
    profile = roster.person(person_id).profile
    identity = [f"성명: {profile.name}"]
    if profile.birth_date:
        identity.append(f"생년월일: {profile.birth_date.isoformat()}")
    if profile.school_name:
        identity.append(f"학교명: {profile.school_name}")
    memberships = tuple(item for item in roster.memberships if item.person_id == person_id)
    periods = [f"- 적용 시작일 {item.period.starts_on.isoformat() if item.period.starts_on else '제한 없음'}, "
               f"종료일 {item.period.ends_on.isoformat() if item.period.ends_on else '없음'}"
               for item in memberships]
    missing = [day for day in service_dates if roster.household_for(person_id, on=day) is None]
    reason = ("식사일에 유효한 가구가 없습니다." if missing or not households
              else "한 번 배송에 서로 다른 가구가 섞입니다. 가구 변경 적용일을 확인해주세요.")
    details = [reason, " / ".join(identity),
               "실제 식사일: " + ", ".join(day.isoformat() for day in sorted(set(service_dates))),
               "저장된 가구 소속 기간:", *(periods or ["- 기록 없음"])]
    if missing:
        details.append("가구가 없는 식사일: " + ", ".join(day.isoformat() for day in sorted(set(missing))))
        details.append("식종 시작일과 가구 소속 시작일은 별도로 저장됩니다. "
                       "과거 행 추가 대상자는 설정 → 가구 소속 기록 복구에서 대상과 시작일을 확인해주세요.")
    raise HouseholdServiceDatesError("\n".join(details), person_id)
