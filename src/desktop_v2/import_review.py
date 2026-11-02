"""Import staging dialog that asks only for unresolved identity decisions."""

from __future__ import annotations

from uuid import UUID, uuid4

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.domain.roster import RosterSnapshot
from src.importing.revision import ImportPurpose
from src.importing.households import (
    ExistingHouseholdProposal,
    HouseholdGroupingDecision,
    HouseholdGroupingPlan,
    HouseholdMatchCriterion,
)
from src.importing.application import (
    IdentityResolution,
    ReconciliationDecision,
)
from src.importing.contracts import ProfileField
from src.importing.reconciliation import (
    ReconciliationDisposition,
    ReconciliationPlan,
    StagedPerson,
    ReviewReason,
)

from .theme import application_stylesheet, register_application_fonts


_PLACEHOLDER = "__unresolved__"
_HOUSEHOLD_PLACEHOLDER = "__household_unresolved__"
_REVIEW_LABELS = {
    ReviewReason.SAME_NAME_HOUSEHOLD_HEAD: "이름·세대주명 일치",
    ReviewReason.DUPLICATE_SOURCE_IDENTITY: "원본 중복",
    ReviewReason.DUPLICATE_ROSTER_IDENTITY: "기존 후보 여러 명",
    ReviewReason.MISSING_MOBILE_PHONE: "핸드폰 없음",
    ReviewReason.MISSING_BIRTH_DATE: "생년월일 없음",
    ReviewReason.NAME_COLLISION: "이름 일치",
    ReviewReason.BIRTH_DATE_COLLISION: "생년월일 일치",
}
_HOUSEHOLD_CRITERION_LABELS = {
    HouseholdMatchCriterion.MOBILE_PHONE: "핸드폰",
    HouseholdMatchCriterion.HOUSEHOLD_HEAD_NAME: "세대주 성명",
    HouseholdMatchCriterion.ADDRESS: "주소",
    HouseholdMatchCriterion.TELEPHONE: "전화번호",
}


def import_decision_rows(
    plan: ReconciliationPlan,
    household_plan: HouseholdGroupingPlan | None,
) -> tuple[StagedPerson, ...]:
    """Only identity conflicts and ambiguous households for new people need input."""
    household_required = set(
        () if household_plan is None else household_plan.required_decision_ids
    )
    return tuple(
        item for item in plan.staged_people
        if item.disposition is ReconciliationDisposition.REVIEW
        or (
            item.disposition is ReconciliationDisposition.NEW
            and item.observation.observation_id in household_required
        )
    )


