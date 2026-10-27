"""Immutable selections and source rows for a substitute driver's delivery sheet."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from uuid import UUID

from .roster import Meal
from .revisions import JsonValue, freeze_json_object


@dataclass(frozen=True, slots=True)
class SubstituteColumn:
    key: str
    label: str


STANDARD_COLUMNS = (
    SubstituteColumn("sequence", "배송순서"),
    SubstituteColumn("jurisdiction", "관할동"),
    SubstituteColumn("name", "성명"),
    SubstituteColumn("detail_address", "상세주소"),
    SubstituteColumn("road_address", "도로명주소"),
    SubstituteColumn("mobile_phone", "핸드폰"),
    SubstituteColumn("note", "비고"),
    SubstituteColumn("telephone", "전화번호"),
    SubstituteColumn("school_name", "학교명"),
    SubstituteColumn("grade", "학년"),
    SubstituteColumn("birth_date", "생년월일"),
    SubstituteColumn("form", "배송 형태"),
)


@dataclass(frozen=True, slots=True)
class SubstituteOptions:
    driver_key: str
    columns: tuple[str, ...]
    orientation: str = "portrait"
    split: str = "groups"
    groups_per_page: int = 18
    fallbacks: Mapping[str, str] | None = None
    column_labels: Mapping[str, str] = field(default_factory=dict)
    horizontal_alignments: Mapping[str, str] = field(default_factory=dict)
    vertical_alignments: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.driver_key, str) or not self.driver_key:
            raise ValueError("배송담당을 선택해주세요.")
        columns = tuple(self.columns)
        if not columns or any(not isinstance(key, str) or not key for key in columns):
            raise ValueError("출력할 칼럼을 하나 이상 선택해주세요.")
        if len(set(columns)) != len(columns):
            raise ValueError("출력 칼럼을 중복 선택할 수 없습니다.")
        if self.orientation not in ("auto", "portrait", "landscape"):
            raise ValueError("용지 방향이 올바르지 않습니다.")
        if self.split not in ("none", "dong", "groups"):
            raise ValueError("페이지 구분 방식이 올바르지 않습니다.")
        if type(self.groups_per_page) is not int or not 1 <= self.groups_per_page <= 100:
            raise ValueError("페이지당 가구 수는 1~100이어야 합니다.")
        for name, allowed in (("horizontal_alignments", ("left", "center", "right")),
                              ("vertical_alignments", ("top", "center", "bottom"))):
            values = getattr(self, name)
            if not isinstance(values, Mapping) or any(
                not isinstance(key, str) or value not in allowed for key, value in values.items()
            ):
                raise ValueError("배송대타 칼럼 정렬 설정이 올바르지 않습니다.")
            object.__setattr__(self, name, MappingProxyType(dict(values)))
        object.__setattr__(self, "columns", columns)
        for mapping in (self.fallbacks, self.column_labels):
            if mapping is not None and (
                not isinstance(mapping, Mapping)
                or any(not isinstance(key, str) or not isinstance(value, str) for key, value in mapping.items())
            ):
                raise ValueError("배송대타 칼럼 설정이 올바르지 않습니다.")
        if self.fallbacks is not None:
            if any(key == value for key, value in self.fallbacks.items()):
                raise ValueError("대체 칼럼은 다른 칼럼을 선택해주세요.")
            object.__setattr__(self, "fallbacks", MappingProxyType(dict(self.fallbacks)))
        object.__setattr__(self, "column_labels", MappingProxyType(dict(self.column_labels)))

    def manifest(self):
        return {"driver_key": self.driver_key, "columns": self.columns,
                "orientation": self.orientation, "split": self.split,
                "groups_per_page": self.groups_per_page,
                "horizontal_alignments": dict(self.horizontal_alignments),
                "vertical_alignments": dict(self.vertical_alignments),
                **({"fallbacks": dict(self.fallbacks), "column_labels": dict(self.column_labels)}
                   if self.fallbacks is not None else {})}


@dataclass(frozen=True, slots=True)
class SubstituteRow:
    person_id: UUID
    household_id: UUID
    driver_key: str
    driver_label: str
    values: Mapping[str, str]
    entitlement_count: int
    box: bool = False
    grouping_phone: str | None = None
    grouping_jurisdiction: str | None = None

    def __post_init__(self):
        if self.entitlement_count < 1:
            raise ValueError("배송 식수는 1 이상이어야 합니다.")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True, slots=True)
class PreparedSubstituteData:
    day: date
    meal: Meal
    rows: tuple[SubstituteRow, ...]
    columns: tuple[SubstituteColumn, ...]
    default_columns: tuple[str, ...]
    calculation_manifest: Mapping[str, JsonValue]

    def __post_init__(self):
        object.__setattr__(self, "rows", tuple(self.rows))
        object.__setattr__(self, "columns", tuple(self.columns))
        object.__setattr__(self, "default_columns", tuple(self.default_columns))
        object.__setattr__(self, "calculation_manifest", freeze_json_object(self.calculation_manifest))

    def selected_rows(self, options: SubstituteOptions) -> tuple[SubstituteRow, ...]:
        known = {column.key for column in self.columns}
        if not set(options.columns) <= known:
            raise ValueError("선택한 원본에 없는 출력 칼럼입니다. 칼럼을 다시 선택해주세요.")
        rows = tuple(row for row in self.rows if row.driver_key == options.driver_key)
        if not rows:
            raise ValueError("선택한 날짜·식종·배송담당에 실제 배송할 대상이 없습니다.")
        return rows
