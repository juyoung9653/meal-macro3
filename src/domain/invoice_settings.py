"""Revisioned monthly count policy and template destinations for history notes."""

from dataclasses import asdict, dataclass
from uuid import UUID

from .revisions import AuditChange, ChangeOperation, RevisionMetadata, RevisionRecord, RevisionSource

INVOICE_SETTINGS_KIND = "invoice_settings"
INVOICE_SETTINGS_ID = UUID("032063e3-6629-49c6-94cc-52fbb53c3399")


@dataclass(frozen=True, slots=True)
class InvoiceSettings:
    include_next_month: bool = True
    school_history_column: str = "평일중식중지=>초1-2,고3"
    lunchbox_history_column: str = "비고"

    def __post_init__(self):
        if type(self.include_next_month) is not bool:
            raise TypeError("다음 달 식수 포함 여부는 참/거짓이어야 합니다.")
        if not all(isinstance(value, str) for value in (self.school_history_column, self.lunchbox_history_column)):
            raise TypeError("청구서 이력 칼럼은 문자열이어야 합니다.")

    def history_column(self, profile):
        if profile not in ("급식", "행복도시락"):
            raise ValueError("지원하지 않는 청구서 양식입니다.")
        return self.school_history_column if profile == "급식" else self.lunchbox_history_column


def invoice_settings_from_snapshot(snapshot):
    state = snapshot.entity(INVOICE_SETTINGS_KIND, INVOICE_SETTINGS_ID)
    return InvoiceSettings() if state is None else InvoiceSettings(**state)


def build_invoice_settings_revision(snapshot, settings, *, actor, recorded_at, revision_id):
    if not isinstance(settings, InvoiceSettings):
        raise TypeError("식수·청구서 설정이 올바르지 않습니다.")
    if invoice_settings_from_snapshot(snapshot) == settings:
        return None
    before = snapshot.entity(INVOICE_SETTINGS_KIND, INVOICE_SETTINGS_ID)
    return RevisionRecord(
        RevisionMetadata(
            revision_id=revision_id, parent_revision_id=snapshot.revision_id,
            recorded_at=recorded_at, effective_on=None, actor=actor,
            reason="식수·청구서 설정 변경", source=RevisionSource.UI,
        ),
        (AuditChange(INVOICE_SETTINGS_KIND, INVOICE_SETTINGS_ID,
                     ChangeOperation.INSERT if before is None else ChangeOperation.UPDATE,
                     before, asdict(settings)),),
    )
