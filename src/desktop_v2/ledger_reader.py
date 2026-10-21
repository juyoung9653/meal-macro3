"""Read an exact revision ancestry for history screens and exports."""


def load_revision_chain(database, revision_id):
    if hasattr(database, "branch_revision_chain"):
        return database.branch_revision_chain(revision_id)
    records = []
    visited = set()
    while revision_id is not None:
        if revision_id in visited:
            raise ValueError("변경 이력 연결이 순환합니다.")
        visited.add(revision_id)
        record = database.revision(revision_id)
        records.append(record)
        revision_id = record.metadata.parent_revision_id
    return tuple(reversed(records))
