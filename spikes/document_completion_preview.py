"""Render the verified document-completion docket with synthetic data."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from src.desktop_v2.document_hub import DocumentAction
from src.desktop_v2.document_completion import (
    DocumentCompletionDialog,
    GeneratedDocumentResult,
)
from src.desktop_v2.document_meal import DocumentMealSelection
from src.desktop_v2.document_output import (
    DocumentOutputMode,
    next_available_document_output,
)
from src.desktop_v2.document_period import DocumentPeriodSelection
from src.domain.roster import Meal
from src.domain.revisions import (
    RevisionActor,
    RevisionSource,
    RevisionSummary,
)


def main() -> int:
    destination = Path(
        sys.argv[1] if len(sys.argv) > 1 else "document-completion-preview.png"
    ).resolve()
    application = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix="meal-document-preview-") as directory:
        result = _sample_result(Path(directory))
        dialog = DocumentCompletionDialog(result, path_opener=lambda _path: True)
        dialog.show()
        application.processEvents()
        saved = dialog.grab().save(str(destination), "PNG")
        dialog.close()
    return 0 if saved else 1


def _sample_result(directory: Path) -> GeneratedDocumentResult:
    source = RevisionSummary(
        sequence=5,
        revision_id=UUID("5c000000-0000-0000-0000-000000000001"),
        display_code="20260831-005",
        recorded_at=datetime(2026, 8, 31, 9, 0, tzinfo=UTC),
        effective_on=date(2026, 8, 17),
        actor=RevisionActor("preview", "S-1-5-21-preview"),
        reason="완료 화면 미리보기",
        source=RevisionSource.UI,
    )
    period = DocumentPeriodSelection(
        DocumentAction.INVOICE,
        source,
        date(2026, 8, 17),
        Meal.LUNCH,
        date(2026, 8, 1),
        date(2026, 8, 31),
    )
    document = DocumentMealSelection(period, tuple(Meal))
    output = next_available_document_output(
        document,
        directory,
        DocumentOutputMode.SAVED_DEFAULT,
    )
    content = b"synthetic workbook bytes for visual preview"
    output.output_path.write_bytes(content)
    return GeneratedDocumentResult(
        output,
        datetime(2026, 8, 31, 15, 40, tzinfo=UTC),
        hashlib.sha256(content).hexdigest(),
        len(content),
    )


if __name__ == "__main__":
    raise SystemExit(main())
