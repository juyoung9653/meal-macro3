# 암호화 SQLite 저장소 계약

이 문서는 새 SQLite 데스크톱 앱의 현재 저장소 Interface와 아직 구현하지 않은
보안 범위를 구분한다. 기존 Excel 앱의 계약은
[연간 배송 Excel 형식](annual-delivery-workbook.md)에 남아 있다.

## 현재 구현

[EncryptedDatabase](../src/storage/encrypted_database.py)는
`apsw-sqlite3mc`를 감싸는 유일한 운영 DB Adapter다.

- `create`는 기존 파일을 절대 덮어쓰지 않는다.
- `open`은 없는 파일을 새 DB로 만들지 않는다.
- cipher는 SQLite3 Multiple Ciphers의 권장 기본인 `chacha20`을 명시한다.
- key는 32-byte random DB key만 받는다. 사용자 비밀번호 문자열은 이
  Interface에 전달하지 않는다.
- cipher와 `hexkey`는 어떤 schema SQL보다 먼저 설정한다.
- 일반 APSW처럼 `apsw.mc_version`이 없는 runtime은 평문 DB를 만들지 않고
  실패한다.
- 평문 `SQLite format 3` 헤더, 다른 application ID, 지원 범위 밖 schema version,
  migration checksum 불일치, 잘못된 key 또는 손상 파일을 거부한다. schema 2 DB는
  transaction 안에서 schema 3, 4, 5, 6을 순서대로 적용해 현재 version으로 자동 변환한다.
- foreign key, trusted schema off, secure delete, memory temp store, 5초 busy
  timeout을 연결마다 설정한다.
- 한 PC에서의 단순 복구를 우선해 현재 journal mode는 `DELETE`다.

저수준 Adapter는 key를 객체 필드로 보관하지 않는다. 다만 자동 백업을 담당하는
`DatabaseSession`은 앱이 열린 동안 key를 `bytearray`로 보관하고 닫을 때
best-effort로 지운다. Python과 SQLite wrapper 내부의 모든 복사본까지 지우는
메모리 zeroization을 보장하는 Interface는 아니다.

## 운영 파일 비밀번호

새 운영 파일은 비밀번호 하나로 만들고 로그인·백업 복원에 같은 비밀번호를 사용한다.
[DatabaseCredentials](../src/storage/database_credentials.py)는 DB마다 생성한
32-byte random key를 비밀번호로 wrap한다.

- 새 파일의 `user` slot은 프로그램을 열 때 입력하는 비밀번호를 사용한다.
- 기존 파일의 `recovery` slot은 저장 형식 호환을 위해 보존한다. 앱에서는 기관
  복구 입력·비밀번호 재설정 경로를 제공하지 않으며 일반 비밀번호로 연다.
- 각 slot은 random 16-byte salt와 scrypt KDF를 사용하고, random 12-byte
  nonce의 AES-256-GCM으로 DB key를 인증 암호화한다.
- credential JSON에는 비밀번호와 DB key의 평문이 저장되지 않는다.
- 비밀번호 변경은 key wrapping을 교체하며 명단·이력·DB 본문을 다시 암호화하지 않는다.
- 빈 비밀번호는 허용하지 않으며 복잡도 조합 규칙은 강제하지 않는다.

credential JSON은 DB 옆의 앱 관리 sidecar다. 사용자가 별도 복구 파일로 복사하거나
짝을 맞출 필요는 없으며 자동 백업에 DB와 함께 들어간다. 일반 QSettings에는 DB와
백업 폴더 경로만 저장하고 비밀번호는 저장하지 않는다.

## 자동 백업과 복원

[DatabaseBackupService](../src/storage/database_backup.py)는 열린 DB에 APSW online
backup을 적용해 일관된 암호화 snapshot을 만든다. 백업 하나는 사용자가 다루기
쉬운 단일 `.mealbackup` 파일이며 내부에는 다음 세 항목만 있다.

| 항목 | 내용 |
|---|---|
| `database.sqlite3` | 원본과 같은 random key로 암호화한 SQLite snapshot |
| `credentials.json` | 비밀번호로 wrap한 key 정보(기존 파일은 복구 slot 포함) |
| `manifest.json` | 생성 시각, schema/revision, 파일 크기와 SHA-256 |

생성한 청구서·확인서 Excel bytes는 `.mealbackup`에 넣지 않는다. DB 안의
`document_run`에는 파일명·크기·SHA-256과 원본 변경번호·계산·템플릿 manifest만
남는다. SHA-256은 다시 만든 파일이 당시 출력과 같은지 확인하는 값이지, hash만으로
파일이나 DB를 복원하는 수단은 아니다.

