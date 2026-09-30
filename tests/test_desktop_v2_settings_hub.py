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
        "문서 저장 폴더",
    ]
    assert not any("조식 배송 규칙" in action.label for action in SettingsAction)
    assert "이 컴퓨터" in SettingsAction.DOCUMENT_DIRECTORY.scope_label
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
