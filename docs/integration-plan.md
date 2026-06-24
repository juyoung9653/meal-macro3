# meal_macro2 → meal-macro3 통합 계획

## 목표

meal_macro2 **배송 리포트 생성** 기능을 meal-macro3에 3번째 탭으로 통합.
기존 meal-macro3 코드 최대 재사용, 필요한 로직만 포팅. pandas/numpy 불필요 — list[dict] + openpyxl + stdlib로 처리.

---

## 설계 개선 사항 (통합 계기로 같이 수정)

| # | 문제 | 해결 | 파일 |
|---|------|------|------|
| 1 | `InvoiceService`가 `get_db_reader()` 싱글턴 직접 호출 → 테스트 어려움 | 생성자에서 `DataSource` 주입받도록 변경 | `services/invoice_controller.py` |
| 2 | `templates/config.py` `_get_semantic_column_roles()` fallback이 `settings.py` DEFAULTS와 중복 | fallback 제거, settings만 참조 | `templates/config.py` |
| 3 | `DeliveryService`도 같은 실수 반복 방지 | 처음부터 `DataSource` 생성자 주입 | `services/delivery_service.py` (신규) |
| 4 | Staff sort 저장소 JSON 파일 vs QSettings 혼용 | `QSettings`로 통일 | `widgets/order_editor.py` |
| 5 | `InMemoryDataSource`가 실험실에서만 존재, 실제 테스트에 미활용 | `DeliveryService` 단위 테스트에 즉시 활용 | `tests/test_delivery_service.py` |

---

## 비교: 뭐 가져오고 뭐 버리고

### meal-macro3가 이미 가진 것 (재사용)

| 영역 | meal-macro3 파일 | meal_macro2 대체 대상 |
|------|------------------|----------------------|
| DB 읽기 | `excel/db_reader.py` | `reader.py` — **완전 대체**. `명단/배송전체/배송정보/기타` 모든 시트 + 접수장 비고 + 배송휴무 + 방학일정 제공 |
| 접수장 메모 | `DbReader.read_roster()` → `list[dict]` 비고 컬럼 | `memo.py`의 **입력 소스** — 명단 시트 비고 컬럼 값 직접 사용. 파싱 로직 자체는 `core/memo.py`가 담당 |
| 배송휴무 | `DbReader.read_holidays()`, `get_holiday_set()` | `utils.py` + `holidays` 라이브러리 — **완전 대체**. 기타 시트 휴무 테이블이 공휴일 데이터 제공 |
| 설정 | `core/settings.py` | `config.py` — **완전 대체**. `SettingsManager` 싱글턴 더 유연 |
| 파일/상태 캐시 | `QSettings` 기반 (main_window, invoice_tab 등) | `cache.py` — 불필요 |
| 엑셀 템플릿 처리 | `templates/config.py`, `excel/excel_utils.py` | exporter 일부 — `copy_style`, `adjust_formula` 등 이미 있음 |
| 메인 GUI 구조 | `main_window.py` (QTabWidget) — 탭만 추가하면 됨 | main_window 모놀리식 → 버림 |

### meal_macro2에서 가져올 것 (포팅 필요)

| 모듈 | 포팅 대상 | 새 위치 | 변경량 |
|------|----------|---------|--------|
| `memo.py` | 메모 파싱 로직 (일시중지/배송/IN/OUT/기간) | `core/memo.py` | **그대로**. 입력 소스만 변경: 명단 시트 접수장의 `비고` 컬럼 값에서 읽어온 문자열을 파싱함. `reader.py` 경유 불필요 — `DbReader.read_roster()`에서 바로 `person["비고"]` 전달 |
| `diff_format.py` | diff 표기 `3(+1)` 생성/파싱 | `core/diff_format.py` | **거의 그대로**. tiny utility |
| `builder.py` | 배송 리포트 집계 코어 | `services/delivery_service.py` | **많이 바꿈**. pandas/numpy → list[dict] + stdlib. **DataSource 생성자 주입** |
| `calendar_widget.py` | 주말/공휴일 차단 캘린더 | `widgets/meal_calendar.py` | **수정**. `holidays` 라이브러리 의존 제거. `DbReader.get_holiday_set()`을 통해 `기타` 시트의 배송휴무 테이블에서 공휴일 데이터 공급받음 |
| `order_editor.py` | 배송순서 편집 다이얼로그 | `widgets/order_editor.py` | **수정**. `cache.py` → `QSettings` 기반 저장 |
| `exporter.py` | 리포트 엑셀 출력 (템플릿 복사 + water-filling + 페이지설정) | `generators/report_generator.py` | **일부 수정**. openpyxl만 사용 (이미 의존성 있음), pandas/calamine 의존 제거 |
| `통합 문서1.xlsx` | 리포트 템플릿 파일 | `assets/report_template.xlsx` | **그대로 복사**. 시트명 `aaa` 유지 |
| 신규 탭 UI | — | `widgets/report_tab.py` | **신규 작성**. invoice_tab.py 스타일 따름 |

