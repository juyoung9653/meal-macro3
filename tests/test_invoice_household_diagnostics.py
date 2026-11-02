"""Legacy household dates never block the document worker or publication."""
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
def test_real_worker_publishes_without_household_date_repair(app, tmp_path, case, location, row):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    with DatabaseSession.create(tmp_path / "documents.sqlite3", "password", backup_directory=tmp_path / "backups") as session:
        session.append_revision(_invalid_revision(case))
        roster = session.database.current_roster()
        selection = selection_for(session, outputs, DocumentAction.INVOICE)
        revisions = session.database.revision_summaries()
        messages = Messages(app)
        parent = None if location == "no_parent" else QWidget() if location == "plain_parent" else _Parent(roster, location)
        outcome = generation.DocumentGenerationController(
            session, message_box=messages, template_controller_type=configured_template,
        ).run(selection, parent)
        assert outcome.completed
        assert not messages.errors and not messages.warnings
        assert outcome.result.output_path.exists()
        assert len(session.database.document_runs()) == 1
        assert session.database.current_roster() == roster
        assert session.database.revision_summaries() == revisions
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
