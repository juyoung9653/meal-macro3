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
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.domain.roster import Meal, RosterSnapshot
from src.importing import (
    ImportPurpose,
    ExistingHouseholdProposal,
    HouseholdGroupingDecision,
    HouseholdGroupingPlan,
    HouseholdMatchCriterion,
    IdentityResolution,
    ProfileField,
    ReconciliationDecision,
    ReconciliationDisposition,
    ReconciliationPlan,
    StagedPerson,
)

from .theme import PAUSED, application_stylesheet, register_application_fonts


_FIELD_LABELS = {
    ProfileField.NAME: "성명",
    ProfileField.BIRTH_DATE: "생년월일",
    ProfileField.SCHOOL_TYPE: "취학유형",
    ProfileField.SCHOOL_NAME: "학교명",
    ProfileField.GRADE: "학년",
    ProfileField.HOUSEHOLD_HEAD_NAME: "세대주",
    ProfileField.RELATIONSHIP_TO_CHILD: "관계",
    ProfileField.JURISDICTION: "관할동",
    ProfileField.PROVIDER: "제공기관",
    ProfileField.ROAD_ADDRESS: "도로명주소",
    ProfileField.DETAIL_ADDRESS: "상세주소",
    ProfileField.TELEPHONE: "전화번호",
    ProfileField.MOBILE_PHONE: "핸드폰",
    ProfileField.NOTE: "비고",
}
_DISPOSITION_LABELS = {
    ReconciliationDisposition.MATCHED: "자동 연결",
    ReconciliationDisposition.NEW: "신규",
    ReconciliationDisposition.REVIEW: "확인 필요",
}
_PLACEHOLDER = "__unresolved__"
_HOUSEHOLD_PLACEHOLDER = "__household_unresolved__"
_HOUSEHOLD_CRITERION_LABELS = {
    HouseholdMatchCriterion.MOBILE_PHONE: "핸드폰",
    HouseholdMatchCriterion.HOUSEHOLD_HEAD_NAME: "세대주 성명",
    HouseholdMatchCriterion.ADDRESS: "주소",
    HouseholdMatchCriterion.TELEPHONE: "전화번호",
}


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
        self.setWindowTitle("명단 가져오기 마지막 확인")
        self.resize(1420, 680)
        self.setMinimumSize(1080, 540)
        self._build_ui()
        self._refresh_accept_state()

    def decisions(self) -> dict[UUID, ReconciliationDecision]:
        if not self._all_resolved():
            raise ValueError("확인 필요 행의 사람 연결 방식을 모두 선택해야 합니다.")
        decisions: dict[UUID, ReconciliationDecision] = {}
        for staged in self._plan.staged_people:
            observation_id = staged.observation.observation_id
            combo = self._combos[observation_id]
            data = combo.currentData()
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
        decisions = {}
        for observation_id, combo in self._household_combos.items():
            household_id = combo.currentData()
            identity_data = self._combos[observation_id].currentData()
            if household_id == _HOUSEHOLD_PLACEHOLDER and identity_data != "new":
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
        title = QLabel("4. 명단에 저장할 내용 마지막 확인")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel(
            f"적용일 {self._plan.effective_on.isoformat()} · "
            + ("중식 명단의 사람 연결을 확인하세요. 기존 배송 대상자는 자동 비교합니다." if self._purpose is ImportPurpose.VACATION_LUNCH else "사람 처리와 제안된 가구 묶음을 확인하세요.")
        )
        subtitle.setObjectName("workspaceSubtitle")
        title_column.addWidget(title)
        title_column.addWidget(subtitle)
        header.addLayout(title_column)
        header.addStretch(1)
        self._summary = QLabel()
        self._summary.setObjectName("sectionLabel")
        header.addWidget(self._summary)
        layout.addLayout(header)

        self._table = QTableWidget(len(self._plan.staged_people), 10)
        self._table.setHorizontalHeaderLabels(
            (
                "분류",
                "원본 위치",
                "성명",
                "생년월일",
                "인적사항 변경",
                "식종 변경",
                "같은 사람 후보",
                "기존 가구 후보",
                "사람 처리",
                "가구 처리",
            )
        )
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        widths = (88, 128, 92, 100, 180, 130, 150, 100, 200, 220)
        for column, width in enumerate(widths):
            self._table.setColumnWidth(column, width)
        self._table.horizontalHeader().setStretchLastSection(True)
        for row, staged in enumerate(self._plan.staged_people):
            self._populate_row(row, staged)
        layout.addWidget(self._table, 1)

        if self._purpose is not ImportPurpose.VACATION_LUNCH:
            note = QLabel(
                "가구 묶기는 사람 기록을 합치는 기능이 아닙니다. "
                "기존 구성원 이름을 확인하고, 맞지 않으면 '이 사람만 별도 가구'를 선택하세요."
            )
            note.setObjectName("muted")
            layout.addWidget(note)
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "확인한 내용 저장"
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName(
            "primaryAction"
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self._buttons.rejected.connect(self.reject)
        self._buttons.accepted.connect(self._accept_if_resolved)
        layout.addWidget(self._buttons)

    def _populate_row(self, row: int, staged: StagedPerson) -> None:
        observation = staged.observation
        source_rows = ", ".join(str(number) for number in observation.source.row_numbers)
        profile_changes = ", ".join(
            _FIELD_LABELS[item.field]
            for item in staged.profile_differences
            if item.field in _FIELD_LABELS
        ) or "없음"
        if self._purpose is ImportPurpose.VACATION_LUNCH:
            profile_changes = "기존 정보 유지" if staged.disposition is ReconciliationDisposition.MATCHED else "신규 등록"
        meal_changes = " ".join(
            (
                *(f"+{meal.value}" for meal in staged.meals_added),
                *(f"−{meal.value}" for meal in staged.meals_removed),
            )
        ) or "없음"
        if self._purpose is ImportPurpose.VACATION_LUNCH:
            meal_changes = "중식 배송"
        identity_candidates = ", ".join(
            self._person_label(person_id)
            for person_id in staged.candidate_person_ids
        ) or "—"
        existing_household_proposal = (
            None
            if self._household_plan is None
            else self._household_plan.existing_proposal_for(
                observation.observation_id
            )
        )
        values = (
            _DISPOSITION_LABELS[staged.disposition],
            f"{observation.source.sheet_name} {source_rows}행",
            observation.name,
            observation.birth_date.isoformat() if observation.birth_date else "미입력",
            profile_changes,
            meal_changes,
            identity_candidates,
            (
                f"{len(existing_household_proposal.candidate_household_ids)}개"
                if existing_household_proposal is not None
                else "—"
            ),
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignCenter
                if column in {0, 2, 3, 7}
                else Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
            )
            if staged.disposition is ReconciliationDisposition.REVIEW:
                item.setBackground(QColor("#FFF1E3"))
                if column == 0:
                    item.setForeground(QColor(PAUSED))
            elif staged.disposition is ReconciliationDisposition.NEW and column == 0:
                item.setForeground(QColor("#16788A"))
            self._table.setItem(row, column, item)
        combo = self._decision_combo(staged)
        self._combos[observation.observation_id] = combo
        self._table.setCellWidget(row, 8, combo)
        household_combo = self._household_combo(staged)
        self._household_combos[observation.observation_id] = household_combo
        self._table.setCellWidget(row, 9, household_combo)
        self._sync_household_choice(observation.observation_id)

    def _decision_combo(self, staged: StagedPerson) -> QComboBox:
        combo = QComboBox()
        if staged.disposition is ReconciliationDisposition.MATCHED:
            assert staged.matched_person_id is not None
            combo.addItem(
                f"자동 연결 · {self._person_label(staged.matched_person_id)}",
                "auto",
            )
            if self._purpose is not ImportPurpose.VACATION_LUNCH:
                combo.addItem("이 행 제외", "ignore")
        elif staged.disposition is ReconciliationDisposition.NEW:
            combo.addItem("신규 사람으로 추가", "new")
            if self._purpose is not ImportPurpose.VACATION_LUNCH:
                combo.addItem("이 행 제외", "ignore")
        else:
            combo.addItem("사람 연결 방식 선택", _PLACEHOLDER)
            for person_id in staged.candidate_person_ids:
                combo.addItem(f"기존 연결 · {self._person_label(person_id)}", person_id)
            combo.addItem("별도 신규 사람으로 추가", "new")
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
        combo = QComboBox()
        observation_id = staged.observation.observation_id
        if self._household_plan is None:
            combo.addItem("가구 제안 없음", None)
            combo.setEnabled(False)
            return combo
        existing = self._household_plan.existing_proposal_for(observation_id)
        if existing is not None:
            if len(existing.candidate_household_ids) > 1:
                combo.addItem("기존 가구 선택 필요", _HOUSEHOLD_PLACEHOLDER)
            for household_id in existing.candidate_household_ids:
                combo.addItem(
                    f"기존 가구에 포함 · {self._household_label(household_id)}",
                    household_id,
                )
            combo.addItem(
                "이 사람만 별도 가구",
                self._household_plan.separate_household_ids[observation_id],
            )
            combo.setToolTip(self._existing_household_tooltip(existing))
            combo.setStyleSheet(
                "QComboBox { background: "
                + ("#FFF1E3" if len(existing.candidate_household_ids) > 1 else "#E8F5F6")
                + "; border: 1px solid "
                + (PAUSED if len(existing.candidate_household_ids) > 1 else "#9FCED3")
                + "; }"
            )
            combo.currentIndexChanged.connect(self._refresh_accept_state)
            return combo
        proposal = self._household_plan.proposal_for(observation_id)
        if proposal is None:
            combo.addItem(
                "별도 가구",
                self._household_plan.separate_household_ids[observation_id],
            )
            combo.setEnabled(False)
            return combo
        combo.addItem(
            f"같은 가구로 묶기 · {len(proposal.observation_ids)}명",
            proposal.shared_household_id,
        )
        combo.addItem(
            "이 사람만 별도 가구",
            proposal.separate_household_ids[observation_id],
        )
        criteria = " · ".join(
            f"{_HOUSEHOLD_CRITERION_LABELS[criterion]}: {value}"
            for criterion, value in proposal.match_values.items()
        )
        combo.setToolTip(f"모두 일치: {criteria}")
        combo.setStyleSheet(
            "QComboBox { background: #E8F5F6; border: 1px solid #9FCED3; }"
        )
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
            raise ValueError("최초 가져오기에는 기존 사람 후보가 있을 수 없습니다.")
        person = self._roster.person(person_id)
        birth = (
            "생년 미등록"
            if person.profile.birth_date is None
            else person.profile.birth_date.isoformat()
        )
        return f"{person.profile.name} · {birth} · {str(person_id)[:8]}"

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
        matched = len(self._plan.by_disposition(ReconciliationDisposition.MATCHED))
        new = len(self._plan.by_disposition(ReconciliationDisposition.NEW))
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
        )
        if self._purpose is ImportPurpose.VACATION_LUNCH:
            present_ids = {
                (staged.matched_person_id if combo.currentData() == "auto" else combo.currentData())
                for staged in self._plan.staged_people
                for combo in (self._combos[staged.observation.observation_id],)
                if combo.currentData() != "new"
            }
            omitted_count = len(self._lunch_delivering_ids - present_ids)
            self._summary.setText(self._summary.text() + f" · 중식 미배송 전환 {omitted_count}")
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            unresolved == 0 and self._all_resolved()
        )

    def _accept_if_resolved(self) -> None:
        if self._all_resolved():
            self.accept()
