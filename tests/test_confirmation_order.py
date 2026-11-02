"""Confirmation ordering is opt-in, stable, DB-scoped and rendered to Excel."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import openpyxl
import pytest
from PySide6.QtWidgets import QApplication, QDialog

from src.core.invoice_data import MealPrices, MonthlyDeliveryBatch
from src.desktop_v2.document_template import DocumentTemplateController, DocumentTemplateDialog
from src.domain.document_templates import (
    DOCUMENT_TEMPLATE_SETTING_ID, DOCUMENT_TEMPLATE_SETTING_KIND, DocumentTemplateProfile,
    build_document_template_revision, document_template_setting_from_state, document_template_setting_state,
)
from src.domain.revisions import RevisionActor
from src.generators.confirm_generator import make_confirm_publication
from src.storage.database_session import DatabaseSession
from tests.test_confirm_generator import _typed_delivery
from tests.test_desktop_v2_document_template import _base_revision, _Messages


@pytest.mark.parametrize('profile', ['급식', '행복도시락'])
@pytest.mark.parametrize('enabled', [False, True])
def test_workbook_stably_orders_household_blocks(tmp_path, profile, enabled):
    template = Path('assets') / profile / f'{profile}_확인서템플릿.xlsx'
    members = [('세식', 3), ('한식B', 1), ('두식', 2), ('한식A', 1)]
    deliveries = tuple(_typed_delivery(f'{name}{i}', name, name+'주소')
                       for name, count in members for i in range(1, count+1))
    batch = MonthlyDeliveryBatch(2026, 6, MealPrices(10000,10000,10000), False, deliveries)
    output = tmp_path/'confirmation.xlsx'
    created, _ = make_confirm_publication(batch, {}, output, str(template),
                                         meal_type=profile, single_meal_first=enabled)
    workbook = openpyxl.load_workbook(output)
    try:
        sheet = workbook['정자동']
        names = {name+'1' for name, _ in members}
        positions = {cell.value: cell.row for row in sheet for cell in row if cell.value in names}
        actual = sorted(positions, key=positions.get)
        assert actual == (['한식B1','한식A1','두식1','세식1'] if enabled else
                          ['세식1','한식B1','두식1','한식A1'])
        assert created[0]['total_hh'] == 4
        assert (created[0]['count_1'], created[0]['count_2'], created[0]['count_3']) == (2,1,1)
    finally:
        workbook.close()


def test_setting_only_toggle_persists_and_legacy_default_stays_off(tmp_path):
    app = QApplication.instance() or QApplication([])
    actor = RevisionActor('tester', 'test')
    with DatabaseSession.create(tmp_path/'settings.sqlite3','password') as session:
        session.append_revision(_base_revision())
        prepared = build_document_template_revision(session.database.current_roster().revision_id, None,
            DocumentTemplateProfile.SCHOOL_MEAL, actor=actor, recorded_at=datetime.now(UTC), revision_id=uuid4())
        session.append_revision(prepared.record)
        legacy = dict(document_template_setting_state(prepared.setting))
        legacy.pop('confirmation_single_meal_first')
        assert document_template_setting_from_state(legacy).confirmation_single_meal_first is False
        for enabled in (True, False):
            class Toggle(DocumentTemplateDialog):
                def exec(self):
                    assert self.confirmation_order.isChecked() is not enabled
                    self.confirmation_order.setChecked(enabled)
                    return QDialog.DialogCode.Accepted
            outcome = DocumentTemplateController(session, dialog_type=Toggle, actor_provider=lambda:actor,
                                                 message_box=_Messages).run()
            assert outcome.changed and outcome.completed
            assert outcome.setting.confirmation_single_meal_first is enabled
        assert len(session.database.revision_summaries()) == 4
    with DatabaseSession.open(tmp_path/'settings.sqlite3','password') as session:
        state = session.database.snapshot(session.database.latest_revision_id()).entity(
            DOCUMENT_TEMPLATE_SETTING_KIND,DOCUMENT_TEMPLATE_SETTING_ID)
        assert document_template_setting_from_state(state).confirmation_single_meal_first is False
        session.database.check_integrity()


def test_profile_change_preserves_confirmation_order():
    actor=RevisionActor('tester','test')
    initial=build_document_template_revision(uuid4(),None,DocumentTemplateProfile.SCHOOL_MEAL,
        actor=actor,recorded_at=datetime.now(UTC),revision_id=uuid4(),confirmation_single_meal_first=True)
    changed=build_document_template_revision(initial.record.metadata.revision_id,
        document_template_setting_state(initial.setting),DocumentTemplateProfile.HAPPY_LUNCHBOX,
        actor=actor,recorded_at=datetime.now(UTC),revision_id=uuid4())
    assert changed.setting.confirmation_single_meal_first is True
    assert changed.setting.public_manifest()['confirmation_single_meal_first'] is True


def test_generation_passes_order_option_and_records_manifest(tmp_path):
    from datetime import date
    from tests import test_desktop_v2_document_generation as fixture
    from src.desktop_v2.document_generation import DocumentGenerationController
    from src.desktop_v2.document_hub import DocumentAction
    from src.desktop_v2.document_period import DocumentPeriodSelection
    from src.desktop_v2.document_meal import DocumentMealSelection
    from src.desktop_v2.document_output import DocumentOutputMode, next_available_document_output
    from src.domain.roster import Meal
    from src.domain.documents import DocumentKind
    with DatabaseSession.create(tmp_path/'generation.sqlite3','password') as session:
        session.append_revision(fixture._initial_revision())
        source=session.database.revision_summaries()[0]
        snap=session.database.snapshot(source.revision_id)
        current=document_template_setting_from_state(snap.entity(DOCUMENT_TEMPLATE_SETTING_KIND,DOCUMENT_TEMPLATE_SETTING_ID))
        prepared=build_document_template_revision(source.revision_id,document_template_setting_state(current),current.profile,
            actor=RevisionActor('tester','test'),recorded_at=datetime.now(UTC),revision_id=uuid4(),confirmation_single_meal_first=True)
        session.append_revision(prepared.record)
        period=DocumentPeriodSelection(DocumentAction.CONFIRMATION,source,date(2026,6,17),Meal.LUNCH,date(2026,6,1),date(2026,6,30))
        selection=next_available_document_output(DocumentMealSelection(period,(Meal.LUNCH,)),tmp_path,DocumentOutputMode.SAVED_DEFAULT)
        calls=[]
        def writer(*args,**kwargs):
            calls.append(kwargs['single_meal_first'])
            return make_confirm_publication(*args,**kwargs)
        outcome=DocumentGenerationController(session,message_box=fixture._Messages,confirmation_writer=writer).run(selection)
        assert outcome.completed and calls==[True]
        runs=session.database.document_runs()
        assert runs[-1].kind is DocumentKind.CONFIRMATION
        assert runs[-1].template_manifest['single_meal_first'] is True
