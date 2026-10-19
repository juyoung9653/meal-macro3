"""Publish SQLite-v2 monthly documents and seal their document-run history."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from PySide6.QtWidgets import QMessageBox, QWidget

from src.core.settings import SettingsManager, get_settings
from src.domain.documents import (
    DocumentKind,
    DocumentRun,
)
from src.domain.document_templates import (
    DocumentTemplateProfile,
    require_document_template_setting,
)
from src.domain.revisions import RevisionActor
from src.domain.roster import RosterSnapshot
from src.domain.export_security import require_export_password_setting
from src.excel.publication import WorkbookPublicationResult
from src.generators.confirm_generator import make_confirm_publication
from src.generators.invoice_generator import make_invoice_publication
from src.generators.report_generator import make_report_publication
from src.services.sqlite_delivery_report_service import (
    prepare_delivery_report_data,
)
from src.services.document_column_mappings import (
    confirmation_default_mapping,
    effective_mapping,
    effective_fallbacks,
    invoice_default_mapping,
    load_mapping_overrides,
    load_mapping_fallbacks,
    mapping_setting_from_snapshot,
)
from src.services.sqlite_document_service import prepare_monthly_document_data
from src.storage.database_session import (
    AutomaticBackupResult,
    DatabaseSession,
)

from .document_completion import GeneratedDocumentResult
from .document_hub import DocumentAction
from .document_output import (
    DocumentOutputSelection,
    next_available_document_output,
)
from .document_template import (
    DocumentTemplateChangeStatus,
    DocumentTemplateController,
)
from .import_controller import current_windows_actor


DOCUMENT_RENDER_CONTRACT_VERSION = 1


class DocumentGenerationStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DocumentGenerationOutcome:
    status: DocumentGenerationStatus
    result: GeneratedDocumentResult | None = None
    roster: RosterSnapshot | None = None
    backup_result: AutomaticBackupResult | None = None

    @property
    def completed(self) -> bool:
        return self.status is DocumentGenerationStatus.COMPLETED


class DocumentGenerationController:
    """One guarded boundary from reviewed inputs to an immutable output run."""

    def __init__(
        self,
        session: DatabaseSession,
        *,
        template_controller_type=DocumentTemplateController,
        settings: SettingsManager | None = None,
        message_box=QMessageBox,
        actor_provider: Callable[[], RevisionActor] | None = None,
        generated_at_provider: Callable[[], datetime] | None = None,
        run_id_factory: Callable[[], UUID] = uuid4,
        invoice_writer=make_invoice_publication,
        confirmation_writer=make_confirm_publication,
        report_writer=make_report_publication,
    ) -> None:
        if not isinstance(session, DatabaseSession):
            raise TypeError("session은 DatabaseSession이어야 합니다.")
        self._session = session
        self._template_controller_type = template_controller_type
        self._settings = settings or get_settings()
        self._message_box = message_box
        self._actor_provider = actor_provider or current_windows_actor
        self._generated_at_provider = generated_at_provider or _utc_now
        self._run_id_factory = run_id_factory
        self._invoice_writer = invoice_writer
        self._confirmation_writer = confirmation_writer
        self._report_writer = report_writer

    def run(
        self,
        selection: DocumentOutputSelection,
        parent: QWidget | None = None,
    ) -> DocumentGenerationOutcome:
        if not isinstance(selection, DocumentOutputSelection):
            raise TypeError("selection은 DocumentOutputSelection이어야 합니다.")
        action = selection.document.period.action
        if action not in (
            DocumentAction.INVOICE,
            DocumentAction.CONFIRMATION,
            DocumentAction.DELIVERY_REPORT,
        ):
            self._message_box.information(
                parent,
                f"{action.label} 연결 준비 중",
                "현재 단계에서는 청구서, 확인서와 배송 리포트를 만들 수 있습니다.",
            )
            return DocumentGenerationOutcome(DocumentGenerationStatus.FAILED)

        if action in (DocumentAction.INVOICE, DocumentAction.CONFIRMATION):
            template_outcome = self._template_controller_type(
                self._session
            ).ensure_configured(parent)
            if not template_outcome.completed:
                status = (
                    DocumentGenerationStatus.CANCELLED
                    if template_outcome.status
                    is DocumentTemplateChangeStatus.CANCELLED
                    else DocumentGenerationStatus.FAILED
                )
                return DocumentGenerationOutcome(status)

        current_roster = self._session.database.current_roster()
        if current_roster is None:
            self._message_box.critical(
                parent,
                "문서를 만들지 못했습니다",
                "현재 명단을 찾을 수 없습니다.",
            )
            return DocumentGenerationOutcome(DocumentGenerationStatus.FAILED)
        publication: WorkbookPublicationResult | None = None
        refreshed: DocumentOutputSelection | None = None
        try:
            current_snapshot = self._session.database.snapshot(
                current_roster.revision_id
            )
            password_setting = require_export_password_setting(current_snapshot)
            template_setting = (
                require_document_template_setting(current_snapshot)
                if action in (DocumentAction.INVOICE, DocumentAction.CONFIRMATION)
                else None
            )

            period = selection.document.period
            source_snapshot = self._session.database.snapshot(
                period.source.revision_id
            )
            if action is DocumentAction.DELIVERY_REPORT:
                prepared = prepare_delivery_report_data(
                    source_snapshot,
                    period.starts_on,
                    period.ends_on,
                    selection.document.meals,
                )
                if not prepared.reports:
                    raise ValueError(
                        "선택한 기간·식종에 실제 배송할 대상이 없습니다."
                    )
            else:
                prepared = prepare_monthly_document_data(
                    source_snapshot,
                    period.starts_on,
                    period.ends_on,
                    selection.document.meals,
                )
                if not prepared.batch.deliveries:
                    raise ValueError(
                        "선택한 기간·식종에 청구하거나 확인할 급식 대상이 없습니다."
                    )

            refreshed = next_available_document_output(
                selection.document,
                selection.directory,
                selection.mode,
            )
            template_path = self._template_path(template_setting, action)
            if not template_path.is_file():
                raise FileNotFoundError(
                    f"문서 양식 파일을 찾을 수 없습니다: {template_path.name}"
                )
            if action is DocumentAction.INVOICE:
                assert template_setting is not None
                profile_key = template_setting.profile.value
                column_mapping = _invoice_column_mapping(
                    template_path,
                    profile_key,
                    prepared.batch.is_vacation_month,
                )
                mapping_setting = mapping_setting_from_snapshot(current_snapshot, profile_key, "invoice")
                column_mapping = effective_mapping(
                    column_mapping,
                    load_mapping_overrides(current_snapshot, profile_key, "invoice"),
                )
                column_fallbacks = effective_fallbacks(column_mapping, load_mapping_fallbacks(current_snapshot, profile_key, "invoice"), document_kind="invoice")
                _validate_custom_mapping_sources(column_mapping, current_roster)
                _validate_custom_mapping_sources(column_fallbacks, current_roster)
                _count, publication = self._invoice_writer(
                    prepared.batch,
                    column_mapping,
                    str(refreshed.output_path),
                    str(template_path),
                    meal_type=profile_key,
                    global_note=prepared.global_note,
                    output_password=password_setting.password,
                    **({"column_fallbacks": column_fallbacks} if column_fallbacks else {}),
                )
                kind = DocumentKind.INVOICE
                writer_name = "invoice_generator.make_invoice_publication"
            elif action is DocumentAction.CONFIRMATION:
                assert template_setting is not None
                profile_key = template_setting.profile.value
                column_mapping = _confirmation_column_mapping()
                mapping_setting = mapping_setting_from_snapshot(current_snapshot, profile_key, "confirmation")
                column_mapping = effective_mapping(
                    column_mapping,
                    load_mapping_overrides(current_snapshot, profile_key, "confirmation"),
                )
                column_fallbacks = effective_fallbacks(column_mapping, load_mapping_fallbacks(current_snapshot, profile_key, "confirmation"), document_kind="confirmation")
                _validate_custom_mapping_sources(column_mapping, current_roster)
                _validate_custom_mapping_sources(column_fallbacks, current_roster)
                _created, publication = self._confirmation_writer(
                    prepared.batch,
                    column_mapping,
                    refreshed.output_path,
                    str(template_path),
                    meal_type=profile_key,
                    output_password=password_setting.password,
                    **({"column_fallbacks": column_fallbacks} if column_fallbacks else {}),
                )
                kind = DocumentKind.CONFIRMATION
                writer_name = "confirm_generator.make_confirm_publication"
            else:
                column_mapping = {}
                column_fallbacks = {}
                mapping_setting = None
                publication = self._report_writer(
                    prepared.reports,
                    refreshed.output_path,
                    template_path,
                    output_password=password_setting.password,
                )
                kind = DocumentKind.DELIVERY_REPORT
                writer_name = "report_generator.make_report_publication"

            generated_at = self._generated_at_provider()
            if generated_at.tzinfo is None or generated_at.utcoffset() is None:
                raise ValueError("문서 생성 시각에는 시간대가 있어야 합니다.")
            template_manifest: dict[str, object] = {
                "render_contract_version": DOCUMENT_RENDER_CONTRACT_VERSION,
                "template_filename": template_path.name,
                "template_sha256": _file_sha256(template_path),
                "template_byte_size": template_path.stat().st_size,
                "writer": writer_name,
                "column_mapping": dict(sorted(column_mapping.items())),
                "column_fallbacks": dict(sorted(column_fallbacks.items())),
                "content_sha256": publication.content_sha256,
                "export_password": password_setting.public_manifest(),
            }
            if template_setting is not None:
                template_manifest[
                    "template_setting"
                ] = template_setting.public_manifest()
                template_manifest["column_mapping_setting"] = (
                    mapping_setting.public_manifest() if mapping_setting is not None else None
                )
            run = DocumentRun(
                run_id=self._run_id_factory(),
                kind=kind,
                source_revision_id=period.source.revision_id,
                source_revision_display_code=period.source.display_code,
                period_start=period.starts_on,
                period_end=period.ends_on,
                generated_at=generated_at,
                actor=self._actor_provider(),
                calculation_manifest=prepared.calculation_manifest,
                template_manifest=template_manifest,
                output_filename=refreshed.filename,
                output_sha256=publication.output_sha256,
                output_byte_size=publication.output_byte_size,
            )
            backup_result = self._session.append_document_run(run)
            result = GeneratedDocumentResult(
                refreshed,
                generated_at,
                publication.output_sha256,
                publication.output_byte_size,
            )
        except Exception as error:
            orphan_warning = ""
            if publication is not None and refreshed is not None:
                try:
                    refreshed.output_path.unlink(missing_ok=True)
                except OSError:
                    orphan_warning = (
                        "\n\n생성 이력을 저장하지 못했지만 파일 삭제도 실패했습니다. "
                        f"저장 폴더에서 {refreshed.filename} 파일을 확인해주세요."
                    )
            self._message_box.critical(
                parent,
                f"{action.label}를 만들지 못했습니다",
                "파일과 문서 이력을 완료하지 않았습니다. 입력 자료와 설정을 확인해주세요.\n\n"
                + str(error)
                + orphan_warning,
            )
            return DocumentGenerationOutcome(
                DocumentGenerationStatus.FAILED,
                roster=current_roster,
            )

        if backup_result.error is not None:
            self._message_box.warning(
                parent,
                f"{action.label}는 만들었지만 자동 백업에 실패했습니다",
                "파일과 문서 생성 이력은 정상 저장되었습니다. 백업 폴더를 확인해주세요.\n\n"
                + str(backup_result.error),
            )
        return DocumentGenerationOutcome(
            DocumentGenerationStatus.COMPLETED,
            result,
            current_roster,
            backup_result,
        )

    def _template_path(
        self,
        setting,
        action: DocumentAction,
    ) -> Path:
        if action is DocumentAction.DELIVERY_REPORT:
            return Path(self._settings.get_report_template_path()).resolve()
        if setting is None:
            raise ValueError("청구서·확인서 문서 양식 설정이 없습니다.")
        profile: DocumentTemplateProfile = setting.profile
        if action is DocumentAction.INVOICE:
            value = self._settings.get_invoice_template_path(profile.value)
        else:
            value = self._settings.get_confirm_template_path(profile.value)
        return Path(value).resolve()


def _invoice_column_mapping(
    template_path: Path,
    profile_key: str,
    is_vacation_month: bool,
) -> dict[str, str]:
    return invoice_default_mapping(template_path, profile_key, is_vacation_month)


def _confirmation_column_mapping() -> dict[str, str]:
    return confirmation_default_mapping()


def _validate_custom_mapping_sources(column_mapping: Mapping[str, str], roster: RosterSnapshot) -> None:
    known = {f"custom:{definition.definition_id}" for definition in roster.custom_fields}
    for target, source in column_mapping.items():
        if source.startswith("custom:") and source not in known:
            raise ValueError(f"{target} 칼럼에 연결된 사용자 정의 항목이 현재 명단에 없습니다. 설정에서 다시 연결해주세요.")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)
