# `db.xlsx` 기반 코드 대체 계획

## 1. 개요

현재 코드는 외부 명단 파일(CSV/Excel)을 불러와서 **수동 컬럼 매핑**, **달력 기반 배송일 계산**, **비고 문자열 파싱** 등을 수행.
그런데 `db.xlsx` 안에 **Power Query**가 이미 배송일을 계산해놓고 모든 데이터가 통합되어 있음.

→ **`db.xlsx`를 단일 데이터 소스**로 삼고, 기존 복잡한 파이프라인을 대체.

---

## 2. `db.xlsx` 데이터 구조 (발견된 내용)

| 시트 | 테이블명 | 주요 컬럼 | 용도 |
|---|---|---|---|
| **명단** | `명단` | ID, 학교명, 학년, 관할동, 성명, 생년월일, 도로명주소, 전화번호, 핸드폰, 석식여부, 조식여부, 중식여부, 비고, 취학유형 | 기존 `RosterLoader`가 읽던 명단 데이터 |
| **접수장** | `접수장` | 접수일, ID, 이름(XLOOKUP), 식종, 상태, 적용일, 종료일, 요일패턴, 접수방법 | 구독/신청 정보 (식종+기간+요일패턴) |
| **배송전체** | `배송전체` (PQ) | 관할동, ID, 이름, 변동유무, 식종, **6/1~6/30 일별 O/X** | **핵심**: 이미 계산된 개인별 일별 배송 여부 |
| **배송정보** | `배송순서` (PQ) | 관할동, 도로명주소, 핸드폰, 식종, 배송유무, 배송순서, 배송담당, 비고 | 배송 라우팅 정보 |
| **일일식수** | `일일식수_조/중/석식` (PQ) | 배송담당, 관할동, **1~5일/total 건수** | 배송담당별 일자별 식수 합계 |
| **기타** | `방학일정` | 구분, 학교명, 방학날짜, 개학날짜, 급식제공여부 등 | 방학 일정 |
| **기타** | `배송휴무` | 휴무일 | 공휴일/휴무일 목록 |
| **기타** | `식종별_배송일` | 식종, 배송일 | 식종→배송요일 매핑 |
| **기타** | `일시중지_적용일` (PQ) | 총 적용일 | 일시중지 적용일 계산 결과 |

**Power Query 연결 (8개):** `마스터_배송데이터`, `배송순서`, `배송순서_수동입력`, `배송전체`, `일시중지_적용일`, `일일식수_석/조/중식`

---

## 3. 대체 전략

### 원칙

- **`db.xlsx`를 유일한 데이터 소스**로 사용. 외부 파일 로드 완전 제거.
- **`db.xlsx`는 읽기 전용**으로만 열고 절대 쓰지 않음.
- 기존 코드가 외부 파일 + 수동 계산하던 것을 **db.xlsx에서 이미 계산된 값**을 읽어오는 방식으로 전면 교체.
- Power Query가 계산해놓은 값은 `data_only=True`로 읽으면 캐시된 값이 나옴.

### 단계별 계획

---

### Phase 0: `DbReader` 모듈 신규 생성

**신규 파일:** `src/dbreader.py` (또는 `src/excel/db_reader.py`)

**책임:**
- `db.xlsx` 경로 설정 (.settings.json에 경로 저장)
- `openpyxl`로 각 시트/테이블 읽기
- Power Query 출력 테이블 (`배송전체`, `배송순서`, `일일식수_*`) 읽기
- 데이터 타입 변환 (날짜 직렬번호 → `date`, 문자열 bool → `bool`)
- 조회 메서드 제공

```python
class DbReader:
    PATH = "assets/db.xlsx"
    
    def read_roster(self) -> list[dict]        # 명단 데이터
    def read_registrations(self) -> list[dict]  # 접수장 데이터
    def read_delivery_matrix(self) -> list[dict]  # 배송전체 (일별 O/X)
    def read_delivery_order(self) -> list[dict]   # 배송정보
    def read_daily_meal_counts(self, meal_type) -> list[dict]  # 일일식수
    def read_vacation_schedule(self) -> list[dict]  # 방학일정
    def read_holidays(self) -> list[date]         # 배송휴무
    def read_meal_day_map(self) -> dict           # 식종별_배송일
```

