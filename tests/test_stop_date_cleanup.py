"""Synthetic coverage for selected dates, legacy history and the portable console."""
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts import cleanup_september30_stops as cli
from src.domain.projections import roster_audit_changes
from src.domain.revisions import RevisionRecord
from src.domain.roster import InclusiveDateRange, Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot
from src.domain.status import MealStatus
from src.services.revision_restore import restore_revision
from src.services.september30_cleanup import RESET_DECISION, StopDate, build_cleanup, find_targets
from src.storage.database_session import DatabaseSession
from tests.test_cleanup_september30_stops import ACTOR, _event, _metadata


@pytest.mark.parametrize("text,selected", [
    ("2026-10-01", StopDate(10, 1, 2026)), ("10/1", StopDate(10, 1)),
    (" 09/30 ", StopDate(9, 30)), ("2/29", StopDate(2, 29)),
    ("2024-02-29", StopDate(2, 29, 2024)),
])
def test_date_input_and_serialization(text, selected):
    assert StopDate.parse(text) == selected
    assert StopDate.from_state(selected.state()) == selected


@pytest.mark.parametrize("text", ["", "2026-02-29", "13/1", "4/31", "9-30", "2026-9-30", "2026-09-30/", "0000-09-30"])
def test_invalid_date_is_actionable_and_never_defaults_to_september30(text):
    from src.desktop_v2.user_messages import user_error
    with pytest.raises(ValueError) as error:
        StopDate.parse(text)
    message = user_error(error.value)
    assert "입력해주세요" in message or "확인해주세요" in message
    assert "2026-09-30" in message and "9/30" in message


@pytest.mark.parametrize("state", [None, {}, {"year": None, "month": 13, "day": 1},
                                    {"year": True, "month": 9, "day": 30}])
def test_invalid_saved_date_is_rejected(state):
    with pytest.raises(ValueError):
        StopDate.from_state(state)


def initialize(session):
    people = tuple(Person(uuid4(), PersonProfile(name)) for name in ("해당연도", "다른연도", "다른날짜"))
    roster = RosterSnapshot(uuid4(), people, enrollments=tuple(
        MealEnrollment(p.person_id, meal, InclusiveDateRange(date(2024, 1, 1)))
        for p in people for meal in (Meal.DINNER, Meal.BREAKFAST)))
    session.database.append_revision(RevisionRecord(_metadata(roster.revision_id, None), roster_audit_changes(None, roster)))
    for person, day in zip(people, (date(2026, 10, 1), date(2025, 10, 1), date(2026, 9, 30))):
        _event(session, person.person_id, Meal.DINNER, MealStatus.STOPPED, day)
    _event(session, people[0].person_id, Meal.DINNER, MealStatus.DELIVERY, date(2026, 10, 2))
    return people


@pytest.mark.parametrize("selected,indexes", [(StopDate(10, 1, 2026), (0,)), (StopDate(10, 1), (0, 1))])
def test_selected_date_is_saved_reopened_and_restored_without_touching_other_meals(tmp_path, selected, indexes):
    path = tmp_path / "operations.sqlite3"
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(path, "password", backup_directory=backups) as session:
        people = initialize(session)
        head = session.database.latest_revision_id()
        before = session.database.snapshot(head)
        targets = frozenset((str(people[i].person_id), Meal.DINNER.value) for i in indexes)
        assert find_targets(session.database.branch_revision_chain(head), stop_date=selected) == targets
        record, backup = cli.cleanup_in_place(session, actor=ACTOR, stop_date=selected)
        assert record.metadata.decisions[RESET_DECISION]["stop_date"] == selected.state()
        assert selected.label in record.metadata.reason
        assert backup.succeeded
        assert cli.cleanup_in_place(session, actor=ACTOR, stop_date=selected) == (None, None)
        clean_head = record.metadata.revision_id
    with DatabaseSession.open(path, "password", backup_directory=backups) as session:
        session.database.check_integrity()
        roster = session.database.current_roster()
        for i, person in enumerate(people):
            expected = {Meal.BREAKFAST} if i in indexes else {Meal.DINNER, Meal.BREAKFAST}
            assert set(roster.meals_for(person.person_id, on=date(2026, 10, 5))) == expected
        assert session.database.snapshot(head).entities == before.entities
        restored = restore_revision(session, head, clean_head, ACTOR)
        assert session.database.snapshot(restored.revision_id).entities == before.entities
        assert restored.backup.succeeded
        session.database.check_integrity()


