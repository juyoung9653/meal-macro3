"""명단의 배송 담당/구역 컬럼으로 날짜별 일일배송 엑셀을 생성한다."""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable
from copy import copy
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache

from openpyxl import load_workbook
from openpyxl.styles import Side

from ..core.meal_types import MealType
from ..services.delivery_calculator import get_week_ranges, to_bool
from ..services.holiday_fetcher import get_kr_holidays
from ..services.note_parser import expand_note_dates


@dataclass(frozen=True)
class DeliveryRoute:
    """출력 순서가 있는 배송기사/지역 정의."""

    key: str
    driver: str
    region: str


UNMATCHED_ROUTE = DeliveryRoute("unmatched", "미분류", "미분류")


@dataclass
class DailyDeliveryRow:
    route: DeliveryRoute
    breakfast: int = 0
    dinner: int = 0
    dinner_packages: str = ""
    note: str = ""
    _dinner_household_keys: list[str] = field(default_factory=list, repr=False)


@dataclass
class DailyDeliverySheet:
    delivery_date: date
    rows: list[DailyDeliveryRow]
    breakfast_total: int
    dinner_total: int
    unmatched_count: int = 0


def _compact(value: object) -> str:
    return "".join(_text(value).split())


def _text(value: object) -> str:
    """엑셀 공란과 NaN을 빈 문자열로 정규화한다."""

    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def prepare_daily_delivery_people(
    roster_data: Iterable[dict],
    breakfast_col: str = "",
    dinner_col: str = "",
    note_col: str = "",
    stop_col: str = "",
) -> list[dict]:
    """명단 원본 행에 일일배송 계산용 조식·석식·비고 값을 붙인다."""

    result: list[dict] = []
    for person in roster_data:
        stop_value = str(person.get(stop_col, "")) if stop_col else ""
        if "중지" in stop_value or "중단" in stop_value:
            continue
        daily_person = dict(person)
        daily_person["_breakfast"] = (
            person.get(breakfast_col, 0) if breakfast_col else 0
        )
        daily_person["_dinner"] = person.get(dinner_col, 0) if dinner_col else 0
        daily_person["_note"] = person.get(note_col, "") if note_col else ""
        result.append(daily_person)
    return result


def resolve_delivery_assignment(
    person: dict,
    driver_col: str,
    region_primary_col: str,
    region_secondary_col: str = "",
) -> tuple[str, str] | None:
    """행에서 ``배송담당, 1순위(없으면 2순위)`` 조합을 반환한다."""

    driver = _text(person.get(driver_col, ""))
    primary = _text(person.get(region_primary_col, ""))
    secondary = _text(person.get(region_secondary_col, ""))
    region = primary or secondary
    if not driver or not region:
        return None
    return driver, region


def build_delivery_routes(
    roster_data: Iterable[dict],
    driver_col: str,
    region_primary_col: str,
    region_secondary_col: str = "",
) -> tuple[tuple[DeliveryRoute, ...], dict[tuple[str, str], str]]:
    """명단 값으로 기사별 출력 경로를 만들고 첫 등장 순서를 유지한다."""

    regions_by_driver: dict[str, dict[str, None]] = {}
    for person in roster_data:
        assignment = resolve_delivery_assignment(
            person, driver_col, region_primary_col, region_secondary_col
        )
        if assignment is None:
            continue
        driver, region = assignment
        regions_by_driver.setdefault(driver, {}).setdefault(region, None)

    routes: list[DeliveryRoute] = []
    key_by_assignment: dict[tuple[str, str], str] = {}
    for driver, regions in regions_by_driver.items():
        for region in regions:
            key = f"route_{len(routes) + 1}"
            routes.append(DeliveryRoute(key, driver, region))
            key_by_assignment[(driver, region)] = key

    if not routes:
        raise ValueError(
            "선택한 컬럼에서 배송담당과 담당 구역을 찾을 수 없습니다. "
            "컬럼 선택과 명단 값을 확인하세요."
        )
    return tuple(routes), key_by_assignment


@lru_cache(maxsize=8192)
def _expanded_note_pairs(
    note: str, year: int, month: int
) -> tuple[tuple[date, str], ...]:
    pairs: list[tuple[date, str]] = []
    for target, meals in expand_note_dates(note, year, month):
        for meal in meals:
            pairs.append((target, str(meal)))
    return tuple(pairs)