---

### Phase 1: 명단 로드 전면 교체 (`RosterLoader` 제거 → `DbReader`)

**현재:** `RosterLoader`가 외부 Excel/CSV 읽어서 `RosterParser`로 파싱, `MappingDialog`로 수동 매핑

**변경:**
- `InvoiceTab._select_file()` → "db.xlsx 불러오기" 버튼으로 변경 (파일 선택 다이얼로그에서 `db.xlsx` 고정 or 선택)
- **외부 명단 파일 로드 기능 완전 제거**
- 명단 데이터는 `DbReader.read_roster()`로 바로 dict 리스트 획득
- **컬럼명이 이미 정해져 있음** → `MappingDialog` 불필요
- 명단 시트의 `석식여부/조식여부/중식여부/비고` 컬럼을 그대로 `_breakfast`, `_dinner`, `_lunch`, `_note` 플래그로 사용

**제거 대상:**
- `src/services/roster_loader.py` (전체 파일)
- `src/excel/excel_utils.py` (전체 파일)
- `src/services/lunch_parser.py` (전체 파일)

---

### Phase 2: 배송일 계산 대체 (`delivery_calculator` + `schedule_counter` + `vacation_calculator` + `note_parser`)

**현재:** 4개 모듈이 복잡한 달력 계산 수행

**변경:**
- **`배송전체` 시트가 이미 개인×일자별 O/X를 계산해놓음**
- 각 사람의 `ID`를 키로 `배송전체`에서 해당 월의 배송일을 바로 조회
- `O`/`X` 값을 그대로 배송 건수로 집계
- 비고 기반 차감 로직도 Power Query `일시중지_적용일`이 이미 계산함

**대체 방안 상세:**

| 기존 함수 | 대체 방법 |
|---|---|
| `count_delivery_days()` | `배송전체`에서 ID로 해당 월 O/X를 SUM |
| `count_weekdays()` | 필요 없음 (PQ가 이미 계산) |
| `count_lunch_vacation_days()` | `일일식수_중식`에서 직접 조회 |
| `apply_note_to_month()` | `일시중지_적용일`이 이미 계산 |
| `calc_weekly_breakdown()` | `배송전체` 일별 데이터를 주차로 그룹핑 |
| `get_kr_holidays()` | `배송휴무` 시트에서 직접 읽음 |
| `_get_schedule()` | `식종별_배송일` 시트에서 직접 읽음 |

**제거 대상:**
- `src/services/delivery_calculator.py` (전체)
- `src/services/schedule_counter.py` (전체)
- `src/services/vacation_calculator.py` (전체)
- `src/services/note_parser.py` (전체)
- `src/services/note_parsers/` 디렉토리 (전체)
- `src/services/holiday_fetcher.py` (대체 or 단순화)

---

### Phase 3: InvoiceService/InvoiceController 단순화

**현재:**
```python
# InvoiceService.generate()에서 하는 일:
1. 각 person의 flag 추출 (col_mapping 필요)
2. lunch_roster.find_school() → 방학 계산
3. count_delivery_days() → 월별 배송일 계산
4. person_to_dict() → EnrichedPerson
5. make_invoice() 호출
```

**변경:**
```python
# 새로운 generate():
1. DbReader에서 명단 + 배송전체 + 배송정보 로드
2. 각 person의 배송일을 배송전체에서 바로 조회
3. 배송정보에서 배송순서/배송담당 매핑
4. person_to_dict() → dict (col_mapping 거의 불필요)
5. make_invoice() 호출 (기존 유지 or 변경)
```

**변경 대상:**
- `src/services/invoice_controller.py` → `InvoiceService` 내부 로직 대체
- `src/core/models.py` → `EnrichedPerson` 생성 로직 단순화 (flag를 db에서 직접 읽음)

---

### Phase 4: 컬럼 매핑 다이얼로그 전면 제거