### 버릴 것

| meal_macro2 | 사유 |
|-------------|------|
| `reader.py` | `DbReader`로 완전 대체 |
| `config.py` | `SettingsManager`로 대체 |
| `cache.py` | `QSettings` 기반으로 대체 |
| `utils.py` (`holidays` 라이브러리) | `DbReader.read_holidays()`가 `기타` 시트 배송휴무 테이블에서 직접 읽음 |
| `main.py` (auto-install, logging) | v3 entry point 유지 |
| `__pycache__/` | 불필요 |
| `통합 문서1.xlsx` 코드는 버림 | 파일만 asset으로 이동 |

---

## 데이터 흐름 (신규 리포트 탭)

```
MainWindow
  └─ ReportTab
       ├─ DbReader (DataSource 인터페이스로 주입)
       │    ├─ read_delivery_matrix()  → 배송전체 O/X
       │    ├─ read_roster()           → 명단/접수장 (비고 → memo 파싱)
       │    ├─ read_delivery_contacts()→ 배송담당/연락처
       │    ├─ get_holiday_set()       → 캘린더 휴무일 표시
       │    └─ read_meal_prices()      → 식종정보 (참고용)
       │
       ├─ DeliveryService(data_source)
       │    └─ build_all(start, end, meal_filter?)
       │         ├─ 날짜별 배송여부 판단 (배송전체 O/X)
       │         ├─ memo: person["비고"] → get_active_memo()
       │         ├─ 주말/휴무 제외 (get_holiday_set())
       │         ├─ 담당자별 count 집계
       │         └─ 이전 배송일 대비 diff 계산
       │
       ├─ QTabWidget (날짜별 탭) + QTableWidget (통계)
       │
       └─ ReportGenerator.export() → report_template.xlsx
```

---

## 구현 순서

### Step 1 (선행): `templates/config.py` 설정 중복 제거

**파일:** `src/templates/config.py`
**작업:** `_get_semantic_column_roles()`의 자체 fallback dict 제거.
`settings.py` `DEFAULTS["semantic_columns"]`가 유일한 진실 공급원.
```python
def _get_semantic_column_roles():
    roles = get_settings().get("semantic_columns", default={})
    if not roles:
        raise RuntimeError("semantic_columns not configured in settings")
    return roles
```
**왜 지금:** 새 기능 추가 전에 중복 제거. 통합 후 semantic_column 늘어날 때 불일치 방지.

### Step 2 (선행): `InvoiceService` 생성자 주입으로 변경

**파일:** `src/services/invoice_controller.py`
**작업:**
- `InvoiceService.__init__(self, data_source: DataSource | None = None)` 추가
- `data_source`가 None이면 기존처럼 `get_db_reader()` 사용 (하위 호환)
- `generate()` 내부 `reader = get_db_reader()` 대신 `self._data_source` 사용
- `get_db_reader` import는 유지 (fallback용)

```python
class InvoiceService:
    def __init__(self, data_source: DataSource | None = None):
        self._data_source = data_source

    def _get_reader(self, db_path: str | None) -> DataSource:
        if self._data_source is not None:
            return self._data_source
        return get_db_reader(db_path)

    def generate(self, config: InvoiceGenerationConfig) -> InvoiceResult:
        reader = self._get_reader(config.db_path)
        ...
```
**변경 영향:** 기존 호출자(invoice_tab.py `_generate_invoices`)는 인자 없이 `InvoiceService()` 사용 → fallback 작동. 테스트에서만 `InvoiceService(InMemoryDataSource(...))` 주입.

### Step 3: `core/memo.py` — 메모 파싱 (포팅)

**파일:** `src/core/memo.py`
**상태:** meal_macro2 `memo.py`를 **그대로** 복사.
**변경점:**
- `import pandas as pd` 제거 — `get_active_memo`의 `target_dt` 타입 `pd.Timestamp` → `datetime.date`
- **입력 소스**: 명단 시트(접수장) `비고` 컬럼. `DeliveryService`가 `DbReader.read_roster()`에서 `person["비고"]` 가져와 `get_active_memo()`에 전달.
  meal_macro2처럼 `reader.py` → Excel raw string 파싱 안 함. `DbReader`가 이미 dict로 읽어줌.
- `_normalize_memo()` 캐시는 유지 (동일 메모 중복 파싱 방지).
**의존성:** stdlib only.

### Step 4: `core/diff_format.py` — Diff 표기 (포팅)

**파일:** `src/core/diff_format.py`
**상태:** meal_macro2 `diff_format.py` 그대로 복사.
**의존성:** stdlib only.