def suspended_meals_on(note: object, target: date) -> set[str]:
    """비고상 ``target`` 날짜에 배송하지 않는 식사 종류를 반환한다."""

    text = str(note or "").strip()
    if not text:
        return set()
    return {
        meal
        for note_date, meal in _expanded_note_pairs(text, target.year, target.month)
        if note_date == target
    }


_MEAL_MEMO_ENTRY_RE = re.compile(
    r"(?P<meal>조식|석식)\s*[:：]\s*"
    r"(?P<dates>(?:\d{2,4}/)?\d{1,2}/\d{1,2}"
    r"(?:\s*[~～]\s*(?:(?:\d{2,4}/)?\d{1,2}/\d{1,2}|\d{1,2}))?)"
)


def _confirmation_week(target: date) -> tuple[date, date]:
    """확인서 계산과 같은 일~토 주차에서 ``target``이 속한 범위를 반환한다."""

    for _week, start, end in get_week_ranges(target.year, target.month):
        if start <= target <= end:
            return start, end
    return target, target


def weekly_confirmation_memo_entries(note: object, target: date) -> list[str]:
    """확인서식 메모 중 ``target`` 주차와 날짜가 겹치는 항목만 반환한다.

    지원 형식은 ``조식: 6/13~6/15``와 ``석식: 6/13``이며, 한 메모에
    여러 항목이 있어도 각 식종 항목을 따로 판정한다.
    """

    text = _text(note)
    if not text:
        return []

    week_start, week_end = _confirmation_week(target)
    result: list[str] = []
    seen: set[str] = set()
    for match in _MEAL_MEMO_ENTRY_RE.finditer(text):
        meal_label = match.group("meal")
        meal_type = (
            MealType.BREAKFAST if meal_label == "조식" else MealType.DINNER
        )
        date_text = re.sub(r"\s+", "", match.group("dates")).replace("～", "~")
        parseable = f"{date_text} {meal_label} 일시중지"
        overlaps = any(
            week_start <= memo_date <= week_end and memo_meal == meal_type
            for memo_date, memo_meal in _expanded_note_pairs(
                parseable, target.year, target.month
            )
        )
        entry = f"{meal_label}: {date_text}"
        if overlaps and entry not in seen:
            seen.add(entry)
            result.append(entry)
    return result


def _household_key(person: dict, address_col: str, fallback_index: int) -> str:
    address = _compact(person.get(address_col, "")) if address_col else ""
    return address or f"__person_{fallback_index}"


def _format_distribution(keys: Iterable[str]) -> str:
    household_sizes = Counter(keys)
    size_counts = Counter(household_sizes.values())
    return "/".join(
        f"{size}*{size_counts[size]}" for size in sorted(size_counts, reverse=True)
    )


def format_household_distribution(people: list[dict], address_col: str) -> str:
    """동일 주소를 한 세대로 보고 ``3*2/2*1/1*4`` 형식으로 표시한다."""

    keys = [_household_key(person, address_col, i) for i, person in enumerate(people)]
    return _format_distribution(keys)


def _note_line(
    address: object,
    note: object,
    meals: set[str],
    household_size: int = 1,
) -> str:
    address_text = " ".join(str(address or "").split())
    if len(address_text) > 28:
        address_text = address_text[:27] + "…"
    meal_text = "/".join(
        label
        for key, label in (
            (MealType.BREAKFAST, "조식"),
            (MealType.DINNER, "석식"),
        )
        if key in meals
    )
    note_text = " ".join(str(note or "").split())
    prefix = (
        f"{address_text}({household_size}명)"
        if address_text
        else f"주소 공란({household_size}명)"
    )
    if not meals:
        return f"{prefix} {note_text}".strip()
    return f"{prefix} [{meal_text} 제외] {note_text}".strip()


