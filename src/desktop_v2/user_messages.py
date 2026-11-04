"""Korean presentation of failures; keep original exceptions in the lower layers."""
from __future__ import annotations

import errno
import re


_FALLBACK = ("프로그램이 자료를 처리하는 중 예상하지 못한 문제가 발생했습니다. 현재 저장 상태를 확인한 뒤 "
             "작업 창을 다시 열어 다시 시도해주세요. 같은 문제가 계속되면 수행한 작업과 이 안내를 프로그램 담당자에게 전달해주세요.")
_INTEGRITY_MESSAGE = ("운영 파일의 자료와 변경 이력이 맞지 않아 작업을 진행할 수 없습니다. "
                      "운영 파일을 다시 열어 확인해주세요. 계속되면 파일을 보관한 채 프로그램 담당자에게 점검을 요청하거나 정상 백업을 복원해주세요.")
_CONFLICT_MESSAGE = ("작업 창을 연 뒤 다른 작업에서 자료를 변경하여 저장할 수 없습니다. "
                     "이 창을 닫고 다시 열어 최신 내용을 확인한 뒤 변경 내용을 다시 입력해주세요.")
_FIELDS = {
    "lunch_period": "중식 기간", "lunch_starts_on": "중식 시작일", "lunch_ends_on": "중식 종료일",
    "name": "성명", "birth_date": "생년월일", "mobile_phone": "핸드폰",
    "phone": "전화번호", "phone_number": "전화번호", "telephone": "전화번호",
    "road_address": "도로명주소", "detail_address": "상세주소", "old_address": "지번주소",
    "district": "관할동", "school": "학교", "school_name": "학교", "grade": "학년", "note": "비고",
    "guardian": "보호자", "household_head": "세대주", "provider": "제공기관",
    "person_id": "대상자", "household_id": "가구", "revision_id": "변경번호",
    "definition_id": "사용자 칼럼", "observation_id": "원본 행", "field_type": "입력 형식",
    "custom_fields": "사용자 칼럼", "custom_values": "사용자 칼럼 값",
    "effective_on": "적용일", "recorded_at": "기록 시각", "received_at": "접수 시각",
    "starts_on": "시작일", "ends_on": "종료일", "start_date": "시작일", "end_date": "종료일",
    "service_dates": "배송일", "driver": "배송담당", "sequence": "배송순서",
    "meal": "식종", "meals": "식종", "status": "배송 상태", "field": "항목",
}
_TERMS = {
    **_FIELDS, "머리글": "헤더",
    "profile field": "명단 항목", "import profile": "열 연결 설정",
    "current roster": "현재 명단", "reconciliation plan": "명단 연결 결과",
    "revision ID": "변경번호", "person ID": "대상자", "household ID": "가구",
    "observation ID": "원본 행", "API 키": "주소 조회 인증키",
    "person": "대상자", "household": "가구", "plan": "연결 결과", "decision": "선택 내용",
    "decisions": "선택 내용", "ignored": "제외한", "ignore": "제외", "create": "추가", "link": "연결",
    "batch": "불러온 자료", "typed": "정리된", "basename": "파일 이름", "source": "원본",
    "schedule": "일정", "entitlement": "제공 내역", "calculation": "계산 결과",
    "policy": "설정", "entity": "항목", "canonical": "표준", "metadata": "기록 정보",
    "before": "변경 전 값", "after": "변경 후 값", "insert": "추가", "update": "수정", "delete": "삭제",
    "JSON object": "자료", "JSON": "저장 자료", "object": "자료", "state": "상태",
    "SHA-256": "파일 검증값", "byte": "바이트", "bytes": "바이트", "editor": "입력란",
    "GUI": "화면", "Windows": "윈도우", "Microsoft": "마이크로소프트", "PC": "컴퓨터",
    "SID": "계정 정보", "db": "운영 데이터", "API": "주소 조회 서비스",
    "match mode": "연결 방식", "proposal": "가구 연결 결과", "factory": "생성 처리",
    "header fingerprint": "헤더 확인 정보", "YYMMDD": "연월일 6자리(예: 261029)",
    "index": "번호", "READY": "완성된", "staff": "배송담당", "HTTP": "통신",
    "block header": "자료의 시작 정보", "block": "자료", "payload": "백업 내용",
    "ZIP": "파일", "credential": "비밀번호 정보", "session": "연결",
    "run": "생성 기록", "Complaint": "민원", "append-only": "기록 추가",
    "snapshot": "저장된 명단", "roster": "명단", "revision": "변경 기록",
    "observation": "원본 행", "profile": "명단 설정", "rule": "기준",
    "mapping": "열 연결", "staging": "연결 검토", "matching": "연결",
    "identity": "대상자 연결", "import": "불러오기", "export": "내보내기",
    "manifest": "파일 정보", "schema": "저장 형식", "transaction": "저장 작업",
    "timezone": "시간대", "version": "설정 버전", "actor": "변경자", "key": "항목",
    "date": "날짜", "datetime": "날짜와 시각", "None": "빈 값", "null": "빈 값",
    "int": "정수", "float": "숫자", "str": "문자", "bool": "예 또는 아니요",
    "UUID": "식별 정보", "ID": "식별 정보", "DB": "데이터", "Excel": "엑셀",
    "YYYY-MM-DD": "연-월-일(예: 2026-10-29)", "YYYYMMDD": "연월일 8자리(예: 20261029)",
}
_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(k) for k in sorted(_TERMS, key=len, reverse=True)) + r")(?![A-Za-z0-9_])([은는이가을를과와])?")
# Filenames and quoted user values must not be translated as internal field names.
_USER_TEXT = re.compile(r"^[^:\n]+(?= 값(?:은|이))|(?:[A-Za-z]:[\\/]|\\\\)[^\n\"<>]*|[^\s\"'<>]+\.(?:xlsx|xlsm|xls|sqlite3|db|mealreplica|mealbackup|json|pdf|ini)\b|[\"'][^\"'\n]+[\"']", re.IGNORECASE)
_INTERNAL = re.compile(r"(?<![A-Za-z0-9_])(?:[a-z]+_[a-z_]+|[A-Z][a-z]+(?:[A-Z][a-zA-Z]+)+)(?![A-Za-z0-9_])|<class |Traceback|0x[0-9a-fA-F]+|expected=|actual=|pywin32")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_TECHNICAL = re.compile(
    r"(?<![A-Za-z0-9_])(?:entity|revision|snapshot|projection|ledger|canonical|metadata|payload|transaction|schema|migration|EntityRef|UUID|ID|typed|factory|JSON|bytes)(?![A-Za-z0-9_])"
    r"|\b[a-z][a-z-]*\.[a-z][a-z_.-]*\b", re.I,
)


