"""One-off reversible cleanup in the existing operating file."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from getpass import getpass
from pathlib import Path
import sys
import traceback
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
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


def cleanup_in_place(session, *, actor):
    from src.services.september30_cleanup import build_cleanup
    head = session.database.latest_revision_id()
    if head is None:
        return None, None
    records = session.database.branch_revision_chain(head)
    record = build_cleanup(session.database.snapshot(head), records, actor=actor,
        recorded_at=datetime.now(UTC), revision_id=uuid4())
    if record is None:
        return None, None
    backup = session.append_revision(record)
    return record, backup


def main():
    from src.domain.projections import PERSON_KIND
    from src.desktop_v2.import_controller import current_windows_actor
    from src.desktop_v2.user_messages import user_error
    from src.services.september30_cleanup import find_targets
    from src.storage.database_session import DatabaseSession
    print("프로그램을 종료한 뒤 실행해주세요. 현재 작업본 이력에서 9월 30일 시작 중지를 찾습니다(연도 무관).")
    print("해당 대상자·식종을 처음부터 미가입으로 정리하고, 기존 운영 파일에 변경 기록 하나로 저장합니다.")
    print("파일·비밀번호·백업 연결은 유지합니다. 정리 전 변경번호로 되돌리면 복구할 수 있습니다.")
    path_text = input("운영 파일 경로 (파일을 이 창에 끌어 놓아도 됩니다): ").strip().strip('"')
    if not path_text:
        print("취소했습니다.")
        return
    path = Path(path_text).expanduser().resolve()
    password = getpass("운영 파일 비밀번호: ")
    with DatabaseSession.open(path, password, backup_directory=configured_backup(path)) as session:
        head = session.database.latest_revision_id()
        records = () if head is None else session.database.branch_revision_chain(head)
        targets = find_targets(records)
        if not targets:
            print("9월 30일 시작 중지 기록이 없습니다. 변경하지 않았습니다.")
            return
        names = {}
        for record in records:
            for change in record.changes:
                if change.entity_kind == PERSON_KIND:
                    state = change.after or change.before or {}
                    names[str(change.entity_id)] = state.get("name", "삭제된 대상자")
        print(f"\n정리 대상 {len(targets)}건:")
        for person_id, meal in sorted(targets, key=lambda pair: (names.get(pair[0], ""), pair[1])):
            print(f"  {names.get(person_id, '삭제된 대상자')} / {meal}")
        print(f"되돌릴 변경번호: {session.database.revision_display_code(head)}")
        if input("\n위 대상 식종을 현재 파일에서 정리하려면 '정리' 입력: ").strip() != "정리":
            print("취소했습니다.")
            return
        record, backup = cleanup_in_place(session, actor=current_windows_actor())
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-pause", action="store_true", help="launcher already keeps the console open")
    args = parser.parse_args()
    code = 0
    try:
        main()
    except KeyboardInterrupt:
        print("\n중단했습니다. 저장 완료 안내가 나왔다면 해당 변경번호에서 결과를 확인해주세요.")
        code = 1
    except Exception as error:
        try:
            from src.desktop_v2.user_messages import user_error
            message = user_error(error)
        except ImportError:
            message = "실행에 필요한 구성 요소가 없습니다. 이 프로그램의 Python 의존성을 설치한 환경에서 실행해주세요."
        print("\n처리하지 못했습니다. " + message)
        print("아래는 문제 확인을 위한 기술 상세입니다.")
        traceback.print_exc()
        code = 1
    finally:
        if not args.no_pause:
            try:
                input("\nEnter를 누르면 창을 닫습니다.")
            except (EOFError, KeyboardInterrupt):
                pass
    raise SystemExit(code)