def build_daily_delivery_sheet(
    roster_data: list[dict],
    target: date,
    driver_col: str,
    region_primary_col: str,
    address_col: str,
    region_secondary_col: str = "",
    note_col: str = "_note",
) -> DailyDeliverySheet:
    """한 날짜의 기사/지역별 조식·석식 배송 수량을 계산한다."""

    routes, key_by_assignment = build_delivery_routes(
        roster_data, driver_col, region_primary_col, region_secondary_col
    )
    route_map = {route.key: route for route in routes}
    people_by_route: dict[str, list[tuple[int, dict, bool, bool, set[str]]]] = {
        route.key: [] for route in routes
    }
    unmatched: list[tuple[int, dict, bool, bool, set[str]]] = []

    for index, person in enumerate(roster_data):
        breakfast_flag = to_bool(person.get("_breakfast", 0))
        dinner_flag = to_bool(person.get("_dinner", 0))
        if not breakfast_flag and not dinner_flag:
            continue

        inactive = suspended_meals_on(person.get(note_col, ""), target)
        breakfast = breakfast_flag and MealType.BREAKFAST not in inactive
        dinner = dinner_flag and MealType.DINNER not in inactive

        assignment = resolve_delivery_assignment(
            person,
            driver_col,
            region_primary_col,
            region_secondary_col,
        )
        route_key = key_by_assignment.get(assignment) if assignment else None
        item = (index, person, breakfast, dinner, inactive)
        if route_key in route_map:
            people_by_route[route_key].append(item)
        else:
            unmatched.append(item)

    rows: list[DailyDeliveryRow] = []
    for route in routes:
        entries = people_by_route[route.key]
        household_sizes = Counter(
            _household_key(person, address_col, index)
            for index, person, _breakfast, _dinner, _inactive in entries
        )
        dinner_keys = [
            _household_key(person, address_col, index)
            for index, person, _breakfast, dinner, _inactive in entries
            if dinner
        ]
        note_lines: list[str] = []
        seen_notes: set[str] = set()
        for _index, person, _breakfast, _dinner, inactive in entries:
            household_key = _household_key(person, address_col, _index)
            address = person.get(address_col, "")
            weekly_entries = weekly_confirmation_memo_entries(
                person.get(note_col, ""), target
            )
            for memo_entry in weekly_entries:
                line = _note_line(
                    address,
                    memo_entry,
                    set(),
                    household_sizes[household_key],
                )
                if line and line not in seen_notes:
                    seen_notes.add(line)
                    note_lines.append(line)

            affected = inactive & {MealType.BREAKFAST, MealType.DINNER}
            if not affected:
                continue
            line = _note_line(
                address,
                person.get(note_col, ""),
                affected,
                household_sizes[household_key],
            )
            if line and line not in seen_notes:
                seen_notes.add(line)
                note_lines.append(line)

        row = DailyDeliveryRow(
            route=route,
            breakfast=sum(1 for _i, _p, bf, _dn, _x in entries if bf),
            dinner=sum(1 for _i, _p, _bf, dn, _x in entries if dn),
            dinner_packages=_format_distribution(dinner_keys),
            note="\n".join(note_lines),
            _dinner_household_keys=dinner_keys,
        )
        if row.breakfast or row.dinner:
            rows.append(row)

    active_unmatched = [entry for entry in unmatched if entry[2] or entry[3]]
    if active_unmatched:
        dinner_keys = [
            _household_key(person, address_col, index)
            for index, person, _breakfast, dinner, _inactive in active_unmatched
            if dinner
        ]
        missing_driver_count = sum(
            not _text(person.get(driver_col, ""))
            for _index, person, _bf, _dn, _inactive in active_unmatched
        )
        missing_region_count = sum(
            not (
                _text(person.get(region_primary_col, ""))
                or _text(person.get(region_secondary_col, ""))
            )
            for _index, person, _bf, _dn, _inactive in active_unmatched
        )
        reasons: list[str] = []
        if missing_driver_count:
            reasons.append(f"배송담당 공란 {missing_driver_count}명")
        if missing_region_count:
            reasons.append(f"담당 구역 공란 {missing_region_count}명")
        rows.append(
            DailyDeliveryRow(
                route=UNMATCHED_ROUTE,
                breakfast=sum(1 for _i, _p, bf, _dn, _x in active_unmatched if bf),
                dinner=sum(1 for _i, _p, _bf, dn, _x in active_unmatched if dn),
                dinner_packages=_format_distribution(dinner_keys),
                note="미분류: " + ", ".join(reasons),
                _dinner_household_keys=dinner_keys,
            )
        )

    return DailyDeliverySheet(
        delivery_date=target,
        rows=rows,
        breakfast_total=sum(row.breakfast for row in rows),
        dinner_total=sum(row.dinner for row in rows),
        unmatched_count=len(active_unmatched),
    )


