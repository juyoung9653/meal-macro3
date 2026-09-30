"""Month-end routing uses the chosen source without changing paid meal facts."""
from datetime import date
from uuid import uuid4

import pytest

from src.core.models import FULL_ADDRESS_FIELD
from src.domain.delivery_routing import DRIVER_KIND, ROUTING_EVENT_KIND
from src.domain.projections import roster_from_historical_snapshot
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.domain.roster import Meal
from src.services.sqlite_document_service import prepare_monthly_document_data
from tests.test_sqlite_document_service import _source_snapshot


@pytest.mark.parametrize("end_day", [15, 16, 31])
def test_midmonth_change_and_next_month_reservation_preserve_billing_and_old_source(end_day):
    original = _source_snapshot()
    person = roster_from_historical_snapshot(original).persons[0]
    breakfast_driver, dinner_driver = uuid4(), uuid4()
    states = dict(original.entities)
    for driver_id, name in ((breakfast_driver, "B기사"), (dinner_driver, "C기사")):
        states[EntityRef(DRIVER_KIND, driver_id)] = {"name": name, "telephone": ""}
    for position, day, road in ((1, "2026-08-16", "이사한 주소"), (2, "2026-09-01", "다음 달 주소")):
        states[EntityRef(ROUTING_EVENT_KIND, uuid4())] = {
            "person_id": str(person.person_id), "effective_on": day, "position": position,
            "driver_ids": {"조식": str(breakfast_driver), "석식": str(dinner_driver)},
            "road_address": road, "detail_address": None,
        }
    changed = HistoricalSnapshot(uuid4(), states)
    start, end = date(2026, 8, 1), date(2026, 8, end_day)
    baseline = prepare_monthly_document_data(original, start, end, tuple(Meal))
    current = prepare_monthly_document_data(changed, start, end, tuple(Meal))
    before, after = baseline.batch.deliveries[0], current.batch.deliveries[0]
    assert current.entitlements == baseline.entitlements
    assert current.billing == baseline.billing
    assert after.weekly == before.weekly
    assert after.billing_amount > 0
    assert after.billing_amount == before.billing_amount
    if end_day < 16:
        assert after.delivery_staff == before.delivery_staff
        assert after.roster.extra["주소"] == before.roster.extra["주소"]
    else:
        assert "B기사" in after.delivery_staff and "C기사" in after.delivery_staff
        assert after.roster.extra["도로명주소"] == "이사한 주소"
        assert after.roster.extra["상세주소"] == ""
        assert after.roster.extra["주소"] == after.roster.extra[FULL_ADDRESS_FIELD] == "이사한 주소"
    old_again = prepare_monthly_document_data(original, start, end, tuple(Meal))
    old_delivery = old_again.batch.deliveries[0]
    assert old_delivery.roster.extra == before.roster.extra
    assert old_delivery.delivery_staff == before.delivery_staff
    assert old_delivery.weekly == before.weekly
    assert old_again.entitlements == baseline.entitlements
    assert old_again.billing == baseline.billing
    assert old_again.calculation_manifest == baseline.calculation_manifest
