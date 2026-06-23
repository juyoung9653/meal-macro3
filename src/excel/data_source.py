"""DataSource 추상 인터페이스 — 청구서/확인서가 필요로 하는 8개 메서드."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date


class DataSource(ABC):
    """Abstract interface for invoice/confirm data access."""

    @property
    @abstractmethod
    def db_path(self) -> str: ...

    @abstractmethod
    def read_roster(self) -> list[dict]: ...

    @abstractmethod
    def read_delivery_matrix(self) -> list[dict]: ...

    @abstractmethod
    def build_delivery_map(self) -> dict[str, dict]: ...

    @abstractmethod
    def build_order_map(self) -> dict[str, dict]: ...

    @abstractmethod
    def read_delivery_contacts(self) -> list[dict]: ...

    @abstractmethod
    def get_holiday_set(self) -> set[date]: ...

    @abstractmethod
    def read_meal_prices(self) -> dict[str, int]: ...

    @abstractmethod
    def detect_month(self) -> tuple[int, int]: ...
