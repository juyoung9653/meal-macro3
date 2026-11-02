"""Column-based grouping across settings, edits, imports and rollback."""
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox

from src.domain.household_grouping import GROUPING_ID, GROUPING_KIND, build_grouping_revision, grouping_columns, profile_grouping_key
from src.domain.mobile_household_merge import MobileHouseholdConflict, prepare_mobile_household_revision, with_mobile_shared_values
from src.domain.profile_edits import build_person_profile_revision
from src.domain.roster import CustomFieldDefinition, CustomFieldType, PersonProfile
from src.services.revision_restore import restore_revision
from src.storage.database_session import DatabaseSession
from tests.test_mobile_household_merge import setup, household, DAY, NOW, ACTOR, TARGET, SOURCE


def setting(session, columns):
    roster = session.database.current_roster()
    return build_grouping_revision(session.database.snapshot(roster.revision_id), roster, columns, DAY,
                                   actor=ACTOR, recorded_at=NOW, revision_id=uuid4())


def test_apply_default_merges_existing_equal_numbers_and_restore_keeps_original(tmp_path):
    with DatabaseSession.create(tmp_path/'grouping.sqlite3', 'password') as session:
        before = setup(session, keys=(), same=True, future=True)
        session.append_revision(setting(session, ('mobile',)))
        after = session.database.current_roster()
        assert household(after, 10) == household(after, 11)
        assert household(after, 11, DAY-timedelta(days=1)) == SOURCE
        assert household(after, 11, DAY+timedelta(days=3)) == SOURCE
        assert grouping_columns(session.database.snapshot(after.revision_id)) == ('mobile',)
        restore_revision(session, before.revision_id, after.revision_id, ACTOR)
        assert household(session.database.current_roster(), 11) == SOURCE
        assert session.database.snapshot(session.database.latest_revision_id()).entity(GROUPING_KIND, GROUPING_ID) is None
        session.database.check_integrity()


def test_all_selected_columns_must_match_then_edit_merges_and_reopen_keeps_rule(tmp_path):
    path = tmp_path/'grouping.sqlite3'
    with DatabaseSession.create(path, 'password') as session:
        setup(session, keys=(), same=True, conflict=True)
        session.append_revision(setting(session, ('mobile', 'address')))
        before = session.database.current_roster()
        assert household(before, 11) == SOURCE
        person = before.persons[1]
        record = build_person_profile_revision(before, person.person_id, replace(person.profile, road_address='주소 A'),
            actor=ACTOR, recorded_at=NOW, effective_on=DAY, revision_id=uuid4()).record
        session.append_revision(record)
        assert household(session.database.current_roster(), 10) == household(session.database.current_roster(), 11)
    with DatabaseSession.open(path, 'password') as session:
        assert grouping_columns(session.database.snapshot(session.database.latest_revision_id())) == ('mobile', 'address')
        session.database.check_integrity()


def test_telephone_can_be_selected_independently_of_mobile(tmp_path):
    with DatabaseSession.create(tmp_path/'grouping.sqlite3', 'password') as session:
        setup(session, keys=())
        session.append_revision(setting(session, ('telephone',)))
        assert household(session.database.current_roster(), 10) == household(session.database.current_roster(), 11)


def test_setting_conflict_review_and_cancel_leave_no_partial_setting(tmp_path):
    with DatabaseSession.create(tmp_path/'grouping.sqlite3', 'password') as session:
        before = setup(session, keys=('note',), same=True)
        record = setting(session, ('mobile',))
        with pytest.raises(MobileHouseholdConflict):
            session.append_revision(record)
        assert session.database.latest_revision_id() == before.revision_id
        assert session.database.snapshot(before.revision_id).entity(GROUPING_KIND, GROUPING_ID) is None
        session.append_revision(with_mobile_shared_values(record, 'note', {TARGET: ('공용 메모',)}))
        assert {p.profile.note for p in session.database.current_roster().persons} == {'공용 메모'}
        assert len(session.database.revision_summaries()) == 2


def test_custom_columns_use_values_and_blank_does_not_match():
    key = uuid4()
    person = PersonProfile('합성', mobile_phone='010-1234-5678', custom_values={key: 0})
    assert profile_grouping_key(person, ('mobile', f'custom:{key}')) == ('01012345678', '0')
    assert profile_grouping_key(replace(person, custom_values={}), (f'custom:{key}',)) == ('',)


def test_settings_dialog_default_validation_and_existing_merge_preview(tmp_path):
    from src.desktop_v2.household_grouping import HouseholdGroupingDialog, HouseholdGroupingController
    app = QApplication.instance() or QApplication([])
    with DatabaseSession.create(tmp_path/'grouping.sqlite3', 'password') as session:
        roster = setup(session, keys=(), same=True)
        snapshot = session.database.snapshot(roster.revision_id)
        dialog = HouseholdGroupingDialog(roster, snapshot, DAY)
        assert dialog.selected_columns() == ('mobile',)
        assert '2가구 → 1가구' in dialog.summary.text()
        dialog.checks['mobile'].setChecked(False)
        assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Save).isEnabled()
        dialog.close()
        class Cancel(HouseholdGroupingDialog):
            def exec(self):
                return QDialog.DialogCode.Rejected
        assert HouseholdGroupingController(session, dialog_type=Cancel).run() is None
        assert session.database.latest_revision_id() == roster.revision_id
        class Accept(HouseholdGroupingDialog):
            def exec(self):
                self.date_edit.setDate(DAY)
                return QDialog.DialogCode.Accepted
        result = HouseholdGroupingController(session, dialog_type=Accept, actor_provider=lambda: ACTOR).run()
        assert household(result, 10) == household(result, 11)


