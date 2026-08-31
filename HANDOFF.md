# SQLite 데스크톱 앱 리팩터링 Handoff

## 다음 세션의 목표

기존 Excel 중심 급식 관리 프로그램을 단일 PC용 암호화 SQLite 데스크톱 앱으로 단계적으로 교체한다. 기존 앱은 Excel 템플릿, 배송일 계산, 명단 합치기, 검토, 배송 리포트, 배송대타, 확인서 및 청구서 동작을 비교하는 기준으로 남긴다. 기능 동등성 검증 전에는 기존 구현을 삭제하지 않는다.

SQLite v2의 배송 리포트 계산·암호화 Excel·문서 이력·자동 백업과 업체 DB별 조식 배송
규칙 설정까지 구현됐다. 사용자가 현재 기능을 먼저 검토하기로 했으므로 새 one-file 빌드는
보류했다. 다음 제품 선택 지점은 검토 반영 뒤 남은 `배송대타`의 출력·설정 흐름이다.

## 저장소 상태

- 작업 브랜치: `refactor/sqlite-desktop-v2`
- 작업 기준 커밋: `bd5156b feat(roster): add merge and review workflows`
- 이 worktree는 생성 직후 깨끗했고 `python -m pytest -q tests` 결과는 `327 passed, 5 warnings`였다.
- 경고 5개는 테스트가 비문자 Excel 헤더를 의도적으로 생성할 때 발생하는 openpyxl 경고다.
- 저장소 루트에서 인자 없이 `pytest`를 실행하면 접근 권한이 없는 기존 `output/pytest_*` 폴더를 수집하려다 실패할 수 있다. 검증 명령은 `python -m pytest -q tests`를 사용한다.
- 기준 커밋은 로컬에만 있으며 원격으로 push하지 않았다.
- 기존 worktree에는 실제 명단일 가능성이 높은 약 3.3MB Excel 파일 하나가 untracked 상태로 남아 있다. 개인정보 위험 때문에 열거나 커밋하지 말고, 필요하면 사용자에게 용도를 확인한다.

기존 Excel 프로그램 계약은 [기존 워크북 문서](docs/annual-delivery-workbook.md)와 기준 커밋의 코드·테스트를 우선 참고한다. 임시 아키텍처 검토 자료는 `C:\Users\user\AppData\Local\Temp\architecture-review-20260824-141500.md`에 있으나 임시 파일이므로 영구 계약으로 취급하지 않는다.

## 확정된 제품 범위

- 업체별로 서로 다른 한 PC에서 로컬 DB 하나를 사용한다. 두 업체 DB를 한 앱에서 동시에 조회할 필요는 없다.
- 한 대상자가 두 업체에 동시에 속할 수 없다는 업무 규칙은 유지한다.
- 사용자에게 보이는 새 고유 관리번호는 만들지 않는다. 내부 참조용 UUID는 사용 가능하다.
- 앱 실행 때 사용자가 비밀번호를 입력한다.
- 기존 Excel 템플릿을 사용한 최종 출력은 유지한다.
- 명단을 Excel로 내보내고, 받은 Excel 명단으로 일괄 추가·업데이트할 수 있어야 한다.
- 배송 리포트와 배송대타 기능도 새 앱에 포함한다.
- 명단 화면에는 선택일 기준 상태를 표시하고, 별도 화면에는 날짜별 배송 유무를 표시한다.
- 가구 모아보기 기능이 필요하다.
- 사용자 정의 열을 GUI에서 동적으로 추가할 수 있어야 한다.

## 명단과 식종 모델

- 사람은 한 번 저장하고 조식·중식·석식 가입, 상태, 배송담당, 배송순서는 식종별로 저장한다.
- 배송담당과 배송순서는 사람 공통과 식종별 override를 모두 지원한다.
- 상태는 식종마다 다를 수 있다.
- 명단 우클릭 메뉴는 정확히 `배송`, `일시중지`, `중지`, `이력보기`, `민원` 다섯 개다.
- 배송·일시중지·중지는 시작일과 종료일을 선택한다. 배송과 중지는 한쪽 끝이 열린 기간을 허용한다.
- 과거 날짜로 상태를 변경할 수 있다.
- 이력보기는 상태와 기간을 행으로 보여주되 연속된 같은 상태의 기간은 묶는다.
- `17일 중지`, `19일 배송`처럼 입력된 사건은 이력에 원문 사건으로 보존하면서 실제 처리는 17~18일 일시중지와 같은 결과가 되어야 한다. 원본 사건과 계산된 유효 기간을 구분한다.
- 배송 상태가 되면 실제 서비스 날짜의 배송 값을 채운다. 물리 배송일 계산은 기존 코드를 회귀 기준으로 사용한다.
- 민원에는 이름, 세대주 성명, 핸드폰을 함께 표시하고 내용은 별도로 기록한다. 민원용 사용자 정의 필드도 지원한다.
- 가구 묶기 기준은 GUI에서 핸드폰·세대주 성명·주소·전화번호를 여러 개 선택한다. 기본은 핸드폰 하나다.
- 선택한 기준값이 모두 비어 있지 않고 모두 같은 경우(AND)에만 한 가구로 제안한다. 빈칸끼리는 일치로 보지 않는다.
- 가구 검토는 사람 identity 병합과 분리한다. `이 사람만 별도 가구`는 사람 레코드를 삭제·병합하지 않고 해당 사람의 household membership만 다른 안정적 내부 household ID로 지정한다.

