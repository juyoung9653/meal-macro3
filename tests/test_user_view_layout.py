"""Same-database appearance transfer keeps roster data and local filters intact."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.roster_window import RosterWindow
from src.desktop_v2.user_view_layout import UserViewLayoutController, UserViewLayoutDialog
from src.domain.revisions import RevisionActor
from src.storage.encrypted_database import EncryptedDatabase
from tests.test_desktop_v2_roster import _roster


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def window_at(path):
    return RosterWindow(_roster(), column_settings=QSettings(str(path), QSettings.Format.IniFormat),
                        view_settings_scope="same-database")


def appearance(window):
    window._column_layout.set_visible_keys({"name", "address", "mobile"})
    window._rename_column("address", "배송 주소")
    window._add_sheet("연락처", {"name", "mobile"})
    header = window._table.horizontalHeader()
    header.moveSection(header.visualIndex(1), 0)
    window._table.setColumnWidth(0, 239)
    return window.export_view_layout()


def test_transfer_applies_all_sheets_aliases_and_reopens_without_changing_filters(app, tmp_path):
    source = window_at(tmp_path / "source.ini")
    expected = appearance(source)
    destination = window_at(tmp_path / "destination.ini")
    destination._set_day(date(2026, 8, 17))
    destination.table_model.set_column_search("name", "대상")
    before_roster = destination.roster
    destination.import_view_layout(expected)
    assert destination.export_view_layout() == expected
    assert destination.table_model.service_day == date(2026, 8, 17)
    assert destination.table_model.selected_column_search("name") == "대상"
    assert destination.roster is before_roster
    destination.close()
    reopened = window_at(tmp_path / "destination.ini")
    assert reopened.export_view_layout() == expected
    # Importing default aliases must also clear aliases from a previous user.
    defaults = deepcopy(expected)
    defaults["aliases"] = {}
    reopened.import_view_layout(defaults)
    assert reopened.export_view_layout()["aliases"] == {}
    assert next(c.label for c in reopened.table_model.columns if c.key == "address") != "배송 주소"
    source.close()
    reopened.close()


def test_invalid_layout_is_rejected_before_any_local_change(app, tmp_path):
    window = window_at(tmp_path / "settings.ini")
    before = appearance(window)
    invalid = deepcopy(before)
    invalid["sheets"][0]["layout"]["widths"]["name"] = -1
    with pytest.raises(ValueError):
        window.import_view_layout(invalid)
    assert window.export_view_layout() == before
    window.close()


def test_profiles_are_db_scoped_user_scoped_persistent_and_not_revisions(app, tmp_path):
    window = window_at(tmp_path / "settings.ini")
    state = appearance(window)
    first, second = RevisionActor("같은 이름", "sid-1"), RevisionActor("같은 이름", "sid-2")
    path = tmp_path / "users.db"
    key = b"k" * 32
    with EncryptedDatabase.create(path, key) as db:
        assert db.save_user_view_layout(first, "PC1", state)
        assert not db.save_user_view_layout(first, "PC1", state)
        assert db.save_user_view_layout(second, "PC2", state)
        assert len(db.user_view_layouts()) == 2
        assert db.latest_revision() is None
        db.check_integrity()
        snapshot = db.snapshot_bytes()
    with EncryptedDatabase.open(path, key) as db:
        assert db.user_view_layouts()[0]["layout"] == state
    with EncryptedDatabase.create(tmp_path / "other.db", key) as other:
        assert other.user_view_layouts() == ()
    EncryptedDatabase.restore_snapshot(snapshot, tmp_path / "restored.db", key)
    with EncryptedDatabase.open(tmp_path / "restored.db", key) as restored:
        assert len(restored.user_view_layouts()) == 2
    window.close()


@pytest.mark.parametrize("accept", [False, True])
def test_controller_selects_other_user_cancel_or_apply_and_publishes_on_close(app, tmp_path, accept):
    source = window_at(tmp_path / "source.ini")
    expected = appearance(source)
    destination = window_at(tmp_path / "destination.ini")
    original = destination.export_view_layout()
    own, other = RevisionActor("현재 사용자", "own"), RevisionActor("다른 사용자", "other")
    with EncryptedDatabase.create(tmp_path / "users.db", b"k" * 32) as db:
        db.save_user_view_layout(other, "다른 PC", expected)
        class Choice:
            def __init__(self, profiles, parent):
                assert [p["sid"] for p in profiles] == ["other"]
                self.state = profiles[0]["layout"]
            def exec(self):
                return QDialog.Accepted if accept else QDialog.Rejected
            def selected_layout(self):
                return self.state
        failures = []
        messages = SimpleNamespace(critical=lambda *a: failures.append(a), warning=lambda *a: failures.append(a))
        controller = UserViewLayoutController(SimpleNamespace(database=db), destination, actor=own,
            computer_name="현재 PC", dialog_type=Choice, messages=messages)
        controller.publish()
        controller.run()
        assert not failures
        assert destination.export_view_layout() == (expected if accept else original)
        destination._add_sheet("내 추가 시트", {"name"})
        destination.close()
        own_profile = next(p for p in db.user_view_layouts() if p["sid"] == own.sid)
        assert own_profile["layout"] == destination.export_view_layout()
        assert next(p for p in db.user_view_layouts() if p["sid"] == other.sid)["layout"] == expected
    source.close()


def test_empty_import_dialog_explains_source_setup(app):
    dialog = UserViewLayoutDialog(())
    assert not dialog.apply_button.isEnabled()
    assert "같은 DB" in dialog.preview.toPlainText()
    dialog.close()
