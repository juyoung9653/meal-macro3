"""Driver/meal templates use source projections and the shared protected exporter."""

import os
from datetime import date
from io import BytesIO
from types import SimpleNamespace
from uuid import UUID, uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import msoffcrypto
import openpyxl
import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.current_sheet_export import (
    CurrentSheetExportController,
    TextWorkbookSnapshot,
    build_current_sheet_workbook,
)
from src.desktop_v2.driver_households import DriverHouseholdsDialog
from src.desktop_v2.driver_households_export import (
    TEMPLATE_HEADERS,
    build_driver_households_template_snapshot,
)
from src.desktop_v2.roster_window import RosterWindow
from src.domain.delivery_routing import Driver, RoutingState
from src.domain.roster import DeliveryAssignment, HouseholdMembership, Meal
from src.services.driver_households import (
    DriverHouseholdRow,
    build_driver_household_rows,
)
from tests.test_driver_households import DAY, person, roster, uid
from tests.test_desktop_v2_roster import _roster


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _row(
    driver_id: UUID | None,
    driver_name: str,
    member: str,
    meal: Meal,
    sequence: str = "",
    *,
    household_id: UUID | None = None,
    contact: str = "010-0000",
) -> DriverHouseholdRow:
    return DriverHouseholdRow(
        household_id or uuid4(),
        driver_id,
        driver_name,
        (uuid4(),),
        (member,),
        (meal,),
        "성남시 분당구",
        (contact,),
        (sequence,) if sequence else (),
        has_empty_sequence=not sequence,
    )


def _merged(*meal_rows: DriverHouseholdRow) -> DriverHouseholdRow:
    first = meal_rows[0]
    return DriverHouseholdRow(
        first.household_key,
        first.driver_id,
        first.driver_name,
        tuple(person_id for row in meal_rows for person_id in row.member_ids),
        tuple(name for row in meal_rows for name in row.member_names),
        tuple(row.meals[0] for row in meal_rows),
        first.address,
        tuple(contact for row in meal_rows for contact in row.contacts),
        tuple(sequence for row in meal_rows for sequence in row.sequences),
        meal_rows,
        any(row.has_empty_sequence for row in meal_rows),
    )


def _notices(*, overwrite=QMessageBox.StandardButton.No):
    errors, successes = [], []
    return SimpleNamespace(
        critical=lambda *args: errors.append(args),
        information=lambda *args: successes.append(args),
        question=lambda *args: overwrite,
        errors=errors,
        successes=successes,
    )


def test_dialog_action_uses_unfiltered_source_and_only_include_hidden(app):
    driver = Driver(uuid4(), "홍길동")
    household = uuid4()
    dinner = _row(
        driver.driver_id, driver.name, "저녁 대상", Meal.DINNER, "10",
        household_id=household, contact="010-1111",
    )
    breakfast = _row(
        driver.driver_id, driver.name, "아침 대상", Meal.BREAKFAST, "2",
        household_id=household, contact="010-2222",
    )
    visible = _merged(dinner, breakfast)
    hidden = _row(driver.driver_id, driver.name, "숨김 대상", Meal.LUNCH, "3")

    def rows_provider(include_hidden):
        return (visible, hidden) if include_hidden else (visible,)

    dialog = DriverHouseholdsDialog(
        (visible,),
        service_day=date(2026, 10, 15),
        drivers=(driver,),
        rows_provider=rows_provider,
    )
    captured = []
    dialog.export_requested.connect(captured.append)
    try:
        dialog._set_column_search("members", "일치하지 않음")
        dialog._set_column_values("meals", ())
        dialog.split_button.click()
        dialog.table.sortItems(1, Qt.SortOrder.DescendingOrder)
        dialog.template_export_button.click()
        first = captured[-1]
        assert isinstance(first, TextWorkbookSnapshot)
        assert tuple(sheet.sheet_name for sheet in first.sheets) == (
            "홍길동-석식", "홍길동-조식",
        )
        assert first.sheets[0].rows[0][1:] == (
            "저녁 대상", "석식", "10", "성남시 분당구", "010-1111", "", "",
        )
        assert first.sheets[1].rows[0][1:] == (
            "아침 대상", "조식", "2", "성남시 분당구", "010-2222", "", "",
        )

        dialog.include_hidden_checkbox.click()
        dialog.template_export_button.click()
        assert tuple(sheet.sheet_name for sheet in captured[-1].sheets) == (
            "홍길동-석식", "홍길동-조식", "홍길동-중식",
        )
        assert dialog.template_export_button.objectName() == "quietAction"
    finally:
        dialog.close()