## 조사표·운영명단 가져오기

사용자가 제공한 정부 조사표는 운영 명단이 아니라 필요한 앞부분 인적사항과 식종 표지만 추출하는 외부 입력이다. DB 저장형이나 출력형으로 사용하지 않는다.

현재 확인한 기본 조사표 매핑은 다음과 같다.

| 원본 열 | 의미 | 기본 처리 |
|---|---|---|
| A | 원본 연번 | 관리번호가 아니라 source reference로만 기록 |
| C | 취학유형 | profile |
| D | 학교명 | profile |
| E | 학년 | profile |
| F | 반 | 신규 가져오기에서는 사용하지 않음; 기존 DB 값은 숨겨 보존 |
| G | 성명 | profile |
| H | 생년월일 | profile 및 identity 검토 자료 |
| I | 세대주 성명 | profile |
| J | 아동과의 관계 | profile |
| L | 관할동 | profile |
| M | 기존 주소 | 주소 후보 |
| N | 변경주소 | 주소 후보 |
| O | 핸드폰 | profile 및 household 후보 |
| S | 연중 조식 | 개별 식종 여부 열(1/0) |
| T | 연중 석식 | 개별 식종 여부 열(1/0) |
| V | 방학 중식 | 개별 식종 여부 열(1/0) |
| U | 토·공휴일 중식 | 사용하지 않음 |
| AI | 토·공휴일 석식 | 사용하지 않음 |

- 개별 식종 여부 열은 기본적으로 숫자/문자 `1`이면 급식을 받고, `0` 또는 빈 셀이면 받지 않는 것으로 읽는다. 다른 값은 자동 반영하지 않고 검토 대상으로 표시한다.
- M/N 주소 우선순위를 포함한 Excel 구조 규칙을 하드코딩하지 않는다.
- GUI에서 헤더 행, 데이터 시작 행, 원본 열→DB 필드, 후보 열 우선순위, 급식을 받는 값/받지 않는 값, 날짜·전화 정규화, 무시할 열을 수정한다. 제공기관은 Excel 열에 연결하지 않고 한 번 직접 입력해 이번 명단 전체에 적용한다.
- 설정은 단순 임시 캐시가 아니라 암호화 DB의 이름 있는 `import_profile`로 저장하고 version과 변경 이력을 남긴다.
- 헤더 집합과 구조 fingerprint가 같은 경우에만 프로필을 자동 제안한다. 구조가 다르면 미리보기·매핑 검토를 다시 요구한다.
- 외부 Excel 구조는 import profile, 배송·공휴일 정책은 적용일이 있는 versioned 업무 규칙, 템플릿 열 연결은 export profile로 분리한다.
- 신규/업데이트/충돌/식종 변경을 staging 화면에서 보여주고 사용자가 확정한 뒤 한 revision으로 commit한다.
- 최종 검토에는 가구 제안도 함께 표시한다. 제안을 유지하거나 사람별로 별도 가구를 선택할 수 있으며, 기준 rule과 선택 결과를 revision에 남긴다.
- 명단이 늦게 오면 사용자가 inclusive 적용일을 선택한다.
- 학교·학년의 자연스러운 진급은 자동 후보로 제안할 수 있지만, 그 외 불일치는 사용자가 검토한다. 모호한 identity는 자동 병합하지 않는다.
- 운영명단 입력은 식종별 행 분리형과 한 행의 합쳐진 식종형을 모두 지원한다.
- 열 연결 화면은 식종을 `조식·중식·석식 여부 열이 각각 있음 (1/0)`과 `식종 열 하나에 조식·중식·석식이 적혀 있음` 두 예시로 설명하고, 선택한 방식에 필요한 입력칸만 보여준다. 사용자 오류에는 `flag` 같은 내부 용어를 표시하지 않는다.

## 명단 Excel 출력

- 출력 1: 사람×식종 1행의 분리형.
- 출력 2: 사람 1행에 `조식 / 중식 / 석식`을 넣는 통합형.
- 통합형도 식종별 담당·순서를 잃지 않도록 `조식 담당/순서`, `중식 담당/순서`, `석식 담당/순서` 열이나 보조 assignment 시트를 제공한다.
- 모든 출력은 현재 상태를 직접 읽지 말고 선택한 revision snapshot에서 생성한다.
- 과거 날짜 상태를 나중에 바꿔도 이미 만든 문서를 `재생성 필요`로 표시하거나
  덮어쓰지 않는다. 발행 당시 source revision에 연결된 document run은 그대로 두고,
  정정이 필요할 때 최신 revision으로 새 run을 만든다.
