"""Apply explicit import conflict decisions without committing partial revisions."""
from dataclasses import replace
from datetime import date
from uuid import UUID

from .household_shared import (
    HOUSEHOLD_SHARED_ID, HOUSEHOLD_SHARED_KIND,
    build_household_shared_revision, household_shared_rows, shared_columns_from_state,
)
from .projections import roster_from_historical_snapshot
from .revisions import AuditChange, ChangeOperation, EntityRef, HistoricalSnapshot, RevisionSource

IMPORT_SHARED_VALUES = "import_household_shared_values"


def import_candidate_snapshot(snapshot, record):
    entities = dict(snapshot.entities)
    for change in record.changes:
        ref = EntityRef(change.entity_kind, change.entity_id)
        if change.after is None:
            entities.pop(ref, None)
        else:
            entities[ref] = change.after
    return HistoricalSnapshot(record.metadata.revision_id, entities)


def with_import_shared_values(record, key, on, values):
    choices = dict(record.metadata.decisions.get(IMPORT_SHARED_VALUES, {}))
    choices[key] = {
        "effective_on": on.isoformat(),
        "households": {str(hid): tuple(value) for hid, value in values.items()},
    }
    decisions = {**record.metadata.decisions, IMPORT_SHARED_VALUES: choices}
    return replace(
        record, metadata=replace(record.metadata, decisions=decisions),
        import_batch=(replace(record.import_batch, decisions=decisions)
                      if record.import_batch is not None else None),
    )


def apply_import_shared_values(snapshot, record):
    choices = record.metadata.decisions.get(IMPORT_SHARED_VALUES, {})
    if not choices:
        return record
    if record.metadata.source is not RevisionSource.IMPORT:
        raise ValueError("가져오기에서만 공용 값 검토 결과를 적용할 수 있습니다.")
    stored = snapshot.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
    if not set(choices) <= shared_columns_from_state(stored):
        raise ValueError("공용으로 설정된 칼럼만 검토할 수 있습니다.")
    candidate = import_candidate_snapshot(snapshot, record)
    for key, choice in choices.items():
        on = date.fromisoformat(choice["effective_on"])
        roster = roster_from_historical_snapshot(candidate)
        rows = household_shared_rows(roster, key, on, snapshot=candidate)
        values = {UUID(hid): tuple(value) for hid, value in choice["households"].items()}
        if set(values) != {row.household_id for row in rows}:
            raise ValueError("모든 가구의 공용 값을 확인해주세요.")
        if any(len(values[row.household_id]) != len(row.values)
               or any(not isinstance(value, str) for value in values[row.household_id]) for row in rows):
            raise ValueError("충돌한 값을 직접 입력하거나 비우기로 확정해주세요.")
        if all(values[row.household_id] == row.values for row in rows):
            continue
        correction = build_household_shared_revision(
            roster, stored, key, True, values,
            actor=record.metadata.actor, recorded_at=record.metadata.recorded_at,
            effective_on=on, revision_id=record.metadata.revision_id, snapshot=candidate,
        )
        candidate = import_candidate_snapshot(candidate, correction)
    # Compose each entity's final value against the original DB snapshot so a
    # person changed in several review dialogs still has exactly one audit row.
    changes = []
    for ref in dict.fromkeys((*snapshot.entities, *candidate.entities)):
        before, after = snapshot.entities.get(ref), candidate.entities.get(ref)
        if before == after:
            continue
        operation = (ChangeOperation.INSERT if before is None else
                     ChangeOperation.DELETE if after is None else ChangeOperation.UPDATE)
        changes.append(AuditChange(ref.entity_kind, ref.entity_id, operation, before, after))
    return replace(record, changes=tuple(changes))
