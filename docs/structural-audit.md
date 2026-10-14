# 구조 감사와 단순화 결과

기준은 Git HEAD가 아니라 작업 시작 시 이미 수정 중이던 파일들이다. 그 상태를 `artifacts/structural-audit/before-source.zip`에 보관하고 비교했다. 운영 DB, 비밀번호 파일, 실제 명단, 출력 파일은 변경하지 않았다.

## 조사 범위와 판단 기준

프로덕션 Python 150개 모듈과 테스트 Python 127개 모듈 전체의 AST, import 의존성, 함수·클래스, 테스트 단언과 예외 검증을 조사했다. 실행 진입점 `sqlite_v2.py`, 배포 spec, 미리보기 스크립트, 업무·저장소·UI 문서를 함께 확인했다. 전체 파일을 같은 깊이로 수작업 리뷰했다는 뜻은 아니다. 변경 대상은 호출부·기능·실패 유형까지 대조했고, 나머지는 의존성과 요구사항을 기준으로 유지·추가 조사 후보를 나눴다.

| 영역 | 시작 모듈 | 시작 줄 수 | 조사 결과 |
| --- | ---: | ---: | --- |
| domain | 27 | 8,458 | 폐기된 조식 정책 관리 제거, 같은 식종 검증 통합. 날짜별 상태·가입·단가 의미는 유지 |
| storage | 9 | 4,776 | 동적 재노출 제거. 키·잠금·백업·ledger/projection 무결성 책임은 실제로 필요 |
| importing | 9 | 5,946 | 재노출 제거. 원본 읽기·모호한 identity 검토·확정 revision은 서로 다른 책임 |
| services | 12 | 4,163 | 청구 결과 이중 표현과 고정 정책을 위한 가변 스케줄 제거 |
| desktop_v2 | 46 | 23,152 | 재노출 제거, 반복 명단 갱신 통합, 상태 이력 일괄 조회 |
| core | 8 | 1,296 | typed batch와 legacy 사전의 이중 계약 제거 |
| generators | 5 | 1,414 | 문서 입력과 생성 진입점 단일화 |
| excel | 17 | 2,687 | 원본 워크북 구조와 원자적 발행은 유지해야 할 파일 경계 |
| widgets | 11 | 2,682 | 기존 Qt 앱의 확인서 호출도 새 입력 계약에 연결 |

패키지별 합계 외에 `src/__init__.py`, `src/main_window.py`, `src/version.py`가 있다.

### 외부 계약과 내부 의존성

보존 대상은 실행 프로그램의 사용자 동작, 기존 Excel 표·템플릿·수식·셀·파일명·암호화, 저장된 DB/sidecar/백업 형식, revision별 과거 상태와 문서 이력, 서비스일과 실제 배송일의 차이, 취소 및 실패 처리다. `docs/ui-guidelines.md`에는 과거 조식 정책을 보존하되 새 배송 계산에는 사용하지 않는다는 명시적 규칙이 있다.

`src.*`의 재노출 이름, generator의 옛 사전 입력, `InvoiceResult.enriched`는 내부 Python 계약으로 판단했다. 앱 호출부는 이미 typed batch를 사용했고, 배포된 라이브러리·공개 API·저장 형식에서 해당 이름을 요구하는 근거는 없었다. docstring의 “public compatibility” 표현과 테스트의 사용만으로 외부 계약으로 승격하지 않았다. 기존 Python 이름을 사용하는 저장소 밖의 임의 스크립트에는 의도적인 비호환 변경이다.

## 요구사항에서 다시 만든 대안

단일 PC 앱에 필요한 것은 네 책임이다. 사용자의 선택과 취소를 다루는 Qt 화면, 날짜와 사람에 관한 업무 규칙, 원자적 저장과 복구, Excel 입력·출력이다. 범용 repository, command bus, 정책 플러그인, 별도 DTO 변환 계층은 필요하지 않다.

```mermaid
flowchart LR
    UI[Qt 화면과 구체적인 작업] --> Rules[날짜별 업무 규칙]
    UI --> Session[암호화 세션·잠금·백업]
    Input[Excel 입력과 검토] --> Rules
    Rules --> Facts[MonthlyDeliveryBatch]
    Facts --> Render[청구서·확인서 생성기]
    Render --> Publish[원자적 Excel 발행]
    Session --> Ledger[변경 이력과 현재 상태 검증]
```