`DatabaseSession.append_revision()`은 DB commit을 먼저 확정하고 그 직후 자동
백업을 만든다. 백업 드라이브가 빠진 경우에도 확정된 DB 변경을 되돌리거나 실패한
것처럼 취급하지 않고, `AutomaticBackupResult`로 “저장 성공, 백업 실패”를 따로
보고한다. 시작 설정도 즉시 백업을 한 번 생성해 경로를 확인한다.

`설정 → 자동 백업 폴더`에서 백업 위치를 변경할 수 있다. 새 폴더에 검증된 백업을
생성한 뒤 경로 설정을 저장하고 열린 세션에도 바로 적용한다. 설정 저장에 실패하면
기존 위치를 유지한다. 운영 파일 경로는 바뀌지 않으며 기존 백업은 원래 폴더에 남는다.
폴더 변경을 확인하는 첫 백업은 보관 개수 정리를 하지 않는다.

운영 파일 비밀번호를 바꾸면 설정된 자동 백업 폴더의 같은 `database_id`에 속하는
검증된 과거 백업의 사용자 key wrapper도 새 비밀번호로 바꾼다. 백업 안의 암호화 DB
bytes, 생성 시각, schema와 revision 정보 및 기존 복구 slot은 그대로 둔다. 각 백업은
원래 DB 무결성을 확인한 뒤 임시 archive 전체를 검증하고 원자적으로 교체한다.
갱신 실패 파일과 손상·확인 불가 파일은 보존하고 결과에 표시한다. 비밀번호 변경
직후 만드는 새 백업에서는 보관 개수 정리를 건너뛰어 실패 파일을 삭제하지 않는다.
폴더 밖의 복사본과 갱신 실패 백업은 변경 전 상태로 보존한다. 마지막으로 해당
백업을 열던 비밀번호는 파일마다 다를 수 있다.

복원은 archive 구성과 SHA-256을 확인하고, 실제 비밀번호로 DB를 연 뒤 SQLite,
revision ledger, typed projection 무결성 검사를 모두 통과해야 완료된다. 운영 파일
비밀번호로 복원하며, 앱에서는 기관 복구 비밀번호를 사용하는 복원 경로를 제공하지 않는다. 기존 DB나 credential 파일은 덮어쓰지 않는다.

기존 운영 파일은 `설정 → 운영 데이터 위치`에서 새 `.sqlite3` 경로로 옮길 수 있다.
열린 세션에서 즉시 online 백업을 만들고 사용자 비밀번호로 새 경로에 복원·검증한 뒤에만
QSettings의 DB 경로를 바꾼다. 원본 DB와 credential sidecar는 보존하며 새 경로의
기존 DB나 sidecar는 덮어쓰지 않는다. 성공하면 운영 창을 종료하고 다음 실행부터
새 경로를 사용해 복사 시점 이후 원본만 변경되는 일을 막는다. 백업 폴더 설정은 유지한다.

자동 백업은 `database_id`별 최신 100개를 보관한다. 새 백업의 archive·hash·DB
무결성 검증이 모두 끝난 뒤에만 100개를 초과한 가장 오래된 정상 백업을 삭제한다.
같은 폴더에 있는 다른 DB의 백업과 형식을 확인할 수 없는 손상·임의 파일은
정리 대상에 넣지 않는다. 오래된 파일이 다른 프로그램에서 열려 있어 삭제하지
못해도 새 백업은 유지하며 다음 백업 때 다시 정리한다. schema 2→현재 version 변환은
원본 revision을 고치지 않는 additive migration이고 실패하면 transaction 전체를
rollback한다. migration 실행 전 별도 `.mealbackup` 생성은 아직 추가하지 않았다.

## Revision ledger와 typed projection

schema version 1은 다음 append-only ledger 테이블을 만든다.

| 테이블 | 역할 |
|---|---|
| `schema_migration` | 적용 migration 이름과 SHA-256 checksum |
| `revision` | 기록 시각, 적용일, Windows 사용자명/SID, 사유, 입력 종류, 사용자 결정, rule version |
| `audit_change` | entity별 작업과 전체 `before`/`after` JSON |
| `import_batch` | 파일명만 저장한 source hash·크기·sheet/header fingerprint·mapping manifest·적용일·결정 |
| `revision_seal` | revision의 변경 개수와 import batch 유무를 확정하는 봉인 |

schema version 2는 화면의 현재 상태 조회를 위한 projection을 추가한다.

