from __future__ import annotations

import os
from io import BytesIO
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import msoffcrypto
import openpyxl
import pytest

from src.desktop_v2.document_hub import DocumentAction
from src.desktop_v2.document_generation import DocumentGenerationController
from src.desktop_v2.document_meal import DocumentMealSelection
from src.desktop_v2.document_output import (
    DocumentOutputMode,
    next_available_document_output,
)
from src.desktop_v2.document_period import DocumentPeriodSelection
from src.domain.revisions import (
    AuditChange,
    ChangeOperation,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.domain.document_templates import (
    DOCUMENT_TEMPLATE_SETTING_ID,
    DOCUMENT_TEMPLATE_SETTING_KIND,
    DocumentTemplateProfile,
    DocumentTemplateSetting,
    document_template_setting_state,
)
from src.domain.export_security import (
    EXPORT_PASSWORD_SETTING_ID,
    EXPORT_PASSWORD_SETTING_KIND,
    ExportPasswordSetting,
    export_password_setting_state,
)
from src.domain.documents import DocumentKind
from src.domain.roster import (
    DeliveryAssignment,
    Household,
    HouseholdMembership,
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.projections import roster_audit_changes
from src.domain.entitlements import LunchDayRule, SchoolLunchMark
from src.domain.school_calendar import (
    SCHOOL_LUNCH_SCHEDULE_KIND,
    SchoolLunchDay,
    SchoolLunchSchoolSchedule,
    school_lunch_school_schedule_state,
)
from src.storage.database_session import DatabaseSession
from src.templates.config import read_template_columns
from src.templates.template_scanner import scan_placeholders
from src.services.document_column_mappings import (
    build_mapping_revision,
    load_mapping_overrides,
    mapping_editor_spec,
)


REVISION_ID = UUID("6d000000-0000-0000-0000-000000000001")
PERSON_ID = UUID("6d000000-0000-0000-0000-000000000002")
HOUSEHOLD_ID = UUID("6d000000-0000-0000-0000-000000000003")


def _days(starts_on: date, ends_on: date):
    current = starts_on
    while current <= ends_on:
        yield current
        current += timedelta(days=1)


def _initial_revision(
    profile: DocumentTemplateProfile = DocumentTemplateProfile.SCHOOL_MEAL,
) -> RevisionRecord:
    roster = RosterSnapshot(
        REVISION_ID,
        persons=(
            Person(
                PERSON_ID,
                PersonProfile(
                    "홍길동",
                    birth_date=date(2015, 3, 2),
                    school_type="초등학생",
                    school_name="푸른초",
                    grade="6학년",
                    household_head_name="홍보호",
                    relationship_to_child="자녀",
                    jurisdiction="정자동",
                    road_address="성남대로 1",
                    detail_address="101호",
                    mobile_phone="010-1111-1111",
                    provider="센터",
                    note="알레르기 확인",
                ),
            ),
        ),
        households=(Household(HOUSEHOLD_ID),),
        memberships=(
            HouseholdMembership(
                PERSON_ID,
                HOUSEHOLD_ID,
                InclusiveDateRange(date(2026, 1, 1), None),
            ),
        ),
        enrollments=(
            MealEnrollment(
                PERSON_ID,
                Meal.LUNCH,
                InclusiveDateRange(date(2026, 1, 1), None),
            ),
        ),
        assignments=(
            DeliveryAssignment(
                PERSON_ID,
                None,
                "김기사",
                "1",
            ),
        ),
    )
    schedule = SchoolLunchSchoolSchedule(
        "푸른초",
        (InclusiveDateRange(date(2026, 6, 1), date(2026, 6, 30)),),
        tuple(
            SchoolLunchDay(day, LunchDayRule(SchoolLunchMark.VENDOR_REQUIRED))
            for day in _days(date(2026, 6, 1), date(2026, 6, 30))
            if day.weekday() < 5
        ),
    )
    password = ExportPasswordSetting(1, "xlsx-secret", REVISION_ID)
    template = DocumentTemplateSetting(
        1,
        profile,
        REVISION_ID,
    )
    changes = list(roster_audit_changes(None, roster))
    changes.extend(
        (
            AuditChange(
                SCHOOL_LUNCH_SCHEDULE_KIND,
                schedule.entity_id,
                ChangeOperation.INSERT,
                before=None,
                after=school_lunch_school_schedule_state(schedule),
            ),
            AuditChange(
                EXPORT_PASSWORD_SETTING_KIND,
                EXPORT_PASSWORD_SETTING_ID,
                ChangeOperation.INSERT,
                before=None,
                after=export_password_setting_state(password),
            ),
            AuditChange(
                DOCUMENT_TEMPLATE_SETTING_KIND,
                DOCUMENT_TEMPLATE_SETTING_ID,
                ChangeOperation.INSERT,
                before=None,
                after=document_template_setting_state(template),
            ),
        )
    )
    return RevisionRecord(
        RevisionMetadata(
            REVISION_ID,
            None,
            datetime(2026, 6, 30, 1, 0, tzinfo=UTC),
            date(2026, 6, 1),
            RevisionActor("tester", "S-1-5-21-test"),
            "문서 생성 통합 기준",
            RevisionSource.UI,
        ),
        tuple(changes),
    )


class _Messages:
    @staticmethod
    def critical(*args):
        raise AssertionError(args)

    @staticmethod
    def warning(*args):
        raise AssertionError(args)

    @staticmethod
    def information(*args):
        raise AssertionError(args)


@pytest.mark.parametrize(
    ("action", "kind", "profile"),
    (
        (
            DocumentAction.INVOICE,
            DocumentKind.INVOICE,
            DocumentTemplateProfile.SCHOOL_MEAL,
        ),
        (
            DocumentAction.CONFIRMATION,
            DocumentKind.CONFIRMATION,
            DocumentTemplateProfile.SCHOOL_MEAL,
        ),
        (
            DocumentAction.INVOICE,
            DocumentKind.INVOICE,
            DocumentTemplateProfile.HAPPY_LUNCHBOX,
        ),
        (
            DocumentAction.CONFIRMATION,
            DocumentKind.CONFIRMATION,
            DocumentTemplateProfile.HAPPY_LUNCHBOX,
        ),
    ),
)
def test_publishes_encrypted_monthly_document_records_run_and_backup(
    tmp_path: Path,
    action: DocumentAction,
    kind: DocumentKind,
    profile: DocumentTemplateProfile,
):
    key = f"{action.value}-{profile.value}"
    backups = tmp_path / f"backups-{key}"
    outputs = tmp_path / f"outputs-{key}"
    backups.mkdir()
    outputs.mkdir()
    database_path = tmp_path / f"{key}.sqlite3"
    with DatabaseSession.create(
        database_path,
        "everyday-password",
        "institution-recovery",
        backup_directory=backups,
    ) as session:
        session.append_revision(_initial_revision(profile))
        source = session.database.revision_summaries()[0]
        period = DocumentPeriodSelection(
            action,
            source,
            date(2026, 6, 17),
            Meal.LUNCH,
            date(2026, 6, 1),
            date(2026, 6, 30),
        )
        selection = next_available_document_output(
            DocumentMealSelection(period, (Meal.LUNCH,)),
            outputs,
            DocumentOutputMode.SAVED_DEFAULT,
        )
        outcome = DocumentGenerationController(
            session,
            message_box=_Messages,
            actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
            generated_at_provider=lambda: datetime(
                2026, 6, 30, 2, 0, tzinfo=UTC
            ),
        ).run(selection)

        assert outcome.completed
        assert outcome.result is not None
        outcome.result.verify_file()
        assert outcome.result.output_path == selection.output_path
        assert outcome.backup_result is not None
        assert outcome.backup_result.record is not None
        runs = session.database.document_runs(kind=kind)
        assert len(runs) == 1
        run = runs[0]
        assert run.source_revision_id == REVISION_ID
        assert run.output_filename == selection.filename
        assert run.calculation_manifest["billing"]["total_count"] == 22
        assert run.template_manifest["template_setting"]["profile"] == profile.value
        assert run.template_manifest["export_password"]["protected"] is True
        assert run.output_sha256 == outcome.result.output_sha256
        backup_path = outcome.backup_result.record.path

        with selection.output_path.open("rb") as source_file:
            office_file = msoffcrypto.OfficeFile(source_file)
            assert office_file.is_encrypted()
            office_file.load_key(password="xlsx-secret")
            plaintext = BytesIO()
            office_file.decrypt(plaintext)
        plaintext.seek(0)
        workbook = openpyxl.load_workbook(plaintext, data_only=False)
        try:
            if action is DocumentAction.INVOICE:
                template_path = (
                    "assets/급식/급식_청구서템플릿.xlsx"
                    if profile is DocumentTemplateProfile.SCHOOL_MEAL
                    else "assets/행복도시락/행복도시락_청구서템플릿.xlsx"
                )
                template_sheet = (
                    "청구서"
                    if profile is DocumentTemplateProfile.SCHOOL_MEAL
                    else "방학_총괄"
                )
                columns = read_template_columns(
                    template_path,
                    sheet_name=template_sheet,
                )
                name_column = next(
                    index
                    for index, name in enumerate(columns, start=1)
                    if name == "성명"
                )
                lunch_column = next(
                    index
                    for index, name in enumerate(columns, start=1)
                    if name == "중식"
                )
                price_column = next(
                    index
                    for index, name in enumerate(columns, start=1)
                    if "청구금액" in name
                )
                school_column = columns.index("학교명") + 1
                grade_column = columns.index("학년") + 1
                head_column = next(
                    index
                    for index, name in enumerate(columns, start=1)
                    if "".join(name.split()) == "세대주성명"
                )
                relationship_column = next(
                    index
                    for index, name in enumerate(columns, start=1)
                    if "".join(name.split()) == "아동과의관계"
                )
                jurisdiction_column = columns.index("관할동") + 1
                address_name = (
                    "도로명주소"
                    if profile is DocumentTemplateProfile.SCHOOL_MEAL
                    else "주소"
                )
                address_column = columns.index(address_name) + 1
                provider_column = columns.index("제공기관") + 1
                output_sheet = (
                    "청구서"
                    if profile is DocumentTemplateProfile.SCHOOL_MEAL
                    else "총괄"
                )
                sheet = workbook[output_sheet]
                assert sheet.cell(4, name_column).value == "홍길동"
                if profile is DocumentTemplateProfile.SCHOOL_MEAL:
                    school_type_column = next(
                        index
                        for index, name in enumerate(columns, start=1)
                        if name.startswith("취학유형")
                    )
                    assert sheet.cell(4, school_type_column).value == "초등학생"
                else:
                    assert set(workbook.sheetnames) == {"총괄", "학기중", "방중"}
                assert sheet.cell(4, school_column).value == "푸른초"
                assert sheet.cell(4, grade_column).value == "6학년"
                assert sheet.cell(4, head_column).value == "홍보호"
                assert sheet.cell(4, relationship_column).value == "자녀"
                assert sheet.cell(4, jurisdiction_column).value == "정자동"
                assert sheet.cell(4, address_column).value == "성남대로 1 101호"
                assert sheet.cell(4, provider_column).value == "센터"
                assert sheet.cell(4, lunch_column).value == 22
                assert sheet.cell(4, price_column).value == 220_000
            else:
                confirm_template = (
                    "assets/급식/급식_확인서템플릿.xlsx"
                    if profile is DocumentTemplateProfile.SCHOOL_MEAL
                    else "assets/행복도시락/행복도시락_확인서템플릿.xlsx"
                )
                placeholders = scan_placeholders(
                    confirm_template,
                    ["1명"],
                )["1명"]
                assert workbook.sheetnames == ["정자동"]
                row, column = placeholders["child1"][0]
                assert workbook["정자동"].cell(row, column).value == "홍길동"
        finally:
            workbook.close()
            plaintext.close()

    restored_path = tmp_path / f"restored-{key}.sqlite3"
    with DatabaseSession.restore_and_open_with_user_password(
        backup_path,
        restored_path,
        "everyday-password",
    ) as restored:
        restored_runs = restored.database.document_runs(kind=kind)
        assert len(restored_runs) == 1
        assert restored_runs[0].output_filename == selection.filename
        restored.database.check_integrity()


def test_latest_saved_mapping_changes_old_source_invoice_and_survives_backup(tmp_path: Path):
    backups = tmp_path / "backups-mapping"
    outputs = tmp_path / "outputs-mapping"
    backups.mkdir()
    outputs.mkdir()
    database_path = tmp_path / "mapping.sqlite3"
    with DatabaseSession.create(database_path, "everyday-password", backup_directory=backups) as session:
        session.append_revision(_initial_revision())
        source = session.database.revision_summaries()[0]
        snapshot = session.database.snapshot(REVISION_ID)
        spec = mapping_editor_spec("급식", "invoice")
        assert "학교명" in spec.targets
        desired = {"학교명": "학년"}
        mapping_revision_id = UUID("6d000000-0000-0000-0000-000000000090")
        prepared = build_mapping_revision(
            REVISION_ID, snapshot, "급식", "invoice", desired,
            actor=RevisionActor("tester", "S-1-5-21-test"),
            recorded_at=datetime(2026, 6, 30, 1, 30, tzinfo=UTC),
            revision_id=mapping_revision_id,
        )
        assert prepared is not None
        mapping_backup = session.append_revision(prepared.record)
        latest = session.database.snapshot(mapping_revision_id)
        assert load_mapping_overrides(latest, "급식", "invoice") == desired
        assert load_mapping_overrides(latest, "행복도시락", "invoice") == {}
        assert load_mapping_overrides(latest, "급식", "confirmation") == {}
        assert build_mapping_revision(
            mapping_revision_id, latest, "급식", "invoice", desired,
            actor=RevisionActor("tester", "S-1-5-21-test"),
            recorded_at=datetime(2026, 6, 30, 1, 40, tzinfo=UTC),
            revision_id=UUID("6d000000-0000-0000-0000-000000000091"),
        ) is None
        period = DocumentPeriodSelection(
            DocumentAction.INVOICE, source, date(2026, 6, 17), Meal.LUNCH,
            date(2026, 6, 1), date(2026, 6, 30),
        )
        selection = next_available_document_output(
            DocumentMealSelection(period, (Meal.LUNCH,)), outputs,
            DocumentOutputMode.SAVED_DEFAULT,
        )
        outcome = DocumentGenerationController(session, message_box=_Messages).run(selection)
        assert outcome.completed
        run = session.database.document_runs(kind=DocumentKind.INVOICE)[0]
        assert run.source_revision_id == REVISION_ID
        assert run.template_manifest["column_mapping"]["학교명"] == "학년"
        assert run.template_manifest["column_mapping_setting"]["setting_revision_id"] == str(mapping_revision_id)
        with selection.output_path.open("rb") as source_file:
            office_file = msoffcrypto.OfficeFile(source_file)
            office_file.load_key(password="xlsx-secret")
            plaintext = BytesIO()
            office_file.decrypt(plaintext)
        plaintext.seek(0)
        workbook = openpyxl.load_workbook(plaintext)
        try:
            column = read_template_columns("assets/급식/급식_청구서템플릿.xlsx", sheet_name="청구서").index("학교명") + 1
            assert workbook["청구서"].cell(4, column).value == "6학년"
        finally:
            workbook.close()
            plaintext.close()
        assert mapping_backup.record is not None
        backup_path = mapping_backup.record.path
    restored_path = tmp_path / "mapping-restored.sqlite3"
    with DatabaseSession.restore_and_open_with_user_password(
        backup_path, restored_path, "everyday-password",
    ) as restored:
        assert load_mapping_overrides(restored.database.snapshot(mapping_revision_id), "급식", "invoice") == desired


@pytest.mark.parametrize(("source_field", "expected"), (("학교명", "푸른초"), ("", None)))
def test_confirmation_saved_name_mapping_changes_actual_workbook(
    tmp_path: Path, source_field: str, expected: str | None,
):
    backups = tmp_path / "backups"
    outputs = tmp_path / "outputs"
    backups.mkdir()
    outputs.mkdir()
    with DatabaseSession.create(tmp_path / "confirmation.sqlite3", "everyday-password", backup_directory=backups) as session:
        session.append_revision(_initial_revision())
        source = session.database.revision_summaries()[0]
        revision_id = UUID("6d000000-0000-0000-0000-000000000099")
        prepared = build_mapping_revision(
            REVISION_ID, session.database.snapshot(REVISION_ID), "급식", "confirmation",
            {"성명": source_field}, actor=RevisionActor("tester", "S-1-5-21-test"),
            recorded_at=datetime(2026, 6, 30, 1, 30, tzinfo=UTC), revision_id=revision_id,
        )
        assert prepared is not None
        session.append_revision(prepared.record)
        period = DocumentPeriodSelection(
            DocumentAction.CONFIRMATION, source, date(2026, 6, 17), Meal.LUNCH,
            date(2026, 6, 1), date(2026, 6, 30),
        )
        selection = next_available_document_output(
            DocumentMealSelection(period, (Meal.LUNCH,)), outputs, DocumentOutputMode.SAVED_DEFAULT,
        )
        outcome = DocumentGenerationController(session, message_box=_Messages).run(selection)
        assert outcome.completed
        with selection.output_path.open("rb") as source_file:
            office_file = msoffcrypto.OfficeFile(source_file)
            office_file.load_key(password="xlsx-secret")
            plaintext = BytesIO()
            office_file.decrypt(plaintext)
        plaintext.seek(0)
        workbook = openpyxl.load_workbook(plaintext)
        try:
            placeholders = scan_placeholders("assets/급식/급식_확인서템플릿.xlsx", ["1명"])["1명"]
            row, column = placeholders["child1"][0]
            assert workbook["정자동"].cell(row, column).value == expected
        finally:
            workbook.close()
            plaintext.close()


def test_publishes_encrypted_delivery_report_records_run_and_backup(
    tmp_path: Path,
):
    backups = tmp_path / "backups-report"
    outputs = tmp_path / "outputs-report"
    backups.mkdir()
    outputs.mkdir()
    database_path = tmp_path / "delivery-report.sqlite3"

    class UnexpectedTemplateController:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("배송 리포트는 청구서·확인서 양식 설정을 요구하지 않습니다.")

    with DatabaseSession.create(
        database_path,
        "everyday-password",
        "institution-recovery",
        backup_directory=backups,
    ) as session:
        session.append_revision(_initial_revision())
        source = session.database.revision_summaries()[0]
        report_day = date(2026, 6, 17)
        period = DocumentPeriodSelection(
            DocumentAction.DELIVERY_REPORT,
            source,
            report_day,
            Meal.LUNCH,
            report_day,
            report_day,
        )
        selection = next_available_document_output(
            DocumentMealSelection(period, (Meal.LUNCH,)),
            outputs,
            DocumentOutputMode.SAVED_DEFAULT,
        )

        outcome = DocumentGenerationController(
            session,
            template_controller_type=UnexpectedTemplateController,
            message_box=_Messages,
            actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
            generated_at_provider=lambda: datetime(
                2026, 6, 17, 2, 0, tzinfo=UTC
            ),
        ).run(selection)

        assert outcome.completed
        assert outcome.result is not None
        outcome.result.verify_file()
        assert outcome.backup_result is not None
        assert outcome.backup_result.record is not None
        runs = session.database.document_runs(kind=DocumentKind.DELIVERY_REPORT)
        assert len(runs) == 1
        run = runs[0]
        assert run.output_filename == selection.filename
        assert run.period_start == report_day
        assert run.calculation_manifest["report_count"] == 1
        assert run.calculation_manifest["target_entitlement_count"] == 1
        assert run.template_manifest["writer"] == (
            "report_generator.make_report_publication"
        )
        assert run.template_manifest["template_setting"]["profile"] == "급식"
        assert run.template_manifest["delivery_template_profile"] == "급식"
        assert run.template_manifest["export_password"]["protected"] is True
        backup_path = outcome.backup_result.record.path

        with selection.output_path.open("rb") as source_file:
            office_file = msoffcrypto.OfficeFile(source_file)
            assert office_file.is_encrypted()
            office_file.load_key(password="xlsx-secret")
            plaintext = BytesIO()
            office_file.decrypt(plaintext)
        plaintext.seek(0)
        workbook = openpyxl.load_workbook(plaintext, data_only=False)
        try:
            assert workbook.sheetnames == ["20260617(중식)"]
            worksheet = workbook["20260617(중식)"]
            assert worksheet["A1"].value == "6월 17일"
            assert worksheet["A2"].value == "*아동급식 중식*"
            assert worksheet["A4"].value == "김기사\n(1)"
            assert worksheet["B4"].value == 1
            assert worksheet["G4"].value == 1
        finally:
            workbook.close()
            plaintext.close()

    restored_path = tmp_path / "restored-delivery-report.sqlite3"
    with DatabaseSession.restore_and_open_with_user_password(
        backup_path,
        restored_path,
        "everyday-password",
    ) as restored:
        restored_runs = restored.database.document_runs(
            kind=DocumentKind.DELIVERY_REPORT
        )
        assert len(restored_runs) == 1
        assert restored_runs[0].output_filename == selection.filename
        restored.database.check_integrity()