| 기능 | 이전 구조 | 채택한 대안 / 유지 이유 |
| --- | --- | --- |
| 청구·확인서 | 앱은 이미 batch를 직접 전달. 생성 후 미사용 enriched 결과를 별도 구성하고, 사전 입력 호출자용 adapter와 중복 기간·단가 인자를 유지 | 미사용 결과 생성과 사전 입력 호환 경로를 제거. 생성기는 batch의 기간·단가를 사용 |
| 파일 생성 | `make_*` → `make_*_publication`, 입력 batch를 결과로 다시 반환 | 발행 정보를 반환하는 진입점 하나, 소유한 입력을 다시 반환하지 않음 |
| 조식 정책 | change/schedule/state/revision builder가 있지만 항상 빈 schedule 사용 | 고정 rule 하나로 직접 계산. 과거 ledger 내용과 기존 manifest 유지 |
| 모듈 참조 | package export 목록과 동적 `__getattr__` 라우팅 | 실제 소유 모듈을 직접 import. package 초기화 파일은 표시용 docstring만 유지 |
| 명단 갱신 | 같은 갱신 본문 9곳, 사람×식종마다 조회 | 공통 갱신 본문 하나, 이벤트 한 번 읽고 사람·식종별로 계산 |
| 식종 선택 | 상태·휴무·단가 모듈에 같은 검증 구현 3개 | `roster.canonical_meals` 하나. 오류 종류·문구·정렬 유지 |
| DB 세션 | 단순 wrapper처럼 보이나 키·잠금·백업 결과 소유 | 유지. 저장 성공/백업 실패를 분리해야 하는 실제 외부 동작이 있음 |
| ledger와 projection | 두 상태 표현 | 유지. 과거 재현과 빠른 현재 조회를 제공하며 동일 transaction과 replay 검증으로 관리 |
| 입력 staging | parsing → reconciliation → apply → revision | 유지. 모호한 사람 연결을 사용자 결정 전에 저장하지 않는 계약을 구현 |
| 두 Qt 날짜 편집기 | 유사한 wheel guard | PyQt6와 PySide6 QObject 계층이 달라 그대로 합칠 수 없음. 바인딩 범용화를 추가하는 편이 더 복잡함 |

## 제거·병합 후보의 우선순위와 처리

| 우선순위 | 후보·영향 범위 | 기대 효과와 처리 |
| --- | --- | --- |
| 1 | 문서 이중 모델·변환·생성기 wrapper / core, services, generators, 두 UI, 관련 테스트 | 미사용 enriched 결과 생성과 별도 사전 입력 호환 경로, wrapper를 제거. 앱의 batch 왕복 변환이 제거된 것은 아님. 완료 |
| 2 | 폐기된 조식 정책 / domain, report, 과거 정책 fixture | 사용하지 않는 가변 정책·수정 API·직렬화 모듈 제거. 완료 |
| 3 | package 재노출 / 네 package, 앱·테스트·launcher·spikes의 import 183곳 | 이름의 소유권을 한 곳에 두고 불필요한 eager import와 동적 라우팅 제거. 완료 |
| 4 | 명단 갱신·이력 조회 / application | 중복 갱신 구현과 3N번 SQL 읽기 제거. 완료 |
| 5 | 동일 식종 검증 / 상태·휴무·단가 | AST 본문까지 동일한 세 규칙을 한 곳으로 통합. 완료 |
| 6 | 미사용 `has_custom_delivery_weekdays` | 정의 외 호출이 없어 삭제. 효과는 작으며 주요 개선으로 계산하지 않음 |
| 7 | 월간 문서·배송 리포트 준비 코드 | 해시·날짜 helper는 닮았지만 실제 포함 기간이 다름. 청구는 서비스일, 리포트는 물리 배송일 및 대체배송 범위. 잘못된 공통화 보류 |
| 8 | 편집 controller 공통 틀 | 일부 생성자 본문은 동일하지만 실제 검증·취소·저장 실패 의미가 다름. generic base controller를 추가할 이익이 확인되지 않아 유지 |
| 9 | 기존 Excel 앱 전체 | 기존 워크북을 직접 사용하는 기능과 새 DB 기능의 입력 계약이 다름. 공유 생성기는 통합했지만 기능 동등성 근거 없이 원본 입력 경로를 삭제하지 않음 |
| 10 | projection 또는 세션 계층 전체 | 삭제하면 파일 호환·복구·과거 재현·원자성 설계를 다시 해야 함. 중복처럼 보인다는 이유만으로 제거하지 않음 |

