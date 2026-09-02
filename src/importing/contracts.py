"""Input-profile and typed observation contracts, independent of Excel."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from uuid import UUID

from src.domain.roster import DeliveryAssignment, Meal, PersonProfile, normalize_phone


class ProfileField(StrEnum):
    NAME = "name"
    BIRTH_DATE = "birth_date"
    SCHOOL_TYPE = "school_type"
    SCHOOL_NAME = "school_name"
    GRADE = "grade"
    CLASS_NAME = "class_name"
    HOUSEHOLD_HEAD_NAME = "household_head_name"
    RELATIONSHIP_TO_CHILD = "relationship_to_child"
    JURISDICTION = "jurisdiction"
    PROVIDER = "provider"
    ROAD_ADDRESS = "road_address"
    DETAIL_ADDRESS = "detail_address"
    TELEPHONE = "telephone"
    MOBILE_PHONE = "mobile_phone"
    NOTE = "note"


_PROFILE_FIELDS = tuple(ProfileField)


class DateNormalization(StrEnum):
    AUTO = "auto"
    YYYYMMDD = "yyyymmdd"


class PhoneNormalization(StrEnum):
    KOREAN = "korean"
    DIGITS = "digits"
    PRESERVE = "preserve"


@dataclass(frozen=True, slots=True)
class ImportMappingProfile:
    """Versioned mapping from one external layout to canonical observations."""

    profile_id: UUID
    version: int
    name: str
    source_kind: str
    profile_columns: Mapping[ProfileField, str]
    combined_meals_column: str | None = None
    meal_flag_columns: Mapping[Meal, str] = field(default_factory=dict)
    driver_column: str | None = None
    sequence_column: str | None = None
    custom_field_columns: Mapping[UUID, str] = field(default_factory=dict)
    source_reference_column: str | None = None
    header_row: int = 1
    data_start_row: int = 2
    fallback_profile_columns: Mapping[ProfileField, tuple[str, ...]] = field(
        default_factory=dict
    )
    meal_true_values: tuple[str, ...] = ("1",)
    meal_false_values: tuple[str, ...] = ("", "0")
    date_normalization: DateNormalization = DateNormalization.AUTO
    phone_normalization: PhoneNormalization = PhoneNormalization.KOREAN
    ignored_columns: tuple[str, ...] = ()
    provider_value: str | None = None

    def __post_init__(self) -> None:
        _validate_uuid(self.profile_id, "import profile ID")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("import profile version은 정수여야 합니다.")
        if self.version < 1:
            raise ValueError("import profile version은 1 이상이어야 합니다.")
        object.__setattr__(self, "name", _required_text(self.name, "profile 이름"))
        object.__setattr__(
            self,
            "source_kind",
            _required_text(self.source_kind, "source 종류"),
        )

        profile_columns = {
            _profile_field(field): _required_text(header, "profile 원본 열")
            for field, header in self.profile_columns.items()
        }
        provider_value = _optional_text(self.provider_value, "직접 입력 제공기관")
        if (
            provider_value is not None
            and ProfileField.PROVIDER in profile_columns
        ):
            raise ValueError(
                "제공기관은 Excel 열 연결과 직접 입력을 동시에 사용할 수 없습니다."
            )
        for required in (ProfileField.NAME,):
            if required not in profile_columns:
                raise ValueError(f"import profile에 필수 열이 없습니다: {required.value}")
        combined = _optional_text(self.combined_meals_column, "합쳐진 식종 열")
        meal_flags = {
            _meal(meal): _required_text(header, "조식·중식·석식 여부 열")
            for meal, header in self.meal_flag_columns.items()
        }
        if (combined is None) == (not meal_flags):
            raise ValueError(
                "Excel 식종 표기 방식을 하나만 선택해주세요. ‘식종’ 열 하나를 "
                "고르거나, 조식·중식·석식 여부 열을 각각 고를 수 있습니다. "
                "두 방식을 함께 쓸 수는 없습니다."
            )
        custom_columns: dict[UUID, str] = {}
        for definition_id, header in self.custom_field_columns.items():
            _validate_uuid(definition_id, "사용자 정의 필드 ID")
            custom_columns[definition_id] = _required_text(
                header,
                "사용자 정의 원본 열",
            )
        driver = _optional_text(self.driver_column, "배송담당 열")
        sequence = _optional_text(self.sequence_column, "배송순서 열")
        source_reference = _optional_text(
            self.source_reference_column,
            "원본 참조 열",
        )
        header_row = _positive_int(self.header_row, "헤더 행")
        data_start_row = _positive_int(self.data_start_row, "데이터 시작 행")
        if data_start_row <= header_row:
            raise ValueError("데이터 시작 행은 헤더 행보다 뒤여야 합니다.")
        fallback_columns: dict[ProfileField, tuple[str, ...]] = {}
        for raw_field, raw_columns in self.fallback_profile_columns.items():
            profile_field = _profile_field(raw_field)
            if profile_field not in profile_columns:
                raise ValueError(
                    "후보 열을 지정한 profile field에는 우선 열도 필요합니다: "
                    f"{profile_field.value}"
                )
            columns = tuple(
                _required_text(column, "profile 후보 원본 열")
                for column in raw_columns
            )
            if not columns:
                raise ValueError("profile 후보 원본 열은 하나 이상이어야 합니다.")
            if len({column.casefold() for column in columns}) != len(columns):
                raise ValueError("같은 profile 후보 원본 열을 중복 지정할 수 없습니다.")
            fallback_columns[profile_field] = columns
        true_values = _flag_values(self.meal_true_values, "급식을 받는 값")
        false_values = _flag_values(self.meal_false_values, "급식을 받지 않는 값")
        if set(map(_flag_key, true_values)) & set(map(_flag_key, false_values)):
            raise ValueError("급식을 받는 값과 받지 않는 값은 겹칠 수 없습니다.")
        if not isinstance(self.date_normalization, DateNormalization):
            raise TypeError("날짜 정규화 방식은 DateNormalization이어야 합니다.")
        if not isinstance(self.phone_normalization, PhoneNormalization):
            raise TypeError("전화 정규화 방식은 PhoneNormalization이어야 합니다.")
        ignored_columns = tuple(
            _required_text(column, "무시할 원본 열")
            for column in self.ignored_columns
        )
        if len({column.casefold() for column in ignored_columns}) != len(
            ignored_columns
        ):
            raise ValueError("무시할 원본 열을 중복 지정할 수 없습니다.")
        headers = (
            *profile_columns.values(),
            *(
                column
                for columns in fallback_columns.values()
                for column in columns
            ),
            *((combined,) if combined is not None else ()),
            *meal_flags.values(),
            *((driver,) if driver is not None else ()),
            *((sequence,) if sequence is not None else ()),
            *custom_columns.values(),
            *((source_reference,) if source_reference is not None else ()),
        )
        normalized_headers = tuple(header.casefold() for header in headers)
        if len(set(normalized_headers)) != len(normalized_headers):
            raise ValueError("하나의 원본 열을 둘 이상의 import 역할에 연결할 수 없습니다.")
        used_headers = set(normalized_headers)
        if any(column.casefold() in used_headers for column in ignored_columns):
            raise ValueError("사용하는 원본 열을 동시에 무시할 수 없습니다.")

        object.__setattr__(
            self,
            "profile_columns",
            MappingProxyType(profile_columns),
        )
        object.__setattr__(self, "combined_meals_column", combined)
        object.__setattr__(
            self,
            "meal_flag_columns",
            MappingProxyType(meal_flags),
        )
        object.__setattr__(self, "driver_column", driver)
        object.__setattr__(self, "sequence_column", sequence)
        object.__setattr__(self, "source_reference_column", source_reference)
        object.__setattr__(
            self,
            "custom_field_columns",
            MappingProxyType(custom_columns),
        )
        object.__setattr__(self, "header_row", header_row)
        object.__setattr__(self, "data_start_row", data_start_row)
        object.__setattr__(
            self,
            "fallback_profile_columns",
            MappingProxyType(fallback_columns),
        )
        object.__setattr__(self, "meal_true_values", true_values)
        object.__setattr__(self, "meal_false_values", false_values)
        object.__setattr__(self, "ignored_columns", ignored_columns)
        object.__setattr__(self, "provider_value", provider_value)

    def manifest(self) -> dict[str, object]:
        return {
            "profile_id": str(self.profile_id),
            "version": self.version,
            "name": self.name,
            "source_kind": self.source_kind,
            "profile_columns": {
                field.value: header
                for field, header in sorted(
                    self.profile_columns.items(),
                    key=lambda item: item[0].value,
                )
            },
            "combined_meals_column": self.combined_meals_column,
            "meal_flag_columns": {
                meal.value: header
                for meal, header in sorted(
                    self.meal_flag_columns.items(),
                    key=lambda item: item[0].value,
                )
            },
            "driver_column": self.driver_column,
            "sequence_column": self.sequence_column,
            "source_reference_column": self.source_reference_column,
            "custom_field_columns": {
                str(definition_id): header
                for definition_id, header in sorted(
                    self.custom_field_columns.items(),
                    key=lambda item: str(item[0]),
                )
            },
            "header_row": self.header_row,
            "data_start_row": self.data_start_row,
            "fallback_profile_columns": {
                field.value: list(columns)
                for field, columns in sorted(
                    self.fallback_profile_columns.items(),
                    key=lambda item: item[0].value,
                )
            },
            "meal_true_values": list(self.meal_true_values),
            "meal_false_values": list(self.meal_false_values),
            "date_normalization": self.date_normalization.value,
            "phone_normalization": self.phone_normalization.value,
            "ignored_columns": list(self.ignored_columns),
            "provider_value": self.provider_value,
        }

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, object]) -> ImportMappingProfile:
        """Rehydrate a profile manifest stored inside the encrypted ledger."""

        if not isinstance(manifest, Mapping):
            raise TypeError("import profile manifest는 mapping이어야 합니다.")
        profile_columns = _manifest_mapping(manifest, "profile_columns")
        meal_columns = _manifest_mapping(manifest, "meal_flag_columns")
        custom_columns = _manifest_mapping(manifest, "custom_field_columns")
        fallback_columns = _manifest_mapping(
            manifest,
            "fallback_profile_columns",
            default={},
        )
        return cls(
            profile_id=UUID(str(manifest["profile_id"])),
            version=manifest["version"],
            name=manifest["name"],
            source_kind=manifest["source_kind"],
            profile_columns={
                ProfileField(str(key)): value
                for key, value in profile_columns.items()
            },
            combined_meals_column=manifest.get("combined_meals_column"),
            meal_flag_columns={
                Meal(str(key)): value for key, value in meal_columns.items()
            },
            driver_column=manifest.get("driver_column"),
            sequence_column=manifest.get("sequence_column"),
            source_reference_column=manifest.get("source_reference_column"),
            custom_field_columns={
                UUID(str(key)): value for key, value in custom_columns.items()
            },
            header_row=manifest.get("header_row", 1),
            data_start_row=manifest.get("data_start_row", 2),
            fallback_profile_columns={
                ProfileField(str(key)): _manifest_sequence(value, str(key))
                for key, value in fallback_columns.items()
            },
            meal_true_values=_manifest_sequence(
                manifest.get("meal_true_values", ("1",)),
                "meal_true_values",
            ),
            meal_false_values=_manifest_sequence(
                manifest.get("meal_false_values", ("", "0")),
                "meal_false_values",
            ),
            date_normalization=DateNormalization(
                str(manifest.get("date_normalization", DateNormalization.AUTO.value))
            ),
            phone_normalization=PhoneNormalization(
                str(manifest.get("phone_normalization", PhoneNormalization.KOREAN.value))
            ),
            ignored_columns=_manifest_sequence(
                manifest.get("ignored_columns", ()),
                "ignored_columns",
            ),
            provider_value=manifest.get("provider_value"),
        )


@dataclass(frozen=True, slots=True)
class SourceReference:
    source_filename: str
    sheet_name: str
    row_numbers: tuple[int, ...]
    external_reference: str | None = None

    def __post_init__(self) -> None:
        filename = _required_literal_text(self.source_filename, "원본 파일명")
        if (
            PureWindowsPath(filename).name != filename
            or PurePosixPath(filename).name != filename
        ):
            raise ValueError("typed observation에는 원본 파일의 basename만 저장합니다.")
        object.__setattr__(self, "source_filename", filename)
        object.__setattr__(
            self,
            "sheet_name",
            _required_literal_text(self.sheet_name, "원본 시트명"),
        )
        rows = tuple(self.row_numbers)
        if not rows or any(
            isinstance(row, bool) or not isinstance(row, int) or row < 1
            for row in rows
        ):
            raise ValueError("원본 행은 1 이상의 정수여야 합니다.")
        if tuple(sorted(set(rows))) != rows:
            raise ValueError("원본 행은 중복 없이 오름차순이어야 합니다.")
        object.__setattr__(self, "row_numbers", rows)
        object.__setattr__(
            self,
            "external_reference",
            _optional_literal_text(self.external_reference, "원본 참조값"),
        )


@dataclass(frozen=True, slots=True)
class ObservedDeliveryAssignment:
    meal: Meal | None = None
    driver: str | None = None
    sequence: str | None = None

    def __post_init__(self) -> None:
        if self.meal is not None and not isinstance(self.meal, Meal):
            raise TypeError("meal은 Meal 또는 None이어야 합니다.")
        driver = _optional_text(self.driver, "배송담당")
        sequence = _optional_literal_text(self.sequence, "배송순서")
        if driver is None and sequence is None:
            raise ValueError("배송담당이나 배송순서 중 하나는 관찰되어야 합니다.")
        object.__setattr__(self, "driver", driver)
        object.__setattr__(self, "sequence", sequence)

    def for_person(self, person_id: UUID) -> DeliveryAssignment:
        return DeliveryAssignment(person_id, self.meal, self.driver, self.sequence)


@dataclass(frozen=True, slots=True)
class PersonObservation:
    observation_id: UUID
    source: SourceReference
    profile_values: Mapping[ProfileField, str | date | None]
    meals: tuple[Meal, ...]
    assignments: tuple[ObservedDeliveryAssignment, ...] = ()
    additional_mobile_phones: tuple[str, ...] = ()
    additional_telephones: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_uuid(self.observation_id, "observation ID")
        if not isinstance(self.source, SourceReference):
            raise TypeError("source는 SourceReference여야 합니다.")
        values = {
            _profile_field(field): _profile_value(_profile_field(field), value)
            for field, value in self.profile_values.items()
        }
        if ProfileField.NAME not in values:
            raise ValueError("typed observation에는 성명이 필요합니다.")
        meals = tuple(self.meals)
        if any(not isinstance(meal, Meal) for meal in meals):
            raise TypeError("meals에는 Meal만 허용됩니다.")
        if len(set(meals)) != len(meals):
            raise ValueError("관찰 식종은 중복될 수 없습니다.")
        canonical_meals = tuple(meal for meal in Meal if meal in meals)
        assignments = tuple(self.assignments)
        if any(
            not isinstance(assignment, ObservedDeliveryAssignment)
            for assignment in assignments
        ):
            raise TypeError("assignments에는 ObservedDeliveryAssignment만 허용됩니다.")
        assignment_keys = tuple(assignment.meal for assignment in assignments)
        if len(set(assignment_keys)) != len(assignment_keys):
            raise ValueError("관찰 배송 배정은 같은 식종에 중복될 수 없습니다.")
        additional_mobile_phones = _validated_additional_phones(
            self.additional_mobile_phones,
            values.get(ProfileField.MOBILE_PHONE),
            "핸드폰 번호",
        )
        additional_telephones = _validated_additional_phones(
            self.additional_telephones,
            values.get(ProfileField.TELEPHONE),
            "전화번호",
        )
        object.__setattr__(self, "profile_values", MappingProxyType(values))
        object.__setattr__(self, "meals", canonical_meals)
        object.__setattr__(self, "assignments", assignments)
        object.__setattr__(self, "additional_mobile_phones", additional_mobile_phones)
        object.__setattr__(self, "additional_telephones", additional_telephones)

    @property
    def name(self) -> str:
        return str(self.profile_values[ProfileField.NAME])

    @property
    def birth_date(self) -> date | None:
        value = self.profile_values.get(ProfileField.BIRTH_DATE)
        return value if isinstance(value, date) else None

    def merged_profile(self, base: PersonProfile | None = None) -> PersonProfile:
        values = {
            field.value: None
            for field in _PROFILE_FIELDS
            if field is not ProfileField.NAME
        }
        values[ProfileField.NAME.value] = self.name
        if base is not None:
            if not isinstance(base, PersonProfile):
                raise TypeError("base는 PersonProfile 또는 None이어야 합니다.")
            values.update(
                {field.value: getattr(base, field.value) for field in _PROFILE_FIELDS}
            )
        values.update(
            {
                field.value: value
                for field, value in self.profile_values.items()
                if base is None or value is not None
            }
        )
        custom_values = {} if base is None else base.custom_values
        return PersonProfile(**values, custom_values=custom_values)

    def blank_profile_fields_preserved_from(
        self,
        base: PersonProfile,
    ) -> tuple[ProfileField, ...]:
        """Return mapped blank fields whose existing values survive this observation."""

        if not isinstance(base, PersonProfile):
            raise TypeError("base는 PersonProfile이어야 합니다.")
        return tuple(
            field
            for field in _PROFILE_FIELDS
            if field in self.profile_values
            and self.profile_values[field] is None
            and getattr(base, field.value) is not None
        )


def _profile_value(field: ProfileField, value: object) -> str | date | None:
    if field is ProfileField.BIRTH_DATE:
        if value is None:
            return None
        if isinstance(value, datetime) or not isinstance(value, date):
            raise TypeError("관찰 생년월일은 date 또는 None이어야 합니다.")
        return value
    if field is ProfileField.NAME:
        return _required_text(value, "관찰 성명")
    return _optional_text(value, f"관찰 {field.value}")


def _profile_field(value: object) -> ProfileField:
    if isinstance(value, ProfileField):
        return value
    raise TypeError("profile field key는 ProfileField여야 합니다.")


def _phone_comparison_key(value: str) -> tuple[str, str]:
    digits = normalize_phone(value)
    if digits:
        return "digits", digits
    return "text", "".join(value.split()).casefold()


def _validated_additional_phones(
    raw_values: tuple[str, ...],
    primary_value: str | date | None,
    label: str,
) -> tuple[str, ...]:
    values = tuple(_required_text(value, f"추가 {label}") for value in raw_values)
    all_values = (
        *((primary_value,) if isinstance(primary_value, str) else ()),
        *values,
    )
    phone_keys = tuple(_phone_comparison_key(value) for value in all_values)
    if len(set(phone_keys)) != len(phone_keys):
        raise ValueError(f"관찰 {label}는 중복될 수 없습니다.")
    return values


def _meal(value: object) -> Meal:
    if isinstance(value, Meal):
        return value
    raise TypeError("meal flag key는 Meal이어야 합니다.")


def _validate_uuid(value: object, label: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{label}는 UUID여야 합니다.")


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label}은 문자열이어야 합니다.")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{label}은 비어 있을 수 없습니다.")
    return normalized


def _required_literal_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label}은 문자열이어야 합니다.")
    trimmed = value.strip()
    if not trimmed:
        raise ValueError(f"{label}은 비어 있을 수 없습니다.")
    return trimmed


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label)


def _optional_literal_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_literal_text(value, label)


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label}은 정수여야 합니다.")
    if value < 1:
        raise ValueError(f"{label}은 1 이상이어야 합니다.")
    return value


def _flag_values(values: object, label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{label}은 문자열 sequence여야 합니다.")
    try:
        materialized = tuple(values)
    except TypeError as error:
        raise TypeError(f"{label}은 문자열 sequence여야 합니다.") from error
    if any(not isinstance(value, str) for value in materialized):
        raise TypeError(f"{label}에는 문자열만 허용됩니다.")
    result = tuple(value.strip() for value in materialized)
    if not result:
        raise ValueError(f"{label}은 하나 이상이어야 합니다.")
    keys = tuple(_flag_key(value) for value in result)
    if len(set(keys)) != len(keys):
        raise ValueError(f"{label}을 중복 지정할 수 없습니다.")
    return result


def _flag_key(value: str) -> str:
    return " ".join(value.split()).casefold()


def _manifest_mapping(
    manifest: Mapping[str, object],
    key: str,
    *,
    default: object | None = None,
) -> Mapping[object, object]:
    value = manifest.get(key, default)
    if not isinstance(value, Mapping):
        raise TypeError(f"import profile manifest의 {key}는 mapping이어야 합니다.")
    return value


def _manifest_sequence(value: object, label: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError(f"import profile manifest의 {label}은 sequence여야 합니다.")
    try:
        return tuple(value)
    except TypeError as error:
        raise TypeError(
            f"import profile manifest의 {label}은 sequence여야 합니다."
        ) from error
