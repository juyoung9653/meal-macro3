"""Revisioned policy for interpreting a stop followed by resumed delivery."""

from dataclasses import asdict, dataclass
from uuid import UUID

from .revisions import AuditChange, ChangeOperation, RevisionMetadata, RevisionRecord, RevisionSource

PAUSE_CONVERSION_KIND = "pause_conversion_setting"
PAUSE_CONVERSION_ID = UUID("1fe25bf3-c5a1-4ce1-92ce-6e89ddfa09c3")


@dataclass(frozen=True, slots=True)
class PauseConversionSettings:
    same_month: bool = True

    def __post_init__(self):
        if type(self.same_month) is not bool:
            raise TypeError("일시중지 자동변환 여부는 참/거짓이어야 합니다.")


def pause_conversion_from_snapshot(snapshot):
    state = None if snapshot is None else snapshot.entity(PAUSE_CONVERSION_KIND, PAUSE_CONVERSION_ID)
    return PauseConversionSettings() if state is None else PauseConversionSettings(**state)


def build_pause_conversion_revision(snapshot, settings, *, actor, recorded_at, revision_id):
    if not isinstance(settings, PauseConversionSettings):
        raise TypeError("일시중지 자동변환 설정이 올바르지 않습니다.")
    if pause_conversion_from_snapshot(snapshot) == settings:
        return None
    before = snapshot.entity(PAUSE_CONVERSION_KIND, PAUSE_CONVERSION_ID)
    return RevisionRecord(
        RevisionMetadata(
            revision_id=revision_id, parent_revision_id=snapshot.revision_id,
            recorded_at=recorded_at, effective_on=None, actor=actor,
            reason="일시중지 자동변환 설정 변경", source=RevisionSource.UI,
        ),
        (AuditChange(PAUSE_CONVERSION_KIND, PAUSE_CONVERSION_ID,
                     ChangeOperation.INSERT if before is None else ChangeOperation.UPDATE,
                     before, asdict(settings)),),
    )