이름만 바꾸거나 파일을 옮긴 변경은 없다. 새 호환 wrapper도 없다. `_replace_roster_if_changed`는 같은 revision이면 갱신하지 않는 결정 책임을 유지하고, 공통 갱신 함수는 아홉 본문의 실제 중복을 대체한다. 행 삭제/추가의 routing 갱신 순서는 별도로 보존했다.

## 테스트 재평가

`test-requirement-evidence.json`은 작업 전 1,064개 테스트 정의별 단언, `raises`, import, 내부 멤버 접근, parameterization을 기록한다. 299개 정의에서 내부 멤버 접근이 있었지만 이를 일괄 삭제 기준으로 쓰지 않았다. 동일 본문 탐지는 fixture를 포함해 후보 5묶음을 찾았으며, PyQt/PySide 구분과 업무 의미를 읽어 판단했다. 테스트 개수 또는 매개변수 사례 개수는 중복 판정 근거가 아니다.

| 대상 | 판단과 보존한 결함 탐지 가치 |
| --- | --- |
| `test_delivery_policies.py`의 3개 테스트 | 이미 폐기된 설정 편집·schedule 선택·state 왕복만 검증하므로 모듈과 함께 제거 |
| 과거 조식 정책 회귀 | 삭제한 builder로 fixture를 만들지 않고 과거 저장 형식을 직접 구성. 이력이 남고 새 배송에 적용되지 않음을 검증 |
| 빈 배송 리포트 | 정책 manifest는 유지하되 사용 rule 목록은 빈 값인 경계를 추가 |
| `test_models.py`의 enriched builder 2개 테스트 | 삭제된 사전 계약과 사전 복제만 검증하므로 제거. 주소 공백·누락 처리는 독립 테스트로 보존 |
| `InvoiceData` 필드 이름만 검사하던 테스트 | 삭제. 실제 화면 간 batch 전달·연월 표시·출력 대상 검증은 integration에서 유지 |
| invoice service / generator | 월 범위·빈 명단·요일 배수·주차 경계·단가 override·배송 메타데이터·원문 순서·노트·실제 셀·암호화 검증 유지. 기대값을 batch로 읽도록 수정 |
| generator 실패 | 쓰기 예외 및 건수 불일치 시 확인서에 batch를 전달하지 않는 두 실패 유형 추가 |
| 확인서 UI | 설정된 파일명과 전달 batch의 동일성 검증 추가 |
| 상태 일괄 읽기 | 사람·식종 혼합 방지, 같은 기록 시각의 순서, 경계일, 빈 명단, 읽기 오류 전파 추가 |
| 공통 식종 선택 | 원본 선택 미변경, canonical 순서, 빈 선택·중복·문자열 입력 거부 추가 |
| 설정 의존 테스트 7곳 | 운영 `data/settings.ini`의 존재에 의존하던 생성자에 명시적인 가짜 설정 주입. 실제 잠금·경로 테스트는 유지 |
| 마법사 27개 사례 | 닫힌 창과 예약 콜백이 테스트 종료 후 남아 네이티브 오류를 유발. 콜백 처리 후 GUI 스레드에서 객체를 해제하는 teardown 추가. 테스트 단언은 제거하지 않음 |

## 계량 결과

아래 수치는 최초 구조 단순화 완료 시점의 기록이며, 독립 감사 후속 수정은 포함하지 않는다. 작업 시작 시점의 snapshot과 당시 최종 파일을 비교했다. 모든 줄 수는 주석·공백을 포함한 물리적 Python 줄 수이며, 도구·로그·문서는 프로덕션 코드량에 포함하지 않는다.

