import json
import os
from dataclasses import dataclass, field
from datetime import date

CACHE_FILENAME = ".invoice_data_cache.json"


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

    def to_cache_dict(self) -> dict:
        """Serialize to JSON-safe dict."""
        return {
            "invoices": self.invoices,
            "col_mapping": self.col_mapping,
            "year": self.year,
            "month": self.month,
            "excluded_dates": [d.isoformat() for d in self.excluded_dates],
            "roster_headers": self.roster_headers,
            "roster_path": self.roster_path,
        }

    @classmethod
    def from_cache_dict(cls, payload: dict) -> "InvoiceData":
        """Deserialize from JSON-safe dict."""
        return cls(
            invoices=payload.get("invoices", []),
            col_mapping=payload.get("col_mapping", {}),
            year=payload.get("year", 0),
            month=payload.get("month", 0),
            excluded_dates={
                date.fromisoformat(d) for d in payload.get("excluded_dates", [])
            },
            roster_headers=payload.get("roster_headers", []),
            roster_path=payload.get("roster_path"),
        )

    @classmethod
    def load_cache(cls, output_dir: str) -> "InvoiceData | None":
        """Load from cache file in output_dir. Returns None on failure."""
        cache_path = os.path.join(output_dir, CACHE_FILENAME)
        if not os.path.exists(cache_path):
            return None
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return cls.from_cache_dict(json.load(f))
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def save_cache(self, output_dir: str) -> None:
        """Save to cache file in output_dir."""
        cache_path = os.path.join(output_dir, CACHE_FILENAME)
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(self.to_cache_dict(), f, ensure_ascii=False, indent=2)
        except OSError:
            pass
