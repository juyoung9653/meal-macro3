"""Parse imported text against the destination custom column definition."""
from datetime import date, datetime
from src.domain.roster import CustomFieldType


def import_custom_value(definition, value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if definition.field_type is CustomFieldType.NUMBER:
        text = str(value).strip()
        try:
            value = int(text)
        except ValueError:
            try:
                value = float(text)
            except ValueError:
                raise ValueError(f"{definition.name}: 숫자를 입력해주세요.") from None
        if isinstance(value, float) and value.is_integer():
            value = int(value)
    elif definition.field_type is CustomFieldType.DATE:
        text = str(value).strip()
        try:
            value = date.fromisoformat(text)
        except ValueError:
            try:
                value = datetime.fromisoformat(text).date()
            except ValueError:
                raise ValueError(f"{definition.name}: 날짜를 YYYY-MM-DD 형식으로 입력해주세요.") from None
    definition.validate_value(value)
    return value
