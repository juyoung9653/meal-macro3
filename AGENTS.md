# 프로젝트 작업 지침

- UI를 추가하거나 수정하기 전에 [UI 디자인·상호작용 기준](docs/ui-guidelines.md)을 읽고 따른다.
- 사용자의 현재 지시가 문서보다 우선한다. 새로운 UI 규칙이 확정되면 해당 문서도 함께 갱신한다.
- 기존 작업 중인 변경을 보존하고, 요청과 관계없는 화면이나 동작을 변경하지 않는다.
- 사용자가 명시적으로 요청하기 전에는 실행 파일을 빌드하지 않는다.
- 오류 안내는 `src/desktop_v2/user_messages.py`의 공통 처리를 거치며, 원인과 사용자가 취할 다음 행동을 설명한다. 내부 예외·식별자·자료형을 그대로 노출하거나 단어만 한글로 치환하지 않는다. 저장 충돌·자료 검증 실패·백업 실패를 구분하고, 프로그램 오류를 사용자 입력 잘못으로 단정하지 않는다. 오류 처리 추가·수정 시 `tests/test_user_messages.py`의 안내 및 노출 방지 검사를 함께 실행한다.

## 서브 에이전트 진행 확인

- 사용자의 추가 요청·수정 사항 전달이나 작업 중단 지시는 필요한 시점에 바로 전달한다.

## 개발 검증 자료

- 보관할 테스트 로그·스크린샷·검증용 설정 파일은 `artifacts/<작업명>/`에 저장한다. 저장소 루트에 생성하지 않는다.
- 보관할 필요가 없는 테스트 임시 파일은 pytest의 `tmp_path` 또는 시스템 임시 폴더를 사용한다.

## 프로젝트 길잡이

- 현재 앱은 Python·PySide6 기반 SQLite v2다. 실행 진입점은 `sqlite_v2.py`, 앱 버전은 `src/version.py`, Windows 단일 실행 파일 설정은 `sqlite_v2.spec`이다.
- 사용자 설명서는 [설명서.html](설명서.html)과 `html/`에 둔다. 메뉴·절차가 바뀌면 해당 장을 함께 갱신한다. 이 개발 지침에는 사용자 설명서를 복제하지 않고 필요한 코드와 문서를 안내한다. 챗봇에 첨부할 사용자 설명서는 아래 배포용 설명서 갱신 절을 따른다.
- 화면·상호작용: [UI 기준](docs/ui-guidelines.md). 식수·배송·청구 계산: [서비스 규칙](docs/service-rules.md). 암호화·이력·백업: [저장소 문서](docs/sqlite-storage.md). 패키징 기록: [빌드 검증](docs/packaging-spike.md).
- `HANDOFF.md`는 과거 진행 기록이다. 당시 미구현 항목·테스트 개수·빌드 보류를 현재 상태로 해석하지 않는다. 실제 코드와 최신 규칙을 먼저 확인한다.
- 코드 위치: `src/desktop_v2/` 화면·작업 흐름, `src/domain/` 업무 모델·계산, `src/importing/` Excel 입력·검토, `src/services/` 저장·문서 작업, `src/storage/` 암호화 DB·백업, `src/generators/`·`src/excel/` Excel 출력, `assets/` 기본 양식.
- 검증에는 합성 데이터와 임시 DB를 사용한다. 실제 운영 파일·명단·공통 설정을 검증용으로 수정하지 않는다.

## 실행·검증·빌드

저장소 루트에서 실행한다. 의존성은 `requirements-sqlite-v2.txt`를 기준으로 확인한다.

- 소스 실행: `python sqlite_v2.py`
- 회귀 테스트: `python -m pytest -q tests`. 설치된 Excel을 사용하는 검사를 제외할 때는 `-m "not excel"`을 추가하고, 제외 사실을 결과에 명시한다.
- 소스 패키징 확인: `python sqlite_v2.py --packaging-smoke`
- 사용자 빌드 요청이 있을 때: `python -m PyInstaller --noconfirm --workpath artifacts/<작업명>/work sqlite_v2.spec`
- 결과: `dist/meal_delivery.exe`. 기존 버전을 임의로 올리지 않는다. 빌드 후 파일 버전과 실행 파일의 `--packaging-smoke` 종료 코드 0을 확인한다. 이 검사는 공휴일 데이터 포함 여부를 확인하며 전체 GUI·Excel 기능 검증을 대신하지 않는다.
- 배포 설명서는 실행 파일에 포함되지 않는다. 설명서 전달 시 `설명서.html`과 `html/` 폴더를 함께 전달한다.

## 배포용 설명서 갱신

- 배포 사용자에게 제공하는 AI 설명서는 `AI_사용설명서.txt`다. 사용자 설명서이며 개발 지침과 구분한다.
- 사용법의 기준 원본은 `설명서.html`과 `html/01-*.html`~`html/07-*.html`이다. 상황별 질문 연결은 `docs/manual-intents.json`에 관리한다.
- HTML을 수정한 뒤 `python scripts/export_ai_manual.py`를 실행하면 전체 본문·질문 찾아보기를 포함한 단일 AI 설명서와 HTML 시작 페이지의 질문 목차를 함께 갱신한다. AI 설명서만 직접 고치지 않는다.
- 메뉴·옵션을 추가하거나 이름을 바꾸면 본문·질문 연결·화면 예시를 대조한다. 사용자에게 보이지 않는 내부 기능을 조작 가능한 메뉴처럼 안내하지 않는다.
- 설명서 갱신일과 대상 앱 버전을 확인하고 배포 폴더의 설명서도 최신 생성본으로 맞춘다.
