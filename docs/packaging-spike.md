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
청구서·확인서 생성 및 설정 허브 UI까지 포함한 최신 `meal_delivery_sqlite_v2.exe`는
73,939,294 bytes였다.
SHA-256은 `64c50906106c41521438465b49dae317130a8e48888da1edbcf129e0415b9321`이다.
PyInstaller 분석에서 학교 일정 import·직접 편집·문서 계산·생성 모듈 포함을 확인했고 번들 내부 smoke도
종료 코드 0으로 통과했다.

이 파일은 마지막으로 **완료된** 패키징 검증 산출물이다. 이후 소스에 추가한 배송 리포트
생성, schema 5와 조식 배송 규칙 설정은 사용자가 기능을 검토한 뒤 다시 빌드하기로 해 아직
이 실행 파일에 포함되지 않았다. 따라서 현재 `dist/meal_delivery_sqlite_v2.exe`를 새 기능의
검토·배포본으로 사용하지 않는다.

실행파일의 숨은 `--packaging-smoke` 검사는 2026년 삼일절 대체 휴일과 제헌절의 한국어
명칭을 번들 내부에서 읽어 종료 코드 0을 반환했다. 따라서 앱 실행 중 인터넷 연결이나
개발 PC의 별도 `holidays` 설치 없이 공휴일 후보를 만들 수 있다. 빌드 경고는 Windows에서
없는 POSIX 모듈과 선택 의존성 위주였고, 공휴일 smoke가 사용하는 필수 모듈 누락은 없었다.

```powershell
python -m PyInstaller --noconfirm --clean .\sqlite_v2.spec
Start-Process .\dist\meal_delivery_sqlite_v2.exe `
  -ArgumentList '--packaging-smoke' -WindowStyle Hidden -Wait
```

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
