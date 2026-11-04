"""Read-only first and last lunch delivery dates for each driver."""

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from src.domain.delivery_routing import routing_state_from_snapshot
from src.domain.projections import roster_from_historical_snapshot
from src.domain.roster import Meal
from src.services.sqlite_delivery_report_service import prepare_physical_delivery_data


@dataclass(frozen=True, slots=True)
class DriverLunchPeriod:
    driver_id: UUID | None
    driver_name: str
    starts_on: date | None
    ends_on: date | None


def build_driver_lunch_periods(snapshot, starts_on: date, ends_on: date):
    """Use dated assignments and the same delivery rules as daily reports.

    Missing school-calendar dates are excluded, as in monthly meal statistics.
    Keep registered UUIDs distinct from legacy names and unassigned deliveries.
    """
    physical = prepare_physical_delivery_data(
        snapshot, starts_on, ends_on, (Meal.LUNCH,),
        missing_lunch_schedule_as_zero=True,
    )
    roster = roster_from_historical_snapshot(snapshot)
    routing = routing_state_from_snapshot(snapshot)
    bounds = {("registered", driver.driver_id): [driver.driver_id, driver.name, None, None]
              for driver in routing.drivers}
    for shipment in physical.shipments:
        day = shipment.delivery_on
        # The shared calculation also returns earlier comparison deliveries.
        if not starts_on <= day <= ends_on:
            continue
        resolved = routing.resolve(roster, shipment.person_id, day)
        driver_id, name = resolved.driver_ids[Meal.LUNCH], resolved.drivers[Meal.LUNCH]
        key = (("registered", driver_id) if driver_id is not None else
               ("legacy", name) if name else ("unassigned", None))
        row = bounds.setdefault(key, [driver_id, name or "미지정", None, None])
        row[2] = day if row[2] is None else min(row[2], day)
        row[3] = day if row[3] is None else max(row[3], day)
    registered = [key for key in bounds if key[0] == "registered"]
    others = sorted((key for key in bounds if key[0] != "registered"),
                    key=lambda key: (key[0] == "unassigned", str(key[1])))
    return tuple(DriverLunchPeriod(*bounds[key]) for key in (*registered, *others))
