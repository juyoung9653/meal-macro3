"""Priority mappings persist and render actual invoice/confirmation workbooks."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from dataclasses import replace
from datetime import UTC, datetime, date
from io import BytesIO
from uuid import uuid4
import openpyxl
import msoffcrypto
import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
from src.core.models import FULL_ADDRESS_FIELD
from src.core.settings import get_settings
from src.core.delivery_matrix import RosterEntry
from src.core.invoice_data import MealPrices, MonthlyDeliveryBatch
from src.domain.document_column_mappings import (
    MAPPING_ENTITY_KIND, build_mapping_revision, mapping_entity_id,
    mapping_setting_from_state, load_mapping_fallbacks)
from src.domain.revisions import HistoricalSnapshot, EntityRef, RevisionActor
from src.generators.invoice_generator import make_invoice_publication
from src.generators.confirm_generator import make_confirm_publication
from src.services.document_column_mappings import invoice_default_mapping, confirmation_default_mapping, effective_fallbacks
from src.templates.config import read_template_columns
from src.templates.template_scanner import scan_placeholders
from tests.test_confirm_generator import _roster_person


class _ExpectedErrors:
    errors = []
    @classmethod
    def critical(cls, *args): cls.errors.append(args)
    @staticmethod
    def warning(*args): pass



@pytest.mark.parametrize("profile",["급식","행복도시락"])
@pytest.mark.parametrize("kind",["invoice","confirmation"])
@pytest.mark.parametrize("primary,secondary,expected",[("first","second","first"),(None,"second","second"),("  ","second","second"),("",None,None),(0,"second",0),(False,"second",False),(None,0,0),(None,False,False),("  ",FULL_ADDRESS_FIELD,"Road Unit"),("PRIMARY_FULL","second","Road Unit")])
def test_actual_workbooks_choose_first_nonblank_per_recipient(tmp_path,profile,kind,primary,secondary,expected):
    primary_key=FULL_ADDRESS_FIELD if primary=="PRIMARY_FULL" else "custom:primary";secondary_key="custom:secondary"
    person=_roster_person("Synthetic","Road",detail_address="Unit")
    extra={**person.roster.extra,primary_key:primary,secondary_key:secondary}
    person=replace(person,roster=RosterEntry(2,extra))
    batch=MonthlyDeliveryBatch(2026,6,MealPrices(10000,10000,10000),False,(person,))
    settings=get_settings();out=tmp_path/"priority.xlsx"
    fallback=FULL_ADDRESS_FIELD if secondary==FULL_ADDRESS_FIELD else secondary_key
    if kind=="invoice":
        template=settings.get_invoice_template_path(profile)
        mapping=invoice_default_mapping(template,profile,False)
        mapping["성명"]=primary_key
        make_invoice_publication(batch,mapping,str(out),str(template),meal_type=profile,column_fallbacks={"성명":fallback})
        original_sheet="청구서" if profile=="급식" else "학기_총괄"
        col=read_template_columns(str(template),sheet_name=original_sheet).index("성명")+1
        workbook=openpyxl.load_workbook(out)
        actual=workbook["청구서" if profile=="급식" else "총괄"].cell(4,col).value
        assert actual==expected
        if expected is not None:
            assert type(actual) is type(expected)
    else:
        template=settings.get_confirm_template_path(profile)
        mapping=confirmation_default_mapping()
        mapping.update({"성명":primary_key,"세대주 성명":primary_key,"핸드폰":primary_key})
        make_confirm_publication(batch,mapping,out,str(template),meal_type=profile,
            column_fallbacks={"성명":fallback,"세대주 성명":fallback,"핸드폰":fallback})
        workbook=openpyxl.load_workbook(out)
        placeholders=scan_placeholders(str(template),["1명"])["1명"]
        rendered=None if expected is None else str(expected)
        for placeholder in ("child1","carer1","phone1"):
            row,col=placeholders[placeholder][0]
            assert workbook["정자동"].cell(row,col).value==rendered
    workbook.close()


def test_confirmation_aliases_use_same_priority_rule(tmp_path):
    person=_roster_person("Synthetic","Road")
    person=replace(person,roster=RosterEntry(2,{**person.roster.extra,"missing":" ","second":"Fallback"}))
    batch=MonthlyDeliveryBatch(2026,6,MealPrices(1,1,1),False,(person,))
    template=get_settings().get_confirm_template_path("급식");out=tmp_path/"aliases.xlsx"
    make_confirm_publication(batch,{"이름":"missing","세대주성명":"missing","전화번호":"missing"},out,str(template),
        column_fallbacks={"이름":"second","세대주성명":"second","전화번호":"second"})
    workbook=openpyxl.load_workbook(out);ph=scan_placeholders(str(template),["1명"])["1명"]
    for key in ("child1","carer1","phone1"):
        assert workbook["정자동"].cell(*ph[key][0]).value=="Fallback"
    workbook.close()


def test_legacy_read_noop_secondary_only_revision_and_reset():
    revision=uuid4();actor=RevisionActor("tester","sid");now=datetime.now(UTC)
    state={"policy_version":1,"version":1,"profile":"급식","document_kind":"invoice","overrides":{},"changed_in_revision_id":str(revision)}
    ref=EntityRef(MAPPING_ENTITY_KIND,mapping_entity_id("급식","invoice"));snap=HistoricalSnapshot(revision,{ref:state})
    assert mapping_setting_from_state(state).fallbacks=={}
    options=dict(defaults={"Name":"first"},allowed_sources=("","first","second"),actor=actor,recorded_at=now)
    assert build_mapping_revision(revision,snap,"급식","invoice",{"Name":"first"},revision_id=uuid4(),**options) is None
    changed=build_mapping_revision(revision,snap,"급식","invoice",{"Name":"first"},desired_fallbacks={"Name":"second"},revision_id=uuid4(),**options)
    assert changed.setting.overrides=={} and changed.setting.fallbacks=={"Name":"second"}
    assert changed.setting.public_manifest()["fallbacks"]=={"Name":"second"}
    assert changed.record.changes[0].after["policy_version"]==2
    assert state["policy_version"]==1 and "fallbacks" not in state
    saved=HistoricalSnapshot(changed.record.metadata.revision_id,{ref:changed.record.changes[0].after})
    assert load_mapping_fallbacks(saved,"급식","invoice")=={"Name":"second"}
    assert load_mapping_fallbacks(saved,"행복도시락","invoice")=={}
    reset=build_mapping_revision(saved.revision_id,saved,"급식","invoice",{"Name":"first"},desired_fallbacks={},revision_id=uuid4(),**options)
    assert reset.setting.fallbacks=={}
    for invalid in ({"Name":"unknown"},{"locked":"first"}):
        with pytest.raises(ValueError):
            build_mapping_revision(revision,snap,"급식","invoice",{"Name":"first"},desired_fallbacks=invalid,revision_id=uuid4(),**options)


def test_historical_secondary_source_retained_but_new_unknown_rejected():
    revision=uuid4();ref=EntityRef(MAPPING_ENTITY_KIND,mapping_entity_id("급식","invoice"))
    state={"policy_version":2,"version":1,"profile":"급식","document_kind":"invoice","overrides":{},"fallbacks":{"Name":"retired"},"changed_in_revision_id":str(revision)}
    snap=HistoricalSnapshot(revision,{ref:state})
    options=dict(defaults={"Name":"first"},allowed_sources=("","first"),actor=RevisionActor("tester","sid"),recorded_at=datetime.now(UTC),revision_id=uuid4())
    assert build_mapping_revision(revision,snap,"급식","invoice",{"Name":"first"},desired_fallbacks={"Name":"retired"},**options) is None
    with pytest.raises(ValueError):
        build_mapping_revision(revision,snap,"급식","invoice",{"Name":"first"},desired_fallbacks={"Name":"new-unknown"},**options)
    assert effective_fallbacks({"__calc_total":"total"},{"total":"first","Name":"first","연번":"first"},document_kind="invoice")=={"Name":"first"}
    assert effective_fallbacks({}, {"도로명주소":"first","성명":"first"},document_kind="confirmation")=={"성명":"first"}


def test_editor_secondary_only_dirty_switch_reset_cancel():
    from tests.test_desktop_v2_document_column_mapping import _dialog, _SwitchMessages
    app=QApplication.instance() or QApplication([])
    _SwitchMessages.calls=0;_SwitchMessages.answer=QMessageBox.StandardButton.Cancel
    dialog=_dialog(_SwitchMessages)
    combo=dialog._fallback_combos["성명"];combo.setCurrentIndex(combo.findData("학교명"))
    assert dialog.desired_fallbacks()=={"성명":"학교명"}
    assert dialog._dirty()
    dialog._document_combo.setCurrentIndex(1)
    assert _SwitchMessages.calls==1 and dialog.document_kind=="invoice"
    dialog._reset_scope()
    assert not dialog.desired_fallbacks() and not dialog._dirty()
    dialog.reject()


def test_saved_fallback_used_with_historical_source_and_manifest(tmp_path):
    from tests.test_desktop_v2_document_generation import (_initial_revision,_Messages,REVISION_ID,
        DocumentPeriodSelection,DocumentAction,Meal,DocumentMealSelection,next_available_document_output,DocumentOutputMode,DocumentKind)
    from src.services.document_column_mappings import build_mapping_revision as build
    from src.storage.database_session import DatabaseSession
    from src.desktop_v2.document_generation import DocumentGenerationController
    out=tmp_path/"out";out.mkdir();path=tmp_path/"data.sqlite3"
    with DatabaseSession.create(path,"password") as session:
        session.append_revision(_initial_revision());source=session.database.revision_summaries()[0]
        setting=build(REVISION_ID,session.database.snapshot(REVISION_ID),"급식","invoice",{"성명":"전화번호"},desired_fallbacks={"성명":"학교명"},actor=RevisionActor("tester","sid"),recorded_at=datetime.now(UTC),revision_id=uuid4())
        session.append_revision(setting.record)
        period=DocumentPeriodSelection(DocumentAction.INVOICE,source,date(2026,6,17),Meal.LUNCH,date(2026,6,1),date(2026,6,30))
        selection=next_available_document_output(DocumentMealSelection(period,(Meal.LUNCH,)),out,DocumentOutputMode.SAVED_DEFAULT)
        result=DocumentGenerationController(session,message_box=_ExpectedErrors).run(selection)
        assert result.completed
        manifest=session.database.document_runs(kind=DocumentKind.INVOICE)[0].template_manifest
        assert manifest["column_fallbacks"]=={"성명":"학교명"}
        assert manifest["column_mapping_setting"]["fallbacks"]=={"성명":"학교명"}
        with selection.output_path.open("rb") as file:
            encrypted=msoffcrypto.OfficeFile(file);encrypted.load_key(password="xlsx-secret");buffer=BytesIO();encrypted.decrypt(buffer)
        buffer.seek(0);workbook=openpyxl.load_workbook(buffer)
        col=read_template_columns("assets/급식/급식_청구서템플릿.xlsx",sheet_name="청구서").index("성명")+1
        assert workbook["청구서"].cell(4,col).value=="푸른초"
        workbook.close();buffer.close()
    with DatabaseSession.open(path,"password") as session:
        current=session.database.current_roster()
        assert load_mapping_fallbacks(session.database.snapshot(current.revision_id),"급식","invoice")=={"성명":"학교명"}


@pytest.mark.parametrize("priority",[1,2])
def test_generation_rejects_deleted_custom_source_in_either_priority(tmp_path,priority):
    from tests.test_desktop_v2_document_generation import (_initial_revision,_Messages,REVISION_ID,
        DocumentPeriodSelection,DocumentAction,Meal,DocumentMealSelection,next_available_document_output,DocumentOutputMode)
    from src.storage.database_session import DatabaseSession
    from src.desktop_v2.document_generation import DocumentGenerationController
    out=tmp_path/"out";out.mkdir();missing="custom:"+str(uuid4())
    with DatabaseSession.create(tmp_path/"data.sqlite3","password") as session:
        session.append_revision(_initial_revision());source=session.database.revision_summaries()[0]
        setting=build_mapping_revision(REVISION_ID,session.database.snapshot(REVISION_ID),"급식","invoice",
            {"성명":missing if priority==1 else "성명"},desired_fallbacks={"성명":missing} if priority==2 else {},
            defaults={"성명":"성명"},allowed_sources=("성명",missing),actor=RevisionActor("tester","sid"),recorded_at=datetime.now(UTC),revision_id=uuid4())
        session.append_revision(setting.record)
        period=DocumentPeriodSelection(DocumentAction.INVOICE,source,date(2026,6,17),Meal.LUNCH,date(2026,6,1),date(2026,6,30))
        selection=next_available_document_output(DocumentMealSelection(period,(Meal.LUNCH,)),out,DocumentOutputMode.SAVED_DEFAULT)
        def forbidden(*args,**kwargs): pytest.fail("invalid source reached workbook writer")
        result=DocumentGenerationController(session,message_box=_ExpectedErrors,invoice_writer=forbidden).run(selection)
        assert not result.completed and not selection.output_path.exists()


def test_controller_secondary_only_save_noop_cancel_and_failed_save(tmp_path,monkeypatch):
    from tests.test_desktop_v2_document_column_mapping import _base_revision,_Messages
    from src.desktop_v2.document_column_mapping import DocumentColumnMappingController,DocumentColumnMappingStatus
    from src.storage.database_session import DatabaseSession
    from PySide6.QtWidgets import QDialog
    mode=["save"]
    class Dialog:
        selected_profile="급식";document_kind="confirmation"
        def __init__(self,*args,**kwargs): pass
        def exec(self): return QDialog.DialogCode.Rejected if mode[0]=="cancel" else QDialog.DialogCode.Accepted
        def desired_mapping(self): return {"성명":"성명","세대주 성명":"세대주 성명","핸드폰":"핸드폰"}
        def desired_fallbacks(self): return {"성명":"학교명" if mode[0]!="fail" else "학년"}
    app=QApplication.instance() or QApplication([])
    with DatabaseSession.create(tmp_path/"data.sqlite3","password") as session:
        session.append_revision(_base_revision())
        def run(): return DocumentColumnMappingController(session,dialog_type=Dialog,message_box=_ExpectedErrors,actor_provider=lambda:RevisionActor("tester","sid")).run()
        saved=run();assert saved.completed and saved.changed
        revision=session.database.current_roster().revision_id
        assert load_mapping_fallbacks(session.database.snapshot(revision),"급식","confirmation")=={"성명":"학교명"}
        assert not run().changed
        mode[0]="cancel";assert run().status is DocumentColumnMappingStatus.CANCELLED
        mode[0]="fail"
        monkeypatch.setattr(session,"append_revision",lambda record:(_ for _ in ()).throw(RuntimeError("write failure")))
        assert run().status is DocumentColumnMappingStatus.FAILED
        assert session.database.current_roster().revision_id==revision


@pytest.mark.parametrize("value",[0,False])
def test_invoice_memo_keeps_valid_zero_and_false(tmp_path,value):
    person=_roster_person("Synthetic","Road")
    person=replace(person,roster=RosterEntry(2,{**person.roster.extra,"primary":value,"secondary":"wrong"}))
    batch=MonthlyDeliveryBatch(2026,6,MealPrices(1,1,1),False,(person,))
    template=get_settings().get_invoice_template_path("급식");out=tmp_path/"memo.xlsx"
    target="평일중식중지=>초1-2,고3"
    mapping=invoice_default_mapping(template,"급식",False);mapping[target]="primary"
    make_invoice_publication(batch,mapping,str(out),str(template),column_fallbacks={target:"secondary"})
    workbook=openpyxl.load_workbook(out);col=read_template_columns(str(template),sheet_name="청구서").index(target)+1
    assert workbook["청구서"].cell(4,col).value==str(value)
    workbook.close()


def test_fallback_rejects_explicitly_locked_target_and_bad_state():
    revision=uuid4();snap=HistoricalSnapshot(revision,{})
    with pytest.raises(ValueError,match="편집"):
        build_mapping_revision(revision,snap,"급식","invoice",{},desired_fallbacks={"locked":"first"},
            defaults={"locked":"first"},allowed_sources=("first",),locked_targets=("locked",),
            actor=RevisionActor("tester","sid"),recorded_at=datetime.now(UTC),revision_id=uuid4())
    state={"policy_version":2,"version":1,"profile":"급식","document_kind":"invoice","overrides":{},"fallbacks":[],"changed_in_revision_id":str(revision)}
    with pytest.raises(ValueError): mapping_setting_from_state(state)


def test_invoice_explicitly_empty_priorities_clear_template_sample(tmp_path):
    template=tmp_path/"template.xlsx"
    workbook=openpyxl.load_workbook(get_settings().get_invoice_template_path("급식"))
    columns=read_template_columns(get_settings().get_invoice_template_path("급식"),sheet_name="청구서")
    col=columns.index("성명")+1;workbook["청구서"].cell(4,col).value="Sample must clear";workbook.save(template);workbook.close()
    person=_roster_person("Synthetic","Road");batch=MonthlyDeliveryBatch(2026,6,MealPrices(1,1,1),False,(person,))
    output=tmp_path/"blank.xlsx"
    make_invoice_publication(batch,{"성명":""},str(output),str(template),column_fallbacks={})
    workbook=openpyxl.load_workbook(output)
    assert workbook["청구서"].cell(4,col).value is None
    workbook.close()
