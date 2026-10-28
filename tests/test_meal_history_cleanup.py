"""Cleanup is previewable, backed up, atomic and scoped by person and meal."""
from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest

from src.domain.projections import STATUS_EVENT_KIND, status_event_audit_change, status_event_state
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.domain.roster import Meal
from src.domain.status import MealStatus
from src.services.meal_history_cleanup import build_meal_history_cleanup, plan_meal_history_cleanup
from src.desktop_v2.meal_history import build_person_meal_history_rows
from src.storage.database_session import DatabaseSession
from tests.test_monthly_meal_history import _initial, _event, ACTOR, STAMP


def _source(actions, people=1):
    roster, records, states = _initial(people=people)
    for status, day in actions:
        _event(roster, records, states, status, date(2026, 10, day),
            date(2026, 10, day + 1) if status is MealStatus.PAUSED else None)
    return roster, records, states


@pytest.mark.parametrize("status", [MealStatus.STOPPED, MealStatus.DELIVERY])
def test_same_day_keeps_latest_then_consecutive_keeps_earliest_start(status):
    roster, records, states = _source([(status, 22), (status, 23), (status, 22), (status, 24)])
    snapshot = HistoricalSnapshot(records[-1].metadata.revision_id, states)
    plan = plan_meal_history_cleanup(snapshot, records)
    assert len(plan) == 3
    assert {item.retained.event_id for item in plan} == {records[3].changes[0].entity_id}
    assert {item.removed.event_id for item in plan} == {records[i].changes[0].entity_id for i in (1, 2, 4)}


def test_intervening_status_breaks_run_and_pause_is_never_removed():
    roster, records, states = _source([
        (MealStatus.STOPPED, 22), (MealStatus.DELIVERY, 23), (MealStatus.STOPPED, 24),
        (MealStatus.PAUSED, 25), (MealStatus.STOPPED, 26),
    ])
    assert plan_meal_history_cleanup(HistoricalSnapshot(records[-1].metadata.revision_id, states), records) == ()


def test_people_and_meals_do_not_share_duplicate_groups():
    roster, records, states = _source([(MealStatus.STOPPED, 22)], people=2)
    other_person = _event(roster, records, states, MealStatus.STOPPED, date(2026, 10, 22), person=1)
    another = replace(other_person, event_id=uuid4(), meal=Meal.BREAKFAST,
                      person_id=roster.persons[0].person_id, revision_id=uuid4())
    records.append(replace(records[-1], metadata=replace(records[-1].metadata,
        revision_id=another.revision_id, parent_revision_id=records[-1].metadata.revision_id),
        changes=(status_event_audit_change(another),)))
    states[EntityRef(STATUS_EVENT_KIND, another.event_id)] = status_event_state(another)
    assert plan_meal_history_cleanup(HistoricalSnapshot(records[-1].metadata.revision_id, states), records) == ()


def test_finite_records_that_add_dates_are_kept():
    roster, records, states = _initial()
    _event(roster, records, states, MealStatus.STOPPED, date(2026, 10, 22), date(2026, 10, 23))
    _event(roster, records, states, MealStatus.STOPPED, date(2026, 10, 23), date(2026, 10, 25))
    assert plan_meal_history_cleanup(HistoricalSnapshot(records[-1].metadata.revision_id, states), records) == ()


def test_storage_validates_cleanup_and_keeps_enrollments_and_immutable_records(tmp_path):
    roster, records, states = _source([(MealStatus.DELIVERY, 22), (MealStatus.DELIVERY, 22),
                                     (MealStatus.DELIVERY, 23)])
    path = tmp_path / "cleanup.sqlite3"
    with DatabaseSession.create(path, "password") as session:
        for record in records:
            session.append_revision(record)
        latest = session.database.latest_revision_id()
        snapshot = session.database.snapshot(latest)
        chain = session.database.branch_revision_chain(latest)
        prepared = build_meal_history_cleanup(snapshot, chain, actor=ACTOR,
            recorded_at=STAMP, revision_id=uuid4())
        invalid = replace(prepared, changes=prepared.changes[:-1])
        with pytest.raises(ValueError, match="저장 내용"):
            session.append_revision(invalid)
        assert session.database.latest_revision_id() == latest
        session.append_revision(prepared)
        assert session.database.current_roster().enrollments == roster.enrollments
        assert len(session.database.status_events()) == 1
        new_chain = session.database.branch_revision_chain(prepared.metadata.revision_id)
        assert len(build_person_meal_history_rows(new_chain, roster.persons[0].person_id)) == 1
        assert session.database.revision(records[1].metadata.revision_id).changes == records[1].changes
        assert plan_meal_history_cleanup(session.database.snapshot(prepared.metadata.revision_id), new_chain) == ()
    with DatabaseSession.open(path, "password") as session:
        assert len(session.database.status_events()) == 1
        assert session.database.current_roster().enrollments == roster.enrollments