def test_explicit_criteria_prevent_mobile_only_import_grouping(tmp_path):
    import openpyxl
    from src.importing.application import IdentityResolution, ReconciliationDecision
    from src.importing.contracts import ImportMappingProfile, ProfileField
    from src.importing.households import HouseholdMatchingRule, stage_household_grouping
    from src.importing.reconciliation import stage_reconciliation
    from src.importing.revision import build_import_revision
    from src.importing.workbook import parse_workbook
    path = tmp_path/'input.xlsx'
    book = openpyxl.Workbook()
    book.active.append(('성명', '핸드폰', '주소', '식종'))
    book.active.append(('합성 첫째', '01012345678', '주소 A', '석식'))
    book.active.append(('합성 둘째', '01012345678', '주소 B', '석식'))
    book.active.append(('합성 셋째', '01012345678', '주소 A', '석식'))
    book.save(path)
    book.close()
    profile = ImportMappingProfile(uuid4(), 1, '합성', 'roster-combined-xlsx',
        {ProfileField.NAME: 'A', ProfileField.MOBILE_PHONE: 'B', ProfileField.ROAD_ADDRESS: 'C'}, combined_meals_column='D')
    preview = parse_workbook(path, profile)
    plan = stage_reconciliation(None, preview.observations, effective_on=DAY)
    households = stage_household_grouping(preview.observations, HouseholdMatchingRule(uuid4(), 1),
        effective_on=DAY, separate_only=True)
    prepared = build_import_revision(None, preview, plan,
        decisions={o.observation_id: ReconciliationDecision(o.observation_id, IdentityResolution.CREATE_NEW, uuid4()) for o in preview.observations},
        actor=ACTOR, recorded_at=NOW, revision_id=uuid4(), batch_id=uuid4(), household_plan=households,
        household_grouping_columns=('mobile', 'address'))
    result = prepared.result.snapshot
    groups = {p.profile.name: result.household_for(p.person_id, on=DAY).household_id for p in result.persons}
    assert groups['합성 첫째'] == groups['합성 셋째'] != groups['합성 둘째']


def test_format_only_mobile_edit_still_checks_existing_matches(tmp_path):
    with DatabaseSession.create(tmp_path/'grouping.sqlite3', 'password') as session:
        before = setup(session, keys=(), same=True)
        person = before.persons[1]
        record = build_person_profile_revision(before, person.person_id, replace(person.profile, mobile_phone='010-1234-5678'),
            actor=ACTOR, recorded_at=NOW, effective_on=DAY, revision_id=uuid4()).record
        session.append_revision(record)
        assert household(session.database.current_roster(), 10) == household(session.database.current_roster(), 11)


def test_unrelated_profile_edit_does_not_merge_existing_households(tmp_path):
    with DatabaseSession.create(tmp_path/'grouping.sqlite3', 'password') as session:
        before = setup(session, keys=(), same=True)
        person = before.persons[1]
        record = build_person_profile_revision(before, person.person_id, replace(person.profile, note='메모 정정'),
            actor=ACTOR, recorded_at=NOW, effective_on=DAY, revision_id=uuid4()).record
        session.append_revision(record)
        assert household(session.database.current_roster(), 11) == SOURCE


def test_import_controller_uses_saved_and_criteria_for_existing_and_new_people(tmp_path):
    from src.desktop_v2.import_controller import GuidedRosterImportController
    from src.importing.contracts import ProfileField
    from tests.test_desktop_v2_application import _selection, _wizard_type, _AcceptedReview, _Messages
    app = QApplication.instance() or QApplication([])
    selection = _selection(tmp_path)
    observation = selection.preview.observations[0]
    observation = replace(observation, profile_values={**observation.profile_values, ProfileField.ROAD_ADDRESS: '주소 B'})
    selection = replace(selection, effective_on=DAY,
        preview=replace(selection.preview, observations=(observation,)))
    _Messages.reset()
    with DatabaseSession.create(tmp_path/'controller.sqlite3', 'password') as session:
        setup(session, keys=(), same=True, conflict=True)
        session.append_revision(setting(session, ('mobile', 'address')))
        before_count = len(session.database.revision_summaries())
        result = GuidedRosterImportController(session, wizard_type=_wizard_type(selection),
            review_type=_AcceptedReview, message_box=_Messages).run()
        assert result.completed, _Messages.critical_calls
        person = next(p for p in result.roster.persons if p.profile.name == '홍길동')
        assert result.roster.household_for(person.person_id, on=DAY).household_id == SOURCE
        assert household(result.roster, 10) == TARGET
        assert len(session.database.revision_summaries()) == before_count + 1
        session.database.check_integrity()


def test_custom_column_setting_actually_merges_matching_values(tmp_path):
    from src.domain.projections import roster_audit_changes
    from src.domain.revisions import RevisionRecord, RevisionMetadata, RevisionSource
    with DatabaseSession.create(tmp_path/'custom.sqlite3', 'password') as session:
        roster = setup(session, keys=())
        definition = CustomFieldDefinition(uuid4(), '가구 코드', CustomFieldType.TEXT, 0)
        updated = replace(roster, revision_id=uuid4(), custom_fields=(definition,),
            persons=tuple(replace(p, profile=replace(p.profile, custom_values={definition.definition_id: '합성 코드'})) for p in roster.persons))
        session.append_revision(RevisionRecord(RevisionMetadata(updated.revision_id, roster.revision_id, NOW, DAY, ACTOR, '합성 칼럼', RevisionSource.UI),
            roster_audit_changes(roster, updated)))
        session.append_revision(setting(session, (f'custom:{definition.definition_id}',)))
        assert household(session.database.current_roster(), 10) == household(session.database.current_roster(), 11)
