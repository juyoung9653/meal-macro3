"""Ordered schema migrations for the encrypted SQLite Adapter."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        encoded = "\n-- statement --\n".join(self.statements).encode("utf-8")
        return sha256(encoded).hexdigest()


def _projection_guard_statements(table: str, revision_column: str) -> tuple[str, ...]:
    """Require typed projection writes to happen inside an open revision."""

    return (
        f"""
        CREATE TRIGGER {table}_guard_insert
        BEFORE INSERT ON {table}
        WHEN NOT EXISTS (
            SELECT 1
            FROM projection_write_context
            WHERE revision_id = NEW.{revision_column}
        )
        BEGIN
            SELECT RAISE(ABORT, 'projection write requires an open revision');
        END
        """,
        f"""
        CREATE TRIGGER {table}_guard_update
        BEFORE UPDATE ON {table}
        WHEN NOT EXISTS (
            SELECT 1
            FROM projection_write_context
            WHERE revision_id = NEW.{revision_column}
        )
        BEGIN
            SELECT RAISE(ABORT, 'projection write requires an open revision');
        END
        """,
        f"""
        CREATE TRIGGER {table}_guard_delete
        BEFORE DELETE ON {table}
        WHEN NOT EXISTS (SELECT 1 FROM projection_write_context)
        BEGIN
            SELECT RAISE(ABORT, 'projection write requires an open revision');
        END
        """,
    )


MIGRATIONS = (
    Migration(
        version=1,
        name="initial_revision_ledger",
        statements=(
            """
            CREATE TABLE schema_migration (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                checksum TEXT NOT NULL CHECK(length(checksum) = 64),
                applied_at TEXT NOT NULL
            ) STRICT
            """,
            """
            CREATE TABLE revision (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                revision_id TEXT NOT NULL UNIQUE CHECK(length(revision_id) = 36),
                parent_revision_id TEXT UNIQUE
                    REFERENCES revision(revision_id),
                recorded_at TEXT NOT NULL,
                effective_on TEXT,
                actor_username TEXT NOT NULL CHECK(length(trim(actor_username)) > 0),
                actor_sid TEXT NOT NULL CHECK(length(trim(actor_sid)) > 0),
                reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
                source TEXT NOT NULL CHECK(source IN ('ui', 'import', 'system')),
                decisions_json TEXT NOT NULL
                    CHECK(json_valid(decisions_json) AND json_type(decisions_json) = 'object'),
                rule_versions_json TEXT NOT NULL
                    CHECK(
                        json_valid(rule_versions_json)
                        AND json_type(rule_versions_json) = 'object'
                    )
            ) STRICT
            """,
            """
            CREATE TABLE audit_change (
                revision_id TEXT NOT NULL REFERENCES revision(revision_id),
                ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
                entity_kind TEXT NOT NULL CHECK(length(trim(entity_kind)) > 0),
                entity_id TEXT NOT NULL CHECK(length(entity_id) = 36),
                operation TEXT NOT NULL
                    CHECK(operation IN ('insert', 'update', 'delete')),
                before_json TEXT CHECK(
                    before_json IS NULL
                    OR (json_valid(before_json) AND json_type(before_json) = 'object')
                ),
                after_json TEXT CHECK(
                    after_json IS NULL
                    OR (json_valid(after_json) AND json_type(after_json) = 'object')
                ),
                PRIMARY KEY (revision_id, ordinal),
                CHECK(
                    (operation = 'insert' AND before_json IS NULL AND after_json IS NOT NULL)
                    OR (
                        operation = 'update'
                        AND before_json IS NOT NULL
                        AND after_json IS NOT NULL
                    )
                    OR (operation = 'delete' AND before_json IS NOT NULL AND after_json IS NULL)
                )
            ) STRICT
            """,
            """
            CREATE INDEX audit_change_entity_history
            ON audit_change(entity_kind, entity_id, revision_id)
            """,
            """
            CREATE UNIQUE INDEX audit_change_once_per_revision
            ON audit_change(revision_id, entity_kind, entity_id)
            """,
            """
            CREATE TABLE import_batch (
                batch_id TEXT PRIMARY KEY CHECK(length(batch_id) = 36),
                revision_id TEXT NOT NULL UNIQUE REFERENCES revision(revision_id),
                source_filename TEXT NOT NULL CHECK(
                    length(trim(source_filename)) > 0
                    AND instr(source_filename, '/') = 0
                    AND instr(source_filename, char(92)) = 0
                ),
                sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
                byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
                sheet_name TEXT,
                header_fingerprint TEXT NOT NULL
                    CHECK(length(trim(header_fingerprint)) > 0),
                mapping_manifest_json TEXT NOT NULL
                    CHECK(
                        json_valid(mapping_manifest_json)
                        AND json_type(mapping_manifest_json) = 'object'
                    ),
                effective_on TEXT NOT NULL,
                decisions_json TEXT NOT NULL
                    CHECK(json_valid(decisions_json) AND json_type(decisions_json) = 'object')
            ) STRICT
            """,
            """
            CREATE TABLE revision_seal (
                revision_id TEXT PRIMARY KEY REFERENCES revision(revision_id),
                sealed_at TEXT NOT NULL,
                change_count INTEGER NOT NULL CHECK(change_count >= 1),
                has_import_batch INTEGER NOT NULL CHECK(has_import_batch IN (0, 1))
            ) STRICT
            """,
            """
            CREATE TRIGGER revision_linear_insert
            BEFORE INSERT ON revision
            BEGIN
                SELECT CASE
                    WHEN (SELECT COUNT(*) FROM revision) = 0
                         AND NEW.parent_revision_id IS NOT NULL
                    THEN RAISE(ABORT, 'first revision cannot have a parent')
                    WHEN (SELECT COUNT(*) FROM revision) > 0
                         AND NEW.parent_revision_id IS NOT (
                             SELECT revision_id
                             FROM revision
                             ORDER BY sequence DESC
                             LIMIT 1
                         )
                    THEN RAISE(ABORT, 'revision parent must be the latest revision')
                    WHEN (SELECT COUNT(*) FROM revision) > 0
                         AND NOT EXISTS (
                             SELECT 1
                             FROM revision_seal
                             WHERE revision_id = (
                                 SELECT revision_id
                                 FROM revision
                                 ORDER BY sequence DESC
                                 LIMIT 1
                             )
                         )
                    THEN RAISE(ABORT, 'latest revision must be sealed')
                END;
            END
            """,
            """
            CREATE TRIGGER revision_seal_validate
            BEFORE INSERT ON revision_seal
            BEGIN
                SELECT CASE
                    WHEN NEW.change_count != (
                        SELECT COUNT(*)
                        FROM audit_change
                        WHERE revision_id = NEW.revision_id
                    )
                    THEN RAISE(ABORT, 'revision seal change count mismatch')
                    WHEN NEW.has_import_batch != EXISTS (
                        SELECT 1
                        FROM import_batch
                        WHERE revision_id = NEW.revision_id
                    )
                    THEN RAISE(ABORT, 'revision seal import flag mismatch')
                END;
            END
            """,
            """
            CREATE TRIGGER audit_change_no_insert_after_seal
            BEFORE INSERT ON audit_change
            WHEN EXISTS (
                SELECT 1 FROM revision_seal WHERE revision_id = NEW.revision_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'sealed revision cannot accept audit changes');
            END
            """,
            """
            CREATE TRIGGER import_batch_no_insert_after_seal
            BEFORE INSERT ON import_batch
            WHEN EXISTS (
                SELECT 1 FROM revision_seal WHERE revision_id = NEW.revision_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'sealed revision cannot accept an import batch');
            END
            """,
            """
            CREATE TRIGGER schema_migration_no_update
            BEFORE UPDATE ON schema_migration
            BEGIN
                SELECT RAISE(ABORT, 'schema migration rows are immutable');
            END
            """,
            """
            CREATE TRIGGER schema_migration_no_delete
            BEFORE DELETE ON schema_migration
            BEGIN
                SELECT RAISE(ABORT, 'schema migration rows are immutable');
            END
            """,
            """
            CREATE TRIGGER revision_no_update
            BEFORE UPDATE ON revision
            BEGIN
                SELECT RAISE(ABORT, 'revision rows are immutable');
            END
            """,
            """
            CREATE TRIGGER revision_no_delete
            BEFORE DELETE ON revision
            BEGIN
                SELECT RAISE(ABORT, 'revision rows are immutable');
            END
            """,
            """
            CREATE TRIGGER audit_change_no_update
            BEFORE UPDATE ON audit_change
            BEGIN
                SELECT RAISE(ABORT, 'audit change rows are immutable');
            END
            """,
            """
            CREATE TRIGGER audit_change_no_delete
            BEFORE DELETE ON audit_change
            BEGIN
                SELECT RAISE(ABORT, 'audit change rows are immutable');
            END
            """,
            """
            CREATE TRIGGER import_batch_no_update
            BEFORE UPDATE ON import_batch
            BEGIN
                SELECT RAISE(ABORT, 'import batch rows are immutable');
            END
            """,
            """
            CREATE TRIGGER import_batch_no_delete
            BEFORE DELETE ON import_batch
            BEGIN
                SELECT RAISE(ABORT, 'import batch rows are immutable');
            END
            """,
            """
            CREATE TRIGGER revision_seal_no_update
            BEFORE UPDATE ON revision_seal
            BEGIN
                SELECT RAISE(ABORT, 'revision seals are immutable');
            END
            """,
            """
            CREATE TRIGGER revision_seal_no_delete
            BEFORE DELETE ON revision_seal
            BEGIN
                SELECT RAISE(ABORT, 'revision seals are immutable');
            END
            """,
        ),
    ),
    Migration(
        version=2,
        name="typed_roster_projection",
        statements=(
            """
            CREATE TABLE projection_write_context (
                revision_id TEXT PRIMARY KEY REFERENCES revision(revision_id)
            ) STRICT
            """,
            """
            CREATE TRIGGER projection_write_context_validate_insert
            BEFORE INSERT ON projection_write_context
            BEGIN
                SELECT CASE
                    WHEN EXISTS (SELECT 1 FROM projection_write_context)
                    THEN RAISE(ABORT, 'projection write context is already open')
                    WHEN NOT EXISTS (
                        SELECT 1
                        FROM revision
                        WHERE revision_id = NEW.revision_id
                    )
                    THEN RAISE(ABORT, 'projection revision does not exist')
                    WHEN EXISTS (
                        SELECT 1
                        FROM revision_seal
                        WHERE revision_id = NEW.revision_id
                    )
                    THEN RAISE(ABORT, 'sealed revision cannot project changes')
                END;
            END
            """,
            """
            CREATE TRIGGER projection_write_context_no_update
            BEFORE UPDATE ON projection_write_context
            BEGIN
                SELECT RAISE(ABORT, 'projection write context cannot be updated');
            END
            """,
            """
            CREATE TRIGGER projection_write_context_validate_delete
            BEFORE DELETE ON projection_write_context
            WHEN EXISTS (
                SELECT 1
                FROM revision_seal
                WHERE revision_id = OLD.revision_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'sealed projection context cannot be closed');
            END
            """,
            """
            CREATE TRIGGER revision_seal_requires_closed_projection
            BEFORE INSERT ON revision_seal
            WHEN EXISTS (
                SELECT 1
                FROM projection_write_context
                WHERE revision_id = NEW.revision_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'projection context must close before sealing');
            END
            """,
            """
            CREATE TABLE custom_field_definition_projection (
                definition_id TEXT PRIMARY KEY CHECK(length(definition_id) = 36),
                last_revision_id TEXT NOT NULL REFERENCES revision(revision_id),
                name TEXT NOT NULL COLLATE NOCASE
                    UNIQUE CHECK(length(trim(name)) > 0),
                field_type TEXT NOT NULL
                    CHECK(field_type IN ('text', 'number', 'date', 'choice')),
                display_order INTEGER NOT NULL UNIQUE CHECK(display_order >= 0),
                choices_json TEXT NOT NULL CHECK(
                    json_valid(choices_json)
                    AND json_type(choices_json) = 'array'
                    AND (
                        (field_type = 'choice' AND json_array_length(choices_json) > 0)
                        OR (field_type != 'choice' AND json_array_length(choices_json) = 0)
                    )
                )
            ) STRICT
            """,
            """
            CREATE TABLE person_projection (
                person_id TEXT PRIMARY KEY CHECK(length(person_id) = 36),
                last_revision_id TEXT NOT NULL REFERENCES revision(revision_id),
                name TEXT NOT NULL CHECK(length(trim(name)) > 0),
                birth_date TEXT CHECK(
                    birth_date IS NULL OR date(birth_date) = birth_date
                ),
                school_type TEXT,
                school_name TEXT,
                grade TEXT,
                class_name TEXT,
                household_head_name TEXT,
                relationship_to_child TEXT,
                jurisdiction TEXT,
                provider TEXT,
                road_address TEXT,
                detail_address TEXT,
                telephone TEXT,
                mobile_phone TEXT,
                note TEXT,
                custom_values_json TEXT NOT NULL CHECK(
                    json_valid(custom_values_json)
                    AND json_type(custom_values_json) = 'object'
                )
            ) STRICT
            """,
            """
            CREATE TABLE household_projection (
                household_id TEXT PRIMARY KEY CHECK(length(household_id) = 36),
                last_revision_id TEXT NOT NULL REFERENCES revision(revision_id)
            ) STRICT
            """,
            """
            CREATE TABLE household_membership_projection (
                person_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
                household_id TEXT NOT NULL,
                starts_on TEXT CHECK(
                    starts_on IS NULL OR date(starts_on) = starts_on
                ),
                ends_on TEXT CHECK(ends_on IS NULL OR date(ends_on) = ends_on),
                last_revision_id TEXT NOT NULL REFERENCES revision(revision_id),
                PRIMARY KEY(person_id, ordinal),
                FOREIGN KEY(person_id) REFERENCES person_projection(person_id)
                    DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY(household_id)
                    REFERENCES household_projection(household_id)
                    DEFERRABLE INITIALLY DEFERRED,
                CHECK(starts_on IS NULL OR ends_on IS NULL OR starts_on <= ends_on)
            ) STRICT
            """,
            """
            CREATE INDEX household_membership_by_household
            ON household_membership_projection(household_id, starts_on, ends_on)
            """,
            """
            CREATE TABLE meal_enrollment_projection (
                person_id TEXT NOT NULL,
                meal TEXT NOT NULL CHECK(meal IN ('조식', '중식', '석식')),
                ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
                starts_on TEXT CHECK(
                    starts_on IS NULL OR date(starts_on) = starts_on
                ),
                ends_on TEXT CHECK(ends_on IS NULL OR date(ends_on) = ends_on),
                last_revision_id TEXT NOT NULL REFERENCES revision(revision_id),
                PRIMARY KEY(person_id, meal, ordinal),
                FOREIGN KEY(person_id) REFERENCES person_projection(person_id)
                    DEFERRABLE INITIALLY DEFERRED,
                CHECK(starts_on IS NULL OR ends_on IS NULL OR starts_on <= ends_on)
            ) STRICT
            """,
            """
            CREATE TABLE delivery_assignment_projection (
                person_id TEXT NOT NULL,
                meal TEXT NOT NULL CHECK(meal IN ('', '조식', '중식', '석식')),
                driver TEXT,
                sequence TEXT,
                last_revision_id TEXT NOT NULL REFERENCES revision(revision_id),
                PRIMARY KEY(person_id, meal),
                FOREIGN KEY(person_id) REFERENCES person_projection(person_id)
                    DEFERRABLE INITIALLY DEFERRED,
                CHECK(driver IS NOT NULL OR sequence IS NOT NULL)
            ) STRICT
            """,
            """
            CREATE TABLE status_event_projection (
                event_id TEXT PRIMARY KEY CHECK(length(event_id) = 36),
                created_revision_id TEXT NOT NULL REFERENCES revision(revision_id),
                person_id TEXT NOT NULL,
                meal TEXT NOT NULL CHECK(meal IN ('조식', '중식', '석식')),
                status TEXT NOT NULL CHECK(status IN ('배송', '일시중지', '중지')),
                starts_on TEXT CHECK(
                    starts_on IS NULL OR date(starts_on) = starts_on
                ),
                ends_on TEXT CHECK(ends_on IS NULL OR date(ends_on) = ends_on),
                recorded_at TEXT NOT NULL,
                source_text TEXT,
                CHECK(starts_on IS NOT NULL OR ends_on IS NOT NULL),
                CHECK(starts_on IS NULL OR ends_on IS NULL OR starts_on <= ends_on),
                CHECK(status != '일시중지' OR (starts_on IS NOT NULL AND ends_on IS NOT NULL))
            ) STRICT
            """,
            """
            CREATE INDEX status_event_by_person_meal_date
            ON status_event_projection(person_id, meal, starts_on, ends_on)
            """,
            *_projection_guard_statements(
                "custom_field_definition_projection",
                "last_revision_id",
            ),
            *_projection_guard_statements(
                "person_projection",
                "last_revision_id",
            ),
            *_projection_guard_statements(
                "household_projection",
                "last_revision_id",
            ),
            *_projection_guard_statements(
                "household_membership_projection",
                "last_revision_id",
            ),
            *_projection_guard_statements(
                "meal_enrollment_projection",
                "last_revision_id",
            ),
            *_projection_guard_statements(
                "delivery_assignment_projection",
                "last_revision_id",
            ),
            *_projection_guard_statements(
                "status_event_projection",
                "created_revision_id",
            ),
        ),
    ),
    Migration(
        version=3,
        name="stable_revision_display_code",
        statements=(
            """
            CREATE TABLE revision_display_code (
                revision_id TEXT PRIMARY KEY REFERENCES revision(revision_id),
                recorded_local_date TEXT NOT NULL CHECK(
                    date(recorded_local_date) = recorded_local_date
                ),
                daily_ordinal INTEGER NOT NULL CHECK(daily_ordinal >= 1),
                display_code TEXT NOT NULL UNIQUE CHECK(
                    display_code = replace(recorded_local_date, '-', '')
                                   || '-'
                                   || printf('%03d', daily_ordinal)
                ),
                UNIQUE(recorded_local_date, daily_ordinal)
            ) STRICT
            """,
            """
            WITH ranked AS (
                SELECT revision_id,
                       date(recorded_at, '+9 hours') AS recorded_local_date,
                       row_number() OVER (
                           PARTITION BY date(recorded_at, '+9 hours')
                           ORDER BY sequence
                       ) AS daily_ordinal
                FROM revision
            )
            INSERT INTO revision_display_code(
                revision_id, recorded_local_date, daily_ordinal, display_code
            )
            SELECT revision_id,
                   recorded_local_date,
                   daily_ordinal,
                   replace(recorded_local_date, '-', '')
                       || '-'
                       || printf('%03d', daily_ordinal)
            FROM ranked
            """,
            """
            CREATE TRIGGER revision_display_code_no_insert_after_seal
            BEFORE INSERT ON revision_display_code
            WHEN EXISTS (
                SELECT 1
                FROM revision_seal
                WHERE revision_id = NEW.revision_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'sealed revision cannot accept a display code');
            END
            """,
            """
            CREATE TRIGGER revision_display_code_no_update
            BEFORE UPDATE ON revision_display_code
            BEGIN
                SELECT RAISE(ABORT, 'revision display codes are immutable');
            END
            """,
            """
            CREATE TRIGGER revision_display_code_no_delete
            BEFORE DELETE ON revision_display_code
            BEGIN
                SELECT RAISE(ABORT, 'revision display codes are immutable');
            END
            """,
            """
            CREATE TRIGGER revision_seal_requires_display_code
            BEFORE INSERT ON revision_seal
            WHEN NOT EXISTS (
                SELECT 1
                FROM revision_display_code
                WHERE revision_id = NEW.revision_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'revision display code is required before seal');
            END
            """,
        ),
    ),
    Migration(
        version=4,
        name="append_only_document_run",
        statements=(
            """
            CREATE TABLE document_run (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL UNIQUE CHECK(length(run_id) = 36),
                document_kind TEXT NOT NULL CHECK(
                    document_kind IN ('invoice', 'confirmation')
                ),
                source_revision_id TEXT NOT NULL REFERENCES revision(revision_id),
                source_revision_display_code TEXT NOT NULL CHECK(
                    length(source_revision_display_code) >= 12
                ),
                period_start TEXT NOT NULL CHECK(date(period_start) = period_start),
                period_end TEXT NOT NULL CHECK(date(period_end) = period_end),
                generated_at TEXT NOT NULL,
                actor_username TEXT NOT NULL CHECK(length(trim(actor_username)) > 0),
                actor_sid TEXT NOT NULL CHECK(length(trim(actor_sid)) > 0),
                calculation_manifest_json TEXT NOT NULL CHECK(
                    json_valid(calculation_manifest_json)
                    AND json_type(calculation_manifest_json) = 'object'
                ),
                template_manifest_json TEXT NOT NULL CHECK(
                    json_valid(template_manifest_json)
                    AND json_type(template_manifest_json) = 'object'
                ),
                output_filename TEXT NOT NULL CHECK(
                    length(trim(output_filename)) > 0
                    AND instr(output_filename, '/') = 0
                    AND instr(output_filename, char(92)) = 0
                ),
                output_sha256 TEXT NOT NULL CHECK(length(output_sha256) = 64),
                output_byte_size INTEGER NOT NULL CHECK(output_byte_size >= 1),
                CHECK(period_start <= period_end)
            ) STRICT
            """,
            """
            CREATE INDEX document_run_by_source_revision
            ON document_run(source_revision_id, sequence)
            """,
            """
            CREATE TRIGGER document_run_validate_insert
            BEFORE INSERT ON document_run
            BEGIN
                SELECT CASE
                    WHEN NOT EXISTS (
                        SELECT 1
                        FROM revision_seal
                        WHERE revision_id = NEW.source_revision_id
                    )
                    THEN RAISE(ABORT, 'document source revision must be sealed')
                    WHEN NEW.source_revision_display_code IS NOT (
                        SELECT display_code
                        FROM revision_display_code
                        WHERE revision_id = NEW.source_revision_id
                    )
                    THEN RAISE(ABORT, 'document source display code mismatch')
                END;
            END
            """,
            """
            CREATE TRIGGER document_run_no_update
            BEFORE UPDATE ON document_run
            BEGIN
                SELECT RAISE(ABORT, 'document runs are immutable');
            END
            """,
            """
            CREATE TRIGGER document_run_no_delete
            BEFORE DELETE ON document_run
            BEGIN
                SELECT RAISE(ABORT, 'document runs are immutable');
            END
            """,
        ),
    ),
    Migration(
        version=5,
        name="delivery_report_document_run",
        statements=(
            "DROP TRIGGER document_run_validate_insert",
            "DROP TRIGGER document_run_no_update",
            "DROP TRIGGER document_run_no_delete",
            "ALTER TABLE document_run RENAME TO document_run_schema4",
            """
            CREATE TABLE document_run (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL UNIQUE CHECK(length(run_id) = 36),
                document_kind TEXT NOT NULL CHECK(
                    document_kind IN (
                        'invoice', 'confirmation', 'delivery_report'
                    )
                ),
                source_revision_id TEXT NOT NULL REFERENCES revision(revision_id),
                source_revision_display_code TEXT NOT NULL CHECK(
                    length(source_revision_display_code) >= 12
                ),
                period_start TEXT NOT NULL CHECK(date(period_start) = period_start),
                period_end TEXT NOT NULL CHECK(date(period_end) = period_end),
                generated_at TEXT NOT NULL,
                actor_username TEXT NOT NULL CHECK(length(trim(actor_username)) > 0),
                actor_sid TEXT NOT NULL CHECK(length(trim(actor_sid)) > 0),
                calculation_manifest_json TEXT NOT NULL CHECK(
                    json_valid(calculation_manifest_json)
                    AND json_type(calculation_manifest_json) = 'object'
                ),
                template_manifest_json TEXT NOT NULL CHECK(
                    json_valid(template_manifest_json)
                    AND json_type(template_manifest_json) = 'object'
                ),
                output_filename TEXT NOT NULL CHECK(
                    length(trim(output_filename)) > 0
                    AND instr(output_filename, '/') = 0
                    AND instr(output_filename, char(92)) = 0
                ),
                output_sha256 TEXT NOT NULL CHECK(length(output_sha256) = 64),
                output_byte_size INTEGER NOT NULL CHECK(output_byte_size >= 1),
                CHECK(period_start <= period_end)
            ) STRICT
            """,
            """
            INSERT INTO document_run(
                sequence, run_id, document_kind, source_revision_id,
                source_revision_display_code, period_start, period_end,
                generated_at, actor_username, actor_sid,
                calculation_manifest_json, template_manifest_json,
                output_filename, output_sha256, output_byte_size
            )
            SELECT sequence, run_id, document_kind, source_revision_id,
                   source_revision_display_code, period_start, period_end,
                   generated_at, actor_username, actor_sid,
                   calculation_manifest_json, template_manifest_json,
                   output_filename, output_sha256, output_byte_size
            FROM document_run_schema4
            ORDER BY sequence
            """,
            "DROP TABLE document_run_schema4",
            """
            CREATE INDEX document_run_by_source_revision
            ON document_run(source_revision_id, sequence)
            """,
            """
            CREATE TRIGGER document_run_validate_insert
            BEFORE INSERT ON document_run
            BEGIN
                SELECT CASE
                    WHEN NOT EXISTS (
                        SELECT 1
                        FROM revision_seal
                        WHERE revision_id = NEW.source_revision_id
                    )
                    THEN RAISE(ABORT, 'document source revision must be sealed')
                    WHEN NEW.source_revision_display_code IS NOT (
                        SELECT display_code
                        FROM revision_display_code
                        WHERE revision_id = NEW.source_revision_id
                    )
                    THEN RAISE(ABORT, 'document source display code mismatch')
                END;
            END
            """,
            """
            CREATE TRIGGER document_run_no_update
            BEFORE UPDATE ON document_run
            BEGIN
                SELECT RAISE(ABORT, 'document runs are immutable');
            END
            """,
            """
            CREATE TRIGGER document_run_no_delete
            BEFORE DELETE ON document_run
            BEGIN
                SELECT RAISE(ABORT, 'document runs are immutable');
            END
            """,
        ),
    ),
    Migration(
        version=6,
        name="household_delivery_settings",
        statements=(
            "ALTER TABLE household_projection ADD COLUMN delivery_settings_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(delivery_settings_json) AND json_type(delivery_settings_json) = 'array')",
        ),
    ),
    Migration(
        version=7,
        name="person_delivery_settings",
        statements=(
            "ALTER TABLE person_projection ADD COLUMN delivery_settings_json TEXT CHECK(delivery_settings_json IS NULL OR (json_valid(delivery_settings_json) AND json_type(delivery_settings_json) = 'array'))",
        ),
    ),
)

CURRENT_SCHEMA_VERSION = MIGRATIONS[-1].version
