"""Household failures retain their identity through the worker and visible roster."""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import date
from threading import get_ident
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from src.desktop_v2 import document_generation as generation
from src.desktop_v2.document_hub import DocumentAction
from src.desktop_v2.roster_window import RosterTableModel
from src.domain.projections import (
    ROSTER_ENTITY_KINDS,
    roster_audit_changes,
    roster_from_historical_snapshot,
)
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.domain.roster import (
    Household, HouseholdMembership, InclusiveDateRange, Person, PersonProfile,
)
from src.storage.database_session import DatabaseSession
from tests.test_desktop_v2_document_generation import _initial_revision, PERSON_ID
from tests.test_document_generation_progress import Messages, configured_template, selection_for


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _invalid_revision(case):
    record = _initial_revision()
    source = HistoricalSnapshot(record.metadata.revision_id, {
        EntityRef(change.entity_kind, change.entity_id): change.after for change in record.changes
    })
    roster = roster_from_historical_snapshot(source)
    original = roster.memberships[0]
    second = Household(UUID("6d000000-0000-0000-0000-000000000099"))
    memberships = {
        "missing": (),
        "outside": (replace(original, period=InclusiveDateRange(date(2026, 7, 1), None)),),
        "multiple": (
            replace(original, period=InclusiveDateRange(None, date(2026, 6, 15))),
            HouseholdMembership(PERSON_ID, second.household_id,
                                InclusiveDateRange(date(2026, 6, 16), None)),
        ),
    }[case]
    roster = replace(roster, memberships=memberships, households=(*roster.households, second))
    return replace(record, changes=(
        *roster_audit_changes(None, roster),
        *(change for change in record.changes if change.entity_kind not in ROSTER_ENTITY_KINDS),
    ))


class _Parent(QWidget):
    def __init__(self, roster, location):
        super().__init__()
        self.gui = get_ident()
        extra = (
            Person(UUID("6d000000-0000-0000-0000-000000000010"), PersonProfile("가가")),
            Person(UUID("6d000000-0000-0000-0000-000000000011"), PersonProfile("홍하나")),
        )
        people = () if location == "deleted" else (*roster.persons, *extra)
        self.model = RosterTableModel(replace(roster, persons=people) if people else
                                      replace(roster, persons=(), memberships=(), enrollments=(), assignments=()))
        name_column = next(index for index, item in enumerate(self.model.columns) if item.key == "name")
        self.model.sort(name_column, Qt.SortOrder.AscendingOrder)
        if location == "filtered":
            self.model.set_search("홍")
        elif location == "hidden":
            self.model.set_search("가가")

    @property
    def table_model(self):
        assert get_ident() == self.gui, "visible model must only be read on the GUI thread"
        return self.model


@pytest.mark.parametrize(("case", "location", "row"), (
    ("missing", "sorted", 2),
    ("outside", "sorted", 2),
    ("missing", "filtered", 1),
    ("missing", "hidden", None),
    ("missing", "deleted", None),
    ("missing", "no_parent", None),
    ("missing", "plain_parent", None),
))
def test_real_worker_household_failure_reports_truthful_location_and_publishes_nothing(
    app, tmp_path, case, location, row,
):
    outputs, backups = tmp_path / "outputs", tmp_path / "backups"
    outputs.mkdir()
    backups.mkdir()
    with DatabaseSession.create(tmp_path / "documents.sqlite3", "password", backup_directory=backups) as session:
        session.append_revision(_invalid_revision(case))
        roster = session.database.current_roster()
        selection = selection_for(session, outputs, DocumentAction.INVOICE)
        backups_before = set(backups.iterdir())
        revisions_before = session.database.revision_summaries()
        messages = Messages(app)
        if location == "no_parent":
            parent = None
        elif location == "plain_parent":
            parent = QWidget()
        else:
            parent = _Parent(roster, location)

        def unexpected_writer(*args, **kwargs):
            pytest.fail("household failure must happen before publication")

        outcome = generation.DocumentGenerationController(
            session, message_box=messages, template_controller_type=configured_template,
            invoice_writer=unexpected_writer,
        ).run(selection, parent)

        assert outcome.status is generation.DocumentGenerationStatus.FAILED
        assert len(messages.errors) == 1 and not messages.warnings
        text = messages.errors[0][-1]
        assert "성명: 홍길동 / 생년월일: 2015-03-02 / 학교명: 푸른초" in text
        assert f"문서 원본 변경번호: {selection.document.period.source.display_code}" in text
        assert "문서 기간: 2026-06-01 ~ 2026-06-30" in text
        assert "원본 Excel 행·열 번호는 저장된 자료에서 확인할 수 없습니다" in text
        assert "확인할 저장 항목: 가구 소속 / 적용 시작일 / 종료일" in text
        assert str(PERSON_ID) not in text
        if row is None:
            assert "현재 화면 명단 행: 확인할 수 없습니다" in text
            assert "현재 화면 명단:" not in text
        else:
            assert f"현재 화면 명단: {row}행 (현재 검색·필터·정렬 기준)" in text
            assert parent.table_model.person_at(row - 1).person_id == PERSON_ID
            assert parent.table_model.headerData(row - 1, Qt.Orientation.Vertical) == row
        assert not list(outputs.iterdir())
        assert session.database.document_runs() == ()
        assert session.database.revision_summaries() == revisions_before
        assert set(backups.iterdir()) == backups_before
        if parent is not None:
            parent.close()


@pytest.mark.parametrize("action", [DocumentAction.INVOICE, DocumentAction.CONFIRMATION])
def test_real_worker_publishes_monthly_documents_after_household_change(app, tmp_path, action):
    import msoffcrypto

    outputs = tmp_path / "outputs"
    outputs.mkdir()
    with DatabaseSession.create(tmp_path / "documents.sqlite3", "password",
                                backup_directory=tmp_path / "backups") as session:
        session.append_revision(_invalid_revision("multiple"))
        roster = session.database.current_roster()
        revisions = session.database.revision_summaries()
        messages = Messages(app)
        outcome = generation.DocumentGenerationController(
            session, message_box=messages, template_controller_type=configured_template,
        ).run(selection_for(session, outputs, action))
        assert outcome.completed
        assert not messages.errors and not messages.warnings
        assert len(session.database.document_runs()) == 1
        assert session.database.current_roster() == roster
        assert session.database.revision_summaries() == revisions
        with outcome.result.output_path.open("rb") as stream:
            assert msoffcrypto.OfficeFile(stream).is_encrypted()
        session.database.check_integrity()
