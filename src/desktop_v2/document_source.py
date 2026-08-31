"""Revision-source picker shared by generated document workflows."""

from __future__ import annotations

from datetime import timedelta, timezone
from uuid import UUID

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.domain.revisions import RevisionSource, RevisionSummary

from .theme import application_stylesheet, register_application_fonts


_KOREA_TIME_ZONE = timezone(timedelta(hours=9), name="Asia/Seoul")
_SOURCE_LABELS = {
    RevisionSource.UI: "직접 변경",
    RevisionSource.IMPORT: "Excel 가져오기",
    RevisionSource.SYSTEM: "시스템",
}


class DocumentSourceDialog(QDialog):
    """Keep the latest revision selected until a user explicitly chooses history."""

    def __init__(
        self,
        summaries: tuple[RevisionSummary, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        values = tuple(summaries)
        if not values:
            raise ValueError("문서 원본으로 사용할 변경 이력이 없습니다.")
        if any(not isinstance(item, RevisionSummary) for item in values):
            raise TypeError("summaries에는 RevisionSummary만 허용됩니다.")
        if len({item.revision_id for item in values}) != len(values):
            raise ValueError("변경 이력에 같은 revision ID가 중복되었습니다.")
        self._summaries = tuple(
            sorted(values, key=lambda item: item.sequence, reverse=True)
        )
        self._latest = self._summaries[0]
        self._selected = self._latest

        register_application_fonts()
        self.setObjectName("documentSourceDialog")
        self.setWindowTitle("문서에 사용할 변경번호")
        self.setModal(True)
        self.resize(680, 470)
        self.setMinimumSize(600, 440)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()
        self._render_selection()

    @property
    def selected_revision_id(self) -> UUID:
        return self._selected.revision_id

    @property
    def selected_summary(self) -> RevisionSummary:
        return self._selected

    @property
    def using_latest(self) -> bool:
        return self._selected.revision_id == self._latest.revision_id

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 20)
        layout.setSpacing(14)
        layout.addWidget(self._build_header())
        layout.addWidget(self._build_source_ticket())

        self._archive_button = QPushButton("이전 변경번호로 만들기")
        self._archive_button.setObjectName("archiveAction")
        self._archive_button.setCheckable(True)
        self._archive_button.setEnabled(len(self._summaries) > 1)
        if len(self._summaries) == 1:
            self._archive_button.setText("이전 변경번호 없음")
        self._archive_button.toggled.connect(self._toggle_archive)
        layout.addWidget(self._archive_button)

        self._archive_frame = self._build_archive()
        self._archive_frame.setVisible(False)
        layout.addWidget(self._archive_frame, 1)

        self._selection_note = QLabel()
        self._selection_note.setObjectName("documentSourceNote")
        self._selection_note.setWordWrap(True)
        self._selection_note.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Maximum,
        )
        layout.addWidget(self._selection_note)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "이 변경번호 사용"
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName(
            "primaryAction"
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

    def _build_header(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("documentSourceHeader")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 15, 18, 16)
        layout.setSpacing(4)
        eyebrow = QLabel("DOCUMENT BASIS")
        eyebrow.setObjectName("editEyebrow")
        title = QLabel("문서에 사용할 명단")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel(
            "기본은 지금 화면의 최신 명단입니다. 예전 문서를 다시 만들 때만 이전 변경번호를 고르세요."
        )
        subtitle.setObjectName("workspaceSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(eyebrow)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        return frame

    def _build_source_ticket(self) -> QFrame:
        self._ticket = QFrame()
        self._ticket.setObjectName("documentSourceTicket")
        layout = QVBoxLayout(self._ticket)
        layout.setContentsMargins(18, 15, 18, 16)
        layout.setSpacing(5)
        heading = QHBoxLayout()
        label = QLabel("선택된 변경번호")
        label.setObjectName("sectionLabel")
        heading.addWidget(label)
        heading.addStretch(1)
        self._source_stamp = QLabel()
        self._source_stamp.setObjectName("documentSourceStamp")
        heading.addWidget(self._source_stamp)
        layout.addLayout(heading)
        self._source_code = QLabel()
        self._source_code.setObjectName("documentSourceCode")
        layout.addWidget(self._source_code)
        self._source_reason = QLabel()
        self._source_reason.setObjectName("documentSourceReason")
        self._source_reason.setWordWrap(True)
        layout.addWidget(self._source_reason)
        self._source_detail = QLabel()
        self._source_detail.setObjectName("muted")
        self._source_detail.setWordWrap(True)
        layout.addWidget(self._source_detail)
        return self._ticket

    def _build_archive(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("revisionArchive")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        title = QLabel("이전 변경번호")
        title.setObjectName("sectionLabel")
        heading.addWidget(title)
        heading.addStretch(1)
        self._latest_button = QPushButton("최신으로 되돌리기")
        self._latest_button.setObjectName("linkAction")
        self._latest_button.setVisible(False)
        self._latest_button.clicked.connect(self._select_latest)
        heading.addWidget(self._latest_button)
        layout.addLayout(heading)

        helper = QLabel("변경 사유와 적용일을 확인한 뒤 하나를 선택하세요.")
        helper.setObjectName("muted")
        layout.addWidget(helper)
        self._archive_list = QListWidget()
        self._archive_list.setObjectName("revisionArchiveList")
        self._archive_list.setAlternatingRowColors(True)
        self._archive_list.itemSelectionChanged.connect(
            self._select_archive_item
        )
        for summary in self._summaries[1:]:
            item = QListWidgetItem(_archive_item_text(summary))
            item.setData(Qt.ItemDataRole.UserRole, summary)
            item.setToolTip(_archive_item_tooltip(summary))
            item.setSizeHint(item.sizeHint().expandedTo(item.sizeHint()))
            self._archive_list.addItem(item)
        layout.addWidget(self._archive_list, 1)
        return frame

    def _toggle_archive(self, visible: bool) -> None:
        self._archive_frame.setVisible(visible)
        self._archive_button.setText(
            "이전 변경번호 닫기" if visible else "이전 변경번호로 만들기"
        )
        self.resize(max(self.width(), 680), 600 if visible else 470)

    def _select_archive_item(self) -> None:
        items = self._archive_list.selectedItems()
        if not items:
            return
        summary = items[0].data(Qt.ItemDataRole.UserRole)
        if not isinstance(summary, RevisionSummary):
            return
        self._selected = summary
        self._render_selection()

    def _select_latest(self) -> None:
        self._archive_list.clearSelection()
        self._selected = self._latest
        self._render_selection()

    def _render_selection(self) -> None:
        historical = not self.using_latest
        self._ticket.setProperty("historical", historical)
        self._source_stamp.setProperty("historical", historical)
        self._source_stamp.setText("이전 변경번호" if historical else "최신 · 기본")
        self._source_code.setText(self._selected.display_code)
        self._source_reason.setText(self._selected.reason)
        self._source_detail.setText(_summary_detail(self._selected))
        self._selection_note.setProperty("historical", historical)
        self._selection_note.setText(
            "선택한 변경번호 이후에 저장된 수정은 이 문서에 포함되지 않습니다."
            if historical
            else "현재까지 저장된 수정이 모두 포함됩니다. 그대로 진행해도 됩니다."
        )
        self._latest_button.setVisible(historical)
        for widget in (self._ticket, self._source_stamp, self._selection_note):
            widget.style().unpolish(widget)
            widget.style().polish(widget)


def _summary_detail(summary: RevisionSummary) -> str:
    recorded = summary.recorded_at.astimezone(_KOREA_TIME_ZONE)
    effective = (
        "적용일 없음"
        if summary.effective_on is None
        else f"적용일 {summary.effective_on:%Y.%m.%d}"
    )
    return (
        f"{recorded:%Y.%m.%d %H:%M} 저장 · {effective} · "
        f"{_SOURCE_LABELS[summary.source]}"
    )


def _archive_item_text(summary: RevisionSummary) -> str:
    recorded = summary.recorded_at.astimezone(_KOREA_TIME_ZONE)
    return f"{summary.display_code}   {summary.reason}\n{recorded:%Y.%m.%d %H:%M} 저장"


def _archive_item_tooltip(summary: RevisionSummary) -> str:
    return _summary_detail(summary)
