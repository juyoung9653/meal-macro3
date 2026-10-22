"""Per-person exemption behavior at the revision and UI boundaries."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog
from tests.test_household_shared import (roster, historical, apply, record_for, DAY, NOW, ACTOR,
    roster_from_historical_snapshot, roster_audit_changes, RevisionRecord, RevisionMetadata,
    RevisionSource, build_person_profile_revision, Meal, InclusiveDateRange, StatusEvent,
    MealStatus, status_event_audit_change, RoutingEvent, _routing_event_state,
    AuditChange, ChangeOperation, ROUTING_EVENT_KIND, routing_state_from_snapshot,
    CustomFieldDefinition, CustomFieldType)
from src.domain.household_shared import *
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.storage.database_session import DatabaseSession
from src.desktop_v2.household_shared_exceptions import (
    HouseholdSharedExceptionsDialog, HouseholdSharedExceptionsController)


def exempt(r, keys, pairs):
    snap = historical(r, keys)
    entities = dict(snap.entities)
    entities[EntityRef(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)] = {
        "columns": list(keys), "exceptions": [{"person_id": str(pid), "column": key} for pid, key in pairs]}
    return HistoricalSnapshot(r.revision_id, entities)


def exception_record(r, snap, pairs):
    return build_household_shared_exceptions_revision(r, snap.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID), pairs,
        actor=ACTOR, recorded_at=NOW, effective_on=DAY, revision_id=uuid4())


def edit_mobile(r, snap, pid, text):
    prepared = build_person_profile_revision(r, pid, replace(r.person(pid).profile, mobile_phone=text),
        actor=ACTOR, recorded_at=NOW, effective_on=DAY, revision_id=uuid4())
    return apply(snap, expand_household_shared_revision(snap, prepared.record))


@pytest.mark.parametrize("source", [0, 1])
def test_exempt_person_neither_sends_nor_receives(source):
    r = roster(); first, second, _ = r.persons
    snap = exempt(r, ("mobile",), ((second.person_id, "mobile"),))
    final = roster_from_historical_snapshot(edit_mobile(r, snap, r.persons[source].person_id, "new"))
    assert final.person(r.persons[source].person_id).profile.mobile_phone == "new"
    assert final.person(r.persons[1-source].person_id).profile.mobile_phone == "old"


def test_exception_changes_preserve_values_and_rejoin_future_edits():
    r = roster(); first, second, _ = r.persons
    snap = exempt(r, ("mobile",), ((second.person_id, "mobile"),))
    snap = edit_mobile(r, snap, second.person_id, "individual")
    current = roster_from_historical_snapshot(snap)
    record = exception_record(current, snap, ())
    assert len(record.changes) == 1
    snap = apply(snap, record)
    joined = roster_from_historical_snapshot(snap)
    assert joined.person(second.person_id).profile.mobile_phone == "individual"
    final = roster_from_historical_snapshot(edit_mobile(joined, snap, first.person_id, "future"))
    assert final.person(second.person_id).profile.mobile_phone == "future"


def test_review_excludes_exceptions_all_exempt_skips_and_toggle_preserves():
    r = roster(); pairs = {(p.person_id, "mobile") for p in r.persons}
    snap = exempt(r, (), pairs)
    assert household_shared_rows(r, "mobile", DAY, snapshot=snap) == ()
    record = build_household_shared_revision(r, snap.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID), "mobile", True, {},
        actor=ACTOR, recorded_at=NOW, effective_on=DAY, revision_id=uuid4(), snapshot=snap)
    assert len(record.changes) == 1
    assert shared_exceptions_from_state(record.changes[0].after) == pairs
    assert shared_exceptions_from_state(None) == frozenset()
    assert shared_exceptions_from_state({"columns": []}) == frozenset()
    with pytest.raises(ValueError):
        exception_record(r, snap, {(uuid4(), "mobile")})
    with pytest.raises(ValueError):
        exception_record(r, snap, {(r.persons[0].person_id, "not-a-column")})
    assert exception_record(r, snap, pairs) is None


def test_review_and_enable_conflict_use_only_participants():
    r = roster(); a,b,_ = r.persons
    snap = exempt(r, (), {(b.person_id, "mobile")})
    rows = household_shared_rows(r, "mobile", DAY, snapshot=snap)
    own = next(row for row in rows if a.profile.name in row.member_names)
    assert own.member_names == (a.profile.name,)
    values = {row.household_id: ("shared",) for row in rows}
    record = build_household_shared_revision(r, snap.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID), "mobile", True, values,
        actor=ACTOR, recorded_at=NOW, effective_on=DAY, revision_id=uuid4(), snapshot=snap)
    result = roster_from_historical_snapshot(apply(snap, record))
    assert result.person(b.person_id).profile.mobile_phone == "old"
    assert result.person(a.person_id).profile.mobile_phone == "shared"


@pytest.mark.parametrize("source_exempt", [False, True])
def test_status_events_and_enrollments_respect_both_directions(source_exempt):
    r = roster(); a,b,_ = r.persons
    pid = a.person_id if source_exempt else b.person_id
    snap = exempt(r, ("status:dinner",), {(pid, "status:dinner")})
    event = StatusEvent(uuid4(), uuid4(), a.person_id, Meal.DINNER, MealStatus.STOPPED, InclusiveDateRange(DAY, None), NOW)
    changed = replace(r, revision_id=uuid4(), enrollments=tuple(e for e in r.enrollments if e.person_id != a.person_id))
    result = expand_household_shared_revision(snap, record_for(r, changed, extra=(status_event_audit_change(event),)))
    final = roster_from_historical_snapshot(apply(snap, result))
    assert Meal.DINNER in final.meals_for(b.person_id, on=DAY)
    assert not any(c.after and c.entity_kind == "roster.status_event" and c.after.get("person_id") == str(b.person_id) for c in result.changes)


@pytest.mark.parametrize("source_exempt", [False, True])
def test_dated_routing_filters_fields_independently(source_exempt):
    r = roster(); a,b,_ = r.persons
    pid = a.person_id if source_exempt else b.person_id
    snap = exempt(r, ("address", "jurisdiction"), {(pid, "address")})
    event = RoutingEvent(uuid4(), a.person_id, DAY, 1, {}, road_address="road", detail_address="detail", jurisdiction="district")
    change = AuditChange(ROUTING_EVENT_KIND,event.event_id,ChangeOperation.INSERT,None,_routing_event_state(event))
    result = apply(snap, expand_household_shared_revision(snap, record_for(r,replace(r,revision_id=uuid4()),extra=(change,))))
    view = routing_state_from_snapshot(result).resolve(roster_from_historical_snapshot(result),b.person_id,DAY)
    assert not view.road_address
    assert view.jurisdiction == "district"


def test_deleted_custom_column_removes_exception_without_shared_columns():
    r = roster(); field = CustomFieldDefinition(uuid4(), "Extra", CustomFieldType.TEXT, 0)
    r = replace(r, custom_fields=(field,))
    snap = exempt(r, (), {(r.persons[0].person_id, f"custom:{field.definition_id}")})
    record = record_for(r, replace(r, revision_id=uuid4(), custom_fields=()))
    result = apply(snap, expand_household_shared_revision(snap, record))
    assert shared_exceptions_from_state(result.entity(HOUSEHOLD_SHARED_KIND,HOUSEHOLD_SHARED_ID)) == frozenset()


def test_database_reopen_retains_exceptions_and_preserves_values(tmp_path):
    r = roster(); path = tmp_path / "exceptions.sqlite3"
    initial = RevisionRecord(RevisionMetadata(r.revision_id,None,NOW,None,ACTOR,"initial",RevisionSource.IMPORT),roster_audit_changes(None,r))
    pair = (r.persons[0].person_id,"mobile")
    with DatabaseSession.create(path,"password","recovery") as session:
        session.append_revision(initial)
        session.append_revision(exception_record(r,historical(r),{pair}))
        saved = session.database.current_roster()
        assert saved.revision_id != r.revision_id
        assert saved.person(pair[0]).profile == r.person(pair[0]).profile
    with DatabaseSession.open(path,"password") as session:
        current = session.database.current_roster()
        snap = session.database.snapshot(current.revision_id)
        assert shared_exceptions_from_state(snap.entity(HOUSEHOLD_SHARED_KIND,HOUSEHOLD_SHARED_ID)) == {pair}


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_dialog_person_drafts_search_alias_and_inactive(app):
    r = replace(roster(), enrollments=())
    first, second = r.persons[:2]
    stored = {
        "address_columns_version": 2,
        "columns": ["mobile", "note"],
        "exceptions": [
            {"person_id": str(first.person_id), "column": "mobile"},
            {"person_id": str(second.person_id), "column": "note"},
        ],
    }
    dialog = HouseholdSharedExceptionsDialog(r,stored,DAY,labels={"mobile":"연락 번호"})
    index = next(i for i in range(dialog.columns.count()) if dialog.columns.item(i).data(Qt.ItemDataRole.UserRole)=="mobile")
    item = dialog.columns.item(index)
    assert item.text().startswith("연락 번호")
    first_item = next(dialog.people.item(i) for i in range(dialog.people.count())
        if dialog.people.item(i).data(Qt.ItemDataRole.UserRole)==first.person_id)
    dialog.people.setCurrentItem(first_item)
    pid = dialog.people.currentItem().data(Qt.ItemDataRole.UserRole)
    assert "미가입" in dialog.people.currentItem().text()
    assert item.checkState() == Qt.CheckState.Checked
    second_item = next(dialog.people.item(i) for i in range(dialog.people.count())
        if dialog.people.item(i).data(Qt.ItemDataRole.UserRole)==second.person_id)
    dialog.people.setCurrentItem(second_item)
    dialog.people.setCurrentItem(first_item)
    assert item.checkState() == Qt.CheckState.Checked
    assert dialog.selected_exceptions() == {
        (first.person_id,"mobile"), (second.person_id,"note"),
    }
    dialog.search.setText(str(pid))
    assert sum(not dialog.people.item(i).isHidden() for i in range(dialog.people.count())) == 1
    dialog.reject()


@pytest.mark.parametrize("mode", ["cancel", "unchanged", "failed", "stale", "success"])
def test_controller_save_boundaries(mode):
    r = roster(); snap = historical(r); calls=[]; errors=[]
    pair = (r.persons[0].person_id,"mobile")
    class Dialog:
        def __init__(self,*args,**kwargs): pass
        def exec(self):
            if mode=="stale": db.current_roster=lambda:replace(r,revision_id=uuid4())
            return QDialog.DialogCode.Rejected if mode=="cancel" else QDialog.DialogCode.Accepted
        def selected_exceptions(self): return () if mode=="unchanged" else {pair}
    db=SimpleNamespace(current_roster=lambda:r,snapshot=lambda _:snap)
    def append(record):
        calls.append(record)
        if mode=="failed": raise RuntimeError("save failed")
        return SimpleNamespace(error=None)
    session=SimpleNamespace(database=db,append_revision=append,last_committed_roster=None)
    messages=SimpleNamespace(critical=lambda *args:errors.append(args),warning=lambda *args:None)
    outcome=HouseholdSharedExceptionsController(session,dialog_type=Dialog,message_box=messages,actor_provider=lambda:ACTOR,recorded_at_provider=lambda:NOW).run(DAY)
    assert len(calls)==(1 if mode in ("failed","success") else 0)
    assert bool(errors)==(mode in ("failed","stale"))
    assert (outcome.roster is not None)==(mode=="success")


def test_cell_exception_controller_persists_reopens_unchecks_and_noops(tmp_path):
    r=roster(); path=tmp_path/"cell-exception.sqlite3"
    initial=RevisionRecord(RevisionMetadata(r.revision_id,None,NOW,None,ACTOR,"initial",RevisionSource.IMPORT),roster_audit_changes(None,r))
    pair=(r.persons[0].person_id,"mobile")
    with DatabaseSession.create(path,"password") as session:
        session.append_revision(initial)
        current=session.database.current_roster()
        snapshot=session.database.snapshot(current.revision_id)
        rows=household_shared_rows(current,"mobile",DAY,snapshot=snapshot)
        values={row.household_id:("공용 연락처",) for row in rows}
        shared=build_household_shared_revision(
            current,snapshot.entity(HOUSEHOLD_SHARED_KIND,HOUSEHOLD_SHARED_ID),
            "mobile",True,values,actor=ACTOR,recorded_at=NOW,effective_on=DAY,
            revision_id=uuid4(),snapshot=snapshot,
        )
        session.append_revision(shared)
        controller=HouseholdSharedExceptionsController(
            session,actor_provider=lambda:ACTOR,recorded_at_provider=lambda:NOW,
        )
        added=controller.set_exception(*pair,True,DAY)
        assert added.roster is not None
        revision=added.roster.revision_id
        assert controller.set_exception(*pair,True,DAY).roster is None
        assert session.database.current_roster().revision_id==revision
    with DatabaseSession.open(path,"password") as session:
        current=session.database.current_roster()
        state=session.database.snapshot(current.revision_id).entity(HOUSEHOLD_SHARED_KIND,HOUSEHOLD_SHARED_ID)
        assert shared_exceptions_from_state(state)=={pair}
        removed=HouseholdSharedExceptionsController(
            session,actor_provider=lambda:ACTOR,recorded_at_provider=lambda:NOW,
        ).set_exception(*pair,False,DAY)
        assert removed.roster is not None
    with DatabaseSession.open(path,"password") as session:
        current=session.database.current_roster()
        state=session.database.snapshot(current.revision_id).entity(HOUSEHOLD_SHARED_KIND,HOUSEHOLD_SHARED_ID)
        assert shared_exceptions_from_state(state)==frozenset()


def test_cell_exception_save_failure_refreshes_committed_state(app):
    from src.desktop_v2.application import DesktopStartupController
    r=roster(); pair=(r.persons[0].person_id,"mobile")
    snapshot=exempt(r,("mobile",),())
    errors=[]
    session=SimpleNamespace(
        database=SimpleNamespace(current_roster=lambda:r,snapshot=lambda _revision:snapshot),
        append_revision=lambda _record: (_ for _ in ()).throw(RuntimeError("save failed")),
    )
    messages=SimpleNamespace(critical=lambda *args:errors.append(args),warning=lambda *args:None)
    controller=DesktopStartupController(
        household_shared_exceptions_controller_type=lambda active:
            HouseholdSharedExceptionsController(
                active,message_box=messages,actor_provider=lambda:ACTOR,
                recorded_at_provider=lambda:NOW,
            )
    )
    refreshed=[]
    window=SimpleNamespace(
        table_model=SimpleNamespace(service_day=DAY),
        set_household_shared_state=lambda columns,exceptions:refreshed.append((columns,exceptions)),
    )
    controller._run_household_shared_exception(session,window,*pair,True)
    assert errors
    assert refreshed==[(frozenset({"mobile"}),frozenset())]



def test_deleted_person_cleans_exception_when_sharing_disabled():
    r = roster(); a = r.persons[0]
    snap = exempt(r, (), {(a.person_id, "mobile")})
    changed = replace(r, revision_id=uuid4(), persons=tuple(p for p in r.persons if p.person_id != a.person_id),
        memberships=tuple(m for m in r.memberships if m.person_id != a.person_id),
        enrollments=tuple(e for e in r.enrollments if e.person_id != a.person_id))
    final = apply(snap, expand_household_shared_revision(snap, record_for(r, changed)))
    assert not shared_exceptions_from_state(final.entity(HOUSEHOLD_SHARED_KIND,HOUSEHOLD_SHARED_ID))


def test_separate_exception_settings_entry_is_removed():
    from src.desktop_v2.settings_hub import SettingsAction
    assert "household_shared_exceptions" not in {action.value for action in SettingsAction}


def test_search_switches_to_visible_person_and_disables_no_results(app):
    r=roster(); first, target_person = r.persons[:2]
    stored={
        "address_columns_version":2,
        "columns":["mobile","note"],
        "exceptions":[
            {"person_id":str(first.person_id),"column":"mobile"},
            {"person_id":str(target_person.person_id),"column":"note"},
        ],
    }
    dialog=HouseholdSharedExceptionsDialog(r,stored,DAY)
    old_pid=dialog._person_id
    dialog.search.setText(str(target_person.person_id))
    assert dialog._person_id==target_person.person_id
    item=next(dialog.columns.item(i) for i in range(dialog.columns.count())
        if dialog.columns.item(i).data(Qt.ItemDataRole.UserRole)=="note")
    assert item.checkState()==Qt.CheckState.Checked
    item.setCheckState(Qt.CheckState.Unchecked)
    assert dialog._person_id is None
    assert not dialog.columns.isEnabled()
    assert not dialog.empty.isHidden()
    draft=dialog.selected_exceptions()
    item.setCheckState(Qt.CheckState.Checked)
    assert dialog.selected_exceptions()==draft
    dialog.search.clear()
    assert dialog._person_id==first.person_id
    assert dialog.columns.isEnabled()


def test_settings_empty_state_lists_no_unmarked_people_and_separates_addresses(app):
    r=roster()
    dialog=HouseholdSharedExceptionsDialog(r,None,DAY)
    assert dialog._person_id is None
    assert all(dialog.people.item(i).isHidden() for i in range(dialog.people.count()))
    assert dialog.empty.text()=="예외가 지정된 대상자가 없습니다. 명단의 공용 셀을 우클릭해 공용 예외를 지정하세요."
    assert not dialog.empty.isHidden()
    labels={dialog.columns.item(i).data(Qt.ItemDataRole.UserRole):dialog.columns.item(i).text()
        for i in range(dialog.columns.count())}
    assert labels["address"].startswith("도로명주소")
    assert labels["detail_address"].startswith("상세주소")
    assert "상세주소" not in labels["address"]
    dialog.close()


def test_settings_cannot_add_another_shared_or_unshared_exception(app):
    r=roster(); person=r.persons[0]
    pair=(person.person_id,"mobile")
    stored={
        "address_columns_version":2,
        "columns":["mobile","note"],
        "exceptions":[{"person_id":str(person.person_id),"column":"mobile"}],
    }
    dialog=HouseholdSharedExceptionsDialog(r,stored,DAY)
    items={dialog.columns.item(i).data(Qt.ItemDataRole.UserRole):dialog.columns.item(i)
        for i in range(dialog.columns.count())}
    assert not items["mobile"].isHidden()
    for key in ("note","jurisdiction"):
        assert items[key].isHidden()
        assert not items[key].flags() & Qt.ItemFlag.ItemIsEnabled
        items[key].setCheckState(Qt.CheckState.Checked)
        assert dialog.selected_exceptions()=={pair}
    dialog.close()


@pytest.mark.parametrize("source_exempt", [False, True])
def test_mixed_driver_event_keeps_nonexempt_meal_only(source_exempt):
    r=roster(); a,b,_=r.persons
    pid=a.person_id if source_exempt else b.person_id
    snap=exempt(r,("status:dinner","status:breakfast"),{(pid,"status:dinner")})
    dinner_driver,breakfast_driver=uuid4(),uuid4()
    from src.domain.delivery_routing import DRIVER_KIND, Driver, _driver_state
    entities=dict(snap.entities)
    for driver_id,name in ((dinner_driver,"Evening"),(breakfast_driver,"Morning")):
        entities[EntityRef(DRIVER_KIND,driver_id)]=_driver_state(Driver(driver_id,name))
    snap=HistoricalSnapshot(r.revision_id,entities)
    event=RoutingEvent(uuid4(),a.person_id,DAY,1,{Meal.DINNER:dinner_driver,Meal.BREAKFAST:breakfast_driver})
    change=AuditChange(ROUTING_EVENT_KIND,event.event_id,ChangeOperation.INSERT,None,_routing_event_state(event))
    result=apply(snap,expand_household_shared_revision(snap,record_for(r,replace(r,revision_id=uuid4()),extra=(change,))))
    propagated=[e for e in routing_state_from_snapshot(result).events if e.person_id==b.person_id]
    assert len(propagated)==1
    assert dict(propagated[0].driver_ids)=={Meal.BREAKFAST:breakfast_driver}


@pytest.mark.parametrize("fail", [False, True])
def test_exception_save_keeps_ui_responsive_and_errors_on_gui_thread(app, tmp_path, monkeypatch, fail):
    from threading import Event, get_ident
    from PySide6.QtCore import QTimer
    from src.desktop_v2.startup_loading import StartupLoadingDialog

    # Exercise the visible-dialog/thread contract; delay policy has separate tests.
    monkeypatch.setattr("src.desktop_v2.startup_loading.PROGRESS_DELAY_MS", 0)

    r = roster()
    pair = (r.persons[0].person_id, "mobile")
    path = tmp_path / "responsive.sqlite3"
    initial = RevisionRecord(RevisionMetadata(r.revision_id, None, NOW, None, ACTOR, "initial", RevisionSource.IMPORT), roster_audit_changes(None, r))
    ui_thread = get_ident()
    entered, released = Event(), Event()
    messages, calls, heartbeat = [], [], []
    with DatabaseSession.create(path, "password") as session:
        session.append_revision(initial)
        current = session.database.current_roster()
        snapshot = session.database.snapshot(current.revision_id)
        rows = household_shared_rows(current, "mobile", DAY, snapshot=snapshot)
        record = build_household_shared_revision(
            current, None, "mobile", True,
            {row.household_id: ("공용 연락처",) for row in rows},
            actor=ACTOR, recorded_at=NOW, effective_on=DAY, revision_id=uuid4(), snapshot=snapshot,
        )
        session.append_revision(record)
        before = session.database.current_roster().revision_id
        original = session.append_revision

        def append(record):
            calls.append(get_ident())
            entered.set()
            assert released.wait(5), "GUI timer did not continue during the save"
            if fail:
                raise OSError("write unavailable")
            return original(record)

        monkeypatch.setattr(session, "append_revision", append)
        def report(*args):
            messages.append((get_ident(), args))
            assert not isinstance(app.activeModalWidget(), StartupLoadingDialog)

        controller = HouseholdSharedExceptionsController(
            session, message_box=SimpleNamespace(critical=report, warning=report),
            actor_provider=lambda: ACTOR, recorded_at_provider=lambda: NOW,
        )
        def tick():
            if entered.is_set() and not released.is_set():
                dialog = app.activeModalWidget()
                heartbeat.append(isinstance(dialog, StartupLoadingDialog) and dialog.isVisible())
                released.set()
        timer = QTimer()
        timer.timeout.connect(tick)
        timer.start(10)
        try:
            result = controller.set_exception(*pair, True, DAY)
        finally:
            timer.stop()
            released.set()
        assert calls and all(thread != ui_thread for thread in calls)
        assert heartbeat == [True]
        assert bool(messages) == fail
        assert all(thread == ui_thread for thread, _ in messages)
        if fail:
            assert result.roster is None
            assert session.database.current_roster().revision_id == before
        else:
            assert result.roster.revision_id != before
            assert controller.set_exception(*pair, True, DAY).roster is None
            assert len(calls) == 1
    with DatabaseSession.open(path, "password") as session:
        current = session.database.current_roster()
        stored = session.database.snapshot(current.revision_id).entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
        assert (pair in shared_exceptions_from_state(stored)) != fail
