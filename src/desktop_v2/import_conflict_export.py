"""Read-only Excel comparison of the actual conflicts shown by import review."""
from collections import Counter

from openpyxl.styles import PatternFill

from src.importing.contracts import ProfileField
from .current_sheet_export import (
    CurrentSheetSnapshot, TextWorkbookSnapshot, build_current_sheet_workbook,
)


def conflict_export_snapshot(result, plan, rows) -> TextWorkbookSnapshot:
    """One source person per row, with adjacent DB/source pairs per field."""
    fields = dict((row.field, row.label) for row in rows)
    counts = Counter(fields.values())
    used_labels = set()
    labels = []
    for field, label in fields.items():
        if counts[label] > 1 and field.startswith("custom:"):
            label += " (사용자 항목)"
        base = label
        number = 2
        while label in used_labels:
            label = f"{base} ({number})"
            number += 1
        used_labels.add(label)
        labels.append(label)
    headers = ("원본 파일", "시트", "원본 행번호", "이름", "핸드폰", "세대주명", *(
        heading for label in labels for heading in (f"DB_{label}", f"명단_{label}")
    ))
    conflicts = {row.key: row for row in rows}
    people = {row.person_id for row in rows}
    exported = []
    for staged in plan.staged_people:
        obs = staged.observation
        person_id = result.person_ids_by_observation.get(obs.observation_id)
        if person_id not in people:
            continue
        values = [obs.source.source_filename, obs.source.sheet_name,
                  ", ".join(map(str, obs.source.row_numbers)), obs.name,
                  _text(obs.profile_values.get(ProfileField.MOBILE_PHONE)),
                  _text(obs.profile_values.get(ProfileField.HOUSEHOLD_HEAD_NAME))]
        for field in fields:
            row = conflicts.get((person_id, field))
            values.extend(("", "") if row is None else (_text(row.before), _text(row.incoming)))
        exported.append(tuple(values))
    comparison = CurrentSheetSnapshot("불일치", headers, tuple(exported))
    instructions = CurrentSheetSnapshot("안내", ("항목", "설명"), (
        ("적용일", plan.effective_on.isoformat()),
        ("비교 대상", "값 충돌 검토창에 표시된 불일치만 담았습니다. 자동 처리·같은 값·신규 대상자는 제외합니다."),
        ("이름·핸드폰·세대주명", "왼쪽 식별 정보는 원본 명단 값입니다. 원본 파일·시트·행번호로 대조하세요."),
        ("DB / 명단", "DB_항목은 불러오기 전 저장값, 명단_항목은 원본 파일 값입니다. 서로 다른 값 쌍은 노란색입니다."),
        ("빈칸", "한 사람의 항목 쌍이 모두 빈칸이면 그 항목은 검토 대상 불일치가 아닙니다."),
        ("GUI에서 수정", "검토창의 항목별 반영 방향·사람별 예외 수정을 계속 사용할 수 있습니다. 내보내기는 선택을 바꾸거나 저장하지 않습니다."),
        ("원본 수정 후 다시 불러오기", "원본을 고칠 때는 현재 불러오기를 취소한 뒤 원본 Excel을 수정하고 다시 불러오세요. 이 비교표는 원본 명단을 대신하는 입력 파일이 아닙니다."),
        ("GUI 선택과 비교값", "GUI에서 값을 선택하거나 수정해도 이 파일의 DB 값·명단 값은 바뀌지 않습니다. 최종 반영값은 검토창에서 확인하세요."),
    ))
    return TextWorkbookSnapshot("명단 불일치", (comparison, instructions))


def build_conflict_workbook(snapshot, *, progress=None):
    def report(_stage, completed, total):
        if progress is not None:
            progress("명단 불일치 Excel 준비 중", completed, total)

    workbook = build_current_sheet_workbook(snapshot, progress=report)
    worksheet = workbook["불일치"]
    worksheet.freeze_panes = "G2"
    highlight = PatternFill("solid", fgColor="FFF1CC")
    for row in worksheet.iter_rows(min_row=2):
        for column in range(6, len(row), 2):
            pair = row[column:column + 2]
            if any(cell.value not in (None, "") for cell in pair):
                for cell in pair:
                    cell.fill = highlight
    return workbook


def _text(value):
    return "" if value is None else str(value)