| 테이블 | 역할 |
|---|---|
| `person_projection` | 표준 인적사항과 typed custom value의 현재 상태 |
| `custom_field_definition_projection` | text/number/date/choice 사용자 정의 열 정의 |
| `household_projection` | 전화번호와 분리된 명시적 가구 ID |
| `household_membership_projection` | 적용 기간이 있는 사람-가구 소속 |
| `meal_enrollment_projection` | 사람·식종별 가입 기간 |
| `delivery_assignment_projection` | 사람 공통 및 식종별 배송담당·순서 override |
| `status_event_projection` | 수정하지 않고 보존하는 원문 상태 사건 |

schema version 3은 내부 UUID를 바꾸지 않고 사람이 읽을 수 있는 변경번호를 추가한다.

| 테이블 | 역할 |
|---|---|
| `revision_display_code` | 한국 기록일과 그날의 저장 순번으로 만든 고정 변경번호 |

변경번호 형식은 `YYYYMMDD-001`이다. 날짜는 `effective_on`이 아니라 revision을
실제로 기록한 시각의 한국 날짜다. 같은 날짜에 저장할 때마다 `001`, `002`처럼
증가하며 DB가 `BEGIN IMMEDIATE` transaction 안에서 부여한다. 화면과 문서에는 이
번호를 보여주지만 관계·snapshot 조회·문서 원본 연결에는 충돌하지 않는 UUID를 계속
사용한다. schema 2에서 올라온 기존 revision도 한국 기록일과 원래 ledger 순서로
번호를 한 번 부여하고 이후 바꾸지 않는다.

schema version 4는 명단 revision과 분리된 문서 생성 이력을 추가한다.

| 테이블 | 역할 |
|---|---|
| `document_run` | 성공한 청구서·확인서·배송 리포트 파일의 원본 revision, 기간, 생성자와 재현 manifest |

문서를 여러 번 만들어도 roster revision이나 화면의 변경번호는 증가하지 않는다.
`document_run` 자체의 update/delete는 trigger가 거부하며, 원본 revision이 봉인되어
있고 저장된 변경번호가 실제 원본과 일치할 때만 insert할 수 있다.

schema version 5는 기존 `document_run` 행과 append-only trigger를 보존하면서 허용
문서 종류에 `delivery_report`를 추가한다. 새 DB와 schema 4에서 올라온 DB 모두 청구서·
확인서·배송 리포트 run을 같은 무결성 계약으로 저장한다.

schema version 6은 `household_projection`에 식종별 제공 요일·배송요일·자유 입력 배송 형태를
저장하는 `delivery_settings_json`을 추가한다. 기존 가구는 빈 설정으로 시작하며
식종 기본값을 따른다. 가구 설정 변경도 기존 household audit entity와 revision으로
기록하므로 과거 snapshot과 암호화 자동 백업·복원에 함께 포함된다.
`delivery_weekdays`는 월~일 제공일에 대응하는 실제 배송요일 7개(월=0~일=6)이며,
필드가 없으면 식종 기본 배송 규칙을 상속한다. 기존 설정도 그대로 읽는다.

schema version 7은 `person_projection.delivery_settings_json`으로 개인별 배송 설정을
보존한다. 개인 설정이 없으면 기존 가구 설정을 읽어 이전 파일의 동작을 유지한다.
개인 설정의 빈 배열은 가구의 별도 설정 대신 운영 기본값을 사용한다는 뜻이다.
개인 설정은 사람 audit state에도 기록하며, 같은 가구 전체 적용은 조회일의 가구원
개인 설정을 하나의 revision에서 함께 변경한다. 취소·변경 없음은 revision을 만들지 않는다.

projection은 원장이 아니다. 현재 화면은 projection을 읽을 수 있지만 과거 명단과
모든 출력은 선택한 revision의 audit state를 재생해 생성한다.

각 revision은 현재 최신 revision만 parent로 가질 수 있다. 첫 revision은 parent가
없다. Adapter는 한 transaction 안에서 다음 순서로 기록한다.

```mermaid
flowchart LR
    P[현재 parent 확인] --> B[before와 현재 entity 상태 비교]
    B --> R[revision 기록]
    R --> D[표시용 변경번호 부여]
    D --> C[audit changes 기록]
    C --> T[typed projection 갱신]
    T --> V[ledger와 projection 비교]
    V --> I[선택적 import batch 기록]
    I --> S[revision 봉인]
    S --> M[commit]
```

projection write context는 봉인 전의 현재 revision 하나만 허용한다. context 밖에서
projection을 insert/update/delete하면 trigger가 거부한다. 표시용 변경번호가 없으면
revision을 봉인할 수 없다. 봉인 후에는 trigger가 revision, 표시용 변경번호, audit
change, import batch, seal의 update/delete와 추가 audit/import 행도 거부한다. DB key를
가진 공격자가 schema와 trigger 자체를 바꾸는 상황까지 막는 tamper-proof 원장은
아니지만, 앱 오류나 우회 SQL로 이미 확정한 revision을 고치는 동작은 fail-closed로
처리한다.