- 명단·청구서 등 개인정보가 든 생성 Excel은 명단 상단 `설정`에서 지정한 센터 공통 비밀번호를 자동 적용한다. 생성 때마다 묻지 않고 변경 후 새 파일부터 사용한다. 설정 화면에서는 현재 값만 가린 채 두고, 요청하면 30초 동안 보거나 복사할 수 있다. 과거 비밀번호 목록은 일반 화면에 보이지 않는다.
- SQLite v2의 조식·중식·석식 기본 단가는 모두 10,000원이다. 명단 상단 `단가 관리`에서 식종을 한 개 이상 선택하고 같은 새 단가와 inclusive 적용일을 저장한다. 식종별 금액이 다르면 변경을 나눠 저장한다.
- DB 하나가 업체 하나를 대표한다. `휴무·공휴일` 화면은 오프라인 한국 공휴일을 자동 후보로만 보여주며, 사용자가 날짜를 한 개 이상 체크한 뒤 같은 식종 집합과 `정상 운영/미제공/대체식`을 한 번에 확정해야 반영한다. 날짜는 기본 선택하지 않고 `미검토 전체 선택`을 제공한다. 업체 자체 휴무는 inclusive 기간으로 직접 추가한다. 조식·중식·석식이 기본 전체 선택되지만 일부만 고를 수 있다. 미제공은 청구에서 제외하고, 대체식은 원래 식사일 청구를 유지하면서 실제 제공일을 날짜×식종별로 바꾼다.

## 중식 명단과 학교 일정

- 조식과 석식은 연중 개별 업데이트다.
- 중식은 여름방학·겨울방학 차수별 baseline 명단이 한꺼번에 오고 이후 추가·제외 명단이 올 수 있다.
- 중식 baseline/add/remove와 학교 일정 Excel은 서로 다른 입력이다.
- 중식은 월~금 서비스이고 묶음 배송이 없다. 서비스일과 물리 배송일이 같다.
- 학교별 방학 일정과 급식 제공일을 적용하며 입학·졸업, 특정 학년 제외, 날짜/기간 예외가 있다.
- 일정 Excel의 `O`는 학교가 중식을 제공하므로 업체 배송 없음, `X`는 학교 중식이 없으므로 업체 배송 있음이다.
- 비고의 `6학년 미급식` 같은 표현은 해당 학년에 학교 급식이 없다는 뜻이므로 업체 배송 포함 override다.
- 확인한 일정 파일은 헤더 2행, 데이터 3~161행 구조였다. 한 학교명이 병합 셀로 두 행에 이어지는 사례가 있으므로 실제 병합 범위 안에서만 학교명을 forward-fill한다.
- 방학식/개학일 기본 O/X와 비고의 날짜·기간·학년 예외를 구조화된 override 후보로 만들되, 자연어를 무조건 확정하지 말고 GUI에서 확인시킨다.

## 조식과 배송·청구 계산

- 기본적으로 검은색 평일에 배송하지만 공휴일에도 예외 배송이 가능하다.
- 조식은 일반적으로 금요일에 금·토·일 3일치를 물리 배송한다.
- 금요일이 빨간 공휴일이면 정책에 따라 목요일에 토·일분만 배송하거나 금·토·일 전체를 배송하지 않을 수 있다.
- 물리 배송 이벤트와 식사 제공일별 entitlement를 분리한다.
- 청구 수량은 배송 행 수가 아니라 실제 entitlement 식수 기준이다.
- 월 중간에 단가가 바뀌면 entitlement 서비스일의 유효 단가로 나눠 계산한다. 식종별 총 식수는 합산하되 금액은 단가별 `식수×단가` 소계를 더한다.
- 중식은 월~금 1일 1건이며 묶지 않는다.
- 날짜별 배송 유무, 선택일 명단 상태, 배송 리포트, 배송대타 및 청구서는 같은 entitlement 결과를 사용해야 한다.

## DB, 감사 이력, 보안·복구

- 무료 암호화 SQLite 후보는 `apsw-sqlite3mc`다. 현재 실행 환경이 Python 3.14이므로 실제 wheel/패키징 지원 여부를 가장 먼저 spike하고, 지원이 불안하면 Python runtime 버전을 고정한다.
- SQLCipher를 전제로 하지 않는다.
- DB의 임의 수정까지 포함해 변경 전후 값, 유효 적용일, 기록 시각, Windows 사용자명/SID, 입력 경로, source batch, 사용자 결정, rule version을 immutable revision에 남긴다.
- 특정 날짜 또는 revision 기준 명단·배송 리포트·청구서를 다시 생성할 수 있어야 한다.
- 내부 revision ID는 UUID를 유지한다. 화면과 문서에는 한국 기록일과 그날 저장
  순번을 조합한 고정 변경번호 `YYYYMMDD-001`을 보여준다. 적용일이 아니라 실제
  기록일을 쓰며 DB가 번호를 부여한다.
