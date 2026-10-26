"""비고2에 '/'가 정확히 1개 들어 있는 사람의 전체 명단 열을 XLSX로 추출.

프로젝트 폴더에서 실행:
    python -m src.tools.export_note2_single_slash
    python -m src.tools.export_note2_single_slash "D:\\명단.db" -o "D:\\추출.xlsx"

프로그램을 닫고 실행하세요. DB 경로 생략 시 입력받고 비밀번호는 가려서 입력합니다.
원본 DB는 임시 폴더로 복사한 뒤 읽습니다. 결과는 사람당 한 행이며,
기본 정보·전체 사용자 항목·급식 상태·배송담당·순서를 포함합니다.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
import getpass
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import warnings

from openpyxl import Workbook

from src.services.database_export import _add_table, build_database_workbook
from src.storage.database_credentials import DatabaseCredentials
from src.storage.encrypted_database import EncryptedDatabase
from src.storage.file_lock import database_lock


def build_filtered_workbook(roster, snapshot, *, day: date):
    fields = [field for field in roster.custom_fields if field.name.strip() == "비고2"]
    if len(fields) != 1:
        raise ValueError("이름이 '비고2'인 사용자 항목이 정확히 하나 있어야 합니다.")
    field_id = fields[0].definition_id
    selected = {
        str(person.person_id)
        for person in roster.persons
        if str(person.profile.custom_values.get(field_id) or "").count("/") == 1
    }
    source = build_database_workbook(roster, snapshot, (), day=day, sections=("roster",))
    try:
        sheet = source["명단(통합)" if "명단(통합)" in source.sheetnames else "명단"]
        values = iter(sheet.values)
        headers = next(values)
        rows = tuple(row for row in values if row[0] in selected)
        result = Workbook()
        result.remove(result.active)
        _add_table(result, "비고2 슬래시 1개", headers, rows)
        _add_table(result, "추출 정보", ("항목", "값"), (
            ("조건", "비고2에 /가 정확히 1개 포함된 행"),
            ("기준일", day.isoformat()),
            ("추출 인원", len(rows)),
            ("명단 revision UUID", str(roster.revision_id)),
        ))
        return result, len(rows)
    finally:
        source.close()


def export_database(database_path: Path, password: str, output: Path, *, day: date) -> int:
    database_path = Path(database_path).resolve()
    output = Path(output).resolve()
    if not database_path.is_file():
        raise FileNotFoundError(f"DB 파일을 찾을 수 없습니다: {database_path}")
    if output.suffix.lower() != ".xlsx":
        raise ValueError("출력 파일 확장자는 .xlsx여야 합니다.")
    if output.exists():
        raise FileExistsError(f"출력 파일이 이미 있습니다. 다른 이름을 지정하세요: {output}")
    with TemporaryDirectory(prefix="note2-export-") as temporary:
        copied = Path(temporary) / database_path.name
        with database_lock(database_path):
            key = DatabaseCredentials.load(database_path).unlock_user(password)
            shutil.copy2(database_path, copied)
            # 종료되지 않은 트랜잭션/체크포인트도 복사본에서 처리한다.
            for suffix in ("-wal", "-journal"):
                sidecar = Path(str(database_path) + suffix)
                if sidecar.is_file():
                    shutil.copy2(sidecar, Path(str(copied) + suffix))
        try:
            with EncryptedDatabase.open(copied, key) as database:
                roster = database.current_roster()
                if roster is None:
                    raise ValueError("DB에 명단이 없습니다.")
                snapshot = database.snapshot(roster.revision_id)
                book, count = build_filtered_workbook(roster, snapshot, day=day)
        finally:
            key = None
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("xb") as stream:
                book.save(stream)
        finally:
            book.close()
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description="비고2에 /가 정확히 1개인 행 전체를 엑셀로 추출")
    parser.add_argument("database", nargs="?", help="DB 파일 경로 (생략하면 입력받음)")
    parser.add_argument("-o", "--output", type=Path, help="저장할 .xlsx 경로")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today(),
                        help="급식·배송 정보 기준일 YYYY-MM-DD (기본: 오늘)")
    args = parser.parse_args(argv)
    try:
        path = Path(args.database or input("DB 파일 경로: ").strip().strip('"')).expanduser()
        output = args.output or (
            Path(__file__).resolve().parents[2] / "output"
            / f"비고2_슬래시1개_{datetime.now():%Y%m%d_%H%M%S_%f}.xlsx"
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("DB 비밀번호: ")
        try:
            count = export_database(path, password, output, day=args.date)
        finally:
            password = None
        print(f"완료: {count}명 / {output.resolve()}")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\n입력을 취소했습니다.")
        return 1
    except getpass.GetPassWarning:
        print("비밀번호를 가려 입력할 수 있는 터미널에서 실행해주세요.")
        return 1
    except Exception as error:
        print(f"추출 실패: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