**현재:** `MappingDialog`에서 명단 헤더 → 템플릿 컬럼 수동 매핑

**변경:** `db.xlsx` 명단은 **컬럼명이 고정**되어 있으므로:
- 템플릿 컬럼 매핑은 자동으로 처리 (컬럼명 기반 lookup)
- `LunchMappingDialog` 불필요 (방학일정을 `DbReader`에서 직접 읽음)
- 매핑 다이얼로그 **모두 제거**

**제거 대상:**
- `src/widgets/mapping_dialog.py` (전체)
- `src/widgets/lunch_mapping_dialog.py` (전체)
- `src/widgets/base_mapping_dialog.py` (전체)

---

### Phase 5: 세부 계산 함수 대체 (InvoiceTab 내부)

**변경 대상:**
- `InvoiceTab._open_mapping()` → mapping 불필요 (또는 템플릿 매핑만)
- `InvoiceTab._open_lunch_mapping()` → 불필요
- `InvoiceTab._reload_lunch_roster()` → 불필요
- `InvoiceTab._select_lunch_file()` → 불필요
- `InvoiceTab._sync_delivery_cols_to_mapping()` → 불필요

---

### Phase 6: 템플릿 config → db.xlsx 기반 설정

**현재:** `src/templates/config.py` + `.settings.json` 에서 템플릿 컬럼/설정 관리

**변경:**
- `식종별_배송일` 시트의 값을 `.settings.json`의 `schedule` 대체
- `배송휴무` 시트가 `holiday_fetcher`를 대체
- `.settings.json`의 `flag_search_terms`, `semantic_columns` 등은 db.xlsx 사용 시 불필요 (컬럼명이 고정이므로)

---

## 4. 파일별 영향도 매트릭스

| 파일 | 상태 | 비고 |
|---|---|---|
| `main.py` | ✅ 유지 | 변경 없음 |
| `src/main_window.py` | ✅ 유지 | 변경 없음 |
| `src/core/settings.py` | 🔧 수정 | `db_path` 설정 추가, 불필요한 설정 제거 |
| `src/core/models.py` | 🔧 수정 | `EnrichedPerson` 생성 방식 변경 (db 직접) |
| `src/core/date_utils.py` | 🔧 수정/축소 | 필요 시 유지 |
| `src/core/meal_types.py` | ✅ 유지 | MealType enum은 계속 사용 |
| `src/core/invoice_data.py` | ❓ 검토 | 파일 확인 필요 |
| `**src/excel/db_reader.py**` | **🆕 신규** | 핵심 신규 모듈 |
| `src/excel/excel_utils.py` | 🔧 축소 | 불필요한 함수 제거 |
| `src/services/delivery_calculator.py` | **🗑️ 제거** | db.xlsx가 대체 |
| `src/services/schedule_counter.py` | **🗑️ 제거** | db.xlsx가 대체 |
| `src/services/vacation_calculator.py` | **🗑️ 제거** | db.xlsx가 대체 |
| `src/services/note_parser.py` | **🗑️ 제거** | db.xlsx가 대체 |
| `src/services/note_parsers/` | **🗑️ 제거** | db.xlsx가 대체 |
| `src/services/holiday_fetcher.py` | 🔧 대체 | `DbReader.read_holidays()`로 대체 |
| `src/services/roster_loader.py` | **🗑️ 제거** | 외부 파일 로드 완전 폐기 |
| `src/services/lunch_parser.py` | **🗑️ 제거** | 방학데이터 db.xlsx에서 직접 |
| `src/services/lunch_data.py` | **🗑️ 제거** | `LunchRoster`/`LunchSchoolEntry` 불필요 |
| `src/services/invoice_controller.py` | **🔧 대규모 수정** | `InvoiceService` 로직 전면 교체 |
| `src/generators/invoice_generator.py` | 🔧 수정 | `make_invoice()` 시그니처 변경 가능 |
| `src/generators/confirm_generator.py` | ✅ 유지 | 단순화 가능하나 변경 필수 아님 |
| `src/templates/config.py` | 🔧 수정 | `read_template_columns()` 유지, 나머지 축소 |
| `src/templates/template_scanner.py` | ✅ 유지 | 변경 없음 |
| `src/widgets/invoice_tab.py` | **🔧 대규모 수정** | mapping/lunch 관련 UI 제거 |
| `src/widgets/confirm_tab.py` | 🔧 수정 | db.xlsx → 배송담당/전화번호 자동 매핑 |
| `src/widgets/mapping_dialog.py` | **🗑️ 제거** |
| `src/widgets/lunch_mapping_dialog.py` | **🗑️ 제거** |
| `src/widgets/base_mapping_dialog.py` | **🗑️ 제거** |
| `src/widgets/settings_dialog.py` | 🔧 수정 | db.xlsx 경로 설정 UI 추가 |
| `.settings.json` | 🔧 수정 | `db.path` 추가, 불필요한 설정 제거 |