def user_error(error: BaseException | str | None) -> str:
    """Show actionable known failures, readable validation, or a bounded fallback."""
    raw = str(error).strip() if error is not None else ""
    low = raw.lower()
    kind = type(error).__name__
    # Explain the failure, not its implementation vocabulary. Keep these checks
    # ahead of token translation; translating entity/insert/UUID was misleading.
    if "이력이 있는 entity ID" in raw or "entity ID가 다시 insert" in raw:
        subject = "학교 일정" if "school-lunch.schedule" in raw else "항목"
        return (f"{subject}을 저장하지 못했습니다. 이전 등록 기록과 중복되어 저장이 차단되었습니다. "
                "작업 창을 닫고 다시 열어 현재 등록 내용을 확인해주세요. 복구 후 다시 불러올 때 반복되면 프로그램 담당자에게 점검을 요청해주세요.")
    if kind == "RevisionConflictError" or "parent revision이 현재" in raw or "audit before가 현재" in raw:
        return _CONFLICT_MESSAGE
    if kind in {"DatabaseIntegrityError", "ProjectionContractError"} or any(word in low for word in (
        "typed projection", "audit before", "revision parent chain", "revision ledger", "봉인", "무결성 검사", "before chain",
    )):
        return _INTEGRITY_MESSAGE
    if kind in {"DatabaseUnlockError", "CredentialUnlockError"}:
        return "운영 파일의 잠금을 풀지 못했습니다. 선택한 운영 파일과 비밀번호를 확인해주세요."
    if "school" in low and "entity id" in low:
        return "학교 일정의 저장 정보가 맞지 않습니다. 학교 일정 창을 다시 열어 확인해주세요. 계속되면 프로그램 담당자에게 점검을 요청해주세요."
    if "snapshot에 없는 사람" in raw:
        return "선택한 대상자가 현재 명단에 없습니다. 명단을 다시 열어 대상자가 삭제되었는지 확인해주세요."
    if "중식 일정이 없습니다" in raw:
        return raw + " 운영 일정에서 해당 학교·기간의 방학 일정을 불러온 뒤 다시 시도해주세요."
    code = getattr(error, "errno", None)
    win = getattr(error, "winerror", None)
    if win in (32, 33) or "sharing violation" in low or "being used by another process" in low:
        return "다른 프로그램에서 파일을 사용 중입니다. 파일을 닫고 다시 시도해주세요."
    if isinstance(error, PermissionError) or code in (errno.EACCES, errno.EPERM) or "permission denied" in low or "access is denied" in low or "read-only database" in low:
        return "파일에 접근할 수 없습니다. 파일을 연 프로그램을 닫고 폴더의 쓰기 권한을 확인해주세요."
    if isinstance(error, FileExistsError) or code == errno.EEXIST:
        return "같은 이름의 파일이 이미 있습니다. 다른 이름이나 저장 위치를 선택해주세요."
    if isinstance(error, FileNotFoundError) or code == errno.ENOENT or "no such file or directory" in low:
        return "파일 또는 폴더를 찾을 수 없습니다. 선택한 위치와 공유 폴더 연결을 확인해주세요."
    if code == errno.ENOSPC or win == 112 or "disk is full" in low or "no space left" in low:
        return "저장 공간이 부족합니다. 여유 공간을 확보한 뒤 다시 시도해주세요."
    if isinstance(error, (ConnectionError, TimeoutError)) or win in (53, 64, 67, 121, 1231) or any(x in low for x in ("timed out", "connection refused", "network path", "network is unreachable", "connection reset")):
        return "연결이 끊겼거나 응답이 늦습니다. 인터넷·공유 폴더 연결을 확인하고 다시 시도해주세요."
    if "database is locked" in low or "database table is locked" in low:
        return "다른 작업에서 데이터를 사용 중입니다. 잠시 기다린 뒤 다시 시도해주세요."
    if any(x in low for x in ("file is not a database", "database disk image is malformed", "bad magic number", "file is not a zip file")):
        return "파일 형식이 올바르지 않거나 파일이 손상되었습니다. 원본 또는 백업 파일을 다시 선택해주세요."
    if "unique constraint" in low:
        return "이미 등록된 항목과 중복됩니다. 내용을 확인해주세요."
    if "foreign key constraint" in low:
        return "연결된 항목을 찾을 수 없습니다. 화면을 다시 열어 확인해주세요."
    if "not null constraint" in low:
        return "필수 항목이 비어 있습니다. 입력 내용을 확인해주세요."
    if any(x in low for x in ("invalid isoformat", "does not match format", "day is out of range", "month must be", "year must be")):
        return "날짜가 올바르지 않습니다. 연-월-일 형식으로 입력해주세요. 예: 2026-10-29"
    if any(x in low for x in ("invalid literal for int", "could not convert string to float", "conversion syntax", "conversionsyntax")):
        return "숫자 형식이 올바르지 않습니다. 숫자로 입력해주세요."
    if isinstance(error, KeyError):
        label = _FIELDS.get(raw.strip("'\""))
        return f"{label} 항목을 찾을 수 없습니다. 입력 내용과 열 연결을 확인해주세요." if label else "필요한 항목을 찾을 수 없습니다. 화면을 다시 열어 확인해주세요."
    if isinstance(error, (TypeError, AttributeError)):
        label = next((title for key, title in _FIELDS.items() if re.search(r"(?<![A-Za-z_])" + key + r"(?![A-Za-z_])", raw)), None)
        return (f"{label + ' ' if label else ''}자료를 프로그램이 처리하지 못했습니다. "
                "작업 창을 다시 열어 확인해주세요. 계속되면 수행한 작업을 프로그램 담당자에게 전달해주세요.")
    if "migration 이력" in raw or "sqlite v2 db" in low:
        return "운영 파일의 데이터 형식이 프로그램과 맞지 않습니다. 선택한 파일과 프로그램 버전을 확인해주세요."
    if any(word in low for word in ("암호화 runtime", "pywin32가 필요", "qapplication이 실행")):
        return "작업에 필요한 구성 요소를 사용할 수 없습니다. 프로그램 설치 상태를 확인해주세요."
    if re.search(r"(?<![.a-z0-9_])sqlite(?![a-z0-9_])", low):
        return "운영 파일을 읽을 수 없습니다. 올바른 파일인지 확인해주세요."
    if not raw or not re.search(r"[가-힣]", raw):
        return _FALLBACK
    if raw.startswith(("알 수 없는", "Unknown column")) and re.search(r"[A-Za-z_]{2,}", raw):
        return "선택한 항목을 사용할 수 없습니다. 화면을 다시 열어 선택해주세요."
    protected = []
    def protect(match):
        protected.append(match.group())
        return f"§{len(protected)-1}§"
    # Protect genuine filenames/quoted input before inspecting implementation
    # tokens. Never display mechanically translated dotted entity names or UUIDs.
    message = _USER_TEXT.sub(protect, _UUID.sub("해당 항목", raw))
    if _TECHNICAL.search(message) or "해당 항목" in message or "표준 식종순" in message:
        return _FALLBACK
    def translate(match):
        label = _TERMS[match.group(1)]
        particle = match.group(2) or ""
        # Do not mistake the verb ending in "이어야" for a subject particle.
        copula = particle == "이" and message[match.end():].startswith(("어야", "다", "라", "면", "며", "었", "고", "니"))
        if particle and not copula and "가" <= label[-1] <= "힣":
            final = (ord(label[-1]) - ord("가")) % 28 != 0
            for pair in ("은는", "이가", "을를", "과와"):
                if particle in pair:
                    particle = pair[0 if final else 1]
                    break
        return label + particle
    message = _TOKEN_PATTERN.sub(translate, message)
    message = message.replace("머리글", "헤더").replace("가져오기", "불러오기").replace("임포트", "불러오기")
    if _INTERNAL.search(message):
        return _FALLBACK
    for index, value in enumerate(protected):
        message = message.replace(f"§{index}§", value)
    return message
