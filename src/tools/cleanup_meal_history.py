"""Preview: python -m src.tools.cleanup_meal_history <DB path>

Apply after preview: add --apply. Passwords are prompted without echo and never
accepted on the command line or written to files. Close the application first.
"""
import argparse
from datetime import UTC, datetime
import getpass
from pathlib import Path
import sys
from uuid import uuid4
import warnings

from src.domain.revisions import RevisionActor
from src.services.meal_history_cleanup import build_meal_history_cleanup, plan_meal_history_cleanup
from src.storage.database_session import DatabaseSession


def _period(period):
    return f"{period.starts_on or '-'} ~ {period.ends_on or ''}"


def main(argv=None):
    parser = argparse.ArgumentParser(description="중복 중지·배송 이력 정리. 기본은 미리보기이며 운영 프로그램을 닫은 뒤 실행하세요.")
    parser.add_argument("database", nargs="?", help="운영 DB 경로 (생략하면 입력받음)")
    parser.add_argument("--apply", action="store_true", help="검증된 사전 백업 후 정리 적용")
    parser.add_argument("--backup-dir", type=Path, help="백업 상위 폴더 (기본: DB 옆 meal-history-cleanup-backups)")
    parser.add_argument("--no-pause", action="store_true", help="완료 후 Enter 입력을 기다리지 않음")
    args = parser.parse_args(argv)
    path = Path(args.database or input("운영 DB 파일 경로: ").strip().strip('"')).expanduser().resolve()
    if not path.is_file():
        parser.error("운영 DB 파일을 찾을 수 없습니다.")
    backup_root = args.backup_dir or path.parent / "meal-history-cleanup-backups"
    backup_directory = backup_root / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("운영 DB 비밀번호 (화면에 표시되지 않음): ")
        try:
            print("운영 DB를 여는 중입니다...", flush=True)
            session = DatabaseSession.open(path, password, backup_directory=backup_directory)
        finally:
            password = None
        with session:
            print("중복 이력을 확인하는 중입니다...", flush=True)
            latest = session.database.latest_revision_id()
            if latest is None:
                print("명단이 없습니다. 변경하지 않았습니다.")
                return 0
            snapshot = session.database.snapshot(latest)
            revisions = session.database.branch_revision_chain(latest)
            plan = plan_meal_history_cleanup(snapshot, revisions)
            roster = session.database.current_roster()
            print(f"현재 분기 · 정리 대상 {len(plan)}건")
            for item in plan:
                event = item.removed
                person = roster.person(event.person_id)
                print(f"{person.profile.name} ({person.profile.birth_date or '-'}, {str(person.person_id)[:8]}) · {event.meal.value} · {event.status.value}")
                print(f"  제거: {_period(event.period)} / 등록 {event.recorded_at.isoformat()}")
                print(f"  유지: {_period(item.retained.period)} / 등록 {item.retained.recorded_at.isoformat()}")
                print(f"  기준: {item.reason}")
            print("일시중지·다른 상태 사이의 기록·삭제하면 기간이 짧아지는 유한 기록은 유지합니다.")
            if not plan or not args.apply:
                print("변경하지 않았습니다." if not plan else "미리보기만 했습니다. 적용하려면 같은 명령에 --apply를 추가하세요.")
                return 0
            record = build_meal_history_cleanup(snapshot, revisions,
                actor=RevisionActor(getpass.getuser(), "maintenance:meal-history-cleanup"),
                recorded_at=datetime.now(UTC), revision_id=uuid4())
            backup_directory.mkdir(parents=True, exist_ok=False)
            backup = session.backup_now()
            print(f"정리 전 백업: {backup.path}")
            result = session.append_revision(record)
            print(f"정리 완료: {len(plan)}건. 가입·가구·배정은 유지했고 원본 감사 이력은 보존했습니다.")
            if result.error is not None:
                print("정리는 저장됐지만 정리 후 백업은 실패했습니다. 위 정리 전 백업은 보존되어 있습니다.")
            else:
                print(f"정리 후 백업: {result.record.path}")
            return 0
    except getpass.GetPassWarning:
        print("비밀번호를 가려 입력할 수 있는 터미널에서 실행해주세요. 변경하지 않았습니다.")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("입력을 취소했습니다.")
        return 1
    except Exception as error:
        print(f"정리를 완료하지 못했습니다 ({type(error).__name__}): {error}", flush=True)
        return 1


def run_cli(argv=None):
    """Keep an interactive console open for both results and argument errors."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        result = main(arguments)
    except SystemExit as error:
        result = error.code
    except (KeyboardInterrupt, EOFError):
        print("입력을 취소했습니다.")
        result = 1
    if "--no-pause" not in arguments and sys.stdin.isatty():
        try:
            input("\n결과를 확인한 뒤 Enter를 누르면 종료합니다...")
        except (KeyboardInterrupt, EOFError):
            pass
    return result


if __name__ == "__main__":
    raise SystemExit(run_cli())
