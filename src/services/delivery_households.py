"""Current household lookup shared by delivery documents."""
from __future__ import annotations

from datetime import date
from uuid import UUID

from src.domain.roster import RosterSnapshot
from src.services.sqlite_document_service import HouseholdServiceDatesError


def require_delivery_household(roster: RosterSnapshot, person_id: UUID,
                               service_dates: tuple[date, ...]) -> UUID:
    """Use the selected revision's current household, including legacy singletons."""
    return roster.current_membership(person_id).household_id
