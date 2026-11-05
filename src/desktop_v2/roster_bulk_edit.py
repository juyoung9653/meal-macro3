"""Find/replace selected roster cells and commit one reviewed revision."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from src.domain.column_input_rules import default_rule, load_rules, rule_for, validate_input
from src.domain.household_shared import (
    HouseholdSharedConflict, build_profile_correction_revision, profile_with_shared_routing,
)
from src.domain.import_shared import import_candidate_snapshot
from src.domain.lunch_periods import LUNCH_DATE_COLUMNS, build_lunch_period_revision
from src.domain.mobile_household_merge import prepare_mobile_household_revision
from src.domain.profile_edits import build_person_profile_revision
from src.domain.projections import roster_from_historical_snapshot
from src.domain.revisions import (
    AuditChange, ChangeOperation, HistoricalSnapshot, RevisionMetadata, RevisionRecord, RevisionSource,
)
from src.domain.roster import InclusiveDateRange
from src.storage.database_session import committed_roster_after_save
from src.storage.encrypted_database import RevisionConflictError
from .dialog_workflow import DialogRequest, dialog_workflow
from .import_controller import current_windows_actor
from .roster_cell_edit import (
    RosterCellEditOutcome, RosterCellEditStatus, _ROUTING_PROFILE_FIELDS,
    is_roster_cell_editable, roster_cell_text, updated_profile_for_cell,
)
from .startup_loading import run_progress_task, save_revision
from .theme import application_stylesheet, register_application_fonts
from .user_messages import user_error


@dataclass(frozen=True)
class BulkEditPlan:
    record: RevisionRecord | None
    rows: tuple[tuple[str, str, str, str, str], ...] = ()
    selected_changes: int = 0


def _cell_text(roster, snapshot, person_id, key, on):
    person = roster.person(person_id)
    if key in _ROUTING_PROFILE_FIELDS:
        person = replace(person, profile=profile_with_shared_routing(roster, person_id, on, snapshot))
    definition = next((d for d in roster.custom_fields if key == f"custom:{d.definition_id}"), None)
    return roster_cell_text(person, key, definition)


def plan_bulk_edit(snapshot, targets, labels, *, operation, match_mode, find, replacement,
                   service_day, effective_on, actor, recorded_at, revision_id):
    """Build against frozen selected values; no database writes or per-cell saves."""
    if operation not in {"replace", "fill"} or match_mode not in {"exact", "partial"}:
        raise ValueError("변경 방식과 일치 조건을 다시 선택해주세요.")
    if operation == "replace" and match_mode == "partial" and not find:
        raise ValueError("부분 일치로 바꾸려면 찾을 내용을 입력해주세요.")
    targets = tuple(dict.fromkeys(targets))
    if not targets or any(not is_roster_cell_editable(key) for _, key in targets):
        raise ValueError("직접 수정할 수 있는 일반 칼럼의 셀을 선택해주세요.")
    roster = roster_from_historical_snapshot(snapshot)
    rules = load_rules(snapshot)
    changes = {}
    for pid, key in targets:
        before = _cell_text(roster, snapshot, pid, key, service_day)
        if operation == "fill":
            after = replacement
        elif match_mode == "exact":
            after = replacement if before == find else before
        else:
            after = before.replace(find, replacement)
        if before == after:
            continue
        try:
            if key in LUNCH_DATE_COLUMNS:
                after = after.strip()
                validate_input(key, after, default_rule(key))
            else:
                person = roster.person(pid)
                updated = updated_profile_for_cell(person, key, after, roster.custom_fields)
                definition = next((d for d in roster.custom_fields if key == f"custom:{d.definition_id}"), None)
                after = roster_cell_text(replace(person, profile=updated), key, definition)
            validate_input(key, after, rule_for(rules, key))
        except (ValueError, TypeError) as error:
            raise ValueError(f"{roster.person(pid).profile.name} · {labels.get(key, '선택 칼럼')}: {user_error(error)}") from error
        if before != after:
            changes[(pid, key)] = (before, after)
    if not changes:
        return BulkEditPlan(None)

    working = snapshot
    corrected_components, corrected_events = [], []
    lunch_edits = False

    def apply_step(step):
        nonlocal working
        candidate = import_candidate_snapshot(working, step.record)
        # All steps remain in memory; every builder sees the same DB parent.
        working = HistoricalSnapshot(snapshot.revision_id, candidate.entities)
        correction = step.record.metadata.decisions.get("profile_routing_correction", {})
        corrected_components.extend(correction.get("components", ()))
        corrected_events.extend(correction.get("event_ids", ()))

    kwargs = dict(actor=actor, recorded_at=recorded_at, effective_on=effective_on, revision_id=revision_id)
    for pid in dict.fromkeys(pid for pid, _ in changes):
        current = roster_from_historical_snapshot(working)
        person = current.person(pid)
        profile_keys = [key for person_id, key in changes if person_id == pid and key not in LUNCH_DATE_COLUMNS]
        if profile_keys:
            routing_keys = [key for key in profile_keys if key in _ROUTING_PROFILE_FIELDS]
            editing_person = replace(person, profile=profile_with_shared_routing(current, pid, effective_on, working)) if routing_keys else person
            updated = editing_person.profile
            for key in profile_keys:
                updated = updated_profile_for_cell(replace(editing_person, profile=updated), key, changes[(pid, key)][1], current.custom_fields)
            if updated != editing_person.profile or routing_keys:
                if routing_keys:
                    try:
                        step = build_profile_correction_revision(current, pid, updated, snapshot=working,
                            forced_routing_fields=tuple(_ROUTING_PROFILE_FIELDS[key] for key in routing_keys),
                            reason="명단 셀 일괄 변경", **kwargs)
                    except ValueError as error:
                        # An earlier selected member may already have applied
                        # this identical shared address correction.
                        if str(error) == "변경된 사람 정보가 없습니다.":
                            step = None
                        else:
                            raise
                else:
                    step = build_person_profile_revision(current, pid, updated, reason="명단 셀 일괄 변경", **kwargs)
                if step is not None:
                    apply_step(step)

        date_keys = [key for person_id, key in changes if person_id == pid and key in LUNCH_DATE_COLUMNS]
        if date_keys:
            lunch_edits = True
            old = current.person(pid).lunch_period or InclusiveDateRange()
            values = {key: date.fromisoformat(changes[(pid, key)][1]) if changes[(pid, key)][1] else None for key in date_keys}
            start = values.get("lunch_starts_on", old.starts_on)
            end = values.get("lunch_ends_on", old.ends_on)
            if "lunch_starts_on" in values and start is None:
                if values.get("lunch_ends_on") is not None:
                    raise ValueError("중식 시작일을 비우면서 종료일을 입력할 수 없습니다.")
                end = None
            if start is None and end is not None:
                raise ValueError("중식 시작일을 먼저 입력해주세요.")
            InclusiveDateRange(start, end)
            # Move the end first when moving a whole period past its old end.
            if start is not None and old.ends_on is not None and start > old.ends_on:
                date_keys.sort(key=lambda key: key != "lunch_ends_on")
            else:
                date_keys.sort(key=lambda key: key != "lunch_starts_on")
            for key in date_keys:
                current = roster_from_historical_snapshot(working)
                step = build_lunch_period_revision(current, pid, key, values[key], **kwargs)
                if step is not None:
                    apply_step(step)

    audit = []
    for ref in dict.fromkeys((*snapshot.entities, *working.entities)):
        before, after = snapshot.entities.get(ref), working.entities.get(ref)
        if before != after:
            operation_kind = ChangeOperation.INSERT if before is None else ChangeOperation.DELETE if after is None else ChangeOperation.UPDATE
            audit.append(AuditChange(ref.entity_kind, ref.entity_id, operation_kind, before, after))
    if not audit:
        return BulkEditPlan(None)
    decisions = {"roster_bulk_edit": {"operation": operation, "match_mode": match_mode,
        "cells": [{"person_id": str(pid), "column": key} for pid, key in changes]}}
    if corrected_components:
        decisions["profile_routing_correction"] = {"components": corrected_components, "event_ids": list(dict.fromkeys(corrected_events))}
    if lunch_edits:
        decisions["bulk_lunch_period_edit"] = True
    record = RevisionRecord(RevisionMetadata(revision_id, snapshot.revision_id, recorded_at, effective_on,
        actor, f"명단 셀 일괄 변경 · {len(changes)}개", RevisionSource.UI, decisions=decisions), tuple(audit))
    try:
        record = prepare_mobile_household_revision(snapshot, record)
    except HouseholdSharedConflict as error:
        raise ValueError("같은 가구의 공용 값이 서로 다르게 바뀌어 저장할 수 없습니다. 같은 가구에는 같은 값을 입력하거나 칼럼의 공용 예외를 확인해주세요.") from error
    final_snapshot = import_candidate_snapshot(snapshot, record)
    final_roster = roster_from_historical_snapshot(final_snapshot)
    # Shared routing corrections must not silently overwrite a selected value.
    for (pid, key), (_, expected) in changes.items():
        actual = _cell_text(final_roster, final_snapshot, pid, key, effective_on)
        if actual != expected:
            raise ValueError("같은 가구의 선택한 셀에 서로 다른 값이 적용됩니다. 공용 칼럼의 값을 같게 입력하거나 공용 예외를 확인해주세요.")

    rows = []
    custom_keys = {f"custom:{definition.definition_id}" for definition in roster.custom_fields}
    keys = [key for key in labels if is_roster_cell_editable(key)
            and (not key.startswith("custom:") or key in custom_keys)]
    affected_ids = {pid for pid, _ in changes}
    for change in record.changes:
        if change.entity_kind == "roster.person":
            affected_ids.add(change.entity_id)
        elif change.after is not None and "person_id" in change.after:
            affected_ids.add(UUID(change.after["person_id"]))
    for person in roster.persons:
        if person.person_id not in affected_ids:
            continue
        for key in keys:
            target = (person.person_id, key)
            before = _cell_text(roster, snapshot, person.person_id, key, effective_on)
            after = _cell_text(final_roster, final_snapshot, person.person_id, key, effective_on)
            if target in changes:
                before, after = changes[target]
            if before != after:
                rows.append((person.profile.name, labels[key], before, after,
                             "선택 셀" if target in changes else "함께 반영"))
    return BulkEditPlan(record, tuple(rows), len(changes))


class RosterBulkEditDialog(QDialog):
    def __init__(self, snapshot, targets, labels, service_day, parent=None, *, actor=None):
        super().__init__(parent)
        self._snapshot, self._targets, self._labels = snapshot, targets, labels
        self._service_day = service_day
        self._actor = actor or current_windows_actor()
        self.plan = BulkEditPlan(None)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("선택 셀 일괄 변경")
        self.resize(820, 640)
        self.setMinimumSize(620, 520)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        title = QLabel("선택 셀 일괄 변경")
        title.setObjectName("workspaceTitle")
        root.addWidget(title)
        note = QLabel(f"선택한 {len(targets)}개 셀에서 바꿉니다. 가구 공용 칼럼은 공용 예외를 제외한 같은 가구원에게 자동 반영됩니다.")
        note.setWordWrap(True)
        root.addWidget(note)
        form = QFormLayout()
        self.operation = QComboBox()
        self.operation.addItem("찾아 바꾸기", "replace")
        self.operation.addItem("같은 값으로 채우기", "fill")
        self.match_mode = QComboBox()
        self.match_mode.addItem("완전 일치", "exact")
        self.match_mode.addItem("부분 일치", "partial")
        self.find_text = QPlainTextEdit()
        self.find_text.setFixedHeight(58)
        self.find_text.setPlaceholderText("찾을 내용 · 완전 일치에서 빈칸은 빈 셀을 찾습니다")
        self.replacement = QPlainTextEdit()
        self.replacement.setFixedHeight(58)
        self.replacement.setPlaceholderText("바꿀 내용 · 비우면 해당 값을 지웁니다")
        for label, widget in (("변경 방식", self.operation), ("일치 조건", self.match_mode),
                              ("찾을 내용", self.find_text), ("바꿀 내용", self.replacement)):
            widget.setAccessibleName(label)
            form.addRow(label, widget)
        root.addLayout(form)
        self.preview_button = QPushButton("변경 미리보기")
        self.preview_button.setObjectName("quietAction")
        self.preview_button.clicked.connect(self.refresh_preview)
        root.addWidget(self.preview_button)
        self.summary = QLabel("변경할 내용을 입력한 뒤 미리보기를 눌러주세요.")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.summary)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(("성명", "칼럼", "변경 전", "변경 후", "반영 범위"))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        root.addWidget(self.table, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("취소")
        cancel.setObjectName("quietAction")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self.save_button = QPushButton("변경 저장")
        self.save_button.setObjectName("primaryAction")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.accept)
        actions.addWidget(self.save_button)
        root.addLayout(actions)
        self.operation.currentIndexChanged.connect(self._invalidate)
        self.match_mode.currentIndexChanged.connect(self._invalidate)
        self.find_text.textChanged.connect(self._invalidate)
        self.replacement.textChanged.connect(self._invalidate)

    def _invalidate(self, *_args):
        self.plan = BulkEditPlan(None)
        self.save_button.setEnabled(False)
        self.table.setRowCount(0)
        finding = self.operation.currentData() == "replace"
        self.find_text.setEnabled(finding)
        self.match_mode.setEnabled(finding)
        self.summary.setText("변경 미리보기를 눌러 적용할 내용을 확인해주세요.")

    def refresh_preview(self):
        self.plan = BulkEditPlan(None)
        self.save_button.setEnabled(False)
        self.table.setRowCount(0)
        options = dict(operation=self.operation.currentData(), match_mode=self.match_mode.currentData(),
            find=self.find_text.toPlainText(), replacement=self.replacement.toPlainText(),
            service_day=self._service_day, effective_on=date.today(), actor=self._actor,
            recorded_at=datetime.now(UTC), revision_id=uuid4())
        self.preview_button.setEnabled(False)
        try:
            self.plan = run_progress_task(lambda: plan_bulk_edit(self._snapshot, self._targets, self._labels,
                **options),
                message="선택한 셀의 변경 내용을 확인하는 중입니다.", parent=self)
        except Exception as error:
            self.summary.setText(user_error(error))
            return
        finally:
            self.preview_button.setEnabled(True)
        if (self.operation.currentData(), self.match_mode.currentData(), self.find_text.toPlainText(), self.replacement.toPlainText()) != (
            options["operation"], options["match_mode"], options["find"], options["replacement"]):
            self._invalidate()
            return
        self.table.setRowCount(len(self.plan.rows))
        for row, values in enumerate(self.plan.rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, column, item)
        self.table.resizeRowsToContents()
        self.summary.setText(f"선택 셀 {self.plan.selected_changes}개 변경 · 함께 반영되는 항목 포함 {len(self.plan.rows)}개"
                             if self.plan.record else "바뀌는 값이 없습니다. 찾을 내용과 일치 조건을 확인해주세요.")
        self.save_button.setEnabled(self.plan.record is not None)


class RosterBulkEditController:
    def __init__(self, session, *, dialog_type=RosterBulkEditDialog, message_box=QMessageBox):
        self._session, self._dialog_type, self._message_box = session, dialog_type, message_box

    @dialog_workflow
    def run(self, targets, labels, expected_revision_id, service_day, parent=None):
        try:
            roster = self._session.database.current_roster()
            if roster is None or roster.revision_id != expected_revision_id:
                raise RevisionConflictError()
            snapshot = self._session.database.snapshot(roster.revision_id)
            dialog = self._dialog_type(snapshot, targets, labels, service_day, parent)
            if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
                return RosterCellEditOutcome(RosterCellEditStatus.CANCELLED)
            if self._session.database.current_roster().revision_id != snapshot.revision_id:
                raise RevisionConflictError()
            record = dialog.plan.record
            if record is None:
                return RosterCellEditOutcome(RosterCellEditStatus.CANCELLED)
            backup = save_revision(self._session, record, parent=parent)
        except Exception as error:
            self._message_box.critical(parent, "일괄 변경을 저장하지 못했습니다", user_error(error))
            return RosterCellEditOutcome(RosterCellEditStatus.FAILED)
        if backup.warning_required:
            self._message_box.warning(parent, "변경은 저장됐지만 자동 백업에 실패했습니다",
                "명단과 변경 이력은 저장되었습니다. 백업 폴더 연결을 확인한 뒤 다시 백업해주세요.\n\n" + user_error(backup.error))
        saved = roster_from_historical_snapshot(import_candidate_snapshot(snapshot, record))
        return RosterCellEditOutcome(RosterCellEditStatus.COMPLETED,
            committed_roster_after_save(self._session, saved), backup)