- import batch에는 원본 파일명(전체 개인 경로 제외), SHA-256, 크기, sheet/header fingerprint, mapping manifest, 적용일과 사용자 결정을 남긴다.
- 운영 DB에는 실제 사용한 canonical 값과 근거만 저장하고 U/AI 등 미사용 조사표 개인정보를 extra JSON으로 복제하지 않는다.
- 원본 Excel 전체 bytes의 암호화 보관은 개인정보 정책과 보존 기간이 확인된 경우에만 선택 기능으로 둔다.
- 앱 비밀번호나 고정 master password를 DB 평문으로 넣지 않는다. 사용자는 복잡한 recovery file/card 대신 **기관 복구 비밀번호 방식**을 확정했다. random DB key를 평소 비밀번호와 기관 복구 비밀번호로 각각 독립 wrap하며, 기관 비밀번호로 평소 비밀번호를 재설정한다.
- 자동 백업도 확정했다. 위치는 사용자가 선택하며, 앱은 본체와 다른 드라이브/분리된 위치를 권장한다. 현재 구현은 revision 저장 직후 단일 `.mealbackup`을 만들고 평소 비밀번호 또는 기관 복구 비밀번호로 검증 복원한다. **같은 DB의 최신 100개**만 보관하며, 새 백업 검증 후 오래된 정상 백업만 삭제한다. 다른 DB·손상 파일은 자동 삭제하지 않는다.
- 생성된 청구서·확인서 Excel bytes는 자동 `.mealbackup`에 넣지 않는다. DB에는 document run의 원본 변경번호, 계산·템플릿 manifest, 파일명·크기·SHA-256만 남기고 과거 snapshot에서 다시 생성한다. DB와 백업을 모두 잃고 출력 파일만 남은 경우 전체 운영 DB 복구는 불가능하며, 출력 뒤 수동으로 고친 Excel 내용도 재생성하지 않는다.
- 공통 Excel 비밀번호 원문은 암호화 DB의 versioned `export_password_setting`에만 저장하고 QSettings·document run·로그에는 남기지 않는다. 공개 manifest는 설정 version과 revision ID만 기록한다. 암호화 bytes는 매번 달라질 수 있어 당시 파일의 `output_sha256`과 별도로 평문 workbook `content_sha256`을 재생성 비교에 사용한다.
- 새 DB는 설정 완료 직후 **기존 Excel 명단 가져오기를 강제하는 1번 흐름**으로 확정했다. `파일 선택 → 열 연결 확인 → 반영 전 미리보기 → 최종 검토`가 끝나기 전에는 빈 명단 화면을 열지 않는다. 중간 취소 시 가짜 revision을 만들지 않고 DB와 초기 백업만 남겨 다음 실행에 가져오기부터 다시 안내한다.

## 2026-08-31 구현 상태