---

## 5. 데이터 흐름 (변경 후)

```
db.xlsx
  │
  ├─ 명단 ────────────────────→ list[dict]  (성명, 학교명, 학년, 관할동, ...)
  ├─ 배송전체 ─────────────────→ list[dict]  (ID + 일별 O/X 행렬) 
  ├─ 접수장 ──────────────────→ list[dict]  (ID + 식종 + 적용일 + 요일패턴)
  ├─ 배송정보 ────────────────→ list[dict]  (배송순서, 배송담당, ...)
  ├─ 배송휴무 ────────────────→ set[date]   (휴무일 목록)
  ├─ 식종별_배송일 ───────────→ dict        (식종→배송요일)
  ├─ 방학일정 ────────────────→ list[dict]  (학교별 방학/개학, 급식제공여부)
  └─ 일일식수_조/중/석식 ──────→ list[dict]  (배송담당별 일별 식수)
         │
         ▼
   DbReader (신규)
         │
         ├──► InvoiceTab / InvoiceService
         │     ├─ 배송일 = 배송전체에서 ID로 조회 (더 이상 계산 안 함)
         │     ├─ 배송담당 = 배송정보에서 매핑
         │     └─ make_invoice() 호출
         │
         └──► ConfirmTab
               ├─ 배송정보에서 배송순서/배송담당 읽음
               └─ make_confirm() 호출
```

---

## 6. 주요 리스크 및 고려사항

| 리스크 | 대응 |
|---|---|
| Power Query가 새로고침 안 되면 `배송전체` 데이터가 구버전 | `data_only=True`로 읽으면 마지막 캐시 값 사용. 명시적 문서화 |
| `db.xlsx`가 열려 있으면 `openpyxl` 읽기 충돌 | `read_only=True` 모드 사용. 또는 `calamine` 엔진 fallback |
| `db.xlsx`의 명단 구조가 변경되면? | 컬럼명 기반 lookup이므로 컬럼명 변경 시만 영향. 에러 처리 강화 |
| 6월 데이터만 있음 (`6/1~6/30`) | 다른 월 데이터는 `접수장` + Power Query로 자동 생성됨. 월별 자동 대응 확인 필요 |

---

## 7. 권장 실행 순서

```
Phase 0: DbReader 신규 생성 (src/excel/db_reader.py)
    ↓
Phase 1: InvoiceTab에서 명단 로드를 DbReader로 변경 (RosterLoader 분기)
    ↓
Phase 2: 배송일 계산을 배송전체 조회로 변경 (delivery_calculator 등 제거)
    ↓
Phase 3: InvoiceService/Controller 단순화 (col_mapping 의존성 제거)
    ↓
Phase 4: MappingDialog 제거/간소화 (LunchMappingDialog 제거)
    ↓
Phase 5: InvoiceTab UI 정리 (lunch 관련 UI 제거)
    ↓
Phase 6: 설정파일 정리 (불필요 설정 제거)
    ↓
Phase 7: 테스트 수정 및 검증
```

각 Phase는 독립적으로 테스트 가능. Phase 0→1→2 순서로 진행해야 데이터 흐름이 끊기지 않음.
