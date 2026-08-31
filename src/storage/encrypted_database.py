"""Encrypted SQLite Adapter with an immutable linear revision ledger."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import apsw

from src.domain.documents import DocumentKind, DocumentRun
from src.domain.projections import roster_from_historical_snapshot
from src.domain.revisions import (
    AuditChange,
    ChangeOperation,
    EntityRef,
    HistoricalSnapshot,
    ImportBatch,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSummary,
    RevisionSource,
    freeze_json_object,
    plain_json,
)
from src.domain.roster import Meal, RosterSnapshot
from src.domain.status import StatusEvent, StatusHistory, resolve_status_history

from .migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS
from .typed_projection import (
    ProjectionContractError,
    apply_typed_projection_changes,
    load_current_roster,
    load_status_events,
    validate_typed_projections,
)

_APPLICATION_ID = 0x4D445632  # "MDV2"
_CIPHER = "chacha20"
_KEY_BYTES = 32
_BUSY_TIMEOUT_MS = 5_000
_KOREA_TIME_ZONE = timezone(timedelta(hours=9), name="Asia/Seoul")
_MINIMUM_AUTO_MIGRATION_VERSION = 2


class DatabaseNotFoundError(FileNotFoundError):
    pass


class DatabaseAlreadyExistsError(FileExistsError):
    pass


class DatabaseEncryptionUnavailableError(RuntimeError):
    pass


class DatabaseUnlockError(RuntimeError):
    pass


class DatabaseFormatError(RuntimeError):
    pass


class DatabaseIntegrityError(RuntimeError):
    pass


class RevisionConflictError(RuntimeError):
    pass


class EncryptedDatabase:
    """Owns one encrypted connection and hides APSW from domain callers."""

    def __init__(self, path: Path, connection: apsw.Connection) -> None:
        self._path = path
        self._connection: apsw.Connection | None = connection

    @classmethod
    def create(cls, path: str | Path, key: bytes) -> EncryptedDatabase:
        database_path = _database_path(path)
        _validate_key(key)
        if database_path.exists():
            raise DatabaseAlreadyExistsError(f"DB 파일이 이미 있습니다: {database_path}")
        if not database_path.parent.is_dir():
            raise DatabaseNotFoundError(
                f"DB 상위 폴더가 없습니다: {database_path.parent}"
            )

        connection: apsw.Connection | None = None
        try:
            connection = _connect(database_path, key, create=True)
            _initialize_schema(connection)
            _verify_encrypted_header(database_path)
            _apply_runtime_pragmas(connection)
            return cls(database_path, connection)
        except Exception:
            if connection is not None:
                connection.close()
            _remove_incomplete_database(database_path)
            raise

    @classmethod
    def open(cls, path: str | Path, key: bytes) -> EncryptedDatabase:
        database_path = _database_path(path)
        _validate_key(key)
        if not database_path.is_file():
            raise DatabaseNotFoundError(f"DB 파일이 없습니다: {database_path}")
        _verify_encrypted_header(database_path)
        connection = _connect(database_path, key, create=False)
        try:
            _upgrade_schema(connection)
            _validate_schema(connection)
            _validate_ledger(connection)
            _validate_typed_projection_integrity(connection)
            _validate_document_runs(connection)
            _apply_runtime_pragmas(connection)
        except Exception:
            connection.close()
            raise
        return cls(database_path, connection)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def schema_version(self) -> int:
        return int(self._require_connection().pragma("user_version"))

    def append_revision(self, record: RevisionRecord) -> None:
        if not isinstance(record, RevisionRecord):
            raise TypeError("record는 RevisionRecord여야 합니다.")
        if record.metadata.display_code is not None:
            raise ValueError("표시용 변경번호는 DB가 저장 시 자동으로 부여합니다.")
        connection = self._require_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT revision_id FROM revision ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            actual_parent = None if row is None else UUID(str(row[0]))
            expected_parent = record.metadata.parent_revision_id
            if expected_parent != actual_parent:
                raise RevisionConflictError(
                    "parent revision이 현재 최신 revision과 다릅니다. "
                    f"expected={expected_parent}, actual={actual_parent}"
                )
            _validate_changes_against_current(connection, record.changes)
            _insert_revision(connection, record.metadata)
            _insert_revision_display_code(connection, record.metadata)
            for ordinal, change in enumerate(record.changes, start=1):
                _insert_change(connection, record.metadata.revision_id, ordinal, change)
            apply_typed_projection_changes(
                connection,
                record.metadata.revision_id,
                record.changes,
            )
            validate_typed_projections(connection)
            if record.import_batch is not None:
                _insert_import_batch(connection, record.import_batch)
            connection.execute(
                """
                INSERT INTO revision_seal(
                    revision_id, sealed_at, change_count, has_import_batch
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    str(record.metadata.revision_id),
                    datetime.now(UTC).isoformat(),
                    len(record.changes),
                    int(record.import_batch is not None),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def latest_revision(self) -> RevisionRecord | None:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT revision_id FROM revision ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return self.revision(UUID(str(row[0])))

    def revision(self, revision_id: UUID) -> RevisionRecord:
        if not isinstance(revision_id, UUID):
            raise TypeError("revision ID는 UUID여야 합니다.")
        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT parent_revision_id, recorded_at, effective_on,
                   actor_username, actor_sid, reason, source,
                   decisions_json, rule_versions_json,
                   revision_display_code.display_code
            FROM revision
            JOIN revision_display_code
              ON revision_display_code.revision_id = revision.revision_id
            WHERE revision.revision_id = ?
            """,
            (str(revision_id),),
        ).fetchone()
        if row is None:
            raise KeyError(f"없는 revision ID입니다: {revision_id}")
        seal = connection.execute(
            """
            SELECT change_count, has_import_batch
            FROM revision_seal
            WHERE revision_id = ?
            """,
            (str(revision_id),),
        ).fetchone()
        if seal is None:
            raise DatabaseIntegrityError(f"봉인되지 않은 revision입니다: {revision_id}")

        metadata = RevisionMetadata(
            revision_id=revision_id,
            parent_revision_id=None if row[0] is None else UUID(str(row[0])),
            recorded_at=datetime.fromisoformat(str(row[1])),
            effective_on=None if row[2] is None else date.fromisoformat(str(row[2])),
            actor=RevisionActor(str(row[3]), str(row[4])),
            reason=str(row[5]),
            source=RevisionSource(str(row[6])),
            decisions=json.loads(str(row[7])),
            rule_versions=json.loads(str(row[8])),
            display_code=str(row[9]),
        )
        changes = tuple(
            AuditChange(
                entity_kind=str(change[0]),
                entity_id=UUID(str(change[1])),
                operation=ChangeOperation(str(change[2])),
                before=None if change[3] is None else json.loads(str(change[3])),
                after=None if change[4] is None else json.loads(str(change[4])),
            )
            for change in connection.execute(
                """
                SELECT entity_kind, entity_id, operation, before_json, after_json
                FROM audit_change
                WHERE revision_id = ?
                ORDER BY ordinal
                """,
                (str(revision_id),),
            )
        )
        batch_row = connection.execute(
            """
            SELECT batch_id, source_filename, sha256, byte_size, sheet_name,
                   header_fingerprint, mapping_manifest_json, effective_on,
                   decisions_json
            FROM import_batch
            WHERE revision_id = ?
            """,
            (str(revision_id),),
        ).fetchone()
        import_batch = None
        if batch_row is not None:
            import_batch = ImportBatch(
                batch_id=UUID(str(batch_row[0])),
                revision_id=revision_id,
                source_filename=str(batch_row[1]),
                sha256=str(batch_row[2]),
                byte_size=int(batch_row[3]),
                sheet_name=None if batch_row[4] is None else str(batch_row[4]),
                header_fingerprint=str(batch_row[5]),
                mapping_manifest=json.loads(str(batch_row[6])),
                effective_on=date.fromisoformat(str(batch_row[7])),
                decisions=json.loads(str(batch_row[8])),
            )
        if int(seal[0]) != len(changes) or bool(seal[1]) != (import_batch is not None):
            raise DatabaseIntegrityError(f"revision 봉인 정보가 다릅니다: {revision_id}")
        return RevisionRecord(metadata, changes, import_batch)

    def snapshot(self, revision_id: UUID) -> HistoricalSnapshot:
        if not isinstance(revision_id, UUID):
            raise TypeError("revision ID는 UUID여야 합니다.")
        self.revision(revision_id)
        connection = self._require_connection()
        target = connection.execute(
            "SELECT sequence FROM revision WHERE revision_id = ?",
            (str(revision_id),),
        ).fetchone()
        if target is None:
            raise KeyError(f"없는 revision ID입니다: {revision_id}")

        entities: dict[EntityRef, object] = {}
        for row in connection.execute(
            """
            SELECT change.entity_kind, change.entity_id,
                   change.operation, change.after_json
            FROM audit_change AS change
            JOIN revision ON revision.revision_id = change.revision_id
            WHERE revision.sequence <= ?
            ORDER BY revision.sequence, change.ordinal
            """,
            (int(target[0]),),
        ):
            reference = EntityRef(str(row[0]), UUID(str(row[1])))
            operation = ChangeOperation(str(row[2]))
            if operation is ChangeOperation.DELETE:
                entities.pop(reference, None)
            else:
                entities[reference] = freeze_json_object(json.loads(str(row[3])))
        return HistoricalSnapshot(revision_id, entities)

    def roster_snapshot(self, revision_id: UUID) -> RosterSnapshot:
        """Rehydrate the roster at an explicit immutable revision."""

        return roster_from_historical_snapshot(self.snapshot(revision_id))

    def revision_display_code(self, revision_id: UUID) -> str:
        """Return the stable human-facing code assigned when a revision was saved."""

        if not isinstance(revision_id, UUID):
            raise TypeError("revision ID는 UUID여야 합니다.")
        row = self._require_connection().execute(
            "SELECT display_code FROM revision_display_code WHERE revision_id = ?",
            (str(revision_id),),
        ).fetchone()
        if row is None:
            raise KeyError(f"없는 revision ID입니다: {revision_id}")
        return str(row[0])

    def revision_summaries(self) -> tuple[RevisionSummary, ...]:
        """List sealed revisions newest-first for history/source selection."""

        return tuple(
            RevisionSummary(
                sequence=int(row[0]),
                revision_id=UUID(str(row[1])),
                display_code=str(row[2]),
                recorded_at=datetime.fromisoformat(str(row[3])),
                effective_on=(
                    None if row[4] is None else date.fromisoformat(str(row[4]))
                ),
                actor=RevisionActor(str(row[5]), str(row[6])),
                reason=str(row[7]),
                source=RevisionSource(str(row[8])),
            )
            for row in self._require_connection().execute(
                """
                SELECT revision.sequence, revision.revision_id,
                       revision_display_code.display_code,
                       revision.recorded_at, revision.effective_on,
                       revision.actor_username, revision.actor_sid,
                       revision.reason, revision.source
                FROM revision
                JOIN revision_display_code
                  ON revision_display_code.revision_id = revision.revision_id
                JOIN revision_seal
                  ON revision_seal.revision_id = revision.revision_id
                ORDER BY revision.sequence DESC
                """
            )
        )

    def append_document_run(self, run: DocumentRun) -> None:
        """Append one successful generated-file record without a roster revision."""

        if not isinstance(run, DocumentRun):
            raise TypeError("run은 DocumentRun이어야 합니다.")
        connection = self._require_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            _insert_document_run(connection, run)
            _validate_document_runs(connection)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def document_runs(
        self,
        *,
        kind: DocumentKind | None = None,
    ) -> tuple[DocumentRun, ...]:
        if kind is not None and not isinstance(kind, DocumentKind):
            raise TypeError("kind는 DocumentKind 또는 None이어야 합니다.")
        sql = """
            SELECT run_id, document_kind, source_revision_id,
                   source_revision_display_code, period_start, period_end,
                   generated_at, actor_username, actor_sid,
                   calculation_manifest_json, template_manifest_json,
                   output_filename, output_sha256, output_byte_size
            FROM document_run
        """
        parameters: tuple[str, ...] = ()
        if kind is not None:
            sql += " WHERE document_kind = ?"
            parameters = (kind.value,)
        sql += " ORDER BY sequence DESC"
        return tuple(
            _document_run_from_row(row)
            for row in self._require_connection().execute(sql, parameters)
        )

    def document_run(self, run_id: UUID) -> DocumentRun:
        if not isinstance(run_id, UUID):
            raise TypeError("문서 run ID는 UUID여야 합니다.")
        row = self._require_connection().execute(
            """
            SELECT run_id, document_kind, source_revision_id,
                   source_revision_display_code, period_start, period_end,
                   generated_at, actor_username, actor_sid,
                   calculation_manifest_json, template_manifest_json,
                   output_filename, output_sha256, output_byte_size
            FROM document_run
            WHERE run_id = ?
            """,
            (str(run_id),),
        ).fetchone()
        if row is None:
            raise KeyError(f"없는 문서 run ID입니다: {run_id}")
        return _document_run_from_row(row)

    def document_source_snapshot(self, run_id: UUID) -> HistoricalSnapshot:
        """Replay the immutable source state used by one generated document."""

        run = self.document_run(run_id)
        return self.snapshot(run.source_revision_id)

    def current_roster(self) -> RosterSnapshot | None:
        """Load the typed projection used by current-state UI queries."""

        connection = self._require_connection()
        row = connection.execute(
            "SELECT revision_id FROM revision ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return load_current_roster(connection, UUID(str(row[0])))

    def status_events(
        self,
        *,
        person_id: UUID | None = None,
        meal: Meal | None = None,
    ) -> tuple[StatusEvent, ...]:
        """Load preserved raw events, optionally filtered by person and meal."""

        return load_status_events(
            self._require_connection(),
            person_id=person_id,
            meal=meal,
        )

    def status_history(self, person_id: UUID, meal: Meal) -> StatusHistory:
        """Resolve preserved raw events into effective status intervals."""

        return resolve_status_history(
            person_id,
            meal,
            self.status_events(person_id=person_id, meal=meal),
        )

    def check_integrity(self) -> None:
        rows = tuple(self._require_connection().execute("PRAGMA integrity_check"))
        if rows != (("ok",),):
            details = "; ".join(str(row[0]) for row in rows)
            raise DatabaseIntegrityError(f"DB 무결성 검사 실패: {details}")
        _validate_ledger(self._require_connection())
        _validate_typed_projection_integrity(self._require_connection())
        _validate_document_runs(self._require_connection())

    def backup_to(self, path: str | Path, key: bytes) -> Path:
        """Create and verify a consistent encrypted online backup."""

        backup_path = _database_path(path)
        _validate_key(key)
        if backup_path == self._path:
            raise ValueError("원본 DB와 백업 DB 경로는 달라야 합니다.")
        if backup_path.exists():
            raise DatabaseAlreadyExistsError(
                f"백업 DB 파일이 이미 있습니다: {backup_path}"
            )
        if not backup_path.parent.is_dir():
            raise DatabaseNotFoundError(
                f"백업 DB 상위 폴더가 없습니다: {backup_path.parent}"
            )

        self.check_integrity()
        destination: apsw.Connection | None = None
        try:
            destination = _connect(backup_path, key, create=True)
            with destination.backup(
                "main",
                self._require_connection(),
                "main",
            ) as backup:
                while not backup.done:
                    backup.step()
            _verify_encrypted_header(backup_path)
            _validate_schema(destination)
            _validate_ledger(destination)
            _validate_typed_projection_integrity(destination)
            _validate_document_runs(destination)
            rows = tuple(destination.execute("PRAGMA integrity_check"))
            if rows != (("ok",),):
                details = "; ".join(str(row[0]) for row in rows)
                raise DatabaseIntegrityError(
                    f"백업 DB 무결성 검사 실패: {details}"
                )
        except Exception:
            if destination is not None:
                destination.close()
                destination = None
            _remove_incomplete_database(backup_path)
            raise
        finally:
            if destination is not None:
                destination.close()
        return backup_path

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> EncryptedDatabase:
        self._require_connection()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()

    def _require_connection(self) -> apsw.Connection:
        if self._connection is None:
            raise RuntimeError("DB 연결이 이미 닫혔습니다.")
        return self._connection


def _connect(path: Path, key: bytes, *, create: bool) -> apsw.Connection:
    _require_encrypted_runtime()
    flags = apsw.SQLITE_OPEN_READWRITE
    if create:
        flags |= apsw.SQLITE_OPEN_CREATE | apsw.SQLITE_OPEN_EXCLUSIVE
    connection = apsw.Connection(str(path), flags=flags)
    try:
        connection.pragma("cipher", _CIPHER)
        connection.pragma("hexkey", key.hex())
        connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()
    except apsw.Error as error:
        connection.close()
        raise DatabaseUnlockError(
            "DB를 열 수 없습니다. 비밀번호가 다르거나 파일이 손상되었습니다."
        ) from error
    return connection


def _initialize_schema(connection: apsw.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.pragma("application_id", _APPLICATION_ID)
        for migration in MIGRATIONS:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO schema_migration(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.pragma("user_version", migration.version)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _upgrade_schema(connection: apsw.Connection) -> None:
    application_id = int(connection.pragma("application_id"))
    if application_id != _APPLICATION_ID:
        raise DatabaseFormatError("이 앱에서 만든 SQLite v2 DB가 아닙니다.")
    user_version = int(connection.pragma("user_version"))
    if user_version == CURRENT_SCHEMA_VERSION:
        return
    if not (_MINIMUM_AUTO_MIGRATION_VERSION <= user_version < CURRENT_SCHEMA_VERSION):
        raise DatabaseFormatError(
            "자동 변환할 수 없는 DB schema version입니다: "
            f"{user_version} (지원: {_MINIMUM_AUTO_MIGRATION_VERSION}~"
            f"{CURRENT_SCHEMA_VERSION})"
        )

    applied_rows = tuple(
        connection.execute(
            "SELECT version, name, checksum FROM schema_migration ORDER BY version"
        )
    )
    expected_prefix = tuple(
        (migration.version, migration.name, migration.checksum)
        for migration in MIGRATIONS
        if migration.version <= user_version
    )
    if applied_rows != expected_prefix:
        raise DatabaseFormatError("DB migration 이력이 실행 코드와 일치하지 않습니다.")

    connection.pragma("foreign_keys", True)
    connection.execute("BEGIN IMMEDIATE")
    try:
        for migration in MIGRATIONS:
            if migration.version <= user_version:
                continue
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO schema_migration(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.pragma("user_version", migration.version)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _validate_schema(connection: apsw.Connection) -> None:
    application_id = int(connection.pragma("application_id"))
    if application_id != _APPLICATION_ID:
        raise DatabaseFormatError("이 앱에서 만든 SQLite v2 DB가 아닙니다.")
    user_version = int(connection.pragma("user_version"))
    if user_version != CURRENT_SCHEMA_VERSION:
        raise DatabaseFormatError(
            "지원하지 않는 DB schema version입니다: "
            f"{user_version} (지원: {CURRENT_SCHEMA_VERSION})"
        )
    rows = tuple(
        connection.execute(
            "SELECT version, name, checksum FROM schema_migration ORDER BY version"
        )
    )
    expected = tuple(
        (migration.version, migration.name, migration.checksum)
        for migration in MIGRATIONS
    )
    if rows != expected:
        raise DatabaseFormatError("DB migration 이력이 실행 코드와 일치하지 않습니다.")


def _apply_runtime_pragmas(connection: apsw.Connection) -> None:
    connection.pragma("foreign_keys", True)
    connection.pragma("trusted_schema", False)
    connection.pragma("secure_delete", True)
    connection.pragma("temp_store", 2)
    connection.pragma("busy_timeout", _BUSY_TIMEOUT_MS)
    connection.pragma("journal_mode", "delete")
    connection.pragma("synchronous", "full")


def _insert_revision(
    connection: apsw.Connection,
    metadata: RevisionMetadata,
) -> None:
    connection.execute(
        """
        INSERT INTO revision(
            revision_id, parent_revision_id, recorded_at, effective_on,
            actor_username, actor_sid, reason, source,
            decisions_json, rule_versions_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(metadata.revision_id),
            None if metadata.parent_revision_id is None else str(metadata.parent_revision_id),
            metadata.recorded_at.astimezone(UTC).isoformat(),
            None if metadata.effective_on is None else metadata.effective_on.isoformat(),
            metadata.actor.username,
            metadata.actor.sid,
            metadata.reason,
            metadata.source.value,
            _json_text(metadata.decisions),
            _json_text(metadata.rule_versions),
        ),
    )