- `src/importing/workbook.py`가 일반/암호 Excel을 구조 검사하고 선택 열만 typed observation으로 만든다. 헤더 행, 데이터 시작 행, 열 문자 매핑, 주소 1·2순위, split/combined 식종, 식종 참/거짓 값, 날짜·전화 정규화와 무시할 열을 지원한다.
- 가져오기 화면은 Excel 비밀번호 확인·시트/표 검사와 최종 명단 미리보기 생성 동안 작업을 UI thread 밖에서 실행한다. 창은 계속 반응하며 하단에 단계별 제목, 설명과 진행 막대를 표시하고 입력·이동 버튼은 완료될 때까지 잠근다.
- 가구 기준은 체크 가능한 버튼으로 표시하며 선택 시 파란 바탕에 직접 그린 굵은 흰색 체크를 쓴다. 닫힌 dropdown은 마우스 휠로 값이 바뀌지 않는다.
- `반`은 새 가져오기, 명단 표와 직접 정보 수정 UI에서 숨겼다. 도메인 필드는 기존 DB 호환을 위해 유지하며 다른 값을 수정해도 과거 `반` 값은 보존된다. 제공기관 직접 입력은 `import_profile.provider_value`로 version 저장되고 모든 observation에 동일하게 적용된다.
- `import_profile`은 암호화 DB의 generic audit entity로 저장한다. 이름, stable UUID, version, mapping manifest와 header fingerprint가 revision history에 남고 version은 1씩 증가한다.
- 빈 DB reconciliation은 `None` 기준 상태를 명시적으로 지원한다. 최초 import 승인 시 사람·식종·profile·batch를 parent 없는 첫 revision 하나로 저장한다.
- `RosterImportWizard`, `GuidedInitialImportController`, `DesktopStartupController`가 첫 실행 경로를 연결한다. 취소하면 onboarding에 남고, 승인/자동 백업 뒤에만 `RosterWindow`를 만든다.
- `src/importing/households.py`가 versioned 가구 묶기 rule, AND matching, 빈칸 제외, 제안·분리 결정과 적용일이 있는 membership을 담당한다. 첫 가져오기 안의 신규 사람뿐 아니라 적용일에 유효한 기존 가구 구성원도 같은 rule로 비교한다.
- 기존 가구가 하나만 일치하면 기본 후보로 제안한다. 둘 이상이면 자동 연결하지 않고 구성원 이름과 일치 근거를 최종 검토에 표시해 반드시 하나 또는 별도 가구를 선택하게 한다. 종료된 household membership은 후보에서 제외한다.
- 열 연결 화면에서 가구 기준을 복수 선택하고 최종 검토의 `가구 처리` 열에서 `같은 가구로 묶기`, `기존 가구에 포함`, `이 사람만 별도 가구` 중 해당 처리를 고른다. rule·결정·가구·membership은 사람·식종·import profile과 같은 revision에 저장된다.
- `GuidedRosterImportController`와 명단 화면의 `Excel 명단 가져오기` 버튼이 후속 import를 두 번째 이후 revision과 자동 백업으로 저장하고, 승인 직후 새 roster를 화면에 교체한다.
- 후속 import 인적사항 update policy version 1은 기존 사람의 매핑된 빈 학교·주소·전화 등을 삭제로 해석하지 않고 DB 값을 유지한다. 빈칸은 최종 검토의 변경 목록에서도 제외하고, 정책 version과 observation별 실제 보존 필드는 revision/import batch 결정에 남긴다. 신규 사람의 빈칸과 식종 flag 해석은 별도 계약이다.
- 명단 inspector의 `정보 수정`은 표준 인적사항을 직접 편집한다. 기존 값이 있는 칸을 비우면 노란 `삭제 예정` 안내와 최종 확인을 거친다. 변경·삭제 필드는 `person_profile_edit_policy_version=1` UI revision에 남고, 가구·식종·배송 배정·사용자 정의 값은 유지된다. 저장 직후 자동 백업하며 복원 뒤 빈 값과 감사 결정까지 검증한다.
- 명단 우클릭의 `배송·일시중지·중지`는 다중 식종 변경 화면을 연다. 조식·중식·석식 중 여러 개에 같은 상태·기간을 한 revision으로 적용한다. 미가입 식종은 `배송`일 때만 요청 기간을 덮는 enrollment를 자동 추가하고, `일시중지·중지`는 차단한다. 식종별 현재→변경 후 요약, raw status event, 자동 가입 결정, 자동 백업과 복원을 검증했다.
- schema version 3은 UUID와 별도로 immutable `revision_display_code`를 저장한다.
  schema 2 DB를 열면 기존 revision을 한국 기록일·ledger 순서로 backfill하고 transaction
  안에서 자동 변환한다. 명단 화면은 시작과 모든 저장 후 `변경번호`를 갱신하며
  자동 백업 복원 뒤에도 같은 번호가 유지된다.
- schema version 4는 roster revision을 증가시키지 않는 append-only `document_run`을
  저장한다. 청구서·확인서 종류, source UUID·변경번호, 대상 기간, 생성 시각·사용자,
  계산·템플릿 manifest, 결과 파일명·크기·SHA-256을 남긴다. update/delete를 막고
  source 봉인·변경번호 일치를 검사하며 저장 직후 자동 백업하고 복원 후 재검증한다.
- schema version 5는 기존 document run을 보존하면서 허용 문서 종류에
  `delivery_report`를 추가한다. schema 4 DB를 transaction 안에서 재구성하고 기존
  append-only·source 봉인 trigger를 다시 적용한다.
- `document_source_snapshot(run_id)`는 나중 revision이 추가된 뒤에도 document run이
  참조한 과거 상태만 다시 재생한다. `.mealbackup` archive는 DB·credentials·manifest
  세 항목만 허용해 생성 Excel bytes가 섞이지 않는 계약을 테스트로 고정했다.
- `meal_unit_price_schedule` policy version 1은 식종별 기본 10,000원과 raw 적용일
  change 이력을 revision snapshot에 저장한다. 같은 작업에서 여러 식종에 같은 단가를
  적용할 수 있고, 적용일이 가장 늦은 변경·같은 날이면 나중 기록을 사용한다. 명단
  상단 `단가 관리` dialog는 현재 단가, 새 단가, 적용일, 변경 전후와 최근 이력을
  보여주며 저장 뒤 변경번호 갱신과 자동 백업을 실행한다.
- `price_entitlements()`는 서비스일마다 source revision의 단가를 적용해 사람·식종·
  단가별 식수와 소계를 만든다. 월 중간 변경도 총 식수와 단가별 근거를 함께 보존하며,
  중복 entitlement를 차단한다. calculation manifest에는
  `unit_price_mode=service_date_effective`, 청구 기간, 단가별 식수·소계·총액과 전체 비고를
  남기고 계산에 쓴 schedule과 다른 단가표 또는 기간 밖 서비스일로 manifest를 만들면
  실패한다.
