"""Verified document-generation result and explicit post-generation actions."""

from __future__ import annotations

from .user_messages import user_error

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.revisions import format_revision_timestamp

from .document_output import DocumentOutputSelection
from .theme import application_stylesheet, register_application_fonts
from .startup_loading import run_progress_task


PathOpener = Callable[[Path], bool]
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class GeneratedDocumentResult:
    """A published file whose current bytes match the generation result."""

    output: DocumentOutputSelection
    generated_at: datetime
    output_sha256: str
    output_byte_size: int

    def __post_init__(self) -> None:
        if not isinstance(self.output, DocumentOutputSelection):
            raise TypeError("문서 출력 선택이 올바르지 않습니다.")
        if not isinstance(self.generated_at, datetime):
            raise TypeError("문서 생성 시각은 datetime이어야 합니다.")
        if (
            self.generated_at.tzinfo is None
            or self.generated_at.utcoffset() is None
        ):
            raise ValueError("문서 생성 시각에는 시간대가 있어야 합니다.")
        if not isinstance(self.output_sha256, str) or not _SHA256_PATTERN.fullmatch(
            self.output_sha256
        ):
            raise ValueError("문서 파일 SHA-256이 올바르지 않습니다.")
        if isinstance(self.output_byte_size, bool) or not isinstance(
            self.output_byte_size,
            int,
        ):
            raise TypeError("문서 파일 크기는 정수여야 합니다.")
        if self.output_byte_size < 1:
            raise ValueError("빈 문서는 생성 완료로 처리할 수 없습니다.")
        self.verify_file()

    @property
    def output_path(self) -> Path:
        return self.output.output_path

    def verify_file(self) -> Path:
        """Recheck the published file before presenting or opening it."""

        path = self.output_path
        try:
            if not path.is_file():
                raise ValueError("생성한 문서 파일을 찾을 수 없습니다.")
            actual_size = path.stat().st_size
            if actual_size != self.output_byte_size:
                raise ValueError(
                    "생성한 문서 파일 크기가 완료 기록과 다릅니다."
                )
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        except ValueError:
            raise
        except OSError as error:
            raise ValueError(
                "생성한 문서 파일을 확인할 수 없습니다."
            ) from error
        if digest.hexdigest() != self.output_sha256:
            raise ValueError(
                "생성한 문서 내용이 완료 기록과 다릅니다."
            )
        return path