`AuditChange.before`와 `after`는 delta가 아니라 해당 entity의 전체 canonical
상태다. insert는 `after`만, update는 둘 다, delete는 `before`만 가진다. Adapter는
기록 직전의 상태와 `before`가 정확히 같은지 검사한다. 같은 revision에서 같은
entity를 두 번 바꿀 수 없고, 한 번 사용한 UUID는 delete 뒤에도 다시 insert할 수
없다.

`snapshot(revision_id)`는 첫 revision부터 대상 revision까지 봉인된 변경을
재생한다. 따라서 이후 명단·배송 리포트·청구서 exporter는 현재 mutable 상태가
아니라 명시적인 historical snapshot을 입력으로 받아야 한다.

`current_roster()`는 UI용 typed projection을, `roster_snapshot(revision_id)`는
선택한 revision의 재생 결과를 반환한다. DB open과 `check_integrity()`는 두 상태를
전부 비교한다. raw 상태 사건은 `status_events()`로 조회하고
`status_history()`가 겹침, 우선순위, 연속 기간 병합과 임시중지 추론을 계산한다.

## 과거 변경과 문서 이력 정책

과거 날짜의 상태를 나중에 고쳐도 이미 만든 청구서·확인서를 수정하거나
`재생성 필요`로 표시하지 않는다. 발행 당시 문서는 그때 선택한 source revision의
결과이고, 수정 내용을 반영한 문서가 필요하면 최신 revision을 선택해 새 document
run을 만든다. 예전 run이나 파일을 덮어쓰지 않는다.

`document_run`은 내부 source revision UUID, 사용자에게 보여줄 고정 변경번호, 대상
기간, 생성 시각·사용자, 계산 rule manifest, template/export profile manifest,
결과 파일명·크기·SHA-256을 append-only로 저장한다. 전체 개인 경로와 파일 비밀번호는
저장하지 않는다. `DatabaseSession.append_document_run()`은 저장 직후 평소와 같은
검증 자동 백업을 만들며 복원 뒤에도 run과 원본 revision 연결을 검사한다. 과거 문서
재현은 `document_source_snapshot(run_id)`로 이 UUID의 snapshot을 다시 읽으며 현재
projection을 암묵적으로 사용하지 않는다.

생성 Excel 공통 비밀번호는 `export_password_setting` audit entity에 versioned state로
저장한다. 원문은 암호화 DB와 암호화 백업 안에서만 존재하며 QSettings·document run·
로그·공개 manifest에는 복제하지 않는다. 공개 manifest에는 `protected=true`, policy
version, 설정 version과 설정을 바꾼 revision ID만 들어간다. 설정 저장은 일반 UI
revision으로 변경번호를 만들고 commit 직후 자동 백업한다. 비밀번호를 바꿔도 과거
revision의 값은 예전 파일을 다시 열거나 재현할 때만 사용할 수 있게 보존한다. 일반 설정
화면은 current state 한 개만 받아 처음에는 password echo로 가린다. 명시적인 보기 동작은
30초 뒤 다시 가리고, 복사 동작은 동일한 값이 클립보드에 남아 있을 때만 30초 뒤 지워
사용자가 이후 복사한 다른 내용은 보존한다. 과거 revision의 비밀번호 목록은 일반 UI에
전달하지 않는다.

`publish_workbook(..., password=...)`는 평문 OOXML을 메모리에서 만든 뒤 암호화한 staging
파일만 대상 폴더에 쓰고 암호 적용을 확인한 다음 `os.replace`한다. 저장·암호화·교체 중
하나라도 실패하면 기존 대상 파일을 보존하고 staging을 지운다. 비밀번호가 설정되지 않은
SQLite v2 출력은 `require_export_password_setting()`이 차단한다.

따라서 출력 Excel을 잃어도 DB 또는 `.mealbackup`이 남아 있고 해당 계산·템플릿
version을 앱이 계속 보유하면 원래 workbook 내용을 다시 만들 수 있다. 비밀번호 기반
OOXML 암호화는 매번 새 salt를 만들 수 있으므로 당시 암호화 파일의 `output_sha256`과
재생성 bytes가 달라도 template manifest의 평문 `content_sha256`으로 내용 일치를
확인한다. 반대로 DB와
백업을 모두 잃고 예전 청구서 파일만 남은 경우에는 전체 명단·상태·가구·감사 이력을
복구할 수 없다. 생성 뒤 Excel 셀을 수동으로 고친 내용도 DB에 기록되지 않으므로
재생성 대상이 아니다. 현재 청구서·확인서 writer는 생성에 사용한 template 파일 hash,
평문 content hash, 양식 설정 version과 비밀번호 설정의 공개 manifest를 document run에
남긴다. 과거 run을 다시 만드는 기능을 연결할 때는 같은 hash의 template과 당시 설정을
찾아야 하며, 평문 content hash가 다르면 일치 실패를 알려야 한다.