- `export_password_setting` policy version 1은 공통 Excel 비밀번호와 설정 version,
  변경 revision ID를 암호화 ledger에 저장한다. 명단 화면 `설정` dialog는 기존 값을
  처음에는 가린 채 표시하고 명시적인 `현재 비밀번호 보기`와 `복사`만 제공한다. 보기는
  30초 뒤 다시 가리고, 복사한 값도 클립보드가 바뀌지 않았을 때만 30초 뒤 지운다. 과거
  비밀번호 version은 일반 UI에 나열하지 않는다. 새 값·재입력·보기 토글로 변경하면 저장
  뒤 변경번호와 자동 백업을 갱신한다. 공개 결정에는 비밀번호 없이 설정 여부와 version만
  남긴다.
- `publish_workbook`은 비밀번호가 있으면 평문 workbook을 메모리에만 두고 암호화 staging을
  검증한 뒤 원자 교체한다. 청구서·확인서·검토·배송 리포트 writer는 선택적 출력
  비밀번호 경계를 공유하고, SQLite v2에서는 미설정 상태의 무암호 출력을 차단하는
  domain 계약을 추가했다.
- `meal_service_adjustment_schedule` policy version 1은 현재 DB의 휴무 기간·식종·사유와
  `정상 운영/미제공/대체식` 선택을 versioned ledger에 저장한다. 명단 상단
  `휴무·공휴일` 허브는 `holidays==0.102` 한국 달력을 인터넷 없이 연도별 후보로 표시하고,
  날짜 체크박스·`미검토 전체 선택`·현재 검토 상태를 제공한다. 후보 표시만으로는 DB를
  바꾸지 않는다. 사용자가 공휴일 N개, 식종 N개와 처리 방법 하나를 명시하면 날짜별
  schedule 변경 N개를 만들되 사용자 작업은 `AuditChange`·revision·자동 백업 각 하나로
  남긴다. revision decision의 `public_holiday_reviews`에는 각 날짜·이름·provider/version과
  식종별 결과를 보존한다. 일부 식종만 확정하면 나머지는 미검토로 표시하고, 이미 검토한
  칸을 다시 선택하면 새 결정이 우선하되 이전 이력이 유지된다는 경고를 보여준다. 업체 자체
  휴무는 직접 추가하며 전체 식종과 하루를 기본으로 열고 기간·일부 식종을 허용한다.
  대체식은 선택한 공휴일을 모두 제외한 직전 운영일을 제안하며 날짜×식종 표의 모든 실제
  제공일을 바꿀 수 있다. 미제공만 entitlement와 청구에서 빠지고 대체식은 원래 서비스일
  청구를 유지한다.
- `delivery.breakfast-policy` version 1은 금요일 조식 휴무 때 기본
  `목요일_토일배송` 또는 `금토일_미배송`을 업체 DB별 적용일 이력으로 저장한다. 설정 허브의
  큰 두 선택 카드가 실제 결과 예시와 과거 불변 안내를 보여준다. 저장은 변경번호와 자동
  백업을 만들며, historical 계산은 source snapshot에 유효한 rule을 사용하고 명시적 대체식
  실제 제공일을 우선한다.
- 청구월 안에서 한 식종의 단가가 실제로 둘 이상일 때만 문서 전체용 단가 비고를 만든다.
  적용일이 1일이어서 그달 전체 단가가 하나뿐이면 전월과 금액이 달라도 비고가 없다. 같은
  일정으로 바뀐 식종 여러 개는 한 문장으로 묶고 사람별 원본 비고와는 분리한다.
