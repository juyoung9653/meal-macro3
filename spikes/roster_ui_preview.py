"""Render the SQLite v2 roster shell with synthetic, non-personal data."""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from src.desktop_v2 import RosterWindow
from src.domain import (
    CustomFieldDefinition,
    CustomFieldType,
    DeliveryAssignment,
    Household,
    HouseholdMembership,
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    MealStatus,
    Person,
    PersonProfile,
    RosterSnapshot,
    StatusEvent,
    resolve_status_history,
)


SERVICE_DAY = date(2026, 8, 17)
REVISION_ID = UUID("90000000-0000-0000-0000-000000000001")
CUSTOM_FIELD_ID = UUID("90000000-0000-0000-0000-000000000002")


def main() -> int:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "roster-v2-preview.png")
    roster, histories = _sample_data()
    application = QApplication.instance() or QApplication([])
    window = RosterWindow(roster, histories)
    window._set_day(SERVICE_DAY)
    window._set_meal(Meal.LUNCH)
    window.show()
    window._table.selectRow(0)
    application.processEvents()
    saved = window.grab().save(str(output), "PNG")
    window.close()
    return 0 if saved else 1


def _sample_data():
    names = (
        ("김도윤", "샘플초", "6학년", "001", "A-01"),
        ("박서연", "나무초", "5학년", "002", "A-02"),
        ("이하준", "샘플중", "1학년", "010", "B-01"),
        ("최지우", "하늘초", "4학년", "011", "B-02"),
        ("정민준", "나무중", "2학년", "020", "C-01"),
        ("한예린", "샘플초", "6학년", "021", "C-02"),
    )
    persons = []
    memberships = []
    enrollments = []
    assignments = []
    households = []
    histories = {}
    for index, (name, school, grade, sequence, zone) in enumerate(names, start=10):
        person_id = UUID(f"90000000-0000-0000-0000-{index:012d}")
        household_id = UUID(f"90000000-0000-0000-0001-{index // 2:012d}")
        persons.append(
            Person(
                person_id,
                PersonProfile(
                    name,
                    birth_date=date(2014 + index % 3, 3, index),
                    school_name=school,
                    grade=grade,
                    mobile_phone=f"010-0000-{index:04d}",
                    jurisdiction="정자동",
                    road_address=f"느티로 {index}",
                    custom_values={CUSTOM_FIELD_ID: zone},
                ),
            )
        )
        if household_id not in {item.household_id for item in households}:
            households.append(Household(household_id))
        memberships.append(HouseholdMembership(person_id, household_id))
        enrollments.append(MealEnrollment(person_id, Meal.LUNCH))
        assignments.append(
            DeliveryAssignment(person_id, Meal.LUNCH, "김기사", sequence)
        )
        if index in (11, 14):
            status = MealStatus.PAUSED if index == 11 else MealStatus.STOPPED
            event = StatusEvent(
                UUID(f"90000000-0000-0000-0002-{index:012d}"),
                REVISION_ID,
                person_id,
                Meal.LUNCH,
                status,
                InclusiveDateRange(
                    SERVICE_DAY,
                    SERVICE_DAY if status is MealStatus.PAUSED else None,
                ),
                datetime(2026, 8, 17, 8, index, tzinfo=UTC),
                f"17일 {status.value}",
            )
            histories[(person_id, Meal.LUNCH)] = resolve_status_history(
                person_id,
                Meal.LUNCH,
                (event,),
            )
    roster = RosterSnapshot(
        REVISION_ID,
        tuple(persons),
        tuple(households),
        tuple(memberships),
        tuple(enrollments),
        tuple(assignments),
        (
            CustomFieldDefinition(
                CUSTOM_FIELD_ID,
                "배송 구역",
                CustomFieldType.TEXT,
                0,
            ),
        ),
    )
    return roster, histories


if __name__ == "__main__":
    raise SystemExit(main())