문서 원본 선택 화면은 가장 최신 ledger sequence를 기본으로 사용한다. 평소에는 최신
변경번호 카드만 보이며, 사용자가 `이전 변경번호로 만들기`를 눌러 과거 목록의 행을
직접 선택해야만 source가 바뀐다. 과거를 고르면 이후 수정이 포함되지 않는다는 안내를
표시하고 `최신으로 되돌리기`를 제공한다. 청구서·확인서는 선택한 historical snapshot의
명단·가입·상태·학교 일정·휴무·단가를 하나의 entitlement 계산으로 처리해 기존 template에
기록하고, 공통 비밀번호로 암호화한 뒤 document run과 자동 백업을 저장한다. 파일 발행 뒤
run 저장이 실패하면 그 작업에서 방금 만든 파일을 삭제해 반쪽 성공을 남기지 않는다.

배송 리포트도 선택한 historical snapshot에서 실제 배송일 기준으로 계산한다. 조식 묶음,
학교 중식, 상태, 휴무·대체식, 안정적 household ID와 배송담당을 함께 사용하며 source
revision과 계산·템플릿 manifest, 평문/암호문 hash를 `delivery_report` document run에
저장한다. 생성 성공 뒤에는 같은 자동 백업을 실행하고 복원 뒤 run과 source snapshot을
재검증한다.

업체 DB별 내장 양식 선택은 versioned `document_template_setting` entity로 저장한다. 현재
profile은 `급식` 또는 `행복도시락`이며 설정 변경은 revision과 자동 백업을 만든다. 첫
청구서·확인서 진입에서는 양식과 Excel 비밀번호를 먼저 확정한 뒤 revision 목록을 새로
읽는다. 계산 source는 사용자가 선택한 revision으로 봉인하되, 발행 비밀번호와 양식 선택은
생성 당시 현재 DB 설정을 사용하고 그 version을 run manifest에 남긴다.

학교 일정 원본은 별도 bytes로 저장하지 않는다. 파일명·SHA-256·크기·sheet/header·열 mapping과
명시적 비고 결정만 import batch에 남기고, 검토된 학교별 평일 rule은 versioned audit entity로
저장한다. Excel의 학교명·시작일·시작 O/X·종료일·종료 O/X·비고 열은 자동 제안하되 GUI에서
학교별 값을 수정하고 모든 자연어 비고를 결정한 뒤 revision 하나와 자동 백업 하나로 확정한다.

저장 일정 직접 수정은 원본 import batch를 고치거나 새 import batch를 만들지 않는다.
최신 snapshot의 `school-lunch.schedule`을 읽어 동일 rule의 연속 평일을 화면 구간으로만
접어 보여주고, 확정 시 달라진 학교 entity마다 전체 canonical state의 before/after를 가진
`UPDATE`를 기록한다. 작업 전체는 `source=ui` revision 하나이며, 결정 manifest에는 학교별
변경 평일 수·최초/마지막 변경일과 같은 O/X 전후가 이어진 변경 기간을 남긴다. 적용일은
가장 이른 실제 변경 평일이다. commit 뒤 평소 자동 백업을 실행하므로 원본 Excel이 없어도
DB 또는 검증된 `.mealbackup`에서 수정된 일정과 이전 revision을 모두 재생할 수 있다.

## Import 개인정보 최소화

명단 가져오기 화면에서는 파일에 정의된 Excel 표 중 하나를 선택한다. `명단` 표가 있으면
기본 선택하며, 없으면 첫 표를 선택한다. 표의 머리글과 데이터 범위를 자동으로 읽고
합계 행과 표 밖의 셀은 제외한다. 표가 없는 파일은 Excel에서 `삽입 → 표`로 명단 범위를
표로 만든 뒤 가져온다. 최초 가져오기에는 선택지가 하나뿐인 목적 항목을 표시하지 않는다.
적용일과 열 연결을 지정하고 `가져오기`를 누르면 내부 검사를 거쳐 바로 저장한다.
오류가 없으면 전체 명단 미리보기와 마지막 확인은 생략한다. 수정이 필요한 오류와
사람·가구 연결을 결정할 수 없는 항목만 별도로 해결하며, 취소하면 저장하지 않는다.