| 측정 항목 | 이전 | 이후 | 차이 |
| --- | ---: | ---: | ---: |
| 프로덕션 모듈 | 150 | 149 | −1 |
| 프로덕션 코드 줄 | 55,467 | 54,082 | −1,385 |
| 프로덕션 클래스 | 397 | 392 | −5 |
| 프로덕션 함수·메서드 | 2,200 | 2,173 | −27 |
| 테스트 모듈 | 127 | 126 | −1 |
| 테스트 코드 줄 | 37,514 | 37,765 | +251 |
| 테스트 정의 | 1,064 | 1,067 | +3 |
| 실행 사례 | 1,255 | 1,264 | +9 |
| 전체 테스트 실행시간 | 164.53초 | 162.67초 | −1.86초 |

모듈 수에는 `__init__.py`와 테스트 지원 파일도 포함한다. 테스트 정의는 매개변수 확장 전, 실행 사례는 확장 후 수다. 실행시간은 각 한 번의 전체 실행 결과이며 1.1% 차이를 성능 개선으로 해석하지 않는다. 테스트 줄 수 증가는 새 실패 유형·경계 검증과 GUI 객체 정리 때문이다.

네 package의 재노출 항목은 합계 328개에서 0개로 줄었다. 동적 `__getattr__` 라우터 두 개도 제거했다. 모듈 수 감소보다 큰 변화는 문서 표현 2→1, 생성기 진입점 4→2, 가변 정책 관리 계층 제거, 갱신 본문 9→1, 동일 식종 검증 3→1이다.

### 실행 측정

합성 인원 200명, 상태 사건 600건을 같은 APSW 메모리 테이블에 넣고 5회 측정했다. 전체 결과의 SHA-256은 전후 동일하다. 이 시험은 실제 SQL 읽기 경로를 쓰지만 디스크 암호화 DB·실제 대규모 명단 전체를 대표하는 성능 보장은 아니다.

- 상태 이력 읽기: 600회 → 1회.
- 이력 계산 중앙값: 68.63ms → 26.03ms.
- 결과 해시: `c7a93b61887d9fdc92ddcd9435159455600fa90bbf3a23f4e3698c6927b4c4be`.

새 Python 프로세스에서 각 모듈을 3회 import한 중앙값과 함께 로딩된 프로젝트 모듈 수:

| import | 이전 | 이후 | 로딩 모듈 이전→이후 |
| --- | ---: | ---: | ---: |
| `src.domain.roster` | 199ms | 42ms | 21→3 |
| `src.importing.contracts` | 758ms | 60ms | 30→5 |
| `src.storage.database_credentials` | 81ms | 91ms | 3→3 |
| `src.desktop_v2.document_period` | 1,551ms | 310ms | 101→9 |

credentials는 원래 필요한 것만 불러오던 경로여서 개선되지 않았다. 측정은 테스트 작업과 같은 머신에서 수행한 진단값이며 보편적 배속을 주장하지 않는다.

## 최초 구조 단순화 당시 검증과 한계

- 비교용 기준 전체 실행: **1,255건 통과, 164.53초**.
- 최종 전체 실행: **1,264건 통과, 162.67초**. 실패·오류·건너뛴 사례 없음.
- 두 실행 모두 의도적으로 비문자 헤더를 만드는 Excel 경계 테스트에서 openpyxl 경고 5건 발생.
- 변경 기능의 집중 검증, 미정의 이름 Ruff 검사, `--packaging-smoke`, `git diff --check` 통과.
- 독립 리뷰 2회에서 차단 결함 없음. 마지막 테스트 추가가 계량 자료에서 빠졌다는 지적은 전체 inventory·지표를 다시 생성하여 반영.

원본 전체 실행의 환경 문제를 보정한 방식은 다음과 같다.

기존 단일 프로세스 기준 실행은 변경 전에 Windows heap 오류 `0xc0000374`로 종료됐다. 파일별 격리와 마법사 사례별 실행에서도 테스트 본문은 통과하고 종료 중 실패하는 사례가 있었다. 정리 fixture를 추가하자 마법사 27건이 한 프로세스에서 통과했다. 비교용 기준 전체 실행에는 원본 소스·테스트 단언을 유지하고 같은 teardown만 외부 pytest plugin으로 적용했으며, 경로 설정의 존재 조건은 임시 폴더의 빈 INI로 맞췄다. 초기 실패 로그는 보존했다.