def test_no_matching_date_does_not_add_revision_and_different_dates_can_be_cleaned(tmp_path):
    with DatabaseSession.create(tmp_path / "operations.sqlite3", "password") as session:
        people = initialize(session)
        head = session.database.latest_revision_id()
        assert cli.cleanup_in_place(session, actor=ACTOR, stop_date=StopDate(12, 31)) == (None, None)
        assert session.database.latest_revision_id() == head
        first, _ = cli.cleanup_in_place(session, actor=ACTOR, stop_date=StopDate(10, 1, 2026))
        second, _ = cli.cleanup_in_place(session, actor=ACTOR, stop_date=StopDate(9, 30, 2026))
        assert second.metadata.parent_revision_id == first.metadata.revision_id
        roster = session.database.current_roster()
        assert roster.meals_for(people[0].person_id, on=date(2026, 10, 5)) == (Meal.BREAKFAST,)
        assert set(roster.meals_for(people[1].person_id, on=date(2026, 10, 5))) == {Meal.DINNER, Meal.BREAKFAST}
        assert roster.meals_for(people[2].person_id, on=date(2026, 10, 5)) == (Meal.BREAKFAST,)
        session.database.check_integrity()


def test_tampering_with_saved_date_rejects_the_entire_revision(tmp_path):
    with DatabaseSession.create(tmp_path / "operations.sqlite3", "password") as session:
        initialize(session)
        head = session.database.latest_revision_id()
        before = session.database.snapshot(head)
        record = build_cleanup(before, session.database.branch_revision_chain(head), actor=ACTOR,
            recorded_at=datetime.now(UTC), revision_id=uuid4(), stop_date=StopDate(10, 1, 2026))
        decision = dict(record.metadata.decisions[RESET_DECISION])
        decision["stop_date"] = StopDate(10, 1, 2025).state()
        bad = replace(record, metadata=replace(record.metadata, decisions={RESET_DECISION: decision}))
        with pytest.raises(ValueError, match="정리 대상"):
            session.append_revision(bad)
        assert session.database.latest_revision_id() == head
        assert session.database.snapshot(head).entities == before.entities
        session.database.check_integrity()


@pytest.mark.parametrize("confirmation,changed", [("취소", False), ("정리", True)])
def test_interactive_date_preview_and_explicit_confirmation(tmp_path, monkeypatch, capsys, confirmation, changed):
    path = tmp_path / "operations.sqlite3"
    with DatabaseSession.create(path, "password") as session:
        people = initialize(session)
        head = session.database.latest_revision_id()
    answers = iter(["2026-02-30", "2026-10-01", str(path), confirmation])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(cli, "getpass", lambda prompt: "password")
    monkeypatch.setattr(cli, "configured_backup", lambda path: None)
    monkeypatch.setattr("src.desktop_v2.import_controller.current_windows_actor", lambda: ACTOR)
    assert cli.run(["--no-pause"]) == 0
    output = capsys.readouterr().out
    assert "정리 대상 1건" in output and "2026년 10월 1일" in output
    assert "해당연도 / 석식" in output and "다른연도 / 석식" not in output
    assert "실제 날짜" in output
    with DatabaseSession.open(path, "password") as session:
        assert (session.database.latest_revision_id() != head) == changed
        assert (Meal.DINNER not in session.database.current_roster().meals_for(people[0].person_id, on=date(2026, 10, 5))) == changed


def test_blank_date_cancels_before_opening_a_file(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    def unexpected(*args, **kwargs):
        pytest.fail("Cancellation must not ask for a password")
    monkeypatch.setattr(cli, "getpass", unexpected)
    assert cli.run(["--no-pause"]) == 0
    assert "취소했습니다" in capsys.readouterr().out


def test_console_errors_use_common_message_without_internal_details(monkeypatch, capsys):
    def fail(**kwargs):
        raise RuntimeError("private_internal_error 01234567-89ab-cdef-0123-456789abcdef")
    monkeypatch.setattr(cli, "main", fail)
    assert cli.run(["--no-pause", "--stop-date", "2026-10-01"]) == 1
    output = capsys.readouterr().out
    assert "다시 시도해주세요" in output
    assert "private_internal" not in output and "Traceback" not in output


def test_frozen_tool_finds_settings_beside_executable_without_changing_them(tmp_path, monkeypatch):
    import runpy
    import sys
    from PySide6.QtCore import QSettings
    base = tmp_path / "portable"
    (base / "data").mkdir(parents=True)
    path = base / "operations.sqlite3"
    settings_path = base / "data" / "settings.ini"
    settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    settings.setValue("storage/database_path", "operations.sqlite3")
    settings.setValue("storage/backup_directory", "backups")
    settings.sync()
    before = settings_path.read_bytes()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(base / "meal_stop_cleanup.exe"))
    loaded = runpy.run_path(str(Path(cli.__file__)))
    assert loaded["ROOT"] == base
    assert loaded["configured_backup"](path) == base / "backups"
    assert settings_path.read_bytes() == before


def test_redirected_windows_console_preserves_unicode(monkeypatch):
    import io
    import sys
    buffer = io.BytesIO()
    output = io.TextIOWrapper(buffer, encoding="cp949")
    monkeypatch.setattr(cli, "main", lambda **kwargs: print("합성 이름 — 😀"))
    with monkeypatch.context() as context:
        context.setattr(sys, "stdout", output)
        assert cli.run(["--no-pause", "--stop-date", "2026-10-01"]) == 0
    output.flush()
    assert buffer.getvalue().decode("utf-8").strip() == "합성 이름 — 😀"