class DocumentCompletionDialog(QDialog):
    """Show a verified dispatch docket without opening anything automatically."""

    def __init__(
        self,
        result: GeneratedDocumentResult,
        parent: QWidget | None = None,
        *,
        path_opener: PathOpener | None = None,
    ) -> None:
        if not isinstance(result, GeneratedDocumentResult):
            raise TypeError("문서 생성 결과가 올바르지 않습니다.")
        run_progress_task(
            result.verify_file, title="문서 파일을 확인하는 중입니다",
            message="저장된 문서의 크기와 내용을 확인하는 중입니다.", parent=parent,
        )
        super().__init__(parent)
        self._result = result
        self._path_opener = path_opener or _open_local_path

        register_application_fonts()
        self.setObjectName("documentCompletionDialog")
        self.setWindowTitle(f"{result.output.document.period.action.label} 발행 완료")
        self.setModal(True)
        self.resize(820, 590)
        self.setMinimumSize(720, 540)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()

    @property
    def result(self) -> GeneratedDocumentResult:
        return self._result

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 19)
        root.setSpacing(13)
        root.addWidget(self._build_header())
        root.addWidget(self._build_dispatch_ticket(), 1)

        notice = QLabel(
            "파일은 자동으로 열지 않았습니다. 필요할 때 아래 버튼을 누르세요."
        )
        notice.setObjectName("documentCompletionNotice")
        notice.setWordWrap(True)
        root.addWidget(notice)

        self._status = QLabel()
        self._status.setObjectName("documentCompletionStatus")
        self._status.setWordWrap(True)
        self._status.hide()
        root.addWidget(self._status)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self._open_file_button = QPushButton("파일 열기")
        self._open_file_button.setObjectName("primaryAction")
        self._open_file_button.clicked.connect(self._open_file)
        actions.addWidget(self._open_file_button)

        self._open_folder_button = QPushButton("폴더 열기")
        self._open_folder_button.setObjectName("quietAction")
        self._open_folder_button.clicked.connect(self._open_folder)
        actions.addWidget(self._open_folder_button)
        actions.addStretch(1)

        close_button = QPushButton("닫기")
        close_button.setObjectName("quietAction")
        close_button.clicked.connect(self.reject)
        actions.addWidget(close_button)
        root.addLayout(actions)

    def _build_header(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("documentCompletionHeader")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 15, 18, 16)
        layout.setSpacing(4)

        eyebrow = QLabel("문서 생성 완료")
        eyebrow.setObjectName("editEyebrow")
        title = QLabel("문서를 정상적으로 만들었습니다")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel(
            "저장된 파일의 크기와 내용을 확인했습니다. 아래에서 필요한 동작을 고르세요."
        )
        subtitle.setObjectName("workspaceSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(eyebrow)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        return frame

    def _build_dispatch_ticket(self) -> QFrame:
        document = self._result.output.document
        period = document.period
        frame = QFrame()
        frame.setObjectName("documentCompletionTicket")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(17, 17, 17, 17)
        layout.setSpacing(16)

        stamp = QFrame()
        stamp.setObjectName("documentCompletionStamp")
        stamp.setFixedWidth(105)
        stamp_layout = QVBoxLayout(stamp)
        stamp_layout.setContentsMargins(8, 18, 8, 18)
        stamp_layout.setSpacing(2)
        stamp_layout.addStretch(1)
        stamp_top = QLabel("발행")
        stamp_top.setObjectName("documentCompletionStampTop")
        stamp_top.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stamp_main = QLabel("완료")
        stamp_main.setObjectName("documentCompletionStampMain")
        stamp_main.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stamp_layout.addWidget(stamp_top)
        stamp_layout.addWidget(stamp_main)
        stamp_layout.addStretch(1)
        layout.addWidget(stamp)

        docket = QVBoxLayout()
        docket.setSpacing(7)
        kind = QLabel(
            f"{period.action.label} · {_period_text(period.starts_on, period.ends_on)}"
        )
        kind.setObjectName("documentCompletionKind")
        docket.addWidget(kind)

        filename = QLabel(self._result.output.filename)
        filename.setObjectName("documentCompletionFilename")
        filename.setWordWrap(True)
        filename.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        docket.addWidget(filename)

        meals = " / ".join(meal.value for meal in document.meals)
        context = QLabel(
            f"식종  {meals}\n"
            f"원본 변경번호  {format_revision_timestamp(period.source.recorded_at)}\n"
            f"생성 시각  {_generated_at_text(self._result.generated_at)}"
        )
        context.setObjectName("documentCompletionContext")
        context.setWordWrap(True)
        docket.addWidget(context)

        verification = QLabel(
            f"파일 확인 완료 · {_file_size_text(self._result.output_byte_size)}"
        )
        verification.setObjectName("documentCompletionVerification")
        docket.addWidget(verification)

        shelf = QFrame()
        shelf.setObjectName("documentCompletionShelf")
        shelf_layout = QVBoxLayout(shelf)
        shelf_layout.setContentsMargins(12, 9, 12, 10)
        shelf_layout.setSpacing(3)
        shelf_label = QLabel("저장 폴더")
        shelf_label.setObjectName("sectionLabel")
        shelf_path = QLabel(str(self._result.output.directory))
        shelf_path.setObjectName("documentCompletionPath")
        shelf_path.setWordWrap(True)
        shelf_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        shelf_layout.addWidget(shelf_label)
        shelf_layout.addWidget(shelf_path)
        docket.addWidget(shelf)
        docket.addStretch(1)
        layout.addLayout(docket, 1)
        return frame

    def _open_file(self) -> None:
        try:
            path = run_progress_task(
                self._result.verify_file, title="문서 파일을 확인하는 중입니다",
                message="문서를 열기 전에 저장된 내용을 확인하는 중입니다.", parent=self,
            )
        except (TypeError, ValueError, OSError) as error:
            self._show_status(user_error(error), outcome="error")
            return
        self._request_open(
            path,
            failure=(
                "파일 열기 요청을 처리하지 못했습니다. Excel 연결 상태를 확인하거나 "
                "저장 폴더에서 직접 열어주세요."
            ),
            success=(
                "파일 열기를 요청했습니다. 창이 보이지 않으면 작업 표시줄을 확인해주세요."
            ),
        )

    def _open_folder(self) -> None:
        directory = self._result.output.directory
        if not run_progress_task(
            directory.is_dir, title="저장 폴더를 확인하는 중입니다",
            message="문서가 저장된 폴더에 연결하는 중입니다.", parent=self,
        ):
            self._show_status(
                "저장 폴더를 찾을 수 없습니다. 폴더가 이동되었는지 확인해주세요.",
                outcome="error",
            )
            return
        self._request_open(
            directory,
            failure="저장 폴더를 열지 못했습니다. 폴더 위치를 직접 확인해주세요.",
            success="저장 폴더 열기를 요청했습니다.",
        )

    def _request_open(
        self,
        path: Path,
        *,
        failure: str,
        success: str,
    ) -> None:
        try:
            opened = bool(self._path_opener(path))
        except Exception:
            opened = False
        if not opened:
            self._show_status(failure, outcome="error")
            return
        self._show_status(success, outcome="success")

    def _show_status(self, text: str, *, outcome: str) -> None:
        self._status.setText(text)
        self._status.setProperty("outcome", outcome)
        self._status.style().unpolish(self._status)
        self._status.style().polish(self._status)
        self._status.show()


def _open_local_path(path: Path) -> bool:
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def _period_text(starts_on, ends_on) -> str:
    if starts_on == ends_on:
        return f"{starts_on.year}년 {starts_on.month}월 {starts_on.day}일"
    if (
        starts_on.year == ends_on.year
        and starts_on.month == ends_on.month
        and starts_on.day == 1
    ):
        return f"{starts_on.year}년 {starts_on.month}월 1~{ends_on.day}일"
    return (
        f"{starts_on.year}년 {starts_on.month}월 {starts_on.day}일 ~ "
        f"{ends_on.year}년 {ends_on.month}월 {ends_on.day}일"
    )


def _generated_at_text(value: datetime) -> str:
    return f"{value.year}년 {value.month}월 {value.day}일 {value:%H:%M}"


def _file_size_text(byte_size: int) -> str:
    if byte_size < 1024:
        return f"{byte_size:,}바이트"
    if byte_size < 1024 * 1024:
        return f"{byte_size / 1024:.1f}KB"
    return f"{byte_size / (1024 * 1024):.1f}MB"
