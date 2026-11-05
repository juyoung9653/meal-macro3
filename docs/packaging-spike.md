# Python 3.14 Windows 패키징 스파이크

검증일: 2026-08-24
환경: Windows 11 x64, CPython 3.14.6

## 결론

새 SQLite 데스크톱 앱은 현재 CPython 3.14를 유지할 수 있다. 공식 Windows
x64 wheel만 사용해 PySide6, `apsw-sqlite3mc`, PyInstaller를 설치했고, 단일
실행 파일 안에서 GUI와 암호화 SQLite 동작을 확인했다. Python 3.13 이하로
내리는 결정은 아직 필요하지 않다.

새 앱의 전체 런타임 의존성은
[requirements-sqlite-v2.txt](../requirements-sqlite-v2.txt)로 기존 PyQt6 앱과
분리한다.

이 결과는 배포 기술의 가능성을 확인한 것이다. 기관용 키 복구 설계나 제품
보안 승인을 대신하지 않는다.

## 고정한 스파이크 버전

버전은 [스파이크 requirements](../spikes/requirements.txt)에 고정한다.

| 패키지 | 버전 | 확인한 배포물 |
|---|---:|---|
| PySide6 | 6.11.1 | CPython 3.10+ ABI3, Windows x64 wheel |
| apsw-sqlite3mc | 3.53.3.1 | CPython 3.14, Windows x64 wheel |
| PyInstaller | 6.21.0 | Python 3, Windows x64 wheel |
| holidays | 0.102 | Python 3, pure Python wheel |

`apsw-sqlite3mc`가 보고한 내장 버전은 APSW 3.53.3.1, SQLite 3.53.3,
SQLite3 Multiple Ciphers 2.3.6이다. 새 버전으로 올릴 때는 아래의 모든
암호화·패키징 검사를 다시 실행한다.

근거:

- [PySide6 6.11.1 배포 메타데이터](https://pypi.org/project/PySide6/6.11.1/)
- [apsw-sqlite3mc 3.53.3.1 Windows CPython 3.14 wheel](https://pypi.org/project/apsw-sqlite3mc/3.53.3.1/)
- [PyInstaller 6.21.0 배포 메타데이터](https://pypi.org/project/pyinstaller/6.21.0/)
- [holidays 0.102 배포 메타데이터](https://pypi.org/project/holidays/0.102/)
- [holidays 한국 달력 문서](https://holidays.readthedocs.io/en/latest/auto_gen_docs/south_korea/)
- [SQLite3 Multiple Ciphers 키 설정 순서](https://utelle.github.io/SQLite3MultipleCiphers/docs/configuration/config_sql_pragmas/)

## 검증한 동작

[패키징 스모크 앱](../spikes/packaging_smoke.py)은 임시 데이터에만 쓰는 고정
테스트 키로 다음을 검증한다.

- 오프스크린 PySide6 `QWidget` 생성
- 기본 권장 cipher인 ChaCha20-Poly1305로 DB 생성
- 파일 헤더가 평문 SQLite 헤더와 다름
- 표준 라이브러리 `sqlite3`의 평문 접근 실패
- 잘못된 키의 조회 실패
- 올바른 키로 닫은 DB를 다시 열어 값 조회

소스 실행과 PyInstaller 단일 파일 실행에서 모두 아래 결과가 나왔다.

```json
{
  "correct_key_reopened": true,
  "encrypted_header": true,
  "gui_widget": true,
  "plain_sqlite_rejected": true,
  "wrong_key_rejected": true
}
```

생성된 스모크 실행 파일 크기는 49,155,261 bytes였다. PyInstaller의 누락
모듈 목록에는 Windows에서 정상적으로 없는 POSIX 선택 모듈만 있었고, 실제
실행은 성공했다.

## SQLite v2 전체 앱 빌드

2026-08-31에는 별도 [v2 launcher](../sqlite_v2.py)와
[PyInstaller spec](../sqlite_v2.spec)으로 실제 SQLite v2 전체 앱을 one-file Windows
실행파일로 빌드했다. `holidays==0.102`의 동적 한국 국가 모듈과 한국어 `KR.mo`를 spec에
명시적으로 포함했다. 공휴일 여러 날짜 일괄 검토, 통합 문서 선택, 기간·식종·저장 폴더와
자동 파일명·재발행 확인, 학교 일정 자동 가져오기·검토와 저장 일정 직접 편집, 실제
청구서·확인서 생성 및 설정 허브 UI까지 포함한 당시 `meal_delivery_sqlite_v2.exe`는
73,939,294 bytes였다.
SHA-256은 `64c50906106c41521438465b49dae317130a8e48888da1edbcf129e0415b9321`이다.
PyInstaller 분석에서 학교 일정 import·직접 편집·문서 계산·생성 모듈 포함을 확인했고 번들 내부 smoke도
종료 코드 0으로 통과했다.

2026-10-13에는 현재 소스로 `dist/meal_delivery.exe`를 다시 빌드했다.
앱 버전은 `0.1.0`이며 창 제목·QApplication·Windows 파일 속성에 기록한다.
실행 파일명은 버전 번호 없이 고정하고 기존 운영 파일과 개인 화면 설정의 식별자는 유지한다.
Excel 표 선택·빠른 읽기·256열 제한 수정과 오류 없는 명단의 직접 가져오기,
설정에서 자동 백업 폴더를 변경하는 기능과 조회 날짜 기준의 미배송 숨기기를 포함한다.
1일 보관 기준에서는 모든 식종이 `미배송` 또는 `-`인 행을 숨긴다.
저장된 칼럼 필터가 배송 행을 가리는 경우를 확인할 수 있도록 적용된 칼럼을 표시하고,
`칼럼 필터 해제`로 해당 조건만 지우는 기능을 포함한다.
`숨긴 행 포함`은 검색어 없이도 보관 행을 함께 표시한다. 칼럼 정렬은 먼저 선택한
칼럼부터 우선 적용하며 각 칼럼이 오름차순·내림차순·해제 순서로 전환된다.
단일 파일이며 `console=False`이고, PE의 Windows GUI subsystem 값 2를 확인했다.
실행 파일 크기는 74,684,472 bytes이며 SHA-256은
`dead54790f4d70d64f92f3f76fdbbd4a83f54ccc94f23bc877570c0f29f6c860`이다.
PE의 FileVersion·ProductVersion 문자열은 `0.1.0`, 숫자 버전은 `(0, 1, 0, 0)`으로 확인했다.
템플릿 5개와 공통 경로 설정·실행 잠금 모듈이 번들에 포함됐으며, `--packaging-smoke`는 종료 코드 0으로 통과했다.
실행 파일을 임시 폴더에 복사하고 빈 공통 설정으로 격리해 `처음 설정` 창이 열리는지 확인했다.
첫 실행을 유지한 상태에서 두 번째 실행은 `프로그램을 열 수 없습니다` 안내 후 종료 코드 1로 차단됐다.
첫 실행을 닫은 뒤 세 번째 실행은 정상 시작·종료했다. 운영 파일과 실제 공통 설정은 열거나 수정하지 않았다.
가져오기 관련 테스트 123개와 기본·최소 창 크기의 합성 데이터 화면 검사도 통과했다.
백업 폴더 변경 후 설정·시작·가져오기·백업 관련 테스트 76개와 새 설정 화면의
기본·최소 크기 및 오류 표시 검사도 통과했다.
미배송 숨김 수정 후 분류·보관함·명단 화면 테스트 72개와 조회 날짜 변경,
기본·최소 크기의 일반 명단·보관함 합성 화면 검사도 통과했다.
칼럼 필터 안내·해제 관련 테스트 30개가 통과했다. 합성 명단 420명에서 보관함 86명,
저장된 칼럼 필터로 일반 명단 0명이 되는 상황을 재현했고, 필터 해제 후와 재실행 후
일반 명단 334명이 표시됨을 확인했다. 기본·최소 크기 화면도 확인했다.
0.1.0의 숨긴 행 포함·다중 정렬·버전 계약과 공유 헤더 회귀 테스트 107개가 통과했다.
실제 헤더 마우스 클릭으로 복합 정렬과 3단계 해제를 확인했으며, 검색어 없는 숨긴 행
표시 및 재실행 후 복원, 기본·최소 크기에서 제목·우선순위·필터 버튼의 배치도 확인했다.
공통 경로는 실행 파일 옆 `data/settings.ini`에 저장한다. 실제 DB·백업·출력 폴더는 직접 지정하며 기존 파일은 이동하지 않는다.
프로그램 폴더와 DB별로 실행 중 잠금을 유지한다. 기존 저장소·시작·응용 흐름 테스트 93개,
최종 설정·잠금·UI·응용 테스트 83개가 통과했다(검사 묶음 간 중복 포함).
Windows 별도 프로세스의 정상 종료·강제 종료 후 잠금 해제, 다른 실행 폴더의 같은 DB 접근 차단,
첫 설정 저장 실패 후 재시도, 복원·위치 변경 실패 시 정리도 검증했다.
공통 설정 화면은 기본·최소 크기로 캡처해 확인했다. 관련 자료는 `artifacts/portable_lock/`에 보관한다.
실제 두 PC의 SMB 공유 및 연결 끊김·재연결은 이 환경에서 검증하지 않았다. 공유 사용자 모두 새 실행 파일을 사용해야 한다.
빌드 로그와 검증 자료는 `artifacts/pyinstaller/`에 보관한다.

실행파일의 숨은 `--packaging-smoke` 검사는 2026년 삼일절 대체 휴일과 제헌절의 한국어
명칭을 번들 내부에서 읽어 종료 코드 0을 반환했다. 따라서 앱 실행 중 인터넷 연결이나
개발 PC의 별도 `holidays` 설치 없이 공휴일 후보를 만들 수 있다. 빌드 경고는 Windows에서
없는 POSIX 모듈과 선택 의존성 위주였고, 공휴일 smoke가 사용하는 필수 모듈 누락은 없었다.

```powershell
python -m PyInstaller --noconfirm --clean --workpath .\artifacts\pyinstaller\work .\sqlite_v2.spec
Start-Process .\dist\meal_delivery.exe `
  -ArgumentList '--packaging-smoke' -WindowStyle Hidden -Wait
```

## 0.1.1 검증 (2026-10-14)

- 앱·Windows 파일 버전을 `0.1.1`로 변경했다. 숫자 버전은 `(0, 1, 1, 0)`이다.
- 장기 미배송 보관함은 화면에 식종 변경 이력이 없고 세 식종 모두 `-`인 행을 기준 일수와 관계없이 포함한다. 일반 Excel 가져오기 기록만 내부에 남아 있는 경우도 포함하며, 표시할 이력이 있으면 기존 기간 판정을 유지한다.
- 중지 시 배송 담당 유지 회귀 검사를 추가했다. 실제 Excel 가져오기부터 중지 저장·암호화 DB 재조회까지 기존 담당이 유지되며, 식종별 배정·명시적 미지정·예약 담당도 검증한다. 신고된 담당 소실은 현재 소스에서 재현되지 않아 담당 저장·조회 로직은 변경하지 않았다.
- `dist/meal_delivery.exe`를 다시 빌드하고 파일 버전·제품 버전 `0.1.1`, 실행파일 `--packaging-smoke` 종료 코드 0을 확인했다. 빌드 로그·해시·검증 결과는 `artifacts/release-0.1.1/`에 보관한다.
- 최종 `python -m pytest tests -q`는 1,283개가 통과했다. 빈·잘못된 Excel 표 머리글을 검증하는 기존 테스트에서 openpyxl 경고 5개가 발생했으며 실패는 없었다.

## 재현

저장소 루트의 PowerShell에서 실행한다. 기존 개발 환경과 섞이지 않도록
별도 임시 가상환경을 사용한다.

```powershell
$spikeEnv = Join-Path $env:TEMP 'sqlite-desktop-v2-packaging-spike'
python -m venv $spikeEnv
$spikePython = Join-Path $spikeEnv 'Scripts\python.exe'
& $spikePython -m pip install --only-binary=:all: -r .\spikes\requirements.txt
& $spikePython .\spikes\packaging_smoke.py
& $spikePython -m PyInstaller --noconfirm --clean --onefile --console `
  --name sqlite_v2_packaging_smoke .\spikes\packaging_smoke.py
& .\dist\sqlite_v2_packaging_smoke.exe
```

## 운영 구현에 남은 제약

- 스모크의 고정 키는 제품 코드에서 사용하지 않는다. 제품은 random DB key와
  단일 비밀번호 기반 key wrapping을 사용하며 기존 기관 복구 slot도 읽는다. 현재 제품
  requirements는 이 용도의 `cryptography==49.0.0`을 직접 고정했으며 CPython
  3.14 Windows 환경의 전체 테스트와 PyInstaller one-file 빌드는 통과했다.
- cipher 선택과 키 설정은 첫 SQL 문보다 먼저 수행해야 한다.
- 운영 DB는 `apsw-sqlite3mc` Adapter만 연다. 표준 `sqlite3` 등 다른 SQLite
  구현이 같은 DB 파일을 동시에 열지 않게 한다.
- Adapter는 `apsw.mc_version`을 확인해 일반 APSW가 설치된 경우 평문 DB를
  만드는 대신 즉시 실패한다.
- 임시 테이블은 메모리에 두는 패키지 기본 설정을 유지하고, 평문 임시 파일을
  만드는 설정으로 바꾸지 않는다.
- 전체 Excel 의존성을 포함한 v2 one-file 빌드는 통과했다. 코드 서명, 설치 프로그램,
  Windows 10/11 클린 PC, 백신 오탐, 실제 운영 자료의 장애 복구는 별도 인수 검사가
  필요하다.
- 새 암호화 SQLite 릴리스는 DB 호환성과 보안 변경을 검토한 뒤 golden DB로
  업그레이드·다운그레이드·백업 복구를 검증한다.


## 0.1.4 문서 생성 후속 수정

- 최신 변경번호로 기간 선택을 바로 열고, 이전 변경번호는 버튼으로 선택한다. 일일식수는 배송 형태별 포함 여부를 선택하며 제외한 형태를 가구·식수 집계에서 뺀다.
- 과거 행 추가에서 가구 소속 시작일이 식종 가입일보다 늦었던 대상자는 `설정 → 가구 소속 기록 복구`에서 확인 후 정정할 수 있다. 운영 파일을 자동으로 수정하지 않는다.
- 관련 통합 테스트 92개와 애플리케이션 문서 흐름 5개가 통과했다. 독립 검토와 화면 확인을 마쳤으며 `dist/meal_delivery.exe` 파일 버전 0.1.4 및 `--packaging-smoke` 종료 코드 0을 확인했다.
- 검증 로그·스크린샷·실행파일 해시는 `artifacts/release-0.1.4/`에 보관한다. 전체 테스트 성공을 뜻하지 않으며 기존 애플리케이션 날짜 기대값 실패와 이 환경의 Excel COM 렌더링 오류는 별도로 남아 있다.

## 0.2.0 재빌드·사용 설명서 갱신 (2026-10-29)

- 사용자 요청으로 현재 작업 중인 소스를 포함해 `sqlite_v2.spec`으로 `dist/meal_delivery.exe`를 재빌드했다. 앱 버전은 기존 `0.2.0`을 유지했다.
- 실행 파일 크기: 76,580,233 bytes. Windows 파일 버전: `0.2.0`. SHA-256: `d56a80da633c08a99c7572affde6ff6931cd75786640e3b3d666e055714e1ada`.
- 실행 파일 `--packaging-smoke` 종료 코드 0. 빌드 명령은 `python -m PyInstaller --noconfirm --workpath artifacts/build-20261029/work sqlite_v2.spec`이다. 운영 DB를 열어 GUI 전체를 검증한 결과는 아니다.
- `QT_QPA_PLATFORM=offscreen`에서 `python -m pytest -q tests -m "not excel"` 실행: 2,544 passed, 8 failed, 23 deselected, 5 warnings. Excel 연동 검사는 제외했다. 전체 회귀 검사가 통과한 빌드는 아니다.
- 실패는 한국어 오류 안내로 바뀐 뒤 영문 문자열을 기대하는 검사 3건, `warning_required` 없는 백업 대역 객체를 쓰는 검사 4건(배송순서 갱신 확인 포함), 확인서의 식수 0을 빈 셀 대신 숫자 0으로 기대하는 검사 1건이다. 이번 빌드·설명서 작업에서는 앱 코드와 테스트를 변경하지 않았다. 실패 상세와 전체 로그는 `artifacts/build-20261029/tests.log`에 보관했다.
- HTML 8개 페이지의 절차·메뉴·설정 안내와 설정 화면 2장을 갱신했다. 설정 20개와 더보기 메뉴 12개를 대조했으며 417개 파일·앵커 링크 검사를 통과했다. 브라우저에서 주요 페이지의 이미지·가로 넘침·확대 보기와 Esc 닫기를 확인했다.
- `AI_사용설명서.txt`는 67개 본문 항목과 132개 상황별 질문을 담은 단일 사용자 설명서다. 코드 없이 챗봇에 첨부하는 용도이며, HTML 원본과 `docs/manual-intents.json`으로부터 `python scripts/export_ai_manual.py`로 재생성한다.
- 배포 폴더에 실행 파일과 `AI_사용설명서.txt`, `설명서.html`, `html/`을 함께 둔다. HTML 설명서는 `설명서.html`과 `html/`을 함께 전달한다. 챗봇에는 AI용 텍스트 한 파일만 첨부한다.
- 빌드 로그·해시·화면 검증·설명서 검증 결과: `artifacts/build-20261029/`.

## 0.2.0 월중 가구 변경 출력 수정 재빌드 (2026-10-29)

- 사용자 요청으로 현재 소스를 재빌드했다. 월중 가구 변경 시 청구서·확인서를 차단하지 않고 문서 종료일 소속으로 묶는다. 기준일 소속이 없으면 포함된 식사일 중 마지막으로 확인되는 가구를 사용하며 날짜별 식수·금액·과거 소속 기록을 유지한다.
- 앱·Windows 파일·제품 버전은 기존 `0.2.0`을 유지했다.
- 결과: `dist/meal_delivery.exe`, 76,598,866 bytes. SHA-256: `f4aba23cf0d7e8d7ef75b6395e9d84c97dc13ff537a558e1a2fb6e115391adef`.
- 빌드 명령: `python -m PyInstaller --noconfirm --workpath artifacts/rebuild-monthly-household-20261029/work sqlite_v2.spec`.
- 소스 및 실행 파일 `--packaging-smoke` 종료 코드 모두 0. 운영 DB를 열거나 전체 GUI·Excel 기능을 검증한 결과는 아니다.
- 직전 소스 변경 검증: 관련 52건 통과. 전체 회귀 검사는 2,616 passed, 5 failed, 23 deselected(Excel 연동 제외)였다. 실패 5건은 수정 전 월간 문서 계산 코드에서도 재현했으며 상세는 `artifacts/monthly-household-output/validation-summary.json`에 있다. 이번 재빌드에서는 전체 검사를 반복하지 않았다.
- 배포 HTML·AI 설명서 63개 파일이 원본과 동일함을 확인했다. 운영 데이터와 공통 설정은 변경하지 않았다.
- 이전 실행 파일, 빌드 로그, 검사 결과·해시는 `artifacts/rebuild-monthly-household-20261029/`에 보관한다.


## 0.2.0 확인서 다음 달 주말 표시 옵션 재빌드 (2026-10-29)

- 사용자 요청으로 현재 소스를 재빌드했다. 확인서 마지막 주에 다음 달 토·일요일을 표시하는 선택 옵션을 포함하며, 월~일 주차와 기존 식수 계산 기준을 유지한다.
- 앱·Windows 파일·제품 버전은 기존 0.2.0을 유지했다.
- 결과: dist/meal_delivery.exe, 76,600,270 bytes. SHA-256: 87743b5379a90ca64e201e554d41c4e0a4b9c03e232dd8f2bd1d1bddc1501238.
- 빌드 명령: python -m PyInstaller --noconfirm --workpath artifacts/build-confirmation-weekend-20261029/work sqlite_v2.spec.
- 소스와 실행 파일의 --packaging-smoke 종료 코드 모두 0. 전체 GUI·Excel 기능 검증을 대신하지 않는다.
- 직전 변경 검증에서 관련 테스트 97개가 통과했다. 이번 빌드에서는 전체 회귀 검사를 반복하지 않았다.
- 배포 HTML·AI 설명서 63개 파일이 원본과 일치했다. 이전 실행 파일·빌드 로그·해시·검사 결과는 artifacts/build-confirmation-weekend-20261029/에 보관한다.

## 1.0.0 중식 기간·학교 일정·배송 양식 빌드 (2026-11-04)

- 사용자 요청으로 작업 중인 변경 전체를 포함해 빌드했다. 기존 작업본의 앱 버전 `1.0.0`을 유지했다. 개인별 중식 기간, 학교 일정 불러오기·삭제·학년 예외, 기사별 중식 기간표, 배송목록 양식 복구, 일일식수 고정 서식과 오류 안내 개선을 포함한다.
- 빌드 명령: `python -m PyInstaller --noconfirm --workpath artifacts/release-20261104/work sqlite_v2.spec`. 결과: `dist/meal_delivery.exe`, 76,756,173 bytes. Windows 파일·제품 버전은 모두 `1.0.0`이다.
- SHA-256: `f9f95379118699de7b6e56ccf2d38fe4febcbc4527e5eca2d138a4fa908d1d60`. 소스와 실행 파일의 `--packaging-smoke` 종료 코드 모두 0. 이 검사는 전체 GUI·설치된 Excel 연동 기능 검증을 대신하지 않는다.
- 최초 회귀 검사: 3,005 passed, 27 failed, 28 deselected. 실패 22건은 이전 가구·배송순서·서식·오류 문구 기대값, 4건은 새 초기화 흐름을 반영하지 않은 Qt 대역, 1건은 진행창 표시 전 작업을 해제하는 검사 타이머 경쟁이었다. 실제 업무 규칙에 맞춰 기대값을 갱신하고 기사 배정·가입·과거 이력·백업 보존과 잠금 정리 검증을 보강했다. 타이머 경쟁은 같은 표시 지연 조건에서 수정 전 2건 실패, 수정 후 2건 통과로 확인했다. 실패 수정을 위해 앱 업무 동작을 변경하지 않았다.
- 최종 전수 재검증: `QT_QPA_PLATFORM=offscreen`, `-m "not excel"`. 전체 테스트 파일을 겹치지 않는 세 그룹으로 나누어 각각 `python -m pytest -q`로 병행 실행했다. 그룹당 1,011건, 합계 **3,033 passed**, 실패·오류·건너뜀 0건, Excel 검사 28건 제외. 행 추가의 가입 전·후 사례를 나눠 최초 검사보다 1건 늘었다. 잘못된 Excel 헤더 합성 입력을 만드는 기존 검사에서 openpyxl 경고 5건이 남았다.
- 설명서의 학교 일정 누락 제외, 이전 정상 배송목록 양식 사용·복구, 작업본 용어와 갱신일을 실제 동작에 맞췄다. AI 설명서는 75절·상황별 질문 215개이며 8개 HTML의 전체 화면 예시 범위는 `2026.10.26~11.04`다. 링크·자산 참조 480개와 이미지 62개를 확인했다.
- `dist/`의 `설명서.html`, `html/`, `AI_사용설명서.txt` 총 77개 파일이 원본과 일치한다. 설명서는 실행 파일에 포함되지 않으므로 HTML 전달 시 시작 파일과 `html/`을 함께 전달한다.
- 빌드·검사·원인 재현·설명서 검증 로그와 그룹별 JUnit 결과, 해시: `artifacts/release-20261104/`. 운영 자료는 검증에 사용하거나 수정하지 않았다.

## 1.0.0 날짜 선택 중지 정리 도구 (2026-11-04)

- 사용자 요청으로 중지 정리 도구를 별도 콘솔 실행 파일 meal_stop_cleanup.exe로 빌드했다. 실행 파일에 Python을 포함하므로 배포 PC에 Python을 설치할 필요가 없다. 메인 앱과 같은 폴더에 두면 기존 공통 설정의 백업 경로를 읽는다.
- 중지 시작일을 연-월-일 또는 월/일로 입력한다. 전자는 해당 연도만, 후자는 모든 연도의 같은 월·일을 찾는다. 기존 9월 30일 정리 기록은 이전 형식 그대로 검증·복구하며 새 정리 기록에는 선택 날짜 범위를 저장한다.
- 도구 빌드: python -m PyInstaller --noconfirm --workpath artifacts/stop-date-cleanup/tool-work stop_cleanup.spec. 메인 앱도 artifacts/stop-date-cleanup/app-work 경로와 sqlite_v2.spec으로 재빌드했다. 두 파일의 앱·파일·제품 버전은 모두 기존 1.0.0을 유지했다.
- 소스와 두 실행 파일의 --packaging-smoke 종료 코드 모두 0. 실행 파일은 Python이 PATH에 없고 PYTHONHOME·PYTHONPATH가 제거된 환경에서 프로젝트 외부 임시 작업 폴더로 실행했다. 정리 도구는 합성 암호화 DB에서 특정 연도 선택, 다른 대상 보존, 저장·백업·중복 실행·복구·재개방을 검사했다. 별도 실행 파일의 빈 날짜 취소와 잘못된 날짜 거절도 확인했다. 완전히 새 Windows 설치 환경의 전체 인수 검사를 대신하지 않는다.
- 실제 콘솔 검증에서 Windows 리디렉션의 cp949가 시작 안내의 일부 문자를 출력하지 못하는 문제를 발견했다. 리디렉션 출력은 UTF-8을 사용하도록 수정하고 한글·특수문자 보존을 회귀 검사와 최종 실행 파일로 확인했다.
- 관련 회귀 검사: 날짜 선택·기존 9월 30일 동작·오류 안내 79건, 식종 이력·취소·작업본·복구·암호화 저장 94건, 합계 173건 통과. 실제 운영 파일과 공통 설정은 검증에 사용하거나 수정하지 않았다.
- 사용 안내와 배치 파일을 dist에 함께 복사했다. 기존 9월30일_중지정리.cmd는 날짜 선택형 도구로 연결된다. HTML·AI 설명서는 76절·질문 220개로 갱신했으며 482개 로컬 참조와 배포 설명서 77개·도구 안내/배치 4개 파일의 원본 일치를 확인했다.
- meal_stop_cleanup.exe: 74,282,078 bytes, SHA-256 92247a5e6c8da8af70109751c66357e422cf0aa77da124bdf7068c6b54f5fd50.
- meal_delivery.exe: 76,760,296 bytes, SHA-256 2f59d3faa05fa2a14743e281f74ed0a9ddc71d4486017cdbcebd695a243e7c6d.
- 빌드·실행·콘솔·회귀·설명서 검증 기록: artifacts/stop-date-cleanup/.

## 1.0.0 명단 열 연결·선택 셀 일괄 변경 배포 (2026-11-05)

- 사용자 요청으로 최신 작업본을 빌드하고 `Z:\2026\행정\DB 수정중`의 실행 파일과 설명서를 갱신했다. 일반·중식 명단은 열 자동 연결 없이 시작하고 일반 명단의 식종 연결은 유지하며 중식 명단에서만 식종 연결 구역을 숨긴다. 최초 DB 생성의 자동 연결은 유지한다. 일반 셀 우클릭 일괄 변경·완전/부분 일치·가구 공용 자동 반영과 앞서 수정한 학교 일정 후 중식 명단 불러오기 오류 수정도 포함한다.
- `artifacts/import-manual-build/work`를 작업 경로로 빌드했다. 파일·제품 버전은 `1.0.0`, 실행 파일은 76,776,602 bytes, SHA-256은 `55133bd09d0338bee2d0c2752c96487534c668ba8f988d5d53ffb7eafb25f750`이다. 소스와 실행 파일의 `--packaging-smoke` 종료 코드 모두 0이다.
- 명단 연결·중식 반영·오류 안내 관련 126개 검사가 통과했다. 일괄 변경 관련 선행 검증은 248개 통과했다. 전체 회귀 검사나 설치된 Excel 연동 검사를 이번 배포에서 다시 실행하지 않았다. 일반·중식 연결창을 합성 자료로 캡처해 표시 상태도 확인했다.
- HTML 8개·로컬 참조 486개를 확인하고 AI 설명서를 76절·질문 228개로 생성했다. 배포 설명서 79개와 실행 파일의 해시가 원본과 일치하며 `dist/` 설명서도 갱신했다. 이전 배포 파일은 `artifacts/import-manual-build/previous-20261105-094001/`에 보관했다.
- 빌드·실행·배포 기록은 `artifacts/import-manual-build/`, 열 연결 검사·화면·설명서 검증은 `artifacts/import-manual-mapping/`에 있다. 실제 운영 DB와 공통 설정은 수정하지 않았다.