def select_delivery_dates(
    start: date,
    end: date,
    skip_non_business_days: bool = True,
    excluded_dates: set[date] | None = None,
    holiday_provider: Callable[[int], set[date]] = get_kr_holidays,
) -> list[date]:
    """선택 범위를 날짜 목록으로 확장한다.

    기본값은 예시 문서처럼 토·일, 공휴일, 청구서 탭의 배송 제외일을
    건너뛴다. ``skip_non_business_days=False``이면 범위의 모든 날짜를 쓴다.
    """

    if start > end:
        raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")

    blocked = set(excluded_dates or set())
    if skip_non_business_days:
        for year in range(start.year, end.year + 1):
            blocked.update(holiday_provider(year))

    result: list[date] = []
    current = start
    while current <= end:
        if not skip_non_business_days or (
            current.weekday() < 5 and current not in blocked
        ):
            result.append(current)
        current += timedelta(days=1)
    return result


_BLACK = "000000"
_THIN = Side(style="thin", color=_BLACK)
_MEDIUM = Side(style="medium", color=_BLACK)


def _driver_ranges(rows: list[DailyDeliveryRow]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(rows) + 1):
        if index == len(rows) or rows[index].route.driver != rows[start].route.driver:
            ranges.append((start, index - 1))
            start = index
    return ranges


def _set_horizontal_border(cell, top: Side, bottom: Side) -> None:
    border = copy(cell.border)
    border.top = copy(top)
    border.bottom = copy(bottom)
    cell.border = border


def _fill_daily_sheet(ws, sheet: DailyDeliverySheet):
    """템플릿의 서식을 복제하면서 필요한 본문 행 수에 맞춰 값을 채운다."""

    rows = sheet.rows
    data_start = 4
    data_end = data_start + len(rows) - 1
    total_row = data_end + 1

    if ws.max_row < 5:
        raise ValueError("일일배송 템플릿에는 헤더, 본문, 합계 서식이 필요합니다.")

    data_styles = [copy(ws.cell(data_start, column)._style) for column in range(1, 11)]
    total_styles = [copy(ws.cell(5, column)._style) for column in range(1, 11)]
    data_height = ws.row_dimensions[data_start].height
    total_height = ws.row_dimensions[5].height
    district_label = ws.cell(data_start, 1).value
    total_label = ws.cell(5, 1).value

    for merged in list(ws.merged_cells.ranges):
        if merged.min_row >= data_start:
            ws.unmerge_cells(str(merged))
    if ws.max_row >= 5:
        ws.delete_rows(5, ws.max_row - 4)
    for row_index in list(ws.row_dimensions):
        if row_index >= 5:
            del ws.row_dimensions[row_index]

    for row_no in range(data_start, data_end + 1):
        ws.row_dimensions[row_no].height = data_height
        for column, style in enumerate(data_styles, start=1):
            cell = ws.cell(row_no, column)
            cell._style = copy(style)
            cell.value = None
            _set_horizontal_border(cell, _THIN, _THIN)

    ws.row_dimensions[total_row].height = total_height
    for column, style in enumerate(total_styles, start=1):
        cell = ws.cell(total_row, column)
        cell._style = copy(style)
        cell.value = None

    if rows:
        ws.cell(data_start, 1, district_label)
    ws.cell(total_row, 1, total_label)
    ws["A1"] = sheet.delivery_date

    for row_no, row_data in enumerate(rows, start=data_start):
        ws.cell(row_no, 3, row_data.route.region)
        ws.cell(row_no, 4, row_data.breakfast)
        ws.cell(row_no, 5, row_data.dinner)
        ws.cell(row_no, 7, row_data.dinner_packages)
        ws.cell(row_no, 9, row_data.note)

    # 구청 및 기사별 병합/합계

    for start_idx, end_idx in _driver_ranges(rows):
        first_row = data_start + start_idx
        last_row = data_start + end_idx
        driver_rows = rows[start_idx : end_idx + 1]
        ws.cell(first_row, 2, driver_rows[0].route.driver)
        ws.cell(first_row, 6, f"=SUM(D{first_row}:D{last_row})")
        ws.cell(first_row, 8, f"=SUM(E{first_row}:E{last_row})")
        driver_households = [
            key for row in driver_rows for key in row._dinner_household_keys
        ]
        ws.cell(
            first_row, 10, _format_distribution(driver_households).replace("/", "\n")
        )

        for row_no in range(first_row, last_row + 1):
            for col in range(1, 11):
                cell = ws.cell(row_no, col)
                top = _MEDIUM if row_no == first_row else _THIN
                bottom = _MEDIUM if row_no == last_row else _THIN
                _set_horizontal_border(cell, top, bottom)

        if first_row != last_row:
            for col in (2, 6, 8, 10):
                ws.merge_cells(
                    start_row=first_row,
                    start_column=col,
                    end_row=last_row,
                    end_column=col,
                )

    if data_end >= data_start:
        ws.merge_cells(
            start_row=data_start, start_column=1, end_row=data_end, end_column=1
        )

    # 합계 행
    if rows:
        ws.cell(total_row, 4, f"=SUM(D{data_start}:D{data_end})")
        ws.cell(total_row, 5, f"=SUM(E{data_start}:E{data_end})")
        ws.cell(total_row, 6, f"=SUM(F{data_start}:F{data_end})")
        ws.cell(total_row, 8, f"=SUM(H{data_start}:H{data_end})")
    else:
        for column in (4, 5, 6, 8):
            ws.cell(total_row, column, 0)
    ws.merge_cells(start_row=total_row, start_column=1, end_row=total_row, end_column=3)
    ws.print_area = f"A1:I{total_row}"
    ws.auto_filter.ref = None