def test_template_action_reaches_roster_and_application_common_gate(app, tmp_path):
    session = object()
    calls = []

    class PasswordController:
        def __init__(self, current_session):
            assert current_session is session

        def ensure_configured(self, parent):
            calls.append(("password", parent))
            return SimpleNamespace(
                completed=True,
                roster=None,
                setting=SimpleNamespace(password="xlsx-password"),
            )

    class SheetController:
        def run(self, snapshot, password, parent, *, directory):
            calls.append(("export", snapshot, password, parent, directory))

    window = RosterWindow(_roster())
    controller = DesktopStartupController(
        settings=SimpleNamespace(document_output_directory=tmp_path),
        export_password_controller_type=PasswordController,
        current_sheet_export_controller_type=SheetController,
    )
    window.driver_households_export_requested.connect(
        lambda snapshot, dialog: controller._run_current_sheet_export(
            session, window, snapshot, parent=dialog
        )
    )

    def export_and_close():
        dialog = window.findChild(DriverHouseholdsDialog)
        assert dialog is not None and dialog.isVisible()
        dialog.template_export_button.click()
        dialog.reject()

    try:
        QTimer.singleShot(0, export_and_close)
        window._show_driver_households()
        assert calls[0][0] == "password"
        assert calls[1][0] == "export"
        assert isinstance(calls[1][1], TextWorkbookSnapshot)
        assert calls[1][2] == "xlsx-password"
        assert calls[0][1] is calls[1][3]
        assert calls[1][4] == tmp_path
    finally:
        window.close()


def test_service_generated_meal_projections_keep_actual_members_and_contacts():
    first = person(101, "저녁·아침", phone="010-101")
    second = person(102, "아침·점심", phone="010-102")
    household = uid(150)
    service_rows = build_driver_household_rows(
        roster(
            first,
            second,
            memberships=(
                HouseholdMembership(first.person_id, household),
                HouseholdMembership(second.person_id, household),
            ),
            meals={
                first.person_id: (Meal.DINNER, Meal.BREAKFAST),
                second.person_id: (Meal.BREAKFAST, Meal.LUNCH),
            },
            assignments=(
                DeliveryAssignment(first.person_id, Meal.DINNER, "기사", "3"),
                DeliveryAssignment(first.person_id, Meal.BREAKFAST, "기사", "2"),
                DeliveryAssignment(second.person_id, Meal.BREAKFAST, "기사", "1"),
                DeliveryAssignment(second.person_id, Meal.LUNCH, "기사", "4"),
            ),
        ),
        routing_state=RoutingState(()),
        on=DAY,
    )
    snapshot = build_driver_households_template_snapshot(service_rows)
    assert tuple(sheet.sheet_name for sheet in snapshot.sheets) == (
        "기사-석식", "기사-조식", "기사-중식",
    )
    assert snapshot.sheets[0].rows[0][1:4] == ("저녁·아침", "석식", "3")
    assert snapshot.sheets[0].rows[0][5] == "010-101"
    assert snapshot.sheets[1].rows[0][1:4] == (
        "저녁·아침, 아침·점심", "조식", "1, 2",
    )
    assert snapshot.sheets[1].rows[0][5] == "010-101, 010-102"
    assert snapshot.sheets[2].rows[0][1:4] == ("아침·점심", "중식", "4")
    assert snapshot.sheets[2].rows[0][5] == "010-102"


def test_numeric_sequence_sort_preserves_literals_and_blank_last():
    driver_id = uuid4()
    rows = (
        _row(driver_id, "기사A", "=열", Meal.DINNER, "10"),
        _row(driver_id, "기사A", "+둘", Meal.DINNER, "2"),
        _row(driver_id, "기사A", "@하나", Meal.DINNER, "001"),
        _row(driver_id, "기사A", "-빈칸", Meal.DINNER),
    )
    snapshot = build_driver_households_template_snapshot(rows)
    assert tuple(row[3] for row in snapshot.sheets[0].rows) == (
        "001", "2", "10", "",
    )

    workbook = build_current_sheet_workbook(snapshot)
    data = BytesIO()
    workbook.save(data)
    workbook.close()
    loaded = openpyxl.load_workbook(data)
    try:
        sheet = loaded.active
        assert tuple(sheet.values)[:-1] == (
            TEMPLATE_HEADERS, *snapshot.sheets[0].rows[:-1],
        )
        assert tuple(sheet.values)[-1][:3] == snapshot.sheets[0].rows[-1][:3]
        assert tuple(sheet.values)[-1][3] is None
        assert sheet["D2"].value == "001"
        assert sheet["B2"].data_type == "s"
        assert sheet["B3"].data_type == "s"
        assert sheet["B4"].data_type == "s"
        assert sheet["B5"].data_type == "s"
    finally:
        loaded.close()


def test_driver_identity_and_case_insensitive_long_title_collisions_stay_separate():
    first_id, second_id = uuid4(), uuid4()
    long_name = "가나다라마바사아자차카타파하" * 3 + "/기사"
    rows = (
        _row(first_id, long_name, "등록1", Meal.DINNER, "1"),
        _row(second_id, long_name, "등록2", Meal.DINNER, "2"),
        _row(None, long_name, "이름전용", Meal.DINNER, "3"),
        _row(uuid4(), long_name.swapcase(), "대소문자", Meal.DINNER, "4"),
    )
    snapshot = build_driver_households_template_snapshot(rows)
    titles = tuple(sheet.sheet_name for sheet in snapshot.sheets)
    assert len(titles) == 4
    assert len({title.casefold() for title in titles}) == 4
    assert all(len(title) <= 31 and title.endswith("-석식") for title in titles)
    assert all(not set("[]:*?/\\") & set(title) for title in titles)
    assert tuple(sheet.rows[0][1] for sheet in snapshot.sheets) == (
        "등록1", "등록2", "이름전용", "대소문자",
    )