- `DocumentSourceDialog`는 최신 ledger revision을 기본 선택하고 평소에는 과거 목록을
  숨긴다. `이전 변경번호로 만들기`를 누른 뒤 행을 직접 골라야 source가 바뀌며,
  과거 선택은 이후 수정이 빠진다는 노란 안내와 `최신으로 되돌리기`를 보여준다.
  `DocumentHubDialog`는 명단 상단의 `문서 만들기` 단일 버튼에서 열리고, 청구서·확인서를
  `정산 문서`, 배송 리포트·배송대타를 `배송 문서`로 묶어 큰 선택 카드로 보여준다. 문서
  종류를 고르면 변경번호 선택까지 이어진다. `DocumentPeriodDialog`는 실제 오늘이 아니라
  명단 화면에서 보고 있던 날짜를 기준으로 청구서·확인서에는 해당 월 전체, 배송 리포트와
  배송대타에는 해당 하루를 제안한다. 월은 다른 연·월로, 리포트는 시작~종료 범위로,
  배송대타는 다른 하루로 바꿀 수 있다. 결과는 source와 기준일을 포함한 typed
  `DocumentPeriodSelection`으로 전달한다. 이어지는 `DocumentMealDialog`에서 청구서·확인서·
  배송 리포트는 조식·중식·석식을 기본 전체 선택하고 하나 이상을 남기는 범위에서 해제할
  수 있다. 배송대타는 명단 화면 식종 하나를 기본 선택하며 radio 방식으로 정확히 하나만
  허용한다. 확정값은 source·기간을 포함한 typed `DocumentMealSelection`으로 전달한다.
  이어지는 `DocumentOutputDialog`는 이 컴퓨터에 기억한 기본 문서 폴더를 제안하고,
  `이번만 다른 폴더`는 기본값을 바꾸지 않는다. 기본 폴더가 없거나 사라졌으면 생성을 막고
  새 기본 폴더를 요구한다. 확정값은 앞의 모든 입력과 기존 폴더·사용 방식을 포함한 typed
  `DocumentOutputSelection`으로 전달한다. `SettingsHubDialog`는 설정 버튼에서 Excel
  비밀번호, 업체 DB별 `급식/행복도시락` 양식, 조식 배송 규칙과 문서 저장 폴더를 분리한다.
  비밀번호·양식·조식 규칙은 versioned DB revision과 자동 백업을 만들고, 문서 폴더는
  QSettings에 경로만 저장해 DB revision·자동 백업을 만들지 않는다. 파일명은 기간·문서 종류·일부 선택 식종·source
  변경번호로 자동 생성한다. 전체 식종이면 식종을 생략하고, 같은 이름이 있으면 기존 파일은
  두고 `_재발행02`, `_재발행03` 순서로 비어 있는 이름을 선택한다. 최종 파일명과 재발행
  번호도 `DocumentOutputSelection`에 포함한다. `GeneratedDocumentResult`는 실제 출력 파일이
  존재하고 비어 있지 않으며 기록한 크기·SHA-256과 현재 bytes가 일치할 때만 성립한다.
  `DocumentCompletionDialog`는 정확한 파일명·종류·기간·식종·source 변경번호·생성 시각·크기·
  폴더를 발행 전표 형태로 보여주되 Excel이나 탐색기를 자동으로 열지 않는다. 사용자가
  `파일 열기`, `폴더 열기`, `닫기`를 직접 고르고, 열기 실패는 dialog를 닫지 않은 채 같은
  화면에 표시한다. 명단 창의 `document_generated` 신호는 `document_output_selected`와 분리해
  미검증 결과로 완료 화면을 열 수 없게 했다. 청구서·확인서는 실제 generator까지 연결됐다.
  선택한 source snapshot의 명단·가입·상태·학교 일정·휴무·단가를 공유 entitlement 계산으로
  처리하고, 현재 DB의 양식·Excel 비밀번호로 파일을 발행한 뒤 append-only document run과
  자동 백업을 저장한다. 생성 파일만 성공하고 run 저장이 실패하면 방금 만든 파일을 지운다.
- 배송 리포트는 선택한 source snapshot의 가입·상태·학교 일정·휴무·대체식·조식 policy를
  공유 계산해 **실제 배송일 범위**의 날짜×식종 시트를 만든다. 담당자별 안정적 가구 ID로
  1~5인 가구와 총 인원을 집계하고 같은 식종의 같은 연도 직전 실제 배송일 대비 증감을
  표시한다. 담당 누락·한 배송의 가구 충돌·6인 이상 가구는 템플릿 한계를 조용히 넘기지
  않고 오류로 막는다. 고정 report template과 현재 공통 Excel 비밀번호로 암호화 발행하며
  계산·policy·template·평문/암호문 hash를 `delivery_report` run에 저장하고 자동 백업·복원을
  통합 검증했다.
- `src/importing/school_calendar.py`는 학교명·시작일·시작 O/X·종료일·종료 O/X·비고 열을
  자동 제안한다. 날짜 셀과 연도 생략 날짜를 읽고 실제 병합 학교명만 forward-fill한다.
  `SchoolCalendarReviewDialog`에서 학교별 학교명·두 날짜·두 O/X와 전체 검토 기간을 수정하며,
  자연어 비고는 반영 안 함/업체 배송(X)/학교 급식(O)을 명시하기 전까지 저장을 막는다.
  검토 결과는 versioned 학교 일정, import batch, revision 하나와 자동 백업 하나로 저장된다.
- `src/domain/school_calendar_edits.py`와 `SchoolCalendarEditDialog`는 최신 DB의 학교별 평일
  rule을 같은 값의 기간으로 묶어 다시 연다. 학교 목록에서 기존 구간의 시작일·종료일·O/X·
  학년 예외를 수정하거나 새 기간을 지정하고, 여러 학교 변경을 저장 전까지 임시로 모으며
  학교별 원상복구를 지원한다. 실제로 달라진 평일만 요약해 import batch 없는 UI revision
  하나와 자동 백업 하나로 저장한다. 저장 일정이 없거나 화면에서 요청하면 기존 Excel
  가져오기로 이어진다.
  반복 계산 policy v1은 조식=가입된 모든 달력일, 중식=학교 일정이 허용한 평일,
  석식=평일이며 status·휴무·대체식·서비스일 유효 단가를 같은 source snapshot에서 적용한다.