@pytest.mark.parametrize("apply", [False, True])
def test_cli_password_prompt_preview_apply_and_restorable_backup(tmp_path, monkeypatch, capsys, apply):
    from src.tools import cleanup_meal_history as cli
    roster, records, states = _source([(MealStatus.STOPPED, 22), (MealStatus.STOPPED, 23)])
    path = tmp_path / "cli.sqlite3"
    password = "synthetic-password-never-print"
    with DatabaseSession.create(path, password) as session:
        for record in records:
            session.append_revision(record)
    prompts = []
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: prompts.append(prompt) or password)
    arguments = [str(path)] + (["--apply"] if apply else [])
    assert cli.main(arguments) == 0
    assert len(prompts) == 1
    assert password not in capsys.readouterr().out
    backups = sorted((tmp_path / "meal-history-cleanup-backups").rglob("*.mealreplica"))
    with DatabaseSession.open(path, password) as session:
        assert len(session.database.status_events()) == (1 if apply else 2)
        assert (session.database.latest_revision_id() != records[-1].metadata.revision_id) is apply
    assert bool(backups) is apply
    if apply:
        from src.storage.database_backup import inspect_backup
        before = next(path for path in backups if inspect_backup(path).latest_revision_id == records[-1].metadata.revision_id)
        with DatabaseSession.restore_and_open_with_user_password(before, tmp_path / "restored.sqlite3", password) as restored:
            assert len(restored.database.status_events()) == 2


def test_backup_failure_prevents_cleanup(tmp_path, monkeypatch, capsys):
    from src.tools import cleanup_meal_history as cli
    roster, records, _ = _source([(MealStatus.STOPPED, 22), (MealStatus.STOPPED, 23)])
    path = tmp_path / "backup-failure.sqlite3"
    with DatabaseSession.create(path, "password") as session:
        for record in records:
            session.append_revision(record)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "password")
    def fail_backup(*args):
        raise OSError("합성 백업 실패")
    monkeypatch.setattr(DatabaseSession, "backup_now", fail_backup)
    assert cli.main([str(path), "--apply"]) == 1
    assert "합성 백업 실패" in capsys.readouterr().out
    with DatabaseSession.open(path, "password") as session:
        assert session.database.latest_revision_id() == records[-1].metadata.revision_id
        assert len(session.database.status_events()) == 2


@pytest.mark.parametrize("result", [0, 1])
def test_interactive_cli_waits_after_success_or_failure(monkeypatch, result):
    from src.tools import cleanup_meal_history as cli
    prompts = []
    monkeypatch.setattr(cli, "main", lambda argv: result)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")
    assert cli.run_cli([]) == result
    assert len(prompts) == 1 and "Enter" in prompts[0]


def test_interactive_cli_waits_on_invalid_database_path(tmp_path, monkeypatch, capsys):
    from src.tools import cleanup_meal_history as cli
    prompts = []
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")
    assert cli.run_cli([str(tmp_path / "missing.sqlite3")]) == 2
    assert len(prompts) == 1
    assert "운영 DB 파일을 찾을 수 없습니다" in capsys.readouterr().err


@pytest.mark.parametrize("interactive,arguments", [(False, []), (True, ["--no-pause"])])
def test_noninteractive_and_launcher_do_not_wait_twice(monkeypatch, interactive, arguments):
    from src.tools import cleanup_meal_history as cli
    monkeypatch.setattr(cli, "main", lambda argv: 0)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: interactive)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("unexpected pause"))
    assert cli.run_cli(arguments) == 0


def test_wrong_password_reports_error_and_waits_without_changes(tmp_path, monkeypatch, capsys):
    from src.tools import cleanup_meal_history as cli
    roster, records, _ = _source([(MealStatus.STOPPED, 22), (MealStatus.STOPPED, 23)])
    path = tmp_path / "wrong-password.sqlite3"
    with DatabaseSession.create(path, "correct-password") as session:
        for record in records:
            session.append_revision(record)
    prompts = []
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "wrong-password-never-print")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")
    assert cli.run_cli([str(path), "--apply"]) == 1
    output = capsys.readouterr().out
    assert "CredentialUnlockError" in output
    assert "wrong-password-never-print" not in output
    assert len(prompts) == 1
    with DatabaseSession.open(path, "correct-password") as session:
        assert len(session.database.status_events()) == 2
