from dataclasses import dataclass, field
from datetime import date
from typing import TypedDict


class EnrichedPersonDict(TypedDict, total=False):
    """Typed dict representing an enriched person record passed between tabs."""

    # Raw roster fields (dynamic, from Excel headers)
    # Enriched fields (always present):
    _breakfast: int  # 0 or 1
    _dinner: int  # 0 or 1
    _lunch: int  # 0 or 1
    _note: str
    _total: int
    _amount: str
    _bf_cnt: int
    _dn_cnt: int
    _lh_cnt: int
    _delivery_start: str
    _delivery_end: str
    _daily: dict
    _delivery_rows_raw: list[dict]
    배송순서: str
    배송담당: str
    핸드폰: str


@dataclass
class InvoiceData:
    """Cross-tab state bundle: InvoiceTab → ConfirmTab handoff."""

    invoices: list[EnrichedPersonDict] = field(default_factory=list)
    col_mapping: dict = field(default_factory=dict)
    year: int = 0
    month: int = 0
    excluded_dates: set[date] = field(default_factory=set)
    roster_headers: list[str] = field(default_factory=list)
    roster_path: str | None = None
