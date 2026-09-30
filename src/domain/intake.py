"""Shared channels through which a delivery-status request was received."""

from enum import StrEnum


class IntakeChannel(StrEnum):
    PHONE = "전화"
    EMAIL = "메일"
    OTHER = "기타"
