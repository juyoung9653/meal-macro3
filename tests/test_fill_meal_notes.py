"""Excel source -> existing DB people, one audited change after verified backup."""
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook

from src.domain.projections import roster_audit_changes
from src.domain.revisions import RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import CustomFieldDefinition, CustomFieldType, Person, PersonProfile, RosterSnapshot
from src.excel.publication import publish_workbook
from src.storage.database_session import DatabaseSession
from src.tools.fill_meal_notes import DEFAULT_SHEET, prepare_import, read_excel_payload, run
from src.tools._meal_notes_xlsx import read_sparse_book


def _fixture(tmp_path):
    old = CustomFieldDefinition(uuid4(), "비고2", CustomFieldType.TEXT, 0)
    existing = CustomFieldDefinition(uuid4(), "기타_비고", CustomFieldType.TEXT, 1)
    def person(name, phone, values=None):
        return Person(uuid4(), PersonProfile(name, mobile_phone=phone, custom_values=values or {}))
    people = (
        person("샘플", "010-1234-5678", {old.definition_id: "옛조식/옛석식", existing.definition_id: "이전"}),
        person("충돌", "01022223333"),
        person("중복", "01044445555"),
        person("중복", "01044445555"),
        person("유지", "01066667777", {existing.definition_id: "유지값"}),
    )
    roster = RosterSnapshot(uuid4(), persons=people, custom_fields=(old, existing))
    db = tmp_path / "sample.db"
    metadata = RevisionMetadata(roster.revision_id, None, datetime.now(UTC), None,
                                RevisionActor("test", "test"), "명단", RevisionSource.UI)
    with DatabaseSession.create(db, "db-password") as session:
        session.append_revision(RevisionRecord(metadata, roster_audit_changes(None, roster)))
    book = Workbook()
    sheet = book.active
    sheet.title = DEFAULT_SHEET
    sheet.append(["제목"])
    sheet.append(["성명", "핸드폰", "식사 구분", "비고2"])
    sheet.append(["샘플", 1012345678, "조식_BOX", "엑셀 메모"])
    sheet.append(["샘플", "01012345678", "석식", "=둘/셋"])
    sheet["D4"].data_type = "s"
    sheet.append(["샘플", "01012345678", "기타", "교체"])
    sheet.append(["샘플", "01012345678", "조식_BOX", "엑셀 메모"])
    sheet.append(["충돌", "01022223333", "임의", "첫째"])
    sheet.append(["충돌", "01022223333", "임의", "둘째"])
    sheet.append(["중복", "01044445555", "중복", "제외"])
    sheet.append(["없음", "01088889999", "외부", "제외"])
    sheet.append(["유지", "01066667777", "기타", None])
    sheet.append(["샘플", "01099999999", "전화다름", "제외"])
    sheet.append(["샘플", "01012345678", "임의 식사 형태", "슬래시 없음"])
    book.create_sheet("읽지 않을 시트")["A1"] = "=1+2"
    source = tmp_path / "source.xlsx"
    publish_workbook(book, source, password="excel-password")
    return db, source, roster, old, existing


def _read(db):
    with DatabaseSession.open(db, "db-password") as session:
        return session.database.current_roster()