- legacy `main.py`는 유지한다. 새 흐름의 별도 실행 진입점은 `python -m src.desktop_v2`다.
- 실제 PySide6 6.11.1 + apsw-sqlite3mc 3.53.3.1 + holidays 0.102 환경 전체 검증은
  `652 passed, 5 warnings in 52.21s`였다. 경고 5개는 기존 openpyxl 비문자 헤더 fixture
  경고다. 배송 리포트·조식 policy UI·schema 5·자동 백업 복원과 이번 가져오기 UI 수정까지 포함한다. 사용자가 기능
  검토 뒤 빌드하기로 했으므로 새 one-file 생성은 중단했다. 현재
  `dist/meal_delivery_sqlite_v2.exe`의 73,939,294 bytes 빌드(SHA-256
  `64c50906106c41521438465b49dae317130a8e48888da1edbcf129e0415b9321`)는 이번 배송 리포트·
  조식 policy 변경 전 산출물이며 배포 후보로 취급하면 안 된다.
- 문서 이력과 source 선택 정책은 구현했다. 과거 상태 변경 때 별도 경고나
  `재생성 필요` 표시는 만들지 않고, 기존 문서는 원래 source revision에 둔 채 정정
  문서를 새 run으로 추가한다. 새 문서는 최신 revision이 기본이고 이전 목록은 요청할
  때만 펼친다. 생성 파일 bytes는 백업하지 않고 metadata/hash로 재생성하는 정책도
  확정했다. 단가표 DB 저장, 월 중간 서비스일별 분할 계산과 조건부 전체 비고 생성도
  구현했다. 전체 비고는 새 행이나 별도 시트를 만들지 않고 각 출력 시트의 데이터 시작
  직전 **소계 3행의 기존 `비고` 셀**에 넣기로 확정했다. writer는 선택적 `global_note`를
  모든 생성 시트의 해당 셀에 쓰며 사람별 데이터 행의 원본 비고는 그대로 보존한다.
  문구는 연월과 별도 계산 설명을 생략한
  `중식 단가: 1~14일 10,000원 / 15~30일 11,000원` 형식이다.

## 권장 내부 모델

최소한 다음 개념을 분리한다.

- person과 hidden UUID
- household와 membership
- meal enrollment
- raw status event와 derived status interval
- person/meal driver assignment와 delivery order
- meal entitlement date
- physical shipment event와 bundle membership
- school calendar와 reviewed override
- complaint, custom field definition/value
- import profile, import batch, staged observation, reconciliation decision
- immutable revision/audit entry와 historical snapshot
- export profile과 document run
- backup/recovery metadata

Excel Adapter는 typed observation만 만들고, identity·진급·충돌·적용일 판단은 reconciliation 계층이 담당한다. split/combined는 DB 저장형이 아니라 입출력 projection이다.

## 권장 구현 순서

1. Python/PySide6/암호화 SQLite 패키징 spike를 만들고 Windows 배포 가능성을 검증한다.
2. 기존 코드를 삭제하지 않은 채 새 패키지 구조와 domain contract를 추가한다.
3. revision ledger, historical snapshot, person/household/meal/status schema를 migration과 함께 만든다.
4. 상태 사건→유효 기간, entitlement, 조식 묶음배송, 중식 일정 규칙을 순수 domain 테스트로 고정한다.
5. 조사표/운영명단/school-calendar Adapter와 import profile·staging/review를 구현한다.
6. 명단/상태/이력/민원/가구 UI를 구현한다.
7. 기존 Excel 템플릿 출력과 split/combined export를 revision snapshot에 연결한다.
8. 배송 리포트·확인서·청구서는 같은 snapshot/entitlement에 연결했다. 배송대타를 같은
   실제 배송 projection에 연결한다.
9. 암호화, 로그인, 백업, 기관 복구, 설치 패키지와 비개발자용 복구 절차를 검증한다.
10. 기존 앱과 golden workbook 비교가 통과한 뒤에만 legacy 경로 제거를 검토한다.

## 다음 작업자가 먼저 확인할 것

- 이 handoff와 `docs/annual-delivery-workbook.md`를 읽는다.
- `git status --short --branch`가 예상 상태인지 확인한다.
- `python -m pytest -q tests`로 기준점을 재검증한다.
- 패키지를 설치하기 전에 Python 3.14에서 PySide6 및 `apsw-sqlite3mc` 배포 가능성을 조사한다.
- architecture와 DB migration/security/recovery 같은 계약은 main agent가 유지하고, 단순 구현 분할만 독립 작업으로 위임한다.
- commit, push, PR, merge는 각각 별도 사용자 권한으로 취급한다. 현재 push 권한은 없다.

## Suggested skills

- `chinus-manager`: 작업 범위, checkpoint, 검증과 Git 권한을 관리한다.
- `improve-codebase-architecture`: 기존 Excel 책임을 Adapter와 깊은 domain Module로 분리할 때 사용한다.
- `frontend-design`: 명단·상태·이력·민원 GUI의 정보 구조와 시각 설계를 구체화할 때 사용한다.
- `subagent-driven-development`: 승인된 구현 계획을 독립적인 비충돌 작업으로 나눠 같은 세션에서 실행할 때만 사용한다.
- `caveman-commit`: 사용자가 로컬 commit을 승인한 뒤 간결한 Conventional Commit 메시지를 만들 때 사용한다.