def make_daily_delivery(
    roster_data: list[dict],
    dates: Iterable[date],
    output_path: str,
    template_path: str,
    driver_col: str,
    region_primary_col: str,
    address_col: str,
    region_secondary_col: str = "",
    note_col: str = "_note",
) -> list[DailyDeliverySheet]:
    """날짜마다 시트를 만든 일일배송 통합문서를 저장한다."""

    unique_dates = sorted(set(dates), reverse=True)
    if not roster_data:
        raise ValueError("청구서 데이터가 없습니다.")
    if not unique_dates:
        raise ValueError("생성할 배송일이 없습니다.")
    if not driver_col:
        raise ValueError("배송 담당 컬럼을 선택하세요.")
    if not region_primary_col:
        raise ValueError("담당 구역 1순위 컬럼을 선택하세요.")
    if not address_col:
        raise ValueError("세대 구분(주소) 컬럼을 선택하세요.")
    if not any(
        to_bool(person.get("_breakfast", 0))
        or to_bool(person.get("_dinner", 0))
        for person in roster_data
    ):
        raise ValueError(
            "선택한 조식·석식 컬럼에서 대상자를 찾을 수 없습니다. "
            "식종 컬럼과 셀 값을 확인하세요."
        )
    if not os.path.isfile(template_path):
        raise ValueError(f"일일배송 템플릿을 찾을 수 없습니다:\n{template_path}")

    # 컬럼 선택 오류는 파일을 만들기 전에 한 번 검증한다.
    build_delivery_routes(
        roster_data, driver_col, region_primary_col, region_secondary_col
    )

    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir and not os.path.isdir(output_dir):
        raise ValueError(f"출력 폴더를 찾을 수 없습니다:\n{output_dir}")

    try:
        wb = load_workbook(template_path)
    except (OSError, ValueError) as error:
        raise ValueError(
            f"일일배송 템플릿을 읽을 수 없습니다:\n{template_path}"
        ) from error
    if not wb.worksheets:
        raise ValueError("일일배송 템플릿에 시트가 없습니다.")

    template_ws = wb.worksheets[0]
    for extra_ws in list(wb.worksheets[1:]):
        wb.remove(extra_ws)
    results: list[DailyDeliverySheet] = []
    for target in unique_dates:
        sheet = build_daily_delivery_sheet(
            roster_data,
            target,
            driver_col=driver_col,
            region_primary_col=region_primary_col,
            region_secondary_col=region_secondary_col,
            address_col=address_col,
            note_col=note_col,
        )
        ws = wb.copy_worksheet(template_ws)
        ws.title = target.strftime("%y.%m.%d")
        ws.freeze_panes = template_ws.freeze_panes
        ws.sheet_view.showGridLines = template_ws.sheet_view.showGridLines
        _fill_daily_sheet(ws, sheet)
        results.append(sheet)

    wb.remove(template_ws)
    wb.save(output_path)
    return results
