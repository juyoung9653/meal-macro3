"""Build immutable driver/meal worksheet snapshots from household projections."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from src.domain.delivery_routing import MEAL_ORDER
from src.domain.roster import Meal
from src.services.driver_households import DriverHouseholdRow, lunch_period_texts
from src.domain.lunch_periods import LUNCH_DATE_COLUMNS

from .current_sheet_export import CurrentSheetSnapshot, TextWorkbookSnapshot


TEMPLATE_HEADERS = ("배송기사", "가구원", "식종", "배송순서", "주소", "연락처")
TEMPLATE_NAME = "기사·식종별 가구"


def build_driver_households_template_snapshot(
    rows: Iterable[DriverHouseholdRow],
    *,
    selected_driver_id: UUID | None = None,
    lunch_date_columns: tuple[str, ...] = (),
) -> TextWorkbookSnapshot:
    """Freeze exact meal projections into driver-first, meal-ordered worksheets."""

    headers = (*TEMPLATE_HEADERS, *(LUNCH_DATE_COLUMNS[key] for key in lunch_date_columns))
    date_indices = tuple(tuple(LUNCH_DATE_COLUMNS).index(key) for key in lunch_date_columns)
    groups: dict[
        tuple[tuple[str, UUID | str | None], Meal],
        list[DriverHouseholdRow],
    ] = {}
    driver_labels: dict[tuple[str, UUID | str | None], str] = {}
    driver_order: dict[tuple[str, UUID | str | None], int] = {}

    for row in tuple(rows):
        if not isinstance(row, DriverHouseholdRow):
            raise TypeError("기사별 가구 내보내기 행 형식이 올바르지 않습니다.")
        if selected_driver_id is not None and row.driver_id != selected_driver_id:
            continue
        for meal_row in _meal_projections(row):
            identity = _driver_identity(meal_row)
            if identity not in driver_order:
                driver_order[identity] = len(driver_order)
                driver_labels[identity] = meal_row.driver_name or "미지정"
            groups.setdefault((identity, meal_row.meals[0]), []).append(meal_row)

    used_titles: set[str] = set()
    sheets: list[CurrentSheetSnapshot] = []
    ordered_groups = sorted(
        groups,
        key=lambda key: (driver_order[key[0]], MEAL_ORDER.index(key[1])),
    )
    for identity, meal in ordered_groups:
        title = _unique_sheet_title(driver_labels[identity], meal, used_titles)
        projections = sorted(groups[(identity, meal)], key=_row_sort_key)
        sheets.append(
            CurrentSheetSnapshot(
                title,
                headers,
                tuple((*_display_values(row), *(lunch_period_texts(row)[i] for i in date_indices))
                      for row in projections),
            )
        )

    if not sheets:
        sheets.append(CurrentSheetSnapshot(TEMPLATE_NAME, headers, ()))
    return TextWorkbookSnapshot(TEMPLATE_NAME, tuple(sheets))


def _meal_projections(row: DriverHouseholdRow) -> tuple[DriverHouseholdRow, ...]:
    if row.meal_rows:
        projections = row.meal_rows
    elif len(row.meals) == 1:
        projections = (row,)
    elif len(row.meals) > 1:
        raise ValueError(
            "여러 식종이 합쳐진 가구 행에 식종별 가구원 정보가 없어 내보낼 수 없습니다."
        )
    else:
        raise ValueError("식종이 없는 가구 행은 내보낼 수 없습니다.")
    if any(
        len(projection.meals) != 1 or projection.meals[0] not in MEAL_ORDER
        for projection in projections
    ):
        raise ValueError("식종별 가구 행 형식이 올바르지 않아 내보낼 수 없습니다.")
    return projections


def _driver_identity(row: DriverHouseholdRow) -> tuple[str, UUID | str | None]:
    if row.driver_id is not None:
        return ("registered", row.driver_id)
    if row.driver_name:
        return ("legacy", row.driver_name)
    return ("unassigned", None)


def _row_sort_key(row: DriverHouseholdRow):
    sequence = min(
        (_sequence_sort_key(value) for value in row.sequences if value.strip()),
        default=(2, 0, ""),
    )
    return (
        sequence,
        " ".join(row.member_names).casefold(),
        row.address.casefold(),
        str(row.household_key),
    )


def _sequence_sort_key(value: str) -> tuple[int, int | str, str]:
    text = value.strip()
    if text.isdigit():
        return (0, int(text), text)
    return (1, text.casefold(), text)


def _display_values(row: DriverHouseholdRow) -> tuple[str, ...]:
    return (
        row.driver_name or "미지정",
        ", ".join(row.member_names),
        row.meals[0].value,
        ", ".join(row.sequences),
        row.address,
        ", ".join(row.contacts),
    )


def _unique_sheet_title(
    driver_name: str,
    meal: Meal,
    used_titles: set[str],
) -> str:
    cleaned_driver = "".join(
        "_" if character in "[]:*?/\\" else character for character in driver_name
    ).strip().strip("'") or "미지정"
    suffix = f"-{meal.value}"
    collision = 1
    while True:
        marker = "" if collision == 1 else f" ({collision})"
        available = max(1, 31 - len(marker) - len(suffix))
        candidate = f"{cleaned_driver[:available]}{marker}{suffix}"
        folded = candidate.casefold()
        if folded not in used_titles:
            used_titles.add(folded)
            return candidate
        collision += 1