def _insert_revision_display_code(
    connection: apsw.Connection,
    metadata: RevisionMetadata,
) -> None:
    recorded_local_date = metadata.recorded_at.astimezone(_KOREA_TIME_ZONE).date()
    row = connection.execute(
        """
        SELECT coalesce(max(daily_ordinal), 0) + 1
        FROM revision_display_code
        WHERE recorded_local_date = ?
        """,
        (recorded_local_date.isoformat(),),
    ).fetchone()
    daily_ordinal = int(row[0])
    display_code = f"{recorded_local_date:%Y%m%d}-{daily_ordinal:03d}"
    connection.execute(
        """
        INSERT INTO revision_display_code(
            revision_id, recorded_local_date, daily_ordinal, display_code
        ) VALUES (?, ?, ?, ?)
        """,
        (
            str(metadata.revision_id),
            recorded_local_date.isoformat(),
            daily_ordinal,
            display_code,
        ),
    )


def _insert_change(
    connection: apsw.Connection,
    revision_id: UUID,
    ordinal: int,
    change: AuditChange,
) -> None:
    connection.execute(
        """
        INSERT INTO audit_change(
            revision_id, ordinal, entity_kind, entity_id, operation,
            before_json, after_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(revision_id),
            ordinal,
            change.entity_kind,
            str(change.entity_id),
            change.operation.value,
            None if change.before is None else _json_text(change.before),
            None if change.after is None else _json_text(change.after),
        ),
    )


def _insert_document_run(
    connection: apsw.Connection,
    run: DocumentRun,
) -> None:
    connection.execute(
        """
        INSERT INTO document_run(
            run_id, document_kind, source_revision_id,
            source_revision_display_code, period_start, period_end,
            generated_at, actor_username, actor_sid,
            calculation_manifest_json, template_manifest_json,
            output_filename, output_sha256, output_byte_size
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(run.run_id),
            run.kind.value,
            str(run.source_revision_id),
            run.source_revision_display_code,
            run.period_start.isoformat(),
            run.period_end.isoformat(),
            run.generated_at.astimezone(UTC).isoformat(),
            run.actor.username,
            run.actor.sid,
            _json_text(run.calculation_manifest),
            _json_text(run.template_manifest),
            run.output_filename,
            run.output_sha256,
            run.output_byte_size,
        ),
    )


