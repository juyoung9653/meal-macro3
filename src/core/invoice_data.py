from dataclasses import dataclass, field
from datetime import date


@dataclass
class InvoiceData:
    """Cross-tab state bundle: InvoiceTab → ConfirmTab handoff."""

    invoices: list[dict] = field(default_factory=list)
    col_mapping: dict = field(default_factory=dict)
    year: int = 0
    month: int = 0
    excluded_dates: set[date] = field(default_factory=set)
    roster_headers: list[str] = field(default_factory=list)
    roster_path: str | None = None
    preparation_error: str | None = None
    preparation_warnings: list[str] = field(default_factory=list)