def test_selected_driver_scope_and_unassigned_sheet():
    selected, other = uuid4(), uuid4()
    snapshot = build_driver_households_template_snapshot(
        (
            _row(other, "다른 기사", "제외", Meal.DINNER, "1"),
            _row(selected, "선택 기사", "포함", Meal.LUNCH, "2"),
            _row(None, "", "미지정", Meal.DINNER, "3"),
        ),
        selected_driver_id=selected,
    )
    assert tuple(sheet.sheet_name for sheet in snapshot.sheets) == ("선택 기사-중식",)
    assert snapshot.sheets[0].rows[0][1] == "포함"

    unassigned = build_driver_households_template_snapshot(
        (_row(None, "", "미지정", Meal.DINNER, "3"),)
    )
    assert unassigned.sheets[0].sheet_name == "미지정-석식"
    assert unassigned.sheets[0].rows[0][0] == "미지정"


def test_empty_export_is_one_header_only_sheet():
    snapshot = build_driver_households_template_snapshot(())
    assert len(snapshot.sheets) == 1
    assert snapshot.sheets[0].headers == TEMPLATE_HEADERS
    assert snapshot.sheets[0].rows == ()
    workbook = build_current_sheet_workbook(snapshot)
    try:
        assert len(workbook.sheetnames) == 1
        assert tuple(workbook.active.values) == (TEMPLATE_HEADERS,)
    finally:
        workbook.close()


def test_multi_meal_direct_row_without_exact_projections_is_rejected():
    driver_id = uuid4()
    malformed = DriverHouseholdRow(
        uuid4(), driver_id, "기사A", (uuid4(),), ("대상",),
        (Meal.DINNER, Meal.BREAKFAST), "주소", ("010",), ("1",),
    )
    with pytest.raises(ValueError, match="식종별 가구원 정보"):
        build_driver_households_template_snapshot((malformed,))


def test_real_encrypted_multi_sheet_roundtrip_uses_shared_controller(app, tmp_path):
    driver_id = uuid4()
    snapshot = build_driver_households_template_snapshot((
        _row(driver_id, "기사A", "저녁", Meal.DINNER, "001"),
        _row(driver_id, "기사A", "아침", Meal.BREAKFAST, "2"),
    ))
    target = tmp_path / "driver-meals.xlsx"
    messages = _notices()
    dialog_titles = []

    def choose_file(*args, **kwargs):
        dialog_titles.append(args[1])
        return str(target), ""

    result = CurrentSheetExportController(
        message_box=messages,
        file_dialog=SimpleNamespace(getSaveFileName=choose_file),
    ).run(snapshot, "driver-meal-password", directory=tmp_path)
    assert result is not None and result.password_protected
    assert not messages.errors and len(messages.successes) == 1
    assert dialog_titles == ["기사·식종별 가구 Excel 내보내기"]

    with target.open("rb") as source:
        office = msoffcrypto.OfficeFile(source)
        assert office.is_encrypted()
        office.load_key(password="driver-meal-password")
        plaintext = BytesIO()
        office.decrypt(plaintext)
    workbook = openpyxl.load_workbook(plaintext)
    try:
        assert workbook.sheetnames == ["기사A-석식", "기사A-조식"]
        assert workbook["기사A-석식"]["D2"].value == "001"
        assert workbook["기사A-조식"]["B2"].value == "아침"
    finally:
        workbook.close()


@pytest.mark.parametrize("mode", ["missing_password", "cancel", "overwrite_cancel"])
def test_shared_gate_does_not_publish_on_password_or_file_cancel(app, tmp_path, mode):
    snapshot = build_driver_households_template_snapshot((
        _row(uuid4(), "기사A", "대상", Meal.DINNER, "1"),
    ))
    target = tmp_path / "existing.xlsx"
    if mode == "overwrite_cancel":
        target.write_bytes(b"original")
    messages = _notices()
    result = CurrentSheetExportController(
        message_box=messages,
        file_dialog=SimpleNamespace(
            getSaveFileName=lambda *args, **kwargs: (
                "" if mode == "cancel" else str(target), ""
            )
        ),
    ).run(
        snapshot,
        "" if mode == "missing_password" else "driver-meal-password",
        directory=tmp_path,
    )
    assert result is None and not messages.successes
    assert bool(messages.errors) is (mode == "missing_password")
    if mode == "missing_password":
        assert messages.errors[0][1] == "기사·식종별 가구 Excel을 내보내지 못했습니다"
    if mode == "overwrite_cancel":
        assert target.read_bytes() == b"original"
    else:
        assert not target.exists()