`ImportBatch`는 전체 로컬 경로를 거부하고 basename만 저장한다. SHA-256, 파일
크기, 시트명, 구조 fingerprint, mapping manifest, 적용일과 사용자 결정은
남기되 원본 workbook bytes와 사용하지 않은 개인정보 셀은 저장하지 않는다.

`import_profile`은 별도 평문 설정 파일이 아니라 같은 immutable ledger의 entity로
저장한다. 프로필 ID는 유지하고 version은 정확히 1씩 올리며, 헤더 행·데이터 시작
행·원본 열 연결·주소 후보 우선순위·식종 참/거짓 값·날짜/전화 정규화·무시할 열을
manifest에 남긴다. 전체 헤더 fingerprint가 정확히 같은 프로필이 하나일 때만 다음
가져오기에서 설정을 제안한다.

`household_matching_rule`도 같은 ledger의 versioned entity다. GUI에서 선택한
핸드폰·세대주 성명·주소·전화번호 기준, AND 방식, 빈칸 제외와 정규화 version을
저장한다. 자동으로 결정되거나 연결 오류 해결에서 선택한 observation별 household ID는 revision과
import batch 결정 manifest에 남는다. 사람 identity와 가구 소속은 별도 entity이므로
가구 분리는 사람 정보를 합치거나 삭제하지 않는다.

후속 import에서는 적용일에 활성인 기존 가구 구성원의 canonical profile도 같은
rule로 비교한다. 유일 후보는 기본 제안하고 복수 후보는 사용자 선택 전까지 commit을
막는다. 기존 household ID를 선택하면 household entity를 복제하지 않고 신규 사람의
적용일 기준 membership만 추가한다.

인적사항 update policy version 1은 기존 사람에게 연결된 observation의 매핑된 빈
셀을 삭제 명령으로 사용하지 않고 현재 canonical 값을 유지한다. 비어 있지 않은 셀만
갱신하며 신규 사람의 빈 셀은 `None`이다. `profile_update_policy_version`은 revision의
rule version에, 정책 이름과 실제 유지된 observation별 필드 목록은 revision과 import
batch의 결정 manifest에 함께 저장한다. 식종 flag 해석은 이 인적사항 정책과 분리한다.

사용자가 명단의 `정보 수정`에서 직접 저장한 값은 import와 분리된 `UI` revision이다.
기존 값이 있는 칸을 비우면 저장 전 삭제 예정 필드를 표시하고 확인을 받아야 하며,
`person_profile_edit` 결정에는 사람 ID, 전체 변경 필드와 실제 삭제 필드를 남긴다.
profile person entity 하나만 update하고 가구·membership·식종·배송 배정은 그대로
유지한다. `DatabaseSession.append_revision()`이 commit 직후 자동 백업하며, 복원한
백업에서도 빈 값과 해당 감사 결정이 함께 재생되어야 한다.

다중 식종 `배송·일시중지·중지`도 `meal_status_change_policy_version=1`인 UI
revision 하나로 저장한다. 식종별 raw `StatusEvent`는 각각 별도 append-only entity로
남기고, 미가입 식종의 배송 기간을 자동 가입시킨 경우 해당 meal enrollment 변경도
같은 transaction에 넣는다. 결정에는 식종 목록, 상태, 시작·종료일과 자동 가입 식종을
남긴다. commit 직후 자동 백업하며 복원 시 enrollment, raw event, 계산 history와 결정
manifest를 함께 검증한다.

식종별 단가는 `meal_unit_price_schedule` audit entity로 저장한다. entity가 아직 없는
revision에서는 조식·중식·석식 모두 10,000원인 policy version 1 기본값을 사용한다.
`단가 관리`에서 선택한 여러 식종, 새 정수 단가와 inclusive 적용일을 raw change 하나로
schedule에 추가하며, 전체 before/after state와 change ID·기록 시각을 revision ledger에
남긴다. revision metadata의 결정 manifest에는 식종 목록·단가·적용일을, rule manifest에는
`unit_price_policy_version=1`을 기록한다. 저장 직후 평소와 같은 자동 백업을 만든다.

단가 조회는 적용일이 가장 늦은 change를 고르고, 같은 적용일이면 schedule에 나중에
추가한 change를 고른다. 그래서 뒤늦게 기록한 과거 단가가 이미 존재하는 더 최근
적용일의 단가를 덮지 않는다. 문서 계산은 현재 설정이 아니라 source revision의
historical snapshot에서 schedule을 읽어야 한다.

