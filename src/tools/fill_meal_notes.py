"""엑셀 비고2를 DB의 <식사 구분 셀 내용>_비고 항목에 반영.

실행: python -m src.tools.fill_meal_notes
미리보기만: python -m src.tools.fill_meal_notes --dry-run

프로그램을 닫고 실행하세요. DB 경로·DB 비밀번호·엑셀 비밀번호를 입력받습니다.
엑셀은 읽기만 합니다. 이름·핸드폰이 일치하는 DB 기존 사람만 수정합니다.
없는 텍스트 항목은 생성하고, 전체 변경을 하나의 이력으로 저장합니다.
빈 비고2·불명확한 매칭·같은 사람/항목의 서로 다른 값은 건너뜁니다.
저장 전 검증된 DB 백업을 만들며, 기존 DB 비고2를 분해하거나 지우지 않습니다.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import getpass
from io import BytesIO
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from time import monotonic
from unicodedata import normalize
from uuid import uuid4
import warnings

import msoffcrypto

from src.domain.projections import roster_audit_changes
from src.domain.revisions import RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import CustomFieldDefinition, CustomFieldType, normalize_phone
from src.storage.database_credentials import DatabaseCredentials
from src.storage.database_session import DatabaseSession
from src.storage.encrypted_database import EncryptedDatabase
from src.storage.file_lock import database_lock
from src.tools._meal_notes_xlsx import read_sparse_book

DEFAULT_SOURCE = Path(r"Z:\2026\행정\01.2026년 청구서\26년10월\2026년 10월_수정구전체통합명단(new).xlsx")
DEFAULT_SHEET = "종합_jon(DB)"


class ConsoleProgress:
    def __init__(self):
        self.started = monotonic()
        self.stage = "작업 준비 중"
        self.stopped = Event()
        self.lock = Lock()
        self.thread = Thread(target=self._heartbeat, daemon=True)
        self.log = None

    def _write(self, message):
        line = f"[{monotonic() - self.started:,.1f}초] {message}"
        print(line, flush=True)
        if self.log is not None:
            self.log.write(line + "\n")
            self.log.flush()

    def __call__(self, message):
        with self.lock:
            self.stage = message
            self._write(message)

    def _heartbeat(self):
        while not self.stopped.wait(10):
            with self.lock:
                self._write(f"{self.stage} … 계속 처리 중")

    def __enter__(self):
        directory = Path(__file__).resolve().parents[2] / "artifacts" / "fill-meal-notes"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"progress_{datetime.now():%Y%m%d_%H%M%S_%f}.log"
        self.log = path.open("x", encoding="utf-8")
        self(f"진행 로그: {path}")
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stopped.set()
        self.thread.join()
        try:
            if exc_type is not None:
                self(f"작업 중단: {exc_type.__name__}: {exc_value}")
        finally:
            self.log.close()
            self.log = None


def _text(value):
    return "" if value is None else str(value)


def _label(value):
    return "".join(normalize("NFKC", _text(value)).split())


def _identity(name, phone):
    if isinstance(phone, float) and phone.is_integer():
        phone = int(phone)
    digits = normalize_phone(_text(phone))
    if len(digits) == 10 and digits.startswith("10"):
        digits = "0" + digits
    return _label(name), digits


def read_excel_payload(path, password, progress=lambda message: None):
    progress("원본 엑셀 읽는 중 (읽기 전용)")
    source = BytesIO(Path(path).read_bytes())
    decrypted = BytesIO()
    try:
        with source as stream:
            office = msoffcrypto.OfficeFile(stream)
            if office.is_encrypted():
                progress("엑셀 비밀번호 확인·복호화 중")
                office.load_key(password=password, verify_password=True)
                office.decrypt(decrypted)
            else:
                stream.seek(0)
                decrypted.write(stream.read())
        return decrypted.getvalue()
    finally:
        decrypted.close()


def read_roster_copy(path, password, progress):
    """Dry-run only: even schema upgrades happen solely in a temporary copy."""
    path = Path(path).resolve()
    with TemporaryDirectory(prefix="meal-notes-preview-") as temporary:
        copied = Path(temporary) / path.name
        with database_lock(path):
            key = DatabaseCredentials.load(path).unlock_user(password)
            shutil.copy2(path, copied)
            for suffix in ("-wal", "-journal"):
                sidecar = Path(str(path) + suffix)
                if sidecar.is_file():
                    shutil.copy2(sidecar, Path(str(copied) + suffix))
        try:
            progress("DB 복사본 검증·명단 읽는 중")
            with EncryptedDatabase.open(copied, key) as database:
                return database.current_roster()
        finally:
            key = None


def _find_headers(sheet):
    for row in sheet.iter_rows(max_row=min(sheet.max_row, 50)):
        labels = defaultdict(list)
        for cell in row:
            labels[_label(cell.value)].append(cell.column)
        columns = (labels["이름"] or labels["성명"], labels["핸드폰"],
                   labels["식사구분"], labels["비고2"])
        if all(columns):
            if any(len(items) != 1 for items in columns):
                raise ValueError("이름·핸드폰·식사 구분·비고2 제목이 중복되어 있습니다.")
            return row[0].row, *(items[0] for items in columns)
    raise ValueError("처음 50행에서 이름(성명)·핸드폰·식사 구분·비고2 제목을 찾지 못했습니다.")


@dataclass
class ImportPlan:
    record: RevisionRecord | None
    changed_people: int
    changed_values: int
    created_fields: tuple[str, ...]
    counts: Counter
    issues: tuple[tuple[tuple[int, ...], str], ...]


def prepare_import(roster, sheet, *, actor=None, progress=lambda message: None):
    if roster is None:
        raise ValueError("DB에 명단이 없습니다.")
    header, name_col, phone_col, meal_col, note_col = _find_headers(sheet)
    people = defaultdict(list)
    for person in roster.persons:
        key = _identity(person.profile.name, person.profile.mobile_phone)
        if all(key):
            people[key].append(person)
    rows = sorted({row for (row, column), cell in sheet._cells.items()
                   if row > header and column in (name_col, phone_col) and cell.value is not None})
    counts = Counter()
    issues = []
    candidates = defaultdict(lambda: defaultdict(list))
    target_names = {}
    for index, number in enumerate(rows, 1):
        if index == 1 or index % 200 == 0 or index == len(rows):
            progress(f"엑셀 → DB 매칭 {index:,}/{len(rows):,}행")
        cells = [sheet.cell(number, col) for col in (name_col, phone_col, meal_col, note_col)]
        if any(cell.data_type == "f" for cell in cells):
            counts["수식 행 건너뜀"] += 1
            issues.append(((number,), "이름·핸드폰·식사 구분·비고2 중 수식이 있음"))
            continue
        name, phone, meal, note = (cell.value for cell in cells)
        key = _identity(name, phone)
        if not all(key):
            counts["이름/핸드폰 누락"] += 1
            continue
        matches = people.get(key, ())
        if not matches:
            counts["DB에 없는 엑셀 행"] += 1
            continue
        if len(matches) != 1:
            counts["DB 중복 매칭"] += 1
            issues.append(((number,), "DB 이름·핸드폰 중복"))
            continue
        if not _text(meal).strip() or not _text(note).strip():
            counts["식사 구분/비고2 빈 값"] += 1
            continue
        target = (_text(meal) + "_비고").strip()
        folded = target.casefold()
        target_names.setdefault(folded, target)
        candidates[matches[0].person_id, folded][_text(note)].append(number)

    fields = list(roster.custom_fields)
    by_name = {field.name.casefold(): field for field in fields}
    replacements = {}
    created = []
    changed_values = 0
    for (person_id, folded), variants in candidates.items():
        source_rows = tuple(sorted(n for numbers in variants.values() for n in numbers))
        if len(variants) > 1:
            counts["서로 다른 값 충돌"] += 1
            issues.append((source_rows, "같은 DB 사람·항목에 다른 비고2 값: 전체 건너뜀"))
            continue
        note = next(iter(variants))
        field = by_name.get(folded)
        if field is not None and field.field_type is not CustomFieldType.TEXT:
            counts["텍스트가 아닌 DB 항목"] += 1
            issues.append((source_rows, "기존 DB 항목이 텍스트형이 아님"))
            continue
        if field is None:
            field = CustomFieldDefinition(uuid4(), target_names[folded], CustomFieldType.TEXT,
                                          max((item.display_order for item in fields), default=-1) + 1)
            fields.append(field)
            by_name[folded] = field
            created.append(field.name)
        person = replacements.get(person_id) or roster.person(person_id)
        if person.profile.custom_values.get(field.definition_id) == note:
            counts["이미 같은 값"] += 1
            continue
        values = dict(person.profile.custom_values)
        values[field.definition_id] = note
        replacements[person_id] = replace(person, profile=replace(person.profile, custom_values=values))
        changed_values += 1
    record = None
    if replacements:
        updated = replace(roster, revision_id=uuid4(), custom_fields=tuple(fields),
                          persons=tuple(replacements.get(p.person_id, p) for p in roster.persons))
        recorded_at = datetime.now(UTC)
        metadata = RevisionMetadata(
            updated.revision_id, roster.revision_id, recorded_at, None,
            actor or RevisionActor(getpass.getuser(), "maintenance:excel-meal-notes"),
            "엑셀 식사 구분별 비고2 가져오기", RevisionSource.UI,
            decisions={"meal_notes_import": {"direction": "excel_to_database",
                        "changed_people": len(replacements), "changed_values": changed_values,
                        "created_fields": created}},
        )
        record = RevisionRecord(metadata, roster_audit_changes(roster, updated))
    return ImportPlan(record, len(replacements), changed_values, tuple(created), counts, tuple(issues))


def _show_plan(plan, progress):
    progress(f"DB 반영 대상: {plan.changed_people:,}명 / {plan.changed_values:,}개 값 / 새 항목 {len(plan.created_fields)}개")
    for name in plan.created_fields:
        progress(f"생성할 DB 항목: {name}")
    for reason, count in plan.counts.items():
        progress(f"{reason}: {count:,}건")
    for rows, reason in plan.issues:
        progress(f"엑셀 행 {', '.join(map(str, rows))}: {reason}")


def run(database, db_password, source, excel_password, *, sheet_name=DEFAULT_SHEET,
        backup_dir=None, dry_run=False, progress=lambda message: None):
    database = Path(database).resolve()
    if not database.is_file():
        raise FileNotFoundError(f"DB 파일을 찾을 수 없습니다: {database}")
    payload = read_excel_payload(source, excel_password, progress)
    book = read_sparse_book(payload, sheet_name, progress)
    del payload
    try:
        sheet = book[sheet_name]
        _find_headers(sheet)  # Validate the Excel source before opening the DB.
        if dry_run:
            roster = read_roster_copy(database, db_password, progress)
            plan = prepare_import(roster, sheet, progress=progress)
            _show_plan(plan, progress)
            progress("미리보기 완료: DB를 수정하지 않았습니다.")
            return plan
        backup_root = Path(backup_dir) if backup_dir else database.parent / "meal-notes-backups"
        backup_directory = backup_root / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8])
        progress("수정할 DB 열기·검증 중")
        with DatabaseSession.open(database, db_password, backup_directory=backup_directory) as session:
            roster = session.database.current_roster()
            plan = prepare_import(roster, sheet, progress=progress)
            _show_plan(plan, progress)
            if plan.record is None:
                progress("변경할 값이 없습니다. 명단 변경 이력을 추가하지 않았습니다.")
                return plan
            progress("DB 수정 전 백업 생성·검증 중")
            backup_directory.mkdir(parents=True, exist_ok=False)
            backup = session.backup_now()
            progress(f"수정 전 백업 완료: {backup.path}")
            progress("DB에 사용자 항목·비고 저장 중 (하나의 변경 이력)")
            result = session.append_revision(plan.record)
            progress(f"DB 저장 완료: {plan.changed_people:,}명 / {plan.changed_values:,}개 값")
            if result.error is not None:
                progress(f"DB 저장은 완료됐으나 저장 후 백업 실패: {result.error}. 수정 전 백업은 보존되어 있습니다.")
            elif result.record is not None:
                progress(f"저장 후 백업 완료: {result.record.path}")
            return plan
    finally:
        book.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="엑셀 비고2 → DB의 <식사 구분 셀 내용>_비고 항목")
    parser.add_argument("database", nargs="?", help="수정할 DB 경로 (생략 시 입력)")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="읽을 원본 엑셀 경로")
    parser.add_argument("--sheet", default=DEFAULT_SHEET)
    parser.add_argument("--backup-dir", type=Path, help="DB 백업 상위 폴더 (기본: DB 옆 meal-notes-backups)")
    parser.add_argument("--dry-run", action="store_true", help="반영 예정 내역만 확인하고 DB는 수정하지 않음")
    parser.add_argument("--no-pause", action="store_true", help="완료 후 Enter 입력을 기다리지 않음")
    args = parser.parse_args(argv)
    try:
        print("엑셀은 읽기만 하며, DB의 식사 구분별 비고 항목을 수정합니다.", flush=True)
        database = args.database or input("수정할 DB 파일 경로: ").strip().strip('"')
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            db_password = getpass.getpass("DB 비밀번호: ")
            excel_password = getpass.getpass("원본 엑셀 비밀번호: ")
        try:
            with ConsoleProgress() as progress:
                run(database, db_password, args.source, excel_password, sheet_name=args.sheet,
                    backup_dir=args.backup_dir, dry_run=args.dry_run, progress=progress)
        finally:
            db_password = excel_password = None
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\n작업이 중단되었습니다. 진행 로그의 DB 저장 완료 여부를 확인해주세요.", flush=True)
        return 1
    except getpass.GetPassWarning:
        print("비밀번호를 가려 입력할 수 있는 터미널에서 실행해주세요.", flush=True)
        return 1
    except Exception as error:
        print(f"DB 반영 실패: {error}", flush=True)
        return 1


def run_cli():
    try:
        return main()
    finally:
        if sys.stdin.isatty() and not any(arg in sys.argv for arg in ("-h", "--help", "--no-pause")):
            try:
                input("\n결과를 확인한 뒤 Enter를 누르면 종료합니다...")
            except (KeyboardInterrupt, EOFError):
                pass


if __name__ == "__main__":
    raise SystemExit(run_cli())