당시 독립 리뷰는 작업 전 snapshot과 구조 단순화 diff를 대상으로 수행했으며 차단할 결함을 찾지 못했다. 이후 독립 감사에서는 기존 DB 조회의 동시각 사건 순서 결함과 실제 XLSX 단가 수식 테스트 공백을 확인했다. 내부 Python import·writer API 변경은 의도적이다. 실제 운영 파일로 수동 사용하거나 Microsoft Excel을 띄워 육안 검수한 것은 아니다. 실제 배포 템플릿의 셀 검증, 암호화 재열기, 합성 DB와 복구 테스트를 근거로 삼았다.

## 재현 자료

`artifacts/structural-audit/`에 작업 전 소스, 작업 기준 diff, 전체 import graph, 전후 inventory, 테스트 요구사항·삭제/추가 목록, 전체/격리 테스트 로그와 XML, 성능 script와 JSON을 보관한다.

- 일반 검증: `python -m pytest -q tests`
- 이름 참조 검사: `python -m ruff check src tests --select F821,F822,F823`
- 실행 진입점: `python sqlite_v2.py --packaging-smoke`
- 성능: `python artifacts/structural-audit/benchmark.py <소스 루트> <결과 JSON>`

전체 의미 중복을 테스트 이름·단언 개수만으로 자동 판정하지 않았다. 원본 구현을 정답으로 고정하지 않되, 출력·복구·과거 데이터 계약을 대체할 근거가 없는 부분은 유지한 결과다.


## 독립 감사 후속 수정

후속 수정의 기준은 `artifacts/structural-audit-fixes/before-source.zip`이다. Git HEAD와의 차이에는 이전 기능 작업도 포함되어 있으므로 이번 수정의 성과로 합산하지 않는다.

### 사건 순서와 기존 DB

현재 projection의 사건 조회가 `recorded_at, event_id` 순서였기 때문에, 같은 시각의 UUID가 기록 순서와 반대면 snapshot과 현재 화면의 상태가 달랐다. 조회를 projection 행의 `created_revision_id`·사건 ID·entity kind에 일치하는 audit insert와 연결하고 `revision.sequence, audit_change.ordinal`로 정렬하도록 수정했다. 기록 시각의 우선순위는 기존 상태 resolver가 aware datetime으로 비교한다. 같은 순간을 서로 다른 UTC offset으로 표현해도 ledger 순서가 동률을 결정한다.

기존 DB에 이미 있는 순서 열과 인덱스를 사용하므로 스키마 migration이나 ledger 재작성은 없다. 실제 암호화 DB 테스트 두 사례는 동일 revision 안의 ordinal과 여러 revision의 sequence를 각각 확인한다. UUID는 기록 순서의 역순이며, DB를 재열고 snapshot·현재 조회·일괄 조회의 전체 이력과 `배송` 결과를 비교한다. APSW SQL trace로 일괄 조회의 사건 SELECT가 한 번임을 검사한다. 새 실행시간 배속을 주장하지 않는다.

회귀 테스트는 수정 전 2건 실패, 수정 후 2건 통과했다. 추가로 후속 작업 전 ZIP의 코드로 만든 합성 암호화 DB를 수정 코드로 열어 순서 복원과 스키마·ledger 불변을 확인했다. `status-event-order-final-red.log`, `status-event-order-final-green.log`, `status-event-order-compat.log`와 재현 script를 `artifacts/structural-audit-fixes/`에 보관했다.

### 실제 XLSX 단가 검증

`test_distinct_meal_prices_reach_saved_invoice_formula`는 실제 급식 양식과 기본 칼럼 연결을 사용한다. `billing_amount=None`, 조식 7,100원·중식 8,300원·석식 9,700원 및 각각 3·2·4식을 입력해 저장 파일을 다시 연다. M/N/O 열이 중식/석식/조식인지, Q4가 `=O4*7100+N4*9700+M4*8300`인지 확인하고 저장된 참조 셀로 계산한 금액을 독립 기대값 76,700원과 비교한다.

