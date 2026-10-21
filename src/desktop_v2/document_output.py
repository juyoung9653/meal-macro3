"""Computer-local default folder and per-document output selection UI."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.revisions import format_revision_timestamp
from src.domain.roster import Meal

from .document_hub import DocumentAction
from .document_meal import DocumentMealSelection
from .theme import application_stylesheet, register_application_fonts
from .startup_loading import run_progress_task


DirectoryPicker = Callable[[QWidget | None, str, str], str]


_FILENAME_ACTION_LABELS = {
    DocumentAction.INVOICE: "청구서",
    DocumentAction.CONFIRMATION: "확인서",
    DocumentAction.DELIVERY_REPORT: "일일식수",
    DocumentAction.DELIVERY_SUBSTITUTE: "배송대타",
}


class DocumentOutputMode(StrEnum):
    """Whether a document uses the saved, replacement, or one-time folder."""

    SAVED_DEFAULT = "saved_default"
    NEW_DEFAULT = "new_default"
    ONE_TIME = "one_time"


@dataclass(frozen=True, slots=True)
class DocumentOutputSelection:
    """Validated document inputs plus a new, non-conflicting output name."""

    document: DocumentMealSelection
    directory: Path
    mode: DocumentOutputMode
    filename: str
    reissue_number: int

    def __post_init__(self) -> None:
        if not isinstance(self.document, DocumentMealSelection):
            raise TypeError("문서 식종 선택이 올바르지 않습니다.")
        if not isinstance(self.mode, DocumentOutputMode):
            raise TypeError("문서 폴더 사용 방식이 올바르지 않습니다.")
        directory = _resolved_path(self.directory)
        if not directory.is_dir():
            raise ValueError("문서를 저장할 폴더를 찾을 수 없습니다.")
        if (
            isinstance(self.reissue_number, bool)
            or not isinstance(self.reissue_number, int)
        ):
            raise TypeError("문서 재발행 번호는 정수여야 합니다.")
        if self.reissue_number < 1:
            raise ValueError("문서 재발행 번호는 1 이상이어야 합니다.")
        expected = suggest_document_filename(
            self.document,
            self.reissue_number,
        )
        if self.filename != expected:
            raise ValueError("문서 파일명이 자동 파일명 규칙과 일치하지 않습니다.")
        if (directory / self.filename).exists():
            raise ValueError("같은 이름의 파일이 이미 있습니다.")
        object.__setattr__(self, "directory", directory)

    @property
    def remembers_directory(self) -> bool:
        return self.mode is DocumentOutputMode.NEW_DEFAULT

    @property
    def output_path(self) -> Path:
        return self.directory / self.filename


def suggest_document_filename(
    document: DocumentMealSelection,
    reissue_number: int = 1,
) -> str:
    """Build one readable filename from sealed document inputs."""

    if not isinstance(document, DocumentMealSelection):
        raise TypeError("문서 식종 선택이 올바르지 않습니다.")
    if isinstance(reissue_number, bool) or not isinstance(reissue_number, int):
        raise TypeError("문서 재발행 번호는 정수여야 합니다.")
    if reissue_number < 1:
        raise ValueError("문서 재발행 번호는 1 이상이어야 합니다.")
    period = document.period
    if period.action in (
        DocumentAction.INVOICE,
        DocumentAction.CONFIRMATION,
    ):
        period_token = period.starts_on.strftime("%Y-%m")
    elif period.starts_on == period.ends_on:
        period_token = period.starts_on.isoformat()
    else:
        period_token = (
            f"{period.starts_on.isoformat()}~{period.ends_on.isoformat()}"
        )
    parts = [period_token, _FILENAME_ACTION_LABELS[period.action]]
    if (
        period.action is DocumentAction.DELIVERY_SUBSTITUTE
        or document.meals != tuple(Meal)
    ):
        parts.append("-".join(meal.value for meal in document.meals))
    parts.append(format_revision_timestamp(period.source.recorded_at))
    if reissue_number > 1:
        parts.append(f"재발행{reissue_number:02d}")
    return "_".join(parts) + ".xlsx"


def next_available_document_output(
    document: DocumentMealSelection,
    directory: str | Path,
    mode: DocumentOutputMode,
) -> DocumentOutputSelection:
    """Choose the first automatic filename that does not already exist."""

    resolved = _resolved_path(directory)
    if not resolved.is_dir():
        raise ValueError("문서를 저장할 폴더를 찾을 수 없습니다.")
    reissue_number = 1
    while True:
        filename = suggest_document_filename(document, reissue_number)
        if not (resolved / filename).exists():
            return DocumentOutputSelection(
                document,
                resolved,
                mode,
                filename,
                reissue_number,
            )
        reissue_number += 1


class DocumentOutputDialog(QDialog):
    """Confirm the remembered document shelf or choose a one-time folder."""

    def __init__(
        self,
        document: DocumentMealSelection,
        saved_directory: str | Path | None,
        parent: QWidget | None = None,
        *,
        directory_picker: DirectoryPicker | None = None,
    ) -> None:
        if not isinstance(document, DocumentMealSelection):
            raise TypeError("문서 식종 선택이 올바르지 않습니다.")
        super().__init__(parent)
        self._document = document
        self._saved_directory, saved_available = run_progress_task(
            lambda: _checked_directory(saved_directory),
            title="문서 저장 위치를 확인하는 중입니다",
            message="저장된 기본 문서 폴더에 연결하는 중입니다.", parent=parent,
        )
        self._selected_directory: Path | None = (
            self._saved_directory
            if saved_available
            else None
        )
        self._mode: DocumentOutputMode | None = (
            DocumentOutputMode.SAVED_DEFAULT
            if self._selected_directory is not None
            else None
        )
        self._accepted_selection: DocumentOutputSelection | None = None
        self._directory_picker = directory_picker or _pick_existing_directory

        register_application_fonts()
        self.setObjectName("documentOutputDialog")
        self.setWindowTitle(
            f"{document.period.action.label} 저장 위치와 파일명"
        )
        self.setModal(True)
        self.resize(820, 630)
        self.setMinimumSize(740, 590)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()
        self._refresh_state()

    @property
    def document(self) -> DocumentMealSelection:
        return self._document

    def selection(self) -> DocumentOutputSelection:
        if self._accepted_selection is not None:
            return self._accepted_selection
        if self._selected_directory is None or self._mode is None:
            raise ValueError("문서를 저장할 폴더를 먼저 선택해주세요.")
        document, directory, mode = self._document, self._selected_directory, self._mode
        return run_progress_task(
            lambda: next_available_document_output(document, directory, mode),
            title="문서 저장 위치를 확인하는 중입니다",
            message="저장 폴더와 사용할 수 있는 파일명을 확인하는 중입니다.", parent=self,
        )

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 19)
        root.setSpacing(13)
        root.addWidget(self._build_header())
        root.addWidget(self._build_document_ticket())


        section = QFrame()
        section.setObjectName("editSection")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(15, 13, 15, 15)
        layout.setSpacing(10)
        label = QLabel("문서를 넣을 보관함")
        label.setObjectName("sectionLabel")
        layout.addWidget(label)
        self._destination = self._build_destination_ticket()
        layout.addWidget(self._destination, 1)

        actions = QHBoxLayout()
        actions.setSpacing(7)
        self._change_default_button = QPushButton("기본 폴더 선택")
        self._change_default_button.setObjectName("quietAction")
        self._change_default_button.clicked.connect(self._choose_new_default)
        actions.addWidget(self._change_default_button)
        self._one_time_button = QPushButton("이번만 다른 폴더")
        self._one_time_button.setObjectName("quietAction")
        self._one_time_button.clicked.connect(self._choose_one_time)
        actions.addWidget(self._one_time_button)
        self._restore_default_button = QPushButton("기본 폴더로 돌아가기")
        self._restore_default_button.setObjectName("linkAction")
        self._restore_default_button.clicked.connect(
            self._restore_saved_default
        )
        actions.addWidget(self._restore_default_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        filename_label = QLabel("자동 파일명")
        filename_label.setObjectName("sectionLabel")
        layout.addWidget(filename_label)
        self._filename_panel = self._build_filename_ticket()
        layout.addWidget(self._filename_panel)
        root.addWidget(section, 1)

        self._error = QLabel()
        self._error.setObjectName("errorMessage")
        self._error.setWordWrap(True)
        self._error.hide()
        root.addWidget(self._error)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._save_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._save_button.setText("이 이름으로 만들기")
        self._save_button.setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("뒤로")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_header(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("documentOutputHeader")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 15, 18, 16)
        layout.setSpacing(4)
        eyebrow = QLabel("문서 저장 · 문서 보관함")
        eyebrow.setObjectName("editEyebrow")
        title = QLabel("저장 위치와 파일명 확인")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel(
            "기억한 폴더와 자동 파일명을 확인합니다. 같은 이름이 있으면 새 재발행 번호를 붙입니다."
        )
        subtitle.setObjectName("workspaceSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(eyebrow)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        return frame

    def _build_document_ticket(self) -> QFrame:
        period = self._document.period
        frame = QFrame()
        frame.setObjectName("documentOutputContext")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(12)
        stamp = QLabel(period.action.label)
        stamp.setObjectName("documentOutputContextStamp")
        stamp.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stamp.setFixedWidth(76)
        layout.addWidget(stamp)
        copy = QVBoxLayout()
        copy.setSpacing(3)
        title = QLabel(_document_period_text(self._document))
        title.setObjectName("sectionLabel")
        meals = " · ".join(meal.value for meal in self._document.meals)
        detail = QLabel(
            "포함 식종 "
            f"{meals}  ·  변경번호 "
            f"{format_revision_timestamp(period.source.recorded_at)}"
        )
        detail.setObjectName("documentOutputContextDetail")
        copy.addWidget(title)
        copy.addWidget(detail)
        if period.action is DocumentAction.DELIVERY_REPORT and self._document.delivery_forms is not None:
            forms = " · ".join(
                "기본" if form is None else form
                for form in self._document.delivery_forms
            )
            form_detail = QLabel(f"포함 배송 형태 {forms}")
            form_detail.setObjectName("documentOutputContextDetail")
            form_detail.setWordWrap(True)
            copy.addWidget(form_detail)
        layout.addLayout(copy, 1)
        return frame

    def _build_destination_ticket(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("documentOutputDestination")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)
        self._folder_stamp = QLabel()
        self._folder_stamp.setObjectName("documentOutputFolderStamp")
        self._folder_stamp.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._folder_stamp.setFixedSize(74, 64)
        layout.addWidget(self._folder_stamp)
        copy = QVBoxLayout()
        copy.setSpacing(4)
        self._folder_status = QLabel()
        self._folder_status.setObjectName("documentOutputFolderStatus")
        self._folder_name = QLabel()
        self._folder_name.setObjectName("documentOutputFolderName")
        self._folder_path = QLabel()
        self._folder_path.setObjectName("documentOutputFolderPath")
        self._folder_path.setWordWrap(True)
        self._folder_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        copy.addWidget(self._folder_status)
        copy.addWidget(self._folder_name)
        copy.addWidget(self._folder_path)
        layout.addLayout(copy, 1)
        return frame

    def _build_filename_ticket(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("documentOutputFilenamePanel")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(12)
        stamp = QLabel("파일")
        stamp.setObjectName("documentOutputFilenameStamp")
        stamp.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stamp.setFixedSize(58, 48)
        layout.addWidget(stamp)
        copy = QVBoxLayout()
        copy.setSpacing(3)
        self._filename_status = QLabel()
        self._filename_status.setObjectName("documentOutputFilenameStatus")
        self._filename_status.setWordWrap(True)
        self._filename = QLabel()
        self._filename.setObjectName("documentOutputFilename")
        self._filename.setWordWrap(True)
        self._filename.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        copy.addWidget(self._filename_status)
        copy.addWidget(self._filename)
        layout.addLayout(copy, 1)
        return frame

    def _choose_new_default(self) -> None:
        self._choose_directory(
            "앞으로 사용할 기본 문서 폴더 선택",
            DocumentOutputMode.NEW_DEFAULT,
        )

    def _choose_one_time(self) -> None:
        self._choose_directory(
            "이번 문서만 저장할 폴더 선택",
            DocumentOutputMode.ONE_TIME,
        )

    def _choose_directory(
        self,
        title: str,
        mode: DocumentOutputMode,
    ) -> None:
        start = self._selected_directory or self._saved_directory
        selected = self._directory_picker(
            self,
            title,
            "" if start is None else str(start),
        )
        if not selected:
            return
        try:
            directory, exists = run_progress_task(
                lambda: _checked_directory(selected),
                title="문서 저장 위치를 확인하는 중입니다",
                message="선택한 문서 폴더에 연결하는 중입니다.", parent=self,
            )
        except (ValueError, OSError) as error:
            self._error.setText(str(error))
            self._error.show()
            return
        if not exists:
            self._error.setText(
                "선택한 폴더를 찾을 수 없습니다. 다른 폴더를 선택해주세요."
            )
            self._error.show()
            return
        self._selected_directory = directory
        self._mode = mode
        self._refresh_state()

    def _restore_saved_default(self) -> None:
        saved = self._saved_directory
        if not run_progress_task(
            lambda: _is_existing_directory(saved),
            title="문서 저장 위치를 확인하는 중입니다",
            message="기본 문서 폴더에 연결하는 중입니다.", parent=self,
        ):
            return
        self._selected_directory = self._saved_directory
        self._mode = DocumentOutputMode.SAVED_DEFAULT
        self._refresh_state()

    def _refresh_state(self) -> None:
        self._accepted_selection = None
        selected, saved, document, requested_mode = (
            self._selected_directory, self._saved_directory, self._document, self._mode,
        )

        def check():
            available = _is_existing_directory(selected)
            saved_available = _is_existing_directory(saved)
            selection = (
                next_available_document_output(document, selected, requested_mode)
                if available and requested_mode is not None else None
            )
            return available, saved_available, selection

        try:
            available, saved_available, selection = run_progress_task(
                check, title="문서 저장 위치를 확인하는 중입니다",
                message="저장 폴더와 사용할 수 있는 파일명을 확인하는 중입니다.", parent=self,
            )
        except (ValueError, OSError) as error:
            self._error.setText(str(error))
            self._error.show()
            self._save_button.setEnabled(False)
            return
        mode = self._mode if available else None
        mode_value = "missing" if mode is None else mode.value
        self._destination.setProperty(
            "mode",
            mode_value,
        )
        self._destination.style().unpolish(self._destination)
        self._destination.style().polish(self._destination)
        for label in (self._folder_stamp, self._folder_status):
            label.setProperty("mode", mode_value)
            label.style().unpolish(label)
            label.style().polish(label)

        self._one_time_button.setVisible(saved_available)
        self._restore_default_button.setVisible(
            saved_available and mode is not DocumentOutputMode.SAVED_DEFAULT
        )

        if mode is None:
            self._folder_stamp.setText("선택")
            self._folder_status.setText("기본 문서 폴더가 필요합니다")
            if self._saved_directory is None:
                self._folder_name.setText("아직 정한 폴더가 없습니다")
                self._folder_path.setText(
                    "‘기본 폴더 선택’을 눌러 문서를 모아둘 곳을 정해주세요."
                )
                self._error.hide()
            else:
                self._folder_name.setText(self._saved_directory.name)
                self._folder_path.setText(str(self._saved_directory))
                self._error.setText(
                    "기억한 기본 폴더를 찾을 수 없습니다. 새 기본 폴더를 선택해주세요."
                )
                self._error.show()
            self._change_default_button.setText("기본 폴더 선택")
            self._save_button.setEnabled(False)
            self._refresh_filename(None)
            return

        assert self._selected_directory is not None
        self._error.hide()
        self._folder_name.setText(
            self._selected_directory.name or str(self._selected_directory)
        )
        self._folder_path.setText(str(self._selected_directory))
        if mode is DocumentOutputMode.SAVED_DEFAULT:
            self._folder_stamp.setText("기본")
            self._folder_status.setText("설정에 저장된 기본 문서 폴더")
            self._change_default_button.setText("기본 폴더 변경")
        elif mode is DocumentOutputMode.NEW_DEFAULT:
            self._folder_stamp.setText("새 기본")
            self._folder_status.setText(
                "이 폴더에 만들고 앞으로의 기본 폴더로 기억합니다"
            )
            self._change_default_button.setText("다른 기본 폴더 선택")
        else:
            self._folder_stamp.setText("이번만")
            self._folder_status.setText(
                "이번 문서에만 사용합니다 · 기본 폴더는 바꾸지 않습니다"
            )
            self._change_default_button.setText("기본 폴더 변경")
        self._save_button.setEnabled(True)
        self._refresh_filename(selection)

    def _refresh_filename(
        self,
        selection: DocumentOutputSelection | None,
    ) -> None:
        reissue = selection is not None and selection.reissue_number > 1
        self._filename_panel.setProperty("reissue", reissue)
        self._filename_panel.setProperty("available", selection is not None)
        self._filename_panel.style().unpolish(self._filename_panel)
        self._filename_panel.style().polish(self._filename_panel)
        if selection is None:
            self._filename_status.setText(
                "저장 폴더를 정하면 파일명을 자동으로 만듭니다"
            )
            self._filename.setText("기간_문서종류_변경번호.xlsx")
            return
        self._filename.setText(selection.filename)
        if reissue:
            self._filename_status.setText(
                "기존 파일 보존 · "
                f"재발행 {selection.reissue_number:02d}로 새 이름을 사용합니다"
            )
        else:
            self._filename_status.setText(
                "자동으로 정한 이름 · 기존 파일은 덮어쓰지 않습니다"
            )

    def _accept_if_valid(self) -> None:
        try:
            selection = self.selection()
        except (TypeError, ValueError, OSError) as error:
            self._error.setText(str(error))
            self._error.show()
            return
        self._accepted_selection = selection
        self._refresh_filename(selection)
        self.accept()


class DefaultDocumentDirectoryDialog(QDialog):
    """Set the computer-local default output folder from Settings."""

    def __init__(
        self,
        current_directory: str | Path | None,
        parent: QWidget | None = None,
        *,
        directory_picker: DirectoryPicker | None = None,
    ) -> None:
        super().__init__(parent)
        self._current_directory, current_available = run_progress_task(
            lambda: _checked_directory(current_directory),
            title="문서 저장 위치를 확인하는 중입니다",
            message="저장된 기본 문서 폴더에 연결하는 중입니다.", parent=parent,
        )
        self._selected_directory: Path | None = (
            self._current_directory
            if current_available
            else None
        )
        self._directory_picker = directory_picker or _pick_existing_directory
        register_application_fonts()
        self.setObjectName("defaultDocumentDirectoryDialog")
        self.setWindowTitle("설정 · 문서 저장 폴더")
        self.setModal(True)
        self.resize(720, 430)
        self.setMinimumSize(650, 400)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()
        self._refresh_state()

    @property
    def selected_directory(self) -> Path:
        if self._selected_directory is None:
            raise ValueError("기본 문서 폴더를 먼저 선택해주세요.")
        selected = self._selected_directory
        directory, exists = run_progress_task(
            lambda: _checked_directory(selected),
            title="문서 저장 위치를 확인하는 중입니다",
            message="선택한 기본 문서 폴더에 연결하는 중입니다.", parent=self,
        )
        if not exists:
            raise ValueError("선택한 기본 문서 폴더를 찾을 수 없습니다.")
        return directory

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 19)
        root.setSpacing(13)
        header = QFrame()
        header.setObjectName("documentDirectoryHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(18, 15, 18, 16)
        header_layout.setSpacing(4)
        eyebrow = QLabel("문서 저장 · 공통 기본 폴더")
        eyebrow.setObjectName("editEyebrow")
        title = QLabel("문서 저장 폴더")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel(
            "청구서·확인서·배송 문서를 한곳에서 찾도록 기본 폴더를 정합니다."
        )
        subtitle.setObjectName("workspaceSubtitle")
        header_layout.addWidget(eyebrow)
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        root.addWidget(header)

        self._panel = QFrame()
        self._panel.setObjectName("documentDirectoryPanel")
        panel_layout = QVBoxLayout(self._panel)
        panel_layout.setContentsMargins(16, 14, 16, 14)
        panel_layout.setSpacing(6)
        self._status = QLabel()
        self._status.setObjectName("documentOutputFolderStatus")
        self._name = QLabel()
        self._name.setObjectName("documentOutputFolderName")
        self._path = QLabel()
        self._path.setObjectName("documentOutputFolderPath")
        self._path.setWordWrap(True)
        self._path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        panel_layout.addWidget(self._status)
        panel_layout.addWidget(self._name)
        panel_layout.addWidget(self._path)
        root.addWidget(self._panel, 1)

        choose = QPushButton("폴더 선택")
        choose.setObjectName("quietAction")
        choose.clicked.connect(self._choose_directory)
        root.addWidget(choose, 0, Qt.AlignmentFlag.AlignLeft)

        self._error = QLabel()
        self._error.setObjectName("errorMessage")
        self._error.setWordWrap(True)
        self._error.hide()
        root.addWidget(self._error)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._save_button = buttons.button(
            QDialogButtonBox.StandardButton.Save
        )
        self._save_button.setText("기본 폴더 저장")
        self._save_button.setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("닫기")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _choose_directory(self) -> None:
        start = self._selected_directory or self._current_directory
        selected = self._directory_picker(
            self,
            "기본 문서 폴더 선택",
            "" if start is None else str(start),
        )
        if not selected:
            return
        try:
            directory, exists = run_progress_task(
                lambda: _checked_directory(selected),
                title="문서 저장 위치를 확인하는 중입니다",
                message="선택한 문서 폴더에 연결하는 중입니다.", parent=self,
            )
        except (ValueError, OSError) as error:
            self._error.setText(str(error))
            self._error.show()
            return
        if not exists:
            self._error.setText(
                "선택한 폴더를 찾을 수 없습니다. 다른 폴더를 선택해주세요."
            )
            self._error.show()
            return
        self._selected_directory = directory
        self._refresh_state()

    def _refresh_state(self) -> None:
        selected = self._selected_directory
        available = run_progress_task(
            lambda: _is_existing_directory(selected),
            title="문서 저장 위치를 확인하는 중입니다",
            message="문서 폴더에 연결하는 중입니다.", parent=self,
        )
        self._panel.setProperty("available", available)
        self._panel.style().unpolish(self._panel)
        self._panel.style().polish(self._panel)
        if not available:
            self._status.setText("기본 문서 폴더가 아직 없습니다")
            if self._current_directory is None:
                self._name.setText("폴더를 선택해주세요")
                self._path.setText(
                    "여기에 저장한 경로는 같은 프로그램 폴더를 사용하는 모든 PC에 적용됩니다."
                )
                self._error.hide()
            else:
                self._name.setText(self._current_directory.name)
                self._path.setText(str(self._current_directory))
                self._error.setText(
                    "기억한 폴더를 찾을 수 없습니다. 새 폴더를 선택해주세요."
                )
                self._error.show()
            self._save_button.setEnabled(False)
            return
        assert self._selected_directory is not None
        self._status.setText("공통 기본 문서 폴더")
        self._name.setText(
            self._selected_directory.name or str(self._selected_directory)
        )
        self._path.setText(str(self._selected_directory))
        self._error.hide()
        self._save_button.setEnabled(True)

    def _accept_if_valid(self) -> None:
        try:
            self.selected_directory
        except (ValueError, OSError) as error:
            self._error.setText(str(error))
            self._error.show()
            return
        self.accept()


def _pick_existing_directory(
    parent: QWidget | None,
    title: str,
    start: str,
) -> str:
    return QFileDialog.getExistingDirectory(parent, title, start)


def _resolved_path(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise TypeError("문서 폴더 경로는 문자열 또는 Path여야 합니다.")
    return Path(value).expanduser().resolve()


def _optional_resolved_path(value: str | Path | None) -> Path | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _resolved_path(value)


def _checked_directory(value: str | Path | None) -> tuple[Path | None, bool]:
    directory = _optional_resolved_path(value)
    return directory, _is_existing_directory(directory)


def _is_existing_directory(value: Path | None) -> bool:
    return value is not None and value.is_dir()


def _document_period_text(document: DocumentMealSelection) -> str:
    period = document.period
    if period.starts_on == period.ends_on:
        dates = period.starts_on.strftime("%Y.%m.%d")
    else:
        dates = f"{period.starts_on:%Y.%m.%d} ~ {period.ends_on:%Y.%m.%d}"
    return f"{period.action.label} · {dates}"