`price_entitlements()`는 각 `MealEntitlement.service_on`의 단가를 조회한 뒤 사람·식종·
단가별 근거로 합친다. 월 중간 단가 변경도 총 식수를 잃지 않고 단가별 식수×단가의
합으로 금액을 만든다. 같은 사람·식종·서비스일 entitlement가 두 번 들어오면 조용히
중복 청구하지 않고 실패한다. 결과의 calculation manifest는
`unit_price_mode=service_date_effective`, policy version, 청구 시작일·종료일, 기본 단가,
사람별 단가·식수·소계와 전체 식수·금액을 포함한다. 청구 기간 안에서 같은 식종의 단가가
둘 이상이면 문서용 전체 비고도 함께 고정하고, 월 1일부터 같은 단가가 적용되어 월 전체가
한 구간이면 `null`로 남긴다. 결과와 다른 schedule로 manifest를 만들거나 청구 기간 밖의
서비스일을 포함할 수도 없다.

DB 하나가 업체 하나의 운영 일정을 대표하며 별도 업체 ID나 업체 선택 설정을 두지 않는다.
휴무·대체식은 현재 DB의 `meal_service_adjustment_schedule` policy version 1 audit entity에
append-only 변경으로 저장한다. 변경 하나는 inclusive 시작일·종료일, 기본 전체인 선택
식종, `normal`·`not_provided`·`replacement` 처리, 사유, 공통 실제 제공일과 원래
식사일×식종별 제공일 override를 가진다. 같은 날짜·식종에 겹치는 변경은 ledger에서
나중에 추가한 변경이 계산상 우선하지만 이전 원문은 유지한다. 저장은 일반 UI revision으로
변경번호를 만들고 commit 직후 자동 백업한다.

`holidays==0.102`의 한국 달력은 실행 중 네트워크 없이 연도별 `PublicHolidayCandidate`를
만든다. 후보는 읽기 전용 참고값이며 그 자체를 audit entity로 저장하거나 휴무로 계산하지
않는다. 허브는 아무 날짜도 기본 선택하지 않고, 사용자가 공휴일을 한 개 이상 체크하거나
`미검토 전체 선택`을 눌러 검토할 날짜를 정한다. 선택한 여러 날짜에는 같은 식종 집합과
`normal`·`not_provided`·`replacement` 처리를 한 번에 적용한다. 이미 검토한 날짜·식종을
다시 포함하면 저장 전에 재검토임을 알리고, 새 변경이 계산상 우선하되 이전 기록은 지우지
않는다. 일부 식종만 확정하면 나머지는 후보 목록에서 미검토로 남는다.

공휴일 여러 개를 한 번에 저장해도 schedule에는 날짜별 `ServiceAdjustmentChange`를 각각
추가한다. 반면 사용자 작업 경계는 하나이므로 `AuditChange` 하나, revision 하나, 자동 백업
하나만 만든다. revision decision의 `public_holiday_reviews` 배열에는 각 날짜의 한국어 이름,
provider ID/version, 입력 경로 `public_holiday`, 식종별 처리와 실제 제공일을 모두 고정한다.
대체식은 선택한 모든 공휴일×식종 칸에 실제 제공일이 있어야 저장할 수 있다. 기본 제공일은
화면에 표시된 다른 공휴일까지 제외한 직전 공통 운영일이고, 사용자는 날짜×식종 표에서 각
칸을 바꿀 수 있다. 직접 추가한 업체 자체 휴무는 입력 경로 `manual`이며 공휴일 원본 정보가
없다. 기존 policy version 1 state에 새 필드가 없으면 `manual`로 읽어 이전 상태와 호환한다.

entitlement 생성에서 `not_provided`는 서비스 불가로 처리해 배송과 청구 양쪽에서 빼고,
`replacement`는 원래 서비스일 entitlement를 유지한다. 따라서 청구·단가는 원래 서비스일,
배송 projection은 별도로 기록한 실제 제공일을 사용한다. 수동 휴무의 기본 실제 제공일은
현재 schedule상 선택 식종이 모두 정상 운영하는 직전 평일을 제안하며 사용자가 공통일 또는
개별 매핑을 수정할 수 있다. 공휴일 후보에서 `normal`을 명시하면 해당 식종은 평소 배송·청구로
확정한다. 휴무가 공휴일인지 여부는 필수 조건이 아니므로 업체 자체 휴무도 같은 계약으로
저장한다.