### Step 5: `services/delivery_service.py` — 배송 리포트 집계 (신규)

**파일:** `src/services/delivery_service.py`
**설계:**
- 생성자: `def __init__(self, data_source: DataSource, staff_sort: dict | None = None)`
- `DataSource` 인터페이스에만 의존 → `InMemoryDataSource`로 단위 테스트 가능
- pandas/numpy 전혀 안 씀 — `list[dict]` + `collections.Counter` + `defaultdict`
- `_get_delivery_dates` → `datetime.date` 기반, pandas Timestamp 없음
- `_norm` 날짜 변환 불필요 (DbReader가 이미 처리)

```python
class DeliveryService:
    def __init__(self, data_source: DataSource, staff_sort: dict | None = None):
        self._ds = data_source
        self._staff_sort = staff_sort or {}

    def build_report(self, target_day: date, meal: str) -> list[list]:
        """단일 날짜 리포트."""
        ...

    def build_all(
        self, start: date, end: date, meal_filter: str | None = None
    ) -> list[tuple[str, str, list[list]]]:
        """날짜 범위 리포트. 주말/휴무 자동 제외."""
        ...
```
**핵심 로직 변화 (meal_macro2 builder.py 대비):**

| 항목 | meal_macro2 (pandas/numpy) | meal-macro3 (stdlib) |
|------|---------------------------|---------------------|
| 데이터 타입 | `pd.DataFrame` → `np.ndarray` | `list[dict]` |
| 컬럼 접근 | `header_idx[str] → data[:, idx]` | `row["컬럼명"]` |
| 문자열 strip | `np.char.strip(arr)` | `s.strip()` |
| 조건 마스크 | `np.logical_or.reduce(...)` | `any(...)` / set |
| 중복 제거 | `seen` set 수동 관리 | 동일 |
| diff 계산 | 동일 (`Counter`) | 동일 |

**왜 DataSource 필수인가:** `DeliveryService` 내부에서 `self._ds.read_delivery_matrix()`, `self._ds.read_roster()`, `self._ds.get_holiday_set()`을 모두 사용. 주입 없으면 `InvoiceService`와 같은 문제 반복.

### Step 6: `widgets/meal_calendar.py` — 캘린더 위젯 (포팅 + 수정)

**파일:** `src/widgets/meal_calendar.py`
**상태:** `calendar_widget.py` 기반.
**변경점:**
- `get_korean_holidays()` (`holidays` 라이브러리) → **완전 제거**
- 생성자 시그니처: `def __init__(self, holiday_set: set[date] | None = None, parent=None)`
- `holiday_set`이 없으면 빈 set → 주말만 차단 (데이터 없어도 크래시 안 남)
- `_holidays` → `_holiday_set` (타입: `set[date]`)
- `_is_invalid_date`에서 `self._holiday_set`과 주말 둘 다 체크
- `paintCell`에서 공휴일은 빨간색, 토요일은 파란색 표시 (기존 로직 유지)
- `set_holidays(holidays: set[date])` 메서드로 런타임에 갱신 가능
**의존성:** `holidays` 패키지 불필요. `DbReader.get_holiday_set()`의 결과를 받아서 쓰면 됨.

### Step 7: `widgets/order_editor.py` — 배송순서 편집 (포팅 + 수정)

**파일:** `src/widgets/order_editor.py`
**상태:** `order_editor.py` 기반.
**변경점:**
- `from src.cache import load_staff_order, save_staff_order` → `from PyQt6.QtCore import QSettings`
- `QSettings("분당구", "급식비 정산 프로그램")`으로 저장 (meal-macro3 스타일)
- 키: `"report/staff_order_조식"`, `"report/staff_order_중식"`, `"report/staff_order_석식"`
- `_load_cache`와 `_on_save`에서 `QSettings` 읽기/쓰기

### Step 8: `generators/report_generator.py` — 엑셀 출력 (포팅 + 단순화)

**파일:** `src/generators/report_generator.py`
**변경점:**
- `exporter.py`에서 pandas/numpy 의존 제거
- openpyxl만 사용 (이미 의존성 있음)
- `통합 문서1.xlsx` 템플릿 → `assets/report_template.xlsx`로 위치 변경
- 템플릿 경로: `get_settings().get("templates", "report_path")` 등록

### Step 9: `widgets/report_tab.py` — 리포트 탭 UI (신규)

**파일:** `src/widgets/report_tab.py`
**스타일:** `invoice_tab.py` 템플릿 따름
**구성:**
- 설정 그룹: 날짜 범위 (QDateEdit×2, MealCalendarWidget 적용), 중식 체크박스
- db.xlsx 자동 로드 (MainWindow에서 주입받거나 ReportTab 내부에서 DbReader 생성)
- 버튼: 리포트 생성 / 배송순서 편집 / 엑셀 열기 / 바로 인쇄
- 결과: QTabWidget (날짜별 탭) + QTableWidget (담당자 통계)
- **DeliveryService(data_source)** 생성해서 build_all 호출

