"""도메인 타입 — 명단/보강 파이프라인의 타입 안전 인터페이스.

Pipeline: RosterPerson (원본) → EnrichedPerson (보강+계산) → InvoiceData (탭 간 핸드오프)

Usage::
    from src.core.models import RosterPerson, EnrichedPerson
    p = RosterPerson(raw={"성명": "홍길동"}, name="홍길동")
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class RosterPerson:
    """명단 파일에서 읽은 한 사람의 원시 데이터.

    ``raw``에 원본 dict 보존 (이전 dict 기반 코드와의 하위 호환).
    """

    raw: dict[str, Any]
    name: str
    dept: str = ""

    def __getitem__(self, key: str) -> Any:
        """dict 하위 호환: ``person["성명"]`` → ``raw["성명"]``."""
        return self.raw.get(key, "")

    def get(self, key: str, default: Any = "") -> Any:
        """dict 하위 호환: ``person.get("성명")``."""
        return self.raw.get(key, default)


@dataclass
class EnrichedPerson:
    """보강된 명단 데이터 — 배송 플래그, 비고, 계산 결과 포함.

    ``__getitem__``/``get()``으로 dict 하위 호환 유지 → 점진적 마이그레이션 가능.
    """

    person: RosterPerson
    note: str = ""
    flag_breakfast: bool = False
    flag_dinner: bool = False
    flag_lunch: bool = False
    _total: int = 0
    _amount: str = ""

    # dict 하위 호환
    def __getitem__(self, key: str) -> Any:
        _map = {
            "_breakfast": self.flag_breakfast,
            "_dinner": self.flag_dinner,
            "_lunch": self.flag_lunch,
            "_note": self.note,
            "_total": self._total,
            "_amount": self._amount,
        }
        if key in _map:
            return _map[key]
        return self.person[key]

    def get(self, key: str, default: Any = "") -> Any:
        try:
            val = self[key]
            return val if val != "" else default
        except (KeyError, IndexError):
            return default

    def __contains__(self, key: str) -> bool:
        """``"성명" in person`` 지원."""
        return key in self.person.raw or key in (
            "_breakfast",
            "_dinner",
            "_lunch",
            "_note",
            "_total",
            "_amount",
        )


def person_to_dict(p: EnrichedPerson) -> dict:
    """EnrichedPerson → dict (기존 ``list[dict]`` 다운스트림용)."""
    d = dict(p.person.raw)
    d.update(
        {
            "_breakfast": 1 if p.flag_breakfast else 0,
            "_dinner": 1 if p.flag_dinner else 0,
            "_lunch": 1 if p.flag_lunch else 0,
            "_note": p.note,
            "_total": p._total,
            "_amount": p._amount,
        }
    )
    return d
