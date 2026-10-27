"""Stable selectable sources for calculated invoice values."""

AUTO_SEQUENCE = "__auto_sequence"
AUTO_BREAKFAST = "__auto_breakfast"
AUTO_LUNCH = "__auto_lunch"
AUTO_DINNER = "__auto_dinner"
AUTO_TOTAL = "__auto_total"
AUTO_PRICE = "__auto_price"
AUTO_HISTORY = "__auto_history"
TEMPLATE_VALUE = "__template_value"

INVOICE_SOURCE_LABELS = {
    AUTO_SEQUENCE: "연번 (자동 계산)",
    AUTO_BREAKFAST: "조식 식수 (자동 계산)",
    AUTO_DINNER: "석식 식수 (자동 계산)",
    AUTO_LUNCH: "중식 식수 (자동 계산)",
    AUTO_TOTAL: "합계 식수 (자동 계산)",
    AUTO_PRICE: "청구금액 (자동 계산)",
    "제공기관": "공통 제공기관",
    AUTO_HISTORY: "식종 변경 이력",
    TEMPLATE_VALUE: "템플릿 값·수식 유지",
}

CALCULATION_ROLES = {
    "__calc_breakfast": ("조식", AUTO_BREAKFAST),
    "__calc_dinner": ("석식", AUTO_DINNER),
    "__calc_lunch": ("중식", AUTO_LUNCH),
    "__calc_total": ("계", AUTO_TOTAL),
    "__calc_price": ("청구금액", AUTO_PRICE),
}


def invoice_output_defaults(mapping, columns, history_column=""):
    """Fill legacy implicit defaults without overwriting explicit connections."""
    result = dict(mapping)
    if history_column in columns:
        result.setdefault(history_column, AUTO_HISTORY)
    for role, (legacy_target, source) in CALCULATION_ROLES.items():
        target = mapping.get(role, legacy_target)
        if target in columns:
            result.setdefault(target, source)
    if "연번" in columns:
        result.setdefault("연번", AUTO_SEQUENCE)
    if "제공기관" in columns:
        result.setdefault("제공기관", "제공기관")
    for target in columns:
        result.setdefault(target, TEMPLATE_VALUE)
    return result
