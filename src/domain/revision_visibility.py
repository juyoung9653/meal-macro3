"""Select the effective history while retaining the complete audit ledger."""
from uuid import UUID


def effective_history_revisions(revisions):
    """A restore continues from its target, not from the discarded edits.

    Use ledger order rather than timestamps (several saves can share a time).
    The supplied branch chain may omit checkout parents, so ordinary records
    link to the preceding supplied record.
    """
    records = {}
    previous = {}
    head = None
    for record in revisions:
        rid = record.metadata.revision_id
        target = record.metadata.decisions.get("snapshot_restore_target")
        previous[rid] = UUID(str(target)) if target is not None else head
        records[rid] = record
        head = rid
    result = []
    seen = set()
    while head is not None:
        if head in seen or head not in records:
            raise ValueError("복구 대상의 변경 이력을 다시 불러와주세요.")
        seen.add(head)
        result.append(records[head])
        head = previous[head]
    return tuple(reversed(result))