def _insert_import_batch(
    connection: apsw.Connection,
    batch: ImportBatch,
) -> None:
    connection.execute(
        """
        INSERT INTO import_batch(
            batch_id, revision_id, source_filename, sha256, byte_size,
            sheet_name, header_fingerprint, mapping_manifest_json,
            effective_on, decisions_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(batch.batch_id),
            str(batch.revision_id),
            batch.source_filename,
            batch.sha256,
            batch.byte_size,
            batch.sheet_name,
            batch.header_fingerprint,
            _json_text(batch.mapping_manifest),
            batch.effective_on.isoformat(),
            _json_text(batch.decisions),
        ),
    )


def _validate_changes_against_current(
    connection: apsw.Connection,
    changes: tuple[AuditChange, ...],
) -> None:
    for change in changes:
        row = connection.execute(
            """
            SELECT change.operation, change.after_json
            FROM audit_change AS change
            JOIN revision ON revision.revision_id = change.revision_id
            WHERE change.entity_kind = ? AND change.entity_id = ?
            ORDER BY revision.sequence DESC
            LIMIT 1
            """,
            (change.entity_kind, str(change.entity_id)),
        ).fetchone()
        current = None
        if row is not None and ChangeOperation(str(row[0])) is not ChangeOperation.DELETE:
            current = json.loads(str(row[1]))

        if change.operation is ChangeOperation.INSERT:
            if row is not None:
                raise RevisionConflictError(
                    f"이력이 있는 entity ID를 다시 insert할 수 없습니다: "
                    f"{change.entity_kind}/{change.entity_id}"
                )
            continue
        expected_before = plain_json(change.before)
        if current != expected_before:
            raise RevisionConflictError(
                "audit before가 현재 entity 상태와 다릅니다: "
                f"{change.entity_kind}/{change.entity_id}"
            )


def _json_text(value) -> str:
    return json.dumps(
        plain_json(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _database_path(value: str | Path) -> Path:
    path = Path(value)
    if not path.name:
        raise ValueError("DB 파일 경로가 필요합니다.")
    return path.resolve()


def _validate_key(key: object) -> None:
    if not isinstance(key, bytes):
        raise TypeError("DB key는 bytes여야 합니다.")
    if len(key) != _KEY_BYTES:
        raise ValueError(f"DB key는 {_KEY_BYTES} bytes여야 합니다.")


def _require_encrypted_runtime() -> None:
    mc_version = getattr(apsw, "mc_version", None)
    if not isinstance(mc_version, str) or not mc_version.startswith(
        "SQLite3 Multiple Ciphers "
    ):
        raise DatabaseEncryptionUnavailableError(
            "apsw-sqlite3mc 암호화 runtime이 필요합니다."
        )


def _verify_encrypted_header(path: Path) -> None:
    with path.open("rb") as database_file:
        header = database_file.read(16)
    if header == b"SQLite format 3\x00":
        raise DatabaseFormatError("평문 SQLite DB는 열 수 없습니다.")


def _validate_ledger(connection: apsw.Connection) -> None:
    rows = tuple(
        connection.execute(
            """
            SELECT revision.sequence, revision.revision_id,
                   revision.parent_revision_id,
                   revision_seal.change_count,
                   revision_seal.has_import_batch,
                   COUNT(DISTINCT audit_change.ordinal),
                   COUNT(DISTINCT import_batch.batch_id)
            FROM revision
            LEFT JOIN revision_seal
                   ON revision_seal.revision_id = revision.revision_id
            LEFT JOIN audit_change
                   ON audit_change.revision_id = revision.revision_id
            LEFT JOIN import_batch
                   ON import_batch.revision_id = revision.revision_id
            GROUP BY revision.sequence, revision.revision_id,
                     revision.parent_revision_id,
                     revision_seal.change_count,
                     revision_seal.has_import_batch
            ORDER BY revision.sequence
            """
        )
    )
    previous_id: str | None = None
    for sequence, revision_id, parent_id, sealed_changes, sealed_import, changes, batches in rows:
        if parent_id != previous_id:
            raise DatabaseIntegrityError(
                f"revision parent chain이 끊겼습니다: {revision_id}"
            )
        if sealed_changes is None:
            raise DatabaseIntegrityError(f"봉인되지 않은 revision입니다: {revision_id}")
        if int(sealed_changes) != int(changes) or int(sealed_import) != int(batches):
            raise DatabaseIntegrityError(f"revision 봉인 정보가 다릅니다: {revision_id}")
        if int(sequence) < 1:
            raise DatabaseIntegrityError("revision sequence가 올바르지 않습니다.")
        previous_id = str(revision_id)

    code_rows = tuple(
        connection.execute(
            """
            SELECT revision.revision_id, revision.recorded_at,
                   revision_display_code.recorded_local_date,
                   revision_display_code.daily_ordinal,
                   revision_display_code.display_code
            FROM revision
            LEFT JOIN revision_display_code
              ON revision_display_code.revision_id = revision.revision_id
            ORDER BY revision.sequence
            """
        )
    )
    code_count = int(
        connection.execute("SELECT count(*) FROM revision_display_code").fetchone()[0]
    )
    if code_count != len(code_rows):
        raise DatabaseIntegrityError("revision 표시용 변경번호 개수가 맞지 않습니다.")
    daily_counts: dict[date, int] = {}
    for revision_id, recorded_at, local_date, daily_ordinal, display_code in code_rows:
        if local_date is None or daily_ordinal is None or display_code is None:
            raise DatabaseIntegrityError(
                f"revision 표시용 변경번호가 없습니다: {revision_id}"
            )
        expected_date = datetime.fromisoformat(str(recorded_at)).astimezone(
            _KOREA_TIME_ZONE
        ).date()
        expected_ordinal = daily_counts.get(expected_date, 0) + 1
        daily_counts[expected_date] = expected_ordinal
        expected_code = f"{expected_date:%Y%m%d}-{expected_ordinal:03d}"
        if (
            str(local_date) != expected_date.isoformat()
            or int(daily_ordinal) != expected_ordinal
            or str(display_code) != expected_code
        ):
            raise DatabaseIntegrityError(
                f"revision 표시용 변경번호가 기록 순서와 다릅니다: {revision_id}"
            )

    import_source_mismatches = tuple(
        connection.execute(
            """
            SELECT revision.revision_id
            FROM revision
            JOIN import_batch ON import_batch.revision_id = revision.revision_id
            WHERE revision.source != 'import'
            """
        )
    )
    if import_source_mismatches:
        raise DatabaseIntegrityError("import batch의 revision source가 import가 아닙니다.")

    current: dict[EntityRef, object] = {}
    seen: set[EntityRef] = set()
    for row in connection.execute(
        """
        SELECT audit_change.entity_kind, audit_change.entity_id,
               audit_change.operation, audit_change.before_json,
               audit_change.after_json
        FROM audit_change
        JOIN revision ON revision.revision_id = audit_change.revision_id
        ORDER BY revision.sequence, audit_change.ordinal
        """
    ):
        reference = EntityRef(str(row[0]), UUID(str(row[1])))
        operation = ChangeOperation(str(row[2]))
        before = None if row[3] is None else json.loads(str(row[3]))
        after = None if row[4] is None else json.loads(str(row[4]))
        if operation is ChangeOperation.INSERT:
            if reference in seen:
                raise DatabaseIntegrityError(
                    f"entity ID가 다시 insert되었습니다: {reference}"
                )
            seen.add(reference)
            current[reference] = after
            continue
        if reference not in current or current[reference] != before:
            raise DatabaseIntegrityError(
                f"audit before chain이 일치하지 않습니다: {reference}"
            )
        if operation is ChangeOperation.UPDATE:
            current[reference] = after
        else:
            current.pop(reference)


def _validate_typed_projection_integrity(connection: apsw.Connection) -> None:
    try:
        validate_typed_projections(connection)
    except ProjectionContractError as error:
        raise DatabaseIntegrityError(str(error)) from error


def _validate_document_runs(connection: apsw.Connection) -> None:
    for row in connection.execute(
        """
        SELECT document_run.run_id, document_run.document_kind,
               document_run.source_revision_id,
               document_run.source_revision_display_code,
               document_run.period_start, document_run.period_end,
               document_run.generated_at, document_run.actor_username,
               document_run.actor_sid,
               document_run.calculation_manifest_json,
               document_run.template_manifest_json,
               document_run.output_filename, document_run.output_sha256,
               document_run.output_byte_size,
               revision_display_code.display_code,
               revision_seal.revision_id
        FROM document_run
        LEFT JOIN revision_display_code
          ON revision_display_code.revision_id = document_run.source_revision_id
        LEFT JOIN revision_seal
          ON revision_seal.revision_id = document_run.source_revision_id
        ORDER BY document_run.sequence
        """
    ):
        if row[14] is None or row[15] is None:
            raise DatabaseIntegrityError(
                f"문서 run의 원본 revision이 유효하지 않습니다: {row[0]}"
            )
        if str(row[3]) != str(row[14]):
            raise DatabaseIntegrityError(
                f"문서 run의 원본 변경번호가 일치하지 않습니다: {row[0]}"
            )
        try:
            _document_run_from_row(row[:14])
        except (TypeError, ValueError) as error:
            raise DatabaseIntegrityError(
                f"문서 run 저장값이 올바르지 않습니다: {row[0]}"
            ) from error


def _document_run_from_row(row) -> DocumentRun:
    return DocumentRun(
        run_id=UUID(str(row[0])),
        kind=DocumentKind(str(row[1])),
        source_revision_id=UUID(str(row[2])),
        source_revision_display_code=str(row[3]),
        period_start=date.fromisoformat(str(row[4])),
        period_end=date.fromisoformat(str(row[5])),
        generated_at=datetime.fromisoformat(str(row[6])),
        actor=RevisionActor(str(row[7]), str(row[8])),
        calculation_manifest=json.loads(str(row[9])),
        template_manifest=json.loads(str(row[10])),
        output_filename=str(row[11]),
        output_sha256=str(row[12]),
        output_byte_size=int(row[13]),
    )


def _remove_incomplete_database(path: Path) -> None:
    for candidate in (path, Path(f"{path}-journal"), Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)
