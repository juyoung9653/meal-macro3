"""Settings hub and computer-local document-folder settings checks."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2 import (
    DefaultDocumentDirectoryDialog,
    DesktopStartupController,
    SettingsAction,
    SettingsHubDialog,
)
from src.storage import AutomaticBackupResult
from src.storage.database_backup import BackupPasswordRotation


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_settings_hub_has_db_and_computer_local_jobs(app):
    dialog = SettingsHubDialog()
    dialog.show()
    app.processEvents()

    assert tuple(dialog._cards) == tuple(SettingsAction)
    assert [action.label for action in dialog._cards] == [
        "공통 제공기관",
        "생성 Excel 비밀번호",
        "청구서·확인서 양식",
        "출력 칼럼 연결",
        "식종별 배송 기본값",
        "문서 저장 폴더",
        "운영 데이터 위치",
        "자동 백업 폴더",
        "운영 파일 비밀번호 변경",
    ]
    assert not any("조식 배송 규칙" in action.label for action in SettingsAction)
    assert "공통 설정" in SettingsAction.DOCUMENT_DIRECTORY.scope_label
    assert "공통 설정" in SettingsAction.DATABASE_LOCATION.scope_label
    assert "공통 설정" in SettingsAction.BACKUP_DIRECTORY.scope_label
    with pytest.raises(RuntimeError, match="아직 선택"):
        _ = dialog.selected_action
    dialog.close()


@pytest.mark.parametrize("action", tuple(SettingsAction))
def test_settings_card_click_accepts_the_exact_job(app, action):
    dialog = SettingsHubDialog()

    dialog._cards[action].click()
    app.processEvents()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.selected_action is action


def test_directory_settings_dialog_requires_an_existing_folder(
    app,
    tmp_path: Path,
):
    chosen = tmp_path / "센터 문서"
    chosen.mkdir()
    picker_calls: list[tuple[str, str]] = []

    def picker(_parent, title, start):
        picker_calls.append((title, start))
        return str(chosen)

    dialog = DefaultDocumentDirectoryDialog(None, directory_picker=picker)
    assert not dialog._save_button.isEnabled()

    dialog.findChild(type(dialog._save_button), "quietAction").click()
    app.processEvents()

    assert picker_calls == [("기본 문서 폴더 선택", "")]
    assert dialog.selected_directory == chosen.resolve()
    assert dialog._save_button.isEnabled()
    assert chosen.name in dialog._name.text()
    dialog.close()


def test_settings_folder_job_saves_path_without_running_password_controller(
    tmp_path: Path,
):
    chosen = tmp_path / "문서함"
    chosen.mkdir()
    window = object()

    class Settings:
        document_output_directory = None

        def __init__(self):
            self.saved: Path | None = None

        def save_document_output_directory(self, directory):
            self.saved = Path(directory)

    settings = Settings()

    class Hub:
        selected_action = SettingsAction.DOCUMENT_DIRECTORY

        def __init__(self, parent=None):
            assert parent is window

        @staticmethod
        def exec():
            return QDialog.DialogCode.Accepted

    class Directory:
        selected_directory = chosen

        def __init__(self, current, parent=None):
            assert current is None
            assert parent is window

        @staticmethod
        def exec():
            return QDialog.DialogCode.Accepted

    controller = DesktopStartupController(
        settings=settings,
        settings_hub_dialog_type=Hub,
        document_directory_dialog_type=Directory,
        export_password_controller_type=lambda *_args: pytest.fail(
            "비밀번호 controller를 실행하면 안 됩니다"
        ),
    )

    controller._run_settings_hub(SimpleNamespace(), window)

    assert settings.saved == chosen


@pytest.mark.parametrize("accepted", [False, True])
def test_location_job_closes_window_only_after_success(accepted):
    session = object()
    settings = object()
    closed = []

    class Window:
        def close(self):
            closed.append(True)

    window = Window()

    class Hub:
        selected_action = SettingsAction.DATABASE_LOCATION

        def __init__(self, parent=None):
            assert parent is window

        def exec(self):
            return QDialog.DialogCode.Accepted

    class Location:
        def __init__(self, current_session, current_settings, parent=None):
            assert current_session is session
            assert current_settings is settings
            assert parent is window

        def exec(self):
            return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

    controller = DesktopStartupController(
        settings=settings,
        settings_hub_dialog_type=Hub,
        database_location_dialog_type=Location,
    )
    controller._run_settings_hub(session, window)
    assert closed == ([True] if accepted else [])


@pytest.mark.parametrize("accepted", [False, True])
def test_backup_folder_job_keeps_window_open(accepted):
    session = object()
    settings = object()
    calls = []

    class Window:
        def close(self):
            calls.append("closed")

    window = Window()

    class Hub:
        selected_action = SettingsAction.BACKUP_DIRECTORY

        def __init__(self, parent=None):
            assert parent is window

        def exec(self):
            return QDialog.DialogCode.Accepted

    class BackupFolder:
        def __init__(self, current_session, current_settings, parent=None):
            assert current_session is session
            assert current_settings is settings
            assert parent is window

        def exec(self):
            calls.append("opened")
            return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

    controller = DesktopStartupController(
        settings=settings,
        settings_hub_dialog_type=Hub,
        backup_directory_dialog_type=BackupFolder,
    )
    controller._run_settings_hub(session, window)
    assert calls == ["opened"]


@pytest.mark.parametrize("accepted,changed,backup_error", [
    (False, False, None), (True, False, None),
    (True, True, None), (True, True, OSError("backup unavailable")),
])
def test_operating_password_route_reports_result_without_closing_window(accepted, changed, backup_error):
    session = object()
    calls = []

    class Window:
        def close(self):
            calls.append("closed")

    window = Window()

    class Hub:
        selected_action = SettingsAction.DATABASE_PASSWORD

        def __init__(self, parent=None):
            assert parent is window

        def exec(self):
            return QDialog.DialogCode.Accepted

    class Password:
        password_changed = changed
        backup_result = AutomaticBackupResult(error=backup_error)

        def __init__(self, current_session, parent=None):
            assert current_session is session
            assert parent is window

        def exec(self):
            return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

    class Messages:
        @staticmethod
        def information(_parent, title, body):
            calls.append(("information", title, body))

        @staticmethod
        def warning(_parent, title, body):
            calls.append(("warning", title, body))

    controller = DesktopStartupController(
        settings=SimpleNamespace(),
        settings_hub_dialog_type=Hub,
        database_password_dialog_type=Password,
        database_location_dialog_type=lambda *_args, **_kwargs: pytest.fail("location route opened"),
        export_password_controller_type=lambda *_args: pytest.fail("Excel password route opened"),
        message_box=Messages,
    )
    controller._run_settings_hub(session, window)
    if not accepted:
        assert calls == []
    elif not changed:
        assert len(calls) == 1 and calls[0][0] == "information"
        assert "변경 없음" in calls[0][1]
    elif backup_error:
        assert len(calls) == 1 and calls[0][0] == "warning"
        assert "운영 파일 비밀번호는 새 비밀번호로 변경됐습니다" in calls[0][2]
        assert "백업을 열던 비밀번호는 파일마다 다를 수 있습니다" in calls[0][2]
    else:
        assert len(calls) == 1 and calls[0][0] == "information"
        assert "변경됐습니다" in calls[0][2]
        assert "자동 백업 폴더가 설정되지 않아" in calls[0][2]


def test_operating_password_route_reports_partial_rotation(app, tmp_path):
    session = object()
    window = object()
    calls = []
    class Hub:
        selected_action = SettingsAction.DATABASE_PASSWORD
        def __init__(self, parent=None):
            assert parent is window
        def exec(self):
            return QDialog.DialogCode.Accepted
    class Password:
        password_changed = True
        backup_result = AutomaticBackupResult(rotation=BackupPasswordRotation(
            updated=(tmp_path / "updated.mealbackup",),
            failed=((tmp_path / "failed.mealbackup", "locked"),),
            unreadable=(tmp_path / "corrupt.mealbackup",),
        ))
        def __init__(self, current_session, parent=None):
            assert current_session is session and parent is window
        def exec(self):
            return QDialog.DialogCode.Accepted
    class Messages:
        @staticmethod
        def warning(_parent, title, body):
            calls.append((title, body))
    controller = DesktopStartupController(
        settings=SimpleNamespace(),
        settings_hub_dialog_type=Hub,
        database_password_dialog_type=Password,
        message_box=Messages,
    )
    controller._run_settings_hub(session, window)
    assert len(calls) == 1
    assert "변경 완료" in calls[0][0]
    assert "갱신: 1개, 실패: 1개, 확인 불가: 1개" in calls[0][1]
    assert "백업을 열던 비밀번호는 파일마다 다를 수 있습니다" in calls[0][1]


@pytest.mark.parametrize("completed", [False, True])
def test_output_mapping_setting_routes_without_reimport_and_refreshes_on_save(completed):
    session = object()
    window = object()
    roster = object()
    calls = []

    class Hub:
        selected_action = SettingsAction.DOCUMENT_COLUMN_MAPPING

        def __init__(self, parent=None):
            assert parent is window

        def exec(self):
            return QDialog.DialogCode.Accepted

    class MappingController:
        def __init__(self, current_session):
            assert current_session is session

        def run(self, parent):
            assert parent is window
            calls.append("mapping")
            return SimpleNamespace(completed=completed, roster=roster if completed else None)

    controller = DesktopStartupController(
        settings=SimpleNamespace(),
        settings_hub_dialog_type=Hub,
        document_column_mapping_controller_type=MappingController,
    )
    controller._replace_roster_if_changed = lambda s, w, r: calls.append((s, w, r))
    controller._run_settings_hub(session, window)
    assert calls == (["mapping", (session, window, roster)] if completed else ["mapping"])