금요일 조식 휴무 방식은 고정 코드값으로만 두지 않고 같은 업체 DB의
`delivery.breakfast-policy` audit entity에 policy version 1 schedule로 저장한다. 기본값은
금요일분을 배송하지 않고 토·일분만 목요일에 미리 배송하는 방식이다. 설정 화면에서
`목요일_토일배송` 또는 `금토일_미배송`과 적용일을 고르면 append-only change, revision
결정, 변경번호와 자동 백업을 함께 남긴다. 문서 계산은 현재 설정이 아니라 선택한 source
snapshot에서 서비스일에 유효한 change를 읽는다. 별도로 저장한 대체식 실제 제공일은 이
일반 묶음 규칙보다 우선한다.

[Excel Adapter](../src/importing/workbook.py)는 일반 `.xlsx`/`.xlsm`과 비밀번호로
암호화한 파일을 읽는다. 파일 비밀번호는 파싱 중 메모리에서만 사용하고 미리보기
생성 직후 GUI 입력란에서도 지운다. Adapter는 선택한 열만 typed observation으로
바꾸며 수식, 알 수 없는 식종 flag, 같은 사람의 서로 다른 값은 자동 추측하지 않고
반영을 막는 오류로 표시한다. 식종별 여러 행은 같은 성명과 전체 생년월일이 정확히
같을 때만 한 observation으로 합친다.

## 최초 실행과 기존 Excel 명단

첫 설정에서 DB와 초기 백업을 만든 다음 다음 경로를 강제한다.

```text
파일 선택 → 열·가구 기준 확인 → 반영 전 미리보기 → 사람·가구 최종 검토 → 최초 revision → 자동 백업 → 명단 화면
```

빈 DB는 가짜 기준 revision이나 빈 roster snapshot을 만들지 않는다. 파일 선택이나
최종 검토에서 취소하면 DB·credential·초기 백업만 남고 운영 명단 화면은 열리지
않는다. 다음 실행에서는 평소 비밀번호로 같은 DB를 연 뒤 가져오기 안내부터 다시
시작한다. 한 명 이상을 확정해야 최초 import revision을 만들 수 있다.

승인 시 사람·식종·가구·membership·import profile·household matching rule·source
batch를 parent가 없는 한 revision으로 commit하고 `DatabaseSession.append_revision()`이
즉시 자동 백업한다. 복원 검증은 가구 소속과 rule 이력까지 재생되는지 확인한다.
DB commit 뒤 백업만 실패하면 “명단 저장 성공, 백업 실패”로 분리해 알린다. 현재
SQLite v2 앱은 다음 명령으로 실행한다.

```powershell
python -m src.desktop_v2
```

명단 화면의 `Excel 명단 가져오기`는 같은 wizard와 최종 검토를 후속 revision에
사용한다. 승인 직후 자동 백업을 만든 다음 현재 roster와 상태 이력을 다시 읽어
창을 갱신한다. 검증 복원한 백업에서도 기존·신규 구성원의 같은 가구 membership과
결정 manifest가 유지되어야 한다. 기존 사람의 매핑된 빈 인적사항을 포함한 후속
import도 자동 백업 복원 뒤 원래 학교·주소·전화 값이 유지되는지 통합 검증한다.

## 검증

기존 환경의 전체 회귀:

```powershell
python -m pytest -q tests
```

암호화 의존성을 포함한 환경의 집중 검증:

```powershell
python -m pip install -r .\requirements-sqlite-v2.txt
python -m pytest -q `
  .\tests\test_domain_roster.py `
  .\tests\test_domain_status.py `
  .\tests\test_domain_projections.py `
  .\tests\test_entitlements.py `
  .\tests\test_billing.py `
  .\tests\test_import_reconciliation.py `
  .\tests\test_import_workbook.py `
  .\tests\test_household_grouping.py `
  .\tests\test_import_revision.py `
  .\tests\test_revision_contracts.py `
  .\tests\test_document_runs.py `
  .\tests\test_unit_pricing.py `
  .\tests\test_encrypted_database.py `
  .\tests\test_database_credentials.py `
  .\tests\test_database_backup.py `
  .\tests\test_desktop_v2_database_access.py `
  .\tests\test_desktop_v2_import_wizard.py `
  .\tests\test_desktop_v2_application.py `
  .\tests\test_desktop_v2_roster.py `
  .\tests\test_desktop_v2_unit_price.py
```

## 아직 구현하지 않은 범위

- migration 실행 전 자동 백업과 schema downgrade 복구 검사
- 손상되거나 형식을 확인할 수 없는 백업의 사용자 알림·격리 흐름
- 과거 template/export profile 파일 보존과 document run 재생성·평문 hash 대조 UI
- 배송대타를 같은 historical entitlement 계산에 연결
- 원격 공격이나 DB key 탈취에 대한 tamper-evident 서명 원장
- 앱 실행 파일 코드 서명, 설치 프로그램, 클린 PC 인수 검사