class ImportReviewDialog(QDialog):
    """Collect link/new/ignore decisions without committing the revision."""

    def __init__(
        self,
        roster: RosterSnapshot | None,
        plan: ReconciliationPlan,
        parent: QWidget | None = None,
        *,
        household_plan: HouseholdGroupingPlan | None = None,
        purpose: ImportPurpose = ImportPurpose.DATABASE,
        lunch_delivering_ids: tuple[UUID, ...] = (),
    ) -> None:
        super().__init__(parent)
        if roster is not None and not isinstance(roster, RosterSnapshot):
            raise TypeError("roster는 RosterSnapshot 또는 None이어야 합니다.")
        if not isinstance(plan, ReconciliationPlan):
            raise TypeError("plan은 ReconciliationPlan이어야 합니다.")
        roster_revision_id = None if roster is None else roster.revision_id
        if roster_revision_id != plan.roster_revision_id:
            raise ValueError("확인 중인 명단이 변경되었습니다. 검토 화면을 다시 열어주세요.")
        if household_plan is not None:
            if not isinstance(household_plan, HouseholdGroupingPlan):
                raise TypeError(
                    "household_plan은 HouseholdGroupingPlan 또는 None이어야 합니다."
                )
            if household_plan.effective_on != plan.effective_on:
                raise ValueError("가구 묶기와 대상자 확인의 적용일이 다릅니다.")
            observation_ids = {
                item.observation.observation_id for item in plan.staged_people
            }
            if set(household_plan.observation_ids) != observation_ids:
                raise ValueError("가구 묶기와 대상자 확인의 명단이 다릅니다.")
        self._roster = roster
        self._plan = plan
        self._household_plan = household_plan
        self._decision_rows = import_decision_rows(plan, household_plan)
        self._household_required = frozenset(() if household_plan is None else household_plan.required_decision_ids)
        self._purpose = purpose
        self._lunch_delivering_ids = frozenset(lunch_delivering_ids)
        self._combos: dict[UUID, QComboBox] = {}
        self._household_combos: dict[UUID, QComboBox] = {}
        self._new_person_ids: dict[UUID, UUID] = {
            item.observation.observation_id: uuid4()
            for item in plan.staged_people
            if item.disposition is not ReconciliationDisposition.MATCHED
        }
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("명단 불러오기 · 반영 방법 선택")
        screen = self.screen().availableGeometry()
        self.setMinimumSize(min(640, screen.width()), min(420, screen.height()))
        self.resize(min(960, screen.width() - 32), min(640, screen.height() - 48))
        self._build_ui()
        self._refresh_accept_state()

    def decisions(self) -> dict[UUID, ReconciliationDecision]:
        if not self._all_resolved():
            raise ValueError("각 행의 반영 방법과 필요한 가구를 모두 선택해주세요.")
        decisions: dict[UUID, ReconciliationDecision] = {}
        for staged in self._plan.staged_people:
            observation_id = staged.observation.observation_id
            combo = self._combos.get(observation_id)
            data = (
                combo.currentData() if combo is not None
                else "new" if staged.disposition is ReconciliationDisposition.NEW
                else "auto"
            )
            if data == "auto":
                continue
            if data == "ignore":
                decisions[observation_id] = ReconciliationDecision(
                    observation_id,
                    IdentityResolution.IGNORE,
                )
                continue
            if data == "new":
                decisions[observation_id] = ReconciliationDecision(
                    observation_id,
                    IdentityResolution.CREATE_NEW,
                    self._new_person_ids[observation_id],
                )
                continue
            if isinstance(data, UUID):
                decisions[observation_id] = ReconciliationDecision(
                    observation_id,
                    IdentityResolution.LINK_EXISTING,
                    data,
                )
                continue
            raise ValueError(f"알 수 없는 staging 결정입니다: {data!r}")
        return decisions

    def household_decisions(self) -> dict[UUID, HouseholdGroupingDecision]:
        if self._household_plan is None:
            return {}
        decisions = self._household_plan.default_decisions()
        for observation_id, combo in self._household_combos.items():
            household_id = combo.currentData()
            identity_data = self._combos[observation_id].currentData()
            if identity_data != "new":
                continue
            if not isinstance(household_id, UUID):
                raise ValueError("가구 처리 결정을 확인할 수 없습니다.")
            decisions[observation_id] = HouseholdGroupingDecision(
                observation_id,
                household_id,
            )
        return decisions

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        header = QHBoxLayout()
        title_column = QVBoxLayout()
        title = QLabel("명단 반영 방법을 선택해주세요")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel(
            f"적용일 {self._plan.effective_on.isoformat()} · "
            + "이름·핸드폰이 한 사람과 일치하면 자동 연결합니다. 이름·세대주명이 같거나 연결이 모호한 행은 확인이 필요합니다. 같은 사람이면 기존 대상자에 반영하고, 다른 사람이면 새 대상자로 등록하세요. ‘확인 행 모두 신규 등록’은 표의 모든 행을 별개의 새 사람으로 선택합니다."
            + (" 중식 명단에 연결되지 않은 기존 배송자는 중지됩니다."
               if self._purpose is ImportPurpose.VACATION_LUNCH else "")
        )
        subtitle.setObjectName("workspaceSubtitle")
        subtitle.setWordWrap(True)
        title_column.addWidget(title)
        title_column.addWidget(subtitle)
        header.addLayout(title_column)
        header.addStretch(1)
        self._summary = QLabel()
        self._summary.setObjectName("sectionLabel")
        header.addWidget(self._summary)
        layout.addLayout(header)

        self._table = QTableWidget(len(self._decision_rows), 7)
        self._table.setHorizontalHeaderLabels((
            "번호",
            "성명",
            "핸드폰",
            "세대주명",
            "확인 사유",
            "행 반영 방법",
            "기존 가구 선택",
        ))
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(4, 170)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self._table.setColumnHidden(6, True)
        for row, staged in enumerate(self._decision_rows):
            self._populate_row(row, staged)
        self._table.resizeRowsToContents()
        for row in range(self._table.rowCount()):
            self._table.setRowHeight(row, max(56, self._table.rowHeight(row)))
        layout.addWidget(self._table, 1)

        self._household_note = QLabel("가구 묶기 기준에 일치하는 기존 가구를 확인합니다.")
        self._household_note.setObjectName("muted")
        self._household_note.setWordWrap(True)
        self._household_note.hide()
        layout.addWidget(self._household_note)
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "선택한 내용으로 불러오기"
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName(
            "primaryAction"
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self._buttons.rejected.connect(self.reject)
        self._buttons.accepted.connect(self._accept_if_resolved)
        self._skip_all = QPushButton("확인 행 모두 신규 등록")
        self._skip_all.setObjectName("quietAction")
        self._skip_all.setToolTip("동일인 연결을 건너뛰고 표의 모든 행을 별개의 새 사람으로 선택합니다. 자동 연결된 행은 유지하며, 최종 불러오기 전 개별 선택을 다시 바꿀 수 있습니다.")
        self._skip_all.clicked.connect(self._skip_all_rows)
        self._buttons.addButton(self._skip_all, QDialogButtonBox.ButtonRole.ActionRole)
        layout.addWidget(self._buttons)

    def _skip_all_rows(self) -> None:
        for observation_id, combo in self._combos.items():
            blocked = combo.blockSignals(True)
            try:
                combo.setCurrentIndex(combo.findData("new"))
            finally:
                combo.blockSignals(blocked)
            self._sync_household_choice(observation_id)
        self._refresh_accept_state()

    def _populate_row(self, row: int, staged: StagedPerson) -> None:
        observation = staged.observation
        values = (
            str(row + 1),
            observation.name,
            observation.profile_values.get(ProfileField.MOBILE_PHONE) or "미입력",
            observation.profile_values.get(ProfileField.HOUSEHOLD_HEAD_NAME) or "미입력",
            " · ".join(_REVIEW_LABELS[reason] for reason in staged.review_reasons) or "가구 확인",
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if staged.disposition is ReconciliationDisposition.REVIEW:
                item.setBackground(QColor("#FFF1E3"))
            self._table.setItem(row, column, item)
        combo = self._decision_combo(staged)
        self._combos[observation.observation_id] = combo
        self._table.setCellWidget(row, 5, combo)
        if observation.observation_id in self._household_required:
            household_combo = self._household_combo(staged)
            self._household_combos[observation.observation_id] = household_combo
            self._table.setCellWidget(row, 6, household_combo)
            self._sync_household_choice(observation.observation_id)
        else:
            self._table.setItem(row, 6, QTableWidgetItem("자동 처리"))
        combo.ensurePolished()
        self._table.setRowHeight(row, max(40, combo.sizeHint().height() + 1))

    def _decision_combo(self, staged: StagedPerson) -> QComboBox:
        combo = QComboBox()
        if staged.disposition is ReconciliationDisposition.MATCHED:
            assert staged.matched_person_id is not None
            combo.addItem(
                f"기존 대상자에 자동 반영 · {self._person_label(staged.matched_person_id)}",
                "auto",
            )
        elif staged.disposition is ReconciliationDisposition.NEW:
            combo.addItem("새 대상자로 등록", "new")
        else:
            combo.addItem("반영 방법 선택", _PLACEHOLDER)
            for person_id in staged.candidate_person_ids:
                combo.addItem(f"기존 대상자에 반영 · {self._person_label(person_id)}", person_id)
            combo.addItem("새 대상자로 등록", "new")
        if self._purpose is not ImportPurpose.VACATION_LUNCH:
            combo.addItem("이 행 제외", "ignore")
        combo.currentIndexChanged.connect(
            lambda _index, observation_id=staged.observation.observation_id: (
                self._sync_household_choice(observation_id)
            )
        )
        combo.currentIndexChanged.connect(self._refresh_accept_state)
        return combo

    def _household_combo(self, staged: StagedPerson) -> QComboBox:
        # Only genuinely ambiguous existing households reach this control.
        combo = QComboBox()
        observation_id = staged.observation.observation_id
        existing = self._household_plan.existing_proposal_for(observation_id)
        combo.addItem("기존 가구 선택 필요", _HOUSEHOLD_PLACEHOLDER)
        for household_id in existing.candidate_household_ids:
            combo.addItem(
                f"기존 가구에 포함 · {self._household_label(household_id)}", household_id)
        combo.addItem("이 사람만 별도 가구", self._household_plan.separate_household_ids[observation_id])
        combo.setToolTip(self._existing_household_tooltip(existing))
        combo.currentIndexChanged.connect(self._refresh_accept_state)
        return combo

    def _sync_household_choice(self, observation_id: UUID) -> None:
        household_combo = self._household_combos.get(observation_id)
        identity_combo = self._combos.get(observation_id)
        if household_combo is None or identity_combo is None:
            return
        plan_has_choice = (
            self._household_plan is not None
            and (
                self._household_plan.proposal_for(observation_id) is not None
                or self._household_plan.existing_proposal_for(observation_id)
                is not None
            )
        )
        household_combo.setEnabled(
            plan_has_choice and identity_combo.currentData() == "new"
        )

    def _household_label(self, household_id: UUID) -> str:
        if self._roster is None:
            raise ValueError("기존 가구 후보에는 기존 roster가 필요합니다.")
        members = tuple(
            sorted(
                self._roster.household_members(
                    household_id,
                    on=self._plan.effective_on,
                ),
                key=lambda item: (item.profile.name, str(item.person_id)),
            )
        )
        if not members:
            return f"구성원 없음 · {str(household_id)[:8]}"
        return (
            members[0].profile.name
            if len(members) == 1
            else f"{members[0].profile.name} 외 {len(members) - 1}명"
        )

    def _existing_household_tooltip(
        self,
        proposal: ExistingHouseholdProposal,
    ) -> str:
        if self._roster is None:
            raise ValueError("기존 가구 후보에는 기존 roster가 필요합니다.")
        matched = " · ".join(
            f"{_HOUSEHOLD_CRITERION_LABELS[criterion]}: {value}"
            for criterion, value in proposal.match_values.items()
        )
        households = []
        for household_id in proposal.candidate_household_ids:
            members = self._roster.household_members(
                household_id,
                on=self._plan.effective_on,
            )
            households.append(
                ", ".join(member.profile.name for member in members)
                or str(household_id)[:8]
            )
        return f"모두 일치: {matched}\n기존 구성원: " + " / ".join(households)

    def _person_label(self, person_id: UUID) -> str:
        if self._roster is None:
            raise ValueError("최초 불러오기에는 기존 사람 후보가 있을 수 없습니다.")
        person = self._roster.person(person_id)
        birth = (
            "생년 미등록"
            if person.profile.birth_date is None
            else person.profile.birth_date.isoformat()
        )
        return f"{person.profile.name} · {person.profile.mobile_phone or '핸드폰 없음'} · {person.profile.household_head_name or '세대주명 없음'} · {person.profile.school_name or '학교 없음'} · {birth}"

    def _all_resolved(self) -> bool:
        identities_resolved = all(
            combo.currentData() != _PLACEHOLDER
            for combo in self._combos.values()
        )
        households_resolved = all(
            self._combos[observation_id].currentData() != "new"
            or combo.currentData() != _HOUSEHOLD_PLACEHOLDER
            for observation_id, combo in self._household_combos.items()
        )
        if self._purpose is ImportPurpose.VACATION_LUNCH:
            return identities_resolved and all(combo.currentData() != "ignore" for combo in self._combos.values())
        return identities_resolved and households_resolved

    def _refresh_accept_state(self, *_args) -> None:
        if not hasattr(self, "_buttons"):
            return
        identity_unresolved = sum(
            combo.currentData() == _PLACEHOLDER
            for combo in self._combos.values()
        )
        household_unresolved = sum(
            self._combos[observation_id].currentData() == "new"
            and combo.currentData() == _HOUSEHOLD_PLACEHOLDER
            for observation_id, combo in self._household_combos.items()
        )
        unresolved = identity_unresolved + household_unresolved
        show_households = any(
            self._combos[observation_id].currentData() == "new"
            for observation_id in self._household_combos)
        self._table.setColumnHidden(6, not show_households)
        self._household_note.setVisible(show_households)
        matched = len(self._plan.by_disposition(ReconciliationDisposition.MATCHED))
        new = sum(
            self._combos[staged.observation.observation_id].currentData() == "new"
            if staged.observation.observation_id in self._combos
            else staged.disposition is ReconciliationDisposition.NEW
            for staged in self._plan.staged_people
        )
        review = len(self._plan.by_disposition(ReconciliationDisposition.REVIEW))
        household_groups = (
            0
            if self._household_plan is None
            else len(self._household_plan.proposals)
        )
        existing_candidates = (
            0
            if self._household_plan is None
            else len(self._household_plan.existing_proposals)
        )
        self._summary.setText(
            f"자동 {matched} · 신규 {new} · 확인 {review}"
            + (f" · 가구 묶음 {household_groups}" if self._household_plan else "")
            + (f" · 기존 가구 후보 {existing_candidates}" if existing_candidates else "")
            + (f" · 미선택 {unresolved}" if unresolved else "")
            + f" · 제외 {sum(combo.currentData() == 'ignore' for combo in self._combos.values())}"
        )
        if self._purpose is ImportPurpose.VACATION_LUNCH:
            present_ids = {
                staged.matched_person_id
                for staged in self._plan.staged_people
                if staged.disposition is ReconciliationDisposition.MATCHED
            }
            present_ids.update(
                combo.currentData() for combo in self._combos.values()
                if isinstance(combo.currentData(), UUID)
            )
            omitted_count = len(self._lunch_delivering_ids - present_ids)
            self._summary.setText(self._summary.text() + f" · 중식 미배송 전환 {omitted_count}")
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            unresolved == 0 and self._all_resolved()
        )

    def _accept_if_resolved(self) -> None:
        if self._all_resolved():
            self.accept()
