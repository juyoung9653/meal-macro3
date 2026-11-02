"""Weekend date display persists independently of meal counts and ordering."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.document_template import DocumentTemplateController, DocumentTemplateDialog
from src.domain.document_templates import (
    DOCUMENT_TEMPLATE_SETTING_ID, DOCUMENT_TEMPLATE_SETTING_KIND, DocumentTemplateProfile,
    build_document_template_revision, document_template_setting_from_state, document_template_setting_state,
)
from src.domain.revisions import RevisionActor
from src.generators.confirm_generator import make_confirm_publication
from src.storage.database_session import DatabaseSession
from tests.test_desktop_v2_document_template import _base_revision, _Messages


def test_setting_only_toggle_persists_and_legacy_default_stays_off(tmp_path):
    app = QApplication.instance() or QApplication([])
    actor = RevisionActor('tester', 'test')
    with DatabaseSession.create(tmp_path/'settings.sqlite3','password') as session:
        session.append_revision(_base_revision())
        prepared = build_document_template_revision(session.database.current_roster().revision_id, None,
            DocumentTemplateProfile.SCHOOL_MEAL, actor=actor, recorded_at=datetime.now(UTC), revision_id=uuid4())
        session.append_revision(prepared.record)
        legacy = dict(document_template_setting_state(prepared.setting))
        legacy.pop('confirmation_include_next_month_weekend')
        assert document_template_setting_from_state(legacy).confirmation_include_next_month_weekend is False
        for enabled in (True, False):
            class Toggle(DocumentTemplateDialog):
                def exec(self):
                    assert self.confirmation_next_weekend.isChecked() is not enabled
                    self.confirmation_next_weekend.setChecked(enabled)
                    return QDialog.DialogCode.Accepted
            outcome = DocumentTemplateController(session, dialog_type=Toggle, actor_provider=lambda:actor,
                                                 message_box=_Messages).run()
            assert outcome.changed and outcome.completed
            assert outcome.setting.confirmation_include_next_month_weekend is enabled
        class Cancel(DocumentTemplateDialog):
            def exec(self):
                self.confirmation_next_weekend.setChecked(True)
                return QDialog.DialogCode.Rejected
        cancelled = DocumentTemplateController(session, dialog_type=Cancel, message_box=_Messages).run()
        assert not cancelled.completed
        class Unchanged(DocumentTemplateDialog):
            def exec(self):
                return QDialog.DialogCode.Accepted
        unchanged = DocumentTemplateController(session, dialog_type=Unchanged, message_box=_Messages).run()
        assert unchanged.completed and not unchanged.changed
        assert len(session.database.revision_summaries()) == 4
    with DatabaseSession.open(tmp_path/'settings.sqlite3','password') as session:
        state = session.database.snapshot(session.database.latest_revision_id()).entity(
            DOCUMENT_TEMPLATE_SETTING_KIND,DOCUMENT_TEMPLATE_SETTING_ID)
        assert document_template_setting_from_state(state).confirmation_include_next_month_weekend is False
        session.database.check_integrity()


def test_profile_change_preserves_weekend_display():
    actor=RevisionActor('tester','test')
    initial=build_document_template_revision(uuid4(),None,DocumentTemplateProfile.SCHOOL_MEAL,
        actor=actor,recorded_at=datetime.now(UTC),revision_id=uuid4(),confirmation_include_next_month_weekend=True)
    changed=build_document_template_revision(initial.record.metadata.revision_id,
        document_template_setting_state(initial.setting),DocumentTemplateProfile.HAPPY_LUNCHBOX,
        actor=actor,recorded_at=datetime.now(UTC),revision_id=uuid4())
    assert changed.setting.confirmation_include_next_month_weekend is True
    assert changed.setting.public_manifest()['confirmation_include_next_month_weekend'] is True


def test_generation_passes_weekend_option_and_records_manifest(tmp_path):
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
            actor=RevisionActor('tester','test'),recorded_at=datetime.now(UTC),revision_id=uuid4(),confirmation_include_next_month_weekend=True)
        session.append_revision(prepared.record)
        period=DocumentPeriodSelection(DocumentAction.CONFIRMATION,source,date(2026,6,17),Meal.LUNCH,date(2026,6,1),date(2026,6,30))
        selection=next_available_document_output(DocumentMealSelection(period,(Meal.LUNCH,)),tmp_path,DocumentOutputMode.SAVED_DEFAULT)
        calls=[]
        def writer(*args,**kwargs):
            calls.append(kwargs['include_next_month_weekend'])
            return make_confirm_publication(*args,**kwargs)
        outcome=DocumentGenerationController(session,message_box=fixture._Messages,confirmation_writer=writer).run(selection)
        assert outcome.completed and calls==[True]
        runs=session.database.document_runs()
        assert runs[-1].kind is DocumentKind.CONFIRMATION
        assert runs[-1].template_manifest['include_next_month_weekend'] is True

@pytest.mark.parametrize("year,month,expected", [
    (2026, 10, "10.26~11.01"),
    (2026, 7, "07.27~08.02"),
    (2027, 12, "12.27~01.02"),
    (2022, 12, "12.26~01.01"),
    (2026, 1, "01.26~02.01"),
    (2026, 2, "02.23~03.01"),
    (2026, 6, "06.29~06.30"),
    (2026, 5, "05.25~05.31"),
    (2026, 3, "03.30~03.31"),
])
def test_date_display_only_extends_month_ending_friday_or_saturday(year, month, expected):
    from src.core.date_utils import get_week_ranges, week_date_str
    from src.core.invoice_data import WeeklyDelivery
    from src.generators.confirm_generator import _format_week_range
    ranges = get_week_ranges(year, month)
    for number, start, end in ranges:
        item = WeeklyDelivery(number, start, end, 3, 0, 0)
        assert _format_week_range(item) == week_date_str(start, end)
        assert _format_week_range(item, include_next_month_weekend=True) == (
            expected if number == len(ranges) else week_date_str(start, end)
        )