openpyxl은 Excel 수식을 계산하지 않는다. 이 검증은 저장된 수식과 참조 셀의 산술을 검사하며 Microsoft Excel의 재계산이나 캐시 값을 검증한 것은 아니다. 작성기를 mock으로 바꾸지 않았고, 프로덕션 계산 코드는 수정할 필요가 없었다. 모든 단가를 9,500원으로 바꾸는 메모리 내 결함 주입에서는 새 테스트가 실패했고 정상 구현에서는 통과했다. 실행 로그는 `price-mutation.log`와 `price-regression.log`에 있다.

### 폐기 정책과 과거 데이터

`SKIP_FRIDAY_WEEKEND`의 생산 소비자를 재검색했다. 현재 리포트가 만드는 규칙은 `THURSDAY_WEEKEND_ONLY`뿐이므로 폐기 enum 값과 실행 분기, 해당 분기만 검사하던 테스트를 제거했다. 남은 고정 규칙·rule version·manifest 형식은 유지한다.

과거 `delivery.breakfast-policy`의 `금토일_미배송`은 실행 enum에 의존하지 않는 저장 문자열로 검증한다. 기존 호환성 테스트에서 이 값을 실제 암호화 DB에 저장한 뒤 닫고 다시 열어 ledger 내용과 현재 고정 정책의 리포트를 확인한다. 기본 정책 manifest의 기존 해시 `24536815e0c24815409bb1bc722e7fc826a11e9277e68add506394d36c368038`와 빈 리포트의 사용 규칙 목록 검증도 유지했다.

### 외부 호환성 범위

기존 package 재노출, 옛 generator 입력·반환 계약과 `InvoiceResult.enriched`를 쓰는 저장소 밖 Python 스크립트의 유무는 확인하지 못했다. 이번에 제거한 폐기 enum 이름도 외부에서 직접 import하던 호출자에는 비호환이다. 저장소 내부 소비자와 합성 저장 데이터의 호환성을 확인한 범위이며, 운영 DB 전체나 배포된 EXE를 검증했다는 의미는 아니다.


### 후속 검증 결과

- 운영 DB와 설정을 제외한 임시 소스·테스트·내장 양식 복사본에서 전체 테스트 **1,266건 통과, 169.97초**. 실패·오류·건너뛴 사례 없음. 기존 비문자 Excel 헤더 경계 사례의 openpyxl 경고 5건.
- 전체 실행 시작 후 사건 순서 테스트에 snapshot의 계산된 전체 이력과 현재·일괄 이력의 직접 동등 비교를 보강했다. 최종 테스트를 작업 전 소스에 적용하면 **2건 실패**, 수정 소스에서는 **2건 통과**했다. 전체 실행 이후 프로덕션 코드는 바뀌지 않았다.
- `python sqlite_v2.py --packaging-smoke`와 Ruff 미정의 이름 검사 통과. 실제 배포 EXE를 새로 빌드하거나 실행한 검증은 아니다.
- 단가 고정 결함 주입에서 새 실제 XLSX 테스트 **1건 실패**, 정상 구현에서 **1건 통과**. 전체 suite 통과만으로 보존을 판정하지 않았다.
- 기존 코드로 생성한 합성 암호화 DB 재열기에서 스키마·ledger 내용 불변과 UUID 역순 사건의 올바른 우선순위를 확인했다.

재현 자료는 `artifacts/structural-audit-fixes/`의 `task-relative.diff`, `full-tests.log`/XML, 실행 진입점·이름 검사 로그, 사건 순서 red/green·기존 DB 호환성 로그와 단가 결함 주입 script에 있다. `run_full_verification.py`는 소스·테스트·내장 양식만 시스템 임시 폴더에 복사해 전체 검증을 실행한다. `price-mutation.py`의 정상적인 검증 결과는 새 회귀 테스트의 실패(exit 1)다.

독립 최종 리뷰에서 코드 차단 결함은 발견되지 않았다. 남아 있던 정책 선택 설명은 `docs/sqlite-storage.md`에서 정정하고, `HANDOFF.md`의 과거 기록에는 현재 계약을 안내하는 정정문을 추가했다.
