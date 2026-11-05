"""Choose a historical stop date and reversibly clear those meal registrations."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from getpass import getpass
from pathlib import Path
import sys
from uuid import uuid4

ROOT = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[1])
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(ROOT))


def configured_backup(path, *, bases=None):
    """Read matching portable settings only; never initialize or change settings."""
    from src.desktop_v2.portable_settings import DesktopStorageSettings
    for base in bases or (ROOT, ROOT / "dist", path.parent):
        if not (base / "data" / "settings.ini").is_file():
            continue
        settings = DesktopStorageSettings(base_directory=base)
        if settings.database_path == path.resolve():
            return settings.backup_directory
    return None


def cleanup_in_place(session, *, actor, stop_date=None):
    from src.services.september30_cleanup import build_cleanup
    head = session.database.latest_revision_id()
    if head is None:
        return None, None
    records = session.database.branch_revision_chain(head)
    record = build_cleanup(session.database.snapshot(head), records, actor=actor,
        recorded_at=datetime.now(UTC), revision_id=uuid4(), stop_date=stop_date)
    if record is None:
        return None, None
    backup = session.append_revision(record)
    return record, backup


def read_stop_date():
    from src.desktop_v2.user_messages import user_error
    from src.services.september30_cleanup import StopDate
    while True:
        text = input("중지 시작일 (2026-09-30: 해당 연도 / 9/30: 모든 연도, 빈칸: 취소): ").strip()
        if not text:
            return None
        try:
            return StopDate.parse(text)
        except ValueError as error:
            print(user_error(error))


def main(*, stop_date=None):
    from src.domain.projections import PERSON_KIND
    from src.desktop_v2.import_controller import current_windows_actor
    from src.desktop_v2.user_messages import user_error
    from src.services.september30_cleanup import find_targets
    from src.storage.database_session import DatabaseSession
    print("중지 식종 미배송 정리 — 급식 배송 운영 프로그램을 종료한 뒤 실행해주세요.")
    print("현재 작업본의 과거 중지 기록에서 입력한 시작일을 찾습니다. 이후 배송을 재개한 사람도 포함합니다.")
    print("해당 대상자·식종의 가입 기간과 상태 기록을 현재 자료에서 지워 처음부터 미가입(미배송)으로 정리합니다.")
    print("그 식종의 과거 기간을 현재 자료로 다시 계산하는 결과도 바뀝니다. 이전 변경번호의 원본은 보존합니다.")
    print("파일·비밀번호·백업 연결은 유지합니다. 정리 전 변경번호로 되돌리면 복구할 수 있습니다.")
    if stop_date is None:
        stop_date = read_stop_date()
    if stop_date is None:
        print("취소했습니다.")
        return
    print(f"정리할 중지 시작일: {stop_date.label}")
    path_text = input("운영 파일 경로 (파일을 이 창에 끌어 놓아도 됩니다): ").strip().strip('"')
    if not path_text:
        print("취소했습니다.")
        return
    path = Path(path_text).expanduser().resolve()
    password = getpass("운영 파일 비밀번호: ")
    with DatabaseSession.open(path, password, backup_directory=configured_backup(path)) as session:
        head = session.database.latest_revision_id()
        records = () if head is None else session.database.branch_revision_chain(head)
        targets = find_targets(records, stop_date=stop_date)
        if not targets:
            print(f"{stop_date.label} 시작 중지 기록이 없습니다. 변경하지 않았습니다.")
            return
        names = {}
        for record in records:
            for change in record.changes:
                if change.entity_kind == PERSON_KIND:
                    state = change.after or change.before or {}
                    names[str(change.entity_id)] = state.get("name", "삭제된 대상자")
        print(f"\n{stop_date.label} 시작 중지 — 정리 대상 {len(targets)}건:")
        for person_id, meal in sorted(targets, key=lambda pair: (names.get(pair[0], ""), pair[1])):
            print(f"  {names.get(person_id, '삭제된 대상자')} / {meal}")
        print(f"되돌릴 변경번호: {session.database.revision_display_code(head)}")
        if input("\n위 대상 식종을 현재 파일에서 정리하려면 '정리' 입력: ").strip() != "정리":
            print("취소했습니다.")
            return
        record, backup = cleanup_in_place(session, actor=current_windows_actor(), stop_date=stop_date)
        if record is None:
            print("이미 정리되어 있습니다. 변경하지 않았습니다.")
            return
        print(f"\n정리 저장 완료: {session.path}")
        print(f"정리 변경번호: {session.database.revision_display_code(record.metadata.revision_id)}")
        print("기존 파일을 그대로 열어 사용하세요. 이전 자료로 되돌리기에서 정리 전 변경번호를 선택하면 복구됩니다.")
        print("이미 만든 Excel 파일은 바뀌지 않습니다. 필요한 문서는 다시 생성해주세요.")
        if backup.error is not None:
            print("정리는 저장됐지만 자동 백업을 만들지 못했습니다. " + user_error(backup.error))
        elif backup.succeeded:
            print("기존 자동 백업 폴더에도 저장했습니다.")
        else:
            print("이 도구에서 연결된 백업 경로를 찾지 못했습니다. 기존 앱의 설정은 유지되며, 앱을 열어 자동 백업 상태를 확인해주세요.")


def packaging_smoke():
    """Check frozen imports, encryption, exact-date cleanup and restore in temp data."""
    from datetime import date
    from tempfile import TemporaryDirectory
    from src.domain.projections import roster_audit_changes, status_event_audit_change
    from src.domain.revisions import RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
    from src.domain.roster import InclusiveDateRange, Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot
    from src.domain.status import MealStatus, StatusEvent
    from src.services.revision_restore import restore_revision
    from src.services.september30_cleanup import StopDate
    from src.storage.database_session import DatabaseSession

    actor = RevisionActor("패키징 검사", "synthetic")
    recorded = datetime.now(UTC)
    persons = tuple(Person(uuid4(), PersonProfile(name)) for name in ("합성 해당연도", "합성 다른연도"))
    original = RosterSnapshot(uuid4(), persons, enrollments=tuple(
        MealEnrollment(person.person_id, Meal.DINNER) for person in persons))
    with TemporaryDirectory(prefix="meal-stop-cleanup-") as directory:
        root = Path(directory)
        backups = root / "backups"
        backups.mkdir()
        path = root / "synthetic.sqlite3"
        with DatabaseSession.create(path, "synthetic-password", backup_directory=backups) as session:
            session.database.append_revision(RevisionRecord(RevisionMetadata(
                original.revision_id, None, recorded, None, actor, "합성 초기 자료", RevisionSource.UI,
            ), roster_audit_changes(None, original)), replay=True)
            for person, year in zip(persons, (2026, 2025)):
                revision = uuid4()
                event = StatusEvent(uuid4(), revision, person.person_id, Meal.DINNER,
                    MealStatus.STOPPED, InclusiveDateRange(date(year, 11, 4)), recorded)
                session.database.append_revision(RevisionRecord(RevisionMetadata(
                    revision, session.database.latest_revision_id(), recorded, None, actor,
                    "합성 중지", RevisionSource.UI,
                ), (status_event_audit_change(event),)), replay=True)
            before = session.database.latest_revision_id()
            selected = StopDate.parse("2026-11-04")
            cleanup, backup = cleanup_in_place(session, actor=actor, stop_date=selected)
            assert cleanup is not None and backup.succeeded
            current = session.database.current_roster()
            assert current.meals_for(persons[0].person_id, on=date(2026, 11, 4)) == ()
            assert current.meals_for(persons[1].person_id, on=date(2026, 11, 4)) == (Meal.DINNER,)
            assert cleanup_in_place(session, actor=actor, stop_date=selected) == (None, None)
            restored = restore_revision(session, before, cleanup.metadata.revision_id, actor)
            assert restored.backup.succeeded
            assert session.database.snapshot(restored.revision_id).entities == session.database.snapshot(before).entities
        with DatabaseSession.open(path, "synthetic-password") as session:
            session.database.check_integrity()
    print("중지 정리 실행 검사 통과: 날짜 선택·암호화 저장·백업·복구")


def run(argv=None):
    # Frozen Windows programs can default redirected output to cp949, which
    # cannot encode all names or punctuation. Native consoles already use Unicode.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and not stream.isatty() and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="중지 시작일을 선택해 해당 식종을 미가입(미배송)으로 정리합니다.")
    parser.add_argument("--no-pause", action="store_true", help="종료 시 Enter 입력을 기다리지 않습니다")
    parser.add_argument("--stop-date", help="중지 시작일: 2026-09-30 또는 9/30")
    parser.add_argument("--packaging-smoke", action="store_true", help="임시 합성 자료로 실행 파일을 검사합니다")
    args = parser.parse_args(argv)
    code = 0
    try:
        if args.packaging_smoke:
            packaging_smoke()
        else:
            from src.services.september30_cleanup import StopDate
            selected = StopDate.parse(args.stop_date) if args.stop_date is not None else None
            main(stop_date=selected)
    except (KeyboardInterrupt, EOFError):
        print("\n중단했습니다. 저장 완료 안내가 나왔다면 해당 변경번호에서 결과를 확인해주세요.")
        code = 1
    except Exception as error:
        from src.desktop_v2.user_messages import user_error
        print("\n처리하지 못했습니다. " + user_error(error))
        code = 1
    finally:
        if not args.no_pause and not args.packaging_smoke:
            try:
                input("\nEnter를 누르면 창을 닫습니다.")
            except (EOFError, KeyboardInterrupt):
                pass
    return code


if __name__ == "__main__":
    raise SystemExit(run())