def test_excel_notes_update_db_in_one_revision_and_keep_source_and_old_notes(tmp_path):
    db, source, original, old, existing = _fixture(tmp_path)
    source_hash = sha256(source.read_bytes()).digest()
    events = []
    plan = run(db, "db-password", source, "excel-password", progress=events.append)
    assert plan.changed_people == 1
    assert plan.changed_values == 4
    assert set(plan.created_fields) == {"조식_BOX_비고", "석식_비고", "임의 식사 형태_비고"}
    current = _read(db)
    assert current.revision_id == plan.record.metadata.revision_id
    assert plan.record.metadata.parent_revision_id == original.revision_id
    assert {p.person_id for p in current.persons} == {p.person_id for p in original.persons}
    fields = {field.name: field.definition_id for field in current.custom_fields}
    values = current.person(original.persons[0].person_id).profile.custom_values
    assert values[fields["조식_BOX_비고"]] == "엑셀 메모"
    assert values[fields["석식_비고"]] == "=둘/셋"
    assert values[fields["임의 식사 형태_비고"]] == "슬래시 없음"
    assert values[existing.definition_id] == "교체"
    assert values[old.definition_id] == "옛조식/옛석식"
    assert "외부_비고" not in fields and "임의_비고" not in fields
    assert all(current.person(person.person_id) == person for person in original.persons[1:])
    assert sha256(source.read_bytes()).digest() == source_hash
    assert any("수정 전 백업 완료" in event for event in events)
    assert list((tmp_path / "meal-notes-backups").rglob("*"))
    rerun = run(db, "db-password", source, "excel-password")
    assert rerun.record is None
    assert _read(db).revision_id == current.revision_id


def test_dry_run_changes_neither_db_nor_excel(tmp_path):
    db, source, original, _, _ = _fixture(tmp_path)
    hashes = {path: sha256(path.read_bytes()).digest() for path in (db, source)}
    plan = run(db, "db-password", source, "excel-password", dry_run=True)
    assert plan.changed_values == 4
    assert all(sha256(path.read_bytes()).digest() == digest for path, digest in hashes.items())
    assert not (tmp_path / "meal-notes-backups").exists()
    assert _read(db).revision_id == original.revision_id


def test_pre_backup_failure_prevents_any_db_import(tmp_path, monkeypatch):
    db, source, original, _, _ = _fixture(tmp_path)
    def fail(self):
        raise OSError("backup failed")
    monkeypatch.setattr(DatabaseSession, "backup_now", fail)
    with pytest.raises(OSError, match="backup failed"):
        run(db, "db-password", source, "excel-password")
    current = _read(db)
    assert current.revision_id == original.revision_id
    assert current.custom_fields == original.custom_fields
    assert all(current.person(person.person_id) == person for person in original.persons)


def test_post_backup_failure_reports_already_committed_values(tmp_path, monkeypatch):
    db, source, original, _, _ = _fixture(tmp_path)
    real_backup = DatabaseSession.backup_now
    calls = []
    def backup(self):
        calls.append(None)
        if len(calls) == 2:
            raise OSError("post backup failed")
        return real_backup(self)
    monkeypatch.setattr(DatabaseSession, "backup_now", backup)
    events = []
    plan = run(db, "db-password", source, "excel-password", progress=events.append)
    assert _read(db).revision_id == plan.record.metadata.revision_id
    assert any("DB 저장은 완료됐으나" in event for event in events)


def test_wrong_excel_password_never_opens_db_for_writing(tmp_path, monkeypatch):
    db, source, original, _, _ = _fixture(tmp_path)
    def fail(*args, **kwargs):
        pytest.fail("DB must not be opened")
    monkeypatch.setattr(DatabaseSession, "open", fail)
    with pytest.raises(Exception):
        run(db, "db-password", source, "wrong")


def test_sparse_source_ignores_twenty_thousand_formatted_empty_rows(tmp_path):
    db, source, original, _, _ = _fixture(tmp_path)
    payload = read_excel_payload(source, "excel-password")
    changed = BytesIO()
    blanks = b"".join(f'<row r="{row}"><c r="A{row}" s="0" t="n"/></row>'.encode()
                      for row in range(100000, 120000))
    with ZipFile(BytesIO(payload)) as archive, ZipFile(changed, "w", ZIP_DEFLATED) as target:
        for info in archive.infolist():
            data = archive.read(info)
            if info.filename == "xl/worksheets/sheet1.xml":
                data = data.replace(b"</sheetData>", blanks + b"</sheetData>")
            target.writestr(info, data)
    book = read_sparse_book(changed.getvalue(), DEFAULT_SHEET)
    assert book.sheetnames == [DEFAULT_SHEET]
    assert book.active.max_row == 13
    assert prepare_import(original, book.active).changed_values == 4
    book.close()


