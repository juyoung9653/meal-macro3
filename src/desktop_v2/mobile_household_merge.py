"""Review conflicts in an automatic mobile household merge before saving."""
from PySide6.QtWidgets import QDialog, QDialogButtonBox

from src.domain.household_shared import household_shared_components, household_shared_rows
from src.domain.mobile_household_merge import (
    MobileHouseholdConflict, needs_mobile_household_merge,
    prepare_mobile_household_revision, with_mobile_shared_values,
)
from src.domain.projections import roster_from_historical_snapshot
from .dialog_workflow import DialogRequest, dialog_workflow


@dialog_workflow
def review_mobile_household_merge(session, record, parent=None, *, dialog_type=None):
    if record.metadata.parent_revision_id is None or not needs_mobile_household_merge(record):
        return record
    reader = getattr(session.database, "snapshot", None)
    if reader is None:
        return record
    from .household_shared_columns import HouseholdSharedColumnDialog
    dialog_type = dialog_type or HouseholdSharedColumnDialog
    snapshot = reader(record.metadata.parent_revision_id)
    while True:
        try:
            return prepare_mobile_household_revision(snapshot, record)
        except MobileHouseholdConflict as conflict:
            key, on = conflict.column_key, conflict.effective_on
            candidate, household_ids = conflict.candidate, conflict.household_ids
        roster = roster_from_historical_snapshot(candidate)
        components = household_shared_components(roster, key, snapshot=candidate)
        rows = tuple(row for row in household_shared_rows(roster, key, on, snapshot=candidate)
                     if row.household_id in household_ids and any(v is None for v in row.values))
        label = {"sequence": "배송순서", "delivery_settings": "배송 설정",
                 "status:breakfast": "조식", "status:lunch": "중식", "status:dinner": "석식"}.get(key, components[0].label)
        dialog = dialog_type(label, True, components, rows, on, parent)
        dialog.setWindowTitle("가구 자동 합치기 · 공용 값 확인 · " + label)
        dialog.enabled.setEnabled(False)
        dialog.note.setText(
            "가구 묶기 기준이 일치하는 가구를 합칩니다. 연결일과 관계없이 즉시 적용합니다. "
            "왼쪽 값 셀을 선택하거나 오른쪽에 공용 값을 입력해주세요. "
            "모든 칼럼을 확인한 뒤 함께 저장하며 중간에 취소하면 아무것도 저장하지 않습니다."
        )
        dialog.buttons.button(QDialogButtonBox.StandardButton.Save).setText("다음")

        def validate(enabled, values):
            draft = with_mobile_shared_values(record, key, values)
            try:
                prepare_mobile_household_revision(snapshot, draft)
            except MobileHouseholdConflict as remaining:
                if remaining.column_key == key:
                    raise ValueError("이 칼럼의 공용 값을 모두 정해주세요.") from remaining

        dialog.validation_callback = validate
        if (yield DialogRequest(dialog, session)) != QDialog.DialogCode.Accepted:
            return None
        if session.database.current_roster().revision_id != snapshot.revision_id:
            raise ValueError("검토 중 명단이 변경되었습니다. 최신 명단에서 다시 확인해주세요.")
        record = with_mobile_shared_values(record, key, dialog.selected_values())