### Step 10: `main_window.py` — 3번째 탭 추가 (수정)

**파일:** `src/main_window.py`
**변경:**
```python
from src.widgets.report_tab import ReportTab

# _setup_tabs()에 추가
self.report_tab = ReportTab(meal_type=meal_type)
self.tab_widget.addTab(self.report_tab, "③ 배송리포트")
```

### Step 11: `core/settings.py` — 리포트 설정 추가 (수정)

**파일:** `src/core/settings.py`
**변경:** `DEFAULTS`에 리포트 관련 키 추가
```python
"templates": {
    # ... 기존 유지 +
    "report_path": "assets/report_template.xlsx",
}
```

### Step 12: Asset 복사

- `통합 문서1.xlsx` → `assets/report_template.xlsx` (이름 변경)

---

## 의존성 변화

| 패키지 | 현재 | 통합 후 | 비고 |
|--------|------|---------|------|
| PyQt6 | ✅ | ✅ | |
| openpyxl | ✅ | ✅ | |
| pytest | ✅ | ✅ | |
| pandas | ❌ | ❌ | 안 씀 |
| numpy | ❌ | ❌ | 안 씀 |
| calamine | ❌ | ❌ | DbReader가 openpyxl 쓰므로 불필요 |
| msoffcrypto-tool | ❌ | ❌ | db.xlsx 암호화 안 했다고 가정 |
| holidays | ❌ | ❌ | DbReader가 db.xlsx에서 휴무일 읽음 |

**새 의존성 없음.** 기존 `pip install PyQt6 openpyxl pytest` 유지.

---

## 재사용 맵 (기존 코드 vs 새 코드)

| meal-macro3 기존 파일 | 통합 시 역할 |
|-----------------------|-------------|
| `excel/db_reader.py` (`DbReader`) | **데이터 제공자**. `read_delivery_matrix()`, `read_delivery_contacts()`, `get_holiday_set()`이 리포트에 필요한 모든 데이터 제공 |
| `excel/data_source.py` (`DataSource`) | **추상화**. `InvoiceService`(수정 후)와 `DeliveryService`(신규) 모두 이 인터페이스만 의존 |
| `core/settings.py` | 리포트 설정 키 추가만 |
| `excel/excel_utils.py` | 템플릿 셀 복사/수식 조정 재사용 |
| `tests/conftest.py` (`InMemoryDataSource`) | `InvoiceService` + `DeliveryService` 단위 테스트에 활용 |
| `main_window.py` | 탭 1개만 추가 |

---

## 테스트 계획

| 테스트 파일 | 대상 | 방식 |
|-------------|------|------|
| `tests/test_memo.py` | `core/memo.py` | 단위 테스트 — 다양한 메모 문자열 파싱 |
| `tests/test_diff_format.py` | `core/diff_format.py` | 단위 테스트 — `fmt_cell`, `has_diff`, `strip_diff` |
| `tests/test_delivery_service.py` | `services/delivery_service.py` | `InMemoryDataSource`로 집계 로직 검증 |
| `tests/test_invoice_controller.py` (기존 확장) | `InvoiceService` | `InMemoryDataSource` 주입해서 단위 테스트 |

---

## 요약: 파일 단위 작업 목록

| # | 작업 | 파일 | 변경 유형 |
|---|------|------|----------|
| 1 | 수정 (선행) | `src/templates/config.py` | 중복 fallback 제거 |
| 2 | 수정 (선행) | `src/services/invoice_controller.py` | 생성자 주입 추가 |
| 3 | 포팅 | `src/core/memo.py` | 신규 |
| 4 | 포팅 | `src/core/diff_format.py` | 신규 |
| 5 | 신규 | `src/services/delivery_service.py` | 신규 (DataSource 주입) |
| 6 | 포팅+수정 | `src/widgets/meal_calendar.py` | 신규 (holidays 제거) |
| 7 | 포팅+수정 | `src/widgets/order_editor.py` | 신규 (QSettings) |
| 8 | 포팅+단순화 | `src/generators/report_generator.py` | 신규 |
| 9 | 신규 | `src/widgets/report_tab.py` | 신규 |
| 10 | 수정 | `src/main_window.py` | 기존 수정 |
| 11 | 수정 | `src/core/settings.py` | 기존 수정 |
| 12 | 복사 | `assets/report_template.xlsx` | 신규 asset |
| 13 | 신규 | `tests/test_memo.py` | 신규 |
| 14 | 신규 | `tests/test_diff_format.py` | 신규 |
| 15 | 신규 | `tests/test_delivery_service.py` | 신규 |
